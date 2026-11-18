"""QCoreApplication 循环：headless 下替代 QApplication 的事件循环载体。

Qt 主线程只跑引擎既有行为（信号槽 / QThreadPool / QTimer），
无任何 QtWidgets/QtGui 触达。
"""
from PyQt5.QtCore import QCoreApplication

from server import __version__


def create_core_app(argv: list[str]) -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(argv)
    app.setApplicationName("DriFox")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("DriFox")
    return app
