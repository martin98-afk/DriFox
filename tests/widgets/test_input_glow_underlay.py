# -*- coding: utf-8 -*-
"""InputGlowUnderlay 光晕性能观测量契约测试。

背景：`_paint_inner_halo` 每帧要画 42 层描边圆角矩形，原实现每层新建
`QColor` + `QPen`（输入时每次重绘 = 84 个临时对象）。包 A 的两项优化把这两处
都做了短路，本文件锁住行为，防止后续重构悄悄退回去：

1. **QPen 复用**：同 (颜色, alpha) 必须命中同一个 QPen 实例。
2. **set_color 失效**：换色后旧色 pen 不能再被复用（否则光晕停在旧主题色）。
3. **高度容忍带**：宽/x/y/radius 不变、高度变化 ≤2px 时不重绘（但记账）；
   超出容忍带、或宽度变化时必须重绘。

这些是「可观测的性能契约」，不是像素级视觉断言 —— 光晕外观由人工目视验收，
这里只锁「不该干的活没干」。
"""

import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    """确保 QApplication 可用（与 test_elided_label.py 同款，不建主窗口）。"""
    return QApplication.instance() or QApplication(sys.argv)


def _make_underlay():
    """构造独立 underlay 并设为不显示（WA_DontShowOnScreen，避免真窗口）。"""
    from app.widgets.bottom_input_area import InputGlowUnderlay

    w = InputGlowUnderlay()
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
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


class TestPillGeometryTolerance:
    def test_identical_geometry_is_noop(self):
        """全等判重：完全相同的几何不记账变化。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(0, 0, 100, 48, 16)
        w.set_pill_geometry(0, 0, 100, 48, 16)
        assert (w._pill_x, w._pill_y, w._pill_w, w._pill_h, w._radius) == (0, 0, 100, 48, 16)

    @pytest.mark.parametrize("delta", [1, -1, 2, -2])
    def test_height_within_tolerance_is_recorded_not_repainted(self, delta):
        """高度变化 ≤2px 且其余不变：记账但不重绘。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(10, 20, 300, 48, 16)
        calls = []
        w.update = lambda *a, **k: calls.append(1)  # 拦截重绘

        w.set_pill_geometry(10, 20, 300, 48 + delta, 16)

        assert w._pill_h == 48 + delta, "容忍带内也要记账（否则下次判定基准错）"
        assert not calls, f"高度变化 {delta}px 在容忍带内，不应触发重绘"

    @pytest.mark.parametrize("delta", [3, -3, 10, -20])
    def test_height_beyond_tolerance_repaints(self, delta):
        """高度变化 >2px：必须重绘（输入框真撑高时要跟着画）。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(10, 20, 300, 48, 16)
        calls = []
        w.update = lambda *a, **k: calls.append(1)

        w.set_pill_geometry(10, 20, 300, 48 + delta, 16)

        assert w._pill_h == 48 + delta
        assert calls, f"高度变化 {delta}px 超出容忍带，必须重绘"

    def test_width_change_repaints(self):
        """宽度变化即使高度在容忍带内也必须重绘（横向形变影响光晕轮廓）。"""
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
        """从 0 高起步不受容忍带影响：否则首个可见帧会被吞掉。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(10, 20, 300, 0, 16)
        calls = []
        w.update = lambda *a, **k: calls.append(1)

        w.set_pill_geometry(10, 20, 300, 2, 16)  # |2-0|=2 在容忍带内

        assert calls, "从 0 高起步必须重绘，否则光晕首帧不出现"
        assert w._pill_h == 2

    def test_tolerance_accumulates_across_calls(self):
        """连续微抖动逐次记账后，累计超过容忍带即重绘（不会无限吞）。"""
        _ensure_qapp()
        w = _make_underlay()
        w.set_pill_geometry(10, 20, 300, 48, 16)
        calls = []
        w.update = lambda *a, **k: calls.append(1)

        w.set_pill_geometry(10, 20, 300, 50, 16)  # +2 → 记账
        assert not calls
        w.set_pill_geometry(10, 20, 300, 52, 16)  # 相对 50 又是 +2 → 记账
        assert not calls
        w.set_pill_geometry(10, 20, 300, 55, 16)  # 相对 52 是 +3 → 重绘
        assert calls, "累计超过容忍带必须重绘"
        assert w._pill_h == 55
