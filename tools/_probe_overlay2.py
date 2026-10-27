import os
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

from app.widgets.sidebar_hover_preview import HoverPreviewOverlay

win = QApplication.primaryScreen()
from PyQt5.QtWidgets import QWidget

host = QWidget()
host.resize(1200, 800)
host.move(60, 60)
host.show()
for _ in range(5):
    app.processEvents()

ov = HoverPreviewOverlay(host, side="left", titlebar_h=38)
ov.place(360)
for _ in range(5):
    app.processEvents()
print("A place(360): vis=", ov.isVisible(), "geo=", ov.geometry().getRect())

# 模拟滑出到 0 宽度后是否仍可见（slide_out 的终点）
ov.show()
ov._place_at_width(0)
for _ in range(10):
    app.processEvents()
print("B width=0 : vis=", ov.isVisible(), "geo=", ov.geometry().getRect())
img = ov.grab().toImage()
print("B grab=", img.width(), img.height())

# 真实 slide_out 轨迹
ov.place(360)
ov.show()
for _ in range(5):
    app.processEvents()
print("C place again: vis=", ov.isVisible(), "geo=", ov.geometry().getRect())
ov.slide_out(on_done=lambda: print("  slide_out on_done"))
from PyQt5.QtCore import QTimer

timer = QTimer()
timer.timeout.connect(app.quit)
timer.start(800)
app.exec_()
print("D after slide_out: vis=", ov.isVisible(), "geo=", ov.geometry().getRect())
img = ov.grab().toImage()
print("D grab=", img.width(), img.height())
img.save(os.path.join(os.environ["TEMP"], "ov_slideout.png"), "PNG")

host.close()
app.processEvents()
