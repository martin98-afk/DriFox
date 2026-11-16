# -*- coding: utf-8 -*-
"""滚动区滚轮接管：关掉 qfw 平滑动画，保留连续滚轮加速，一次到位。

## 背景

qfw 的 SmoothScroll 每格滚轮要跑 400ms / 60fps 的逐帧 setValue，每帧都把视口
重画一遍。离屏实测服务商编辑卡：每滚动步 61 次 paint、4.6 倍视口面积，而同结构
纯 QScrollArea 只有 7 次 / 0.23 倍。

关平滑（``SmoothMode.NO_SMOOTH``）能把重绘降下来（滚轮期 paint 1791→488），但
qfw 引擎里的两级放大也一并失效：基础 1.5 倍 ``stepRatio``，以及 500ms 窗口内
最多 +100% 的连续滚轮加速。本模块把这两级放大搬到「一次 setValue」上：加速手感
保留，重绘仍是每格一次。

## 用法

    from app.utils.scroll_tuning import install_fast_wheel
    install_fast_wheel(self.scroll_area)   # 幂等，重复调用无副作用

兼容 qfw ``ScrollArea`` / ``SingleDirectionScrollArea`` 与原生 ``QScrollArea``
（原生本来就没有平滑动画，只装接管器）。

## 边界行为

滚到顶/底时返回 False 让事件继续冒泡，不吞掉外层容器（卡片套卡片、WebView 内外
路由、消息卡把滚轮转给聊天区）已有的滚轮链路。
"""

from __future__ import annotations

import math
import time
from collections import deque

from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtWidgets import QAbstractScrollArea, QApplication

try:  # qfw 是硬依赖，这里只是让模块可被单独 import 做自检
    from qfluentwidgets import SmoothMode
except Exception:  # pragma: no cover - 仅降级保护
    SmoothMode = None

# qfw 默认放大系数（common/smooth_scroll.py：stepRatio=1.5、acceleration=1）
_DEFAULT_STEP_RATIO = 1.5
_DEFAULT_ACCEL_WINDOW_MS = 500
_DEFAULT_ACCEL_STAMP_CAP = 15
_WHEEL_NOTCH = 120
_INSTALLED_ATTR = "_drifox_fast_wheel_filter"


class _FastWheelFilter(QObject):
    """viewport 级滚轮过滤器：一次步进 + 连续滚轮加速。"""

    def __init__(self, area: QAbstractScrollArea, parent=None):
        super().__init__(parent or area)
        self._area = area
        self._stamps: deque = deque()
        self._step_ratio = _DEFAULT_STEP_RATIO
        self._window_ms = _DEFAULT_ACCEL_WINDOW_MS
        self._stamp_cap = _DEFAULT_ACCEL_STAMP_CAP

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 回调
        if event.type() != QEvent.Wheel:
            return False
        dy = event.angleDelta().y()
        dx = event.angleDelta().x()
        delta = dy or dx
        if not delta:
            return False
        # 触控板 / 高精度滚轮（非 120 的整数倍）交给 Qt 原生按像素滚
        if abs(delta) % _WHEEL_NOTCH != 0:
            return False

        area = self._area
        try:
            bar = area.verticalScrollBar() if dy else area.horizontalScrollBar()
        except RuntimeError:
            return False  # area 已销毁
        if bar is None or bar.maximum() <= bar.minimum():
            return False
        # 到顶 / 到底放行，保留外层容器的滚轮链路
        if (delta > 0 and bar.value() <= bar.minimum()) or (
            delta < 0 and bar.value() >= bar.maximum()
        ):
            return False

        now = time.monotonic() * 1000.0
        stamps = self._stamps
        stamps.append(now)
        while stamps and now - stamps[0] > self._window_ms:
            stamps.popleft()
        # 与 qfw 同构：窗口内滚轮越密，越接近 2 倍距离
        accel_ratio = min(len(stamps) / self._stamp_cap, 1.0)

        lines = QApplication.wheelScrollLines()
        if lines <= 0:  # -1 = 系统未设，取 Windows 默认 3 行
            lines = 3
        step = max(1, bar.singleStep()) * lines
        notches = abs(delta) / _WHEEL_NOTCH
        distance = step * notches * self._step_ratio * (1.0 + accel_ratio)
        bar.setValue(int(bar.value() - math.copysign(distance, delta)))
        event.accept()
        return True


def install_fast_wheel(
    area: QAbstractScrollArea,
    *,
    step_ratio: float = _DEFAULT_STEP_RATIO,
    accel_window_ms: int = _DEFAULT_ACCEL_WINDOW_MS,
    accel_stamp_cap: int = _DEFAULT_ACCEL_STAMP_CAP,
) -> bool:
    """关闭 qfw 平滑动画并装上「一次步进 + 加速」接管器（幂等）。

    Args:
        area: 任意 QAbstractScrollArea（qfw ScrollArea / SingleDirectionScrollArea / 原生）。
        step_ratio: 单格距离放大系数，1.5 = qfw 默认。
        accel_window_ms: 连续滚轮的统计窗口，窗口内越密越快（qfw 默认 500ms）。
        accel_stamp_cap: 加速饱和所需的窗口内滚轮事件数（qfw 默认 15）。

    Returns:
        True = 本次完成安装；False = 已装过或对象不合法。
    """
    if area is None:
        return False
    if getattr(area, _INSTALLED_ATTR, None) is not None:
        return False

    # ① 关平滑：两种 qfw 滚动区接口不同
    if SmoothMode is not None:
        set_mode = getattr(area, "setSmoothMode", None)  # ScrollArea(mode, orientation)
        smooth = getattr(area, "smoothScroll", None)  # SingleDirectionScrollArea
        smooth_del = getattr(area, "scrollDelagate", None)  # ScrollArea 的 delegate
        try:
            if smooth is not None:
                smooth.setSmoothMode(SmoothMode.NO_SMOOTH)
            elif smooth_del is not None:
                for name in ("verticalSmoothScroll", "horizonSmoothScroll"):
                    engine = getattr(smooth_del, name, None)
                    if engine is not None:
                        engine.setSmoothMode(SmoothMode.NO_SMOOTH)
            elif callable(set_mode):
                set_mode(SmoothMode.NO_SMOOTH, Qt.Vertical)
        except Exception:  # noqa: BLE001 - 平滑关不掉也不能影响滚动可用性
            pass

    # ② 装接管器（viewporthook：事件先到 viewport）
    flt = _FastWheelFilter(area)
    flt._step_ratio = step_ratio
    flt._window_ms = accel_window_ms
    flt._stamp_cap = accel_stamp_cap
    target = area.viewport() if hasattr(area, "viewport") else area
    target.installEventFilter(flt)
    setattr(area, _INSTALLED_ATTR, flt)  # 挂住引用，防 GC 后过滤器失效
    return True
