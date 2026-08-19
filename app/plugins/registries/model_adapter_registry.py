# -*- coding: utf-8 -*-
"""模型协议适配器注册表 — 单例，resolve 按 matches 优先级取最优，空表走兜底。"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.plugins.contracts.model_adapter import ModelAdapter, ProtocolFlags


class _FallbackAdapter:
    """兜底适配器：无任何注册时的保守行为（全 False = OpenAI 标准路径）"""

    id = "__fallback__"

    def matches(self, llm_config: Dict[str, Any]) -> int:
        return 1

    def protocol_flags(self, llm_config: Dict[str, Any]) -> ProtocolFlags:
        return ProtocolFlags()


class ModelAdapterRegistry:
    """适配器注册表：register 覆盖同名；resolve 取 matches 最大者"""

    def __init__(self) -> None:
        self._adapters: Dict[str, Tuple[ModelAdapter, str]] = {}  # id -> (adapter, source)
        self._lock = threading.Lock()

    def register(self, adapter: ModelAdapter, source: str = "") -> None:
        with self._lock:
            self._adapters[adapter.id] = (adapter, source)

    def unregister_source(self, source: str) -> None:
        with self._lock:
            dead = [k for k, (_, s) in self._adapters.items() if s == source]
            for k in dead:
                del self._adapters[k]

    def resolve(self, llm_config: Dict[str, Any]) -> ModelAdapter:
        with self._lock:
            items = list(self._adapters.values())
        best: Optional[ModelAdapter] = None
        best_score = 0
        for adapter, _src in items:
            try:
                score = adapter.matches(llm_config or {})
            except Exception as e:
                logger.warning(f"[ModelAdapterRegistry] matches 异常 ({adapter.id}): {e}")
                continue
            if score > best_score:
                best, best_score = adapter, score
        return best if best is not None else _FallbackAdapter()

    def adapters(self) -> Dict[str, ModelAdapter]:
        with self._lock:
            return {k: v[0] for k, v in self._adapters.items()}

    @staticmethod
    def get_instance() -> "ModelAdapterRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = ModelAdapterRegistry()
            return _instance


_instance: Optional[ModelAdapterRegistry] = None
_instance_lock = threading.Lock()