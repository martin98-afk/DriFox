# -*- coding: utf-8 -*-
"""右侧工作台 hover 悬浮预览的接线与状态机测试

宿主在 offscreen 下真实构造 TabManagerWindow，验证重构后的语义：

- 装配段创建 _wb_overlay / _wb_preview_ctrl，标题栏 hover 信号接到控制器；
- 收起态 hover 进入预览：_workbench_frame 被 reparent 到浮层、控制器进入
  previewing；但**预览≠打开**——is_workbench_visible() 保持 False、不写
  per-tab 显隐记忆（预览是浮层盖在已稳定的对话区上，不动 splitter 布局）；
- 浮层滑入/滑出是异步动画（QVariantAnimation），故 leave/click 后要等动画
  完成（_SLIDE_MS）再看 reparent 回挂结果；
- hover 超时离开 → 浮层滑出 → frame 以 0 宽 hide 回挂 splitter index2、
  回到收起、记忆保持 False；
- 预览态点击 → 浮层滑出 → frame 回挂后走 set_workbench_visible(True) 转
  常驻展开、is_workbench_visible True、记忆 True；
- eventFilter 把浮层自身 HoverEnter/Leave 转给控制器，驱动缓收计时取消。

浮层 isVisible 在 offscreen + native-child 下不可靠，不断言，交由人工实测。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# ★ 临时禁用：WA_NativeWindow 原生浮层与 frameless 主窗口的边缘 resize 冲突
# （常驻原生子窗口 → 四边命中测试全废）。装配已从 __init__ 摘除，本组依赖浮层
# 装配的测试整体 skip，待悬浮预览重构方向确定后重写。
pytest.skip(
    "hover 原生浮层因 frameless resize 死结临时禁用，待重构方向", allow_module_level=True
)

from app.widgets.sidebar_hover_preview import HoverPreviewOverlay
from app.widgets.tab_manager_window import TabManagerWindow

# slide_out 动画 150ms，测试等待留余量
_SLIDE_MS = 280


@pytest.fixture(autouse=True)
def _reset_tab_manager_singleton():
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


@pytest.fixture
def tm(qtbot):
    w = TabManagerWindow.create_instance()
    qtbot.addWidget(w)
    w.set_workbench_visible(False, animate=False)
    return w


def _fake_cur(tm):
    cur = SimpleNamespace(_workbench_visible_memory=False)
    tm.get_current_window = lambda: cur
    return cur


class TestAssembly:
    def test_overlay_and_controller_attached(self, tm):
        from PyQt5.QtCore import Qt

        assert isinstance(tm._wb_overlay, HoverPreviewOverlay)
        assert tm._wb_overlay.parentWidget() is tm
        assert tm._wb_overlay.testAttribute(Qt.WA_Hover)
        assert tm._wb_preview_ctrl is not None
        assert hasattr(tm.titleBar, "_workbench_btn")

    def test_flags_initialized(self, tm):
        assert tm._wb_in_preview is False
        assert tm._wb_promote_on_leave is False


class TestHoverEntersPreview:
    def test_frame_to_overlay_preview_not_open(self, tm):
        frame = tm._workbench_frame
        assert frame.parent() is tm._splitter

        tm.titleBar.workbench_hover_changed.emit(True)

        # frame 同步挂入 overlay（set_content），随后 slide_in 异步展开
        assert frame.parent() is tm._wb_overlay
        assert tm._wb_in_preview is True
        assert tm._wb_preview_ctrl.is_previewing() is True
        # 预览≠打开：is_workbench_visible 保持 False（_wb_visible_target=False）
        assert tm.is_workbench_visible() is False


class TestPreviewDoesNotPolluteMemory:
    def test_memory_untouched_during_preview(self, tm):
        cur = _fake_cur(tm)
        tm.titleBar.workbench_hover_changed.emit(True)
        # 预览不调 set_workbench_visible，记忆保持 False
        assert cur._workbench_visible_memory is False
        assert tm.is_workbench_visible() is False
        # 清理进行中的 slide_in 动画
        tm._wb_overlay._slide.stop()


class TestEventFilterOverlayHover:
    def test_overlay_leave_starts_timer_enter_cancels(self, tm):
        from PyQt5.QtCore import QEvent, QPoint, Qt
        from PyQt5.QtGui import QHoverEvent

        tm.titleBar.workbench_hover_changed.emit(True)
        assert tm._wb_in_preview is True

        p = QPoint(0, 0)
        tm.eventFilter(tm._wb_overlay, QHoverEvent(QEvent.HoverLeave, p, p, Qt.NoModifier))
        assert tm._wb_preview_ctrl._hide_timer.isActive() is True
        tm.eventFilter(tm._wb_overlay, QHoverEvent(QEvent.HoverEnter, p, p, Qt.NoModifier))
        assert tm._wb_preview_ctrl._hide_timer.isActive() is False

        tm._wb_overlay._slide.stop()


class TestHoverTimeoutLeaveCollapses:
    def test_timeout_leave_reparents_back_and_collapses(self, tm, qtbot):
        cur = _fake_cur(tm)
        frame = tm._workbench_frame
        tm.titleBar.workbench_hover_changed.emit(True)
        assert frame.parent() is tm._wb_overlay
        tm._wb_overlay._slide.stop()  # 结束 slide_in，进入稳定预览

        # hover 离开 → 控制器缓收计时 → 触发 leave
        tm.titleBar.workbench_hover_changed.emit(False)
        tm._wb_preview_ctrl._hide_timer.timeout.emit()
        # slide_out 异步，等 _done 回挂
        qtbot.wait(_SLIDE_MS)

        assert tm._wb_in_preview is False
        assert tm._wb_preview_ctrl.is_previewing() is False
        assert frame.parent() is tm._splitter
        assert tm._splitter.widget(2) is frame
        assert tm.is_workbench_visible() is False
        assert cur._workbench_visible_memory is False


class TestClickPromotesToEmbeddedOpen:
    def test_click_while_previewing_promotes(self, tm, qtbot):
        cur = _fake_cur(tm)
        frame = tm._workbench_frame
        tm.titleBar.workbench_hover_changed.emit(True)
        assert tm._wb_in_preview is True
        tm._wb_overlay._slide.stop()  # 结束 slide_in

        # 预览态点标题栏按钮 → 转常驻嵌入展开
        tm.toggle_workbench()
        qtbot.wait(_SLIDE_MS)  # 等 slide_out 的 _done（promote 回挂 + 展开）

        assert tm._wb_in_preview is False
        assert tm.is_workbench_visible() is True  # spec：转常驻打开
        assert frame.parent() is tm._splitter
        assert tm._splitter.widget(2) is frame
        assert cur._workbench_visible_memory is True  # promote 分支正常落账
        tm._wb_anim.stop() if tm._wb_anim else None  # 清理展开动画


class TestNonPreviewToggleUnchanged:
    def test_toggle_outside_preview_flips(self, tm):
        cur = _fake_cur(tm)
        assert tm._wb_in_preview is False
        tm.toggle_workbench()
        assert tm.is_workbench_visible() is True
        assert cur._workbench_visible_memory is True
        tm._wb_anim.stop() if tm._wb_anim else None
        tm.toggle_workbench()
        assert tm.is_workbench_visible() is False
        assert cur._workbench_visible_memory is False
        tm._wb_anim.stop() if tm._wb_anim else None
