# -*- coding: utf-8 -*-
"""树行化回归（v2 两行式）：已打开对话页在树模式下并入树内行体系

覆盖：
- 树模式重建：行高 44 / 无图标 / 标题 12px / 副行可见 / margins (8,5,6,5)
- 切回列表：完整复位（行高 40 / 图标恢复 / 13px / 副行隐藏 / margins 还原）
- 选中渲染：淡靛蓝（深色主题采样中心像素）
- 关闭钮 / 图钉：hover 显 → 移出隐（置顶态常显）
- set_tree_row 幂等
"""

from unittest.mock import patch

import pytest
from PyQt5.QtCore import QEvent

from app.utils.design_tokens import scale_font_size
from app.widgets.tab_panel import PANEL_MODE_LIST, PANEL_MODE_TREE, TabPanel, TreeNodeSpec
from app.widgets.workspace_tree import KIND_PROJECT, KIND_WIDGET


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    # 固定列表模式：TabPanel 会恢复上次持久化模式（本地存了 tree 会误判）
    p.set_mode(PANEL_MODE_LIST, persist=False)
    qtbot.addWidget(p)
    return p


def _add_tab(panel):
    idx = panel.add_tab("对话 A", project_initials="DF", project_color="#22c55e")
    return panel._items[idx]


def _tree_specs_with(item):
    """构造含该 TabItem 的最小树节点序列（v2：项目直挂会话，无工作树层）"""
    return [
        TreeNodeSpec(key="project:Demo", kind=KIND_PROJECT, title="Demo", expanded_by_default=True),
        TreeNodeSpec(
            key="tab:0",
            kind=KIND_WIDGET,
            title="",
            indent=14,
            widget=item,
            record={"session_id": "s1", "preview": "测试预览"},
            subtitle="测试预览 · 刚刚",
        ),
    ]


def _switch_to_tree(panel, item):
    with (
        patch.object(panel, "_warn_tree_mode_in_dev"),
        patch.object(panel, "_collect_tree_specs", return_value=_tree_specs_with(item)),
    ):
        panel.set_mode(PANEL_MODE_TREE, persist=False)


def test_tree_mode_marks_item_as_tree_row(panel):
    """树模式重建：行高 44 / 图标隐藏 / 12px / 副行可见"""
    item = _add_tab(panel)
    _switch_to_tree(panel, item)
    assert item._tree_row is True
    # 未 show 的 widget .height() 不即时更新，断 min/max 分档
    assert item.minimumHeight() == 44 and item.maximumHeight() == 44
    assert item._icon_widget.isHidden() is True
    # 字号断 stylesheet（font().pointSize() 会被 stylesheet 合成字体置 -1）
    assert f"font-size: {scale_font_size(12)}px" in item._title_label.styleSheet()
    assert item.layout().getContentsMargins() == (8, 5, 6, 5)
    assert item._sub_label.isHidden() is False


def test_tree_mode_syncs_subtitle(panel):
    """树模式副行文本按 spec 同步（预览 · 时间）"""
    item = _add_tab(panel)
    _switch_to_tree(panel, item)
    assert item._sub_label.text() == "测试预览 · 刚刚"


def test_switch_back_to_list_resets_tree_row(panel):
    """切回列表：完整复位（行高/图标/字号/副行/margins）"""
    item = _add_tab(panel)
    _switch_to_tree(panel, item)
    panel.set_mode(PANEL_MODE_LIST, persist=False)
    assert item._tree_row is False
    assert item.minimumHeight() == 40 and item.maximumHeight() == 40
    assert item._icon_widget.isHidden() is False
    assert f"font-size: {scale_font_size(13)}px" in item._title_label.styleSheet()
    assert item.layout().getContentsMargins() == (8, 4, 4, 4)
    assert item._sub_label.isHidden() is True


def test_tree_row_selected_paints_tint(panel, monkeypatch):
    """树行选中：淡靛蓝底色（深色主题采样中心像素）"""
    monkeypatch.setattr("app.widgets.tab_panel.isDarkTheme", lambda: True)
    item = _add_tab(panel)
    item.set_tree_row(True)
    item.set_selected(True)
    item.resize(240, 44)
    img = item.grab().toImage()
    center = img.pixelColor(120, 22)
    assert center.alpha() > 0, "树行选中应有底色"
    # 淡靛蓝 rgba(99,102,241,56)，预乘往返误差容差
    assert abs(center.red() - 99) <= 12 and abs(center.green() - 102) <= 12 and abs(center.blue() - 241) <= 12
    assert abs(center.alpha() - 56) <= 8


def test_tree_row_close_button_hover_only(panel):
    """树行关闭钮：hover 显 → 移出隐（选中不保持）"""
    item = _add_tab(panel)
    item.set_tree_row(True)
    item.set_selected(True)
    assert item._close_btn.isHidden() is True

    item.enterEvent(QEvent(QEvent.Enter))
    assert item._close_btn.isHidden() is False

    item.leaveEvent(QEvent(QEvent.Leave))
    assert item._close_btn.isHidden() is True, "树行移出即隐（选中不保持）"


def test_tree_row_pin_button_hover_and_pinned(panel):
    """树行图钉：hover 显 / 移出隐 / 置顶后常显"""
    item = _add_tab(panel)
    item.set_tree_row(True)
    assert item._pin_btn.isHidden() is True

    item.enterEvent(QEvent(QEvent.Enter))
    assert item._pin_btn.isHidden() is False

    item.leaveEvent(QEvent(QEvent.Leave))
    assert item._pin_btn.isHidden() is True

    # 置顶态常显（移出后仍在）
    item.set_pinned(True)
    assert item._pin_btn.isHidden() is False
    item.set_pinned(False)
    assert item._pin_btn.isHidden() is True


def test_list_mode_hides_pin_button(panel):
    """列表模式：图钉不参与显示（即使置顶态为真）"""
    item = _add_tab(panel)
    item.set_pinned(True)
    assert item._tree_row is False
    assert item._pin_btn.isHidden() is True


def test_set_tree_row_idempotent(panel):
    """set_tree_row 幂等：重复调用无副作用"""
    item = _add_tab(panel)
    item.set_tree_row(True)
    item.set_tree_row(True)
    assert item.minimumHeight() == 44
    item.set_tree_row(False)
    item.set_tree_row(False)
    assert item.minimumHeight() == 40
