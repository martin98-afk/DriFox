# 直接验证 _InnerCardFrame.eventFilter 是否真的被调用
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QEvent
import sys

qapp = QApplication.instance() or QApplication(sys.argv)

# 1. 创建一个 QWidget 和 _InnerCardFrame，installEventFilter 后手动触发 Show/Hide
from app.widgets.tab_panel import _InnerCardFrame
from PyQt5.QtWidgets import QWidget, QVBoxLayout

target = QWidget()
target.setVisible(False)
card = _InnerCardFrame(target, None)
card._events_received = []
original = _InnerCardFrame.eventFilter

def tracked_eventFilter(self, obj, event):
    self._events_received.append((obj is self._target, event.type()))
    return original(self, obj, event)

_InnerCardFrame.eventFilter = tracked_eventFilter

# 触发 Show
target.show()
QApplication.processEvents()
print(f"after target.show(): events={card._events_received}")
print(f"  card.isVisible={card.isVisible()}, target.isVisible={target.isVisible()}")

# 触发 Hide
target.hide()
QApplication.processEvents()
print(f"after target.hide(): events={card._events_received}")
print(f"  card.isVisible={card.isVisible()}, target.isVisible={target.isVisible()}")
