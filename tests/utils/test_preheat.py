# -*- coding: utf-8 -*-
"""批5 回归门：进程级预热 preheat（幂等 + 预热目标 + SessionStore 冒烟）。"""

import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")


def test_preheat_idempotent_and_warms_targets(qapp, tmp_path, monkeypatch):
    """preheat_process_level：幂等（二次调用直接返回）且预热目标全部落地

    [T7 H1] tools 全量注册已移出 preheat（DeferredTaskQueue 延迟执行），
    预热目标只剩 SessionStore/StorageRegistry 两项。
    """
    import app.utils.preheat as preheat

    # 隔离：单例计数器复位（import 后首测态）
    monkeypatch.setattr(preheat, "_done", False, raising=False)

    calls = {"session_store": 0, "session_storage": 0}
    real_store = preheat._preheat_session_store
    real_storage = preheat._preheat_session_storage

    def wrap(key, real):
        def inner():
            calls[key] += 1
            return real()

        return inner

    monkeypatch.setattr(preheat, "_preheat_session_store", wrap("session_store", real_store))
    monkeypatch.setattr(preheat, "_preheat_session_storage", wrap("session_storage", real_storage))

    t0 = time.perf_counter()
    preheat.preheat_process_level()
    first_ms = (time.perf_counter() - t0) * 1000

    # 预热目标全部触达（且不含 tools 项：进程内无 _preheat_tools 属性）
    assert calls == {"session_store": 1, "session_storage": 1}
    assert not hasattr(preheat, "_preheat_tools"), "tools 注册不应在 preheat 关键路径"

    # 幂等：二次调用直接返回（内部不再触达预热项）
    preheat.preheat_process_level()
    assert calls == {"session_store": 1, "session_storage": 1}, "二次调用重复预热"

    assert first_ms < 3000, f"预热耗时 {first_ms:.0f}ms 异常（预热过重反噬风险）"


def test_preheat_session_store_roundtrip(qapp, tmp_path, monkeypatch):
    """SessionStore 冒烟：preheat 后单例可用、库文件存在、同对象幂等"""
    import app.utils.preheat as preheat
    from app.core.store.session_store import SessionStore

    monkeypatch.setattr(preheat, "_done", False, raising=False)
    preheat.preheat_process_level()

    store = SessionStore.get_instance()
    store2 = SessionStore.get_instance()
    assert store is store2
    db_path = Path(getattr(store, "_db_path", ""))
    assert db_path.exists(), f"SessionStore 库文件不存在: {db_path}"


def test_plugin_tools_load_chain_nonempty(qapp):
    """[T7 H1] 工具注册链迁移后仍可用：load_plugin_tools 能加载出非空工具集

    对应新注册点：main_widget deferred 队列 register_plugin_tools 任务（idle，
    排 create_new_session 之后）。此处用独立 registry 实例直接驱动加载链，
    与进程级单例/加载标志的测试间状态解耦（合跑防污染）。
    """
    from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
    from app.tools.registry import ToolRegistry

    fresh = ToolRegistry()
    loaded = load_plugin_tools(registry=fresh)
    total = sum(len(v) for v in loaded.values())
    assert total > 0, f"插件工具加载映射为空（注册链断裂）: {loaded}"
    assert fresh.names(), "加载后独立 registry 工具名集合为空"
