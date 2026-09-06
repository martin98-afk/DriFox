# -*- coding: utf-8 -*-
"""插件依赖统一加载器（deps_loader）

设计文档：docs/superpowers/specs/2026-08-27-plugin-platform-deps-design.md

职责（宿主侧单一入口）：
1. 平台声明检查：解析 plugin.json 的 platforms 字段，判断当前系统是否兼容
2. deps 路径注入：把插件自包含依赖目录幂等注入 sys.path
3. pip 依赖解析：合并 dependencies.pip 的 default 与当前平台列表

deps 目录规范：
    <plugin>/
      deps/            # 平台无关（纯 Python 包）
      deps/win32/      # Windows 二进制（.pyd/.dll），按 sys.platform 值命名
      deps/linux/
      deps/darwin/

注入顺序：deps/<platform>/ 先于 deps/（平台特定优先，同名包取平台版本）。
兼容存量：仅 deps/ 的老插件行为不变；两者皆无则不注入。

平台名映射（单一事实源）：
    plugin.json platforms 声明用友好名：windows / linux / darwin
    目录名与 dependencies.pip key 用 sys.platform 原值：win32 / linux / darwin
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger

# P1-4：pip spec 白名单——首字符字母数字（拒 -flag 与 . 开头）、包名 + extras 可选 +
# 版本约束可选（裸包名合法）；另拒常见文件扩展名（requirements.txt 等间接注入链）
_PIP_SPEC_RE = re.compile(r"^(?!-)[A-Za-z0-9_][\w.\[\]-]*(\[\w[.\w-]*\])?((==|>=|<=|~=|!=|>|<)\d[\d.*]*)?$")
_PIP_FILE_EXT_RE = re.compile(r"\.(txt|whl|tar|gz|zip|git|json|cfg|ini|yml|yaml)$", re.IGNORECASE)

# 友好名 → sys.platform 值（目录名 / pip key 用）
PLATFORM_DIR_MAP = {
    "windows": "win32",
    "win32": "win32",  # 容忍直接写 sys.platform 值
    "linux": "linux",
    "darwin": "darwin",
    "macos": "darwin",
    "mac": "darwin",
}

# 当前平台的 sys.platform 值（win32 / linux / darwin / 其它原值）
CURRENT_PLATFORM = sys.platform


def current_platform_key() -> str:
    """返回当前平台的 deps 目录名 / pip key（sys.platform 原值，win 归一为 win32）。"""
    return PLATFORM_DIR_MAP.get(CURRENT_PLATFORM, CURRENT_PLATFORM)


def normalize_platforms(manifest: dict) -> Optional[List[str]]:
    """解析 manifest 的 platforms 字段为 sys.platform 值列表。

    返回 None 表示未声明（= 全平台兼容，存量插件零改动）。
    非法值（未知平台名）记 warning 并忽略该项。
    """
    raw = manifest.get("platforms")
    if not raw:
        return None
    if not isinstance(raw, list):
        logger.warning(f"[deps_loader] platforms 字段须为数组，忽略: {raw!r}")
        return None
    keys: List[str] = []
    for name in raw:
        if not isinstance(name, str):
            continue
        key = PLATFORM_DIR_MAP.get(name.strip().lower())
        if key is None:
            logger.warning(f"[deps_loader] 未知平台名 {name!r}（支持 windows/linux/darwin），忽略")
            continue
        if key not in keys:
            keys.append(key)
    return keys or None


def check_platform(manifest: dict) -> Tuple[bool, str]:
    """检查插件是否兼容当前平台。

    Returns:
        (兼容?, 原因文案)。未声明 platforms → 恒兼容（向后兼容）。
    """
    keys = normalize_platforms(manifest)
    if keys is None:
        return True, ""
    if current_platform_key() in keys:
        return True, ""
    declared = ", ".join(keys)
    return False, f"插件仅支持 [{declared}]，当前平台 {current_platform_key()} 不兼容"


def resolve_pip_deps(manifest: dict, platform_key: Optional[str] = None) -> List[str]:
    """合并 dependencies.pip 的 default 与指定平台列表（去重、保序）。

    P1-4：PEP 508 白名单校验——包名+extras 可选+版本约束必需；
    git+/file:///-r/--flag 等 注入形态拒收（移除 + warning）。

    Args:
        manifest: 插件清单
        platform_key: 平台 key，缺省用当前平台

    Returns:
        合并后的依赖规格列表（如 ["httpx>=0.27", "pywin32"]）；无声明返回空。
    """
    deps = manifest.get("dependencies") or {}
    pip = deps.get("pip")
    if not isinstance(pip, dict):
        return []
    key = platform_key or current_platform_key()

    merged: List[str] = []

    def _add(specs) -> None:
        if isinstance(specs, str):
            specs = [specs]
        if not isinstance(specs, list):
            return
        for s in specs:
            if isinstance(s, str) and s and s not in merged:
                # P1-4：spec 白名单 + 文件扩展名拒绝（-r 间接注入链）
                if not _PIP_SPEC_RE.match(s) or _PIP_FILE_EXT_RE.search(s):
                    logger.warning(
                        f"[deps_loader] 拒收非法 pip spec（白名单外，疑似注入/非固定版本）: {s!r}"
                    )
                    continue
                merged.append(s)

    _add(pip.get("default"))
    _add(pip.get(key))
    return merged


def deps_paths(plugin_dir: Path) -> List[Path]:
    """返回插件应注入的 deps 路径列表（仅实际存在的，按优先级排序）。

    顺序即 sys.path 优先级：平台特定目录在前，公共 deps/ 在后。
    """
    if not plugin_dir.is_dir():
        return []
    paths: List[Path] = []
    plat = plugin_dir / "deps" / current_platform_key()
    if plat.is_dir():
        paths.append(plat)
    common = plugin_dir / "deps"
    # 公共 deps/ 必须是目录且非空才有意义；空目录不注入
    if common.is_dir() and any(common.iterdir()):
        # 平台目录是 deps/ 的子目录，已单独注入；公共注入仍保留（纯 Python 包在顶层）
        paths.append(common)

    # —— 安全：剔除会劫持 stdlib / 宿主包的 deps 目录 ——
    # deps 位于 sys.path[0]（优先于 stdlib），deps/json.py 即可替换全进程
    # 的 json 模块。此处按目录粒度拒注入并告警，不影响其余 deps 生效。
    safe: List[Path] = []
    for d in paths:
        shadowed = _shadowing_entries(d)
        if shadowed:
            logger.warning(
                f"[deps_loader] {plugin_dir.name} 的 {d.name} 含与 stdlib/宿主同名的顶层模块 "
                f"{shadowed}，已拒绝注入 sys.path（疑似依赖劫持）"
            )
            continue
        safe.append(d)
    return safe


def _stdlib_toplevel_names() -> frozenset:
    """stdlib 顶层模块名集合（sys.stdlib_module_names，3.10+）。

    用于识别 deps/ 里的"同名劫持"：deps 被 insert 到 sys.path[0]，
    若其中存在 json.py / os.py / typing.py 等，会整体替换全进程对 stdlib 的导入。
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    return frozenset()


def _host_toplevel_names() -> frozenset:
    """宿主自身顶层包名（app / plugins）— 同样禁止被 deps 覆盖。"""
    return frozenset({"app", "plugins"})


def _shadowing_entries(dep_dir: Path) -> List[str]:
    """返回 dep_dir 下会劫持 stdlib / 宿主包的顶层模块名列表。"""
    banned = _stdlib_toplevel_names() | _host_toplevel_names()
    if not banned:
        return []
    hits: List[str] = []
    try:
        for entry in dep_dir.iterdir():
            stem = entry.name
            is_pkg = entry.is_dir() and (entry / "__init__.py").exists()
            if entry.is_file() and entry.suffix == ".py":
                stem = entry.stem
            elif not is_pkg:
                continue
            if stem in banned:
                hits.append(stem)
    except OSError as e:
        logger.debug(f"[deps_loader] 扫描 deps 目录失败 {dep_dir}: {e}")
    return sorted(set(hits))


def ensure_deps_on_path(plugin_dir: Path) -> List[str]:
    """幂等注入插件的 deps 路径到 sys.path（平台目录优先）。

    Returns:
        本次实际注入的路径列表（已在 sys.path 中的不重复注入）。
    """
    injected: List[str] = []
    for p in reversed(deps_paths(plugin_dir)):
        s = str(p)
        if s not in sys.path:
            # 倒序插入（先 deps/ 后 deps/<platform>/）：后插者排 sys.path 更前，
            # 保证平台目录优先于公共目录
            sys.path.insert(0, s)
            injected.append(s)
    injected.reverse()  # 返回按优先级排序（平台在前）
    if injected:
        logger.debug(f"[deps_loader] 注入 {plugin_dir.name} deps: {injected}")
    return injected


def missing_pip_deps(plugin_dir: Path, manifest: dict) -> List[str]:
    """检查声明的 pip 依赖哪些未就绪（既未在 deps/ 落地也不可导入）。

    判定顺序：
    1. 依赖已在 deps/<platform>/ 或 deps/ 有顶层目录（按包名）→ 就绪
    2. 否则尝试 import（可能由宿主环境提供）→ 就绪
    3. 都不行 → 缺失

    Returns:
        缺失的依赖规格列表（供 UI 提示安装）。
    """
    import importlib.util

    missing: List[str] = []
    have_dirs = deps_paths(plugin_dir)
    for spec in resolve_pip_deps(manifest):
        pkg = spec.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].split("!=")[0]
        pkg = pkg.split("[")[0].strip().replace("-", "_")
        if not pkg:
            continue
        # deps 目录里已有同名顶层包（或包名带下划线/连字符变体）
        top = pkg.replace("_", "?")
        if any(hit for d in have_dirs for hit in d.glob(top)):
            continue
        if importlib.util.find_spec(pkg) is not None:
            continue
        missing.append(spec)
    return missing
