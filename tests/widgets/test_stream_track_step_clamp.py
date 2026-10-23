"""流式高度追踪步长限幅回归测试（拉伸黑条修复）。

背景（2026-10-23）：追踪 tick 每拍 diff×0.84 比例逼近，流式 burst 一次涨几百
px 时第一拍就是大跳 → Chromium 新帧未到，Qt6 合成路径把旧帧纹理拉伸填满新
几何 → 文字竖向拉长的黑条一闪（Qt5 无此合成路径，故 PyQt5 版正常）。
修复：每拍步长 clamp 到 _SMOOTH_STEP_PX（160px，2026-09-13 逐帧校准的视觉
阈值），单层逼近器无振荡，把台阶摊成连续生长。
"""
import pytest

from app.widgets.message_card import MessageCard


class _StubViewer:
    def __init__(self, width=843, height=100):
        self._w = width
        self._h = height
        self.fixed_calls: list[int] = []

    def width(self):
        return self._w

    def height(self):
        return self._h

    def minimumHeight(self):
        return 40

    def resize(self, w, h):
        self._w, self._h = w, h

    def clamp_surface_height(self, h):
        return h

    def sync_body_max_height(self, force=False):
        return 0

    def setFixedHeight(self, h):
        self.fixed_calls.append(h)
        self._h = h

    def page(self):
        return None


@pytest.fixture
def card(qapp):
    c = MessageCard(role="assistant")
    c.resize(843, 200)
    c.viewer = _StubViewer(height=100)
    c._streaming = True
    c._target_viewer_height = 0
    yield c
    c.deleteLater()


def test_first_step_clamped_to_visual_threshold(card):
    """大差值首拍不得超过 160px（旧逻辑 0.84×900=756px 一跳 → 拉伸黑条）。"""
    card._target_viewer_height = 1000
    card._stream_height_tick_step()
    assert card.viewer.height() <= 100 + card._SMOOTH_STEP_PX
    assert card.viewer.height() > 100  # 确实朝目标推进了


def test_track_monotonic_no_oscillation(card):
    """连续多拍单调逼近，不回退（fb6aae2e 修的振荡不得回归）。"""
    card._target_viewer_height = 1000
    heights = [card.viewer.height()]
    for _ in range(8):
        card._stream_height_tick_step()
        heights.append(card.viewer.height())
    assert heights == sorted(heights), f"高度回退振荡: {heights}"
    assert all(h <= 1000 for h in heights)


def test_small_step_unaffected(card):
    """小差值（<160px）行为不变：0.84 比例直接落地，丝绸尾音不受影响。"""
    card.viewer._h = 1000
    card._target_viewer_height = 1050  # diff=50 → 0.84×50=42 < 160
    card._stream_height_tick_step()
    assert card.viewer.height() == 1042


def test_nonstream_big_jump_framed(qapp):
    """非流式大变化（终渲染收敛/旧卡重渲）也限幅：一次 ≤160px。

    根因同黑条：_update_height 非流式末段此前一次性落地无上限，虚拟滚动
    重建重渲一次跳数百 px → Qt6 旧帧拉伸。现走 _apply_height_smooth 分帧。
    """
    from app.widgets.message_card import MessageCard as MC

    card = MC(role="assistant")
    card.resize(843, 200)
    card._streaming = False
    card.viewer = _StubViewer(height=500)
    card._last_applied_viewer_height = 500
    try:
        card._update_height(1000)
        # 第一拍：500 + 160 = 660（旧实现一次性 1000 → 拉伸黑条）
        assert card.viewer.height() == 660
        assert card.property("noContainerAnimation") is True
    finally:
        card._cancel_height_smooth()
        card.deleteLater()
