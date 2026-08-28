# -*- coding: utf-8 -*-
"""回归：left/right 浮动卡片 toggle 后 dock wrapper 必须展开

覆盖 _DockSideWrapper isVisible() 死锁：wrapper 默认 hide 时，子控件
primary.isVisible() 恒为 False（祖先链遮蔽），_sync 永远走收起分支，
导致左右侧 UI 插件浮动卡片无法显示。
"""

from PySide6.QtWidgets import QApplication, QWidget

import pytest

from app.widgets.tab_manager_window import TabManagerWindow
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


def _toggle_left_card(tm, card_id):
    reg = UIPluginRegistry.get_instance()
    reg.register_floating_card("tp", card_id, QWidget, "left", title="T")
    reg.toggle_floating_card(card_id)
    for _ in range(5):
        QApplication.processEvents()
    return tm._global_left_wrapper, tm._dock_splitter


def test_initial_collapsed_state(qtbot):
    """初始态（无卡片显示）：wrapper 保持收起、splitter 不给左右 dock 分宽（01edd48f/37933366 行为）"""
    tm = TabManagerWindow.create_instance()
    for _ in range(3):
        QApplication.processEvents()
    assert tm._global_left_wrapper.isHidden(), "无卡片时左 wrapper 应保持收起"
    assert tm._global_right_wrapper.isHidden(), "无卡片时右 wrapper 应保持收起"
    sizes = tm._dock_splitter.sizes()
    assert sizes[0] == 0 and sizes[2] == 0, f"左右 dock 不应分宽，实际 sizes={sizes}"


def test_left_dock_expands_when_window_shown(qtbot):
    """窗口已 show：toggle 左卡片后 wrapper 展开、splitter 分配宽度、卡片可见"""
    tm = TabManagerWindow.create_instance()
    tm.show()
    qtbot.wait(50)
    wrapper, splitter = _toggle_left_card(tm, "tp-left-shown")
    idx = splitter.indexOf(wrapper)
    sizes = splitter.sizes()
    card = next(iter(UIPluginRegistry.get_instance()._card_widget_instances.values()))["tp-left-shown"]
    assert not wrapper.isHidden(), "wrapper 应展开（show）"
    assert sizes[idx] > 0, f"splitter 应给左侧 dock 分配宽度，实际 sizes={sizes}"
    assert card.isVisible(), "卡片应可见"


def test_left_dock_expands_before_window_shown(qtbot):
    """启动时序：窗口未 show 时 toggle，wrapper 仍应记录展开意图（不因祖先隐藏死锁）"""
    tm = TabManagerWindow.create_instance()
    wrapper, splitter = _toggle_left_card(tm, "tp-left-noshow")
    idx = splitter.indexOf(wrapper)
    sizes = splitter.sizes()
    assert not wrapper.isHidden(), "wrapper 应有展开意图（显式 show 过）"
    assert sizes[idx] > 0, f"splitter 应给左侧 dock 分配宽度，实际 sizes={sizes}"
    tm.show()
    qtbot.wait(50)
    QApplication.processEvents()
    assert wrapper.isVisible(), "窗口 show 后 wrapper 应真正可见"
    card = None
    for win_map in UIPluginRegistry.get_instance()._card_widget_instances.values():
        card = win_map.get("tp-left-noshow")
        if card is not None:
            break
    assert card is not None and card.isVisible()
