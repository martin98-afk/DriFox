# -*- coding: utf-8 -*-
"""真实平台诊断：托盘菜单 主题/字体 刷新链

不设 QT_QPA_PLATFORM，走真实 Windows 平台。
"""

import sys

sys.path.insert(0, ".")

from PyQt5.QtWidgets import QApplication

app = QApplication([])
print("platform:", app.platformName())

from qfluentwidgets import FluentIcon, Theme, setTheme

from app.widgets.modules.tray_menu import TrayContextMenu

menu = TrayContextMenu()
menu.add_hint_action(FluentIcon.UPDATE, "检查更新", "v0.6.1", lambda: None)


def sig(m):
    qss = m.styleSheet()
    dark_markers = ["rgb(44, 44, 44)", "rgba(255, 255, 255, 0.1)", "rgb(39, 39, 39)"]
    return "DARK" if any(k in qss for k in dark_markers) else "LIGHT"


setTheme(Theme.LIGHT, lazy=False)
app.processEvents()
print("A. 初始 light:", sig(menu))

setTheme(Theme.DARK, lazy=True)
print("B. setTheme(DARK,lazy) 隐藏态:", sig(menu), "dirty:", menu.property("dirty-qss"))
menu.show()
app.processEvents()
print("C. show() 后:", sig(menu))
menu.hide()
app.processEvents()
print("D. hide() 后 dirty:", menu.property("dirty-qss"))

# 模拟真实弹出序列：隐藏 → 切主题 → 弹出
setTheme(Theme.LIGHT, lazy=True)
print("E. 切回 LIGHT 隐藏态:", sig(menu), "dirty:", menu.property("dirty-qss"))
menu.show()
app.processEvents()
print("F. 再次弹出:", sig(menu), "dirty:", menu.property("dirty-qss"))
menu.hide()
app.processEvents()

print("\n=== 字体链 ===")
from qfluentwidgets import setFontFamilies

print("view QSS 构建时写死:", repr(menu.view.styleSheet()))
setFontFamilies(["楷体"])
print("切字体后 view QSS:", repr(menu.view.styleSheet()))
print("切字体后 view font:", menu.view.font().families())
menu.show()
app.processEvents()
print("弹出后 view QSS:", repr(menu.view.styleSheet()))
print("弹出后 item 宽度:", menu.menuActions()[0].property("item").sizeHint().width())
