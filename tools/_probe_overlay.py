import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from qfluentwidgets import Theme, setTheme

from app.utils.design_tokens import Colors
from app.widgets.tab_manager_window import TabManagerWindow

TabManagerWindow._instance = None
setTheme(Theme.LIGHT)
Colors.refresh()
tm = TabManagerWindow.create_instance()
tm.resize(1400, 900)
tm.setAttribute(Qt.WA_DontShowOnScreen, True)
tm.show()
for _ in range(15):
    app.processEvents()

ov = tm._wb_overlay
print("oid =", ov.objectName())
print("qss =", ov.styleSheet())
print("geo =", ov.geometry().getRect(), "vis =", ov.isVisible())
print("translucent =", ov.testAttribute(Qt.WA_TranslucentBackground))
print("styledbg =", ov.testAttribute(Qt.WA_StyledBackground))

ov.show()
ov.resize(112, 36)
for _ in range(5):
    app.processEvents()
img = ov.grab().toImage()
print("grab =", img.width(), img.height())
if img.width() > 2:
    c = img.pixelColor(img.width() // 2, img.height() // 2)
    print("center =", c.name(), c.alpha())
img.save(os.path.join(os.environ["TEMP"], "ov_probe.png"), "PNG")

tm.close()
app.processEvents()
