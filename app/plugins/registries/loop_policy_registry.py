# -*- coding: utf-8 -*-
"""循环策略注册表 — set_active 激活插件策略。

零硬编码兜底：active id 不在 _policies 时不再 new DefaultLoopPolicy()。
- 先尝试 _policies["default"]（由系统插件 plugins/system/loop_policies/default.py 注册）
- 仍不在则抛 RuntimeError，让调用方/启动器明确报错（引导启用 system 插件）
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

from loguru import logger

from app.plugins.contracts.loop_policy import LoopPolicy


class LoopPolicyRegistry:
    def __init__(self) -> None:
        self._policies: Dict[str, Tuple[LoopPolicy, str]] = {}
        self._active: str = "default"
        self._lock = threading.Lock()

    def register(self, policy: LoopPolicy, source: str = "") -> None:
        with self._lock:
            self._policies[policy.id] = (policy, source)

    def unregister_source(self, source: str) -> None:
        with self._lock:
            dead = [k for k, (_, s) in self._policies.items() if s == source]
            for k in dead:
                del self._policies[k]
            if self._active not in self._policies:
                self._active = "default"

    def set_active(self, policy_id: str) -> bool:
        with self._lock:
            if policy_id in self._policies:
                self._active = policy_id
                return True
            return False

    def get_active(self) -> LoopPolicy:
        with self._lock:
            item = self._policies.get(self._active) or self._policies.get("default")
        if item is None:
            raise RuntimeError(
                "未加载任何 LoopPolicy 插件（含 default），请确认 system 插件已启用"
            )
        return item[0]

    def policies(self) -> Dict[str, LoopPolicy]:
        with self._lock:
            return {k: v[0] for k, v in self._policies.items()}

    @staticmethod
    def get_instance() -> "LoopPolicyRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = LoopPolicyRegistry()
            return _instance


_instance: Optional[LoopPolicyRegistry] = None
_instance_lock = threading.Lock()