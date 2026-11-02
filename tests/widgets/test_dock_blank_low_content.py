# -*- coding: utf-8 -*-
"""[DOCK-BLANK 修] 折叠态矮内容不再被 max(40) 顶高。

症状（2026-11-02 [dock-blank] 打点）：
    思考折叠收起 + 正文未开 → JS scrollHeight≈11 → 上报 13 被
    _update_height/_apply_viewer_height 的 max(40, ...) 顶成 40 →
    折叠框下恒定 29px 空白（viewer=40 scrollHeight=11）。

修法：两处下限放开（max(0, ...)）；塌缩噪声由 B1 挂起窗（流式）与
±2px 死区（非流式）承接，40 下限只剩制造空白这一个作用。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PyQt5.QtWidgets import QWidget

from app.widgets.message_card import MessageCard


class _StubViewer:
    def __init__(self, height=40):
        self._height = height
        self.set_calls = []

    @property
    def height(self):
        return lambda: self._height

    @property
    def minimumHeight(self):
        return lambda: self._height

    def setFixedHeight(self, h):
        self.set_calls.append(h)
        self._height = h


def _make_card(qapp, viewer_height=40):
    container = QWidget()
    card = MessageCard(role="assistant", model_name="test-model")
    card.setParent(container)
    card.viewer = _StubViewer(viewer_height)
    card._streaming = False
    container.show()
    qapp.processEvents()
    return container, card


class TestDockBlankLowContent:
    """折叠态矮内容（上报 13）应落地 13，不再被顶成 40。"""

    def test_small_report_lands_small_height(self, qapp):
        """非流式大变化路径（|13-40|=27 >= 10）直落 _apply_viewer_height。"""
        container, card = _make_card(qapp, viewer_height=40)
        try:
            card._update_height(13)
            assert card.viewer.set_calls == [13], f"set_calls={card.viewer.set_calls}"
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_apply_viewer_height_no_floor(self, qapp):
        """出口钳放开：_apply_viewer_height(13) 照原值应用。"""
        container, card = _make_card(qapp, viewer_height=40)
        try:
            card._apply_viewer_height(13)
            assert card.viewer.set_calls == [13], f"set_calls={card.viewer.set_calls}"
        finally:
            container.deleteLater()
            qapp.processEvents()
