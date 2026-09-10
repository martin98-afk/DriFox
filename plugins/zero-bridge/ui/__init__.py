# -*- coding: utf-8 -*-
"""zero-bridge —— zero 插件引擎与 DriFox 的桥接组件（适配现有架构）。

职责边界：
- **监听 / 变更识别 / 触发**：全部复用 DriFox PluginHostService 现有链路，
  本插件不自建任何监听
- **zero 作为组件类型**：插件目录下 ``zero/`` 子目录（*.py 文件即插件），
  与 hooks/commands 等组件类型平级；kernel.KNOWN_COMPONENTS 已登记
- **重载分派**：zero-bridge 把 reloader 注册到 ComponentReloaderRegistry
  （kernel 支持"插件注册 reloader"），DriFox 识别出 zero 组件变更后
  自动分派到这里，转换为 zero 的「回滚 → 重装」

zero 插件格式（插件目录下 zero/xxx.py）：见 plugin_loader.py 模块文档。
DriFox 特定 import 全部延迟到函数内（插件加载时机早于主窗口就绪）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger

from .bridge import ZeroBridge

_PLUGIN_NAME = "zero-bridge"

_bridge: Optional[ZeroBridge] = None
_loader: Optional[Any] = None


def get_bridge() -> Optional[ZeroBridge]:
    """取桥接器单例（zero 插件从这里拿根上下文）。"""
    return _bridge


def get_loader() -> Optional[Any]:
    """取 zero 组件装载器（诊断 / 手动重载入口）。"""
    return _loader


def _state_provider() -> Dict[str, Any]:
    """收集宿主状态快照（全部弱引用，宿主回收自动失效）。"""
    from app.core.window_registry import alive_window_instances

    windows = alive_window_instances()
    state: Dict[str, Any] = {"windows": windows}
    if windows:
        state["main_window"] = windows[-1]
    return state


class _UIEventBusSource:
    """把 UIEventBus 适配成 bridge 需要的事件源契约。"""

    EVENTS = ("theme_changed", "tab_switched", "card_visibility_changed", "welcome_tab_refreshed")

    def events(self) -> List[str]:
        return list(self.EVENTS)

    def subscribe(self, event: str, callback) -> Any:
        from app.core.ui_event_bus import UIEventBus

        UIEventBus.get_instance().subscribe(event, callback, plugin_name=_PLUGIN_NAME)

        def _unsub() -> None:
            UIEventBus.get_instance().unsubscribe(event, callback)

        return _unsub


def _register_zero_reloader() -> None:
    """把 zero 组件的 reloader 注册进 kernel（插件侧注册，主系统零逻辑）。

    DriFox watcher 识别出 <插件>/zero/*.py 变更 → _reload_single_plugin
    → ComponentReloaderRegistry.reload(ReloadContext) → 本函数注册的入口。
    """
    from app.plugins.kernel import get_reloader_registry

    def _zero_reloader(reload_ctx) -> bool:
        loader = _loader
        if loader is None:
            return False
        return loader.handle_reload(reload_ctx)

    get_reloader_registry().register("zero", _zero_reloader)
    logger.debug("[zero-bridge] zero 组件 reloader 已注册到 kernel")


def _load_all_declared_zero_components() -> None:
    """扫描全部已登记 DriFox 插件，装载声明了 zero 组件的。"""
    from app.plugins.managers.plugin_manager import PluginManager

    loader = _loader
    if loader is None:
        return
    pm = PluginManager.get_instance()
    count = 0
    for info in pm.list_plugins():
        components = info.components or {}
        if components.get("zero") and not info.load_blocked:
            loader.load_plugin(info.name, info.path)
            count += 1
    if count:
        logger.info(f"[zero-bridge] 已装载 {count} 个插件的 zero 组件")


def register_ui(registry) -> None:
    """UI 组件注册入口（主窗口就绪后被调用，此刻桥接安全）。"""
    global _bridge, _loader

    if _bridge is not None and _bridge.started:
        logger.debug("[zero-bridge] 桥已启动，跳过重复注册")
        return

    try:
        from app.widgets.tab_manager_window import TabManagerWindow  # noqa: F401 探测主程序就绪

        bridge = ZeroBridge("drifox")
        bridge.start(event_source=_UIEventBusSource(), state_provider=_state_provider)
        _bridge = bridge

        from .plugin_loader import ZeroPluginLoader

        _loader = ZeroPluginLoader(bridge.root)
        _register_zero_reloader()

        from .core_services import register_core_services

        register_core_services(bridge.root)
        _load_all_declared_zero_components()

        logger.info("[zero-bridge] zero 运行时已接入 DriFox（root=drifox，4 事件通道）")
    except Exception as e:
        logger.warning(f"[zero-bridge] 桥接启动失败（不影响主程序）: {e}")


def unregister_plugin() -> None:
    """插件卸载：回滚全部 zero 组件 + 停桥（UIEventBus 订阅随 plugin_name 自动退订）。"""
    global _bridge, _loader
    if _loader is not None:
        for plugin_name in list(_loader.loaded_plugins()):
            _loader.unload_plugin(plugin_name)
        _loader = None
    if _bridge is not None:
        _bridge.stop()
        _bridge = None
        logger.info("[zero-bridge] zero 运行时已脱离 DriFox")
