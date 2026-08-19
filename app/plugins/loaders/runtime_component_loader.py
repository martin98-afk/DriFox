# -*- coding: utf-8 -*-
"""运行时组件加载器 — 扫描 plugins/*/{model_adapters,loop_policies,storages}/*.py
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

    def __getattr__(self, name):
        return getattr(self._registry, name)

    def register(self, item, source: str = "") -> None:
        item_id = getattr(item, "id", None)
        if item_id is not None:
            with self._lock:
                held = self._occupied.get(item_id)
                if held is not None:
                    if held == self._kind:
                        logger.debug(
                            f"[RuntimeLoader] {item_id} 已被同根实现占用，跳过覆盖"
                        )
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
            # 1) 清理上一次扫描记录过的 plugin:* 来源
            sources = set(self._sources)
            self._sources.clear()
            self._occupied.clear()
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

    def _loop(self) -> None:
        while not self._stop.wait(5.0):
            try:
                if self._snapshot() != self._sigs:
                    self.scan_now()
                    logger.info(f"[RuntimeWatcher:{self._name}] 变更触发重扫")
            except Exception as e:
                logger.warning(f"[RuntimeWatcher:{self._name}] 轮询异常: {e}")

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name=f"rt-watcher-{self._name}"
            )
            self._thread.start()


_adapters_loader: Optional[RuntimeComponentLoader] = None
_loop_loader: Optional[RuntimeComponentLoader] = None
_storage_loader: Optional[RuntimeComponentLoader] = None
_adapters_watcher: Optional[_RuntimeWatcher] = None
_loop_watcher: Optional[_RuntimeWatcher] = None
_storage_watcher: Optional[_RuntimeWatcher] = None
_watchers_lock = threading.Lock()


def _make_adapters_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    return RuntimeComponentLoader("model_adapters", ModelAdapterRegistry.get_instance())


def _make_loop_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    return RuntimeComponentLoader("loop_policies", LoopPolicyRegistry.get_instance())


def _make_storage_loader() -> RuntimeComponentLoader:
    from app.plugins.registries.storage_registry import StorageRegistry

    return RuntimeComponentLoader("storages", StorageRegistry.get_instance())


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


def warmup_runtime_components() -> Dict[str, Set[str]]:
    """启动期一次性加载三类运行时组件（内置实现先注册，插件可覆盖）"""
    from app.plugins.builtin_runtime import ensure_builtin_runtime

    ensure_builtin_runtime()
    result: Dict[str, Set[str]] = {}
    result["model_adapters"] = _make_adapters_loader().scan_roots()
    result["loop_policies"] = _make_loop_loader().scan_roots()
    result["storages"] = _make_storage_loader().scan_roots()
    return result