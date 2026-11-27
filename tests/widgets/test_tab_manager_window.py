# -*- coding: utf-8 -*-
"""TabManagerWindow 组件测试"""

import gc
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtWidgets import QLabel

from app.widgets.tab_manager_window import TabManagerWindow, EmptyStateWidget


# 本文件每个用例各建一个 TabManagerWindow（含全量子 widget），统一登记到此处，
# 由 reset_singleton 的 teardown 集中销毁
_CREATED_WINDOWS = []


def _destroy_created_windows():
    """显式销毁本用例创建的窗口，并泵到 DeferredDelete 落地

    qtbot.addWidget 只调 deleteLater：事件只是投递，processEvents() 不消费
    DeferredDelete，必须 sendPostedEvents(None, QEvent.DeferredDelete)。
    之后还要清掉 Python 侧强引用（_instance / TrayManager / 登记表）并
    gc.collect()，否则 wrapper 留在 gc 里，探针（gc.get_objects）数得到。
    """
    if not _CREATED_WINDOWS:
        return
    for tm in _CREATED_WINDOWS:
        try:
            tm.hide()
            tm.setParent(None)
            tm.deleteLater()
        except RuntimeError:
            continue  # C++ 对象已销毁
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()
    _CREATED_WINDOWS.clear()
    TabManagerWindow._instance = None
    try:
        from app.tray_manager import TrayManager

        TrayManager.get_instance()._tab_manager_window = None
    except Exception:
        pass
    # GlobalCardController 是模块级单例，持有 _tab_manager 强引用：不清它，
    # 被删 window 的 wrapper 仍从单例可达，gc 收不掉（实测残留 1~2 个）
    try:
        from app.widgets.cards import global_card_controller as _gcc

        _gcc._controller = None
    except Exception:
        pass
    gc.collect()
    QCoreApplication.processEvents()


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置单例与 TrayManager 引用

    teardown 里销毁本用例创建的窗口：本 fixture 最先 setup，因而最后
    teardown，此时 qtbot 已释放它对 widget 的引用，gc.collect() 才能真正
    回收 wrapper（放在后 setup 的 fixture 里会因 qtbot 仍持有而收不掉，
    实测 alive 停在 1~3 不归零）。
    """
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    _destroy_created_windows()
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


@pytest.fixture(autouse=True)
def two_phase_construction_complete(monkeypatch):
    """两阶段构造契约适配（T6b）：create_instance 后补建阶段二。

    T6b 后 create_instance 只产出壳期实例（基座+标题栏+空骨架），TabPanel/
    _content_area/_splitter 等由 complete_construction() 补建（生产路径由
    main._show_popup 驱动）。本文件测试均为完整态行为测试，在 fixture 层
    统一包装 create_instance 驱动 complete，模拟主路径；幂等 flag 保证
    兜底路径重复驱动零开销。

    ★ 建出的实例登记到 _CREATED_WINDOWS，由 reset_singleton 的 teardown
    统一销毁（该 fixture 最后 teardown，那时 qtbot 引用已释放）。
    """
    original = TabManagerWindow.create_instance.__func__

    def create_and_complete(cls, parent=None):
        tm = original(cls, parent)
        tm.complete_construction()
        _CREATED_WINDOWS.append(tm)
        return tm

    monkeypatch.setattr(TabManagerWindow, "create_instance", classmethod(create_and_complete))
    yield


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
        assert "hook_edit" not in tm.titleBar._tabs
        # settings 保留（卡片内关闭编辑卡 → 补位回到设置面板，高亮乐观落 settings，
        # 不再经过「对话」中间态）
        assert "settings" in tm.titleBar._tabs
        assert tm._replace_active.get(GLOBAL_WINDOW_ID) == "settings"
        assert tm.titleBar._active_id == "settings"

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

    @staticmethod
    def _wheel_at(x: int, y: int):
        from PyQt5.QtCore import QPoint, QPointF, Qt
        from PyQt5.QtGui import QWheelEvent

        return QWheelEvent(
            QPointF(x, y),
            QPointF(x, y),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,
        )

    @staticmethod
    def _prepare_padded_wrapper(tm, pad: int = 60):
        """让 wrapper 左右各留 pad 空白，返回 (留白点, 内容点)（wrapper 局部坐标）"""
        from PyQt5.QtCore import QPoint
        from PyQt5.QtWidgets import QApplication

        tm._chat_wrapper.resize(400, 300)
        QApplication.processEvents()
        tm._chat_wrapper_layout.setContentsMargins(pad, 0, pad, 0)
        tm._chat_wrapper_layout.activate()
        QApplication.processEvents()
        geo = tm._content_stack.geometry()
        assert geo.left() >= pad, f"内容区未按留白内缩: {geo}"
        padding_pt = QPoint(max(1, geo.left() // 2), 10)
        return padding_pt, geo.center()

    def test_event_filter_wheel_accepted(self, qtbot):
        """留白区 Wheel：转发到 chat_scroll_area 并 accept"""
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        chat_area = self._make_scroll_area(qtbot)
        win = SimpleNamespace(chat_scroll_area=chat_area)
        tm.get_current_window = lambda: win

        padding_pt, content_pt = self._prepare_padded_wrapper(tm)
        assert tm._chat_wrapper.childAt(padding_pt) is None
        assert tm._chat_wrapper.childAt(content_pt) is not None

        ev = self._wheel_at(padding_pt.x(), padding_pt.y())
        handled = tm.eventFilter(tm._chat_wrapper, ev)
        assert handled is True
        assert ev.isAccepted() is True
        assert chat_area.verticalScrollBar().value() > 0

    def test_event_filter_wheel_from_child_not_forwarded(self, qtbot):
        """回归：子控件冒泡上来的 Wheel 不得转发给消息列表

        输入框（或卡片内滚动体）滚到顶/底后未消费的 Wheel 沿父链冒泡到
        _chat_wrapper，此时 pos 落在子控件上；此前被无差别转发给
        chat_scroll_area，表现为输入框滚动带动消息列表一起滚。
        """
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        chat_area = self._make_scroll_area(qtbot)
        win = SimpleNamespace(chat_scroll_area=chat_area)
        tm.get_current_window = lambda: win

        _padding_pt, content_pt = self._prepare_padded_wrapper(tm)
        assert tm._chat_wrapper.childAt(content_pt) is not None

        ev = self._wheel_at(content_pt.x(), content_pt.y())
        handled = tm.eventFilter(tm._chat_wrapper, ev)
        assert handled is False
        assert chat_area.verticalScrollBar().value() == 0

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
