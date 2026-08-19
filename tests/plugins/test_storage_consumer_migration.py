# -*- coding: utf-8 -*-
"""存储消费方迁移测试：backend 门面 get_session_storage() + 引擎覆盖消费方方法。

不变量：
- 门面在注册表空时幂等加载系统插件 → 返回 sqlite 引擎
- 已注册自定义引擎（set_active）→ 返回自定义引擎
- 引擎与 SessionStore 共享同一底层单例（db 路径/连接不分叉）
- 消费方（history_manager/memory_manager/session_handler）经门面获取引擎后行为等价
"""

import pytest


class _MemEngine:
    id = "memory"

    def __init__(self):
        self.saved = []

    def save(self, session):
        self.saved.append(session)
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

    def save_session(self, session):
        return self.save(session)

    def get_session(self, session_id):
        return self.get(session_id)

    def is_initialized(self):
        return True


@pytest.fixture()
def fresh_storage_registry(monkeypatch):
    """每用例独立 StorageRegistry（绕过单例状态污染）"""
    from app.plugins.registries.storage_registry import StorageRegistry

    reg = StorageRegistry()
    monkeypatch.setattr(StorageRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


@pytest.fixture()
def fresh_serializer_registry(monkeypatch):
    """隔离 SerializerRegistry（门面 cold start 不依赖）"""
    from app.plugins.registries.serializer_registry import SerializerRegistry

    reg = SerializerRegistry()
    monkeypatch.setattr(SerializerRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


def test_facade_returns_sqlite_when_empty(fresh_storage_registry, fresh_serializer_registry):
    """注册表空 → 门面触发幂等加载系统插件 → 返回 sqlite 引擎"""
    from app.core.backend import get_session_storage

    engine = get_session_storage()
    assert engine.id == "sqlite"


def test_facade_returns_active_plugin_engine(fresh_storage_registry, fresh_serializer_registry):
    """已注册自定义引擎并 set_active → 门面返回自定义引擎"""
    from app.core.backend import get_session_storage
    from plugins.system.storages.sqlite import SqliteStorageEngine

    fresh_storage_registry.register(SqliteStorageEngine(db_dir=":memory:"), source="plugin:system")
    mem = _MemEngine()
    fresh_storage_registry.register(mem, source="plugin:demo")
    fresh_storage_registry.set_active("memory")
    assert get_session_storage() is mem


def test_engine_shares_session_store_singleton(tmp_path, monkeypatch):
    """引擎与 SessionStore 共享同一底层单例（db 路径/连接不分叉）"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
    from app.core.store.session_store import SessionStore

    monkeypatch.setattr(SessionStore, "_instance", None, raising=False)
    store = SessionStore.get_instance(str(tmp_path))
    engine = SqliteStorageEngine(db_dir=str(tmp_path))
    assert engine.store is store
    # 消费方经引擎写、经 store 读（同一连接/文件）
    engine.save_session({"session_id": "s1", "name": "t", "messages": []})
    assert store.get_session("s1")["session_id"] == "s1"


def test_engine_covers_consumer_methods(tmp_path, monkeypatch):
    """引擎覆盖三个消费方的全部调用方法（委托 SessionStore，行为等价）"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
    from app.core.store.session_store import SessionStore

    monkeypatch.setattr(SessionStore, "_instance", None, raising=False)
    engine = SqliteStorageEngine(db_dir=str(tmp_path))
    assert engine.is_initialized is True
    assert engine.save_session({"session_id": "s1", "name": "t", "messages": []}) is True
    assert engine.get_session("s1")["session_id"] == "s1"
    assert len(engine.get_sessions(limit=10)) == 1
    assert len(engine.get_sessions_lightweight(limit=10)) == 1
    # get_session_count 与底层 SessionStore 逐点等价（既有解析行为，委托不变）
    assert engine.get_session_count() == engine.store.get_session_count()
    assert engine.get_session_counts() == {"默认项目": 1}
    assert engine.get_projects() == ["默认项目"]
    assert engine.update_session_project("s1", "proj") is True
    assert engine.delete_session("s1") is True


def test_consumer_migration_uses_facade(monkeypatch):
    """history_manager / memory_manager / session_handler 经门面获取引擎（不再直接 SessionStore）"""
    import inspect
    import app.utils.history_manager as hm_mod
    import app.core.memory_manager as mm_mod
    import app.gateway.local_service.session_handler as sh_mod

    for mod in (hm_mod, mm_mod, sh_mod):
        src = inspect.getsource(mod)
        assert "get_session_storage" in src or "from app.core.backend import get_session_storage" in src, (
            f"{mod.__name__} 必须经 backend 门面获取存储引擎"
        )


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
