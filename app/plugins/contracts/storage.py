# -*- coding: utf-8 -*-
"""会话存储引擎契约 — 会话持久化后端可插拔（Phase A 只交付接口与 SQLite 默认实现，
消费方迁移在 Phase B）。方法签名与 SessionRepository 对齐。"""
from __future__ import annotations

from typing import Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class SessionStorageEngine(Protocol):
    """会话存储引擎接口"""

    id: str

    def save(self, session: Dict) -> bool: ...

    def get(self, session_id: str) -> Optional[Dict]: ...

    def get_all(self, limit: int = 100, offset: int = 0) -> List[Dict]: ...

    def get_by_project(self, project: str, limit: int = 100) -> List[Dict]: ...

    def get_projects(self) -> List[str]: ...

    def delete(self, session_id: str) -> bool: ...