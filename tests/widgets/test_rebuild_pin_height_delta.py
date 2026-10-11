# -*- coding: utf-8 -*-
"""回归：T42 占位重建的「起步高度」不得污染锚定增量

## 背景

批次被原位重建时（`_render_message_to_card` 的 `anchor_layout_index` 路径），
卡片被 `pin_layout_height(H)` 钉在占位高度，而**新建 viewer 仍停在最小高度 40**
（固定高度的卡片不会把 viewer 拉伸）。此时 `_apply_viewer_height` 若按
`height - viewer.height()` 记账，上报的增量是「viewer 从 40 涨到 HF」，比卡片
**实际**变化 `(HF + K) - H`（K = 页脚/内边距/身份行，实测 144）大一个数量级。

外层 `_on_message_card_height_changed` 照它 `value += delta`（并把滚动上界也抬
`max + delta`）→ 视口被凭空拽走数百 px = 「滚动时动态加载卡片后滚轮到处乱飞」。

## 修复

卸载侧量出 K 并随占位传递（``_batch_placeholder_overhead``），重建时
`pin_layout_height(H, content_overhead=K)`；`_apply_viewer_height` 在 pin 期间
改用 `(HF + K) - H`。本文件锁定该语义。
"""

from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from app.main_widget import OpenAIChatToolWindow
from app.widgets.message_card import MessageCard

H = 800  # 卸载时的实高（占位高度）
VIEW_START = 5000  # 视口顶部（内容坐标）：卡片整体在其上方 → 走锚定补偿分支
VIEWER_MIN = 40  # 新建 viewer 的起步高度（MessageCard 的最小高度）


def _make_viewer_substitute(qapp, container, lay) -> tuple:
    """造一张「viewer 已就位」的 assistant 卡 + 轻量 viewer 替身。

    替身用 **fixed-height** 的 QWidget：真实 CodeWebViewer 未渲染时恒为最小高度
    40，且不会被固定高度的卡片拉伸 —— 这正是污染的来源，替身必须保住这个特性。
    用替身而非真 WebEngine，是为了让本文件不依赖 WebEngine / 共享 profile。
    """
    card = MessageCard(role="assistant", parent=container)
    lay.insertWidget(0, card)  # 不进父布局的话卡片高度恒为默认 30，K 会量成负数
    holder = QWidget(card)
    holder.setFixedHeight(VIEWER_MIN)
    card.viewer = holder
    # 直接挂进卡片主布局：未 ensure_rendered 的卡没有把气泡装进布局链，
    # 挂到气泡上会让替身完全不参与卡片高度。
    card_lay = card.layout()
    assert card_lay is not None, "卡片必须有主布局"
    card_lay.addWidget(holder)
    container.show()
    qapp.processEvents()
    return card, holder


@pytest.fixture()
def _card(qapp):
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addStretch(1)  # 否则 QVBoxLayout 会把唯一子项垂直居中
    container.resize(500, 6000)
    card, holder = _make_viewer_substitute(qapp, container, lay)
    yield container, card, holder
    card.setParent(None)
    container.setParent(None)


def _overhead(card) -> int:
    """卸载侧口径的 K：卡片自然高度 - viewer 高度。"""
    return card.height() - card.viewer.height()


def test_pinned_first_report_uses_card_delta(_card, qapp):
    """pin 期间首报：增量必须等于**卡片**的真实高度变化，不是 viewer 的"""
    _container, card, _holder = _card
    k = _overhead(card)
    card.pin_layout_height(H, content_overhead=k)
    qapp.processEvents()

    before = card.height()
    card._apply_viewer_height(760)
    qapp.processEvents()
    after = card.height()

    assert before == H
    assert abs(card._last_height_delta - (after - before)) <= 2, (
        f"增量 {card._last_height_delta} 与卡片真实变化 {after - before} 不符（K={k}）"
    )


def test_pinned_shrink_case_keeps_sign(_card, qapp):
    """内容比占位矮时增量必须为负（修复前口径恒为正 → 补偿方向也错）"""
    _container, card, _holder = _card
    k = _overhead(card)
    card.pin_layout_height(H, content_overhead=k)
    qapp.processEvents()

    before = card.height()
    card._apply_viewer_height(300)
    qapp.processEvents()

    assert card.height() < before
    assert card._last_height_delta < 0


def test_without_overhead_falls_back_to_viewer_delta(_card, qapp):
    """K 缺失（旧调用方 / 未量出）时回退 viewer 口径，不得抛异常"""
    _container, card, _holder = _card
    card.pin_layout_height(H)  # 无 content_overhead
    qapp.processEvents()

    card._apply_viewer_height(760)
    assert card._last_height_delta == 760 - VIEWER_MIN


def test_pin_released_even_when_height_equals_last_applied(_card, qapp):
    """首报值与起步值相同时也必须解除钉死（否则页脚被拉伸成竖条）"""
    _container, card, _holder = _card
    k = _overhead(card)
    card.pin_layout_height(H, content_overhead=k)
    qapp.processEvents()
    assert card._layout_height_pinned is True

    card._apply_viewer_height(VIEWER_MIN)  # == _last_applied_viewer_height
    assert card._layout_height_pinned is False


# ── 端到端：外层锚定补偿只能按卡片真实变化挪视口 ──────────────────


class _Anchor(QObject):
    """承载 `_on_message_card_height_changed` 的轻量 QObject 载体。"""

    _on_message_card_height_changed = OpenAIChatToolWindow._on_message_card_height_changed
    _should_follow_bottom = OpenAIChatToolWindow._should_follow_bottom
    _is_view_at_bottom = OpenAIChatToolWindow._is_view_at_bottom
    _programmatic_scroll = OpenAIChatToolWindow._programmatic_scroll
    _sync_scroll_maximum = lambda self: 20000  # noqa: E731


def test_anchor_compensation_matches_real_change(_card, qapp):
    """重建批次在视口上方 → 补偿量 == 卡片真实高度变化（修复前是 720 vs 104）"""
    container, card, _holder = _card
    k = _overhead(card)
    card.pin_layout_height(H, content_overhead=k)
    qapp.processEvents()

    bar = MagicMock()
    bar.value.return_value = VIEW_START
    bar.maximum.return_value = 20000
    area = MagicMock()
    area.widget.return_value = container
    area.verticalScrollBar.return_value = bar

    win = _Anchor()
    win.chat_scroll_area = area
    win._programmatic_scroll_depth = 0
    win._user_intentionally_away_from_bottom = True  # 用户在列表中段阅读（非跟随态）
    win._is_streaming = False
    win._scroll_max_cache = None
    card.heightChanged.connect(win._on_message_card_height_changed)

    before = card.height()
    card._apply_viewer_height(760)
    qapp.processEvents()
    real_delta = card.height() - before

    assert bar.setValue.call_count == 1
    compensated = bar.setValue.call_args[0][0] - VIEW_START
    assert abs(compensated - real_delta) <= 2, f"视口被挪 {compensated}，卡片实际变化 {real_delta}"
    # 滚动上界只能按同一真实增量抬
    assert bar.setMaximum.call_args[0][0] == 20000 + real_delta
