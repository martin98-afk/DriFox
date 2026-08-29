"""CustomTitleBar 单元测试：tab 增删/激活/信号/主题刷新/mac 分支"""

import sys

import pytest
from PyQt5.QtWidgets import QWidget

from app.widgets.custom_title_bar import CustomTitleBar


@pytest.fixture
def container(qtbot):
    """顶栏宿主（模拟窗口）"""
    w = QWidget()
    qtbot.addWidget(w)
    return w


def test_instantiates_with_system_buttons(qtbot, container):
    """实例化：高 38，Windows 下三系统按钮存在（基类内置）"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    assert CustomTitleBar.HEIGHT == 38
    assert tb.minimumHeight() == 38
    assert tb.minBtn is not None and tb.maxBtn is not None and tb.closeBtn is not None
    assert tb._is_mac is False
    # 品牌区（自 TabPanel 移入）：DriFox + 版本号
    assert tb._brand_title.text() == "DriFox"
    assert tb._brand_version.text() != ""


def test_sidebar_button_transparent_style(qtbot, container):
    """侧栏折叠按钮：透明背景无边框，refresh_style 后样式非空"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.refresh_style()
    qss = tb._sidebar_btn.styleSheet()
    assert qss != ""
    assert "background: transparent" in qss
    assert "border: none" in qss


def test_add_tab_sets_active_and_emits_signal(qtbot, container):
    """add_tab 后首个 tab 自动激活；点击发射 tab_clicked 并切激活态"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("chat", "聊天")
    tb.add_tab("channel", "频道")

    # 首个 tab 自动激活
    assert tb._active_id == "chat"

    received = []
    tb.tab_clicked.connect(received.append)
    tb._tabs["channel"].click()
    assert received == ["channel"]
    assert tb._active_id == "channel"
    assert tb._tabs["channel"].isChecked() is True
    assert tb._tabs["chat"].isChecked() is False


def test_add_tab_with_callback(qtbot, container):
    """add_tab 的 on_click 回调随点击触发"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    hits = []
    tb.add_tab("chat", "聊天", on_click=lambda: hits.append(1))
    tb._tabs["chat"].click()
    assert hits == [1]


def test_remove_tab_reactivates_remaining(qtbot, container):
    """移除激活 tab 后自动激活剩余第一个；移除不存在的 id 不崩"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("a", "A")
    tb.add_tab("b", "B")
    tb.remove_tab("a")
    assert "a" not in tb._tabs
    assert tb._active_id == "b"
    tb.remove_tab("nonexistent")  # 不抛异常
    tb.remove_tab("b")
    assert tb._active_id is None


def test_refresh_style_applies_qss(qtbot, container):
    """refresh_style 后激活 tab 拥有非空样式表（胶囊高亮）"""
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    tb.add_tab("chat", "聊天")
    tb.refresh_style()
    assert tb._tabs["chat"].styleSheet() != ""


def test_mac_branch_hides_system_buttons(qtbot, container, monkeypatch):
    """mac 分支：隐藏三系统按钮，左区留白 ≥70px"""
    monkeypatch.setattr(sys, "platform", "darwin")
    tb = CustomTitleBar(container)
    qtbot.addWidget(tb)
    assert tb._is_mac is True
    assert tb.minBtn.isHidden() and tb.maxBtn.isHidden() and tb.closeBtn.isHidden()
    m = tb.layout().contentsMargins()
    assert m.left() >= 70
