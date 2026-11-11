# -*- coding: utf-8 -*-
"""system-subagent 会话卡（full 容器浮动卡）与服务门面

覆盖：卡片可离线构造并 load（迁移后 import/基类契约完好）；服务 show 的
toggle 语义（未显示才显式、已显示只刷数据）与失败降级。
"""

import importlib
import importlib.util
import os
import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("qfluentwidgets")

_PKG = "system_subagent_ui_pkg"


def _load_ui_pkg():
    root = os.path.abspath(".")
    ui_path = os.path.join(root, "plugins", "system-subagent", "ui")
    init_path = os.path.join(ui_path, "__init__.py")
    if _PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PKG, init_path, submodule_search_locations=[ui_path])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = module
        spec.loader.exec_module(module)
    return (
        importlib.import_module(f"{_PKG}.session_card"),
        importlib.import_module(f"{_PKG}.session_service"),
    )


_session_card_mod, _session_service_mod = _load_ui_pkg()
SubAgentSessionCard = _session_card_mod.SubAgentSessionCard
SubAgentSessionService = _session_service_mod.SubAgentSessionService
CARD_ID = _session_service_mod.CARD_ID


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def card(qapp):
    c = SubAgentSessionCard()
    yield c
    c.deleteLater()


def test_card_loads_logs_and_summary(card):
    """迁移后的卡片能接收日志与摘要（closed 信号为浮卡契约必需件）。"""
    assert card.closed is not None
    card.load(
        "t1",
        "coder",
        [{"type": "tool_call", "content": "read_file", "tool_name": "read_file"}],
        {"status": "running"},
        None,
    )
    assert card._task_id == "t1"
    assert card._agent_name == "coder"
    assert len(card._logs) == 1


class _FakeWidget:
    def __init__(self, visible: bool = False):
        self.visible = visible
        self.loaded = None

    def isVisible(self):
        return self.visible

    def load(self, *args, **kwargs):
        self.loaded = (args, kwargs)
        self.visible = True


class _FakeRegistry:
    def __init__(self, widget):
        self.widget = widget
        self.calls = []

    def get_floating_card_widget(self, card_id, main_widget=None):
        return self.widget

    def toggle_floating_card(self, card_id, main_widget=None):
        self.calls.append(("toggle", card_id))

    def hide_floating_card_globally(self, card_id):
        self.calls.append(("hide", card_id))


def test_service_shows_card_when_hidden():
    widget = _FakeWidget(visible=False)
    registry = _FakeRegistry(widget)
    service = SubAgentSessionService(registry)

    assert service.show("t1", "coder", [], {"status": "running"}) is True
    assert registry.calls == [("toggle", CARD_ID)]
    assert widget.loaded is not None


def test_service_refreshes_without_toggling_visible_card():
    widget = _FakeWidget(visible=True)
    registry = _FakeRegistry(widget)
    service = SubAgentSessionService(registry)

    service.show("t1", "coder", [], {})

    assert registry.calls == []
    assert widget.loaded is not None


def test_service_returns_false_when_card_unavailable():
    registry = _FakeRegistry(None)
    service = SubAgentSessionService(registry)

    assert service.show("t1", "coder", [], {}) is False
    assert registry.calls == [("toggle", CARD_ID)]


def test_service_hide_uses_registry():
    registry = _FakeRegistry(_FakeWidget())
    SubAgentSessionService(registry).hide()

    assert registry.calls == [("hide", CARD_ID)]


class _FakeFacade:
    """窗口服务门面桩：给出当前会话最近任务。"""

    def __init__(self, data):
        self._data = data
        self.log_calls = []

    def latest_task(self):
        return self._data

    def get_task_logs(self, task_id):
        self.log_calls.append(task_id)
        return {"found": True, "logs": [], "summary": {}}


def test_recover_loads_latest_task_of_current_session(card, monkeypatch):
    """显示时按当前会话恢复内容（同成果框语义）。"""
    facade = _FakeFacade(
        {
            "found": True,
            "task_id": "t9",
            "agent_name": "explore",
            "logs": [{"type": "progress", "content": "开始", "timestamp": 0}],
            "summary": {"status": "finished"},
        }
    )
    monkeypatch.setattr(_session_card_mod, "_current_facade", lambda: (facade, "sess-1"))

    card._recover_for_current_session()

    assert card._task_id == "t9"
    assert card._agent_name == "explore"
    assert card._bound_session_id == "sess-1"
    assert len(card._logs) == 1


def test_recover_skips_when_same_session_already_bound(card, monkeypatch):
    """已绑定同一会话的任务时不重复灌数据。"""
    card.load("t1", "coder", [], {})
    card._bound_session_id = "sess-1"
    facade = _FakeFacade({"found": True, "task_id": "t9", "agent_name": "x", "logs": [], "summary": {}})
    monkeypatch.setattr(_session_card_mod, "_current_facade", lambda: (facade, "sess-1"))

    card._recover_for_current_session()

    assert card._task_id == "t1"


def test_recover_noop_without_facade(card, monkeypatch):
    monkeypatch.setattr(_session_card_mod, "_current_facade", lambda: (None, ""))

    card._recover_for_current_session()

    assert card._task_id == ""
