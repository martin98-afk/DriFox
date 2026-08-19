# -*- coding: utf-8 -*-
"""UI 层存储消费点迁移测试：backend.session_store → StorageRegistry 活跃引擎。

不变量：
- 引擎方法集覆盖 main_widget / FileOperationRecorder 全部 session_store.* 调用点
- backend.session_store 返回活跃引擎（注册自定义引擎后返回自定义；空表冷启动回 sqlite）
- 方法调用与 SessionStore 逐点等价（委托内部单例，db 不分叉）
"""

import pytest


class _MemEngine:
    id = "memory"

    def __init__(self):
        self.titles = {}

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

    def update_session_title(self, session_id, title):
        self.titles[session_id] = title
        return True

    def clear_old_subagent_tasks(self, days=7):
        return 0

    def force_cleanup_project(self, project_name):
        return True

    def get_session_counts(self):
        return {}

    def get_input_history(self, limit=50):
        return []

    def add_input_history(self, content, attachments=None):
        return True


@pytest.fixture()
def fresh_storage_registry(monkeypatch):
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


# ---------- UI 调用点方法集 ----------


def test_engine_covers_ui_call_sites(tmp_path, monkeypatch):
    """引擎方法集 == UI 层（main_widget + FileOperationRecorder）实际调用点"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
    from app.core.store.session_store import SessionStore

    monkeypatch.setattr(SessionStore, "_instance", None, raising=False)
    engine = SqliteStorageEngine(db_dir=str(tmp_path))

    # main_widget 调用点（:2503/:12437/:15151/:15160/:18219/:18606）
    assert hasattr(engine, "clear_old_subagent_tasks")
    assert hasattr(engine, "update_session_title")
    assert hasattr(engine, "get_input_history")
    assert hasattr(engine, "add_input_history")
    assert hasattr(engine, "get_session_counts")
    assert hasattr(engine, "force_cleanup_project")

    # FileOperationRecorder 调用点（构造参数 + 5 方法）
    assert hasattr(engine, "record_file_operation")
    assert hasattr(engine, "get_file_operations_by_call_id")
    assert hasattr(engine, "remove_file_operation")
    assert hasattr(engine, "get_all_file_operations")
    assert hasattr(engine, "clear_session_file_operations")


def test_engine_methods_delegate_to_session_store(tmp_path, monkeypatch):
    """引擎方法委托 SessionStore（标题/计数/输入历史/子任务清理/文件操作等价）"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
    from app.core.store.session_store import SessionStore

    monkeypatch.setattr(SessionStore, "_instance", None, raising=False)
    engine = SqliteStorageEngine(db_dir=str(tmp_path))

    engine.save_session({"session_id": "s1", "name": "t", "messages": []})
    assert engine.update_session_title("s1", "新标题") is True
    assert engine.get_session_counts() == {"默认项目": 1}
    assert engine.add_input_history("hi", []) is True
    assert engine.get_input_history(limit=10)[0]["text"] == "hi"
    assert engine.clear_old_subagent_tasks(14) == 0  # 空库 → 0 条清理
    assert engine.force_cleanup_project("不存在项目") is True
    assert engine.record_file_operation("s1", "c1", "write", "a.txt", "b.bak") is True
    ops = engine.get_file_operations_by_call_id("s1", "c1")
    assert ops and ops[0]["call_id"] == "c1"
    assert engine.remove_file_operation("s1", "c1") >= 1
    assert engine.get_all_file_operations("s1") == []
    assert engine.clear_session_file_operations("s1") == (0, [])


def test_file_operation_recorder_accepts_engine(tmp_path, monkeypatch):
    """FileOperationRecorder(self.session_store) 构造兼容引擎（隐式依赖）"""
    from plugins.system.storages.sqlite import SqliteStorageEngine
    from app.core.store.session_store import SessionStore
    from app.utils.file_operation_recorder import FileOperationRecorder

    monkeypatch.setattr(SessionStore, "_instance", None, raising=False)
    engine = SqliteStorageEngine(db_dir=str(tmp_path))
    recorder = FileOperationRecorder(engine)
    assert recorder._session_store is engine


# ---------- backend.session_store 门面 ----------


def test_backend_session_store_returns_active_engine(fresh_storage_registry, fresh_serializer_registry):
    """backend.session_store 返回 StorageRegistry 活跃引擎（自定义引擎生效）"""
    from app.core.backend import ChatBackend
    from plugins.system.storages.sqlite import SqliteStorageEngine

    backend = ChatBackend.__new__(ChatBackend)
    fresh_storage_registry.register(SqliteStorageEngine(db_dir=":memory:"), source="plugin:system")
    mem = _MemEngine()
    fresh_storage_registry.register(mem, source="plugin:demo")
    fresh_storage_registry.set_active("memory")
    assert backend.session_store is mem


def test_backend_session_store_cold_start_sqlite(fresh_storage_registry, fresh_serializer_registry):
    """backend.session_store 空表冷启动 → 幂等加载系统插件 → sqlite 引擎"""
    from app.core.backend import ChatBackend

    backend = ChatBackend.__new__(ChatBackend)
    engine = backend.session_store
    assert engine.id == "sqlite"


def test_ui_attribute_assignment_shape(monkeypatch):
    """main_widget 侧 self.session_store = self.backend.session_store 用法不变"""
    import inspect
    from app.core import backend as backend_mod

    assert "session_store" in inspect.getsource(backend_mod.ChatBackend)
    # property 保留（main_widget:1185 属性读取，非方法调用）
    assert isinstance(inspect.getattr_static(backend_mod.ChatBackend, "session_store"), property)


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
