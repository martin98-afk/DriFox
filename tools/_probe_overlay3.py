"""探针：hover 预览 → 点击转常驻，检查 overlay 是否留下幽灵窗口。"""

import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget

app = QApplication(sys.argv)

from qfluentwidgets import Theme, setTheme

from app.utils.design_tokens import Colors
from app.widgets.tab_manager_window import TabManagerWindow

TabManagerWindow._instance = None
setTheme(Theme.LIGHT)
Colors.refresh()
tm = TabManagerWindow.create_instance()
tm.resize(1400, 900)
tm.move(40, 40)
tm.show()
for _ in range(20):
    app.processEvents()

ov = tm._wb_overlay
print("init: vis=", ov.isVisible(), "geo=", ov.geometry().getRect())

# 模拟 hover 进入（等 250ms 延时）
tm._wb_preview_ctrl.on_button_hover(True)


def after_enter():
    print("hover-enter: vis=", ov.isVisible(), "geo=", ov.geometry().getRect(),
          "in_preview=", tm._wb_in_preview)
    # 模拟点击转常驻
    tm._wb_promote_on_leave = True
    tm._wb_preview_ctrl.on_clicked()


def after_leave():
    print("after-click: vis=", ov.isVisible(), "geo=", ov.geometry().getRect(),
          "in_preview=", tm._wb_in_preview)
    img = ov.grab().toImage()
    print("  overlay grab=", img.width(), img.height())
    if img.width() > 2:
        img.save(os.path.join(os.environ["TEMP"], "ghost_overlay.png"), "PNG")
    app.quit()


QTimer.singleShot(400, after_enter)
QTimer.singleShot(1400, after_leave)
app.exec_()
tm.close()
app.processEvents()
