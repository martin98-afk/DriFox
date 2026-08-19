# -*- coding: utf-8 -*-
"""
服务商插件加载器 — 扫描 plugins/*/providers/*.py 的 register(registry) 入口

约定（与 tools 插件完全对称）：
- 插件目录：`plugins/<name>/providers/<provider>.py`
- 每个文件暴露 `register(registry)` 函数（入参 ProviderRegistry 代理），
  内部调用 registry.register(ProviderDef(...)) 或 registry.define(name=..., ...)
- 服务商定义（icon/url/模型/能力/余额/用量 fetcher）完全由插件内联声明

扫描范围（含系统插件）：
- 工作树 `plugins/`（含 `plugins/system/providers/` 系统内置服务商插件）
- 用户插件目录 `<app_data>/plugins/`

热重载：
- 后台线程轮询签名（path, mtime, size），变更触发全量重扫
- 幂等：重扫按插件名清理已不存在文件的注册
- 优先级：user 插件可覆盖 system 插件的同名服务商（user > system）
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from loguru import logger

from app.plugins.registries.provider_registry import ProviderRegistry

# 插件根等级：system < user。
_ROOT_KIND_SYSTEM = "system"
_ROOT_KIND_USER = "user"
_ROOT_KIND_PRIORITY: Dict[str, int] = {_ROOT_KIND_SYSTEM: 0, _ROOT_KIND_USER: 1}


def _plugin_roots() -> List[Path]:
    """服务商插件扫描根：项目 plugins/（含 system）+ 用户插件目录"""
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


_PLUGIN_ROOTS: List[Path] = _plugin_roots()


def _iter_provider_modules(plugin_root: Path):
    """遍历插件目录下所有 providers/*.py"""
    if not plugin_root.is_dir():
        return
    for plugin_dir in sorted(plugin_root.iterdir()):
        if not plugin_dir.is_dir():
            continue
        providers_dir = plugin_dir / "providers"
        if providers_dir.is_dir():
            for py in sorted(providers_dir.glob("*.py")):
                if py.name.startswith("_"):
                    continue
                yield plugin_dir.name, py


class _ProviderRegistryProxy:
    """服务商注册代理 — 强制注入 source 与同名覆盖规则。

    插件代码的 `register(registry)` 收到的是本代理而非裸 ProviderRegistry：
    - source 强制为 `plugin:<plugin_name>`（热重载清理依赖）
    - 同名覆盖规则（root_kind 优先级）：
        * 同根内：已被其他插件占用时不覆盖（先注册者优先；同插件重扫由 watcher 卸载兜底）
        * 跨根：user 插件（高优先级）可覆盖 system 插件（低优先级）的同名服务商
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        plugin_name: str,
        root: Optional[Path] = None,
        root_tracker: Optional[dict] = None,
    ):
        self._registry = registry
        self._plugin_name = plugin_name
        self._root = root
        self._root_kind = _root_kind(root)
        self._root_tracker = root_tracker if root_tracker is not None else {}
        self.registered_names: Set[str] = set()

    def register(self, provider, source: str = "") -> bool:
        """注册一个服务商定义（ProviderDef 或任意带 source 属性的定义对象）

        返回 True 表示注册成功；False 表示被同名保护拒绝或 ProviderDef 非法。
        """
        from app.plugins.registries.provider_registry import ProviderDef

        if isinstance(provider, ProviderDef):
            name = provider.name
        elif hasattr(provider, "name"):
            name = provider.name
        else:
            logger.warning(f"[ProviderLoader] 非法服务商定义（缺少 name）: {provider}")
            return False

        # 同名覆盖判定（root_kind 优先级）
        src_root = self._root_tracker.get(name) if self._root is not None else None
        existing = self._registry.get(name)
        is_cross_root = src_root is not None and src_root != self._root
        if is_cross_root:
            # 跨根：仅允许高等级覆盖低等级
            if _ROOT_KIND_PRIORITY.get(self._root_kind, 0) <= _ROOT_KIND_PRIORITY.get(_root_kind(src_root), 0):
                return False
        else:
            # 同根/无 root/首次注册：已被其他插件占用时不覆盖
            if existing is not None and existing.source != f"plugin:{self._plugin_name}":
                return False

        # 覆盖场景：先卸载旧来源（跨根 user 覆盖 system，或无 root 追踪时的同插件重载）
        if existing is not None and existing.source != f"plugin:{self._plugin_name}":
            self._registry.clear_source(existing.source)

        source = source or f"plugin:{self._plugin_name}"
        ok = self._registry.register(provider, source=source)
        if ok:
            self.registered_names.add(name)
            if self._root is not None:
                self._root_tracker[name] = self._root
        return ok


def _load_module(plugin_name: str, path: Path):
    """加载服务商插件模块（唯一模块名，避免命名冲突；显式 compile 绕过 pyc 缓存）"""
    mod_name = f"_plugin_provider_{plugin_name}_{path.stem}"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        logger.warning(f"[ProviderLoader] 无法加载 {path}")
        return None
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__builtins__"] = __builtins__
    sys.modules[mod_name] = module
    try:
        source = path.read_text(encoding="utf-8")
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)
    except Exception as e:
        logger.warning(f"[ProviderLoader] 加载 {path} 失败: {e}")
        sys.modules.pop(mod_name, None)
        return None
    return module


def _run_register(registry: ProviderRegistry, plugin_name: str, py_path: Path, root: Path, root_tracker: dict) -> Set[str]:
    """执行单个 providers/*.py 的 register(registry)，返回注册成功的服务商名集合"""
    module = _load_module(plugin_name, py_path)
    if module is None:
        return set()
    register_fn = getattr(module, "register", None)
    if not callable(register_fn):
        logger.warning(f"[ProviderLoader] {py_path} 缺少 register(registry) 入口")
        return set()

    proxy = _ProviderRegistryProxy(registry, plugin_name, root, root_tracker)
    try:
        register_fn(proxy)
    except Exception:
        # 部分成功回滚：失败插件不留下半套服务商定义
        for name in proxy.registered_names:
            p = registry.get(name)
            if p is not None and p.source == f"plugin:{plugin_name}":
                registry.clear_source(f"plugin:{plugin_name}")
                logger.warning(f"[ProviderLoader] 回滚部分注册服务商: {name} ({plugin_name})")
        raise
    names = proxy.registered_names
    if names:
        logger.info(f"[ProviderLoader] 插件 {plugin_name} 注册服务商: {sorted(names)}")
    return names


def load_providers(
    registry: Optional[ProviderRegistry] = None,
    plugin_roots: Optional[List[Path]] = None,
    root_tracker: Optional[Dict[str, Path]] = None,
) -> Dict[str, Set[str]]:
    """扫描并注册全部服务商插件（含系统插件）。

    Returns:
        {plugin_name: {provider_name, ...}} — 本次加载的插件服务商映射
    """
    registry = registry or ProviderRegistry.get_instance()
    roots = plugin_roots if plugin_roots is not None else _PLUGIN_ROOTS
    loaded: Dict[str, Set[str]] = {}
    if root_tracker is None:
        root_tracker = {}

    for root in roots:
        for plugin_name, py_path in _iter_provider_modules(Path(root)):
            try:
                new_names = _run_register(registry, plugin_name, py_path, Path(root), root_tracker)
                loaded.setdefault(plugin_name, set()).update(new_names)
            except Exception as e:
                logger.warning(f"[ProviderLoader] 插件 {plugin_name} register 失败: {e}")
    return loaded


def unload_provider_source(plugin_name: str, provider_names: Set[str], registry: Optional[ProviderRegistry] = None) -> None:
    """注销指定插件的服务商（热重载/插件卸载时调用，幂等）"""
    registry = registry or ProviderRegistry.get_instance()
    source = f"plugin:{plugin_name}"
    for name in provider_names:
        p = registry.get(name)
        if p is not None and p.source == source:
            registry.clear_source(source)
            logger.info(f"[ProviderLoader] 注销服务商: {name} ({plugin_name})")


class ProviderWatcher:
    """服务商插件热重载 watcher：轮询签名检测变更，变更时全量重扫（幂等）。

    与 PluginToolWatcher 同构：签名 (path, mtime, size) 变更 → 全量重扫。
    """

    def __init__(self, registry: Optional[ProviderRegistry] = None, roots: Optional[List[Path]] = None):
        self._registry = registry or ProviderRegistry.get_instance()
        self._roots = roots if roots is not None else _PLUGIN_ROOTS
        self._loaded: Dict[str, Set[str]] = {}  # plugin_name -> provider_names
        self._root_tracker: Dict[str, Path] = {}
        self._scan_lock = threading.Lock()
        self._thread = None
        self._stop = False

    def scan_now(self) -> None:
        """全量重扫：先注销已加载插件的全部服务商，再全量重新注册（幂等）"""
        with self._scan_lock:
            # 1) 注销已加载插件的全部服务商
            for plugin_name, old_names in self._loaded.items():
                unload_provider_source(plugin_name, old_names, self._registry)
                self._root_tracker = {}
            # 2) 全量重扫注册
            try:
                self._loaded = load_providers(
                    registry=self._registry,
                    plugin_roots=self._roots,
                    root_tracker=self._root_tracker,
                )
            except Exception as e:
                logger.warning(f"[ProviderWatcher] 全量重扫失败: {e}")

    def start(self, poll_interval: float = 2.0) -> None:
        """后台线程轮询监听"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False

        def _loop():
            last_sig = self._signature()
            while not self._stop:
                time.sleep(poll_interval)
                sig = self._signature()
                if sig != last_sig:
                    logger.info("[ProviderWatcher] 检测到服务商插件目录变更，重扫")
                    try:
                        self.scan_now()
                    except Exception as e:
                        logger.warning(f"[ProviderWatcher] 重扫失败: {e}")
                    last_sig = self._signature()

        self._thread = threading.Thread(target=_loop, daemon=True, name="provider-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _signature(self) -> Tuple:
        """目录变更指纹：(path, mtime, size) 列表（多根聚合）"""
        sig = []
        for root in self._roots:
            for plugin_name, py in _iter_provider_modules(Path(root)):
                try:
                    st = py.stat()
                    sig.append((str(py), st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
        return tuple(sig)


# ========== 进程级惰性启动 ==========

_provider_watcher: Optional[ProviderWatcher] = None
_provider_watcher_lock = threading.Lock()


def ensure_provider_watcher() -> Optional[ProviderWatcher]:
    """确保服务商插件热重载 watcher 已启动（进程级一次，幂等）"""
    global _provider_watcher
    if _provider_watcher is not None:
        return _provider_watcher
    with _provider_watcher_lock:
        if _provider_watcher is not None:
            return _provider_watcher
        try:
            _provider_watcher = ProviderWatcher()
            _provider_watcher.start()
            logger.info("[ProviderWatcher] 服务商插件热重载 watcher 已启动")
        except Exception as e:
            logger.warning(f"[ProviderWatcher] watcher 启动失败: {e}")
    return _provider_watcher


def warmup_providers() -> Dict[str, Set[str]]:
    """启动期一次性加载全部服务商插件（拉齐 system + user）。

    在 ProviderWatcher.start() 之前调用，保证主程序启动即有完整服务商表。
    """
    registry = ProviderRegistry.get_instance()
    return load_providers(registry=registry)