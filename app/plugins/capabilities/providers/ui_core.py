# -*- coding: utf-8 -*-
"""UI surface 的能力 provider（host = MainWidget）。"""

from __future__ import annotations

from typing import Any

from loguru import logger


def _resolve_tab_window():
    """延迟解析 TabManagerWindow 单例（测试可 monkeypatch 本函数）。"""
    try:
        from app.widgets.tab_manager_window import TabManagerWindow

        return TabManagerWindow.get_instance()
    except Exception:
        return None


def _stream_subscribe(host: Any, ctx: Any):
    """返回 subscribe(events, callback) -> 已绑定信号名列表。

    events 支持两种形态：
    - 列表/字符串 + 单一 callback：所有信号共用一个回调（按参数形状自行分派）
    - 映射 {事件名: 回调}：每个信号各自的回调（同签名信号无法按参数区分时用这个）

    连接登记到 CapabilityRegistry.track_subscription，插件卸载时由
    UIPluginRegistry.unload_plugin → release_plugin 统一断开。
    """

    def subscribe(events, callback=None):
        backend = getattr(host, "backend", None)
        if backend is None:
            raise RuntimeError("backend 未就绪，无法订阅对话流事件")
        if isinstance(events, dict):
            pairs = list(events.items())
        else:
            names = [events] if isinstance(events, str) else list(events)
            pairs = [(name, callback) for name in names]
        bound = []
        for name, cb in pairs:
            signal = getattr(backend, name, None)
            if not hasattr(signal, "connect") or cb is None:
                logger.warning(f"[Capability] backend 无可用信号 {name}，跳过订阅")
                continue
            signal.connect(cb)
            ctx.registry.track_subscription(ctx.plugin, signal, cb)
            bound.append(name)
        return bound

    return subscribe


def _window_active(host: Any):
    def current():
        tm = _resolve_tab_window()
        return tm.get_current_window() if tm is not None else None

    return current


def _window_list(host: Any):
    def ids():
        tm = _resolve_tab_window()
        if tm is None:
            return set()
        return {getattr(w, "_window_id", "") for w in getattr(tm, "_windows", [])}

    return ids


def _window_events(host: Any, ctx: Any):
    def subscribe(callback):
        tm = _resolve_tab_window()
        if tm is None or not hasattr(tm, "activeTabChanged"):
            raise RuntimeError("TabManagerWindow 未就绪，无法订阅窗口事件")
        tm.activeTabChanged.connect(callback)
        ctx.registry.track_subscription(ctx.plugin, tm.activeTabChanged, callback)
        return True

    return subscribe


def _ui_notify(host: Any):
    def notify(title: str, message: str) -> None:
        from qfluentwidgets import InfoBar, InfoBarPosition

        InfoBar.success(
            title,
            message,
            parent=_resolve_tab_window() or host.window(),
            duration=5000,
            position=InfoBarPosition.BOTTOM,
        )

    return notify


def _host_raw(host: Any):
    return host


def register(registry) -> None:
    registry.register("stream.subscribe", _stream_subscribe, surface="ui", owner="core:ui")
    registry.register("window.active", _window_active, surface="ui", owner="core:ui")
    registry.register("window.list", _window_list, surface="ui", owner="core:ui")
    registry.register("window.events", _window_events, surface="ui", owner="core:ui")
    registry.register("ui.notify", _ui_notify, surface="ui", owner="core:ui")
    registry.register("host.raw", _host_raw, surface="ui", owner="core:ui")
