# -*- coding: utf-8 -*-
"""hover 热路径样式防抖原语

背景：Qt 对 ``setStyleSheet`` 无论内容是否变化都会全量重解析 QSS +
unpolish/polish + relayout。平滑滚动区（qfw SmoothScroll 引擎 60fps 合成
wheel 事件）里，滚动时鼠标下的行持续变化，enterEvent/leaveEvent 若无条件
setStyleSheet 会放大成每帧两次的样式风暴，表现为滚动掉帧。

统一约定：三态样式收敛到一个纯函数（state → 样式串），enter/leave 各调
一次 :func:`style_if_changed`，样式串未变化时零开销跳过。

已接入：ModelItem（model_selector_card.py，内联同款串比较）、
ProjectItem（project_selector_card.py）、_CustomInputCard
（question_floating_widget.py）。
"""

from PyQt5.QtWidgets import QWidget


def style_if_changed(target: QWidget, style_sheet: str) -> bool:
    """样式串与当前不同才应用，避免 hover 热路径重复 setStyleSheet。

    返回是否真正应用（False = 样式串相同，跳过）。
    """
    if target.styleSheet() == style_sheet:
        return False
    target.setStyleSheet(style_sheet)
    return True
