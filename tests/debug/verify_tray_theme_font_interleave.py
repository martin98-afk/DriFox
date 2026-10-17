# -*- coding: utf-8 -*-
"""边界验证：主题切换 + 字体切换 交织场景

关键问题：
1. 主题切换（框架重设 menu QSS）会不会覆盖我在 view 上设的字体 QSS？
2. 字体切换后再切主题，view 字体 QSS 保持？
3. 连续切换：主题→字体→主题→字体 全部正确？
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication([])

import app.utils.design_tokens as design_tokens
from qfluentwidgets import Action, FluentIcon, Theme, setTheme

from app.widgets.modules.tray_menu import TrayContextMenu


def set_font(family, delta=0):
    design_tokens._cached_font_family = family
    design_tokens._cached_font_size_key = "stub"
    design_tokens._cached_font_size_delta = delta
    design_tokens._cached_font_size_base = 14


def state(menu):
    f = menu.view.font()
    qss = menu.styleSheet()
    theme = "DARK" if "rgb(43, 43, 43)" in qss else ("LIGHT" if "rgb(249, 249, 249)" in qss else "?")
    return f"theme={theme} view_family={f.families()[0]} px={f.pixelSize()} view_qss={menu.view.styleSheet()!r}"


set_font("微软雅黑")
menu = TrayContextMenu()
menu.add_hint_action(FluentIcon.UPDATE, "检查更新", "v0.6.1", lambda: None)
menu.addAction(Action(FluentIcon.HOME, "打开主界面", menu))

setTheme(Theme.LIGHT, lazy=True)
menu.aboutToShow.emit()
menu.show()
app.processEvents()
print("1. 初始 LIGHT:", state(menu))
menu.hide()
app.processEvents()

# 切深色主题（框架重设 menu QSS）
setTheme(Theme.DARK, lazy=True)
print("2. 切 DARK 未弹出:", state(menu))
menu.aboutToShow.emit()  # 真实弹出路径
menu.show()
app.processEvents()
print("3. 弹出后:", state(menu))
menu.hide()
app.processEvents()

# 改字体
set_font("楷体", 4)
menu.aboutToShow.emit()
menu.show()
app.processEvents()
print("4. 改字体弹出:", state(menu))
menu.hide()
app.processEvents()

# 再切主题（框架重设 menu QSS —— 会不会冲掉 view 字体 QSS？）
setTheme(Theme.LIGHT, lazy=True)
menu.aboutToShow.emit()
menu.show()
app.processEvents()
print("5. 再切主题弹出:", state(menu))
menu.hide()
app.processEvents()

# 检查：框架重设后 view 自己的 QSS 是否保持
print("\nmenu QSS 里的 MenuActionListWidget 规则:",
      [ln.strip() for ln in menu.styleSheet().splitlines() if "MenuActionListWidget {" in ln or "MenuActionListWidget{" in ln])
print("view 自己 QSS:", menu.view.styleSheet())
