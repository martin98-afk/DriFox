# -*- coding: utf-8 -*-
"""qfluentwidgets ScrollBar 事件类型混淆守卫

背景（2026-11-02 崩溃，同栈两连发）：
    qfluentwidgets 的 ScrollBar（SmoothScrollBar 基类）在 __init__ 里
    ``parent.installEventFilter(self)`` 监听滚动区自身事件，eventFilter
    （v1.11.2 scroll_bar.py:405）：

        if e.type() == QEvent.Resize:
            self._adjustPos(e.size())

    真机在消息卡折叠框上滚动滚轮时崩：

        AttributeError: 'QWheelEvent' object has no attribute 'size'

    即一个 C++ 动态类型为 QWheelEvent、type() 字段却是 Resize 的事件
    通过了判断。实测（tests/utils/test_scrollbar_guard.py 注释）PyQt5/
    sip 对事件包装的选择只看动态类型、不看 type 字段，二者错位只能来自
    底层合成事件（疑似 QtWebEngine/Chromium 输入事件合成路径的类型混
    淆）。Qt 分发自身只信 type 字段、对 Resize 盲转 QResizeEvent*——
    同样的错位对象若走到 Qt 内部是段错误，Python 侧先崩反而留了现场。

修法（防御，不赌根因）：
    monkeypatch ScrollBar.eventFilter：type==Resize 但包装类型不是
    QResizeEvent → 跳过 _adjustPos 直接放行。滚动条位置少重定位一次，
    下一个真 Resize 补回，交互代价为零。SmoothScrollDelegate 的 Wheel
    分支同理：包装不是 QWheelEvent 时不喂 angleDelta。
"""

from PyQt5.QtCore import QEvent
from PyQt5.QtGui import QResizeEvent, QWheelEvent
from qfluentwidgets.components.widgets.scroll_bar import ScrollBar, SmoothScrollDelegate


def _patch_scrollbar_filter(cls) -> None:
    if getattr(cls.eventFilter, "_drifox_guarded", False):
        return

    original = cls.eventFilter

    def _safe_eventFilter(self, obj, e):
        # 类型混淆：type 称 Resize 但包装不是 QResizeEvent → 跳过 e.size()
        # 重定位。原实现对非 Resize 事件的出口是 super().eventFilter ≈ False。
        if (
            obj is self.parent()
            and e.type() == QEvent.Resize
            and not isinstance(e, QResizeEvent)
        ):
            return False
        return original(self, obj, e)

    _safe_eventFilter._drifox_guarded = True
    cls.eventFilter = _safe_eventFilter


def _patch_delegate_filter(cls) -> None:
    if getattr(cls.eventFilter, "_drifox_guarded", False):
        return

    original = cls.eventFilter

    def _safe_eventFilter(self, obj, e):
        # 同类混淆打到 Wheel 分支时不喂 angleDelta，原样放行走默认滚动
        if e.type() == QEvent.Wheel and not isinstance(e, QWheelEvent):
            return False
        return original(self, obj, e)

    _safe_eventFilter._drifox_guarded = True
    cls.eventFilter = _safe_eventFilter


def install_guard() -> None:
    """安装 ScrollBar / SmoothScrollDelegate 事件类型守卫（幂等）

    方法查找在调用期进行，补丁对已创建的实例同样生效；在 UI 起来前后
    装都安全，越早越好。
    """
    _patch_scrollbar_filter(ScrollBar)
    _patch_delegate_filter(SmoothScrollDelegate)
