# -*- coding: utf-8 -*-
"""项目切换联动（EV_PROJECT_CHANGED 派发助手）测试"""

from app.core.project_changed import dispatch_project_changed, is_active_window


class _Sensitive:
    """实现了可选协议的卡片"""

    def __init__(self):
        self.calls = []

    def on_project_changed(self, project: str = "", workdir: str = "", window_id: str = "") -> None:
        self.calls.append((project, workdir, window_id))


class _Insensitive:
    """未实现可选协议的卡片"""


class _Boom:
    def on_project_changed(self, project: str = "", workdir: str = "", window_id: str = "") -> None:
        raise ValueError("boom")


def test_dispatch_calls_sensitive_widget():
    widget = _Sensitive()
    assert dispatch_project_changed(widget, project="项目A", workdir="D:/a", window_id="win_1") is True
    assert widget.calls == [("项目A", "D:/a", "win_1")]


def test_dispatch_skips_widget_without_protocol():
    assert dispatch_project_changed(_Insensitive(), project="项目A") is False


def test_dispatch_isolates_exception():
    """单卡异常不得向上冒泡（否则一个插件炸掉整条广播）"""
    assert dispatch_project_changed(_Boom(), project="项目A") is False


def test_is_active_window_passes_through_without_tab_manager(monkeypatch):
    import app.widgets.tab_manager_window as tmw

    monkeypatch.setattr(tmw.TabManagerWindow, "get_instance", staticmethod(lambda: None))
    assert is_active_window("win_x") is True


def test_is_active_window_filters_background_window(monkeypatch):
    import app.widgets.tab_manager_window as tmw

    class _Win:
        _window_id = "win_active"

    class _TM:
        @staticmethod
        def get_current_window():
            return _Win()

    monkeypatch.setattr(tmw.TabManagerWindow, "get_instance", staticmethod(lambda: _TM()))
    assert is_active_window("win_active") is True
    assert is_active_window("win_other") is False
    assert is_active_window("") is True  # 空 window_id 放行


def test_publish_project_changed_dedupes(monkeypatch):
    """同 (project, workdir) 重复同步只发一次；变化即发"""
    from app.core import ui_event_bus as bus_mod
    from app.main_widget import OpenAIChatToolWindow

    published = []

    class _Bus:
        def publish(self, event, **payload):
            published.append((event, payload))

    monkeypatch.setattr(bus_mod.UIEventBus, "get_instance", staticmethod(lambda: _Bus()))

    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)  # 不跑 __init__，避免真实控件
    win._window_id = "win_1"
    win._last_project_ctx = None

    OpenAIChatToolWindow._publish_project_changed(win, "项目A", "D:/a")
    OpenAIChatToolWindow._publish_project_changed(win, "项目A", "D:/a")  # 去重
    OpenAIChatToolWindow._publish_project_changed(win, "项目B", "D:/b")  # 变化

    assert [p[1]["project"] for p in published] == ["项目A", "项目B"]
    assert published[0][0] == bus_mod.EV_PROJECT_CHANGED
    assert published[0][1]["workdir"] == "D:/a"
    assert published[0][1]["window_id"] == "win_1"


def test_workbench_panel_dispatches_current_page(monkeypatch):
    """面板只对当前页派发；非当前页由切页 refresh_data 补刷"""
    import app.widgets.workbench_panel as wbp

    panel = wbp.WorkbenchPanel.__new__(wbp.WorkbenchPanel)  # 不跑 __init__，避免真实控件
    calls = []

    class _Page:
        def on_project_changed(self, project="", workdir="", window_id=""):
            calls.append(("page", project))

    class _Stack:
        @staticmethod
        def currentWidget():
            return _Page()

    panel._stack = _Stack()
    monkeypatch.setattr(wbp, "is_active_window", lambda _wid: True)

    wbp.WorkbenchPanel._on_project_changed_event(
        panel, {"project": "项目B", "workdir": "D:/b", "window_id": "w1"}
    )
    assert calls == [("page", "项目B")]


def test_workbench_panel_skips_background_window(monkeypatch):
    """非活跃窗口的变更不派发（切回该窗口时由显示路径补刷）"""
    import app.widgets.workbench_panel as wbp

    panel = wbp.WorkbenchPanel.__new__(wbp.WorkbenchPanel)
    calls = []

    class _Page:
        def on_project_changed(self, project="", workdir="", window_id=""):
            calls.append(project)

    class _Stack:
        @staticmethod
        def currentWidget():
            return _Page()

    panel._stack = _Stack()
    monkeypatch.setattr(wbp, "is_active_window", lambda _wid: False)

    wbp.WorkbenchPanel._on_project_changed_event(panel, {"project": "项目B", "window_id": "w2"})
    assert calls == []
