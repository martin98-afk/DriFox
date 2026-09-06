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
import importlib.util
import os
import sys
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Set, Tuple

from loguru import logger


def contains_sys_modules_mutation(
    source: str,
    allowed_names: Iterable[str] = (),
    plugin_dir: Optional[Path] = None,
) -> bool:
    """检测源码是否包含**未被声明许可**的 sys.modules 写入。

    Args:
        source: 源码文本
        allowed_names: 插件在 plugin.json 的 ``module_prefixes`` 里声明的模块名/前缀
            （与热重载 purge 同源契约，见 PluginHostService._purge_module_prefixes）。
            落在声明内、且不会遮蔽真实模块的注册放行；未声明或键无法静态解析则拒载。
        plugin_dir: 插件根目录；用于判定"已加载模块是否本插件自己注册的"
            （热重载覆盖自己允许，覆盖他人拒绝）。

    Returns:
        True 即拒载。
    """
    for write in find_sys_modules_writes(source):
        if _write_rejection_reason(write.keys, allowed_names, plugin_dir) is not None:
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


# ── 写入落点解析（声明式放行的基础） ──────────────────────────────
# 背景：2026-09-06 ui loader 接上本安全网后，assistant_hub 这类"插件按文件路径
# 注册自有共享模块（sys.modules['assistant_hub_manager'] = module）"的存量写法
# 被一刀切拒载（UI 整体加载失败）。一刀切的本意是防"用假模块覆盖真模块"
# （2026-08-22 事故：sys.modules.update({"app.tools": ...})），并非禁止插件注册
# 自己的模块。故改为声明式：plugin.json 的 module_prefixes 声明命名空间，
# 落点可静态解析 + 落在声明内 + 不遮蔽真实模块 → 放行并留日志。


class SysModulesWrite(NamedTuple):
    """一次 sys.modules 写入。

    keys 为 None 表示键是动态表达式（变量拼接 / 函数返回值等），
    静态无法确定落点 —— 调用方必须按最严口径拒载。
    """

    lineno: int
    kind: str  # "update" | "subscript"
    keys: Optional[Tuple[str, ...]]


def _is_sys_modules_attr(node: "ast.AST") -> bool:
    """节点是否为 sys.modules 表达式。"""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "modules"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _module_level_str_constants(tree: "ast.Module") -> dict:
    """收集模块顶层 `NAME = "字面量"` 常量，用于解析 sys.modules[NAME] 的键。"""
    consts: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                consts[node.targets[0].id] = val.value
    return consts


def _resolve_str_key(node: "ast.AST", consts: dict) -> Optional[str]:
    """把 sys.modules 的键表达式解析成字符串；无法静态确定返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


def _dict_literal_keys(call: "ast.Call", consts: dict) -> Optional[Tuple[str, ...]]:
    """解析 sys.modules.update({...}) 的键；非字面量 dict（含 **展开）返回 None。"""
    if len(call.args) != 1 or call.keywords:
        return None
    arg = call.args[0]
    if not isinstance(arg, ast.Dict):
        return None
    keys: List[str] = []
    for k in arg.keys:
        if k is None:  # {**other} 展开
            return None
        resolved = _resolve_str_key(k, consts)
        if resolved is None:
            return None
        keys.append(resolved)
    return tuple(keys)


def find_sys_modules_writes(source: str) -> List[SysModulesWrite]:
    """列出源码中所有 sys.modules 写入（update / 下标赋值）及其可解析的键名。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return _find_sys_modules_writes_in_tree(tree)


def _find_sys_modules_writes_in_tree(tree: "ast.Module") -> List[SysModulesWrite]:
    """find_sys_modules_writes 的 tree 内核（parse-once 聚合门复用）。"""
    consts = _module_level_str_constants(tree)
    writes: List[SysModulesWrite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "update" and _is_sys_modules_attr(node.func.value):
                writes.append(
                    SysModulesWrite(getattr(node, "lineno", 0), "update", _dict_literal_keys(node, consts))
                )
                continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _is_sys_modules_attr(target.value):
                    key = _resolve_str_key(target.slice, consts)
                    writes.append(
                        SysModulesWrite(getattr(node, "lineno", 0), "subscript", None if key is None else (key,))
                    )
    return writes


def _is_dotted_identifier(name: str) -> bool:
    return bool(name) and all(part.isidentifier() for part in name.split("."))


def _origin_under_plugin(mod, plugin_dir: Optional[Path]) -> bool:
    """已加载模块的来源文件是否位于插件目录内（即"插件自己注册的"）。"""
    if plugin_dir is None:
        return False
    origin = getattr(mod, "__file__", None) or getattr(getattr(mod, "__spec__", None), "origin", None)
    if not origin:
        return False
    try:
        base = Path(os.path.normcase(str(Path(plugin_dir).resolve())))
        target = Path(os.path.normcase(str(Path(origin).resolve())))
        return target.is_relative_to(base)
    except Exception:
        return False


def _shadows_real_module(root: str, plugin_dir: Optional[Path]) -> bool:
    """根名是否指向真实模块（宿主/三方/其它插件已加载或可导入）。

    True → 注册即遮蔽，必须拒载。插件自己此前注册的模块（origin 在其目录内）
    视为热重载自覆盖，不算遮蔽。
    """
    existing = sys.modules.get(root)
    if existing is not None:
        return not _origin_under_plugin(existing, plugin_dir)
    try:
        return importlib.util.find_spec(root) is not None
    except (ModuleNotFoundError, ValueError):
        return False  # 父包不存在 / 名称非法 → 环境里没有这个真模块
    except Exception:
        return True  # 未知状态：保守按"可能遮蔽"处理


def _write_rejection_reason(
    keys: Optional[Tuple[str, ...]],
    allowed_names: Iterable[str],
    plugin_dir: Optional[Path],
) -> Optional[str]:
    """返回拒载原因；None 表示放行。"""
    if keys is None:
        return "键为动态表达式，无法静态确认落点"
    declared = tuple(allowed_names or ())
    if not declared:
        return "插件未在 plugin.json 声明 module_prefixes"
    for key in keys:
        if not _is_dotted_identifier(key):
            return f"模块名非法: {key!r}"
        if not any(key == p or key.startswith(p) for p in declared):
            return f"模块名 {key!r} 不在声明前缀 {list(declared)} 内"
        root = key.split(".")[0]
        if _shadows_real_module(root, plugin_dir):
            return f"模块名 {key!r} 会遮蔽真实模块（根 {root!r} 已存在或可导入）"
    return None


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
    return _has_register_in_tree(tree, wanted)


def _has_register_in_tree(tree: "ast.Module", wanted: Set[str]) -> bool:
    """has_register_function 的 tree 内核（parse-once 聚合门复用）。"""
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
    return _audit_dangerous_imports_in_tree(tree)


def _audit_dangerous_imports_in_tree(tree: "ast.Module") -> List[Tuple[int, str]]:
    """audit_dangerous_imports 的 tree 内核（parse-once 聚合门复用）。"""
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
    allowed_sys_modules: Iterable[str] = (),
    plugin_dir: Optional[Path] = None,
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
        allowed_sys_modules: 声明式放行名单（plugin.json 的 module_prefixes）。
            不传 = 一刀切拒载任何 sys.modules 写入（tool/runtime/provider 保持原语义）
        plugin_dir: 插件根目录（判定"覆盖的是不是自己的模块"）；缺省按
            path.parent.parent 推断（ui/tools/runtime/providers 均适用）

    Returns:
        True → 允许加载；False → 拒绝并已 logger.warning
    """
    tag = f"[{component}] " if component else ""
    if plugin_dir is None:
        try:
            plugin_dir = Path(path).parent.parent
        except Exception:
            plugin_dir = None
    # 1) sys.modules 写入：声明内且非遮蔽 → 放行（留 info 便于事后审计）；否则拒载
    for write in find_sys_modules_writes(source):
        reason = _write_rejection_reason(write.keys, allowed_sys_modules, plugin_dir)
        if reason is None:
            logger.info(
                f"{tag}[ASTGuard] 按 plugin.json module_prefixes 放行模块注册 "
                f"{list(write.keys or ())}（line {write.lineno}）: {path}"
            )
            continue
        logger.warning(
            f"{tag}[ASTGuard] 拒绝加载疑似污染 sys.modules 的文件: {path}"
            f"（line {write.lineno}，{reason}）"
        )
        return False
    # 2) 入口函数检查（按需）
    if require_register and not has_register_function(source, names=entry_names):
        # 与原 tool loader 行为一致：缺 register 直接 return False（不警告，
        # 调试日志，让 plugin_tool_loader 自己的 logger.debug 提示）
        return False
    return True


class GuardResult(NamedTuple):
    """guard_plugin_module_once 的聚合判定结果。

    rejected_writes: 被 _write_rejection_reason 判拒的 sys.modules 写入
    （调用方按各自日志口径输出拒载原因）。
    """

    ok: bool
    has_register: bool
    rejected_writes: List[SysModulesWrite]
    dangerous_imports: List[Tuple[int, str]]
    syntax_error: bool


def guard_plugin_module_once(
    source: str,
    path: Path,
    *,
    require_register: bool = True,
    component: str = "",
    entry_names: Iterable[str] = ("register",),
    allowed_sys_modules: Iterable[str] = (),
    plugin_dir: Optional[Path] = None,
) -> GuardResult:
    """P1b：插件模块加载前的 parse-once 聚合门。

    与 guard_plugin_module 同判定语义（sys.modules 声明式放行 + 入口检查），
    额外聚合模块级危险 import 审计；全部判定共享单次 ast.parse
    （热重载全量重扫时原三独立调用为 3 次 parse，此处收敛为 1 次）。

    Returns:
        GuardResult — ok=False 时调用方拒载；dangerous_imports 仅告警不拒载。
    """
    tag = f"[{component}] " if component else ""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return GuardResult(
            ok=False, has_register=False, rejected_writes=[], dangerous_imports=[], syntax_error=True
        )

    writes = _find_sys_modules_writes_in_tree(tree)
    has_register = _has_register_in_tree(tree, set(entry_names))
    dangerous = _audit_dangerous_imports_in_tree(tree)

    if plugin_dir is None:
        try:
            plugin_dir = Path(path).parent.parent
        except Exception:
            plugin_dir = None

    rejected: List[SysModulesWrite] = []
    for write in writes:
        reason = _write_rejection_reason(write.keys, allowed_sys_modules, plugin_dir)
        if reason is None:
            logger.info(
                f"{tag}[ASTGuard] 按 plugin.json module_prefixes 放行模块注册 "
                f"{list(write.keys or ())}（line {write.lineno}）: {path}"
            )
        else:
            logger.warning(
                f"{tag}[ASTGuard] 拒绝加载疑似污染 sys.modules 的文件: {path}"
                f"（line {write.lineno}，{reason}）"
            )
            rejected.append(write)

    if require_register and not has_register:
        logger.debug(f"{tag}[ASTGuard] 缺少入口函数 {list(entry_names)}: {path}")

    ok = not rejected and (has_register or not require_register)
    return GuardResult(ok=ok, has_register=has_register, rejected_writes=rejected,
                       dangerous_imports=dangerous, syntax_error=False)


# 兼容旧调用点：tool loader 原命名空间使用 _is_tool_entry_module / _is_sys_modules_mutation
# 为减少 churn，保留同名薄壳指向本模块实现（tool loader 内部会重写以调用本模块）。
__all__ = [
    "contains_sys_modules_mutation",
    "find_sys_modules_writes",
    "SysModulesWrite",
    "has_register_function",
    "guard_plugin_module",
    "guard_plugin_module_once",
    "GuardResult",
    "audit_dangerous_imports",
    "audit_blocking_calls",
]

