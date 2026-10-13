# -*- coding: utf-8 -*-
"""端到端验证：托盘菜单字体刷新（模拟真实 app 链路）

步骤：
1. 模拟 main.py：setFontFamilies 同步一次（启动期）
2. 构建菜单（与 TrayManager._build_context_menu 一致）
3. 记录初始字体状态
4. 模拟用户改字体：改 design_tokens 缓存 + invalidate
5. 模拟右键弹出（aboutToShow 触发）
6. 校验 view QSS / font() / item 宽度 / 菜单尺寸全部跟随
7. 模拟改字号再验一遍
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")

from PyQt5.QtWidgets import QApplication

app = QApplication([])

import app.utils.design_tokens as design_tokens
from qfluentwidgets import FluentIcon

from app.widgets.modules.tray_menu import TrayContextMenu


def set_font(family, delta):
    """模拟配置变更：改缓存（等价于 invalidate 后重新读取）"""
    design_tokens._cached_font_family = family
    design_tokens._cached_font_size_key = "stub"
    design_tokens._cached_font_size_delta = delta
    design_tokens._cached_font_size_base = 14


def snapshot(tag, menu):
    f = menu.view.font()
    item = menu.menuActions()[0].property("item")
    hint_action = None
    for a in menu.menuActions():
        if a.property("hintText"):
            hint_action = a
            break
    print(
        f"{tag:26s} view_family={f.families()[0]:<10s} px={f.pixelSize():<3d} "
        f"item_w={item.sizeHint().width():<4d} item_h={item.sizeHint().height():<3d} "
        f"menu={menu.width()}x{menu.height()}"
    )


set_font("微软雅黑", 0)
menu = TrayContextMenu()
menu.add_hint_action(FluentIcon.UPDATE, "检查更新", "v0.6.1", lambda: None)
from qfluentwidgets import Action

a1 = Action(FluentIcon.HOME, "打开主界面", menu)
menu.addAction(a1)
menu.addSeparator()
menu.addAction(Action(FluentIcon.DOCUMENT, "打开日志", menu))

menu.aboutToShow.emit()
snapshot("1. 初始(14px 微软雅黑)", menu)

set_font("楷体", 6)
menu.aboutToShow.emit()
snapshot("2. 换楷体+6px", menu)

set_font("Consolas", -5)
menu.aboutToShow.emit()
snapshot("3. 换Consolas-5px", menu)

# 幂等验证：重复 emit 不变
w_before = menu.menuActions()[0].property("item").sizeHint().width()
menu.aboutToShow.emit()
w_after = menu.menuActions()[0].property("item").sizeHint().width()
print(f"4. 幂等: {w_before} → {w_after} {'OK' if w_before == w_after else 'FAIL'}")

# 模拟真实弹出
menu.show()
app.processEvents()
print(f"5. show() 后菜单可见={menu.isVisible()} size={menu.width()}x{menu.height()}")
menu.hide()
