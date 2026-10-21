# -*- coding: utf-8 -*-
"""dock splitter 按卡片记忆回归测试（wrapper 孙项化回归修复）

背景（2026-08-23 回归，01edd48f/37933366 引入 _DockSideWrapper 后）：
1. _on_dock_splitter_moved 读容器自身 width（孙项重排中间态=EXPAND_MAX）
   → 拖拽记忆爆表 16777215 → _ensure_splitter_fits 判溢出压槽位到 40px
2. wrapper._sync 抢先 setSizes 到自身单值记忆/默认 300，与 per-card 恢复打架
   → 切 tab 先弹默认位置再弹记忆位置（双跳）
3. 已展开容器内互斥切卡走早退分支直接 return → 新卡片记忆不恢复
   → 切插件容器停在旧插件位置，之后二次触发才弹到新插件位置

修复：记忆读写改用 splitter 槽位尺寸+防御（F1/F2）；展开先预置槽位再动画（F3）；
wrapper 槽位>0 时不抢 setSizes（F4）；早退分支双向恢复可见卡片记忆（F5）。

注意：拖拽模拟 = setSizes + 手动调 _on_dock_splitter_moved。offscreen 下
moveSplitter 大/小跨度均走 Qt fold-restart 怪路径（槽位瞬间爆表），不可靠；
真实拖拽逐像素正常，结束时 splitterMoved 随最终槽位发射，语义等价。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ⚠ 必须先建 QApplication 再 import app.widgets.*：tab_manager_window →
# theme_manager/design_tokens 在 import 时执行字体/样式测量，Windows
# offscreen 下无 QApplication 会 0xC0000409 直接崩溃（无任何输出）
from PyQt5.QtCore import QCoreApplication, QEventLoop, QSize, Qt, QTimer
from PyQt5.QtWidgets import QApplication, QSplitter, QVBoxLayout, QWidget

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
# ⚠ 必须持有 QApplication 引用：无引用时 Python GC 会销毁 C++ 实例，
# 后续字体/样式操作直接 0xC0000409 崩溃
app = QApplication(["test"])

from app.widgets.cards.card_container import CardContainer
from app.widgets.cards.card_manager import CardManager, ContainerType
from app.widgets.tab_manager_window import _DockSideWrapper

_HOST = None  # 模块级持有，防 GC 后 Qt C++ 对象悬垂


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


class _FakeCard(QWidget):
    def __init__(self, hint_w=300, parent=None):
        super().__init__(parent)
        self._hint = QSize(hint_w, 100)

    def sizeHint(self):
        return self._hint


class _Host(QWidget):
    """对齐 TabManagerWindow.dock_splitter 结构：wrapper(CardContainer, stack) | content"""

    def __init__(self):
        super().__init__()
        CardManager.reset_instance()
        self.cm = CardManager.get_instance()
        self.win = "dock_memory_regression"
        self.cm.register_window(self.win)

        self.ct = CardContainer(ContainerType.RIGHT)
        self.ct.bind_card_manager(self.cm, self.win)
        self.stack = QWidget()
        # wrapper parent=None：显式 parent=host 时 offscreen 下 parent 链
        # 变更（addWidget 入 splitter）触发 Qt 崩溃（0xC0000409）
        self.wr = _DockSideWrapper(self.ct, self.stack, None)
        self.content = QWidget(self)

        self.sp = QSplitter(Qt.Horizontal, self)
        self.sp.addWidget(self.wr)
        self.sp.addWidget(self.content)
        self.sp.setStretchFactor(0, 0)
        self.sp.setStretchFactor(1, 1)
        self.sp.setHandleWidth(6)
        self.sp.setChildrenCollapsible(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.sp)

        self.a = _FakeCard(300, self)
        self.b = _FakeCard(300, self)
        self.cm.register_card(self.win, ContainerType.RIGHT, "card_a", self.a)
        self.cm.register_card(self.win, ContainerType.RIGHT, "card_b", self.b)
        self.ct.add_card("card_a", self.a)
        self.ct.add_card("card_b", self.b)
        self.ct.enable_dock_mode(self.sp)
        self.wr.attach_to_splitter(self.sp, 0)

    def slot(self) -> int:
        return self.sp.sizes()[0]

    def drag(self, w: int):
        """拖拽模拟：槽位分配到目标 + 手动触发 splitterMoved 记忆回调"""
        total = self.sp.width() - self.sp.handleWidth()
        self.sp.setSizes([w, total - w])
        _pump(30)
        self.ct._on_dock_splitter_moved()

    def sample(self, ms=600):
        trace = []
        t = QTimer(self)
        t.setInterval(16)
        t.timeout.connect(lambda: trace.append(self.slot()))
        t.start()
        _pump(ms)
        t.stop()
        return trace


def _make_host():
    global _HOST
    _HOST = _Host()
    _HOST.resize(1000, 600)
    _HOST.show()
    _pump(100)
    return _HOST


def test_drag_memory_uses_slot_size_not_widget_width():
    """T1: 拖拽记忆=槽位尺寸且槽位稳定（回归：记忆爆表→槽位被压 40px）"""
    host = _make_host()
    host.cm.show_card("card_a", host.win)
    _pump(400)
    host.drag(450)

    assert host.ct._dock_card_sizes.get("card_a") == 450, (
        f"拖拽记忆应为槽位 450，实际 {host.ct._dock_card_sizes.get('card_a')}"
    )
    assert host.slot() == 450, f"拖拽后槽位应稳定 450，实际 {host.slot()}"
    _pump(200)
    assert host.slot() >= 300, f"槽位不应被溢出压缩成细条，实际 {host.slot()}"


def test_reopen_restores_memory_no_double_jump():
    """T2: 折叠重开一步恢复记忆宽度（回归：先弹默认再弹记忆的双跳）"""
    host = _make_host()
    host.cm.show_card("card_a", host.win)
    _pump(400)
    host.drag(450)
    host.cm.hide_card("card_a", host.win)
    _pump(400)

    host.cm.show_card("card_a", host.win)  # 重开（切回 tab）
    trace = host.sample(600)
    assert host.slot() == 450, f"重开应恢复记忆 450，实际 {host.slot()}"
    assert not any(v < 100 for v in trace), f"重开过程不应出现细条值: {trace}"
    assert not any(v > 500 for v in trace), f"重开过程不应爆表: {trace}"
    assert trace and trace[0] >= 400, (
        f"重开起点应已在目标区（一步到位），实际首采样 {trace[0] if trace else None}"
    )
    assert host.ct._dock_card_sizes.get("card_a") == 450


def test_switch_card_async_tick_restores_new_card_memory():
    """T3: 分 tick 切插件（折叠动画中 show 新卡）→ 双向恢复各自记忆"""
    host = _make_host()
    host.cm.show_card("card_a", host.win)
    _pump(400)
    host.drag(450)
    host.cm.hide_card("card_a", host.win)
    _pump(400)

    host.cm.show_card("card_b", host.win)
    _pump(400)
    host.drag(380)

    host.cm.hide_card("card_b", host.win)
    _pump(100)  # 折叠动画进行中
    host.cm.show_card("card_a", host.win)
    _pump(400)
    assert host.slot() == 450, f"A 重开应恢复 450，实际 {host.slot()}"

    host.cm.hide_card("card_a", host.win)
    _pump(100)  # 折叠动画进行中（异步 tick）
    host.cm.show_card("card_b", host.win)
    _pump(600)
    assert host.slot() == 380, f"切到 B 应恢复其记忆 380，实际 {host.slot()}"
    assert host.ct._dock_card_sizes.get("card_b") == 380


def test_switch_card_same_tick_restores_new_card_memory():
    """T4: 已展开容器内同 tick 互斥切卡（不折叠）→ 新卡记忆仍恢复（早退分支）"""
    host = _make_host()
    host.cm.show_card("card_a", host.win)
    _pump(400)
    host.drag(450)
    host.cm.hide_card("card_a", host.win)
    _pump(400)
    host.cm.show_card("card_b", host.win)
    _pump(400)
    host.drag(380)
    host.cm.hide_card("card_b", host.win)
    _pump(400)
    host.cm.show_card("card_a", host.win)
    _pump(400)
    assert host.slot() == 450

    # 同 tick：hide(a) 后立刻 show(b)，容器未折叠（早退分支场景）
    host.cm.hide_card("card_a", host.win)
    host.cm.show_card("card_b", host.win)
    _pump(600)
    assert host.slot() == 380, f"同 tick 切换应恢复 B 记忆 380，实际 {host.slot()}"


if __name__ == "__main__":
    failures = 0
    for fn in (
        test_drag_memory_uses_slot_size_not_widget_width,
        test_reopen_restores_memory_no_double_jump,
        test_switch_card_async_tick_restores_new_card_memory,
        test_switch_card_same_tick_restores_new_card_memory,
    ):
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
