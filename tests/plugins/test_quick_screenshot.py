# -*- coding: utf-8 -*-
"""quick-screenshot 插件测试：坐标换算 / 误触取消 / 选区捕获 / 注册行为。"""

import importlib.util
import sys
from pathlib import Path

import pytest
from PyQt5.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt5.QtGui import QColor, QMouseEvent, QPixmap
from PyQt5.QtWidgets import QApplication

# plugins/ 非 Python 包，用 _load_module 模式加载（复用 test_rewrite_inline_script.py 做法）
_PLUGIN_UI = Path(__file__).resolve().parents[2] / "plugins" / "quick-screenshot" / "ui"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ========================================================================
# 坐标换算（高 DPI）
# ========================================================================

def test_physical_rect_identity_at_dpr1():
    mod = _load_module("qs_overlay_test", _PLUGIN_UI / "overlay.py")
    r = mod._physical_rect(QRect(10, 20, 100, 50), 1.0)
    assert (r.x(), r.y(), r.width(), r.height()) == (10, 20, 100, 50)


def test_physical_rect_scales_at_dpr15():
    mod = _load_module("qs_overlay_test2", _PLUGIN_UI / "overlay.py")
    r = mod._physical_rect(QRect(10, 20, 100, 50), 1.5)
    assert (r.x(), r.y(), r.width(), r.height()) == (15, 30, 150, 75)


# ========================================================================
# 遮罩窗交互
# ========================================================================

@pytest.fixture()
def overlay(qtbot):
    mod = _load_module("qs_overlay_test3", _PLUGIN_UI / "overlay.py")
    base = QPixmap(200, 100)
    base.fill(QColor("#123456"))
    ov = mod._ScreenshotOverlay(base, QRect(0, 0, 200, 100))
    yield ov
    # 自管清理：WA_DeleteOnClose 后 qtbot.addWidget 的 teardown 二次 close 会 RuntimeError
    try:
        ov.close()
    except RuntimeError:
        pass
    try:
        ov.deleteLater()
    except RuntimeError:
        pass


def _drag(widget, press: QPoint, move: QPoint, release: QPoint) -> None:
    """手动派发拖拽鼠标事件（qtbot.mouseMove 在未 show 的 widget 上不派发）。"""

    def _send(etype: QEvent.Type, p: QPoint, button: Qt.MouseButton, buttons: Qt.MouseButtons):
        e = QMouseEvent(etype, QPointF(p), button, buttons, Qt.NoModifier)
        QApplication.sendEvent(widget, e)

    _send(QEvent.MouseButtonPress, press, Qt.LeftButton, Qt.LeftButton)
    _send(QEvent.MouseMove, move, Qt.NoButton, Qt.LeftButton)
    _send(QEvent.MouseButtonRelease, release, Qt.LeftButton, Qt.NoButton)


def test_small_release_emits_cancelled(overlay, qtbot):
    """按下即松开（<4px）→ 误触，只发 cancelled 不发 captured。"""
    with qtbot.waitSignal(overlay.cancelled, timeout=2000):
        qtbot.mousePress(overlay, Qt.LeftButton, pos=QPoint(50, 50))
        qtbot.mouseRelease(overlay, Qt.LeftButton, pos=QPoint(51, 51))


def test_drag_capture_emits_pixmap(overlay, qtbot):
    """拖框 (10,10)-(110,60) 松手 → captured 携带 101x51 pixmap。

    QRect 两点构造含端点（right/bottom inclusive），首尾像素都计入选区，
    与 Snipaste 等截图工具坐标语义一致。
    """
    with qtbot.waitSignal(overlay.captured, timeout=2000) as blocker:
        _drag(overlay, QPoint(10, 10), QPoint(110, 60), QPoint(110, 60))
    pm = blocker.args[0]
    assert (pm.width(), pm.height()) == (101, 51)


def test_reverse_drag_normalizes(overlay, qtbot):
    """从右下往左上拖 → normalized 选区尺寸不变。"""
    with qtbot.waitSignal(overlay.captured, timeout=2000) as blocker:
        _drag(overlay, QPoint(110, 60), QPoint(10, 10), QPoint(10, 10))
    assert (blocker.args[0].width(), blocker.args[0].height()) == (101, 51)


# ========================================================================
# 注册行为
# ========================================================================

@pytest.fixture()
def fresh_registry(monkeypatch):
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


def test_register_ui_registers_input_button(fresh_registry):
    ui = _load_module("qs_ui_test", _PLUGIN_UI / "__init__.py")
    ui.register_ui(fresh_registry)
    buttons = fresh_registry.get_input_buttons()
    assert len(buttons) == 1
    info = buttons[0]
    assert info.plugin_name == "quick-screenshot"
    assert info.button_id == "quick-screenshot"
    assert info.tooltip == "选区截图（复制到剪贴板）"
    assert info.icon_path.endswith("screenshot.svg")
    assert info.icon_light_path.endswith("screenshot_light.svg")
    assert callable(info.on_click)
