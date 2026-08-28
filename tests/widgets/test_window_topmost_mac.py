"""macOS 软置顶回归测试

背景：`Qt.Window | Qt.WindowStaysOnTopHint` 在 macOS 上会把窗口提到
NSStatusWindowLevel(8)，WindowServer 对非 normal 层级窗口丢弃标题栏
最小化点击（黄按钮无反应）。修复后 macOS 走软置顶分支，不再给主窗口
加该 hint；Windows/Linux 保持原 hint 行为。
"""

from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

pytest.importorskip("PySide6")


@pytest.fixture()
def qapp():
    """确保 QApplication 可用"""
    app = QApplication.instance() or QApplication([])
    return app


class _FakeTopmostCfg:
    """最小 Settings 替身：仅 window_always_on_top 一个配置项"""

    def __init__(self, enabled: bool):
        self.window_always_on_top = type("Item", (), {"value": enabled})()


def _apply(window, enabled: bool, is_mac: bool):
    from app.widgets import tab_manager_window as tmw

    fake = _FakeTopmostCfg(enabled)
    with (
        patch("app.utils.config.Settings.get_instance", return_value=fake),
        patch.object(tmw, "_IS_MAC", is_mac),
    ):
        tmw._apply_window_topmost(window)


def test_mac_enabled_does_not_add_topmost_hint(qapp):
    """macOS + 置顶开启：不得加 WindowStaysOnTopHint（保最小化能力）"""
    w = QWidget()
    w.setWindowFlags(Qt.Window)
    _apply(w, enabled=True, is_mac=True)
    assert not (w.windowFlags() & Qt.WindowStaysOnTopHint)


def test_mac_strips_residual_topmost_hint(qapp):
    """macOS：摘除历史残留的 StaysOnTopHint（恢复最小化）"""
    w = QWidget()
    w.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
    _apply(w, enabled=False, is_mac=True)
    assert not (w.windowFlags() & Qt.WindowStaysOnTopHint)


def test_mac_enabled_strips_residual_and_raises(qapp):
    """macOS + 置顶开启：残留 hint 也必须摘除（置顶改由软置顶实现）"""
    w = QWidget()
    w.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
    _apply(w, enabled=True, is_mac=True)
    assert not (w.windowFlags() & Qt.WindowStaysOnTopHint)


def test_windows_enabled_adds_topmost_hint(qapp):
    """Windows/Linux + 置顶开启：保持原 hint 行为（与最小化兼容）"""
    w = QWidget()
    w.setWindowFlags(Qt.Window)
    _apply(w, enabled=True, is_mac=False)
    assert w.windowFlags() & Qt.WindowStaysOnTopHint


def test_windows_disabled_strips_topmost_hint(qapp):
    """Windows/Linux + 置顶关闭：摘除 hint"""
    w = QWidget()
    w.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
    _apply(w, enabled=False, is_mac=False)
    assert not (w.windowFlags() & Qt.WindowStaysOnTopHint)
