# -*- coding: utf-8 -*-
"""
SessionStart Hook 函数 — 检测项目元信息（类型、可用命令、test/lint、README、License）

从 project_root 读取本地配置文件，零外部依赖（不调 subprocess），
通过 30s TTL 进程级缓存避免短时间内多次 SessionStart 重复扫描。

输出紧凑的 markdown 块，让 LLM 在首条消息时就能掌握项目结构。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# ── 项目类型识别（按 marker 文件匹配）──────────────────────────────────
# (显示名, marker 列表) — 第一个命中的优先
_PROJECT_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("Python", ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile")),
    ("Node.js", ("package.json",)),
    ("Deno", ("deno.json", "deno.jsonc")),
    ("Go", ("go.mod",)),
    ("Rust", ("Cargo.toml",)),
    ("Java Maven", ("pom.xml",)),
    ("Java Gradle", ("build.gradle", "build.gradle.kts")),
    ("Ruby", ("Gemfile",)),
    ("Elixir", ("mix.exs",)),
    ("PHP Composer", ("composer.json",)),
    ("DriFox", (".drifox/",)),
]

# ── 输出容量控制 ──────────────────────────────────────────────────────
_MAX_COMMANDS_PER_SOURCE = 15
_MAX_README_CHARS = 300
_MAX_LICENSE_CHARS = 80

# ── 进程级 TTL 缓存（key=project_root）────────────────────────────────
_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 30.0


def _safe_read(path: Path, limit: int = 0) -> str:
    """读取文本文件，失败/超大时返回空串"""
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        if limit and len(text) > limit:
            text = text[:limit]
        return text
    except OSError:
        return ""


def _detect_project_types(root: Path) -> list[tuple[str, str]]:
    """返回 [(显示名, 命中的 marker 文件), ...]"""
    found: list[tuple[str, str]] = []
    for display, markers in _PROJECT_MARKERS:
        for m in markers:
            if (root / m).exists():
                found.append((display, m))
                break
    return found


def _extract_makefile_targets(content: str) -> list[str]:
    """Makefile: 提取顶层 target（行首字母+冒号，且不是以 . 开头）"""
    targets: list[str] = []
    for line in content.splitlines():
        # 跳过以 Tab 开头的（命令）、以 # 开头的（注释）、以 . 开头的（.PHONY 等内置）
        if not line or line.startswith(("\t", "#", ".")):
            continue
        m = re.match(r"^([A-Za-z0-9_./-]+)\s*:", line)
        if m:
            targets.append(m.group(1))
    return targets


def _extract_package_json_scripts(root: Path) -> list[str]:
    """package.json: 提取 scripts 段 key"""
    text = _safe_read(root / "package.json")
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    scripts = data.get("scripts") or {}
    return list(scripts.keys())


def _extract_pyproject_scripts(root: Path) -> list[str]:
    """pyproject.toml: 提取 [project.scripts] 和 [project.gui-scripts] 段（不依赖 toml 库）"""
    text = _safe_read(root / "pyproject.toml")
    if not text:
        return []
    scripts: list[str] = []
    in_scripts = False
    in_gui = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_scripts = stripped in ("[project.scripts]", "[tool.poetry.scripts]")
            in_gui = stripped == "[project.gui-scripts]"
            continue
        if (in_scripts or in_gui) and "=" in stripped and not stripped.startswith("#"):
            name = stripped.split("=", 1)[0].strip()
            if name:
                scripts.append(name)
    return scripts


def _detect_test_lint(root: Path, types: list[tuple[str, str]]) -> list[str]:
    """从 pyproject.toml / package.json scripts 中检测 test/lint 关键字"""
    tools: list[str] = []

    # Python: 检查 pyproject.toml 里的 ruff/mypy/pytest 配置段
    pyproject = _safe_read(root / "pyproject.toml")
    if pyproject:
        for tool in ("ruff", "mypy", "pytest", "black", "flake8", "pylint"):
            if re.search(rf"\[tool\.{tool}\]", pyproject):
                tools.append(tool)
            elif f'"{tool}' in pyproject and tool in ("pytest", "ruff"):
                tools.append(tool)

    # Node: package.json scripts 里包含 test/lint
    pkg_text = _safe_read(root / "package.json")
    if pkg_text:
        try:
            scripts = (json.loads(pkg_text) or {}).get("scripts") or {}
        except json.JSONDecodeError:
            scripts = {}
        for name, cmd in scripts.items():
            low = name.lower()
            if "test" in low or "lint" in low or "check" in low or "typecheck" in low:
                tools.append(f"npm run {name}")

    return tools


def _extract_readme_summary(root: Path) -> str:
    """README 摘要：优先 README.md，回退 README.rst/README"""
    for name in ("README.md", "README.rst", "README"):
        text = _safe_read(root / name, limit=_MAX_README_CHARS + 50)
        if not text:
            continue
        # 去前导标题/空行/水平线
        lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "---", "===")):
                if lines:  # 第一个内容前的标题直接跳过
                    break
                continue
            lines.append(stripped)
            if sum(len(s) for s in lines) >= _MAX_README_CHARS:
                break
        summary = " ".join(lines)
        if len(summary) > _MAX_README_CHARS:
            summary = summary[:_MAX_README_CHARS].rsplit(" ", 1)[0] + "..."
        return summary
    return ""


def _detect_license(root: Path) -> str:
    """从 LICENSE / LICENSE.md 提取首行（通常是 license 名）"""
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
        text = _safe_read(root / name, limit=_MAX_LICENSE_CHARS)
        if not text:
            continue
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if first:
            # 常见模式："MIT License" / "Apache License, Version 2.0" / "GNU General Public License v3.0"
            return first[:_MAX_LICENSE_CHARS]
    return ""


def _build_intel_block(root: Path) -> str:
    """根据 root 目录生成项目情报 markdown 块"""
    types = _detect_project_types(root)
    if not types:
        return ""

    parts: list[str] = ["## 项目元信息", f"[工作目录: {root}]"]

    # 类型
    type_lines = [f"- {name}（{marker}）" for name, marker in types]
    parts.append("### 类型\n" + "\n".join(type_lines))

    # 可用命令
    cmd_lines: list[str] = []
    makefile_targets = _extract_makefile_targets(_safe_read(root / "Makefile"))
    if makefile_targets:
        cmd_lines.append(f"- **Makefile**: `{'`, `'.join(makefile_targets[:_MAX_COMMANDS_PER_SOURCE])}`")

    pkg_scripts = _extract_package_json_scripts(root)
    if pkg_scripts:
        cmd_lines.append(f"- **package.json scripts**: `{'`, `'.join(pkg_scripts[:_MAX_COMMANDS_PER_SOURCE])}`")

    pyp_scripts = _extract_pyproject_scripts(root)
    if pyp_scripts:
        cmd_lines.append(f"- **pyproject.toml scripts**: `{'`, `'.join(pyp_scripts[:_MAX_COMMANDS_PER_SOURCE])}`")

    if cmd_lines:
        parts.append("### 可用命令\n" + "\n".join(cmd_lines))

    # 测试与 Lint
    tools = _detect_test_lint(root, types)
    if tools:
        parts.append("### 测试与 Lint\n" + "\n".join(f"- {t}" for t in tools))

    # README 摘要
    readme = _extract_readme_summary(root)
    if readme:
        parts.append(f"### README 摘要\n{readme}")

    # License
    license_name = _detect_license(root)
    if license_name:
        parts.append(f"### License\n{license_name}")

    return "\n".join(parts)


def hook(event: str, context: dict) -> str:
    """检测项目元信息并格式化为 SessionStart hook 输出

    Args:
        event: 事件名称（SessionStart）
        context: 由 backend 预取的上下文，含：
            - project_root: str 项目工作目录
            - state: str 会话状态（startup/resume/clear/compact）

    Returns:
        格式化的项目情报字符串，无项目时返回空字符串
    """
    root_str = context.get("project_root", "")
    if not root_str:
        # compaction/subagent/gateway 等无真实项目上下文 → 跳过注入
        return ""

    root = Path(root_str)

    # 缓存命中 → 直接返回
    import time as _time

    now = _time.monotonic()
    cached = _CACHE.get(root_str)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    try:
        block = _build_intel_block(root)
    except Exception as e:
        # 任意异常都不应阻塞会话启动
        from loguru import logger

        logger.warning(f"[ProjectIntelHook] 检测项目元信息失败: {root}: {e}")
        block = ""

    _CACHE[root_str] = (now, block)
    return block
