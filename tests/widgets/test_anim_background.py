# -*- coding: utf-8 -*-
"""动画背景播放器单测

覆盖路径共享（多区域同素材只解码一次）、帧率 clamp、时钟启停。
动画素材用单帧 PNG 代替：读取语义与动画 WebP 一致（读完重开回到首帧），
避免测试依赖 ffmpeg。
"""

import pytest
from PyQt5.QtGui import QImage

from app.widgets.anim_background import (
    FPS_CAP,
    IDLE_TICK_MS,
    TICK_MS,
    AnimationRegistry,
    AnimationSource,
    BackgroundClock,
    reset_for_tests,
)

class _FakeLayer:
    def __init__(self, visible=True):
        self._visible = visible
        self.frames = []

    def isVisible(self):
        return self._visible

    def window(self):
        return None

    def on_animation_frame(self, image):
        self.frames.append(image)

@pytest.fixture()
def png(tmp_path):
    path = tmp_path / "frame.png"
    img = QImage(32, 18, QImage.Format_ARGB32)
    img.fill(0xFF336699)
    assert img.save(str(path))
    return str(path)

@pytest.fixture(autouse=True)
def _clean(qapp):
    reset_for_tests()
    yield
    reset_for_tests()

class TestAnimationSource:
    def test_loads_first_frame(self, png):
        src = AnimationSource(png, 12)
        assert src.failed is False
        assert src.current is not None
        assert src.current.width() == 32

    def test_fps_clamped(self, png):
        assert AnimationSource(png, 60).fps == FPS_CAP
        assert AnimationSource(png, 0).fps == 1

    def test_missing_file_marked_failed(self, tmp_path):
        src = AnimationSource(str(tmp_path / "nope.webp"), 12)
        assert src.failed is True
        assert src.current is None

    def test_advance_respects_interval(self, png):
        src = AnimationSource(png, 12)
        # 首帧已由构造期读取，1000ms 后首个 advance 必然推进
        assert src.advance(1_000_000) is True
        # 同一毫秒内不应重复推进
        assert src.advance(1_000_000) is False

    def test_loops_after_last_frame(self, png):
        src = AnimationSource(png, 60)
        now = 0
        ok = 0
        for _ in range(5):
            now += 100
            if src.advance(now):
                ok += 1
        assert ok == 5, "读完应重开回到首帧继续播"

class TestRegistrySharing:
    def test_same_path_shares_source(self, png):
        reg = AnimationRegistry()
        a = reg.acquire(png, 12)
        b = reg.acquire(png, 12)
        assert a is b

    def test_prune_drops_unsubscribed(self, png):
        reg = AnimationRegistry()
        src = reg.acquire(png, 12)
        reg.prune()
        assert reg.acquire(png, 12) is not src

class TestClockLifecycle:
    def test_track_starts_timer(self, png):
        clock = BackgroundClock()
        clock.track(AnimationSource(png, 12))
        assert clock._timer.isActive()

    def test_idles_when_no_subscriber(self, png):
        """无订阅者：降频轮询而非停表（停表后无人能重启 → 永久卡帧）"""
        clock = BackgroundClock()
        clock.track(AnimationSource(png, 12))
        clock._tick()
        assert clock._timer.isActive()
        assert clock._timer.interval() == IDLE_TICK_MS

    def test_idles_when_subscriber_hidden(self, png):
        """订阅者隐藏：降频轮询而非停表"""
        clock = BackgroundClock()
        src = AnimationSource(png, 12)
        src.subscribe(_FakeLayer(visible=False))
        clock.track(src)
        clock._tick()
        assert clock._timer.isActive()
        assert clock._timer.interval() == IDLE_TICK_MS

    def test_resumes_after_becoming_visible(self, png):
        """最小化→停跳降频→恢复可见→下一拍回全速并继续推帧（卡死回归）"""
        clock = BackgroundClock()
        src = AnimationSource(png, 60)
        layer = _FakeLayer(visible=False)
        src.subscribe(layer)
        clock.track(src)
        clock._tick()
        assert clock._timer.interval() == IDLE_TICK_MS
        layer._visible = True
        clock._tick()
        assert clock._timer.interval() == TICK_MS
        assert layer.frames, "恢复可见后应继续收到帧"

    def test_track_from_idle_restores_full_speed(self, png):
        """时钟在 idle 轮询中挂入新源：立即回全速，不竛 500ms"""
        clock = BackgroundClock()
        hidden = _FakeLayer(visible=False)
        src_a = AnimationSource(png, 12)
        src_a.subscribe(hidden)
        clock.track(src_a)
        clock._tick()
        assert clock._timer.interval() == IDLE_TICK_MS
        clock.track(AnimationSource(png, 12))
        assert clock._timer.interval() == TICK_MS

    def test_keeps_running_and_pushes_frames(self, png):
        clock = BackgroundClock()
        src = AnimationSource(png, 60)
        layer = _FakeLayer(visible=True)
        src.subscribe(layer)
        clock.track(src)
        clock._tick()
        assert clock._timer.isActive()
        assert clock._timer.interval() == TICK_MS
        assert layer.frames, "可见订阅者应收到帧"

    def test_dead_subscriber_is_dropped(self, png):
        clock = BackgroundClock()
        src = AnimationSource(png, 60)
        layer = _FakeLayer(visible=True)
        src.subscribe(layer)
        clock.track(src)
        del layer
        clock._tick()
        assert src.has_subscribers() is False
