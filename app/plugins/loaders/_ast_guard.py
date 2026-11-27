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
import hashlib
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

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

# urllib 子模块豁免：parse / error 仅 URL 解析与异常定义，无网络能力，不触发告警
_URLLIB_EXEMPT_SUBMODULES = frozenset({"urllib.parse", "urllib.error"})


def _is_dangerous_import(fullname: str) -> bool:
    """按完整模块名判定是否命中危险审计面（根命中且不在豁免清单）。"""
    if fullname in _URLLIB_EXEMPT_SUBMODULES:
        return False
    return fullname.split(".")[0] in _DANGEROUS_IMPORT_ROOTS


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
                if _is_dangerous_import(alias.name):
                    hits.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _is_dangerous_import(node.module):
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
            不传 = 拒载任何 sys.modules 写入；四个 loader（ui/tool/runtime/provider）
            均已按插件声明透传（2026-10-13 口径统一，此前 tool/runtime/provider 漏传）。
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


# guard_plugin_module_once 结果缓存：键 = (path, mtime_ns, size)。
# 同一模块在启动对齐/热重载/差异补载链路中会被重复审计（实测启动期
# system-interact（更名前 system-tools）的 subprocess 告警重复打 3 遍，每遍都重新 ast.parse），
# 文件未变时直接复用上次判定，顺带消除重复告警日志。
#
# ★ 缓存的是「源码派生的纯分析结果」，不是最终放行结论。
#   放行结论还依赖运行时环境（sys.modules 当前内容、find_spec 可导入性），
#   同一源码在不同时刻可能得到不同放行结果：插件 A 先注册 root，插件 B
#   再注册同一 root 时，A 放行、B 必须拒载。把 ok 缓存起来会直接放宽安全
#   语义（B 复用 A 的放行结果）。故 _SourceAnalysis 只存 writes/has_register/
#   dangerous/syntax_error 四项纯 AST 产物，_write_rejection_reason 每次现场跑。
_GUARD_CACHE_MAX = 512
# ★ 存 _SourceAnalysis（纯源码派生），不是 GuardResult（含 ok 放行结论）。
# 命中时必须现场重跑 _evaluate_analysis 合成判定，理由见上方注释块。
_guard_cache: Dict[tuple, "_SourceAnalysis"] = {}


def _guard_cache_key(source: str, names_key: str) -> tuple:
    """进程内缓存键：(源码 sha256, 入口名集合)。

    ⚠️ 刻意**不用 mtime+size**：磁盘层已用内容哈希，进程内层曾用
    (path, mtime_ns, size)。两层策略不一致会留下真实缺口 —— 同 mtime 同 size
    改源码时磁盘层本该正确失效，但进程内层键完全不变，命中旧 _SourceAnalysis
    后根本不再走 _analyze_source，磁盘层的正确失效轮不到执行，此时新加入的
    sys.modules 写入检测不到（review 实测：改写 + os.utime 恢复 mtime，
    改前 ok=True 漏放，改后 ok=False 拒载）。
    这与 plugin_tool_loader._load_module 绕过 __pycache__ 的理由同源（mtime
    不可靠）：一处承认 mtime 不可信、另一处还拿它做键，属内部矛盾。

    names_key 参与 has_register 判定，必须入键。
    不含 require_register / allowed_sys_modules / plugin_dir：只影响最终放行
    判定、不影响 AST 产物，由 _evaluate_analysis 每次现场重算。
    不含 path：分析产物与路径无关，同内容不同路径应共享条目。

    注：本函数算的 digest 与 _analyze_source 内部重复一次（19 模块约 0.33ms）。
    为保持 _analyze_source 签名稳定刻意不去重——这段代码刚经历过一次 P0，
    在已验证的代码上做性能微优化是最不划算的改动类型。
    """
    digest = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return (digest, names_key)


# ── 跨进程持久化：源码分析结果磁盘缓存 ──────────────────────────────
# 动机（实测 2026-11-27）：冷启动期 guard_plugin_module_once 被调用 20 次，
# 首窗就绪前主线程累计 49~52ms，其中 ast.parse 占 6 成、AST walk 占 3 成，
# 而这两步对同一 (源码, entry_names) 恒等——每次启动重算是纯浪费。
#
# 只持久化「源码 + 参数 → 纯分析产物」，且键必须覆盖全部影响产物的输入：
#   source_sha / entry_names / schema 版本
# 不含 require_register / allowed_sys_modules / plugin_dir：这三者只影响
# 最终放行判定（每次都现场跑 _write_rejection_reason），不影响分析产物。
# 把它们塞进键只会制造无谓失效，也会诱导后来者误以为可以缓存 ok。
_SCHEMA_VERSION = 1
_DISK_CACHE_FILENAME = "ast_guard_cache.json"
# 调试/A-B 用：AST_GUARD_DISK_CACHE=0 关闭跨进程缓存（只走进程内），
# 便于对照量化缓存收益。与 builtin_commands._cache_disabled 同款约定。
_DISK_CACHE_ENABLED = os.environ.get("AST_GUARD_DISK_CACHE", "1") != "0"
_disk_cache_state: Dict[str, Any] = {
    "loaded": False,
    "enabled": _DISK_CACHE_ENABLED,
    "dirty": False,
    "flush_pending": False,
    "entries": {},
}


class _SourceAnalysis(NamedTuple):
    """一次 ast.parse 的全部纯源码派生结果（可安全跨进程复用）。

    rejected_writes 不在此列：它依赖运行时 sys.modules 状态。
    """

    writes: List[SysModulesWrite]
    has_register: bool
    dangerous_imports: List[Tuple[int, str]]
    syntax_error: bool

    def to_json(self) -> list:
        return [
            [[w.lineno, w.kind, None if w.keys is None else list(w.keys)] for w in self.writes],
            self.has_register,
            [list(d) for d in self.dangerous_imports],
            self.syntax_error,
        ]

    @classmethod
    def from_json(cls, raw: Any) -> Optional["_SourceAnalysis"]:
        """严格校验；任何形状不符返回 None（回落到重新解析，绝不猜）。"""
        try:
            writes_raw, has_register, dangerous_raw, syntax_error = raw
            if not isinstance(writes_raw, list) or not isinstance(dangerous_raw, list):
                return None
            if not isinstance(has_register, bool) or not isinstance(syntax_error, bool):
                return None
            writes: List[SysModulesWrite] = []
            for item in writes_raw:
                lineno, kind, keys = item
                if not isinstance(lineno, int) or not isinstance(kind, str):
                    return None
                if keys is None:
                    keys_t: Optional[Tuple[str, ...]] = None
                elif isinstance(keys, list) and all(isinstance(k, str) for k in keys):
                    keys_t = tuple(keys)
                else:
                    return None
                writes.append(SysModulesWrite(lineno, kind, keys_t))
            dangerous = [(int(ln), str(sym)) for ln, sym in dangerous_raw]
            return cls(writes, has_register, dangerous, syntax_error)
        except Exception:
            return None


def _disk_cache_path() -> Optional[Path]:
    """缓存落点：<app_data>/cache/ast_guard_cache.json（与 commands_cache 同目录约定）。"""
    try:
        from app.utils.app_paths import get_app_data_dir

        cache_dir = Path(get_app_data_dir()) / "cache"
        return cache_dir / _DISK_CACHE_FILENAME
    except Exception:
        return None


def _disk_cache_load() -> Dict[str, Any]:
    """惰性载入磁盘缓存（进程内一次）。任何异常 → 空表，退化为纯进程内缓存。"""
    st = _disk_cache_state
    if st["loaded"]:
        return st["entries"]
    st["loaded"] = True
    if not st["enabled"]:
        return st["entries"]
    path = _disk_cache_path()
    if path is None:
        st["enabled"] = False
        return st["entries"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict) or payload.get("version") != _SCHEMA_VERSION:
            return st["entries"]
        entries = payload.get("entries")
        if isinstance(entries, dict):
            st["entries"] = entries
    except FileNotFoundError:
        pass
    except Exception as e:  # 缓存损坏/权限问题：不能影响门禁，安静退化
        logger.debug(f"[ASTGuard] 磁盘缓存读取失败，回落重新解析: {e}")
    return st["entries"]


def _disk_cache_store(key: str, analysis: "_SourceAnalysis") -> None:
    """写入内存并标脏（真正的落盘在 flush_disk_cache，由调用方择机执行）。"""
    st = _disk_cache_state
    entries = _disk_cache_load()
    if len(entries) >= _GUARD_CACHE_MAX:
        entries.clear()
    entries[key] = analysis.to_json()
    st["dirty"] = True


def flush_disk_cache() -> None:
    """把待写条目原子落盘。启动路径不调用，由热重载/退出等非关键时机触发。"""
    st = _disk_cache_state
    st["flush_pending"] = False
    if not st["dirty"] or not st["enabled"]:
        return
    path = _disk_cache_path()
    if path is None:
        return
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _SCHEMA_VERSION, "entries": st["entries"]}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        st["dirty"] = False
    except Exception as e:
        logger.debug(f"[ASTGuard] 磁盘缓存写入失败（不影响门禁）: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _analyze_source(source: str, entry_names: Iterable[str]) -> Tuple[str, "_SourceAnalysis"]:
    """返回 (disk_key, analysis)。disk_key 覆盖全部影响分析产物的输入。"""
    digest = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    names_key = ",".join(sorted(set(entry_names)))
    disk_key = f"{_SCHEMA_VERSION}:{digest}:{names_key}"
    cached = _disk_cache_load().get(disk_key)
    if cached is not None:
        analysis = _SourceAnalysis.from_json(cached)
        if analysis is not None:
            return disk_key, analysis
    try:
        tree = ast.parse(source)
    except SyntaxError:
        analysis = _SourceAnalysis([], False, [], True)
        _disk_cache_store(disk_key, analysis)
        return disk_key, analysis
    analysis = _SourceAnalysis(
        writes=_find_sys_modules_writes_in_tree(tree),
        has_register=_has_register_in_tree(tree, set(entry_names)),
        dangerous_imports=_audit_dangerous_imports_in_tree(tree),
        syntax_error=False,
    )
    _disk_cache_store(disk_key, analysis)
    return disk_key, analysis


def _schedule_flush(delay: float = 1.5) -> None:
    """非阻塞落盘：起一条 daemon Timer，延时后写一次（启动路径不等待 IO）。"""
    st = _disk_cache_state
    if st.get("flush_pending"):
        return
    try:
        timer = threading.Timer(delay, flush_disk_cache)
        timer.daemon = True
        timer.start()
        st["flush_pending"] = True
    except Exception:
        pass


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
    # 入参可能是生成器：本函数内要多次迭代（缓存键 / 分析 / 日志），先固化
    entry_names = tuple(entry_names)
    if plugin_dir is None:
        try:
            plugin_dir = Path(path).parent.parent
        except Exception:
            plugin_dir = None

    cache_key = _guard_cache_key(source, ",".join(sorted(set(entry_names))))
    analysis: Optional["_SourceAnalysis"] = None
    cached = _guard_cache.get(cache_key)
    if isinstance(cached, _SourceAnalysis):
        analysis = cached

    cached_hit = analysis is not None
    if analysis is None:
        # 纯源码分析（writes/has_register/dangerous/syntax_error）可跨进程复用。
        _disk_key, analysis = _analyze_source(source, entry_names)
        if not analysis.syntax_error:
            if len(_guard_cache) >= _GUARD_CACHE_MAX:
                # 键里已无 path（k[0] 是 sha256），无法再「只淘汰本文件的旧键」。
                # 改回全清在这里可接受：上层有磁盘缓存兜底，清空后下一次查询
                # 走 _analyze_source 命中磁盘（实测 ~0.05ms/模块），不再付完整
                # ast.parse。若沿用旧的 k[0] == str(path) 筛选，条件永远为
                # False → 淘汰静默失效 → 缓存只增不减。
                _guard_cache.clear()
            _guard_cache[cache_key] = analysis

    # 放行判定必须每次现场跑（缓存命中也一样），见 _evaluate_analysis 的说明。
    result = _evaluate_analysis(
        analysis,
        path,
        tag,
        require_register=require_register,
        entry_names=entry_names,
        allowed_sys_modules=allowed_sys_modules,
        plugin_dir=plugin_dir,
        emit_allow_log=not cached_hit,
    )
    # 有新条目就排一次延时落盘（daemon Timer，不阻塞调用方）；命中时 dirty=False 直接返回
    if _disk_cache_state["dirty"]:
        _schedule_flush()
    return result


def _evaluate_analysis(
    analysis: "_SourceAnalysis",
    path: Path,
    tag: str,
    *,
    require_register: bool,
    entry_names: Iterable[str],
    allowed_sys_modules: Iterable[str],
    plugin_dir: Optional[Path],
    emit_allow_log: bool = True,
) -> GuardResult:
    """用纯源码分析产物 + **当前**运行时状态合成放行判定。

    这是唯一产出 GuardResult 的地方：缓存命中也必须走这里。放行判定依赖
    sys.modules 当前内容与 find_spec 可导入性，同一份源码在不同时刻可以得到
    不同结论（插件 A 注册了某 root 后，插件 B 注册同名 root 必须拒载），
    故缓存只复用 analysis，绝不复用本函数的返回值。

    emit_allow_log=False：命中缓存时跳过「按声明放行」的 info 日志——同源码
    同声明下它是重复输出（缓存的存在价值之一就是消除这条噪声）；拒载
    warning 无条件输出，安全审计痕迹不因缓存而丢失。
    """
    if analysis.syntax_error:
        return GuardResult(
            ok=False, has_register=False, rejected_writes=[], dangerous_imports=[], syntax_error=True
        )
    rejected: List[SysModulesWrite] = []
    for write in analysis.writes:
        reason = _write_rejection_reason(write.keys, allowed_sys_modules, plugin_dir)
        if reason is None:
            if emit_allow_log:
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

    if require_register and not analysis.has_register:
        logger.debug(f"{tag}[ASTGuard] 缺少入口函数 {list(entry_names)}: {path}")

    ok = not rejected and (analysis.has_register or not require_register)
    return GuardResult(
        ok=ok,
        has_register=analysis.has_register,
        rejected_writes=rejected,
        dangerous_imports=analysis.dangerous_imports,
        syntax_error=False,
    )


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

