# -*- coding: utf-8 -*-
"""ModelItem 别名显示测试。

别名只作用于展示层：显示名可替换，但 clicked 信号仍发真实 id，
tooltip 始终暴露真实 id（显示名与请求名不一致时的唯一线索）。
"""

import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

# QApplication 必须先于 qfluentwidgets 首次使用创建（同其它 widget 测试）
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_APP = QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return _APP


def test_alias_shown_as_name(_qapp):
    from app.widgets.cards.settings.model_selector_card import ModelItem

    item = ModelItem("P", "glm-5", display_alias="GLM-5 主力")
    assert item.name_label.text() == "GLM-5 主力"


def test_real_id_in_tooltip(_qapp):
    """tooltip 必须暴露真实模型 id"""
    from app.widgets.cards.settings.model_selector_card import ModelItem

    item = ModelItem("P", "glm-5", display_alias="GLM-5 主力")
    assert "glm-5" in item.name_label.toolTip()


def test_no_alias_uses_model_id(_qapp):
    from app.widgets.cards.settings.model_selector_card import ModelItem

    item = ModelItem("P", "glm-5")
    assert item.name_label.text() == "glm-5"


def test_blank_alias_falls_back_to_id(_qapp):
    from app.widgets.cards.settings.model_selector_card import ModelItem

    item = ModelItem("P", "glm-5", display_alias="   ")
    assert item.name_label.text() == "glm-5"


def test_click_still_emits_real_id(_qapp):
    """点击仍发出真实模型 id（别名只作用于展示层）"""
    from PyQt5.QtCore import QEvent, QPoint
    from PyQt5.QtGui import QMouseEvent

    from app.widgets.cards.settings.model_selector_card import ModelItem

    item = ModelItem("P", "glm-5", display_alias="GLM-5 主力")
    got = {}
    item.clicked.connect(lambda p, m: got.update({"p": p, "m": m}))
    event = QMouseEvent(
        QEvent.MouseButtonPress,
        QPoint(5, 5),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )
    item.mousePressEvent(event)
    assert got.get("m") == "glm-5", "点击必须发真实 id，否则请求会用别名发给 API"
