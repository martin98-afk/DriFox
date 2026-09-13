# -*- coding: utf-8 -*-
"""模型协议适配器注册表 — 单例，resolve 按 matches 优先级取最优，无匹配返回 None。

零硬编码兜底：registry 不自带 fallback adapter。无注册时 resolve 返回 None，
调用方（worker）须显式抛错或引导加载系统插件 openai。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from app.plugins.contracts.model_adapter import ModelAdapter


class ModelAdapterRegistry:
    """适配器注册表：register 覆盖同名；resolve 取 matches 最大者，无匹配返回 None"""

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

    def resolve(self, llm_config: Dict[str, Any]) -> Optional[ModelAdapter]:
        """按 matches 优先级取最优。无注册或最高分=0 → 返回 None。

        返回 None 表示无匹配（调用方需引导加载系统插件或自定义实现）。
        """
        with self._lock:
            items = list(self._adapters.values())
        if not items:
            return None
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
        return best

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