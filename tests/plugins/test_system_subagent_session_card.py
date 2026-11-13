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

pytest.importorskip("PyQt5.QtWidgets")
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
    from PyQt5.QtWidgets import QApplication

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

def test_lazy_webview_lifecycle(card):
    """懒创建 + 隐藏硬释放：构造 None → load 建成 → hideEvent 释放 → load 重建。"""
    assert card._web_view is None  # 构造即建是旧盲区，现在必须懒
    card.load(
        "t1",
        "coder",
        [{"type": "progress", "content": "开始", "timestamp": 0}],
        {"status": "finished"},
    )
    assert card._web_view is not None  # load 懒创建
    old = card._web_view
    card.hideEvent(None)  # 互斥隐藏派发（QHideEvent 不可手工构造，签名允许 None）
    assert card._web_view is None  # 硬释放（renderer 进程随页面销毁收敛）
    card.load("t1", "coder", [], {"status": "finished"})
    assert card._web_view is not None and card._web_view is not old  # 重建为全新实例

def test_runJavaScript_guards_after_release(card):
    """释放后轮询消费点早退不崩（_check_and_update / _apply_update 路径）。"""
    card.load("t1", "coder", [], {"status": "running"})
    card._web_view = None  # 模拟释放后的轮询竞态
    card._logs_provider = lambda: {
        "found": True,
        "logs": [{"type": "progress", "content": "新日志", "timestamp": 1}],
        "summary": {"status": "running"},
    }
    card._poll_update()  # 不抛异常即守卫生效


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

def test_recover_rebuilds_webview_after_hard_release(card, monkeypatch):
    """守卫命中且 _web_view 已被 hideEvent 硬释放：重建并重渲染（二次显示空白回归）。

    触发路径：卡片同会话恢复过一次（_bound_session_id 已赋）→ 打开另一系统卡
    互斥隐藏（hideEvent deleteLater WebView）→ 再点开。守卫命中只跳过数据重灌，
    不得跳过 WebView 重建，否则摘要栏在、日志区永久空白。
    """
    card.load(
        "t1",
        "coder",
        [{"type": "progress", "content": "开始", "timestamp": 0}],
        {"status": "finished"},
    )
    card._bound_session_id = "sess-1"
    card.hideEvent(None)  # 互斥隐藏：硬释放 WebView（QHideEvent 不可手工构造，签名允许 None）
    assert card._web_view is None

    facade = _FakeFacade({"found": True, "task_id": "t9", "agent_name": "x", "logs": [], "summary": {}})
    monkeypatch.setattr(_session_card_mod, "_current_facade", lambda: (facade, "sess-1"))
    card._recover_for_current_session()

    # 守卫命中：不重灌数据（仍绑定原任务）
    assert card._task_id == "t1"
    # 但 WebView 必须重建且渲染链已执行（_load_html 写临时文件为渲染发生物证）
    assert card._web_view is not None
    assert card._tmp_files, "守卫命中分支应触发 _render_logs 重灌日志区"

    # _web_view 存活时再次恢复：不重复创建（不闪屏不重载）
    kept = card._web_view
    card._recover_for_current_session()
    assert card._web_view is kept


def test_recover_noop_without_facade(card, monkeypatch):
    monkeypatch.setattr(_session_card_mod, "_current_facade", lambda: (None, ""))

    card._recover_for_current_session()

    assert card._task_id == ""

def test_result_is_failure_distinguishes_states():
    """失败判定：error 非空 / 失败终态 / 历史「执行出错:」正文都算失败。"""
    f = _session_card_mod._result_is_failure
    assert f("", "Task stalled: no log activity", "stalled") is True
    assert f("完成", "SubAgent execution error: boom", "error") is True
    # 修复前的伪成功数据：DB 状态 finished、error 为空，正文是异常字符串
    assert f("执行出错: The read operation timed out", "", "finished") is True
    assert f("正常结果", "", "finished") is False
    assert f("", "", "running") is False

def test_build_html_renders_failure_when_result_is_error_string(card):
    """历史伪成功数据在会话卡不再显示 ✅（截图场景回归）。"""
    card.load(
        "t1",
        "code-reviewer",
        [],
        {"status": "finished", "result": "执行出错: The read operation timed out", "error": ""},
    )
    html = card._build_html()
    assert "❌ 执行失败" in html
    assert "✅ 执行结果" not in html
    assert "#F44336" in html

def test_build_html_renders_success_for_normal_result(card):
    """正常结果仍走绿色成功态（防矫枉过正）。"""
    card.load("t2", "explore", [], {"status": "finished", "result": "一切正常", "error": ""})
    html = card._build_html()
    assert "✅ 执行结果" in html
    assert "❌ 执行失败" not in html
