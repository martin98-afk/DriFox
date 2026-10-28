# -*- coding: utf-8 -*-
"""CustomTabButton.set_badge 回归：页签徽标挂载/清空/截断/主题刷新。"""

import sys
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def _qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    return app


def _make_button(_qapp, closable=False):
    from app.widgets.custom_title_bar import CustomTabButton

    return CustomTabButton("t1", "页签", closable=closable)


class TestTabBadge:
    def test_badge_shows_and_hides(self, _qapp):
        btn = _make_button(_qapp)
        assert btn._badge_label is None, "无徽标时不预建 label"
        btn.set_badge("3")
        assert btn._badge_label is not None
        assert btn._badge_label.isVisible() or not btn.isVisible()  # 父未显示时看 text
        assert btn._badge_label.text() == "3"
        assert btn._badge_text == "3"
        btn.set_badge("")
        assert not btn._badge_label.isVisible()
        assert btn._badge_text == ""

    def test_badge_truncated_over_three_chars(self, _qapp):
        btn = _make_button(_qapp)
        btn.set_badge("999+")
        assert btn._badge_label.text() == "…"

    def test_badge_tooltip(self, _qapp):
        btn = _make_button(_qapp)
        btn.set_badge("5", tooltip="5 条未读")
        assert btn._badge_label.toolTip() == "5 条未读"
        btn.set_badge("6")
        assert btn._badge_label.toolTip() == ""

    def test_badge_inserted_before_close_button(self, _qapp):
        """有 × 时徽标在文字与 × 之间"""
        btn = _make_button(_qapp, closable=True)
        btn.set_badge("1")
        layout = btn.layout()
        assert layout.indexOf(btn._badge_label) < layout.indexOf(btn._close_btn)
        assert layout.indexOf(btn._label) < layout.indexOf(btn._badge_label)

    def test_refresh_style_keeps_badge(self, _qapp):
        """主题刷新后徽标样式与可见性保持"""
        btn = _make_button(_qapp)
        btn.set_badge("7")
        btn.refresh_style()
        assert btn._badge_label.property("styleSheet")
        assert btn._badge_text == "7"


class TestTabCustomWidget:
    def test_set_widget_mounts_and_clears_text_badge(self, _qapp):
        """挂自定义 widget 时清文本徽标（互斥）"""
        from PySide6.QtWidgets import QLabel

        btn = _make_button(_qapp)
        btn.set_badge("3")
        w = QLabel("●")
        btn.set_widget(w)
        assert btn._custom_widget is w
        assert w.parent() is btn
        assert btn._badge_text == ""
        assert not btn._badge_label.isVisible()

    def test_set_widget_replaces_old_with_delete_later(self, _qapp):
        from PySide6.QtWidgets import QLabel

        btn = _make_button(_qapp)
        w1, w2 = QLabel("a"), QLabel("b")
        btn.set_widget(w1)
        btn.set_widget(w2)
        assert btn._custom_widget is w2
        assert w1.parent() is None, "旧 widget 已摘除，等 deleteLater 销毁"

    def test_set_widget_none_clears(self, _qapp):
        from PySide6.QtWidgets import QLabel

        btn = _make_button(_qapp)
        btn.set_widget(QLabel("x"))
        btn.set_widget(None)
        assert btn._custom_widget is None

    def test_set_badge_drops_custom_widget(self, _qapp):
        """文本徽标反向互斥：清掉自定义 widget"""
        from PySide6.QtWidgets import QLabel

        btn = _make_button(_qapp)
        btn.set_widget(QLabel("x"))
        btn.set_badge("2")
        assert btn._custom_widget is None
        assert btn._badge_text == "2"

    def test_apply_badges_dispatches_three_payload_forms(self, _qapp, monkeypatch):
        """面板消费侧 payload 三形态分派：str/QWidget/工厂；工厂按条目幂等"""
        from PySide6.QtWidgets import QLabel

        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
        from app.widgets.custom_title_bar import CustomTabButton
        from app.widgets.workbench_panel import WorkbenchPanel

        made = []
        widget_entry = SimpleNamespace(entry_id="w1", metadata={"tab_id": "p1"}, payload=QLabel("w"))
        factory_entry = SimpleNamespace(
            entry_id="f1", metadata={"tab_id": "p2"}, payload=lambda: (made.append(1), QLabel("f"))[1]
        )
        text_entry = SimpleNamespace(entry_id="t1", metadata={"tab_id": "p3"}, payload="4")

        monkeypatch.setattr(
            UIPluginRegistry.get_instance(),
            "get_region_entries",
            lambda rid: [widget_entry, factory_entry, text_entry] if rid == "workbench:tab" else [],
        )

        buttons = [
            CustomTabButton("p1", "页1"),
            CustomTabButton("p2", "页2"),
            CustomTabButton("p3", "页3"),
        ]

        class FakePanel:
            _apply_badges = WorkbenchPanel._apply_badges
            _tab_buttons = buttons

        FakePanel()._apply_badges()

        assert buttons[0]._custom_widget is widget_entry.payload
        assert isinstance(buttons[1]._custom_widget, QLabel) and len(made) == 1
        assert buttons[2]._badge_text == "4" and buttons[2]._custom_widget is None

        # 工厂幂等：同一条目不重复调用；文本徽标幂等
        FakePanel()._apply_badges()
        assert len(made) == 1
        assert buttons[2]._badge_text == "4"
