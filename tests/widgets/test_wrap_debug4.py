# 调试：使用 QStackedLayout 风格 + 不要靠 isVisible() 而靠 setVisible 调用
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication, QWidget
import sys

qapp = QApplication.instance() or QApplication(sys.argv)

# 用 panel 模式 + qtbot 真实情况测试
from app.widgets.tab_panel import TabPanel, _InnerCardFrame
with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
    p = TabPanel()

# 关键查询：panel 实有 layout 中 wrap frame 的属性
section = p._system_plugin_section  # 默认 hidden
wrap = section.parent()  # _InnerCardFrame

print(f"section.isVisibleTo(p): {section.isVisibleTo(p)}")
print(f"wrap.isVisibleTo(p): {wrap.isVisibleTo(p)}")
print(f"section.parent() is wrap: {section.parent() is wrap}")
print(f"wrap.objectName: {wrap.objectName()}")

# 强制 setVisible(True) —— 检查是否事件被分发
from PyQt5.QtCore import QEvent
events_at_section = []

def section_eventFilter(obj, event):
    events_at_section.append(event.type())
    return False

section.installEventFilter(p)  # 用 panel 自身作为过滤器
# 用 installEventFilter 到 panel 上追踪
original_section = section.eventFilter
def section_ef(obj, event):
    if obj is section:
        events_at_section.append((event.type(), "section"))
    return original_section(obj, event)
section.eventFilter = section_ef

# 用 panel 的 eventFilter 接收
panel_events = []
def panel_ef(obj, event):
    panel_events.append((event.type(), obj.objectName() if hasattr(obj, 'objectName') else None))
    return False
p.eventFilter = panel_ef

# 现在真正触发 section.setVisible(True)
section.setVisible(True)
QApplication.processEvents()
print()
print(f"after section.setVisible(True):")
print(f"  section.isVisible: {section.isVisible()}")
print(f"  section.isVisibleTo(p): {section.isVisibleTo(p)}")
print(f"  wrap.isVisible: {wrap.isVisible()}")
print(f"  wrap.isVisibleTo(p): {wrap.isVisibleTo(p)}")
print(f"  panel events: {panel_events}")

section.setVisible(False)
QApplication.processEvents()
print()
print(f"after section.setVisible(False):")
print(f"  section.isVisible: {section.isVisible()}")
print(f"  wrap.isVisible: {wrap.isVisible()}")
print(f"  panel events: {panel_events}")
