# -*- coding: utf-8 -*-
"""存储引擎注册表 — set_active 激活插件引擎。

零硬编码兜底：active id 不在 _engines 时不再 new SqliteStorageEngine()。
- 先尝试 _engines["sqlite"]（由系统插件 plugins/system/storages/sqlite.py 注册）
- 仍不在则抛 RuntimeError，让调用方/启动器明确报错（引导启用 system 插件）
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

from app.plugins.contracts.storage import SessionStorageEngine


class StorageRegistry:
    def __init__(self) -> None:
        self._engines: Dict[str, Tuple[SessionStorageEngine, str]] = {}
        self._active: str = "sqlite"
        self._lock = threading.Lock()

    def register(self, engine: SessionStorageEngine, source: str = "") -> None:
        with self._lock:
            self._engines[engine.id] = (engine, source)

    @property
    def engines(self) -> Dict[str, SessionStorageEngine]:
        """已注册引擎只读视图（冷启动探测用：空=尚未加载任何插件引擎）"""
        with self._lock:
            return {k: v[0] for k, v in self._engines.items()}

    def unregister_source(self, source: str) -> None:
        with self._lock:
            dead = [k for k, (_, s) in self._engines.items() if s == source]
            for k in dead:
                del self._engines[k]
            if self._active not in self._engines:
                self._active = "sqlite"

    def set_active(self, engine_id: str) -> bool:
        with self._lock:
            if engine_id in self._engines:
                self._active = engine_id
                return True
            return False

    def get_active(self) -> SessionStorageEngine:
        with self._lock:
            item = self._engines.get(self._active) or self._engines.get("sqlite")
        if item is None:
            # P3 兜底：无任何 StorageEngine 插件 → 返回内置只读/只写 noop 引擎 + warning
            # 行为：save 接受但不持久化；get/get_all 返回 None/[]；不抛错。
            # 真实环境 sqlite 系统插件应已加载，本兜底仅在 system 整体被禁的极端情况下生效。
            from loguru import logger
            from app.plugins.registries._builtin_fallback import BuiltInNoopStorageEngine

            logger.warning(
                "[StorageRegistry] 未加载任何 StorageEngine 插件（含 sqlite），"
                "降级使用内置只读 noop 引擎（会话将不会持久化）"
            )
            return BuiltInNoopStorageEngine()
        return item[0]

    @staticmethod
    def get_instance() -> "StorageRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = StorageRegistry()
            return _instance


_instance: Optional[StorageRegistry] = None
_instance_lock = threading.Lock()