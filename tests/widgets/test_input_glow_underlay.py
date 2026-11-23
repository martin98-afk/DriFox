# -*- coding: utf-8 -*-
"""InputGlowUnderlay 光晕性能观测量契约测试。

背景：`_paint_inner_halo` 每帧要画 42 层描边圆角矩形，原实现每层新建
`QColor` + `QPen`（输入时每次重绘 = 84 个临时对象）。包 A 的两项优化把这两处
都做了短路，本文件锁住行为，防止后续重构悄悄退回去：

1. **QPen 复用**：同 (颜色, alpha) 必须命中同一个 QPen 实例。
2. **set_color 失效**：换色后旧色 pen 不能再被复用（否则光晕停在旧主题色）。
3. **高度变化必然重绘**：胶囊高度任何变化都必须重绘。底层
   `_position_input_glow_underlay` 每拍无条件 setGeometry（尺寸随胶囊高度变），
   resize 自身就会重绘；曾有的 ≤2px 容忍带只是把 `_pill_h` 基准提前挪走，
   绘制次数不变、零收益，已删除。

这些是「可观测的性能契约」，不是像素级视觉断言 —— 光晕外观由人工目视验收，
这里只锁「不该干的活没干」。
"""

import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication

from app.widgets.bottom_input_area import InputGlowUnderlay


def _ensure_qapp():
    """确保 QApplication 可用（与 test_elided_label.py 同款，不建主窗口）。"""
    return QApplication.instance() or QApplication(sys.argv)


def _make_underlay():
    """构造独立 underlay 并设为不显示（WA_DontShowOnScreen，避免真窗口）。"""
    w = InputGlowUnderlay()
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    return w


class _CountingUnderlay(InputGlowUnderlay):
    """paintEvent 是 C++ 虚函数分发，实例属性 monkeypatch 无效，只能用子类计数。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.paint_calls = 0

    def paintEvent(self, event):
        self.paint_calls += 1
        super().paintEvent(event)


def _make_counting_underlay(app):
    """可计数的独立 underlay。必须 show() 才会真正派发 paintEvent；
    WA_DontShowOnScreen 避免弹真窗口。
    """
    w = _CountingUnderlay()
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.resize(460, 220)
    w.show()
    app.processEvents()
    # 让 has_visible_glow() 为真，否则 paintEvent 直接 return（计数仍会 +1，
    # 但为了真正跑到绘制路径这里还是点亮环境光）。
    w.set_glow(0, 0, 128, 42)
    app.processEvents()
    w.paint_calls = 0
    return w


@pytest.fixture(autouse=True)
def _clean_pen_cache():
    """每个用例前后清空模块级 pen 缓存，避免跨用例串色。"""
    import app.widgets.bottom_input_area as mod

    mod._HALO_PEN_CACHE.clear()
    yield
    mod._HALO_PEN_CACHE.clear()


class TestHaloPenCache:
    def test_same_rgb_alpha_returns_same_pen(self):
        """同 (rgb, alpha) 必须复用同一个 QPen 实例。"""
        _ensure_qapp()
        import app.widgets.bottom_input_area as mod

        p1 = mod._halo_pen(0x112233, 100)
        p2 = mod._halo_pen(0x112233, 100)

        assert p1 is p2, "同色同 alpha 应命中缓存，不得新建"
        assert len(mod._HALO_PEN_CACHE) == 1, f"缓存条目应只有 1 个，实际 {len(mod._HALO_PEN_CACHE)}"

    def test_different_alpha_creates_distinct_pen(self):
        """alpha 不同是不同缓存键（42 层光晕各有自己的 alpha）。"""
        _ensure_qapp()
        import app.widgets.bottom_input_area as mod

        p1 = mod._halo_pen(0x112233, 100)
        p2 = mod._halo_pen(0x112233, 101)

        assert p1 is not p2, "alpha 不同必须各自一条缓存"
        assert len(mod._HALO_PEN_CACHE) == 2

    def test_pen_properties_preserved(self):
        """缓存的 pen 仍保有 1px / cosmetic / RoundJoin 三个绘制属性。"""
        _ensure_qapp()
        import app.widgets.bottom_input_area as mod

        pen = mod._halo_pen(0x445566, 128)
        assert pen.width() == 1
        assert pen.isCosmetic() is True
        assert pen.joinStyle() == Qt.RoundJoin

    def test_set_color_clears_cache(self):
        """换色必须清缓存：否则旧色 pen 被复用，光晕停在上一套主题色。"""
        _ensure_qapp()
        import app.widgets.bottom_input_area as mod

        w = _make_underlay()
        mod._halo_pen(0x112233, 100)
        mod._halo_pen(0x112233, 101)
        assert len(mod._HALO_PEN_CACHE) == 2, "前置条件：缓存已有条目"

        w.set_color(QColor(0xFF0000))

        assert len(mod._HALO_PEN_CACHE) == 0, "set_color 必须清空 pen 缓存"

    def test_set_color_same_color_keeps_cache(self):
        """同色 set_color 是等值短路，不应白清缓存（避免无谓重算）。"""
        _ensure_qapp()
        import app.widgets.bottom_input_area as mod

        w = _make_underlay()
        original = QColor(201, 168, 92)  # 与 __init__ 默认色一致
        w.set_color(original)  # 同色 → 提前 return

        mod._halo_pen(0x112233, 100)
        before = len(mod._HALO_PEN_CACHE)
        w.set_color(QColor(original))  # 再次同色

        assert len(mod._HALO_PEN_CACHE) == before, "同色 set_color 不应清缓存"


class TestPillGeometryRepaint:
    def test_identical_geometry_is_noop(self):
        """全等判重：完全相同的几何不记账变化、不重绘。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(0, 0, 100, 48, 16)
        w.set_pill_geometry(0, 0, 100, 48, 16)
        assert (w._pill_x, w._pill_y, w._pill_w, w._pill_h, w._radius) == (0, 0, 100, 48, 16)

    @pytest.mark.parametrize("delta", [1, -1, 2, -2, 3, -3, 10, -20])
    def test_any_height_change_repaints(self, delta):
        """高度任何变化都必须重绘（不再有 ≤2px 容忍带）。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(10, 20, 300, 48, 16)
        calls = []
        w.update = lambda *a, **k: calls.append(1)

        w.set_pill_geometry(10, 20, 300, 48 + delta, 16)

        assert w._pill_h == 48 + delta
        assert calls, f"高度变化 {delta}px 必须重绘"

    def test_width_change_repaints(self):
        """宽度变化必须重绘（横向形变影响光晕轮廓）。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(10, 20, 300, 48, 16)
        calls = []
        w.update = lambda *a, **k: calls.append(1)

        w.set_pill_geometry(10, 20, 301, 49, 16)

        assert w._pill_w == 301
        assert calls, "宽度变了必须重绘"

    def test_position_change_repaints(self):
        """x/y 变化必须重绘（光晕跟着输入框移动）。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(10, 20, 300, 48, 16)
        calls = []
        w.update = lambda *a, **k: calls.append(1)

        w.set_pill_geometry(11, 20, 300, 48, 16)

        assert calls, "x 变了必须重绘"

    def test_radius_change_repaints(self):
        """圆角变化必须重绘。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(10, 20, 300, 48, 16)
        calls = []
        w.update = lambda *a, **k: calls.append(1)

        w.set_pill_geometry(10, 20, 300, 48, 20)

        assert calls, "radius 变了必须重绘"

    def test_growth_from_zero_height_repaints(self):
        """从 0 高起步必须重绘：否则首个可见帧会被吞掉。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(10, 20, 300, 0, 16)
        calls = []
        w.update = lambda *a, **k: calls.append(1)

        w.set_pill_geometry(10, 20, 300, 2, 16)  # 高度从 0 变到 2

        assert calls, "从 0 高起步必须重绘，否则光晕首帧不出现"
        assert w._pill_h == 2

    def test_height_changes_always_repaint(self):
        """48→50→52→55 逐轮驱动：每一轮都要真的重绘一次（共 4 次）。"""
        app = _ensure_qapp()
        w = _make_counting_underlay(app)

        for h in (48, 50, 52, 55):
            w.set_pill_geometry(10, 20, 300, h, 16)
            w.setGeometry(0, 0, 460, h + 160)
            app.processEvents()
            assert w._pill_h == h, f"_pill_h 必须如实等于目标高度 {h}"

        assert w.paint_calls == 4, f"4 轮高度变化应绘制 4 次，实际 {w.paint_calls}"

    def test_twenty_consecutive_plus_two_repaints(self):
        """连续 20 轮 +2px：每轮都重绘，棘轮不应再把基准提前挪走。"""
        app = _ensure_qapp()
        w = _make_counting_underlay(app)

        for i in range(20):
            h = 48 + 2 * (i + 1)
            w.set_pill_geometry(10, 20, 300, h, 16)
            w.setGeometry(0, 0, 460, h + 160)
            app.processEvents()

        assert w.paint_calls == 20, f"20 轮 +2px 应绘制 20 次，实际 {w.paint_calls}"

    def test_twenty_consecutive_one_px_jitter_repaints(self):
        """连续 20 轮 1px 抖动：也是 20 次（证明不再假装拦住抖动）。"""
        app = _ensure_qapp()
        w = _make_counting_underlay(app)

        for i in range(20):
            h = 49 if i % 2 == 0 else 48
            w.set_pill_geometry(10, 20, 300, h, 16)
            w.setGeometry(0, 0, 460, h + 160)
            app.processEvents()

        assert w.paint_calls == 20, f"20 轮 1px 抖动应绘制 20 次，实际 {w.paint_calls}"
