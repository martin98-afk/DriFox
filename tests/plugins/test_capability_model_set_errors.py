# -*- coding: utf-8 -*-
"""model.set / stream.errors：设置窗口模型 / 订阅结构化报错事件。"""
import pytest
from PyQt5.QtCore import QObject, pyqtSignal


class _Backend(QObject):
    conversation_error = pyqtSignal(dict)


class _Host:
    """模拟 MainWidget 的最小面：解析 stub + 赋值字段 + 刷新计数。"""

    def __init__(self, backend=None, target=None, window_id=""):
        self.backend = backend
        self._target = target
        self._window_id = window_id
        self._current_provider_name = "cfg-old"
        self._current_model_name = "old-model"
        self._user_manually_selected_model = False
        self.btn_refreshed = 0
        self.ctx_refreshed = 0

    def _resolve_history_model_target(self, model_name, provider_hint="", config_id=""):
        return self._target

    def _update_model_selector_btn(self):
        self.btn_refreshed += 1

    def _refresh_context_usage_indicator(self):
        self.ctx_refreshed += 1


class _HostNoResolver(_Host):
    def _resolve_history_model_target(self, *a, **k):
        raise AttributeError


@pytest.fixture(autouse=True)
def _fresh():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _pool(host, plugin="auto-fallback", declared=None):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("ui")
    if declared is None:
        declared = {"model.set", "stream.errors"}
    return CapabilityRegistry.get_instance().build_pool(
        surface="ui", host=host, plugin=plugin, declared=declared
    )


def test_model_set_success(qapp):
    host = _Host(target=("cfg-new", "new-model"))
    result = _pool(host)["model.set"](model="new-model", provider="New Provider")
    assert result == {"ok": True, "config_id": "cfg-new", "model": "new-model"}
    assert host._current_provider_name == "cfg-new"
    assert host._current_model_name == "new-model"
    assert host.btn_refreshed == 1
    assert host.ctx_refreshed == 1
    # 不视为用户手动选择（云端同步仍可覆盖）
    assert host._user_manually_selected_model is False


def test_model_set_not_found_keeps_window_state(qapp):
    host = _Host(target=None)
    result = _pool(host)["model.set"](model="ghost")
    assert result["ok"] is False
    assert result["error"] == "not_found"
    assert "detail" in result
    assert host._current_provider_name == "cfg-old"
    assert host._current_model_name == "old-model"
    assert host.btn_refreshed == 0


def test_model_set_missing_resolver(qapp):
    host = _HostNoResolver()
    result = _pool(host)["model.set"](model="x")
    assert result["ok"] is False
    assert result["error"] == "not_found"


def test_stream_errors_subscribes_and_receives(qapp):
    be = _Backend()
    host = _Host(backend=be)
    got = []
    assert _pool(host)["stream.errors"](lambda d: got.append(d)) is True
    be.conversation_error.emit({"window_id": "w1", "error_type": "auth"})
    assert got == [{"window_id": "w1", "error_type": "auth"}]


def test_stream_errors_release_disconnects(qapp):
    from app.plugins.capabilities.registry import CapabilityRegistry

    be = _Backend()
    host = _Host(backend=be)
    got = []
    _pool(host, plugin="auto-fallback")["stream.errors"](lambda d: got.append(d))
    be.conversation_error.emit({"m": 1})
    assert len(got) == 1
    CapabilityRegistry.get_instance().release_plugin("auto-fallback")
    be.conversation_error.emit({"m": 2})
    assert len(got) == 1  # 插件卸载自动断开


def test_stream_errors_no_backend_raises(qapp):
    with pytest.raises(RuntimeError):
        _pool(_Host(backend=None))["stream.errors"](lambda d: None)


def test_stream_errors_missing_signal_returns_false(qapp):
    host = _Host(backend=object())  # 无 conversation_error 信号
    assert _pool(host)["stream.errors"](lambda d: None) is False


# ── window_id 定向（全局监听插件按目标窗口操作）──────────────

class _Tm:
    def __init__(self, windows=None):
        self._windows = list(windows or [])

    def get_current_window(self):
        return self._windows[0] if self._windows else None


class _Queue:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, wid, action, payload):
        self.enqueued.append((wid, action, payload))
        return True


def _two_windows(monkeypatch):
    """建两个窗口 stub（宿主 a + 目标 b），让 ui_core 的窗口解析命中它们。"""
    import app.plugins.capabilities.providers.ui_core as ui_core

    host_a = _Host(target=("cfg-a", "model-a"), window_id="w-a")
    win_b = _Host(target=("cfg-b", "model-b"), window_id="w-b")
    tm = _Tm([host_a, win_b])
    monkeypatch.setattr(ui_core, "_resolve_tab_window", lambda: tm)
    return host_a, win_b


def test_model_set_redirects_to_window_id(qapp, monkeypatch):
    host_a, win_b = _two_windows(monkeypatch)
    result = _pool(host_a)["model.set"](model="model-b", window_id="w-b")
    assert result == {"ok": True, "config_id": "cfg-b", "model": "model-b"}
    assert win_b._current_provider_name == "cfg-b"
    assert win_b._current_model_name == "model-b"
    assert win_b.btn_refreshed == 1
    # 宿主窗口不受影响
    assert host_a._current_provider_name == "cfg-old"


def test_model_set_unknown_window(qapp, monkeypatch):
    host_a, _ = _two_windows(monkeypatch)
    result = _pool(host_a)["model.set"](model="x", window_id="w-ghost")
    assert result["ok"] is False
    assert result["error"] == "window_not_found"
    assert host_a._current_provider_name == "cfg-old"


def test_session_send_redirects_to_window_id(qapp, monkeypatch):
    import app.core.conversation.action_queue as aq

    host_a, win_b = _two_windows(monkeypatch)
    q = _Queue()
    monkeypatch.setattr(aq, "get_action_queue", lambda: q)
    send = _pool(host_a, declared={"session.send"})["session.send"]
    assert send("继续", window_id="w-b") is True
    assert q.enqueued == [("w-b", "send", {"text": "继续", "ephemeral": False})]


def test_session_read_redirects_to_window_id(qapp, monkeypatch):
    from types import SimpleNamespace

    host_a, win_b = _two_windows(monkeypatch)
    sess = SimpleNamespace(
        session_id="s-b", name="n", messages=[{"role": "user", "content": "hi"}], system_prompt=""
    )
    win_b.backend = SimpleNamespace(get_current_session=lambda: sess)
    snap = _pool(host_a, declared={"session.read"})["session.read"](window_id="w-b")
    assert snap["session_id"] == "s-b"
    assert snap["messages"][0]["content"] == "hi"


def test_session_write_redirects_to_window_id(qapp, monkeypatch):
    host_a, win_b = _two_windows(monkeypatch)
    calls = []
    win_b._save_messages_to_session = lambda messages, task_prompt="": calls.append((messages, task_prompt))
    write = _pool(host_a, declared={"session.write"})["session.write"]
    write([{"role": "user", "content": "x"}], window_id="w-b")
    assert calls == [([{"role": "user", "content": "x"}], "")]
