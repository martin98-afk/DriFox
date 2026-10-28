# -*- coding: utf-8 -*-
"""SpinBox 滚轮守卫：数字框不再吃滚轮改值，滚轮交给外层滚动容器。

## 背景

Qt 的 `QAbstractSpinBox` 即使**没有焦点**也响应滚轮：鼠标扫过设置页里的数字框
时滚一下，值就变了；而这种误改通常要等失焦才落盘，用户几乎无感。设置页、卡片
里数字框密度不低（备份间隔、保留份数、审批超时、循环上限…），误改成本高。

## 做法

在 QApplication 级装一个事件过滤器，命中 `QAbstractSpinBox` 的 Wheel 事件就：

1. 不改值（吞掉原事件）；
2. 把滚轮交给最近的 `QAbstractScrollArea`（页面照常滚动，不会「滚不动」）。

改值请用上下箭头、键盘 ↑↓ 或直接输入。有焦点时同样拦截——用户点了输入框后
再滚轮，多数还是想滚页面。

应用级挂一次即可，插件里的 SpinBox 一同生效（同一进程、同一个 QApplication）。
"""

from __future__ import annotations

from loguru import logger

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractScrollArea, QAbstractSpinBox, QApplication


class _SpinBoxWheelGuard(QObject):
    """应用级事件过滤器：SpinBox 的滚轮转给外层滚动区。"""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 回调
        if event.type() != QEvent.Wheel or not isinstance(obj, QAbstractSpinBox):
            return False
        event.ignore()
        self._forward_to_scroll_area(obj, event)
        return True

    @staticmethod
    def _forward_to_scroll_area(widget, event) -> None:
        """把滚轮交给最近的滚动容器；找不到就到此为止（不冒泡、不改值）。"""
        parent = widget.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                try:
                    parent.wheelEvent(event)
                except Exception as e:  # noqa: BLE001 — 滚动失败也不该影响交互
                    logger.debug(f"[WheelGuard] 转交滚轮失败: {e}")
                return
            parent = parent.parentWidget()


def install_guard(app=None) -> bool:
    """安装 SpinBox 滚轮守卫（幂等）。

    Returns:
        True = 本次已安装；False = 已有实例 / 无 QApplication（CLI 分支）
    """
    app = app or QApplication.instance()
    if app is None:
        return False
    if getattr(app, "_drifox_spinbox_wheel_guard", None) is not None:
        return False
    guard = _SpinBoxWheelGuard(app)
    app.installEventFilter(guard)
    app._drifox_spinbox_wheel_guard = guard  # 挂住引用，防止被 GC 后过滤器失效
    return True
