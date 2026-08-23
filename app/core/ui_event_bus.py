# -*- coding: utf-8 -*-
"""UI 事件总线 — 插件订阅主程序 UI 事件（主题切换/Tab 切换/卡片显隐等）

设计：
- 单例（与 UIPluginRegistry 生命周期对齐，reset_instance 供测试）
- 弱约定：事件名为字符串常量，payload 为 kwargs dict
- 回调异常隔离：单个订阅者抛异常不影响其他订阅者（记日志）
- 插件级退订：UIPluginRegistry.unload_plugin 时调用 unsubscribe_plugin，
  插件无需在 unload_ui 里手动退订（防止悬挂回调引用旧模块）
"""

from typing import Any, Callable, Dict, List, Optional

from loguru import logger

# ── 事件常量（payload 字段在注释中约定）──
EV_THEME_CHANGED = "theme_changed"  # payload: theme_id, theme_name, is_dark
EV_TAB_SWITCHED = "tab_switched"  # payload: tab_index, window_id
EV_CARD_VISIBILITY_CHANGED = "card_visibility_changed"  # payload: card_id, window_id, visible
EV_WINDOW_ACTIVATED = "window_activated"  # payload: window_id

_Callback = Callable[[Dict[str, Any]], None]


class UIEventBus:
    """UI 事件总线（单例）"""

    _instance: Optional["UIEventBus"] = None

    def __init__(self):
        # {event: [(plugin_name or None, callback), ...]}
        self._subs: Dict[str, List[tuple]] = {}

    @classmethod
    def get_instance(cls) -> "UIEventBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def subscribe(self, event: str, callback: _Callback, plugin_name: Optional[str] = None) -> None:
        """订阅事件；plugin_name 非空时随插件 unload 自动退订"""
        self._subs.setdefault(event, []).append((plugin_name, callback))

    def unsubscribe_plugin(self, plugin_name: str) -> None:
        """退订某插件的全部事件（unload_plugin 调用）"""
        for event in list(self._subs.keys()):
            self._subs[event] = [pair for pair in self._subs[event] if pair[0] != plugin_name]
            if not self._subs[event]:
                del self._subs[event]

    def publish(self, event: str, **payload: Any) -> None:
        """发布事件；订阅回调异常隔离（吞掉记 warning）"""
        for plugin_name, callback in list(self._subs.get(event, [])):
            try:
                callback(payload)
            except Exception as e:
                logger.warning(f"[UIEventBus] {event} 订阅回调异常 ({plugin_name}): {e}")

    def subscriptions(self) -> Dict[str, int]:
        """当前各事件订阅数（调试/测试）"""
        return {event: len(pairs) for event, pairs in self._subs.items()}
