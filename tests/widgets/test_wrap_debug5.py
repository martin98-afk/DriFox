# 直接 print 我自己的 eventFilter 是否被调用
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QEvent
import sys

qapp = QApplication.instance() or QApplication(sys.argv)

from app.widgets.tab_panel import TabPanel, _InnerCardFrame

# Monkey-patch class eventFilter 加日志
original_ef = _InnerCardFrame.eventFilter

def logged_ef(self, obj, event):
    if obj is self._target:
        print(f"[EF] target event={event.type()} (Show={QEvent.Show}, Hide={QEvent.Hide}, ShowToParent={QEvent.ShowToParent}) -> card.isVisible={self.isVisible()}")
        if event.type() == QEvent.Show:
            print(f"  -> calling card.show()")
            result = self.show()
            print(f"  -> after show: card.isVisible={self.isVisible()}")
        elif event.type() == QEvent.Hide:
            self.hide()
            print(f"  -> calling card.hide()")
    return original_ef(self, obj, event)

_InnerCardFrame.eventFilter = logged_ef

with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
    p = TabPanel()

section = p._system_plugin_section
wrap = section.parent()
print(f"\n=== setup done. wrap={type(wrap).__name__}, section.isVisible={section.isVisible()}, wrap.isVisible={wrap.isVisible()} ===\n")

print("calling section.setVisible(True)")
section.setVisible(True)
QApplication.processEvents()
print(f"after: section.isVisible={section.isVisible()}, wrap.isVisible={wrap.isVisible()}")

print("\ncalling section.setVisible(False)")
section.setVisible(False)
QApplication.processEvents()
print(f"after: section.isVisible={section.isVisible()}, wrap.isVisible={wrap.isVisible()}")
