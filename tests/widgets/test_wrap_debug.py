# 调试 wrap_section 实际行为
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication
import sys

qapp = QApplication.instance() or QApplication(sys.argv)

with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
    from app.widgets.tab_panel import TabPanel
    p = TabPanel()

# 1. 检查 widget 实际 parent
brand_parent = p._brand_widget.parent()
system_parent = p._system_plugin_section.parent()

print(f"brand widget parent: type={type(brand_parent).__name__}, objName={brand_parent.objectName()!r}")
print(f"system widget parent: type={type(system_parent).__name__}, objName={system_parent.objectName()!r}")
print(f"system widget parent class mro: {[c.__name__ for c in type(system_parent).__mro__[:5]]}")

# 2. 检查是否有任何 innerCard 对象
cards = [c for c in p.children() if hasattr(c, "objectName") and c.objectName() == "innerCard"]
print(f"innerCard count via parent children: {len(cards)}")
find_result = p.findChildren(type(p._brand_widget).__bases__[0])
print(f"all widget children: {[type(c).__name__ for c in find_result[:5]]}")

# 3. 检查 install eventFilter 是否生效
print(f"system_plugin_section has eventFilter: {p._system_plugin_section.hasEventFilter(system_parent) if hasattr(p._system_plugin_section, 'hasEventFilter') else 'N/A'}")
