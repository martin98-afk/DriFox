# -*- coding: utf-8 -*-
"""验证：菜单字体刷新的技术前提

1. view 是否注册进 styleSheetManager（决定补刷链是否覆盖它）
2. setStyleSheet 后 view.fontMetrics() 是否立即反映新字体（决定 item 宽度能否重算）
3. _adjustItemText 手动调用是否可行
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")

from PyQt5.QtWidgets import QApplication

app = QApplication([])

from qfluentwidgets import FluentIcon
from qfluentwidgets.common.style_sheet import styleSheetManager

from app.widgets.modules.tray_menu import TrayContextMenu

menu = TrayContextMenu()
menu.add_hint_action(FluentIcon.UPDATE, "检查更新", "v0.6.1", lambda: None)
act = menu.menuActions()[0]
item = act.property("item")

print("1. menu in manager:", menu in styleSheetManager.widgets)
print("1. menu.view in manager:", menu.view in styleSheetManager.widgets)

print("\n2. 改 view QSS 后 fontMetrics 变化？")
w1 = menu.view.fontMetrics().width("检查更新")
menu.view.setStyleSheet("MenuActionListWidget{font: 24px 'Consolas'}")
app.processEvents()
w2 = menu.view.fontMetrics().width("检查更新")
print(f"   14px 宽 {w1} → 24px 宽 {w2}（应显著变大才算立即生效）")
print("   view.font() families:", menu.view.font().families(), "pixel:", menu.view.font().pixelSize())

print("\n3. _adjustItemText 重算 item sizeHint")
before = item.sizeHint()
menu._adjustItemText(item, act)
after = item.sizeHint()
print(f"   before {before.width()}x{before.height()} → after {after.width()}x{after.height()}")

menu.view.adjustSize()
menu.adjustSize()
print("   view size:", menu.view.width(), menu.view.height())
print("   menu size:", menu.width(), menu.height())
