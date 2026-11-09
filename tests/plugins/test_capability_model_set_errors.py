# -*- coding: utf-8 -*-
"""model.set / stream.errors：设置窗口模型 / 订阅结构化报错事件。"""

import pytest
from PyQt5.QtCore import QObject, pyqtSignal


class _Backend(QObject):
    conversation_error = pyqtSignal(dict)


class _Host:
    """模拟 MainWidget 的最小面：解析 stub + 赋值字段 + 刷新计数。"""

    def __init__(self, backend=None, target=None):
        self.backend = backend
        self._target = target
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
    return CapabilityRegistry.get_instance().build_pool(surface="ui", host=host, plugin=plugin, declared=declared)


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
