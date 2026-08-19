# -*- coding: utf-8 -*-
"""SessionStorageEngine：SQLite 内置实现接口形态 + 注册表回落"""

import pytest


def test_sqlite_engine_interface(tmp_path, monkeypatch):
    """SqliteStorageEngine 暴露契约全部方法（空库基本读写）"""
    from app.plugins.builtin_runtime import SqliteStorageEngine
    from app.core.store.session_store import SessionStore

    # 隔离全局单例：SessionStore 是 _instance 单例（非 _instances dict）；
    # 测试前清空类级 _instance + 清掉 _initialized 哨兵，确保 db_dir 生效。
    monkeypatch.setattr(SessionStore, "_instance", None, raising=False)
    engine = SqliteStorageEngine(db_dir=str(tmp_path))
    assert engine.id == "sqlite"
    assert engine.save({"session_id": "s1", "name": "t", "messages": []}) is True
    got = engine.get("s1")
    assert got is not None and got["session_id"] == "s1"
    assert isinstance(engine.get_all(limit=10), list)
    assert engine.delete("s1") is True
    assert engine.get("s1") is None


def test_registry_fallback_default(monkeypatch):
    """未注册任何插件引擎 → get_active 回落 sqlite"""
    from app.plugins.registries.storage_registry import StorageRegistry

    reg = StorageRegistry()
    monkeypatch.setattr(StorageRegistry, "get_instance", staticmethod(lambda: reg))
    assert reg.get_active().id == "sqlite"


def test_plugin_engine_override(monkeypatch):
    from app.plugins.registries.storage_registry import StorageRegistry

    class _MemEngine:
        id = "memory"

        def save(self, session):
            return True

        def get(self, session_id):
            return None

        def get_all(self, limit=100, offset=0):
            return []

        def get_by_project(self, project, limit=100):
            return []

        def get_projects(self):
            return []

        def delete(self, session_id):
            return True

    reg = StorageRegistry()
    monkeypatch.setattr(StorageRegistry, "get_instance", staticmethod(lambda: reg))
    reg.register(_MemEngine(), source="plugin:demo")
    reg.set_active("memory")
    assert reg.get_active().id == "memory"
    reg.unregister_source("plugin:demo")
    assert reg.get_active().id == "sqlite"
