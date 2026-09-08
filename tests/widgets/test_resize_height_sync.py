# -*- coding: utf-8 -*-
"""resize 高度同步改造回归测试。

覆盖三处机制级改动：

* **L0**：``CodeWebViewer._do_resize_check`` 在 resize 锁定期**登记补报**而不是
  静默丢弃（旧行为会让外层的 ``update_height()`` 永久失效）；
  ``set_resize_preview_mode(False)`` 保留**真实增量** delta（旧实现清零 → 外层
  锚定补偿全程被跳过）。
* **L1**：``HeightCommitBatch`` —— 多次提交按卡片合并、批量应用、per-card 补偿
  关闭、滚动修正改为**顺序无关**的锚点修正。
* **L2**：``ResizeOrchestrator`` —— 后台页挂起 / 激活补跑；``is_current`` 语义。

运行::

    python -m pytest tests/widgets/test_resize_height_sync.py -v
"""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QPoint, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.widgets.height_commit_batch import HeightCommitBatch  # noqa: E402
from app.widgets.message_card import CodeWebViewer  # noqa: E402
from app.widgets.resize_orchestrator import ResizeOrchestrator  # noqa: E402

# ---------------------------------------------------------------- L0 桩


class _FakeViewer:
    """只实现 _do_resize_check / _on_resize_unlock 用到的三个成员。"""

    def __init__(self):
        self._resize_locked = False
        self._height_report_pending = False
        self.calls = []

    def page(self):
        return self

    def runJavaScript(self, js):
        self.calls.append(js)

    # _on_resize_unlock 会回调 _do_resize_check，桩上挂到真实实现
    def _do_resize_check(self):
        CodeWebViewer._do_resize_check(self)


def _do_resize_check(viewer):
    CodeWebViewer._do_resize_check(viewer)


def _on_resize_unlock(viewer):
    CodeWebViewer._on_resize_unlock(viewer)


class TestL0ResizeLockCompensation:
    def test_locked_report_is_deferred_not_dropped(self):
        """锁定期的上报必须登记补报，解锁后补发一次。

        回归点：旧实现直接 `return`，既不上报也不登记 → 外层
        `set_resize_preview_mode(False) → update_height()` 静默失效，
        卡片高度永远停在旧值。
        """
        v = _FakeViewer()
        v._resize_locked = True
        _do_resize_check(v)
        assert v._height_report_pending is True
        assert v.calls == []  # 锁定期确实不执行

        _on_resize_unlock(v)
        assert v._height_report_pending is False
        assert v.calls == ["reportHeight();"]

    def test_unlock_without_pending_still_reports_once(self):
        """无挂起补报时，解锁只上报一次（不重复）"""
        v = _FakeViewer()
        v._resize_locked = True
        _on_resize_unlock(v)
        assert v._resize_locked is False
        assert v.calls == ["reportHeight();"]

    def test_unlocked_report_clears_pending(self):
        """未锁定状态下上报后应清空 pending，避免解锁时重复上报"""
        v = _FakeViewer()
        v._height_report_pending = True
        _do_resize_check(v)
        assert v._height_report_pending is False
        assert v.calls == ["reportHeight();"]


# ---------------------------------------------------------------- L1 桩


class _StubCard(QWidget):
    heightChanged = pyqtSignal(int)

    def __init__(self, height=100):
        super().__init__()
        self.viewer = QWidget()
        self.viewer.setFixedHeight(height)
        self.setFixedHeight(height)
        self._last_height_delta = 0
        self._last_applied_viewer_height = height
        self._streaming = False
        self.emitted = []
        self.heightChanged.connect(self.emitted.append)


class _Harness:
    def __init__(self, qapp, card_heights, viewport_height=200, scroll_to=0):
        from PyQt5.QtWidgets import QScrollArea

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.container = QWidget()
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.cards = []
        for h in card_heights:
            card = _StubCard(h)
            self.layout.addWidget(card)
            self.cards.append(card)
        self.scroll_area.setWidget(self.container)
        self.scroll_area.resize(400, viewport_height)
        self.scroll_area.show()
        qapp.processEvents()
        if scroll_to:
            self.scroll_area.verticalScrollBar().setValue(scroll_to)
            qapp.processEvents()

    def flush(self, qapp):
        qapp.processEvents()
        qapp.processEvents()

    def follow(self, value):
        self._follow = value
        return self._follow

    def batch(self, follow_bottom: bool = False):
        return HeightCommitBatch(self.scroll_area, self.container, lambda: follow_bottom)


@pytest.fixture
def harness(qapp):
    def _make(heights, viewport_height=200, scroll_to=0):
        return _Harness(qapp, heights, viewport_height, scroll_to)

    return _make


class TestL1HeightCommitBatch:
    def test_merge_duplicate_submissions(self, harness, qapp):
        """同一张卡的多次提交只应用最后一次"""
        h = harness([100])
        b = h.batch()
        b.begin()
        card = h.cards[0]
        b.submit(card, 120)
        b.submit(card, 160)
        b.submit(card, 140)
        h.flush(qapp)
        assert card.viewer.height() == 140
        assert card._last_applied_viewer_height == 140

    def test_batch_applies_all_cards(self, harness, qapp):
        """N 张卡一次 flush 全部生效，且 per-card delta 被清零"""
        h = harness([100, 100, 100, 100])
        b = h.batch()
        b.begin()
        for i, card in enumerate(h.cards):
            card._last_height_delta = 50  # 模拟旧的增量令牌残留
            b.submit(card, 100 + i * 40)
        h.flush(qapp)
        assert [c.viewer.height() for c in h.cards] == [100, 140, 180, 220]
        assert all(c._last_height_delta == 0 for c in h.cards)

    def test_anchor_correction_is_order_independent(self, harness, qapp):
        """顺序无关性：正序 / 倒序提交得到同一个滚动位置。

        这是 L1 的核心不变量 —— 旧链路是「按卡 delta 逐次累加」，
        N 张卡的到达顺序会改变最终滚动位置。
        """
        heights = [120, 160, 140, 200, 130]

        def run(reverse: bool) -> int:
            h = harness(heights, viewport_height=200, scroll_to=150)
            b = h.batch()
            b.begin()
            cards = list(reversed(h.cards)) if reverse else h.cards
            for i, card in enumerate(cards):
                b.submit(card, 300)
            h.flush(qapp)
            return h.scroll_area.verticalScrollBar().value()

        assert run(reverse=False) == run(reverse=True)

    def test_anchor_keeps_top_card_in_place(self, harness, qapp):
        """非跟随态：锚点卡片相对视口的偏移在批量改高度后保持不变"""
        h = harness([100, 400, 400, 400], viewport_height=200, scroll_to=250)
        sb = h.scroll_area.verticalScrollBar()
        anchor = h.cards[1]
        offset_before = anchor.mapTo(h.container, QPoint(0, 0)).y() - sb.value()

        b = h.batch()
        b.begin()
        for card in h.cards:
            b.submit(card, 600)
        h.flush(qapp)

        offset_after = anchor.mapTo(h.container, QPoint(0, 0)).y() - sb.value()
        assert abs(offset_after - offset_before) <= 2

    def test_follow_bottom_goes_to_maximum(self, harness, qapp):
        """跟随态：flush 后滚到新的底部"""
        h = harness([100, 100], viewport_height=200)
        b = h.batch(follow_bottom=True)
        b.begin()
        for card in h.cards:
            b.submit(card, 400)
        h.flush(qapp)
        sb = h.scroll_area.verticalScrollBar()
        assert sb.value() == sb.maximum()

    def test_submit_ignored_when_inactive(self, harness, qapp):
        """未 begin 时提交不生效（避免影响非 resize 期的正常路径）"""
        h = harness([100])
        b = h.batch()
        card = h.cards[0]
        b.submit(card, 500)
        h.flush(qapp)
        assert card.viewer.height() == 100

    def test_end_flushes_remaining(self, harness, qapp):
        """end() 必须先 flush 余量再复位"""
        h = harness([100, 100])
        b = h.batch()
        b.begin()
        b.submit(h.cards[0], 250)
        b.end()
        assert b.active is False
        assert h.cards[0].viewer.height() == 250


# ---------------------------------------------------------------- L2


class _StubPage(QWidget):
    def __init__(self):
        super().__init__()
        self.sync_calls = 0

    def _sync_all_cards_width(self):
        self.sync_calls += 1


class TestL2ResizeOrchestrator:
    def test_singleton(self):
        assert ResizeOrchestrator.get_instance() is ResizeOrchestrator.get_instance()

    def test_is_current_true_without_stack(self, qapp):
        """未注册 stack 时保守返回 True —— 不改变既有行为"""
        o = ResizeOrchestrator()
        assert o.is_current(_StubPage()) is True

    def test_is_current_uses_stack_current_widget(self, qapp):
        """注册 stack 后按 currentWidget 判定（不能用 isVisible 判定）"""
        stack = QStackedWidget()
        p0, p1 = _StubPage(), _StubPage()
        stack.addWidget(p0)
        stack.addWidget(p1)
        stack.setCurrentIndex(0)

        o = ResizeOrchestrator()
        o.register_stack(stack)
        assert o.is_current(p0) is True
        assert o.is_current(p1) is False

        stack.setCurrentIndex(1)
        qapp.processEvents()
        assert o.is_current(p1) is True
        assert o.is_current(p0) is False

    def test_activation_runs_pending_sync(self, qapp):
        """后台页被挂起 → 重新激活时同步补跑一次宽度+高度同步"""
        stack = QStackedWidget()
        p0, p1 = _StubPage(), _StubPage()
        stack.addWidget(p0)
        stack.addWidget(p1)
        stack.setCurrentIndex(0)

        o = ResizeOrchestrator()
        o.register_stack(stack)
        # 模拟 p1 在后台时恢复链被挂起
        assert o.is_current(p1) is False
        o.mark_paused(p1)
        assert p1.sync_calls == 0

        stack.setCurrentIndex(1)
        qapp.processEvents()
        assert p1.sync_calls == 1  # currentChanged 自动补跑

    def test_activation_is_noop_when_clean(self, qapp):
        """没有挂起记录的页激活时不应触发同步（避免无谓开销）"""
        stack = QStackedWidget()
        p0 = _StubPage()
        stack.addWidget(p0)
        o = ResizeOrchestrator()
        o.register_stack(stack)
        stack.setCurrentIndex(0)
        qapp.processEvents()
        assert p0.sync_calls == 0

    def test_page_switch_does_not_rely_on_is_visible(self, qapp):
        """回归守卫：切页瞬间新页 isVisible() 可能为 False。

        实测（QStackedWidget + offscreen）：切页会给新页派发一次完整
        resizeEvent，但此刻 `page.isVisible()` 仍然是 False。任何基于
        isVisible 的裁剪守卫都会把切页同步永久跳过。
        """
        stack = QStackedWidget()
        p0, p1 = _StubPage(), _StubPage()
        stack.addWidget(p0)
        stack.addWidget(p1)
        stack.setCurrentIndex(0)
        stack.show()
        qapp.processEvents()

        o = ResizeOrchestrator()
        o.register_stack(stack)
        o.mark_paused(p1)
        stack.setCurrentIndex(1)
        qapp.processEvents()
        # 无论此刻 isVisible 是 True 还是 False，都必须完成同步
        assert p1.sync_calls == 1
