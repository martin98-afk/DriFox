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
