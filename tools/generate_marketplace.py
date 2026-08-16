#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_marketplace.py — 从 plugins/system/.drifox-plugin/plugin.json 生成系统插件 marketplace.json

DriFox 主仓库的系统插件市场生成器。系统插件（plugins/system/）随 DriFox 主仓库分发，
由本脚本生成 marketplace.json 供插件市场从 GitHub（martin98-afk/DriFox, master 分支）拉取更新。

用法:
    python tools/generate_marketplace.py           # 生成/更新 marketplace.json
    python tools/generate_marketplace.py --check    # 仅检查一致性，不写文件

退出码:
    0 — marketplace.json 已是最新（或已成功生成）
    1 — marketplace.json 与实际不一致（--check 模式）
    2 — 致命错误（如无插件目录）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ============================================================
# 路径定位
# ============================================================

REPO_ROOT = Path(__file__).resolve().parent.parent
# 系统插件目录：plugins/system/ 为单一系统插件（.drifox-plugin/plugin.json 唯一 manifest）
SYSTEM_PLUGIN_DIR = REPO_ROOT / "plugins" / "system"
MARKETPLACE_PATH = REPO_ROOT / "marketplace.json"
MANIFEST_PATH = Path(".drifox-plugin") / "plugin.json"

# 仓库元信息（与 DriFox 仓库 GitHub 链接一致）
REPO_NAME = "drifox-system"
REPO_DESCRIPTION = (
    "DriFox 系统插件市场 — 系统内置插件（默认命令、智能体、技能、主题、"
    "Hooks、MCP 配置与内置工具）的官方更新源"
)
REPO_URL = "https://github.com/martin98-afk/DriFox"
DEFAULT_REF = "master"
DEFAULT_LICENSE = "MIT"

# ============================================================
# 输出样式
# ============================================================

_IS_TTY = sys.stdout.isatty()


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _IS_TTY else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _IS_TTY else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _IS_TTY else s


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _IS_TTY else s


# ============================================================
# 插件发现与 manifest 读取
# ============================================================


def discover_plugins() -> list[Path]:
    """发现系统插件目录（当前粒度：plugins/system 单插件）。"""
    if not SYSTEM_PLUGIN_DIR.exists():
        return []
    return [SYSTEM_PLUGIN_DIR]


def load_manifest(plugin_dir: Path) -> dict | None:
    """读取插件 manifest，失败返回 None。"""
    manifest_file = plugin_dir / MANIFEST_PATH
    if not manifest_file.exists():
        return None
    try:
        return json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def normalize_author(author: Any) -> str:
    """将 author 字段统一为字符串。"""
    if isinstance(author, str):
        return author
    if isinstance(author, dict):
        return author.get("name", "unknown")
    return "unknown"


def infer_categories(manifest: dict) -> list[str]:
    """从 components 和 keywords 推断插件分类。"""
    categories: list[str] = []
    comps = manifest.get("components", {})
    keywords = [k.lower() for k in manifest.get("keywords", [])]

    # 按组件推断
    if comps.get("themes"):
        categories.append("theme")
    if comps.get("mcp"):
        categories.append("mcp")
    if comps.get("lsp"):
        categories.append("lsp")
    if comps.get("ui"):
        categories.append("ui")
    if comps.get("agents"):
        categories.append("agent")
    if comps.get("tools"):
        categories.append("tool")

    # 按 keywords 推断
    kw_category_map = {
        "git": "workflow",
        "workflow": "workflow",
        "commit": "workflow",
        "branch": "workflow",
        "pr": "workflow",
        "code-review": "workflow",
        "review": "workflow",
        "test": "workflow",
        "testing": "workflow",
        "tdd": "workflow",
        "coverage": "workflow",
        "python": "language",
        "frontend": "language",
        "react": "language",
        "vue": "language",
        "javascript": "language",
        "typescript": "language",
        "java": "language",
        "go": "language",
        "rust": "language",
        "stats": "stats",
        "statistics": "stats",
        "analytics": "stats",
        "usage": "stats",
        "token": "stats",
        "context": "stats",
        "dashboard": "stats",
    }
    for kw in keywords:
        cat = kw_category_map.get(kw)
        if cat and cat not in categories:
            categories.append(cat)

    # 默认分类
    if not categories:
        if comps.get("commands") or comps.get("skills") or comps.get("hooks"):
            categories.append("workflow")

    return categories


# ============================================================
# marketplace.json 生成
# ============================================================


def build_plugin_entry(plugin_dir: Path, manifest: dict) -> dict:
    """从 plugin.json 构建一条 marketplace 插件条目。"""
    entry: dict = {
        "name": manifest["name"],
        "description": manifest["description"],
        "version": manifest["version"],
        "author": normalize_author(manifest.get("author")),
        "license": manifest.get("license", DEFAULT_LICENSE),
        "categories": infer_categories(manifest),
        "source": {
            "type": "git-subdir",
            "url": REPO_URL,
            "path": f"plugins/{plugin_dir.name}",
            "ref": DEFAULT_REF,
        },
        "components": manifest.get("components", {}),
    }

    # 选填字段
    if manifest.get("keywords"):
        entry["keywords"] = manifest["keywords"]

    drifox_compat = manifest.get("drifox")
    if drifox_compat:
        entry["drifox"] = drifox_compat

    if manifest.get("homepage"):
        entry["homepage"] = manifest["homepage"]

    # icon 字段：透传 manifest 中的 string 或 {light, dark} 对象
    icon_value = manifest.get("icon")
    if icon_value:
        entry["icon"] = icon_value

    return entry


def generate_marketplace() -> dict:
    """扫描系统插件，生成完整的 marketplace.json 结构。"""
    plugins = discover_plugins()
    entries: list[dict] = []

    for plugin_dir in plugins:
        manifest = load_manifest(plugin_dir)
        if manifest is None:
            print(f"  {_yellow('SKIP')} {plugin_dir.name} — 无有效 manifest")
            continue
        entries.append(build_plugin_entry(plugin_dir, manifest))

    return {
        "name": REPO_NAME,
        "description": REPO_DESCRIPTION,
        "homepage": REPO_URL,
        "plugins": entries,
    }


# ============================================================
# 一致性检查
# ============================================================


def check_consistency(generated: dict, existing: dict) -> list[str]:
    """比对生成的 marketplace 与现有文件，返回差异列表。"""
    diffs: list[str] = []

    gen_plugins = {p["name"]: p for p in generated.get("plugins", [])}
    ex_plugins = {p["name"]: p for p in existing.get("plugins", [])}

    # 检查缺失的插件
    for name in gen_plugins:
        if name not in ex_plugins:
            diffs.append(f"marketplace.json 缺少插件: {name}")

    # 检查多余的插件
    for name in ex_plugins:
        if name not in gen_plugins:
            diffs.append(f"marketplace.json 包含已不存在的插件: {name}")

    # 检查字段一致性
    for name in gen_plugins:
        if name not in ex_plugins:
            continue
        gen_p = gen_plugins[name]
        ex_p = ex_plugins[name]

        for field in ("name", "description", "version", "author", "license", "components"):
            if gen_p.get(field) != ex_p.get(field):
                diffs.append(
                    f"插件 {name} 字段 {field} 不一致: "
                    f"marketplace={ex_p.get(field)!r} vs plugin.json={gen_p.get(field)!r}"
                )

        # source.path 以生成结果为准（build_plugin_entry 用目录名 plugin_dir.name
        # 拼接，manifest name 可能与目录名不同——不能拿 name 拼，否则误报）
        gen_path = gen_p.get("source", {}).get("path")
        ex_source = ex_p.get("source", {})
        if ex_source.get("path") != gen_path:
            diffs.append(
                f"插件 {name} source.path 应为 {gen_path!r}，"
                f"当前为 {ex_source.get('path')!r}"
            )

    return diffs


# ============================================================
# 主流程
# ============================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 plugins/system/.drifox-plugin/plugin.json 生成系统插件 marketplace.json"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅检查 marketplace.json 是否与实际一致，不写文件",
    )
    args = parser.parse_args(argv if argv is not None else None)

    print(_bold("DriFox system-plugin marketplace generator"))
    print(f"  repo:         {REPO_ROOT}")
    print(f"  marketplace:  {MARKETPLACE_PATH.relative_to(REPO_ROOT)}")
    print()

    generated = generate_marketplace()
    plugin_count = len(generated["plugins"])

    if plugin_count == 0:
        print(_yellow("未发现任何系统插件。"))
        return 2

    print(f"发现 {plugin_count} 个系统插件:")
    for p in generated["plugins"]:
        print(f"  {p['name']:20s} v{p['version']:10s} {p['description'][:50]}")
    print()

    if args.check:
        # 检查模式
        if not MARKETPLACE_PATH.exists():
            print(_red("✗ marketplace.json 不存在，请先运行 generate_marketplace.py 生成"))
            return 1

        try:
            existing = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(_red(f"✗ marketplace.json 不是合法 JSON: {e}"))
            return 1

        diffs = check_consistency(generated, existing)
        if diffs:
            print(_red(f"✗ marketplace.json 与实际不一致（{len(diffs)} 处差异）:"))
            for d in diffs:
                print(f"        {_red('diff')}: {d}")
            print()
            print(_yellow("请运行 `python tools/generate_marketplace.py` 更新 marketplace.json"))
            return 1
        else:
            print(_green("✓ marketplace.json 与所有 plugin.json 一致"))
            return 0
    else:
        # 生成模式
        output = json.dumps(generated, indent=4, ensure_ascii=False) + "\n"
        MARKETPLACE_PATH.write_text(output, encoding="utf-8")
        print(_green(f"✓ 已生成 {MARKETPLACE_PATH.relative_to(REPO_ROOT)}（{plugin_count} 个插件）"))
        return 0


if __name__ == "__main__":
    sys.exit(main())
