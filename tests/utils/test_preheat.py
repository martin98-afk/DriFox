# -*- coding: utf-8 -*-
"""批5 回归门：进程级预热 preheat（幂等 + 预热目标 + SessionStore 冒烟）。"""

import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")


def test_preheat_idempotent_and_warms_targets(qapp, tmp_path, monkeypatch):
    """preheat_process_level：幂等（二次调用直接返回）且预热三项全部落地"""
    import app.utils.preheat as preheat

    # 隔离：单例计数器复位（import 后首测态）
    monkeypatch.setattr(preheat, "_done", False, raising=False)

    calls = {"session_store": 0, "session_storage": 0, "get_all_tools": 0}
    real_store = preheat._preheat_session_store
    real_storage = preheat._preheat_session_storage
    real_tools = preheat._preheat_tools

    def wrap(key, real):
        def inner():
            calls[key] += 1
            return real()

        return inner

    monkeypatch.setattr(preheat, "_preheat_session_store", wrap("session_store", real_store))
    monkeypatch.setattr(preheat, "_preheat_session_storage", wrap("session_storage", real_storage))
    monkeypatch.setattr(preheat, "_preheat_tools", wrap("get_all_tools", real_tools))

    t0 = time.perf_counter()
    preheat.preheat_process_level()
    first_ms = (time.perf_counter() - t0) * 1000

    # 预热目标全部触达
    assert calls == {"session_store": 1, "session_storage": 1, "get_all_tools": 1}

    # 幂等：二次调用直接返回（内部不再触达预热项）
    preheat.preheat_process_level()
    assert calls == {"session_store": 1, "session_storage": 1, "get_all_tools": 1}, "二次调用重复预热"

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


def test_preheat_tools_nonempty(qapp, monkeypatch):
    """get_all_tools 预热后返回非空工具清单（内置工具链已导入）"""
    import app.utils.preheat as preheat

    monkeypatch.setattr(preheat, "_done", False, raising=False)
    preheat.preheat_process_level()
    tools = preheat._preheat_tools()
    assert isinstance(tools, (list, tuple)) and len(tools) > 0
