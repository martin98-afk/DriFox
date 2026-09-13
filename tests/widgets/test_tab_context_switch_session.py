# -*- coding: utf-8 -*-
"""TabPanel 标签页右键菜单「切换会话」回归守卫

覆盖两段契约：
1. ``TabPanel.contextMenuEvent`` —— 菜单项存在、索引回退、信号发射、插件项保留；
2. ``TabManagerWindow._on_tab_switch_session_requested`` —— 跳转标签页 +
   **非 toggle** 地展开历史会话卡 + 按新活跃窗口重刷。

★ 关键回归点：``UIPluginRegistry.toggle_floating_card`` 是 toggle 语义，
历史卡已可见时再调用会把它**关掉**。处理器必须先用 ``CardManager.is_card_visible``
判定，仅在不可见时才调用。
"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtWidgets import QMenu

from app.widgets.tab_panel import TabPanel


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode("list", persist=False)
    qtbot.addWidget(p)
    p.resize(260, 600)
    return p


def _menu_labels(menu: QMenu) -> list:
    return [a.text() for a in menu.actions() if a.text()]


class _FakeMenu:
    """替换 QMenu：记录 addAction 顺序，exec_ 返回指定 action"""

    def __init__(self, pick: str):
        self._pick = pick
        self.actions: list = []
        self._menu = QMenu()

    def addAction(self, text):
        act = MagicMock()
        act.text.return_value = text
        self.actions.append(act)
        return act

    def addSeparator(self):
        return None

    def setStyleSheet(self, *a, **kw):
        return None

    def exec_(self, *a, **kw):
        for act in self.actions:
            if act.text() == self._pick:
                return act
        return None

    def close(self):
        return None


def _trigger_context_menu(panel, pick: str):
    """模拟在标签页 0 上右键并选中 pick 项，返回发出的信号记录"""
    emitted: list = []
    panel.tabSwitchSessionRequested.connect(lambda i: emitted.append(("switch", i)))
    panel.newTabRequested.connect(lambda: emitted.append(("new", None)))
    panel.tabCloseRequested.connect(lambda i: emitted.append(("close", i)))
    panel.tabBranchRequested.connect(lambda i: emitted.append(("branch", i)))

    fake = _FakeMenu(pick)

    class _Ev:
        def globalPos(self):
            from PyQt5.QtCore import QPoint

            return QPoint(5, 5)

    with patch("app.widgets.tab_panel.QMenu", return_value=fake), patch.object(
        panel, "_active_list_container", return_value=panel._list_layout.parentWidget() or panel
    ), patch.object(panel, "childAt", return_value=None):
        panel.contextMenuEvent(_Ev())
    return emitted, fake


def test_switch_session_item_present_when_tab_clicked(panel):
    """点击到标签页时菜单含「切换会话」且置顶（首个可选项）"""
    panel.add_tab("会话A")
    panel.add_tab("会话B")
    _, fake = _trigger_context_menu(panel, "切换会话")
    labels = [a.text() for a in fake.actions]
    assert "切换会话" in labels
    assert labels[0] == "切换会话", "「切换会话」应为主操作置顶"
    assert "新建标签页" in labels


def test_switch_session_emits_clicked_index(panel):
    """选中「切换会话」→ 发 tabSwitchSessionRequested(被右键的索引)"""
    panel.add_tab("会话A")
    panel.add_tab("会话B")
    panel.set_active_index(1)
    # childAt 返回 None → 索引回退到 _active_index
    emitted, _ = _trigger_context_menu(panel, "切换会话")
    assert emitted == [("switch", 1)], emitted


def test_no_switch_session_item_on_blank_area(panel):
    """空白区右键（无标签页）：不出现「切换会话」"""
    _, fake = _trigger_context_menu(panel, "切换会话")
    labels = [a.text() for a in fake.actions]
    assert "切换会话" not in labels


def test_other_items_still_work(panel):
    """原有三项行为不被新项破坏"""
    panel.add_tab("会话A")
    emitted, _ = _trigger_context_menu(panel, "关闭标签页")
    assert emitted == [("close", -1)] or emitted == [("close", 0)], emitted


# ── TabManagerWindow 处理器契约（不实例化真窗口，直接驱动方法） ──


def _make_manager_stub(visible: bool):
    """构造仅含处理器所需属性的 TabManagerWindow 替身

    ★ 用 ``MagicMock`` + ``__getattr__`` 白名单而非 ``Cls.__new__(Cls)``：
    后者未跑 Qt 基类 ``__init__``，处理器里任何 ``hasattr(self, ...)`` /
    ``getattr(self, ...)`` 触发 Qt 描述符都会抛 "super-class __init__() was
    never called"，测试会在与真实逻辑无关的地方失败。
    """
    from app.widgets.tab_manager_window import TabManagerWindow

    tm = MagicMock(spec=TabManagerWindow)
    tm._windows = [MagicMock(), MagicMock()]
    tm._tab_panel = MagicMock()
    tm._window_id = "GLOBAL"
    tm._card_manager = MagicMock()
    tm._card_manager.is_card_visible.return_value = visible
    tm.get_current_window = MagicMock(return_value=tm._windows[1])
    # spec= 下 MagicMock 不会凭空生成 workbench_panel（真实实例上可能没有）
    del tm.workbench_panel
    tm._on_tab_switch_session_requested = TabManagerWindow._on_tab_switch_session_requested.__get__(tm)
    return tm


def test_handler_switches_tab_and_opens_card_when_hidden():
    """卡片不可见 → 跳转标签页 + 调 toggle（等价打开）+ 刷新历史数据"""
    tm = _make_manager_stub(visible=False)
    registry = MagicMock()
    service = MagicMock()
    registry.get_service.return_value = service

    with patch(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance", return_value=registry
    ):
        tm._on_tab_switch_session_requested(1)

    tm._tab_panel.set_active_index.assert_called_once_with(1)
    registry.toggle_floating_card.assert_called_once()
    service.refresh.assert_called_once()
    # 刷新须发生在卡片打开之后
    assert registry.toggle_floating_card.call_args_list[0]


def test_handler_does_not_toggle_when_card_already_visible():
    """★ 卡片已可见 → 绝不调用 toggle（否则会把卡片关掉）"""
    tm = _make_manager_stub(visible=True)
    # 工作台页签形态也视为已打开
    tm.workbench_panel = MagicMock()
    tm.workbench_panel.has_card_tab.return_value = False
    registry = MagicMock()
    registry.get_service.return_value = MagicMock()

    with patch(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance", return_value=registry
    ):
        tm._on_tab_switch_session_requested(1)

    tm._tab_panel.set_active_index.assert_called_once_with(1)
    registry.toggle_floating_card.assert_not_called()
    registry.get_service.return_value.refresh.assert_called_once()


def test_handler_ignores_out_of_range_index():
    """越界索引直接返回（不触碰 UI 与注册表）"""
    tm = _make_manager_stub(visible=False)
    registry = MagicMock()
    with patch(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance", return_value=registry
    ):
        tm._on_tab_switch_session_requested(99)
        tm._on_tab_switch_session_requested(-1)
    tm._tab_panel.set_active_index.assert_not_called()
    registry.toggle_floating_card.assert_not_called()
