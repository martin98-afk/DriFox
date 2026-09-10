# -*- coding: utf-8 -*-
"""zero-bridge —— zero 插件引擎与 DriFox 的桥接插件。

职责：
- 应用启动后创建 ZeroBridge（zero 根上下文）
- 把 UIEventBus 的四个事件桥进 zero（``host.<event>``）
- 把宿主状态喂进 ctx（``host.<key>``，弱引用，宿主回收自动失效）
- 提供窗口域访问器：``get_bridge().window(window_id)``

DriFox 特定 import 全部延迟到函数内（插件加载时机早于主窗口就绪）。
zero 插件接入方式：统一走 ``get_bridge()`` 拿根上下文。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger

from .bridge import ZeroBridge

_PLUGIN_NAME = "zero-bridge"

_bridge: Optional[ZeroBridge] = None


def get_bridge() -> Optional[ZeroBridge]:
    """取桥接器单例（zero 插件从这里拿根上下文）。"""
    return _bridge


def _state_provider() -> Dict[str, Any]:
    """收集宿主状态快照（全部弱引用，宿主回收自动失效）。"""
    from app.core.window_registry import alive_window_instances

    windows = alive_window_instances()
    state: Dict[str, Any] = {"windows": windows}
    if windows:
        # 最近一个存活窗口作为默认宿主入口（弱引用，不延长其寿命）
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


def register_ui(registry) -> None:
    """UI 组件注册入口（主窗口就绪后被调用，此刻桥接安全）。"""
    global _bridge

    if _bridge is not None and _bridge.started:
        logger.debug("[zero-bridge] 桥已启动，跳过重复注册")
        return

    try:
        from app.widgets.tab_manager_window import TabManagerWindow  # noqa: F401 探测主程序就绪

        bridge = ZeroBridge("drifox")
        bridge.start(event_source=_UIEventBusSource(), state_provider=_state_provider)
        _bridge = bridge
        logger.info("[zero-bridge] zero 运行时已接入 DriFox（root=drifox，4 事件通道）")
    except Exception as e:
        logger.warning(f"[zero-bridge] 桥接启动失败（不影响主程序）: {e}")


def unregister_plugin() -> None:
    """插件卸载：停桥（UIEventBus 的订阅随 plugin_name 自动退订，此处兜底）。"""
    global _bridge
    if _bridge is not None:
        _bridge.stop()
        _bridge = None
        logger.info("[zero-bridge] zero 运行时已脱离 DriFox")
