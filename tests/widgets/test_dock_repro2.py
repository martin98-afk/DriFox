# -*- coding: utf-8 -*-
"""回归：左侧浮动卡片 toggle 后卡片区必须展开

覆盖 _DockSideWrapper isVisible() 死锁：wrapper 默认 hide 时，子控件
primary.isVisible() 恒为 False（祖先链遮蔽），_sync 永远走收起分支，
导致左侧 UI 插件浮动卡片无法显示。

注：left 卡片宿主已从对话区 dock_splitter 迁到 #tabFrame 内的
sidebarSplitter（卡片嵌在侧边栏内部右侧，2026-10-12）；dockSplitter
保留仅供外部 objectName 探测 Tab 模式，不再承载左卡片区。
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
    return tm._global_left_wrapper, tm._sidebar_splitter


def _reset_card_orchestration(tm):
    """跨文件单例卫生：复位 left 卡片编排留下的状态

    本文件与 test_left_card_sidebar_embed.py 共用 TabManagerWindow 单例；
    后者跑完后若残留「卡片占位」痕迹（wrapper 已 show、内部槽位非 0、
    TabPanel 占位开关）会让本文件的初始态断言失败。此处显式复位。
    """
    panel = getattr(tm, "_tab_panel", None)
    if panel is not None:
        panel._left_card_present = False
        panel._dragging_splitter = False
    wrapper = getattr(tm, "_global_left_wrapper", None)
    if wrapper is not None:
        for cid in list(wrapper._primary._cards.keys()):
            try:
                wrapper._primary._cards[cid].setVisible(False)
            except Exception:
                pass
        wrapper._sync()
    for _ in range(4):
        QApplication.processEvents()


def test_initial_collapsed_state(qtbot):
    """初始态（无卡片显示）：wrapper 保持收起、sidebar splitter 不给卡片区分宽"""
    tm = TabManagerWindow.create_instance()
    _reset_card_orchestration(tm)
    for _ in range(3):
        QApplication.processEvents()
    qtbot.wait(200)
    QApplication.processEvents()
    assert tm._global_left_wrapper.isHidden(), "无卡片时左 wrapper 应保持收起"
    sizes = tm._sidebar_splitter.sizes()
    idx = tm._sidebar_splitter.indexOf(tm._global_left_wrapper)
    assert sizes[idx] == 0, f"卡片区不应分宽，实际 sizes={sizes}"


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
