# -*- coding: utf-8 -*-
"""流式高度通道噪声守卫回归测试（B1 回缩修复）。

症状（2026-10-09 用户报告）：
1. 流式输出中大模型卡片「生长中回缩」——高度偶尔缩一下再长回去。
2. 消息列表卡片「高度跳变」——静态卡片底边忽高忽低。

根因：save/restore、reorganizeContent 等 DOM 事务存在跨帧 scrollHeight 塌缩
窗口（P052 自证），塌缩读数穿透 rAF 合并 + 80ms 防抖后：
- 追踪活跃时 target 被直接改小（无方向守卫）→ tick 朝小值滑 → 肉眼回缩；
- 非追踪流式路径下调 >=40px 立即应用 → 先缩后涨；
- 非流式路径对 ±2px 测量噪声双向立即 snap → 静态振荡。

修复：追踪中上调 retarget / 下调挂起 500ms；流式收拢统一挂起；非流式 2px 死区。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PyQt5.QtWidgets import QWidget

from app.widgets.message_card import MessageCard


class _StubViewer:
    """最小 viewer 桩：高度可控，记录 setFixedHeight 调用。"""

    def __init__(self, height=40):
        self._height = height
        self.set_calls = []

    @property
    def height(self):
        return lambda: self._height

    @property
    def minimumHeight(self):
        return lambda: self._height

    def setFixedHeight(self, h):
        self.set_calls.append(h)
        self._height = h


def _make_streaming_card(qapp, viewer_height=500):
    """构造一张流式态 assistant 卡 + 可控高度 viewer 桩。"""
    container = QWidget()
    card = MessageCard(role="assistant", model_name="test-model")
    card.setParent(container)
    card.viewer = _StubViewer(viewer_height)
    card._streaming = True
    container.show()
    qapp.processEvents()
    return container, card


class TestTrackDirectionGuard:
    """追踪活跃时上报的方向守卫。"""

    def test_growth_retargets_and_cancels_pending(self, qapp):
        container, card = _make_streaming_card(qapp)
        try:
            card._target_viewer_height = 600
            card._stream_height_anim_active = True
            card._pending_shrink_height = 550  # 预置一笔挂起收缩
            card._update_height(700)
            assert card._target_viewer_height == 700
            # 上调 = 内容填平收拢需求 → 取消挂起
            assert card._pending_shrink_height is None
            # viewer 未被直接改写（交给 tick）
            assert card.viewer.set_calls == []
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_shrink_suspends_not_retargets(self, qapp):
        container, card = _make_streaming_card(qapp)
        try:
            card._target_viewer_height = 600
            card._stream_height_anim_active = True
            # 塌缩噪声：600 → 300（大步下调）
            card._update_height(300)
            # target 不被实时改小（tick 继续朝旧目标走）
            assert card._target_viewer_height == 600
            # 进入 500ms 稳定窗挂起，等待恢复上报或落地
            assert card._pending_shrink_height == 300
            assert card.viewer.set_calls == []
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_duplicate_report_ignored(self, qapp):
        container, card = _make_streaming_card(qapp)
        try:
            card._target_viewer_height = 600
            card._stream_height_anim_active = True
            card._update_height(600)
            assert card._target_viewer_height == 600
            assert card._pending_shrink_height is None
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_equal_report_cancels_pending(self, qapp):
        """等值上报 = 内容意愿未变：必须取消挂起的塌缩读数，否则 500ms 后错误落地。"""
        container, card = _make_streaming_card(qapp)
        try:
            card._target_viewer_height = 600
            card._stream_height_anim_active = True
            card._pending_shrink_height = 400  # 此前一笔塌缩挂起
            card._update_height(600)  # 恢复上报 == 意愿值
            assert card._target_viewer_height == 600
            assert card._pending_shrink_height is None
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_finish_track_follows_shrink_report(self, qapp):
        """FINISH 结束态追踪（_streaming=False）：上报是折叠动画真实收敛值，必须实时跟随。

        回归背景：守卫若覆盖结束态，折叠后的收拢上报被挂起，而
        _apply_pending_shrink 因 _streaming=False 丢弃 → tick 收敛在旧
        target → 流式结束后卡片底部大片空白（2026-10-09 真机截图）。
        """
        container, card = _make_streaming_card(qapp, viewer_height=1300)
        try:
            card._streaming = False
            card._target_viewer_height = 1300
            card._stream_height_anim_active = True
            card._update_height(1100)  # 折叠动画完成后的真实值
            assert card._target_viewer_height == 1100
            assert card._pending_shrink_height is None
        finally:
            container.deleteLater()
            qapp.processEvents()


class TestStreamingShrinkSuspends:
    """非追踪流式路径：收拢（含 >=40px 大步）统一挂起。"""

    def test_large_shrink_suspends(self, qapp):
        container, card = _make_streaming_card(qapp, viewer_height=500)
        try:
            card._update_height(300)  # -200px 大步下调
            # 防抖 80ms timer 启动，值暂存
            assert card._debounced_target_height == 300
            card._apply_debounced_height()
            # 不立即应用：进挂起稳定窗
            assert card._pending_shrink_height == 300
            assert card.viewer.set_calls == []
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_growth_cancels_pending_shrink(self, qapp):
        container, card = _make_streaming_card(qapp, viewer_height=500)
        try:
            card._update_height(400)
            card._apply_debounced_height()
            assert card._pending_shrink_height == 400
            # 正文增长填平收拢需求
            card._update_height(560)
            card._apply_debounced_height()
            assert card._pending_shrink_height is None
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_pending_shrink_lands_when_quiet(self, qapp):
        container, card = _make_streaming_card(qapp, viewer_height=500)
        try:
            card._update_height(300)
            card._apply_debounced_height()
            assert card._pending_shrink_height == 300
            # 500ms 稳定窗到期（工具框折叠确认稳定），手动触发落地
            card._apply_pending_shrink()
            # 落地收拢：追踪或 snap，viewer 高度朝 300 收敛
            assert card.viewer.set_calls or card._stream_height_anim_active
        finally:
            container.deleteLater()
            qapp.processEvents()


class TestNonStreamDeadzone:
    """非流式 ±2px 测量噪声死区。"""

    def test_deadzone_absorbs_noise(self, qapp):
        container, card = _make_streaming_card(qapp, viewer_height=500)
        try:
            card._streaming = False
            card._last_applied_viewer_height = 500
            # ±2px 噪声：viewer 高度不被改写
            card._update_height(502)
            assert card.viewer.height() == 500
            card._update_height(498)
            assert card.viewer.height() == 500
            # ≥3px 真实变化仍走原路径（<10px 立即 snap）
            card._update_height(505)
            assert card.viewer.height() == 505
        finally:
            container.deleteLater()
            qapp.processEvents()
