# -*- coding: utf-8 -*-
"""触控板滚动接管单测（scroll_tuning）。

回归的核心事实（离屏实测，旧实现）：
  `QAbstractSlider::wheelEvent` 只认 angleDelta，按 singleStep×行数 做
  「累积到 120 才走一步」的量化；macOS 触控板的 pixelDelta 被整段丢掉。
  实测 pixelDelta=-10 连发 6 次，滚动条只走 1px（应走 60px）。

覆盖：
- pixelDelta 1:1 跟手，不再量化
- 无 pixelDelta 的细分 angleDelta 按比例折算（保留小数残差）
- 整格鼠标滚轮保持既有距离（step × lines × step_ratio）
- 到顶/到底放行，不吞外层链路
- scroll_wheel_to 走已装接管器（转发点不再绕过 viewport 过滤器）
- 滚动活跃窗口（供动画控件降级重绘）

★ 用假 area / 假 bar，不构造任何 QWidget：向未显示的 QScrollArea 投递 wheel
  并驱动 scrollContentsBy，会在 macOS 离屏环境留下状态，导致后续用例在 qfw
  SVG 图标绘制处段错误（已实测复现）。真实事件派发链路由探针与真机覆盖。
"""

import pytest
from PyQt5.QtCore import QObject, QPoint, QPointF, Qt
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtWidgets import QApplication

from app.utils import scroll_tuning


class _FakeBar:
    """只实现接管器读写的滚动条接口。"""

    def __init__(self, value: int = 2500, maximum: int = 5000) -> None:
        self._value = value
        self._maximum = maximum

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:  # noqa: N802 - Qt 命名
        self._value = max(0, min(self._maximum, int(value)))

    def maximum(self) -> int:
        return self._maximum

    def minimum(self) -> int:
        return 0

    def singleStep(self) -> int:  # noqa: N802 - Qt 命名
        return 1


class _FakeArea:
    """只实现接管器需要的 area 接口。"""

    def __init__(self, vbar: _FakeBar | None = None) -> None:
        self.vbar = vbar or _FakeBar()
        self.hbar = _FakeBar(0, 0)
        self.wheel_event_calls = 0

    def verticalScrollBar(self):  # noqa: N802 - Qt 命名
        return self.vbar

    def horizontalScrollBar(self):  # noqa: N802 - Qt 命名
        return self.hbar

    def wheelEvent(self, _event):  # noqa: N802 - Qt 命名
        self.wheel_event_calls += 1


@pytest.fixture
def flt(qapp):
    """一个挂在真实 QObject 上的接管器（parent 用于 QObject 生命周期）。

    teardown 必须显式 sip.delete：留着 QObject/QTimer 交给 GC，会让后续用例
    在 qfw 的 SVG 图标绘制处段错误（macOS 离屏，已实测稳定复现）。
    """
    from PyQt5 import sip

    holder = QObject()
    filt = scroll_tuning._FastWheelFilter(_FakeArea(), parent=holder)
    yield filt
    if filt._timer is not None:
        filt._timer.stop()
    sip.delete(filt)
    sip.delete(holder)
    qapp.processEvents()


def _wheel(*, angle: int = 0, pixel: int = 0) -> QWheelEvent:
    pos = QPointF(0, 0)
    return QWheelEvent(
        pos,
        pos,
        QPoint(0, pixel),
        QPoint(0, angle),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )


def test_pixel_delta_is_one_to_one(flt):
    """触控板：每个事件的像素位移精确落到滚动条上。"""
    bar = flt._area.vbar
    before = bar.value()

    assert flt.handle_wheel(_wheel(pixel=-10)) is True
    assert bar.value() == before + 10

    assert flt.handle_wheel(_wheel(pixel=-7)) is True
    assert bar.value() == before + 17


def test_trackpad_burst_is_not_quantized(flt):
    """旧实现 6 个细分事件只走 1px；现应走满 60px。"""
    bar = flt._area.vbar
    before = bar.value()

    for _ in range(6):
        assert flt.handle_wheel(_wheel(angle=-15, pixel=-10)) is True

    assert bar.value() - before == 60


def test_fine_angle_without_pixel_is_proportional(flt, monkeypatch):
    """无 pixelDelta 的细分滚轮：按 120 → step×lines(3) 折算，残差不丢。

    关键不是总位移（120 单位最终都是 3px），而是**中途就跟手**：
    旧路径 3 个事件（45 单位）位移为 0，新路径已走 1px。
    """
    monkeypatch.setattr(QApplication, "wheelScrollLines", staticmethod(lambda: 3))
    bar = flt._area.vbar
    before = bar.value()

    for _ in range(3):
        assert flt.handle_wheel(_wheel(angle=-15)) is True
    assert bar.value() - before == 1

    for _ in range(5):
        assert flt.handle_wheel(_wheel(angle=-15)) is True
    assert bar.value() - before == 3


def test_mouse_notch_keeps_existing_distance(flt, monkeypatch):
    """整格鼠标滚轮：距离仍为 step × lines × 1.5。"""
    monkeypatch.setattr(QApplication, "wheelScrollLines", staticmethod(lambda: 3))
    bar = flt._area.vbar
    before = bar.value()

    assert flt.handle_wheel(_wheel(angle=-120)) is True

    assert bar.value() - before == int(1 * 3 * 1.5)


def test_boundary_releases_event(flt):
    """到底/到顶时不 accept，保留外层容器的滚轮链路。"""
    bar = flt._area.vbar

    bar.setValue(bar.maximum())
    ev = _wheel(pixel=-10)
    ev.ignore()  # wheel 事件默认 accepted，放行语义看返回值与 accept 标志是否被改
    assert flt.handle_wheel(ev) is False
    assert bar.value() == bar.maximum()
    assert not ev.isAccepted()

    bar.setValue(bar.minimum())
    ev = _wheel(pixel=10)
    ev.ignore()
    assert flt.handle_wheel(ev) is False
    assert bar.value() == bar.minimum()
    assert not ev.isAccepted()


def test_scroll_wheel_to_uses_installed_filter(flt):
    """转发点走统一入口：装了接管器就走像素直通，不再绕过过滤器。"""
    area = flt._area
    setattr(area, scroll_tuning._INSTALLED_ATTR, flt)
    before = area.vbar.value()

    assert scroll_tuning.scroll_wheel_to(area, _wheel(pixel=-12)) is True
    assert area.vbar.value() == before + 12
    assert area.wheel_event_calls == 0


def test_scroll_wheel_to_falls_back_to_native():
    """未装接管器：回退调用 area.wheelEvent（保持旧语义）。"""
    area = _FakeArea()
    assert scroll_tuning.scroll_wheel_to(area, _wheel(pixel=-12)) is True
    assert area.wheel_event_calls == 1


def test_scroll_activity_window(qapp, monkeypatch):
    """滚动活跃标记：窗口内 True，超窗 False。"""

    class _FakeTime:
        now = 1000.0

        @classmethod
        def monotonic(cls) -> float:
            return cls.now

    monkeypatch.setattr(scroll_tuning, "time", _FakeTime)
    monkeypatch.setattr(scroll_tuning, "_scroll_active_until", 0.0)

    assert scroll_tuning.is_scroll_active() is False
    scroll_tuning.mark_scroll_activity()
    assert scroll_tuning.is_scroll_active() is True
    _FakeTime.now += (scroll_tuning._SCROLL_ACTIVE_MS / 1000.0) + 0.01
    assert scroll_tuning.is_scroll_active() is False
