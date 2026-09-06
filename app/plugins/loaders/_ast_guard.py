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
from typing import Iterable, List, Set, Tuple

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


def has_register_function(source: str, names: Iterable[str] = ("register",)) -> bool:
    """检测源码是否定义了指定名称的函数（组件入口约定）。

    A1：names 参数化——tools/runtime/providers 入口为 register，ui 组件为
    register_ui；默认值保持旧语义，不影响现有调用点。
    """
    wanted: Set[str] = set(names)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            return True
    return False


# A4：危险 import 审计面——模块级命中即记入审计清单（仅日志告警，不拒载）。
# 覆盖网络/socket（socket）、子进程（subprocess）、HTTP 客户端（requests/urllib）、
# 原生调用（ctypes）五类高危能力。
_DANGEROUS_IMPORT_ROOTS = frozenset({"socket", "subprocess", "requests", "urllib", "ctypes"})


def audit_dangerous_imports(source: str) -> List[Tuple[int, str]]:
    """审计模块级危险 import，返回 [(行号, 符号名), ...]。

    只扫模块顶层语句（tree.body）；函数/方法/类内的延迟 import 不算
    （按模块级副作用口径）。语法错误返回空表（拒载判断是调用方职责，
    本函数只做审计，不做行为决定）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: List[Tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _DANGEROUS_IMPORT_ROOTS:
                    hits.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in _DANGEROUS_IMPORT_ROOTS:
                hits.append((node.lineno, node.module))
    return hits


# 模块级阻塞/破坏性调用审计面。
# 说明：插件模块由宿主线程同步 exec_module，顶层 `while True` / `input()` 会直接
# 冻结 Qt 事件循环且无法中断（无子进程隔离、无超时）。此处仅做静态审计告警，
# 不拒载 —— 避免误伤存量插件；日志即为事后追责与用户自查依据。
_BLOCKING_CALLS = {
    "input": "阻塞式读取 stdin（无终端时永久挂起）",
    "system": "执行系统命令",
    "popen": "执行系统命令",
    "rmtree": "递归删除目录",
    "remove": "删除文件",
    "unlink": "删除文件",
    "rmdir": "删除目录",
    "chmod": "修改文件权限",
    "kill": "终止进程",
    "setrecursionlimit": "修改解释器递归上限",
}


def audit_blocking_calls(source: str) -> List[Tuple[int, str, str]]:
    """审计模块级（tree.body 递归）阻塞/破坏性调用。

    Returns:
        [(行号, 调用表达式, 风险说明), ...]；语法错误返回空表。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: List[Tuple[int, str, str]] = []
    seen: Set[int] = set()

    def _expr(node: ast.AST) -> str:
        try:
            return ast.unparse(node)  # type: ignore[attr-defined]
        except Exception:
            return "<unparse-failed>"

    def _record(node: ast.AST, reason: str) -> None:
        line = getattr(node, "lineno", 0)
        if line in seen:
            return
        seen.add(line)
        hits.append((line, _expr(node), reason))

    for node in ast.walk(tree):
        # 顶层 while True —— 无 break 的常量真值循环
        if isinstance(node, ast.While) and _is_constant_true(node.test):
            if not _has_break(node):
                _record(node, "模块级 while True 无 break（将永久冻结宿主主线程）")
            continue
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if not name:
                continue
            leaf = name.rsplit(".", 1)[-1]
            if leaf in _BLOCKING_CALLS:
                _record(node, f"{leaf}(): {_BLOCKING_CALLS[leaf]}")
    return sorted(hits)


def _is_constant_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _has_break(loop: ast.AST) -> bool:
    """循环体内（不含嵌套函数）是否存在 break/return/raise —— 有则可退出。"""
    for child in ast.walk(loop):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, (ast.Break, ast.Return, ast.Raise)):
            return True
    return False


def _call_name(node: ast.Call) -> str:
    """取调用的完整点分名（os.system -> 'os.system'；rmtree(...) -> 'rmtree'）。"""
    parts: List[str] = []
    cur: ast.AST = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    if not parts:
        return ""
    return ".".join(reversed(parts))


def guard_plugin_module(
    source: str,
    path: Path,
    *,
    require_register: bool = True,
    component: str = "",
    entry_names: Iterable[str] = ("register",),
) -> bool:
    """P5：插件模块加载前的统一 AST 网关。

    Args:
        source: 文件源码（path.read_text() 结果）
        path: 文件路径（用于日志/拒绝提示）
        require_register: 是否要求入口函数；False 时不强制
            （如运行时组件 loader 内部另有 callable 检查；provider/工具 loader 开启）
        component: 组件名（仅用于日志：runtime_component_loader 透传"model_adapters"等）
        entry_names: 入口函数名集合（A1：透传给 has_register_function；
            tools/runtime/providers 用默认 ("register",)，ui 组件传 ("register_ui",)）

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
    if require_register and not has_register_function(source, names=entry_names):
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
    "audit_dangerous_imports",
    "audit_blocking_calls",
]


def is_sys_modules_mutation_node(node: "ast.AST") -> bool:
    """兼容原 tool loader 内部按 node 逐个检查的调用语义。"""
    return _is_sys_modules_mutation(node)
