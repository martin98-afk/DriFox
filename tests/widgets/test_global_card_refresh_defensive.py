# -*- coding: utf-8 -*-
"""[T22-A2] 回归：refresh_theme_styles 序列中已销毁卡（RuntimeError）不得中断后续卡刷新。

修复前：hasattr(card, ...) 在 try 外，对已销毁 C++ 对象属性访问抛 RuntimeError
直接炸出循环 → 序列后续健康卡全部漏刷。
"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_APP = QApplication.instance() or QApplication(sys.argv)

from app.widgets.cards.global_card_controller import GlobalCardController  # noqa: E402


class _DeadCard:
    """模拟已销毁 C++ 对象：任何属性访问都抛 RuntimeError。"""

    def __getattr__(self, name):
        raise RuntimeError("wrapped C/C++ object of type XxxCard has been deleted")


class _LiveCard:
    def __init__(self):
        self.refreshed = 0
        self._theme_needs_refresh = False

    def isVisible(self):
        return True

    def refresh_style(self):
        self.refreshed += 1


def _make_controller(cards):
    ctl = GlobalCardController.__new__(GlobalCardController)
    ctl._settings_popup = cards[0]
    ctl._provider_edit_card = cards[1]
    ctl._provider_picker_card = cards[2]
    ctl._hook_edit_card = cards[3]
    ctl._mcp_edit_card = cards[4]
    ctl._diff_viewer_card = cards[5]
    ctl._chart_viewer_card = cards[6]
    ctl._file_undo_card = cards[7]
    ctl._sub_agent_session_card = cards[8]
    return ctl


def test_runtime_error_card_does_not_block_rest():
    """序列 [健康, 已销毁, 健康]：中间卡抛 RuntimeError，后续卡仍被刷新。"""
    live_before, dead, live_after = _LiveCard(), _DeadCard(), _LiveCard()
    ctl = _make_controller([live_before, dead, live_after, None, None, None, None, None, None])
    ctl.refresh_theme_styles()
    assert live_before.refreshed == 1
    assert live_after.refreshed == 1, "已销毁卡抛 RuntimeError 后，序列后续健康卡必须仍被刷新"


def test_all_dead_cards_no_crash():
    """全已销毁序列：整体静默跳过，不向调用方抛错。"""
    ctl = _make_controller([_DeadCard() for _ in range(9)])
    ctl.refresh_theme_styles()  # 不抛即通过
