# -*- coding: utf-8 -*-
"""agent_trace：轻量消息 reasoning 懒读链测试。"""
from types import SimpleNamespace

from plugins.agent_trace.ui.trace_collector import TraceCollector


def _bare_collector():
    """构造最小实例：QObject 必须走 ``__init__``，否则访问方法会报
    ``super-class __init__() of type TraceCollector was never called``。
    但 ``__init__`` 本身需要 PyQt5 应用对象 + 不会引入 plugin 副作用——
    实际上只要 ``QObject.__init__(self, None)`` 走完即可（PyQt5 父级为 None
    时不依赖 qapp）；init 体里也只读 self 默认属性，不依赖环境。直接实例化
    最简洁。
    """
    return TraceCollector(None)


def test_reasoning_loader_returns_content(monkeypatch):
    storage_calls = []

    def load_msg_extras(sid, idxs):
        storage_calls.append((sid, list(idxs)))
        return {idx: {"reasoning_content": f"think-{idx}"} for idx in idxs}

    import app.core.backend as backend_mod

    monkeypatch.setattr(backend_mod, "get_session_storage", lambda: SimpleNamespace(load_msg_extras=load_msg_extras))
    c = _bare_collector()
    loader = c._make_reasoning_loader("sid-1", 5)
    assert loader() == "think-5"
    assert storage_calls == [("sid-1", [5])]


def test_reasoning_loader_safe_on_missing_engine(monkeypatch):
    import app.core.backend as backend_mod

    monkeypatch.setattr(backend_mod, "get_session_storage", lambda: SimpleNamespace())
    c = _bare_collector()
    loader = c._make_reasoning_loader("sid-1", 5)
    assert loader() == ""


def test_reasoning_loader_safe_on_exception(monkeypatch):
    import app.core.backend as backend_mod

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(backend_mod, "get_session_storage", boom)
    c = _bare_collector()
    loader = c._make_reasoning_loader("sid-1", 5)
    assert loader() == ""