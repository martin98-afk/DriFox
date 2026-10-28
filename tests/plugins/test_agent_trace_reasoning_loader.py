# -*- coding: utf-8 -*-
"""agent_trace：轻量消息 reasoning 懒读链测试。"""
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtGui")

from plugins.agent_trace.ui.trace_collector import TraceCollector  # noqa: E402


def _bare_collector():
    """构造最小实例：QObject 必须走 ``__init__``，否则访问方法会报
    ``super-class __init__() of type TraceCollector was never called``。
    但 ``__init__`` 本身需要 PyQt5 应用对象 + 不会引入 plugin 副作用——
    # PySide6/shiboken 拒绝对非直接基类调 QObject.__init__（isn't a direct base class）
    时不依赖 qapp）；init 体里也只读 self 默认属性，不依赖环境。直接实例化
    最简洁。
    """
    return TraceCollector(None)


def test_reasoning_loader_returns_content(monkeypatch):
    storage_calls = []

    def load_msg_extras(sid, idxs):
        storage_calls.append((sid, list(idxs)))
        return {idx: {"reasoning_content": f"think-{idx}"} for idx in idxs}

    # 注入能力池：插件经 session.read 的 extras 面读（不再直连 get_session_storage）
    class _FakePool:
        def __contains__(self, cap):
            return cap == "session.read"

        def __getitem__(self, cap):
            def read():
                return {
                    "session_id": "sid-1",
                    "messages": [],
                    "system_prompt": "",
                    "extras": lambda idxs: load_msg_extras("sid-1", idxs),
                }

            return read

    c = _bare_collector()
    c._caps = _FakePool()
    loader = c._make_reasoning_loader("sid-1", 5)
    assert loader() == "think-5"
    assert storage_calls == [("sid-1", [5])]


def test_reasoning_loader_safe_on_missing_engine(monkeypatch):
    import app.core.conversation.backend as backend_mod

    monkeypatch.setattr(backend_mod, "get_session_storage", lambda: SimpleNamespace())
    c = _bare_collector()
    loader = c._make_reasoning_loader("sid-1", 5)
    assert loader() == ""


def test_reasoning_loader_safe_on_exception(monkeypatch):
    import app.core.conversation.backend as backend_mod

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(backend_mod, "get_session_storage", boom)
    c = _bare_collector()
    loader = c._make_reasoning_loader("sid-1", 5)
    assert loader() == ""