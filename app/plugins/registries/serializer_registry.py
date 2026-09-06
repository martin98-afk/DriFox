# -*- coding: utf-8 -*-
"""消息序列化器注册表 — 单例，按 serializer_id 解析，无该 id 回退 "openai"，仍无抛错。

零硬编码兜底：registry 不自带 fallback serializer（系统插件
plugins/system/serializers/openai.py 提供默认实现）。resolve 回退逻辑仅做
id 回退（"openai" 是约定默认 id），不 new 任何实例。
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

from app.plugins.contracts.message_serializer import MessageSerializer

_DEFAULT_ID = "openai"


class SerializerRegistry:
    """序列化器注册表：register 覆盖同名；resolve 按 id 取，无该 id 回退 openai"""

    def __init__(self) -> None:
        self._serializers: Dict[str, Tuple[MessageSerializer, str]] = {}  # id -> (serializer, source)
        self._lock = threading.Lock()

    def register(self, serializer: MessageSerializer, source: str = "") -> None:
        with self._lock:
            self._serializers[serializer.id] = (serializer, source)

    def unregister_source(self, source: str) -> None:
        with self._lock:
            dead = [k for k, (_, s) in self._serializers.items() if s == source]
            for k in dead:
                del self._serializers[k]

    def resolve(self, serializer_id: Optional[str] = None) -> MessageSerializer:
        """按 id 取序列化器；无该 id 回退 "openai"；仍无抛错（调用方引导加载系统插件）。

        serializer_id=None 等价请求默认 id（"openai"），与 Phase B 薄壳解析一致。
        """
        requested = serializer_id or _DEFAULT_ID
        with self._lock:
            item = self._serializers.get(requested) or self._serializers.get(_DEFAULT_ID)
        if item is None:
            # P3 兜底：无任何 MessageSerializer 插件 → 返回内置 passthrough + warning
            # 行为：serialize 直接透传 messages（不做协议特判）；多模态等高级特性会丢失，
            # 但主链路不抛错，发送链不至于炸。
            from loguru import logger
            from app.plugins.registries._builtin_fallback import BuiltInPassthroughSerializer

            logger.warning(
                "[SerializerRegistry] 未注册任何 MessageSerializer 插件（含系统插件 openai），"
                "降级使用内置 passthrough（不做协议特判，特性可能丢失）"
            )
            return BuiltInPassthroughSerializer()
        return item[0]

    def serializers(self) -> Dict[str, MessageSerializer]:
        with self._lock:
            return {k: v[0] for k, v in self._serializers.items()}

    @staticmethod
    def get_instance() -> "SerializerRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = SerializerRegistry()
            return _instance


_instance: Optional[SerializerRegistry] = None
_instance_lock = threading.Lock()
