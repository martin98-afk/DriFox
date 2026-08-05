# 调试 panel show 后的 widget visible 状态
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication
import sys

qapp = QApplication.instance() or QApplication(sys.argv)

with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
    from app.widgets.tab_panel import TabPanel
    p = TabPanel()

print("before panel.show()")
print(f"  _brand_widget isVisible: {p._brand_widget.isVisible()}, visibleTo(parent): {p._brand_widget.isVisibleTo(p._brand_widget.parent())}")
print(f"  _brand_widget parent.visible: {p._brand_widget.parent().isVisible()}")
print(f"  panel visible: {p.isVisible()}")

p.show()
QApplication.processEvents()

print()
print("after panel.show()")
print(f"  _brand_widget isVisible: {p._brand_widget.isVisible()}, visibleTo(parent): {p._brand_widget.isVisibleTo(p._brand_widget.parent())}")
print(f"  _brand_widget parent.visible: {p._brand_widget.parent().isVisible()}")
print(f"  panel visible: {p.isVisible()}")
print(f"  panel size: {p.size()}")

print()
print("--- tracking visibility test ---")
section = p._system_plugin_section
wrap = section.parent()
print(f"  before setVisible(True): wrap.isVisible={wrap.isVisible()}, section.isVisible={section.isVisible()}")
section.setVisible(True)
QApplication.processEvents()
print(f"  after setVisible(True): wrap.isVisible={wrap.isVisible()}, section.isVisible={section.isVisible()}")
section.setVisible(False)
QApplication.processEvents()
print(f"  after setVisible(False): wrap.isVisible={wrap.isVisible()}, section.isVisible={section.isVisible()}")
