# -*- coding: utf-8 -*-
"""探针 S：验证「删除前把焦点移出被删子树」在 DriFox 桩环境的修复效果（用完即删）。

场景对齐用户报告：
  1. 焦点在卡内按钮（TransparentToolButton，用户点击卡片后遗留）
  2. 滚轮上滚 / 回收触发 → 删除含焦点卡 → 焦点自动转移 → ensureVisible 拽走视口
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402


def p(*a):
    print(*a)
    sys.stdout.flush()


win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout
area = win.chat_scroll_area

# 找卡内可聚焦按钮
card0 = win._batch_cards[0][0]
focusables = [c for c in card0.findChildren(QWidget) if c.focusPolicy() != 0]
p(f"card0 内可聚焦控件数：{len(focusables)}")
for f in focusables[:5]:
    p(f"    {type(f).__name__} policy={f.focusPolicy()}")

# 模拟：用户点过卡 0 内按钮
if focusables:
    focusables[0].setFocus()
    for _ in range(2):
        app.processEvents()
    p(f"setFocus后 focusWidget={QApplication.focusWidget()}")

sb.setValue(sb.maximum())
for _ in range(2):
    app.processEvents()
v0 = sb.value()
p(f"\n滚到底: value={v0} max={sb.maximum()}")

# === 组1：无保护，回收（应跳变）===
win._recycle_out_of_view_batches()
for _ in range(3):
    app.processEvents()
v1 = sb.value()
p(f"[无保护] 回收后 value={v1}（漂移 {v1 - v0:+d}）")

# 恢复：全部还原（桩环境快速重建）
p("\n(重建窗口跑第二组)")
win2 = build_window()
sb2 = win2.chat_scroll_area.verticalScrollBar()
area2 = win2.chat_scroll_area
card0b = win2._batch_cards[0][0]
focusables2 = [c for c in card0b.findChildren(QWidget) if c.focusPolicy() != 0]
if focusables2:
    focusables2[0].setFocus()
    for _ in range(2):
        app.processEvents()
sb2.setValue(sb2.maximum())
for _ in range(2):
    app.processEvents()
v2 = sb2.value()
p(f"滚到底: value={v2} max={sb2.maximum()}")

# === 组2：焦点保护：回收前把焦点移出被删批次 ===
from PySide6.QtWidgets import QApplication as QA  # noqa: E402

fw = QA.focusWidget()
if fw is not None:
    # 找 fw 是否在将被删的批次里；若是，setFocus(area2) 保护
    fw.setFocusPolicy(0)  # 模拟：先禁掉它（最粗暴的保护）
    area2.setFocus() if area2.focusPolicy() != 0 else None
    for _ in range(2):
        app.processEvents()
    p(f"保护后 focusWidget={QA.focusWidget()}")

win2._recycle_out_of_view_batches()
for _ in range(3):
    app.processEvents()
v3 = sb2.value()
p(f"[焦点保护] 回收后 value={v3}（漂移 {v3 - v2:+d}）")
