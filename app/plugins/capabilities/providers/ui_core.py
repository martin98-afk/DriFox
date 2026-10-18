# -*- coding: utf-8 -*-
"""UI surface 的能力 provider（host = MainWidget）。"""

from __future__ import annotations

from typing import Any

from loguru import logger


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


def register(registry) -> None:
    registry.register("stream.subscribe", _stream_subscribe, surface="ui", owner="core:ui")
