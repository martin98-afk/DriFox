# -*- coding: utf-8 -*-
"""临时验证脚本（不属于交付物）：附件栏三项修复的行为验证

1. FlowLayout 的 minimumSizeHint 不随 chip 数线性增长（侧边栏不再被挤塌）
2. FlowLayout 确实换行（高度随行数增长）
3. qcolor_from_token 能解析主题里的 rgba() 色值
4. 文本 → 附件 反向同步的多重集差语义正确
5. SendableTextEdit 构造期间不再 AttributeError（槽函数状态属性前置回归）
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger  # noqa: E402

from app.utils.design_tokens import qcolor_from_token  # noqa: E402
from app.widgets.flow_layout import FlowLayout  # noqa: E402

# 项目模块导入时会往 loguru 灌大量插件注册日志，淹没本脚本的输出
logger.remove()

app = QApplication.instance() or QApplication(sys.argv)


class _FakeChip(QFrame):
    """尺寸行为对齐真实 AttachmentChip（165x26 固定）"""

    def __init__(self, width=165, height=26, parent=None):
        super().__init__(parent)
        self.setFixedSize(width, height)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


def _make_container(kind: str, n: int):
    container = QWidget()
    container.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    if kind == "flow":
        lay = FlowLayout(container, spacing=6, margins=0)
        lay.setContentsMargins(8, 8, 8, 2)
    else:
        lay = QHBoxLayout(container)
        lay.setContentsMargins(6, 6, 6, 0)
        lay.setSpacing(3)
        lay.addStretch()
    for _ in range(n):
        chip = _FakeChip(parent=container)
        if kind == "flow":
            lay.addWidget(chip)
        else:
            lay.insertWidget(lay.count() - 1, chip)
    return container


def _build(kind: str, n: int, width: int):
    outer = QWidget()
    outer.resize(width, 500)
    lay = QVBoxLayout(outer)
    lay.setContentsMargins(0, 0, 0, 0)
    container = _make_container(kind, n)
    lay.addWidget(container)
    lay.addStretch()
    outer.show()
    app.processEvents()
    return outer, container


print("=" * 76)
print("① 附件栏 minimumSizeHint 宽度：QHBoxLayout(旧) vs FlowLayout(新)  [窗口 800px]")
print("=" * 76)
print(f"{'chip数':>6} | {'旧 QHBoxLayout':>15} | {'新 FlowLayout':>15} | {'新：实际高':>10}")
print("-" * 76)
for n in (1, 2, 3, 4, 6, 10, 20, 40):
    old_outer, old_c = _build("h", n, 800)
    new_outer, new_c = _build("flow", n, 800)
    print(
        f"{n:>6} | {old_c.minimumSizeHint().width():>15} | "
        f"{new_c.minimumSizeHint().width():>15} | {new_c.height():>10}"
    )
    old_outer.close()
    new_outer.close()

print()
print("=" * 76)
print("② QSplitter 实测：侧边栏宽度随附件数变化（窗口 1000px，侧栏初始 201px）")
print("   TabPanel 自动折叠阈值 = 100px，低于即折叠")
print("=" * 76)
print(f"{'chip数':>6} | {'旧：侧栏宽':>12} | {'新：侧栏宽':>12} | 结论")
print("-" * 76)
for n in (0, 1, 2, 3, 5, 8, 15, 30):
    widths = {}
    for kind in ("h", "flow"):
        win = QWidget()
        win.resize(1000, 600)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(0, 0, 0, 0)
        sp = QSplitter(Qt.Horizontal, win)
        lay.addWidget(sp)

        sidebar = QWidget(sp)
        sidebar.setMinimumWidth(60)
        sidebar.setMaximumWidth(414)
        sp.addWidget(sidebar)

        holder = QWidget(sp)
        holder_lay = QVBoxLayout(holder)
        holder_lay.setContentsMargins(0, 0, 0, 0)
        holder_lay.addWidget(_make_container(kind, n))
        holder_lay.addStretch()
        sp.addWidget(holder)

        sp.setStretchFactor(0, 0)
        sp.setStretchFactor(1, 1)
        sp.setHandleWidth(4)
        sp.setChildrenCollapsible(False)
        sp.setSizes([201, 799])
        win.show()
        app.processEvents()
        widths[kind] = sidebar.width()
        win.close()
    old_bad = widths["h"] < 100
    new_bad = widths["flow"] < 100
    verdict = f"旧{'塌' if old_bad else 'OK'} / 新{'塌' if new_bad else 'OK'}"
    print(f"{n:>6} | {widths['h']:>12}{'!' if old_bad else ' '} | {widths['flow']:>12} | {verdict}")

print()
print("=" * 76)
print("③ FlowLayout 换行：6 个 165px chip 在不同可用宽度下的高度")
print("=" * 76)
for w in (900, 600, 400, 260, 180):
    outer, container = _build("flow", 6, w)
    rows = (6 * 165 + 6 * 5) // max(1, w - 16)
    print(f"  容器宽 {w:>4}px → 高度 {container.height():>3}px  (约 {max(1, rows)} 行)")
    outer.close()

print()
print("=" * 76)
print("④ qcolor_from_token 解析主题色值")
print("=" * 76)
for raw in (
    "rgba(255, 255, 255, 0.05)",
    "rgba(255, 255, 255, 0.08)",
    "rgba(33, 33, 38, 0.96)",
    "#C9A85C",
    "not-a-color",
):
    c = qcolor_from_token(raw)
    print(f"  {raw:<28} → valid={int(c.isValid())}  rgba=({c.red()},{c.green()},{c.blue()},{c.alphaF():.2f})")

print()
print("=" * 76)
print("⑤ 反向同步：多重集差语义（同名文件只删一个）")
print("=" * 76)


def multiset_diff(last, current):
    """复刻 _sync_placeholder_removals 的差集算法"""
    remaining = list(current)
    removed = []
    for name in last:
        if name in remaining:
            remaining.remove(name)
        else:
            removed.append(name)
    return removed


cases = [
    (["a.py", "b.py"], ["a.py"], ["b.py"], "删掉 b 的引用 → 只删 b"),
    (["a.py", "a.py"], ["a.py"], ["a.py"], "两个同名，删一个 → 只删一个"),
    (["a.py"], ["a.py", "b.py"], [], "新增引用 → 不删任何附件"),
    ([], ["a.py"], [], "从无到有 → 不删（拖放附件的正常路径）"),
    (["a.py"], [], ["a.py"], "清空文本 → 删 a"),
]
for last, current, expect, desc in cases:
    got = multiset_diff(last, current)
    print(f"  {'OK ' if got == expect else 'FAIL'} {desc:<26} last={last} current={current} → removed={got}")

print()
print("=" * 76)
print("⑥ SendableTextEdit 构造回归（槽函数状态属性必须早于 textChanged 连接）")
print("=" * 76)
try:
    from app.widgets.bottom_input_area import SendableTextEdit

    w = SendableTextEdit()
    w.show()
    app.processEvents()
    # 触发一轮完整的文本变更链路
    w.setPlainText("看看 [[main.py]] 里的问题")
    app.processEvents()
    w.setPlainText("看看 里的问题")
    app.processEvents()
    print("  OK  构造 + 文本变更链路无异常")
    print(f"      占位符快照基线 = {w._last_placeholder_names}")
    w.close()
except Exception as exc:  # noqa: BLE001
    print(f"  FAIL {type(exc).__name__}: {exc}")
    raise

print()
print("验证完成。")
