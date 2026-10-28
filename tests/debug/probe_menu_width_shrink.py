# -*- coding: utf-8 -*-
"""诊断2：逐 item 打印 sizeHint，找出宽度卡住的原因"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication

app = QApplication([])

import app.utils.design_tokens as design_tokens
from qfluentwidgets import Action, FluentIcon

from app.widgets.modules.tray_menu import TrayContextMenu


def set_font(family, delta):
    design_tokens._cached_font_family = family
    design_tokens._cached_font_size_key = "stub"
    design_tokens._cached_font_size_delta = delta
    design_tokens._cached_font_size_base = 14


def dump(menu, tag):
    print(f"--- {tag} ---")
    print(f"  view.width={menu.view.width()} menu.width={menu.width()}")
    for i in range(menu.view.count()):
        it = menu.view.item(i)
        print(f"  item[{i}] text={it.text()!r:20s} sizeHint={it.sizeHint().width()}x{it.sizeHint().height()}")


set_font("微软雅黑", 6)
menu = TrayContextMenu()
menu.add_hint_action(FluentIcon.UPDATE, "检查更新", "v0.6.1", lambda: None)
menu.addAction(Action(FluentIcon.HOME, "打开主界面", menu))
menu.aboutToShow.emit()
dump(menu, "大字 20px")

set_font("Consolas", -5)
menu.aboutToShow.emit()
dump(menu, "小字 9px")

# 直接看 _longestShortcutWidth 计算
print("_longestShortcutWidth():", menu._longestShortcutWidth())
print("view.fontMetrics width('检查更新'):", menu.view.fontMetrics().horizontalAdvance("检查更新"))
print("view.fontMetrics width(' v0.6.1'):", menu.view.fontMetrics().horizontalAdvance(" v0.6.1"))
