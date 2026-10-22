# -*- coding: utf-8 -*-
"""stream.subscribe：订阅 backend 信号 / 返回已绑定名单 / release_plugin 断开。"""
import pytest
from PySide6.QtCore import QObject, Signal


class _Backend(QObject):
    stream_started = Signal()
    stream_finished = Signal(str)
    tool_call_started = Signal(str, str, dict)


class _Host:
    def __init__(self, backend):
        self.backend = backend


@pytest.fixture(autouse=True)
def _fresh():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _pool(backend, plugin="agent_trace"):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("ui")
    return CapabilityRegistry.get_instance().build_pool(
        surface="ui", host=_Host(backend), plugin=plugin, declared={"stream.subscribe"}
    )


def test_subscribe_receives_and_lists_bound(qapp):
    be = _Backend()
    pool = _pool(be)
    got = []
    subscribe = pool["stream.subscribe"]
    bound = subscribe(["stream_started", "stream_finished"], lambda *a: got.append(a))
    assert set(bound) == {"stream_started", "stream_finished"}
    be.stream_started.emit()
    be.stream_finished.emit("done")
    assert got == [(), ("done",)]


def test_subscribe_mapping_form(qapp):
    """映射形态：{事件名: 回调}，同名信号各自回调（解决同签名信号无法按参数分派）。"""
    be = _Backend()
    pool = _pool(be)
    got = []
    bound = pool["stream.subscribe"](
        {"stream_started": lambda: got.append("s"), "stream_finished": lambda v: got.append(v)}
    )
    assert set(bound) == {"stream_started", "stream_finished"}
    be.stream_started.emit()
    be.stream_finished.emit("f")
    assert got == ["s", "f"]


def test_unknown_signal_skipped(qapp, log_capture):
    be = _Backend()
    pool = _pool(be)
    bound = pool["stream.subscribe"](["not_a_signal"], lambda *a: None)
    assert bound == []
    assert any("not_a_signal" in r for r in log_capture)


def test_release_plugin_disconnects(qapp):
    from app.plugins.capabilities.registry import CapabilityRegistry

    be = _Backend()
    pool = _pool(be, plugin="agent_trace")
    got = []
    pool["stream.subscribe"]("stream_started", lambda *a: got.append(1))
    be.stream_started.emit()
    assert got == [1]
    CapabilityRegistry.get_instance().release_plugin("agent_trace")
    be.stream_started.emit()
    assert got == [1]  # 已断开


def test_no_backend_raises(qapp):
    pool = _pool(None)
    with pytest.raises(RuntimeError):
        pool["stream.subscribe"]("stream_started", lambda *a: None)
