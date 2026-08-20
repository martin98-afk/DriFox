# -*- coding: utf-8 -*-
"""Gateway 平台注册表 — platform_id → GatewayPlatformDef（进程级单例）。

register 接 source kwarg（对齐 SerializerRegistry / runtime_component_loader
的 _RegistryProxy 调用形态）。GatewayPlatformDef 为 frozen dataclass，因此
registry 内部若 source 非空，用 dataclasses.replace 生成副本再存，避免对
frozen 实例外部写属性。
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Dict, List, Optional, Tuple

from app.plugins.contracts.gateway_platform import GatewayPlatformDef


class GatewayPlatformRegistry:
    """进程级单例：按 platform_id 存 GatewayPlatformDef，list 按 ui_order 升序"""

    def __init__(self) -> None:
        self._defs: Dict[str, Tuple[GatewayPlatformDef, str]] = {}
        self._lock = threading.Lock()

    def register(self, platform_def: GatewayPlatformDef, source: str = "") -> None:
        with self._lock:
            if source:
                platform_def = dataclasses.replace(platform_def, source=source)
            self._defs[platform_def.platform_id] = (platform_def, platform_def.source)

    def get(self, platform_id: str) -> Optional[GatewayPlatformDef]:
        with self._lock:
            item = self._defs.get(platform_id)
            return item[0] if item is not None else None

    def list_platforms(self) -> List[GatewayPlatformDef]:
        with self._lock:
            return sorted(
                (item[0] for item in self._defs.values()),
                key=lambda d: d.ui_order,
            )

    def unregister_source(self, source: str) -> List[str]:
        with self._lock:
            removed = [
                pid for pid, (_, src) in self._defs.items() if src == source
            ]
            for pid in removed:
                self._defs.pop(pid, None)
            return removed

    @staticmethod
    def get_instance() -> "GatewayPlatformRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = GatewayPlatformRegistry()
            return _instance


_instance: Optional[GatewayPlatformRegistry] = None
_instance_lock = threading.Lock()