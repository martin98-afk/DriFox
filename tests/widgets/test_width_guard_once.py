"""宽度漂移守卫「一次性修正」回归测试。

背景（2026-10-23 流式抖动根因）：
- viewer 布局在助手气泡内（MessageBubble 左右边距 12+12 + 卡片 main 4+4），
  布局真实宽度 = 卡片宽 − 32，**不等于卡片宽是常态**。
- 旧守卫判据 abs(viewer.width() − card.width()) > 2 恒真 → 每拍高度上报都把
  viewer 强制 resize 到卡片全宽（右侧溢出气泡被裁）→ 下一拍布局又排回布局宽
  → 每拍互撕，视口 811↔843 振荡，全文反复重排（流式文字抖动 + 右侧文字超出）。
- 修复：同一卡片宽度下只修正一次（_width_guard_card_w 标记），此后布局宽为
  权威不再插手；卡片宽度真变化（sync_width）时标记重置可再修一次。

本测试锁定三个语义：
1. viewer 宽 ≠ 卡片宽时首次命中 → resize 到卡片宽 + 丢弃本拍高度；
2. 布局随后把 viewer 拉回布局宽（模拟互撕）→ 守卫**不再**触发第二次 resize；
3. 卡片宽度变化后标记重置 → 允许按新宽度再修一次。
"""
import pytest

from app.widgets.message_card import MessageCard

_QWIDGETSIZE_MAX = 16777215


class _StubViewer:
    """最小 viewer stub：记录 resize 调用次数与参数（参照 test_t42_pin_unpin 模式）。"""

    def __init__(self, width=100, height=40):
        self._w = width
        self._h = height
        self.resize_calls: list[tuple[int, int]] = []

    def width(self):
        return self._w

    def height(self):
        return self._h

    def resize(self, w, h):
        self.resize_calls.append((w, h))
        self._w, self._h = w, h

    def clamp_surface_height(self, h):
        return h

    def sync_body_max_height(self, force=False):
        return 0

    def setFixedHeight(self, h):
        self._h = h

    def page(self):
        return None


@pytest.fixture
def card(qapp):
    c = MessageCard(role="assistant")
    c.resize(843, 100)
    c.viewer = _StubViewer(width=811)
    yield c
    c.deleteLater()


def test_guard_resizes_once_per_card_width(card):
    """首次命中：resize 到卡片宽（高度保持 viewer 现状）并丢弃本拍高度。"""
    card._last_applied_viewer_height = 0
    card._apply_viewer_height(123)
    assert card.viewer.resize_calls == [(843, 40)]
    # 丢弃本拍高度：上报的 123 未落到 viewer
    assert card.viewer.height() == 40


def test_guard_does_not_fight_layout(card):
    """布局把 viewer 拉回布局宽后，守卫不得再次 resize（互撕振荡的根）。"""
    card._last_applied_viewer_height = 0
    card._apply_viewer_height(123)
    assert len(card.viewer.resize_calls) == 1
    # 模拟布局激活：viewer 被排回布局真实宽度 811
    card.viewer._w = 811
    # 连续多拍高度上报（流式期间每拍都过守卫）
    for h in (130, 140, 150):
        card._apply_viewer_height(h)
    assert len(card.viewer.resize_calls) == 1, "守卫与布局每拍互撕 → 视口宽度振荡"


def test_guard_rearms_on_card_width_change(card):
    """卡片宽度真变化 → 标记重置，允许按新宽度再修一次。"""
    card._last_applied_viewer_height = 0
    card._apply_viewer_height(123)
    assert len(card.viewer.resize_calls) == 1
    card.viewer._w = 811  # 布局拉回
    card.resize(863, 100)  # 窗口变化后卡片新宽度
    card._apply_viewer_height(160)
    assert card.viewer.resize_calls == [(843, 40), (863, 40)]


def test_guard_skips_when_widths_match(card):
    """viewer 宽与卡片一致时不触发任何 resize。"""
    card.viewer._w = 843
    card._last_applied_viewer_height = 0
    card._apply_viewer_height(123)
    assert card.viewer.resize_calls == []
