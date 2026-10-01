# -*- coding: utf-8 -*-
"""流式接收器注册表 — 单例，按 id 解析，无该 id 回退默认 "openai_chat"。

接收器（StreamEventSink）消费归一化 StreamEvent 驱动 worker 状态机与 Qt 信号；
默认实现为系统插件 plugins/system-stream-sinks/stream_sinks/openai_chat.py。

对齐 transport_registry.py / serializer_registry.py 结构。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

_DEFAULT_ID = "openai_chat"


class StreamSinkRegistry:
    """流式接收器注册表：register 覆盖同名；resolve 按 id 取，无该 id 回退 openai_chat"""

    def __init__(self) -> None:
        self._sinks: Dict[str, Tuple[Any, str]] = {}  # id -> (sink, source)
        self._lock = threading.Lock()

    def register(self, sink: Any, source: str = "") -> None:
        with self._lock:
            self._sinks[sink.id] = (sink, source)

    def unregister_source(self, source: str) -> None:
        with self._lock:
            dead = [k for k, (_, s) in self._sinks.items() if s == source]
            for k in dead:
                del self._sinks[k]

    def resolve(self, sink_id: Optional[str] = None) -> Any:
        """按 id 取接收器；无该 id 回退 "openai_chat"；仍无返回 None（调用方报错）。"""
        requested = sink_id or _DEFAULT_ID
        with self._lock:
            direct = self._sinks.get(requested)
            item = direct or self._sinks.get(_DEFAULT_ID)
        if direct is None and item is not None:
            from loguru import logger

            logger.warning(
                f"[StreamSinkRegistry] sink_id={requested!r} 未注册，回退默认 'openai_chat'——"
                f"若对应协议解析需求不同，响应可能解析错误"
            )
        if item is None:
            from loguru import logger

            logger.warning(
                f"[StreamSinkRegistry] 未注册任何 stream_sink 插件（含系统插件），流式响应无法解析 id={requested!r}"
            )
            return None
        return item[0]

    def sinks(self) -> Dict[str, Any]:
        with self._lock:
            return {k: v[0] for k, v in self._sinks.items()}

    @staticmethod
    def get_instance() -> "StreamSinkRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = StreamSinkRegistry()
            return _instance


_instance: Optional[StreamSinkRegistry] = None
_instance_lock = threading.Lock()
