# -*- coding: utf-8 -*-
"""
插件工具加载器 — 扫描 plugins/*/tools/*.py 的 register(registry) 入口

约定：
- 插件目录：`plugins/<name>/tools/<tool>.py`
- 每个工具文件暴露 `register(registry)` 函数（入参 ToolRegistry 代理），
  内部调用 registry.register(name, schema, impl=..., danger=..., icon=...,
  cn_name=..., group=..., description=..., aliases=...)
- 危险级别强制校验：register 必须显式声明 danger（registry 层拒绝未声明插件工具）

扫描范围（含系统插件）：
- 工作树 `plugins/`（含 `plugins/system/tools/` 系统内置工具插件）
- 用户插件目录 `<app_data>/plugins/`

热重载：
- 后台线程轮询签名（path, mtime, size），变更触发全量重扫
- 幂等：重扫按插件名清理已不存在文件的注册
- 并发安全：registry 版本号 + 执行快照（正在执行的工具调用不中断）

优先级：
- 多根遍历按顺序；同名工具「先注册者优先」（工作树 plugins/ 先扫 → system 内置优先）
- 跨根覆盖：user 插件可覆盖 system 插件的同名工具（user > system）；同根或同级按先注册者优先
- 同根内：已被其他插件占用的同名工具不覆盖
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from loguru import logger

from app.tools.registry import ToolRegistry


# 插件根等级：system < user。低等级根注册的同名工具可被高等级根覆盖
_ROOT_KIND_SYSTEM = "system"  # 项目工作树 plugins/（含 plugins/system/）
_ROOT_KIND_USER = "user"  # 用户数据目录 <app_data>/plugins/
_ROOT_KIND_PRIORITY: Dict[str, int] = {_ROOT_KIND_SYSTEM: 0, _ROOT_KIND_USER: 1}


def _plugin_roots() -> List[Path]:
    """插件工具扫描根：项目 plugins/（含 system）+ 用户插件目录"""
    roots = [Path(__file__).resolve().parent.parent.parent.parent / "plugins"]
    try:
        from app.utils.utils import get_app_data_dir

        roots.append(get_app_data_dir() / "plugins")
    except Exception:
        pass
    return roots


def _root_kind(root: Optional[Path]) -> str:
    """识别 root 等级：项目工作树 → system；app_data/plugins → user

    测试场景传入自定义 root（既非工作树也非 app_data），按 system 兜底。
    """
    if root is None:
        return _ROOT_KIND_SYSTEM
    try:
        from app.utils.utils import get_app_data_dir

        user_root = get_app_data_dir() / "plugins"
        if root == user_root:
            return _ROOT_KIND_USER
    except Exception:
        pass
    return _ROOT_KIND_SYSTEM


_PLUGIN_ROOTS: List[Path] = _plugin_roots()


def _iter_tool_modules(plugin_root: Path):
    """遍历插件目录下所有 tools/*.py（含 <name>/tools/ 与 <name>/<name>.py 结构）"""
    if not plugin_root.is_dir():
        return
    for plugin_dir in sorted(plugin_root.iterdir()):
        if not plugin_dir.is_dir():
            continue
        tools_dir = plugin_dir / "tools"
        if tools_dir.is_dir():
            for py in sorted(tools_dir.glob("*.py")):
                if py.name.startswith("_"):
                    continue
                yield plugin_dir.name, py
        else:
            # 单文件插件：<name>/<name>.py 带 register 入口
            single = plugin_dir / f"{plugin_dir.name}.py"
            if single.exists():
                yield plugin_dir.name, single


class _PluginRegistryProxy:
    """插件注册代理 — 强制注入 source 与 danger 校验。

    插件代码的 `register(registry)` 收到的是本代理而非裸 ToolRegistry：
    - source 强制为 `plugin:<plugin_name>`（忽略插件自报，防伪造 builtin 绕过 danger 校验）
    - danger 仍必须显式声明（裸 registry 的校验继续生效）
    - 同名保护规则（root_kind 优先级）：
        * 同根内：已被其他插件/内置占用时不覆盖（同插件重扫由 watcher 卸载兜底）
        * 跨根：user 插件（高优先级）可覆盖 system 插件（低优先级）的同名工具；
          反向与同级按「先注册者优先」（root_tracker 记录首注册根）
    """

    def __init__(
        self,
        registry: "ToolRegistry",
        plugin_name: str,
        root: Optional[Path] = None,
        root_tracker: Optional[dict] = None,
    ):
        self._registry = registry
        self._plugin_name = plugin_name
        self._root = root
        self._root_kind = _root_kind(root)
        # root_tracker: {tool_name: 来源根路径}（跨根优先级判定）
        self._root_tracker = root_tracker if root_tracker is not None else {}
        # 本次 register() 实际注册（含覆盖）成功的工具名。
        # 关键：覆盖场景下工具名在注册前后都在 registry（仅 source 变化），
        # 用 after-before diff 会得到空集 → watcher 的 _loaded 记录不到被覆盖
        # 的工具 → 用户插件删除/禁用后系统插件无法恢复。故由 proxy 显式记录。
        self.registered_names: Set[str] = set()

    def register(self, name, schema, impl=None, danger=None, source=None, metadata=None, **meta) -> bool:
        """注册（透传全部元数据：icon/cn_name/group/description/aliases）"""
        # D10：单个工具被停用 → 不注册（且不让其占用 root_tracker 槽位，
        # 低优先级根的同名工具因此有机会补位）
        if not _is_item_enabled(self._plugin_name, name):
            logger.debug(f"[PluginToolLoader] 工具已停用，跳过注册: {self._plugin_name}:{name}")
            return False
        # 同名工具覆盖判定（root_kind 优先级）
        # - 同根/无 root/首次注册：已被其他插件占用 → 拒绝（先注册者优先；同插件重扫由 watcher 卸载兜底）
        # - 跨根：高等级根（user）可覆盖低等级根（system）的同名工具；同级/反向拒绝
        src_root = self._root_tracker.get(name) if self._root is not None else None
        is_cross_root = src_root is not None and src_root != self._root
        if is_cross_root:
            # 跨根：仅允许高等级覆盖低等级
            if _ROOT_KIND_PRIORITY.get(self._root_kind, 0) <= _ROOT_KIND_PRIORITY.get(_root_kind(src_root), 0):
                return False
            # 高等级覆盖：直接放行（existing 来源是低等级根，应被替换；不再走同根保护）
        else:
            # 同根/无 root/首次注册：已被其他插件占用时不覆盖
            existing = self._registry.get(name)
            if existing is not None and existing.source != f"plugin:{self._plugin_name}":
                return False
        # 插件自带图标目录：<plugin>/tools/icons/（深色）+ tools/icons_light/（浅色，icon 自包含）
        icon_dir = ""
        icon_dir_light = ""
        if self._root is not None:
            _icons = Path(self._root) / self._plugin_name / "tools" / "icons"
            if _icons.is_dir():
                icon_dir = str(_icons)
            _icons_light = Path(self._root) / self._plugin_name / "tools" / "icons_light"
            if _icons_light.is_dir():
                icon_dir_light = str(_icons_light)
        # 把 root_kind 注入 metadata（渲染层/卡片用：区分 system / user 来源）
        merged_metadata = dict(metadata or {})
        merged_metadata.setdefault("_plugin_root_kind", self._root_kind)
        ok = self._registry.register(
            name,
            schema,
            impl=impl,
            danger=danger,
            icon=meta.get("icon", ""),
            icon_dir=icon_dir,
            icon_dir_light=icon_dir_light,
            cn_name=meta.get("cn_name", ""),
            group=meta.get("group", ""),
            description=meta.get("description", ""),
            aliases=meta.get("aliases"),
            team_only=meta.get("team_only", False),
            render=meta.get("render"),
            render_mode=meta.get("render_mode", ""),
            preview=meta.get("preview"),
            summarize=meta.get("summarize"),
            keep_in_content=bool(meta.get("keep_in_content", False)),
            source=f"plugin:{self._plugin_name}",
            metadata=merged_metadata,
        )
        if ok:
            self.registered_names.add(name)
            if self._root is not None:
                self._root_tracker[name] = self._root
        return ok


def _is_tool_entry_module(source: str, path: Path) -> bool:
    """判断文件是否为工具入口模块（须定义 register(registry) 且不得污染 sys.modules）。

    加载安全网：仅当文件确实暴露 register 入口、且不在模块级直接操作
    sys.modules（如 sys.modules.update 覆盖核心模块 app.tools）时才 exec。
    跳过测试脚本/临时文件/误放入 tools/ 目录的其他模块，防止其模块级代码
    在 exec 时污染全局 sys.modules。

    文件名快速过滤（test_*/conftest）+ AST 精确判定（register 函数 +
    拒绝 sys.modules 变异），防止误判 'tool_register=' 等相似标识符。

    P5 加固：原实现为内联 AST 扫描；现委托 app.plugins.loaders._ast_guard
    公共网关，逻辑保持一致（保留薄壳以减少外部调用点 churn）。
    """
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return False
    from app.plugins.loaders._ast_guard import contains_sys_modules_mutation, has_register_function

    if contains_sys_modules_mutation(source):
        logger.warning(f"[PluginToolLoader] 拒绝加载疑似污染 sys.modules 的文件: {path}")
        return False
    return has_register_function(source)


def _is_sys_modules_mutation(node: "ast.AST") -> bool:
    """兼容旧调用点（按 node 判定）— 委托公共网关。

    P5 加固：原实现为内联扫描；现委托 app.plugins.loaders._ast_guard。
    保留薄壳以减少外部调用点 churn。
    """
    from app.plugins.loaders._ast_guard import is_sys_modules_mutation_node

    return is_sys_modules_mutation_node(node)


def _load_module(plugin_name: str, path: Path, root_kind: str = ""):
    """加载插件工具模块（唯一模块名，避免命名冲突）。

    显式 compile 绕过 SourceFileLoader 的 __pycache__ 缓存：
    Windows 文件系统 mtime 精度低，快速写入后 exec 可能误复用旧 pyc
    （热重载内容不更新）——直接读文件编译最可靠。
    """
    mod_name = f"_plugin_tool_{plugin_name}_{path.stem}"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        logger.warning(f"[PluginToolLoader] 无法加载 {path}")
        return None
    module = importlib.util.module_from_spec(spec)
    # 修复：module_from_spec 的模块 dict 中 __builtins__ 可能异常（NoneType），
    # 强制注入真实 builtins（exec 自定义命名空间的规范做法）。
    module.__dict__["__builtins__"] = __builtins__
    # 修复：exec 前注册到 sys.modules——dataclasses 等库在 _process_class 中
    # 检查 `cls.__module__ in sys.modules`，未注册会导致装饰器异常。
    sys.modules[mod_name] = module
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, SyntaxError) as e:
        sys.modules.pop(mod_name, None)
        logger.warning(f"[PluginToolLoader] 读取/编译失败 {path}: {e}")
        return None
    # A4：危险 import 审计（仅日志告警，不拒载）——模块级 socket/subprocess/
    # requests/urllib/ctypes 记入结构化清单（插件名+行号+符号名），供安全巡检。
    from app.plugins.loaders._ast_guard import audit_dangerous_imports

    _audit_hits = audit_dangerous_imports(source)
    if _audit_hits:
        _audit_detail = "; ".join(f"line {ln}: {sym}" for ln, sym in _audit_hits)
        logger.warning(
            f"[PluginToolLoader] [AST审计] 插件 {plugin_name} 工具模块含模块级危险 import"
            f"（已放行，仅告警）: {_audit_detail} ({path}) kind={root_kind or 'unknown'}"
        )
    # [SAFETY] 加载安全网：仅加载暴露 register(registry) 入口的工具文件。
    # 跳过无 register 入口的文件（测试脚本/临时文件/误放入 tools/ 目录的模块），
    # 避免其模块级代码（如 sys.modules.update 覆盖核心模块 app.tools）在 exec
    # 时污染全局 sys.modules。
    # 事故复盘 2026-08-22：自测脚本 test_scaffold_storage.py 落入 self-evolver/tools/，
    # 其模块级 sys.modules.update({"app.tools": ModuleType(...)}) 覆盖真模块，
    # 导致后续 `from app.tools import X` 全部 (unknown location) 崩溃。
    if not _is_tool_entry_module(source, path):
        sys.modules.pop(mod_name, None)
        logger.debug(f"[PluginToolLoader] 跳过非工具入口文件（无 register 函数）: {path}")
        return None
    code = compile(source, str(path), "exec")
    try:
        exec(code, module.__dict__)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return module


def _is_plugin_enabled(plugin_name: str) -> bool:
    """按插件启用状态过滤工具加载（P0-1：修复安全边界失效）。

    统一以 Settings.enabled_plugins 为准（与 PluginManager.is_enabled 同源）。
    关键：不能依赖 pm.is_initialized()——真实启动链中 app.tools 在
    backend.initialize（pm.initialize 唯一调用点）之前即被导入并加载工具，
    此时 pm 恒未初始化。故 pm 未初始化时直接读 Settings（import 期可用）。

    语义对齐 PluginManager 其他组件（commands/agents/skills/mcp 均以
    enabled_plugins 集合为准）：插件被 Settings 禁用后其工具不再注册。

    边界情形：
    - pm 已初始化且插件无 manifest（裸 tools/ 目录）→ 默认启用（不受插件启停管理）
    - 从未配置 enabled_plugins（真·首次启动，pm 尚未填充全部插件）→ 全部启用，
      对齐 _restore_enabled_from_settings「新发现插件默认启用」语义
    """
    try:
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if pm.is_initialized():
            if not pm.has_plugin(plugin_name):
                return True
            return pm.is_enabled(plugin_name)
        # pm 未初始化（真实启动链）：Settings 在 import 期可用
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = cfg.enabled_plugins.value or []
        disabled = cfg.disabled_plugins.value or []
        # 从未配置 enabled 且从未禁用（真·首次启动）→ 全部启用，对齐 _restore 语义
        if not saved and not disabled:
            return True
        return plugin_name in saved
    except Exception as e:
        logger.warning(f"[PluginToolLoader] 插件启用状态检查失败，默认加载 {plugin_name}: {e}")
        return True


def _is_plugin_load_blocked(plugin_name: str) -> bool:
    """P1/P2：检查插件是否被版本/平台门禁拦截（load_blocked）。

    仅在 PluginManager 已初始化且能查到插件时检查；否则视为不拦截（放行）。
    拦截的插件其工具不进 registry——已注册过的会在后续重扫中被清理。
    """
    try:
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if not pm.is_initialized():
            return False
        plugin = pm.get_plugin(plugin_name)
        if plugin is None:
            return False
        return bool(getattr(plugin, "load_blocked", False))
    except Exception as e:
        logger.warning(f"[PluginToolLoader] 门禁检查失败，默认放行 {plugin_name}: {e}")
        return False


def _is_component_enabled(plugin_name: str, component: str = "tools") -> bool:
    """按组件级禁用集过滤工具加载（D9：插件内部 tools 子项开关）。

    与 _is_plugin_enabled 同源策略：直接读 Settings（import 期可用，不依赖
    pm 初始化状态）。组件未禁用即启用；读取失败默认加载（与 _is_plugin_enabled 对齐）。
    """
    try:
        from app.utils.config import Settings

        disabled = Settings.get_instance().disabled_plugin_components.value or []
        return f"{plugin_name}:{component}" not in set(disabled)
    except Exception as e:
        logger.warning(f"[PluginToolLoader] 组件启停检查失败，默认加载 {plugin_name}: {e}")
        return True


def _is_item_enabled(plugin_name: str, item_id: str, component: str = "tools") -> bool:
    """按细项级禁用集过滤单个工具的注册（D10：只关掉某一个工具）

    语义：整类停用 ⇒ 其下全部工具停用；整类启用时再看该工具自身。
    与 _is_component_enabled 同源策略：直接读 Settings，失败默认加载。

    选择在**注册阶段**过滤而非执行阶段，是为了让下游（LLM schema、工具执行、
    权限卡片、危险等级统计）自动保持一致——工具不在 registry 里，
    所有消费方自然都看不到它。
    """
    try:
        from app.utils.config import Settings

        disabled = set(Settings.get_instance().disabled_plugin_components.value or [])
        if f"{plugin_name}:{component}" in disabled:
            return False
        return f"{plugin_name}:{component}:{item_id}" not in disabled
    except Exception as e:
        logger.warning(f"[PluginToolLoader] 细项启停检查失败，默认加载 {plugin_name}:{item_id} ({e})")
        return True


def _run_register(
    registry: ToolRegistry,
    plugin_name: str,
    py_path: Path,
    root: Path,
    root_tracker: Dict[str, Path],
) -> Set[str]:
    """执行单个工具文件的 register，返回本次实际注册（含覆盖）的工具名集合

    注意：返回集是 proxy.registered_names（显式记录），而非 after-before diff——
    覆盖场景（工具已存在、仅替换 source/impl）下 diff 为空集，会导致 watcher
    的 _loaded 漏记被覆盖工具，用户插件删除后系统插件无法恢复。
    """
    module = _load_module(plugin_name, py_path, root_kind=_root_kind(root))
    register_fn = getattr(module, "register", None) if module else None
    if not callable(register_fn):
        return set()
    proxy = _PluginRegistryProxy(registry, plugin_name, root=root, root_tracker=root_tracker)
    try:
        register_fn(proxy)
    except Exception:
        # 部分成功回滚：异常在第 N 个注册时抛出，此前已注册的工具需逐一注销，
        # 避免失败插件留下半套工具污染 registry（含覆盖的工具，proxy 已记录）
        for name in proxy.registered_names:
            reg = registry.get(name)
            if reg is not None and reg.source == f"plugin:{plugin_name}":
                registry.unregister(name)
                logger.warning(f"[PluginToolLoader] 回滚部分注册工具: {name} ({plugin_name})")
        raise  # 保持 load_plugin_tools 既有 warning 语义（loaded 不含该插件）
    new_tools = proxy.registered_names
    if new_tools:
        logger.info(f"[PluginToolLoader] 插件 {plugin_name} 注册工具: {sorted(new_tools)}")
    return new_tools


def load_plugin_tools(
    registry: Optional[ToolRegistry] = None,
    plugin_roots: Optional[List[Path]] = None,
    root_tracker: Optional[Dict[str, Path]] = None,
) -> Dict[str, Set[str]]:
    """扫描并注册全部插件工具（含系统插件）。

    Args:
        root_tracker: 跨根覆盖追踪表（{tool_name: 首注册根}）。watcher 传入
            同一 dict 以持久跨次 scan 的覆盖状态；测试可传 None 取一次性。

    Returns:
        {plugin_name: {tool_name, ...}} — 本次加载的插件工具映射
    """
    registry = registry or ToolRegistry.get_instance()
    roots = plugin_roots if plugin_roots is not None else _PLUGIN_ROOTS
    loaded: Dict[str, Set[str]] = {}
    if root_tracker is None:
        root_tracker = {}  # tool_name -> 来源根（跨根优先级）

    for root in roots:
        for plugin_name, py_path in _iter_tool_modules(Path(root)):
            # P0-1：安全边界——插件被 Settings 禁用后其工具不再注册
            if not _is_plugin_enabled(plugin_name):
                logger.info(f"[PluginToolLoader] 跳过已禁用插件的工具: {plugin_name}")
                continue
            # D9：组件级禁用——tools 子项被关后不再注册
            if not _is_component_enabled(plugin_name):
                logger.info(f"[PluginToolLoader] 跳过 tools 组件已禁用的插件: {plugin_name}")
                continue
            # P1/P2：版本/平台门禁——load_blocked 插件不加载其工具
            if _is_plugin_load_blocked(plugin_name):
                logger.warning(f"[PluginToolLoader] 跳过被门禁拦截插件的工具: {plugin_name}")
                continue
            try:
                new_tools = _run_register(registry, plugin_name, py_path, Path(root), root_tracker)
                loaded.setdefault(plugin_name, set()).update(new_tools)
            except Exception as e:
                logger.warning(f"[PluginToolLoader] 插件 {plugin_name} register 失败: {e}")
    return loaded


def unload_plugin_tools(
    plugin_name: str,
    tool_names: Set[str],
    registry: Optional[ToolRegistry] = None,
    root_tracker: Optional[Dict[str, Path]] = None,
) -> None:
    """注销指定插件的工具（热重载/插件卸载时调用，幂等）

    Args:
        root_tracker: 跨根覆盖追踪表。注销成功后同步清理对应条目——
            工具已从 registry 移除，旧「来源根」记录会误导后续重扫
            （如用户插件删除后，残留的 read→user_root 会挡住 system 恢复）。
    """
    registry = registry or ToolRegistry.get_instance()
    # 同步清理该插件的 schema 过滤器（禁用/卸载/热重载路径统一走这里，防残留）
    try:
        registry.unregister_schema_filter(plugin_name)
    except Exception as e:
        logger.warning(f"[PluginToolLoader] 清理插件 schema 过滤器失败: {plugin_name} {e}")
    for name in tool_names:
        reg = registry.get(name)
        if reg is not None and reg.source == f"plugin:{plugin_name}":
            registry.unregister(name)
            if root_tracker is not None:
                root_tracker.pop(name, None)
            logger.info(f"[PluginToolLoader] 注销插件工具: {name} ({plugin_name})")


class PluginToolWatcher:
    """插件工具热重载 watcher：全量重扫 + 单插件精准卸载/重载。

    用法：:
        watcher = PluginToolWatcher()
        watcher.scan_now()          # 全量重扫（启动对齐 / 插件启停）
        watcher.unload_plugin("x")  # 精准卸载单插件（删除路径，不波及他插件）
        watcher.reload_plugin("x")  # 精准重载单插件（安装/更新路径，不波及他插件）

    单插件精准路径的跨根覆盖恢复：注销目标插件工具后，对更低等级根重扫
    一次，被其覆盖的同名工具按 root_kind 优先级自然恢复（见 _restore_lower_roots）。
    """

    def __init__(self, registry: Optional[ToolRegistry] = None, roots: Optional[List[Path]] = None):
        self._registry = registry or ToolRegistry.get_instance()
        self._roots = roots if roots is not None else _PLUGIN_ROOTS
        self._loaded: Dict[str, Set[str]] = {}  # plugin_name -> tool_names（当前生效）
        self._root_tracker: Dict[str, Path] = {}  # tool_name -> 来源根（跨根优先级）
        self._scan_lock = threading.Lock()  # 重扫互斥（UI 触发 + 轮询线程并发安全）
        self._thread = None
        self._stop = False
        # 热重载完成监听（轮询检测到变更并完成重扫后触发，后台线程回调）
        # 用途：UI 弹风险通知等。仅 watcher 轮询路径触发，
        # 显式 scan_now()（启动对齐 / 插件启停）不触发——那些场景用户已知情。
        self._reload_listeners: List[Callable[[], None]] = []

    def scan_now(self) -> None:
        """全量重扫：先注销已加载插件的全部工具，再全量重新注册（幂等）。

        ⚠️ 修复：旧实现用「注册前后 diff（after-before）」记录 _loaded，
        热更新场景（工具已注册、文件内容变更）diff 为空集，导致 _loaded
        记录丢失 → 后续删除文件时无法注销残留工具。改为先注销再全量重扫，
        _loaded 始终等于 registry 中该插件的实际工具集。

        ⚠️ 跨根覆盖追踪：传入 watcher 自己的 _root_tracker，使 user→system
        的覆盖关系跨多次 scan 持续生效（否则每次 scan 都新建 root_tracker，
        跨根保护失效，用户覆盖会被还原）。
        """
        with self._scan_lock:
            # 批量通知合并：注销+全量重扫期内几十次 register/unregister 的
            # 变更通知合并为一次（registry.notify_batch，异常安全），避免
            # UI 重建/缓存失效在热重载时反复排队。
            with self._registry.notify_batch():
                # 1) 注销已加载插件的全部工具（幂等；执行中的调用不受影响——快照机制）
                for plugin_name, old_names in self._loaded.items():
                    unload_plugin_tools(plugin_name, old_names, self._registry, root_tracker=self._root_tracker)
                # 2) 全量重扫注册（含跨根优先级保护与 enabled 过滤，load_plugin_tools 内部处理）
                try:
                    self._loaded = load_plugin_tools(
                        registry=self._registry,
                        plugin_roots=self._roots,
                        root_tracker=self._root_tracker,
                    )
                except Exception as e:
                    logger.warning(f"[PluginToolWatcher] 全量重扫失败: {e}")
                    # 重扫失败：registry 可能已被部分修改，下次 scan 会重新注销+重扫

    def _root_of_plugin(self, plugin_name: str) -> Optional[Path]:
        """从 root_tracker 反推某插件名所属扫描根（磁盘目录可能已删除）"""
        for name in self._loaded.get(plugin_name, set()):
            r = self._root_tracker.get(name)
            if r is not None:
                return Path(r)
        return None

    def _restore_lower_roots(self, removed_rank: int) -> None:
        """跨根覆盖恢复：重扫比目标根更低等级的所有根（被覆盖方），补注册被覆盖工具

        仅重扫低等级根（如 user 插件卸载后重扫 system 根），其模块的 register
        经 _PluginRegistryProxy 的「先注册者优先」保护：已存在的他插件工具
        不被覆盖，仅补注册因目标插件注销而缺失的同名工具（跨根覆盖恢复）。
        """
        for root in self._roots:
            if _ROOT_KIND_PRIORITY.get(_root_kind(root), 0) >= removed_rank:
                continue
            low_loaded = load_plugin_tools(
                registry=self._registry,
                plugin_roots=[Path(root)],
                root_tracker=self._root_tracker,
            )
            for pname, names in low_loaded.items():
                self._loaded.setdefault(pname, set()).update(names)

    def unload_plugin(self, plugin_name: str) -> None:
        """精准卸载单个插件工具（删除路径专用，不注销他插件工具）

        与 scan_now 的区别：scan_now 先注销 _loaded 全部插件再全量重注册，
        会波及 system 等无关插件（造成全量卸载+重载抖动）。本方法只注销
        目标插件名下的工具，并从 _loaded 移除其记录。

        跨根覆盖恢复：若被删插件位于高等级根（如 user 覆盖 system 同名工具），
        仅精准注销会使被覆盖的低级根工具永久消失。故在注销后，对**所有更低
        等级根**重扫一次（load_plugin_tools 内部按 root_kind 优先级恢复同名工具，
        已存在的他插件工具被「先注册者优先」保护跳过，无副作用）。
        """
        with self._scan_lock:
            with self._registry.notify_batch():
                tool_names = set(self._loaded.get(plugin_name, set()))
                # 兜底：_loaded 未跟踪但 registry 残留的同源工具（防御状态漂移）
                for reg in self._registry.list():
                    if reg.source == f"plugin:{plugin_name}":
                        tool_names.add(reg.name)
                # 在 unload 前（root_tracker 尚未被 pop）捕获被删插件所属根
                removed_root = self._root_of_plugin(plugin_name)
                removed_rank = _ROOT_KIND_PRIORITY.get(_root_kind(removed_root), 0)
                if tool_names:
                    unload_plugin_tools(plugin_name, tool_names, self._registry, root_tracker=self._root_tracker)
                self._loaded.pop(plugin_name, None)
                # 跨根覆盖恢复：仅重扫比被删根更低等级的根（被覆盖方）
                self._restore_lower_roots(removed_rank)

    def reload_plugin(self, plugin_name: str) -> None:
        """精准重载单个插件工具（安装/更新路径专用，不注销他插件工具）

        语义与 scan_now 的区别：只处理目标插件——注销其旧工具 → 恢复被其
        覆盖的低等级根同名工具 → 重新注册该插件当前模块。不波及 system 等
        其他插件（避免「装/改一个插件全量卸载+重载全部工具」的抖动）。

        跨根覆盖恢复：与 unload_plugin 相同，注销后对更低等级根重扫一次，
        被覆盖工具按 root_kind 优先级自然恢复；随后重注册本插件（高等级根）
        再次按优先级覆盖 → 最终状态与全量重扫一致。

        P2-2 last-known-good：先试加载新模块（exec 层面），任一失败即放弃本次
        重载并保留旧注册（坏版本重载失败旧版仍可用；新版成功前旧 registry 项
        不清空）；同名注册冲突以新加载结果为准（旧项已随成功路径注销）。
        """
        with self._scan_lock:
            pending: list = []
            for root in self._roots:
                root_path = Path(root)
                for pname, py in _iter_tool_modules(root_path):
                    if pname != plugin_name:
                        continue
                    try:
                        _load_module(plugin_name, py, root_kind=_root_kind(root_path))
                    except Exception as e:
                        logger.warning(
                            f"[PluginToolLoader] 插件 {plugin_name} 新模块加载失败，"
                            f"保留旧注册（last-known-good）: {e}"
                        )
                        return
                    pending.append((root_path, py))

            with self._registry.notify_batch():
                tool_names = set(self._loaded.get(plugin_name, set()))
                # 兜底：_loaded 未跟踪但 registry 残留的同源工具（防御状态漂移）
                for reg in self._registry.list():
                    if reg.source == f"plugin:{plugin_name}":
                        tool_names.add(reg.name)
                # 在 unload 前（root_tracker 尚未被 pop）捕获目标插件所属根
                removed_root = self._root_of_plugin(plugin_name)
                removed_rank = _ROOT_KIND_PRIORITY.get(_root_kind(removed_root), 0)
                if tool_names:
                    unload_plugin_tools(plugin_name, tool_names, self._registry, root_tracker=self._root_tracker)
                self._loaded.pop(plugin_name, None)
                # 跨根覆盖恢复：仅重扫比目标根更低等级的根（被覆盖方）
                self._restore_lower_roots(removed_rank)
                # 重注册该插件当前模块（enabled 过滤与 load_plugin_tools 一致）
                if not _is_plugin_enabled(plugin_name):
                    logger.info(f"[PluginToolLoader] 跳过已禁用插件的工具: {plugin_name}")
                    return
                # D9：组件级禁用——tools 子项被关后不再重注册
                if not _is_component_enabled(plugin_name):
                    logger.info(f"[PluginToolLoader] 跳过 tools 组件已禁用的插件: {plugin_name}")
                    return
                # P1/P2：版本/平台门禁——load_blocked 插件不重注册其工具
                if _is_plugin_load_blocked(plugin_name):
                    logger.warning(f"[PluginToolLoader] 跳过被门禁拦截插件的工具: {plugin_name}")
                    return
                new_names: Set[str] = set()
                for root_path, py in pending:
                    try:
                        new_names.update(
                            _run_register(self._registry, plugin_name, py, root_path, self._root_tracker)
                        )
                    except Exception as e:
                        # exec 已在试加载阶段验证通过；此处失败属 register 期错误，
                        # 旧项已注销无法原地恢复——warning 上报（与既有语义一致）
                        logger.warning(f"[PluginToolLoader] 插件 {plugin_name} register 失败: {e}")
                if new_names:
                    self._loaded[plugin_name] = new_names

    def on_tools_reloaded(self, listener: Callable[[], None]) -> None:
        """注册热重载完成监听（后台线程回调；仅 watcher 轮询检测到变更时触发）"""
        self._reload_listeners.append(listener)

    def _notify_reloaded(self) -> None:
        """通知全部监听者：一次轮询周期内的重扫已完成"""
        for listener in list(self._reload_listeners):
            try:
                listener()
            except Exception as e:
                logger.warning(f"[PluginToolWatcher] 热重载监听回调失败: {e}")

    def start(self, poll_interval: float = 2.0) -> None:
        """[已退役] 轮询线程不再启动。

        tools 组件变更改由 backend watchfiles 主链驱动：kernel
        KNOWN_COMPONENTS 已含 tools → _identify_all_components_from_changes
        识别 → builtin_reloaders._reload_tools 调 scan_now。本方法保留
        是为向后兼容旧调用点，空转即可。scan_now() 语义不变。
        """
        return

    def stop(self) -> None:
        self._stop = True

    def _signature(self) -> Tuple:
        """目录变更指纹：(path, mtime, size) 列表（多根聚合）"""
        sig = []
        for root in self._roots:
            for plugin_name, py in _iter_tool_modules(Path(root)):
                try:
                    st = py.stat()
                    sig.append((str(py), st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
        return tuple(sig)


# ========== 进程级惰性启动 ==========

_plugin_watcher: Optional[PluginToolWatcher] = None
_plugin_watcher_lock = threading.Lock()


def ensure_plugin_tool_watcher() -> Optional[PluginToolWatcher]:
    """确保插件工具热重载 watcher 已启动（进程级一次，幂等）"""
    global _plugin_watcher
    if _plugin_watcher is not None:
        return _plugin_watcher
    with _plugin_watcher_lock:
        if _plugin_watcher is not None:
            return _plugin_watcher
        try:
            _plugin_watcher = PluginToolWatcher()
            _plugin_watcher.start()
            logger.info("[PluginToolWatcher] 插件工具热重载 watcher 已启动")
        except Exception as e:
            logger.warning(f"[PluginToolWatcher] watcher 启动失败: {e}")
    return _plugin_watcher
