# -*- coding: utf-8 -*-
"""SQLite 存储引擎 — 系统插件实现（id="sqlite"）。

薄包装现有 SessionRepository（行为零变化）。
"""

from __future__ import annotations


class SqliteStorageEngine:
    """SQLite 存储引擎 — 薄包装现有 SessionRepository（行为零变化）"""

    id = "sqlite"

    def __init__(self, db_dir: str = None):
        from app.core.store.session_store import SessionStore

        store = SessionStore(db_dir=db_dir) if db_dir is not None else SessionStore.get_instance()
        # 复用 SessionStore 内部已初始化的 SessionRepository，避免重复构造连接池；
        # SessionRepository 期望的是 DatabaseManager，而非 SessionStore 本身。
        self._repo = store.session_repo

    def save(self, session: dict) -> bool:
        return self._repo.save(session)

    def get(self, session_id: str):
        return self._repo.get(session_id)

    def get_all(self, limit: int = 100, offset: int = 0):
        return self._repo.get_all(limit=limit, offset=offset)

    def get_by_project(self, project: str, limit: int = 100):
        return self._repo.get_by_project(project, limit=limit)

    def get_projects(self):
        return self._repo.get_projects()

    def delete(self, session_id: str) -> bool:
        return self._repo.delete(session_id)


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。

    source 由 loader 的 _RegistryProxy 强制为 "plugin:system"，
    本函数不显式传入，保持与 tools/providers 插件约定一致。
    """
    registry.register(SqliteStorageEngine())