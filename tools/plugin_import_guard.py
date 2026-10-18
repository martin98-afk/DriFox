# -*- coding: utf-8 -*-
"""插件 import 边界守卫：检查 plugins/**/*.py 是否只依赖插件合法面。

合法面（与 app/plugins/sdk.py docstring 一致）：
- app.plugins.contracts
- app.plugins.registries
- app.plugins.managers.plugin_config_store
- app.plugins.sdk

默认只告警（退出码 0），--strict 时违规即退出码 1（供 CI 用）。
本工具不做自动改写；迁移是长期的、按插件分批的工作。
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

ALLOWED_PREFIXES = (
    "app.plugins.contracts",
    "app.plugins.registries",
    "app.plugins.managers.plugin_config_store",
    "app.plugins.sdk",
    # 能力池：插件读 catalog 做自我校验 / 经 registry 建池（如 agent_trace 无 ctx 时自建）
    "app.plugins.capabilities",
    # 插件生态自身的基础设施：依赖安装器、插件清单查询（marketplace / installer 类插件需要）
    "app.plugins.deps_loader",
    "app.plugins.managers.plugin_manager",
)

# 第三方依赖 / 示例 / 缓存不参与判定
_SKIP_DIRS = {"__pycache__", "deps", "skills", "examples"}


@dataclass
class Report:
    violations: Dict[str, int] = field(default_factory=dict)
    by_plugin: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # system 插件（主程序内部契约的插件化实现）单独统计，不计入 violations
    system_usages: Dict[str, int] = field(default_factory=dict)

    def add(self, plugin: str, module: str) -> None:
        self.violations[module] = self.violations.get(module, 0) + 1
        bucket = self.by_plugin.setdefault(plugin, {})
        bucket[module] = bucket.get(module, 0) + 1

    def add_system(self, module: str) -> None:
        self.system_usages[module] = self.system_usages.get(module, 0) + 1


# system 插件是主程序内部契约的插件化实现（如 system-storages 实现 SessionStore、
# system-serializers 复用 message_content），天然需要访问内部类型。对它们只禁
# 最核心的巨型模块，其余放行。
SYSTEM_FORBIDDEN = ("app.main_widget",)


def classify(module: str, *, is_system: bool = False) -> bool:
    """True = 合规（或不在守卫范围）。

    is_system: 插件 manifest 的 type 为 system 时用宽松口径（只禁
    SYSTEM_FORBIDDEN），其余 app.* 放行。
    """
    if not module.startswith("app."):
        return True
    if any(module == p or module.startswith(p + ".") for p in ALLOWED_PREFIXES):
        return True
    if is_system:
        return not (
            module in SYSTEM_FORBIDDEN
            or module.startswith(tuple(p + "." for p in SYSTEM_FORBIDDEN))
        )
    return False


def plugin_type(root: Path, plugin: str) -> str:
    """读插件 manifest 的 type 字段（缺失/解析失败视为 user，最严口径）。"""
    import json

    for sub in (".drifox-plugin", ".claude-plugin"):
        path = root / "plugins" / plugin / sub / "plugin.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return "user"
        val = (data or {}).get("type")
        return str(val) if isinstance(val, str) else "user"
    return "user"


def _iter_py(root: Path):
    plugins = root / "plugins"
    if not plugins.is_dir():
        return
    for path in plugins.rglob("*.py"):
        rel = path.relative_to(plugins)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        yield path


def _modules_of(node: ast.AST) -> List[str]:
    """单条 import 语句涉及的模块名（from X import a,b 只算 X 一次）。"""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module]
    return []


def scan(root: Path) -> Report:
    report = Report()
    type_cache: Dict[str, str] = {}
    for path in _iter_py(root):
        plugin = path.relative_to(root / "plugins").parts[0]
        if plugin not in type_cache:
            type_cache[plugin] = plugin_type(root, plugin)
        is_system = type_cache[plugin] == "system"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            for mod in _modules_of(node):
                if not mod.startswith("app."):
                    continue
                if any(mod == p or mod.startswith(p + ".") for p in ALLOWED_PREFIXES):
                    continue  # 合法面
                if is_system:
                    # system 插件：内部依赖做可见性统计；只有 SYSTEM_FORBIDDEN 算违规
                    if classify(mod, is_system=True):
                        report.add_system(mod)
                    else:
                        report.add(plugin, mod)
                else:
                    report.add(plugin, mod)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="插件 import 边界守卫")
    ap.add_argument("--root", default=".", help="仓库根目录（含 plugins/）")
    ap.add_argument("--strict", action="store_true", help="违规时退出码 1")
    args = ap.parse_args(argv)

    report = scan(Path(args.root))
    if not report.violations:
        print("[plugin-import-guard] 未发现越界 import")
        if report.system_usages:
            print(
                f"（system 插件另有 {sum(report.system_usages.values())} 处内部依赖，"
                "属主程序契约实现，不计违规）"
            )
        return 0

    total = sum(report.violations.values())
    print(f"[plugin-import-guard] 越界 import {total} 处，涉及 {len(report.by_plugin)} 个插件\n")
    print("按模块聚合：")
    for mod, n in sorted(report.violations.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {mod}")
    print("\n按插件聚合：")
    for plugin, mods in sorted(report.by_plugin.items()):
        joined = ", ".join(f"{m}({n})" for m, n in sorted(mods.items(), key=lambda kv: -kv[1]))
        print(f"  {plugin}: {joined}")
    if report.system_usages:
        print(
            f"\nsystem 插件内部依赖 {sum(report.system_usages.values())} 处"
            "（主程序契约实现，仅禁 app.main_widget）："
        )
        for mod, n in sorted(report.system_usages.items(), key=lambda kv: -kv[1]):
            print(f"  {n:4d}  {mod}")
    print(
        "\n合法面：app.plugins.contracts / app.plugins.registries / "
        "app.plugins.managers.plugin_config_store / app.plugins.sdk / "
        "app.plugins.capabilities"
    )
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
