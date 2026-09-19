# -*- coding: utf-8 -*-
"""UI 驱动动作：click / type / scroll + 信号等待原语（零 pytest 依赖）。

等待原语（wait_signal / wait_until）用 QEventLoop + QTimer 自实现，
不依赖 pytest-qt，供驱动库与 ui-driver 插件在任意线程复用。
"""

from __future__ import annotations

from typing import Any, Callable, Optional, cast

from PyQt5.QtCore import QEvent, QEventLoop, QTimer, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QAbstractButton, QApplication, QScrollBar, QWidget

from .bus import guard, invoke

# PyQt5 存根未暴露部分枚举成员（pyright reportAttributeAccessIssue 误报），
# 运行时常量存在，以 getattr 取值收窄类型
_KEY_PRESS = cast("QEvent.Type", getattr(QEvent, "KeyPress"))
_KEY_UNKNOWN = cast("Qt.Key", getattr(Qt, "Key_unknown"))
_NO_MODIFIER = cast("Qt.KeyboardModifiers", getattr(Qt, "NoModifier"))


def wait_signal(signal: Any, timeout_ms: int = 10000) -> bool:
    """阻塞等待 Qt 信号触发一次；主线程调用，超时返回 False。

    工作线程请直接用 :meth:`bus.invoke` 包住本调用（QEventLoop 需在目标线程跑）。
    """
    loop = QEventLoop()
    fired = {"ok": False}

    def _on_fire(*_args) -> None:
        fired["ok"] = True
        loop.quit()

    signal.connect(_on_fire)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec_()
    try:
        signal.disconnect(_on_fire)
    except TypeError:
        pass
    return fired["ok"]


def wait_until(predicate: Callable[[], bool], timeout_ms: int = 10000, interval_ms: int = 100) -> bool:
    """轮询 predicate 直到为真或超时（QTimer 轮询，不忙等）；超时返回 False。"""
    if predicate():
        return True
    loop = QEventLoop()
    state = {"ok": False}

    def _tick() -> None:
        if predicate():
            state["ok"] = True
            loop.quit()

    timer = QTimer()
    timer.setInterval(max(10, interval_ms))
    timer.timeout.connect(_tick)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec_()
    timer.stop()
    return state["ok"]


def wait_idle(ms: int = 300) -> None:
    """让主线程事件循环消化一段时间（渲染/防抖落定的驱动侧标准等待）。"""
    loop = QEventLoop()
    QTimer.singleShot(max(0, ms), loop.quit)
    loop.exec_()


def click(w: QWidget) -> bool:
    """点击控件：优先调用控件自身 click()（QAbstractButton 等），无则 False。

    线程安全：非主线程自动投递。
    """
    guard("actions.click")

    def _run() -> bool:
        fn = getattr(w, "click", None)
        if callable(fn):
            fn()
            return True
        return False

    return bool(invoke(_run))


def type_text(w: QWidget, text: str) -> bool:
    """向控件注入键盘文本事件（QKeyEvent 序列，兼容 QLineEdit/QTextEdit 系列）。"""
    guard("actions.type")

    def _run() -> bool:
        w.setFocus()
        for ch in text:
            event = QKeyEvent(_KEY_PRESS, _KEY_UNKNOWN, _NO_MODIFIER, ch)
            QApplication.sendEvent(w, event)
        return True

    return bool(invoke(_run))


def scroll(target: QWidget, value: int) -> bool:
    """把 target（或其子树内首个可见有范围的 QScrollBar）滚动到指定值。

    定位失败返回 False；调用方配合 :func:`wait_idle` 等防抖落定
    （滚动重建链有 500ms 防抖，见 main_widget 虚拟滚动）。
    """
    guard("actions.scroll")

    def _run() -> bool:
        bar = _find_scroll_bar(target)
        if bar is None:
            return False
        bar.setValue(max(0, min(value, bar.maximum())))
        return True

    return bool(invoke(_run))


def _find_scroll_bar(w: QWidget) -> Optional[QScrollBar]:
    if isinstance(w, QScrollBar):
        return w
    for child in w.findChildren(QScrollBar):
        if child.isVisible() and child.maximum() > 0:
            return child
    return None


def is_clickable(w: QWidget) -> bool:
    """控件是否具备 click() 能力（QAbstractButton 语义）。"""
    return isinstance(w, QAbstractButton)


__all__ = [
    "click",
    "is_clickable",
    "scroll",
    "type_text",
    "wait_idle",
    "wait_signal",
    "wait_until",
]
