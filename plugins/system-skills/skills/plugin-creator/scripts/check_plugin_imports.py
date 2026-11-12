#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_plugin_imports.py — 插件内跨模块 import 的符号级自检。

为什么存在：插件会在函数体里做延迟 import（`register_ui` 里的 `from .store import x`），
这类符号漂移 `python -m py_compile` 查不出来（语法合法），`validate_plugins.py` 也查不出
（它只校验结构与 manifest），只有宿主真实加载那一行时才炸成
`ImportError: cannot import name 'x' from 'ui_plugin_<plugin>.store'`。
2026-11 实测：store.py 重构删掉 get_plugin_data_dir，ui/__init__.py 的延迟 import 留着，
插件直接加载失败。

检查范围：插件自身 .py 内部的相对 import（level >= 1）。第三方/宿主 import 不管。

用法：
    python scripts/check_plugin_imports.py <插件目录>
    python scripts/check_plugin_imports.py <插件目录> --quiet

退出码：0 全部符号可解析 / 1 存在缺失符号 / 2 路径不合法
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def top_level_names(tree: ast.Module) -> set[str]:
    """模块顶层可见名字：定义、赋值、导入（含再导出）、带注解赋值。"""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    names.update(e.id for e in target.elts if isinstance(e, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def module_file(base: Path, modpath: tuple[str, ...]) -> Path | None:
    """把相对 import 的模块路径解析成文件。"""
    target = base.joinpath(*modpath)
    for cand in (target.with_suffix(".py"), target / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def check(plugin_dir: Path) -> list[str]:
    py_files = sorted(p for p in plugin_dir.rglob("*.py") if "__pycache__" not in p.parts)
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in py_files}
    exports = {p: top_level_names(t) for p, t in trees.items()}
    problems: list[str] = []

    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            # level=1 指当前文件的父包；node.module 为空表示 `from . import x`
            base = path.parent
            for _ in range(node.level - 1):
                base = base.parent
            mod = tuple(node.module.split(".")) if node.module else ()
            target = module_file(base, mod) if mod else base / "__init__.py"
            if target is None or target not in exports:
                problems.append(
                    f"{path.relative_to(plugin_dir).as_posix()}:{node.lineno} "
                    f"相对模块不存在: from {'.' * node.level}{node.module or ''} import ..."
                )
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name not in exports[target]:
                    problems.append(
                        f"{path.relative_to(plugin_dir).as_posix()}:{node.lineno} "
                        f"from {'.' * node.level}{node.module or ''} import {alias.name} "
                        f"→ {target.name} 顶层没有该名字"
                    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="插件内相对 import 符号自检")
    parser.add_argument("plugin_dir", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not args.plugin_dir.is_dir():
        print(f"[error] 不是目录: {args.plugin_dir}")
        return 2

    problems = check(args.plugin_dir.resolve())
    if not problems:
        if not args.quiet:
            print(f"[OK] {args.plugin_dir.name}: 相对 import 符号全部可解析")
        return 0

    print(f"[FAIL] {args.plugin_dir.name}: {len(problems)} 处 import 指向不存在的符号")
    for line in problems:
        print(f"  - {line}")
    print("修法：改 import 指向真实符号，或把被删的符号补回目标模块（延迟 import 只在加载时炸）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
