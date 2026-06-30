# -*- coding: utf-8 -*-
"""CardManager 外部卡片注册测试"""
import os
import sys

# 必须在创建 QApplication 前设置 Qt 属性
from PyQt5.QtCore import Qt
QApplication_ShareOpenGL = Qt.AA_ShareOpenGLContexts
QtCore = Qt


def _ensure_qapp():
    """确保 QApplication 可用（在 conftest 中已经设置，这里 fallback）"""
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


def test_register_external_card():
    """注册外部卡片"""
    from PyQt5.QtWidgets import QWidget
    from app.widgets.cards.card_manager import CardManager, ContainerType

    _ensure_qapp()
    mgr = CardManager.get_instance()

    class FakeCard(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)

    mgr.register_external_card(
        window_id="w1",
        card_id="plug-a:mycard",
        widget_class=FakeCard,
        container=ContainerType.TOP,
    )
    info = mgr.get_external_card("w1", "plug-a:mycard")
    assert info is not None
    assert info["container"] == ContainerType.TOP
    # 清理
    mgr.unregister_external_card("w1", "plug-a:mycard")


def test_unregister_external_card():
    """注销外部卡片"""
    from PyQt5.QtWidgets import QWidget
    from app.widgets.cards.card_manager import CardManager, ContainerType

    _ensure_qapp()
    mgr = CardManager.get_instance()

    class FakeCard(QWidget):
        pass

    mgr.register_external_card(
        window_id="w1", card_id="c1",
        widget_class=FakeCard, container=ContainerType.TOP
    )
    mgr.unregister_external_card("w1", "c1")
    assert mgr.get_external_card("w1", "c1") is None
