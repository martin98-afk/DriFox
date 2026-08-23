# -*- coding: utf-8 -*-
"""运行时组件加载器 — 扫描 plugins/*/{model_adapters,loop_policies,storages,serializers}/*.py
的 register(registry) 入口（与 tools/providers 插件完全对称的约定）。

插件注册代理强制 source="plugin:<name>"（热重载清理依赖）；
user 根可覆盖 system 根同名实现（user > system，对齐 provider_loader）。
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from loguru import logger

# 插件根等级：system < user（对齐 provider_loader._ROOT_KIND_PRIORITY）
_ROOT_KIND_SYSTEM = "system"
_ROOT_KIND_USER = "user"
_ROOT_KIND_PRIORITY: Dict[str, int] = {_ROOT_KIND_SYSTEM: 0, _ROOT_KIND_USER: 1}


def _plugin_roots() -> List[Path]:
    """扫描根：项目 plugins/（含 system）+ 用户插件目录（同 provider_loader）"""
    roots = [Path(__file__).resolve().parent.parent.parent.parent / "plugins"]
    try:
        from app.utils.utils import get_app_data_dir

        roots.append(get_app_data_dir() / "plugins")
    except Exception:
        pass
    return roots


def _root_kind(root: Optional[Path]) -> str:
    """识别 root 等级：项目工作树 → system；app_data/plugins → user"""
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


def _is_plugin_enabled(plugin_name: str) -> bool:
    """按插件启用状态过滤运行时组件加载（对齐 provider_loader._is_plugin_enabled）。

    统一以 Settings.enabled_plugins 为准。pm 未初始化时（真实启动链中
    app.plugins 可能在 backend.initialize 之前被导入）直接读 Settings。
    """
    try:
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if pm.is_initialized():
            if not pm.has_plugin(plugin_name):
                return True
            return pm.is_enabled(plugin_name)
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        saved = cfg.enabled_plugins.value or []
        disabled = cfg.disabled_plugins.value or []
        if not saved and not disabled:
            return True
        return plugin_name in saved
    except Exception as e:
        logger.warning(f"[RuntimeLoader] 启用状态检查失败，默认加载 {plugin_name}: {e}")
        return True


class _RegistryProxy:
    """注册代理 — 强制 source + 跨根覆盖规则（user > system，对齐 provider_loader）

    插件代码的 `register(registry)` 收到的是本代理而非裸 registry：
    - source 强制为 `plugin:<plugin_name>`（热重载清理依赖）
    - 跨根覆盖规则（root_kind 优先级）：
        * 同 kind：已被同根其他插件占用时跳过（先注册者优先）
        * 跨 kind：本 proxy 优先级高 → 覆盖（更新 occupied + registry.register）
        * 跨 kind：本 proxy 优先级低 → 跳过（防低等级覆盖高等级）
    """

    def __init__(
        self,
        registry,
        plugin_name: str,
        kind: str,
        occupied: Dict[str, str],
        lock: threading.Lock,
    ):
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_source", f"plugin:{plugin_name}")
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_occupied", occupied)  # id -> kind
        object.__setattr__(self, "_lock", lock)
        # 本次 register 成功占据（含覆盖）的 item id 集——loader 用于精准卸载时
        # 释放 _occupied 条目（否则低等级根恢复扫描会被残留占用挡住）
        object.__setattr__(self, "_occupied_items", set())

    def __getattr__(self, name):
        return getattr(self._registry, name)

    def register(self, item, source: str = "") -> None:
        item_id = getattr(item, "id", None)
        if item_id is not None:
            with self._lock:
                held = self._occupied.get(item_id)
                if held is not None:
                    if held == self._kind:
                        logger.debug(f"[RuntimeLoader] {item_id} 已被同根实现占用，跳过覆盖")
                        return
                    held_pri = _ROOT_KIND_PRIORITY.get(held, 0)
                    self_pri = _ROOT_KIND_PRIORITY.get(self._kind, 0)
                    if self_pri <= held_pri:
                        logger.debug(
                            f"[RuntimeLoader] {item_id} 已被高/同优先级根占用"
                            f"（held={held}, self={self._kind}），跳过覆盖"
                        )
                        return
                self._occupied[item_id] = self._kind
                self._occupied_items.add(item_id)
        self._registry.register(item, source=self._source)


class RuntimeComponentLoader:
    """一类运行时组件的扫描+注册+清理器（watcher 轮询复用 tools/providers 模式）"""

    def __init__(
        self,
        comp_dir: str,
        registry,
        register_attr: str = "register",
        unregister_attr: str = "unregister_source",
    ):
        self._comp_dir = comp_dir
        self._registry = registry
        self._register_attr = register_attr
        self._unregister_attr = unregister_attr
        self._occupied: Dict[str, str] = {}
        self._sources: Set[str] = set()
        self._lock = threading.Lock()
        # 精准卸载状态：source -> 占据的 item id 集（_occupied 反向索引）
        self._occupied_by_source: Dict[str, Set[str]] = {}
        # source -> root kind（磁盘目录被移走后仍能判定优先级，用于覆盖恢复）
        self._source_kind: Dict[str, str] = {}
        # 最近一次 scan_roots 的扫描根（精准方法复用；未扫过时回退全局根）
        self._scan_roots_cache: Optional[List[Path]] = None
        # scan_roots 整体互斥：防并发 scan 双重 unregister / 重复注册
        # （对齐 ProviderWatcher.scan_now 的 _scan_lock 模式）
        self._scan_lock = threading.Lock()

    def scan_roots(self, roots: Optional[List[Path]] = None) -> Set[str]:
        """全量重扫（幂等，整体持锁）：先注销 loader 记录过的 plugin:* 来源，再按 root 顺序注册。

        与 ProviderWatcher.scan_now 同构：清理 → 注册 全程在 _scan_lock 内串行执行。
        """
        roots = roots if roots is not None else _plugin_roots()
        unregister = getattr(self._registry, self._unregister_attr)
        with self._scan_lock:
            self._scan_roots_cache = list(roots)
            # 1) 清理上一次扫描记录过的 plugin:* 来源
            sources = set(self._sources)
            self._sources.clear()
            self._occupied.clear()
            self._occupied_by_source.clear()
            self._source_kind.clear()
            for s in sources:
                unregister(s)
            # 2) 全量重扫注册
            loaded: Set[str] = set()
            for root in roots:
                kind = _root_kind(root)
                if not root.is_dir():
                    continue
                for plugin_dir in sorted(root.iterdir()):
                    if not plugin_dir.is_dir():
                        continue
                    comp = plugin_dir / self._comp_dir
                    if not comp.is_dir():
                        continue
                    if not _is_plugin_enabled(plugin_dir.name):
                        continue
                    for py in sorted(comp.glob("*.py")):
                        if py.name.startswith("_"):
                            continue
                        if self._load_module(py, plugin_dir.name, kind):
                            loaded.add(plugin_dir.name)
            return loaded

    def _rescan_roots_below(self, rank: int) -> None:
        """覆盖恢复：重扫比目标根更低等级的所有根（被覆盖方），补注册被覆盖组件

        仅重扫低等级根（如 user 插件卸载后重扫 system 根），其模块的 register
        经 _RegistryProxy 的「高/同优先级根占用跳过」保护：已存在的他插件组件
        不被覆盖，仅补注册因目标插件注销而缺失的同名组件（跨根覆盖恢复）。
        """
        roots = self._scan_roots_cache or _plugin_roots()
        for root in roots:
            if _ROOT_KIND_PRIORITY.get(_root_kind(root), 0) >= rank:
                continue
            if not root.is_dir():
                continue
            for plugin_dir in sorted(root.iterdir()):
                comp = plugin_dir / self._comp_dir
                if not comp.is_dir():
                    continue
                if not _is_plugin_enabled(plugin_dir.name):
                    continue
                for py in sorted(comp.glob("*.py")):
                    if py.name.startswith("_"):
                        continue
                    self._load_module(py, plugin_dir.name, _root_kind(root))

    def _unload_source(self, plugin_name: str) -> int:
        """注销单插件来源并释放其占据的 item（_occupied 反向索引），返回移除的 item 数"""
        source = f"plugin:{plugin_name}"
        items = self._occupied_by_source.pop(source, set())
        for it in items:
            self._occupied.pop(it, None)
        unregister = getattr(self._registry, self._unregister_attr)
        unregister(source)
        self._sources.discard(source)
        self._source_kind.pop(source, None)
        return len(items)

    def unload_plugin(self, plugin_name: str) -> None:
        """精准卸载单个插件的运行时组件（删除/禁用路径），不波及他插件

        与 scan_roots 的区别：scan_roots 先注销全部 plugin:* 来源再全量重注册，
        会波及 system 等无关插件。本方法只注销目标插件来源，并对更低等级根
        重扫一次恢复被其覆盖的同名组件（_rescan_roots_below）。
        """
        with self._scan_lock:
            # 先捕获来源等级（_unload_source 会清除 _source_kind 记录）
            removed_rank = _ROOT_KIND_PRIORITY.get(self._source_kind.get(f"plugin:{plugin_name}", "system"), 0)
            removed_items = self._unload_source(plugin_name)
            if removed_items:
                logger.info(f"[RuntimeLoader] 注销插件组件: {plugin_name}（{removed_items} 项）")
            # 跨根覆盖恢复：仅重扫比目标根更低等级的根（被覆盖方）
            self._rescan_roots_below(removed_rank)

    def reload_plugin(self, plugin_name: str) -> None:
        """精准重载单个插件的运行时组件（安装/更新/启用路径），不波及他插件

        语义：注销该插件旧组件 → 恢复被其覆盖的低等级根同名组件 → 重新注册
        该插件当前模块（高等级根再次覆盖）→ 最终状态与全量重扫一致。
        """
        with self._scan_lock:
            # 先捕获来源等级（_unload_source 会清除 _source_kind 记录）
            removed_rank = _ROOT_KIND_PRIORITY.get(self._source_kind.get(f"plugin:{plugin_name}", "system"), 0)
            removed_items = self._unload_source(plugin_name)
            if removed_items:
                logger.info(f"[RuntimeLoader] 重载前注销插件组件: {plugin_name}（{removed_items} 项）")
            # 跨根覆盖恢复：仅重扫比目标根更低等级的根（被覆盖方）
            self._rescan_roots_below(removed_rank)
            # 重注册该插件当前模块（enabled 过滤与 scan_roots 一致）
            if not _is_plugin_enabled(plugin_name):
                logger.info(f"[RuntimeLoader] 跳过已禁用插件的组件: {plugin_name}")
                return
            roots = self._scan_roots_cache or _plugin_roots()
            for root in roots:
                if not (root / plugin_name).is_dir():
                    continue
                comp = root / plugin_name / self._comp_dir
                if not comp.is_dir():
                    continue
                for py in sorted(comp.glob("*.py")):
                    if py.name.startswith("_"):
                        continue
                    self._load_module(py, plugin_name, _root_kind(root))

    def _load_module(self, py: Path, plugin_name: str, kind: str) -> bool:
        mod_name = f"drifox_rt_{self._comp_dir}_{plugin_name}_{py.stem}"
        # 防模块 GC 回收导致插件类定义丢失（对齐 provider_loader._load_module）
        sys.modules.pop(mod_name, None)
        try:
            spec = importlib.util.spec_from_file_location(mod_name, py)
            if spec is None or spec.loader is None:
                return False
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            register = getattr(mod, "register", None)
            if not callable(register):
                logger.warning(f"[RuntimeLoader] {py} 缺少 register(registry)，跳过")
                return False
            proxy = _RegistryProxy(self._registry, plugin_name, kind, self._occupied, self._lock)
            register(proxy)
            # 注册成功（即使代理内部跳过）即记录 source，便于下次重扫清理
            with self._lock:
                self._sources.add(f"plugin:{plugin_name}")
                self._occupied_by_source.setdefault(f"plugin:{plugin_name}", set()).update(proxy._occupied_items)
                self._source_kind[f"plugin:{plugin_name}"] = kind
            return True
        except Exception as e:
            logger.error(f"[RuntimeLoader] 加载 {py} 失败: {e}")
            sys.modules.pop(mod_name, None)
            return False


class _RuntimeWatcher:
    """轻量轮询 watcher（签名集变更触发 scan_roots 全量重扫）"""

    def __init__(self, loader: RuntimeComponentLoader, name: str):
        self._loader = loader
        self._name = name
        self._sigs: Dict[Path, Tuple[float, int]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _snapshot(self) -> Dict[Path, Tuple[float, int]]:
        sigs: Dict[Path, Tuple[float, int]] = {}
        for root in _plugin_roots():
            if not root.is_dir():
                continue
            for plugin_dir in root.iterdir():
                comp = plugin_dir / self._loader._comp_dir
                if comp.is_dir():
                    for py in comp.glob("*.py"):
                        try:
                            st = py.stat()
                            sigs[py] = (st.st_mtime, st.st_size)
                        except OSError:
                            pass
        return sigs

    def scan_now(self) -> None:
        self._loader.scan_roots()
        self._sigs = self._snapshot()

    def unload_plugin(self, plugin_name: str) -> None:
        """精准卸载单插件（透传 loader，不触发全量重扫）"""
        self._loader.unload_plugin(plugin_name)

    def reload_plugin(self, plugin_name: str) -> None:
        """精准重载单插件（透传 loader，不触发全量重扫）"""
        self._loader.reload_plugin(plugin_name)

    def _loop(self) -> None:
        while not self._stop.wait(5.0):
            try:
                new_sigs = self._snapshot()
                if new_sigs == self._sigs:
                    continue
                # 精准重扫：按快照 diff 识别变更文件所属插件，逐个精准重载/卸载，
                # 不再全量注销+重注册全部插件（避免「改一个插件波及全部同类组件」）
                old_paths = set(self._sigs)
                new_paths = set(new_sigs)
                changed_plugins: Set[str] = set()
                removed_paths = old_paths - new_paths
                for p in removed_paths:
                    name = self._plugin_name_of(p)
                    if name:
                        changed_plugins.add(name)
                for p in old_paths & new_paths:
                    if self._sigs[p] != new_sigs[p]:
                        name = self._plugin_name_of(p)
                        if name:
                            changed_plugins.add(name)
                for p in new_paths - old_paths:
                    name = self._plugin_name_of(p)
                    if name:
                        changed_plugins.add(name)
                for name in changed_plugins:
                    # 插件目录仍存在 → 精准重载；已删除 → 精准卸载
                    if any((Path(root) / name).is_dir() for root in _plugin_roots()):
                        self._loader.reload_plugin(name)
                    else:
                        self._loader.unload_plugin(name)
                    logger.info(f"[RuntimeWatcher:{self._name}] 精准重载插件 {name}")
                self._sigs = new_sigs
            except Exception as e:
                logger.warning(f"[RuntimeWatcher:{self._name}] 轮询异常: {e}")

    @staticmethod
    def _plugin_name_of(py_path: Path) -> str:
        """从快照路径提取插件名（结构：<root>/<plugin>/<comp_dir>/*.py）"""
        try:
            parts = Path(py_path).parts
            return parts[-3] if len(parts) >= 3 else ""
        except Exception:
            return ""

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name=f"rt-watcher-{self._name}")
            self._thread.start()


_adapters_loader: Optional[RuntimeComponentLoader] = None
_loop_loader: Optional[RuntimeComponentLoader] = None
_hook_loader: Optional[RuntimeComponentLoader] = None
_storage_loader: Optional[RuntimeComponentLoader] = None
_serializer_loader: Optional[RuntimeComponentLoader] = None
_gateway_loader: Optional[RuntimeComponentLoader] = None
_engine_loader: Optional[RuntimeComponentLoader] = None
_adapters_watcher: Optional[_RuntimeWatcher] = None
_loop_watcher: Optional[_RuntimeWatcher] = None
_hook_watcher: Optional[_RuntimeWatcher] = None
_storage_watcher: Optional[_RuntimeWatcher] = None
_serializer_watcher: Optional[_RuntimeWatcher] = None
_gateway_watcher: Optional[_RuntimeWatcher] = None
_engine_watcher: Optional[_RuntimeWatcher] = None
_watchers_lock = threading.Lock()


def _make_adapters_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    return RuntimeComponentLoader("model_adapters", ModelAdapterRegistry.get_instance())


def _make_loop_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    return RuntimeComponentLoader("loop_policies", LoopPolicyRegistry.get_instance())


def _make_hook_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.hook_policy_registry import HookPolicyRegistry

    return RuntimeComponentLoader("hook_policies", HookPolicyRegistry.get_instance())


def _make_storage_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.storage_registry import StorageRegistry

    return RuntimeComponentLoader("storages", StorageRegistry.get_instance())


def _make_serializer_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.serializer_registry import SerializerRegistry

    return RuntimeComponentLoader("serializers", SerializerRegistry.get_instance())


def _make_gateway_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    return RuntimeComponentLoader("gateways", GatewayPlatformRegistry.get_instance())


def _make_engine_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.engine_registry import EngineRegistry

    return RuntimeComponentLoader("engines", EngineRegistry.get_instance())


def ensure_model_adapter_watcher() -> Optional[_RuntimeWatcher]:
    global _adapters_loader, _adapters_watcher
    with _watchers_lock:
        if _adapters_watcher is not None:
            return _adapters_watcher
        _adapters_loader = _adapters_loader or _make_adapters_loader()
        _adapters_watcher = _RuntimeWatcher(_adapters_loader, "model_adapters")
        _adapters_watcher.scan_now()
        _adapters_watcher.start()
        return _adapters_watcher


def ensure_loop_policy_watcher() -> Optional[_RuntimeWatcher]:
    global _loop_loader, _loop_watcher
    with _watchers_lock:
        if _loop_watcher is not None:
            return _loop_watcher
        _loop_loader = _loop_loader or _make_loop_loader()
        _loop_watcher = _RuntimeWatcher(_loop_loader, "loop_policies")
        _loop_watcher.scan_now()
        _loop_watcher.start()
        return _loop_watcher


def ensure_hook_policy_watcher() -> Optional[_RuntimeWatcher]:
    global _hook_loader, _hook_watcher
    with _watchers_lock:
        if _hook_watcher is not None:
            return _hook_watcher
        _hook_loader = _hook_loader or _make_hook_loader()
        _hook_watcher = _RuntimeWatcher(_hook_loader, "hook_policies")
        _hook_watcher.scan_now()
        _hook_watcher.start()
        return _hook_watcher


def ensure_storage_watcher() -> Optional[_RuntimeWatcher]:
    global _storage_loader, _storage_watcher
    with _watchers_lock:
        if _storage_watcher is not None:
            return _storage_watcher
        _storage_loader = _storage_loader or _make_storage_loader()
        _storage_watcher = _RuntimeWatcher(_storage_loader, "storages")
        _storage_watcher.scan_now()
        _storage_watcher.start()
        return _storage_watcher


def ensure_serializer_watcher() -> Optional[_RuntimeWatcher]:
    global _serializer_loader, _serializer_watcher
    with _watchers_lock:
        if _serializer_watcher is not None:
            return _serializer_watcher
        _serializer_loader = _serializer_loader or _make_serializer_loader()
        _serializer_watcher = _RuntimeWatcher(_serializer_loader, "serializers")
        _serializer_watcher.scan_now()
        _serializer_watcher.start()
        return _serializer_watcher


def ensure_gateway_watcher() -> Optional[_RuntimeWatcher]:
    global _gateway_loader, _gateway_watcher
    with _watchers_lock:
        if _gateway_watcher is not None:
            return _gateway_watcher
        _gateway_loader = _gateway_loader or _make_gateway_loader()
        _gateway_watcher = _RuntimeWatcher(_gateway_loader, "gateways")
        _gateway_watcher.scan_now()
        _gateway_watcher.start()
        return _gateway_watcher


def ensure_engine_watcher() -> Optional[_RuntimeWatcher]:
    global _engine_loader, _engine_watcher
    with _watchers_lock:
        if _engine_watcher is not None:
            return _engine_watcher
        _engine_loader = _engine_loader or _make_engine_loader()
        _engine_watcher = _RuntimeWatcher(_engine_loader, "engines")
        _engine_watcher.scan_now()
        _engine_watcher.start()
        return _engine_watcher


def warmup_runtime_components() -> Dict[str, Set[str]]:
    """启动期一次性加载五类运行时组件（系统插件 plugins/system 提供默认实现）。

    五类运行时组件（model_adapters / loop_policies / storages / serializers / gateways / engines）
    的默认实现现已迁入系统插件（plugins/system/{model_adapters,loop_policies,storages,
    serializers,gateways}/），不再需要 builtin 层兜底。registry 完全由插件目录扫描结果填充。
    """
    result: Dict[str, Set[str]] = {}
    result["model_adapters"] = _make_adapters_loader().scan_roots()
    result["loop_policies"] = _make_loop_loader().scan_roots()
    result["hook_policies"] = _make_hook_loader().scan_roots()
    result["storages"] = _make_storage_loader().scan_roots()
    result["serializers"] = _make_serializer_loader().scan_roots()
    result["gateways"] = _make_gateway_loader().scan_roots()
    result["engines"] = _make_engine_loader().scan_roots()
    return result
