# -*- coding: utf-8 -*-
"""SpinBox 编辑区塌缩兼容补丁：修复 Qt6 下数字框内容不可见的问题。

## 症状

设置页所有数字框（qfluentwidgets SpinBox，`setButtonSymbols(NoButtons)`）在
PySide6/Qt6 下看不到数字：内部 lineEdit 被布局成 **1px 宽**
（实测 `QRect(0, 1, 1, 31)`，本体外框 338px）。

## 根因

Qt6 的 `QStyleSheetStyle::subControlRect(SC_SpinBoxEditField)` 在
`ButtonSymbols == NoButtons` 且有任一 qss 规则命中 QSpinBox 时，把编辑区
宽度算成 1px（Qt5 同条件正常，qfw 预留按钮区的 `padding: 0 80px 0 10px`
在 Qt6 下的减法顺序变化所致）。与 qss 内容无关——改 padding 数值、追加
`::up-button` 规则、`setCustomStyleSheet` 均无效；清除 qss 则恢复正常宽度。

## 做法

应用级事件过滤器，在 QAbstractSpinBox 的 `Paint` / `Show` 事件（此时 Qt 的
1px 重排已完成、绘制尚未开始）校正编辑区几何：左右各留 10px、垂直居中。
**只救宽度 < 4px 的塌缩实例**，不干扰任何正常布局；插件里的数字框同进程同
QApplication，一并生效。

安装点：`main.py` 启动时装 wheel_guard 处同装（幂等）。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractSpinBox, QApplication

_PAD_LEFT = 10
_PAD_RIGHT = 10
_COLLAPSED_W = 4  # 编辑区宽度低于此值视为塌缩


class _SpinBoxEditGuard(QObject):
    """应用级过滤器：校正被 Qt6 布局压塌的 SpinBox 编辑区。"""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 回调
        et = event.type()
        if et not in (QEvent.Paint, QEvent.Show):
            return False
        if not isinstance(obj, QAbstractSpinBox):
            return False
        le = obj.lineEdit()
        if le is None or le.width() >= _COLLAPSED_W or obj.width() <= _PAD_LEFT + _PAD_RIGHT:
            return False
        h = obj.height()
        le.setGeometry(
            _PAD_LEFT,
            max(1, (h - le.height()) // 2),
            max(1, obj.width() - _PAD_LEFT - _PAD_RIGHT),
            le.height(),
        )
        return False


def install_guard(app=None) -> bool:
    """安装 SpinBox 编辑区塌缩补丁（幂等）。

    Returns:
        True = 本次已安装；False = 已有实例 / 无 QApplication（CLI 分支）
    """
    app = app or QApplication.instance()
    if app is None:
        return False
    if getattr(app, "_drifox_spinbox_edit_guard", None) is not None:
        return False
    guard = _SpinBoxEditGuard(app)
    app.installEventFilter(guard)
    app._drifox_spinbox_edit_guard = guard  # 挂住引用，防止被 GC 后过滤器失效
    return True
