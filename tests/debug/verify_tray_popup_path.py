# -*- coding: utf-8 -*-
"""验证真实弹出路径 popup() 下字体刷新（真实 Windows 平台）

popup() 是 QSystemTrayIcon 右键弹出 contextMenu 的真实路径。
"""

import sys

sys.path.insert(0, ".")

from PyQt5.QtCore import QPoint, QTimer
from PyQt5.QtWidgets import QApplication

app = QApplication([])

import app.utils.design_tokens as design_tokens
from qfluentwidgets import Action, FluentIcon

from app.widgets.modules.tray_menu import TrayContextMenu


def set_font(family, delta=0):
    design_tokens._cached_font_family = family
    design_tokens._cached_font_size_key = "stub"
    design_tokens._cached_font_size_delta = delta
    design_tokens._cached_font_size_base = 14


set_font("微软雅黑")
menu = TrayContextMenu()
menu.add_hint_action(FluentIcon.UPDATE, "检查更新", "v0.6.1", lambda: None)
menu.addAction(Action(FluentIcon.HOME, "打开主界面", menu))

# 初始弹出
menu.popup(QPoint(100, 100))
app.processEvents()
print("1. popup 后:", menu.view.font().families()[0], menu.view.font().pixelSize(), menu.view.styleSheet())
menu.hide()
app.processEvents()

# 改字体后再 popup（真实用户路径）
set_font("楷体", 6)
menu.popup(QPoint(100, 100))
app.processEvents()
print("2. 改字体后 popup:", menu.view.font().families()[0], menu.view.font().pixelSize(), menu.view.styleSheet())
menu.hide()
app.processEvents()

# 用 exec() 路径（另一条途径）
set_font("Consolas", 0)
QTimer.singleShot(150, menu.close)
menu.exec(QPoint(100, 100))
app.processEvents()
print("3. exec 路径:", menu.view.font().families()[0], menu.view.font().pixelSize(), menu.view.styleSheet())
