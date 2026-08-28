# -*- coding: utf-8 -*-
"""pytest 全局 fixtures"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _setup_qt_attributes():
    """qfluentwidgets SingleDirectionScrollArea 需在 QApplication 创建前设置 Qt::AA_ShareOpenGLContexts"""
    from PySide6.QtCore import QCoreApplication, Qt

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)


@pytest.fixture(scope="session")
def qapp(_setup_qt_attributes):
    """PySide6 QApplication 单例（Phase F：UIModule 测试需要 Qt 事件循环）"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
