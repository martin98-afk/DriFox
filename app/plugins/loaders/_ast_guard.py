# -*- coding: utf-8 -*-
"""插件模块加载前的 AST 安全网（公共）。

事故复盘 2026-08-22：自测脚本 test_scaffold_storage.py 落入 self-evolver/tools/，
其模块级 sys.modules.update({"app.tools": ModuleType(...)}) 覆盖真模块，导致
后续 `from app.tools import X` 全部 (unknown location) 崩溃。

原防护只覆盖 tool loader，本模块把它抽到公共位置供 runtime_component_loader
与 provider_loader 复用，三处对齐（避免三份漂移）。

检测内容：
- 拒绝模块级 sys.modules 写入（覆盖/更新/pollute）
- 工具入口要求必须定义 register(registry)（避免加载测试脚本/临时文件）
- 运行时组件（model_adapters/loop_policies/storages/serializers）入口：register
- 服务商入口：register(registry)
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

from loguru import logger


def contains_sys_modules_mutation(source: str) -> bool:
    """检测源码任意位置是否包含直接操作 sys.modules 的危险语句。

    覆盖两种常见形式：sys.modules.update({...}) 与 sys.modules['x'] = <obj>。
    返回 True 即拒载。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # 语法错误由调用方按"拒载"处理（tool loader 行为：直接 return False，
        # runtime/provider loader 走 exec 的 try/except 捕获后 return False）
        return False
    for node in ast.walk(tree):
        if _is_sys_modules_mutation(node):
            return True
    return False


def _is_sys_modules_mutation(node: "ast.AST") -> bool:
    """单节点判定：是否为 sys.modules 直接写操作（update 或下标赋值）。"""
    # sys.modules.update(...)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        val = node.func.value
        if (
            isinstance(val, ast.Attribute)
            and val.attr == "modules"
            and isinstance(val.value, ast.Name)
            and val.value.id == "sys"
            and node.func.attr == "update"
        ):
            return True
    # sys.modules[...] = ...
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute):
                val = target.value
                if val.attr == "modules" and isinstance(val.value, ast.Name) and val.value.id == "sys":
                    return True
    return False


def has_register_function(source: str) -> bool:
    """检测源码是否定义了 register(registry) 顶层函数（工具/运行时/服务商的统一入口约定）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "register":
            return True
    return False


def guard_plugin_module(
    source: str,
    path: Path,
    *,
    require_register: bool = True,
    component: str = "",
) -> bool:
    """P5：插件模块加载前的统一 AST 网关。

    Args:
        source: 文件源码（path.read_text() 结果）
        path: 文件路径（用于日志/拒绝提示）
        require_register: 是否要求 register(registry) 顶层函数；False 时不强制
            （如运行时组件 loader 内部另有 callable 检查；provider/工具 loader 开启）
        component: 组件名（仅用于日志：runtime_component_loader 透传"model_adapters"等）

    Returns:
        True → 允许加载；False → 拒绝并已 logger.warning
    """
    tag = f"[{component}] " if component else ""
    # 1) 拒绝 sys.modules 污染——任何位置命中即拒载
    if contains_sys_modules_mutation(source):
        logger.warning(
            f"{tag}[ASTGuard] 拒绝加载疑似污染 sys.modules 的文件: {path}"
        )
        return False
    # 2) 入口函数检查（按需）
    if require_register and not has_register_function(source):
        # 与原 tool loader 行为一致：缺 register 直接 return False（不警告，
        # 调试日志，让 plugin_tool_loader 自己的 logger.debug 提示）
        return False
    return True


# 兼容旧调用点：tool loader 原命名空间使用 _is_tool_entry_module / _is_sys_modules_mutation
# 为减少 churn，保留同名薄壳指向本模块实现（tool loader 内部会重写以调用本模块）。
__all__ = [
    "contains_sys_modules_mutation",
    "has_register_function",
    "guard_plugin_module",
    "is_sys_modules_mutation_node",
]


def is_sys_modules_mutation_node(node: "ast.AST") -> bool:
    """兼容原 tool loader 内部按 node 逐个检查的调用语义。"""
    return _is_sys_modules_mutation(node)
