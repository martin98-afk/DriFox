# -*- coding: utf-8 -*-
"""DriFox UI 驱动库（S1 三层结构的底层：自研驱动，供 S3b 脚手架与 S3c ui-driver 插件消费）。

模块组成：
- :mod:`bus`：主线程投递 + ARM 总闸 + 审计 + 重入锁（所有操作的安全底座）
- :mod:`query`：find(selector) / tree(depth) 控件查询
- :mod:`actions`：click / type / scroll / wait_signal / wait_until
- :mod:`observe`：screenshot / state / memory 观测

用法（工作线程 / 插件侧）::

    from tools.ui_driver import setArmed, find, click, screenshot

    setArmed(True)                       # 总闸：生产环境保持 False
    w = find({"objectName": "send_btn"})
    if w:
        click(w)
        png = screenshot(w)              # PNG bytes

S2 硬约束（必读）：
- 本库**不提供 runJavaScript 封装**：QtWebEngine 5.15.2 下 runJavaScript 稳定触发
  renderer 0xC0000409。渲染断言替代方案 = JS 高度回报信号 + screenshot() 像素多样性。
- 窗口形态禁 showMinimized（最小化态 Chromium 合成随机崩），
  统一用 :func:`place_offscreen`。
"""

from __future__ import annotations

from PyQt5.QtWidgets import QWidget

from .actions import click, scroll, type_text, wait_idle, wait_signal, wait_until
from .bus import DriverNotArmedError, DriverTimeoutError, init_main_caller, invoke, is_armed, setArmed
from .observe import memory, screenshot, state
from .query import find, tree

# A 档标准窗口形态（S2 POC：showMinimized 下 Chromium 合成器随机崩）。
# 用法：w.show(); w.move(*WINDOW_OFFSCREEN_FORM)
WINDOW_OFFSCREEN_FORM = (-32480, 0)


def place_offscreen(w: QWidget) -> None:
    """把窗口摆到 A 档标准屏幕外位置（真实可见，合成器工作，不遮挡桌面）。

    禁止用 showMinimized 替代：最小化态 WebEngine renderer 随机 0xC0000409
    （S2 POC 实测，崩族②同签名）。
    """
    if not w.isWindow():
        raise ValueError(
            "place_offscreen 仅接受顶层窗口（isWindow()==True）；"
            "子 widget 请先 setParent 到窗口或直接传窗口实例"
        )
    w.show()
    w.move(*WINDOW_OFFSCREEN_FORM)


__all__ = [
    "WINDOW_OFFSCREEN_FORM",
    "DriverNotArmedError",
    "DriverTimeoutError",
    "click",
    "find",
    "invoke",
    "is_armed",
    "memory",
    "place_offscreen",
    "screenshot",
    "scroll",
    "setArmed",
    "state",
    "tree",
    "type_text",
    "wait_idle",
    "wait_signal",
    "wait_until",
]
