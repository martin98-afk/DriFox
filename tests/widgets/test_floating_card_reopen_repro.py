# -*- coding: utf-8 -*-
"""复现：UI 插件浮动卡（full/left）只能打开一次，第二次 toggle 无法再显示

用户路径：侧栏点击开卡 → 点卡片 X 关闭 → 再点侧栏 → 无反应。
[DEBUG-reopen] 临时复现用例，定位后转正式回归或删除。
"""

from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QApplication, QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID, CardManager
from app.widgets.tab_manager_window import TabManagerWindow


@pytest.fixture(autouse=True)
def reset_state():
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    UIPluginRegistry.get_instance().reset()
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    UIPluginRegistry.get_instance().reset()


def _make_env(qtbot, container: str, card_id: str):
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm._windows = [SimpleNamespace(_window_id="winA")]
    tm._tab_panel._active_index = 0
    reg = UIPluginRegistry.get_instance()
    reg._active_tab_scope = "winA"
    reg.register_floating_card("test-plugin", card_id, QWidget, container, title="Repro")
    return tm, reg


def _pump(n: int = 3):
    for _ in range(n):
        QApplication.processEvents()


@pytest.mark.parametrize("container", ["full", "left"])
def test_close_then_reopen_floating_card(qtbot, container):
    """开 → 关（模拟点 X）→ 再开：第二次必须能重新显示"""
    card_id = f"reopen-{container}"
    tm, reg = _make_env(qtbot, container, card_id)
    cm = CardManager.get_instance()

    # 第一次打开
    reg.toggle_floating_card(card_id)
    _pump()
    widget = reg.get_floating_card_widget(card_id)
    assert widget is not None
    assert cm.is_card_visible(card_id, GLOBAL_WINDOW_ID), f"[{container}] 第一次打开失败"
    assert not widget.isHidden(), f"[{container}] 第一次打开 widget 未显示"

    # 模拟用户点 X：widget 隐藏 + closed 信号 → registry lambda → hide_card
    widget.setVisible(False)
    widget.closed.emit()
    _pump()
    assert not cm.is_card_visible(card_id, GLOBAL_WINDOW_ID), f"[{container}] 关闭后记账未清"

    # 第二次打开（复现点）
    reg.toggle_floating_card(card_id)
    _pump()
    assert cm.is_card_visible(card_id, GLOBAL_WINDOW_ID), f"[{container}] 第二次记账不可见"
    assert not widget.isHidden(), f"[{container}] 第二次打开 widget 未显示（用户视角：打不开）"
