# -*- coding: utf-8 -*-
"""探针 U：中段阅读时「焦点卡被回收」的跳变复现 + 修复方案验证（用完即删）。

对齐用户场景：
  1. 用户曾点击卡片内按钮（焦点遗留在该卡）
  2. 用户滚动到中段阅读
  3. 虚拟滚动回收删除含焦点的卡（hide()）
  4. Qt 焦点转移 → QScrollArea 自动滚动 → 视口跳变
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

CARD_H = 300


def p(*a):
    print(*a)
    sys.stdout.flush()


def make_case():
    win = build_window()
    sb = win.chat_scroll_area.verticalScrollBar()
    return win, sb


def focus_card(win, idx):
    """把焦点放到 idx 卡内的按钮（模拟用户点过该卡）。"""
    card = win._batch_cards[idx][0]
    focusables = [c for c in card.findChildren(QWidget) if c.focusPolicy() != Qt.NoFocus]
    if not focusables:
        p("!! 无聚焦控件")
        return None
    focusables[0].setFocus(Qt.OtherFocusReason)
    for _ in range(2):
        app.processEvents()
    return focusables[0]


def run(name, protect_fn):
    win, sb = make_case()
    fw = focus_card(win, 0)  # 焦点在卡 0（将在滚到中段后被回收）

    # 中段阅读：视口到卡 15
    sb.setValue(15 * CARD_H)
    for _ in range(2):
        app.processEvents()
    v0 = sb.value()
    p(f"\n=== [{name}] 焦点控件={type(fw).__name__ if fw else None}  中段 value={v0}（卡15）===")

    if protect_fn is not None:
        protect_fn(win)

    win._recycle_out_of_view_batches()
    for _ in range(4):
        app.processEvents()

    v1 = sb.value()
    drift = v1 - v0
    ok = abs(drift) <= 2
    p(f"[{name}] 回收后 value={v1}  drift={drift:+d}  {'PASS 保持原位' if ok else 'FAIL 跳变'}")
    p(f"    回收批次={[i for i, b in enumerate(win._batch_cards) if b is None][:10]}...")
    p(f"    焦点 now = {QApplication.focusWidget()}")
    return ok


def protect_none(win):
    pass


def protect_clear(win):
    """方案 A：清掉聊天区内的焦点（模拟在删除前判断焦点在待删子树）。"""
    fw = QApplication.focusWidget()
    if fw is not None and win.chat_scroll_area.isAncestorOf(fw):
        fw.clearFocus()


def protect_move(win):
    """方案 B：把焦点移回滚动区。"""
    win.chat_scroll_area.setFocus(Qt.OtherFocusReason)


results = []
results.append(("S 无保护", run("S 无保护", protect_none)))
results.append(("A clearFocus", run("A clearFocus", protect_clear)))
results.append(("B setFocus(area)", run("B setFocus(area)", protect_move)))

p("\n================ 汇总 ================")
for name, ok in results:
    p(f"{name:20s} {'PASS' if ok else 'FAIL'}")
