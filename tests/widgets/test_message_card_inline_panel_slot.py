# -*- coding: utf-8 -*-
"""MessageCard 通用内嵌面板槽位（ensure / remove / 高度锚定）

覆盖：未注册返回 None / 注册后创建并挂进气泡布局 / 幂等 / 移除 / 高度变化上报 /
插件卸载（registry.detach）链路。真实 MessageCard 构造，起 QApplication 后运行。
"""

import pytest

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtCore import pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def registry(monkeypatch):
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


class _DemoPanel(QWidget):
    """最小内嵌面板：带 heightChanged 信号 + 可控高度。"""

    heightChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fixed_h = 40

    def sizeHint(self):
        from PyQt5.QtCore import QSize

        return QSize(100, self._fixed_h)

    def set_demo_height(self, h):
        self._fixed_h = h
        self.heightChanged.emit()


@pytest.fixture()
def card(qapp):
    from app.widgets.message_card import MessageCard

    c = MessageCard(role="assistant")
    yield c
    c.deleteLater()


def test_missing_registration_returns_none(card, registry):
    assert card.ensure_inline_panel("nope") is None
    assert card.get_inline_panel("nope") is None


def test_ensure_creates_and_mounts(card, registry):
    registry.register_card_inline_panel("demo", "panel", _DemoPanel)

    panel = card.ensure_inline_panel("panel")

    assert isinstance(panel, _DemoPanel)
    assert card.get_inline_panel("panel") is panel
    lay = card._assistant_bubble.layout()
    assert lay.indexOf(panel) >= 0


def test_ensure_is_idempotent(card, registry):
    registry.register_card_inline_panel("demo", "panel", _DemoPanel)

    first = card.ensure_inline_panel("panel")
    second = card.ensure_inline_panel("panel")

    assert first is second
    lay = card._assistant_bubble.layout()
    assert sum(1 for i in range(lay.count()) if lay.itemAt(i).widget() is first) == 1


def test_remove_inline_panel_unmounts(card, registry):
    registry.register_card_inline_panel("demo", "panel", _DemoPanel)
    panel = card.ensure_inline_panel("panel")
    lay = card._assistant_bubble.layout()

    card.remove_inline_panel("panel")

    assert card.get_inline_panel("panel") is None
    assert lay.indexOf(panel) < 0


def test_height_change_reports_anchor_delta(card):
    """记账逻辑：可见面板按实际占高差累加 _last_height_delta（不依赖真实显示链）。"""

    class _VisiblePanel:
        def __init__(self, h):
            self._h = h

        def isVisible(self):
            return True

        def sizeHint(self):
            return self

        def height(self):
            return self._h

    fake = _VisiblePanel(40)
    card._inline_panels["panel"] = fake
    card._on_inline_panel_height_changed("panel")  # 基线
    card._last_height_delta = 0

    fake._h = 90
    card._on_inline_panel_height_changed("panel")

    assert card._last_height_delta == 50


def test_hidden_panel_reports_zero(card):
    """隐藏面板不记账（高度未参与布局，不应触发外层锚定补偿）。"""

    class _HiddenPanel:
        def isVisible(self):
            return False

        def sizeHint(self):
            return self

        def height(self):
            return 120

    card._inline_panels["panel"] = _HiddenPanel()
    card._last_height_delta = 0

    card._on_inline_panel_height_changed("panel")

    assert card._last_height_delta == 0


def test_detach_from_registry_removes_from_card(card, registry):
    registry.register_card_inline_panel("demo", "panel", _DemoPanel)
    host = type("_Host", (), {})()
    host._current_assistant_card = card
    host._find_latest_assistant_card = lambda: card

    assert registry.attach_card_inline_panel("panel", main_widget=host) is not None
    registry.detach_card_inline_panel("panel")

    assert card.get_inline_panel("panel") is None
