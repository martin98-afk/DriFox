# -*- coding: utf-8 -*-
"""协议传输器注册表 — 单例，按 id 解析，无该 id 回退默认 "openai_chat"。

零硬编码兜底：registry 不自带 fallback transport（系统插件
plugins/system-transports/transports/openai_chat.py 提供默认实现）。resolve 回退逻辑仅做
id 回退（"openai_chat" 是约定默认 id），不 new 任何实例；全无时返回 None 由调用方
决定降级行为（协议通道不可用属配置错误，不可静默透传）。

对齐 serializer_registry.py 结构：单例 + register/resolve + unregister_source。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

_DEFAULT_ID = "openai_chat"


class TransportRegistry:
    """协议传输器注册表：register 覆盖同名；resolve 按 id 取，无该 id 回退 openai_chat"""

    def __init__(self) -> None:
        self._transports: Dict[str, Tuple[Any, str]] = {}  # id -> (transport, source)
        self._lock = threading.Lock()

    def register(self, transport: Any, source: str = "") -> None:
        with self._lock:
            self._transports[transport.id] = (transport, source)

    def unregister_source(self, source: str) -> None:
        with self._lock:
            dead = [k for k, (_, s) in self._transports.items() if s == source]
            for k in dead:
                del self._transports[k]

    def resolve(self, transport_id: Optional[str] = None) -> Any:
        """按 id 取传输器；无该 id 回退 "openai_chat"；仍无返回 None（调用方报错）。

        transport_id=None 等价请求默认 id（"openai_chat"）。
        """
        requested = transport_id or _DEFAULT_ID
        with self._lock:
            direct = self._transports.get(requested)
            item = direct or self._transports.get(_DEFAULT_ID)
        if direct is None and item is not None:
            from loguru import logger

            logger.warning(
                f"[TransportRegistry] transport_id={requested!r} 未注册，回退默认 'openai_chat'——"
                f"若该配置指向非 OpenAI 协议端点，请求将失败，请确认对应传输器插件已启用"
            )
        if item is None:
            from loguru import logger

            logger.warning(
                f"[TransportRegistry] 未注册任何 transport 插件（含系统插件 openai_chat），"
                f"协议通道不可用 id={requested!r}"
            )
            return None
        return item[0]

    def transports(self) -> Dict[str, Any]:
        with self._lock:
            return {k: v[0] for k, v in self._transports.items()}

    @staticmethod
    def get_instance() -> "TransportRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = TransportRegistry()
            return _instance


_instance: Optional[TransportRegistry] = None
_instance_lock = threading.Lock()
