# -*- coding: utf-8 -*-
"""SessionStorageEngine：SQLite 内置实现接口形态 + 注册表回落"""

import pytest


def test_sqlite_engine_interface(tmp_path, monkeypatch):
    """SqliteStorageEngine 暴露契约全部方法（空库基本读写）"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
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
    """注册 sqlite 插件引擎（系统插件路径）→ get_active 回落 sqlite"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
    from app.plugins.registries.storage_registry import StorageRegistry

    reg = StorageRegistry()
    monkeypatch.setattr(StorageRegistry, "get_instance", staticmethod(lambda: reg))
    reg.register(SqliteStorageEngine(db_dir=":memory:"), source="plugin:system")
    assert reg.get_active().id == "sqlite"


def test_plugin_engine_override(monkeypatch):
    from plugins.system.storages.sqlite import SqliteStorageEngine
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
    reg.register(SqliteStorageEngine(db_dir=":memory:"), source="plugin:system")
    reg.register(_MemEngine(), source="plugin:demo")
    reg.set_active("memory")
    assert reg.get_active().id == "memory"
    reg.unregister_source("plugin:demo")
    assert reg.get_active().id == "sqlite"


# ---------- Phase B：可选能力接口（isinstance 探测 + 安全降级） ----------


def test_sqlite_engine_implements_capabilities(tmp_path, monkeypatch):
    """SQLite 引擎声明全部可选能力（标题/计数/输入历史）"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
    from app.core.store.session_store import SessionStore
    from app.plugins.contracts.storage import (
        InputHistoryCapability,
        SessionCountsCapability,
        SessionTitleCapability,
    )

    monkeypatch.setattr(SessionStore, "_instance", None, raising=False)
    engine = SqliteStorageEngine(db_dir=str(tmp_path))
    assert isinstance(engine, SessionTitleCapability)
    assert isinstance(engine, SessionCountsCapability)
    assert isinstance(engine, InputHistoryCapability)


def test_sqlite_capability_methods_delegate(tmp_path, monkeypatch):
    """能力方法实际委托 SessionStore 同名方法（标题更新 / 计数 / 输入历史读写）"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
    from app.core.store.session_store import SessionStore

    monkeypatch.setattr(SessionStore, "_instance", None, raising=False)
    engine = SqliteStorageEngine(db_dir=str(tmp_path))
    engine.save({"session_id": "s1", "name": "old", "messages": []})
    assert engine.update_session_title("s1", "新标题") is True
    assert engine.get("s1")["name"] == "新标题"
    assert engine.get_session_counts() == {"默认项目": 1}
    assert engine.add_input_history("hello", []) is True
    history = engine.get_input_history(limit=10)
    assert history and history[0]["text"] == "hello"


def test_engine_without_capability_safe_degrades(monkeypatch):
    """无能力引擎：isinstance 探测为 False，消费方走降级分支（不炸）"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
    from app.plugins.contracts.storage import SessionTitleCapability

    class _PlainEngine:
        id = "plain"

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

    assert isinstance(_PlainEngine(), SessionTitleCapability) is False
    # 消费方探测模式：有能力才调用，无能力走默认值
    engine = _PlainEngine()
    title_updated = engine.update_session_title("s1", "x") if isinstance(engine, SessionTitleCapability) else False
    assert title_updated is False
    # 对照：sqlite 引擎探测为 True
    from app.core.store.session_store import SessionStore

    monkeypatch.setattr(SessionStore, "_instance", None, raising=False)
    sqlite_engine = SqliteStorageEngine(db_dir=":memory:")
    assert isinstance(sqlite_engine, SessionTitleCapability) is True
