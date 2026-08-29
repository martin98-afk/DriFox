# -*- coding: utf-8 -*-
"""TabManagerWindow 组件测试"""

from types import SimpleNamespace

import pytest
from PyQt5.QtWidgets import QLabel

from app.widgets.tab_manager_window import TabManagerWindow, EmptyStateWidget


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置单例与 TrayManager 引用"""
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


class TestEmptyStateWidget:
    def test_create(self, qtbot):
        w = EmptyStateWidget()
        qtbot.addWidget(w)

    def test_shows_empty_text(self, qtbot):
        """空状态页显示提示文本（无新建按钮——入口在 Tab 面板）"""
        w = EmptyStateWidget()
        qtbot.addWidget(w)
        texts = [lbl.text() for lbl in w.findChildren(QLabel)]
        assert any("没有打开的窗口" in t for t in texts)


class TestTabManagerWindow:
    def test_singleton_create(self, qtbot):
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        assert TabManagerWindow.get_instance() is tm

    def test_singleton_raises_on_second_init(self):
        TabManagerWindow._instance = None
        tm1 = TabManagerWindow.create_instance()
        assert tm1 is not None
        with pytest.raises(RuntimeError, match="单例"):
            TabManagerWindow()

    def test_initial_state(self, qtbot):
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        assert tm.window_count == 0
        assert tm.get_current_window() is None
        # 初始应显示空状态页
        assert tm._content_area.currentWidget() is tm._empty_state

    def test_has_tab_panel(self, qtbot):
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        assert tm._tab_panel is not None
        assert tm._content_area is not None


class TestReplaceTabCloseFromCardInside:
    """卡片内部关闭（closed 信号路径）→ 必须移除 replace tab

    回归：hook/provider/mcp 编辑卡在卡片内关闭（关闭按钮/保存后）时，因 settings
    卡片可见被 120ms 去抖误判为互斥切换，导致 tab 残留。close_replace_card 提供
    明确的"真正关闭"语义，绕过去抖猜测。
    """

    @staticmethod
    def _seed_open(tm, card_ids):
        """向 open 集合灌入卡片并同步标题栏 tab（模拟 settings + 编辑卡互斥共存）"""
        from collections import OrderedDict

        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

        tm._replace_open[GLOBAL_WINDOW_ID] = OrderedDict((cid, cid) for cid in card_ids)
        tm._replace_active[GLOBAL_WINDOW_ID] = card_ids[-1]
        tm._refresh_titlebar_cards()

    def test_card_inside_close_removes_tab_though_settings_visible(self, qtbot):
        """卡片内关闭编辑卡：即使 settings 仍可见，hook_edit tab 也应移除"""
        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        self._seed_open(tm, ["settings", "hook_edit"])
        assert "hook_edit" in tm.titleBar._tabs

        tm.close_replace_card("hook_edit")

        assert "hook_edit" not in tm._replace_open[GLOBAL_WINDOW_ID]
        assert tm._replace_active.get(GLOBAL_WINDOW_ID) is None
        assert "hook_edit" not in tm.titleBar._tabs
        # settings 保留（卡片内关闭编辑卡 → 回到设置面板）
        assert "settings" in tm.titleBar._tabs

    def test_tab_close_click_uses_same_public_entry(self, qtbot):
        """tab × 关闭走同一公共方法（行为一致）"""
        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        self._seed_open(tm, ["settings", "mcp_edit"])

        tm._on_replace_tab_close_clicked("mcp_edit")

        assert "mcp_edit" not in tm._replace_open[GLOBAL_WINDOW_ID]
        assert "mcp_edit" not in tm.titleBar._tabs


class TestTabManagerWindowShowEvent:
    """T3: showEvent 补刷 UI 插件列表（隐藏期间热加载 → 重新显示时刷新）"""

    def test_show_event_refreshes_ui_plugins(self, qtbot):
        """窗口重新显示时必须调用 _tab_panel.refresh_ui_plugins 补刷"""
        from unittest.mock import MagicMock

        from PyQt5.QtGui import QShowEvent

        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        tm._tab_panel.refresh_ui_plugins = MagicMock()
        tm.showEvent(QShowEvent())
        tm._tab_panel.refresh_ui_plugins.assert_called_once()


class TestChatWrapperWheelForward:
    """限宽居中留白区滚轮转发：左右留白无子控件接收 Wheel，

    由 _chat_wrapper 的 eventFilter 转发给当前窗口 chat_scroll_area。
    """

    @staticmethod
    def _make_scroll_area(qtbot, inner_height=2000):
        """构造带内容的滚动区域（内容高于视口，可滚动）"""
        from PyQt5.QtWidgets import QScrollArea, QWidget

        area = QScrollArea()
        inner = QWidget()
        inner.setMinimumHeight(inner_height)
        area.setWidget(inner)
        area.resize(400, 300)
        area.show()
        qtbot.addWidget(area)
        return area

    def test_wheel_forward_to_current_window_chat_area(self, qtbot):
        """滚轮事件应转发到当前窗口的 chat_scroll_area"""
        from PyQt5.QtCore import QPoint, QPointF, Qt
        from PyQt5.QtGui import QWheelEvent

        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        chat_area = self._make_scroll_area(qtbot)
        assert chat_area.verticalScrollBar().value() == 0

        # 构造一个"当前窗口"：仅需带 chat_scroll_area 属性的对象
        win = SimpleNamespace(chat_scroll_area=chat_area)
        tm.get_current_window = lambda: win

        ev = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        forwarded = tm._forward_wheel_to_scroll_area(ev)
        assert forwarded is True
        assert chat_area.verticalScrollBar().value() > 0

    def test_wheel_forward_no_window_falls_back_to_content(self, qtbot):
        """无当前窗口时回退查找内容区下第一个可见滚动区域"""
        from PyQt5.QtCore import QPoint, QPointF, Qt
        from PyQt5.QtGui import QWheelEvent

        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        tm.show()
        chat_area = self._make_scroll_area(qtbot)

        # 无窗口 + 内容区没有滚动区域 → 返回 False（不消费事件）
        tm._windows = []
        tm._window_to_index = {}
        ev = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        assert tm._forward_wheel_to_scroll_area(ev) is False

        # 内容区挂上滚动区域后回退查找生效
        tm._content_stack.setCurrentIndex(1)  # 覆盖层页面
        from PyQt5.QtWidgets import QVBoxLayout

        layout = tm._global_overlay.layout()
        if layout is None:
            layout = QVBoxLayout(tm._global_overlay)
        layout.addWidget(chat_area)
        qtbot.wait(50)
        assert tm._forward_wheel_to_scroll_area(ev) is True
        assert chat_area.verticalScrollBar().value() > 0

    def test_event_filter_wheel_accepted(self, qtbot):
        """eventFilter 收到 Wheel 且转发成功时返回 True 并 accept"""
        from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
        from PyQt5.QtGui import QWheelEvent

        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        chat_area = self._make_scroll_area(qtbot)
        win = SimpleNamespace(chat_scroll_area=chat_area)
        tm.get_current_window = lambda: win

        ev = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        handled = tm.eventFilter(tm._chat_wrapper, ev)
        assert handled is True
        assert ev.isAccepted() is True
        assert chat_area.verticalScrollBar().value() > 0

    def test_event_filter_other_widget_ignored(self, qtbot):
        """非 wrapper 对象上的 Wheel 不拦截"""
        from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
        from PyQt5.QtGui import QWheelEvent
        from PyQt5.QtWidgets import QLabel

        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        label = QLabel("x")
        qtbot.addWidget(label)
        ev = QWheelEvent(
            QPointF(10, 10),
            QPointF(10, 10),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )
        handled = tm.eventFilter(label, ev)
        assert handled is False


class TestSidebarAutoExpandAfterSqueeze:
    """回归：折叠态拉宽窗口应退出折叠（含手动按钮折叠来源）

    原 bug：手动拖窄自动折叠可退出，但点击折叠按钮(_collapsed_by_squeeze=False)
    后拉宽退不出——窗口拉宽路径(_maybe_auto_expand_after_squeeze,
    growth_required=True) 的 _collapsed_by_squeeze 守卫把手动折叠挡掉。
    """

    def test_button_collapse_then_window_widen_expands(self, qtbot):
        """点击折叠按钮后主动拉宽窗口应退出折叠（修复核心场景）"""
        from unittest.mock import MagicMock, patch

        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        panel = tm._tab_panel

        # 模拟点击折叠按钮：手动折叠，_collapsed_by_squeeze=False
        panel._collapsed = True
        panel._collapsed_by_squeeze = False
        panel._animating = False

        # 折叠时窗口总宽(base)；窗口主动拉宽后 splitter 总宽增至 1300
        # (>= base + _AUTO_EXPAND_GROWTH=200)。用 mock 隔离真实 splitter
        # 尺寸约束，聚焦验证展开判定逻辑。
        tm._squeeze_total_width = 1060
        tm._splitter.sizes = MagicMock(return_value=[60, 1240])

        with patch.object(tm, "_on_sidebar_toggled") as m_toggle:
            tm._maybe_auto_expand_after_squeeze(growth_required=True)
            qtbot.wait(20)

        assert panel._collapsed is False, "点击折叠按钮后拉宽窗口应自动退出折叠"
        m_toggle.assert_called_once_with(False)

    def test_button_collapse_then_overlay_close_keeps_collapsed(self, qtbot):
        """手动折叠 + 关闭卡片 relayout(growth_required=False) 不应被动撑开

        验证修复未破坏"尊重手动折叠意图"：仅窗口主动拉宽(growth_required=True)
        才退出，relayout/关闭卡片恢复仍保持折叠。
        """
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        panel = tm._tab_panel

        # 模拟点击折叠按钮：手动折叠，_collapsed_by_squeeze=False
        panel._collapsed = True
        panel._collapsed_by_squeeze = False
        panel._animating = False
        tm._squeeze_total_width = 1300  # 窗口总宽未变

        # 关闭卡片：只 relayout，窗口总宽不变 → growth_required=False
        tm._maybe_auto_expand_after_squeeze(growth_required=False)

        assert panel._collapsed is True, "关闭卡片恢复不应被动撑开手动折叠"
