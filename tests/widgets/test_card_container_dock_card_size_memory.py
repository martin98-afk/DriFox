# -*- coding: utf-8 -*-
"""停靠区按卡片独立记忆轴向尺寸测试（tab 切换宽度恢复默认修复）

背景（两个 bug，均在 card_container.py）：
1. 横向停靠区重开恢复逻辑 `_dock_last_size if > natural_h else natural_h`
   ——用户拖窄后折叠重开，记忆宽度 < natural（≈卡片 sizeHint）→ 恢复默认。
   tab 切换触发的正是「hide（折叠）→ show（重开）」循环。
2. `_dock_last_size` 是容器级单值：左/右/下停靠区所有插件卡片共享一份，
   不同 tab 投影不同卡片时互相覆盖宽度记忆。

覆盖：
- 拖窄后折叠重开：恢复拖窄宽度（而非 natural 默认）
- 按卡片独立记忆：A 拖到 450，B 重开恢复自己的 natural；A 重开恢复 450
- 纵向（BOTTOM）同样按卡片记忆高度
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 必须在创建 QApplication 前设置 Qt 属性（QtWebEngine 依赖）
from PyQt5.QtCore import Qt

QApplication_ShareOpenGL = Qt.AA_ShareOpenGLContexts

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PyQt5.QtCore import QEventLoop, QSize, QTimer
from PyQt5.QtWidgets import QApplication, QSplitter, QVBoxLayout, QWidget

from app.widgets.cards.card_container import BottomCardContainer, CardContainer
from app.widgets.cards.card_manager import CardManager, ContainerType


def _app():
    return QApplication.instance() or QApplication(sys.argv)


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


class _FakeCard(QWidget):
    """模拟 dock 卡片：sizeHint 提供自然尺寸，不设硬性 minimum（可拖窄）"""

    def __init__(self, hint_w: int = 300, hint_h: int = 200, parent=None):
        super().__init__(parent)
        self._hint = QSize(hint_w, hint_h)

    def sizeHint(self):
        return self._hint


class _MiniDockHost(QWidget):
    """横向停靠宿主：LEFT 容器 + 内容区放 QSplitter（对齐 TabManagerWindow 结构）"""

    def __init__(self):
        super().__init__()
        CardManager.reset_instance()
        self._card_manager = CardManager.get_instance()
        self._window_id = "test_dock_size_memory"
        self._card_manager.register_window(self._window_id)

        self._left_container = CardContainer(ContainerType.LEFT)
        self._left_container.bind_card_manager(self._card_manager, self._window_id)
        self._content = QWidget(self)

        self._splitter = QSplitter(Qt.Horizontal, self)
        self._splitter.addWidget(self._left_container)
        self._splitter.addWidget(self._content)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setHandleWidth(6)
        self._splitter.setChildrenCollapsible(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._splitter)

        self._card_a = _FakeCard(300, 100, self)
        self._card_b = _FakeCard(300, 100, self)
        self._card_manager.register_card(self._window_id, ContainerType.LEFT, "card_a", self._card_a)
        self._card_manager.register_card(self._window_id, ContainerType.LEFT, "card_b", self._card_b)
        self._left_container.add_card("card_a", self._card_a)
        self._left_container.add_card("card_b", self._card_b)

        self._left_container.enable_dock_mode(self._splitter)


class _MiniBottomHost(QWidget):
    """纵向停靠宿主：vdock splitter = 内容区 + BOTTOM 容器"""

    def __init__(self):
        super().__init__()
        CardManager.reset_instance()
        self._card_manager = CardManager.get_instance()
        self._window_id = "test_dock_size_memory_v"
        self._card_manager.register_window(self._window_id)

        self._bottom_container = BottomCardContainer()
        self._bottom_container.bind_card_manager(self._card_manager, self._window_id)
        self._content = QWidget(self)

        self._splitter = QSplitter(Qt.Vertical, self)
        self._splitter.addWidget(self._content)
        self._splitter.addWidget(self._bottom_container)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setHandleWidth(6)
        self._splitter.setChildrenCollapsible(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._splitter)

        self._card_a = _FakeCard(300, 200, self)
        self._card_b = _FakeCard(300, 200, self)
        self._card_manager.register_card(self._window_id, ContainerType.BOTTOM, "card_a", self._card_a)
        self._card_manager.register_card(self._window_id, ContainerType.BOTTOM, "card_b", self._card_b)
        self._bottom_container.add_card("card_a", self._card_a)
        self._bottom_container.add_card("card_b", self._card_b)

        self._bottom_container.enable_dock_mode(self._splitter)


def _drag_splitter(sp: QSplitter, sizes: list):
    """模拟用户拖拽：setSizes + 手动发射 splitterMoved（setSizes 不发该信号）"""
    sp.setSizes(sizes)
    sp.splitterMoved.emit(0, 0)
    _pump(50)


class TestHorizontalPerCardSizeMemory:
    def test_drag_narrow_survives_collapse_reopen(self):
        """拖窄后折叠重开：恢复拖窄宽度而非 natural 默认（核心 bug）"""
        w = _MiniDockHost()
        w.resize(900, 600)
        w.show()
        _pump(50)
        container = w._left_container
        mgr = w._card_manager

        mgr.show_card("card_a", w._window_id)
        _pump(300)
        expanded_w = container.width()
        assert expanded_w > 200, f"展开失败: w={expanded_w}"

        # 拖窄到 220（< natural ≈308，> min_floor）
        _drag_splitter(w._splitter, [220, 674])
        assert abs(container.width() - 220) <= 2, f"拖窄失败: w={container.width()}"

        # 折叠（模拟 tab 切换：卡片在目标 tab 不可见）
        mgr.hide_card("card_a", w._window_id)
        _pump(300)
        assert container.isHidden() or container.width() == 0, "容器未折叠"

        # 重开（切回 tab）
        mgr.show_card("card_a", w._window_id)
        _pump(300)
        assert abs(container.width() - 220) <= 6, f"拖窄宽度丢失（恢复默认）: w={container.width()} 期望≈220"

    def test_per_card_independent_memory(self):
        """A 记忆 450，B 重开恢复 natural，A 再重开恢复 450"""
        w = _MiniDockHost()
        w.resize(900, 600)
        w.show()
        _pump(50)
        container = w._left_container
        mgr = w._card_manager

        # A 拖宽到 450 → 折叠
        mgr.show_card("card_a", w._window_id)
        _pump(300)
        _drag_splitter(w._splitter, [450, 444])
        assert abs(container.width() - 450) <= 2
        mgr.hide_card("card_a", w._window_id)
        _pump(300)

        # B 重开：恢复 B 自己的 natural（无记忆），不是 A 的 450
        mgr.show_card("card_b", w._window_id)
        _pump(300)
        b_w = container.width()
        assert abs(b_w - 450) > 20, f"B 不应继承 A 的记忆宽度: w={b_w}"
        assert 250 <= b_w <= 360, f"B 应展开到 natural: w={b_w}"
        mgr.hide_card("card_b", w._window_id)
        _pump(300)

        # A 再重开：恢复 450
        mgr.show_card("card_a", w._window_id)
        _pump(300)
        assert abs(container.width() - 450) <= 6, f"A 记忆宽度丢失: w={container.width()}"

    def test_tab_switch_projection_loop(self):
        """模拟 tab 切换投影循环（sync_floating_cards_to_tab 的 show/hide 链）"""
        w = _MiniDockHost()
        w.resize(900, 600)
        w.show()
        _pump(50)
        container = w._left_container
        mgr = w._card_manager

        mgr.show_card("card_a", w._window_id)
        _pump(300)
        _drag_splitter(w._splitter, [380, 514])
        user_w = container.width()
        assert abs(user_w - 380) <= 2

        # 模拟切走再切回（多轮）
        for _ in range(2):
            mgr.hide_card("card_a", w._window_id)
            _pump(300)
            mgr.show_card("card_a", w._window_id)
            _pump(300)
        assert abs(container.width() - user_w) <= 6, f"tab 切换后宽度漂移: w={container.width()} 期望≈{user_w}"


class TestVerticalPerCardSizeMemory:
    def test_bottom_per_card_height_memory(self):
        """BOTTOM 高度按卡片记忆：A 拉高 300，B 恢复 natural，A 恢复 300"""
        w = _MiniBottomHost()
        w.resize(900, 600)
        w.show()
        _pump(50)
        container = w._bottom_container
        mgr = w._card_manager

        mgr.show_card("card_a", w._window_id)
        _pump(300)
        # 纵向有 30% ratio_floor（600*0.3=180）；拉高到 300 附近（handle 6px 计入分配）
        _drag_splitter(w._splitter, [300, 294])
        dragged_h = container.height()
        assert 290 <= dragged_h <= 306, f"拉高失败: h={dragged_h}"

        mgr.hide_card("card_a", w._window_id)
        _pump(300)

        mgr.show_card("card_b", w._window_id)
        _pump(300)
        b_h = container.height()
        assert abs(b_h - dragged_h) > 20, f"B 不应继承 A 的高度: h={b_h}"

        mgr.hide_card("card_b", w._window_id)
        _pump(300)

        mgr.show_card("card_a", w._window_id)
        _pump(300)
        assert abs(container.height() - dragged_h) <= 6, f"A 高度记忆丢失: h={container.height()}"


class TestResizeWhileOtherCardOpen:
    def test_resize_small_while_b_open_then_reopen_a_restores_memory(self):
        """回归：B 展开时把窗口缩到比 A 记忆宽度还小，再重开 A、窗口拉回
        大尺寸后，A 必须恢复到记忆宽度（而非卡在压缩值）。

        根因：dock 折叠 _release_dock_space 把槽位置 0，重开 A 时窗口太小
        _restore_dock_size 借不到空间静默失败；窗口拉大后既无 resizeEvent
        （dock 自身尺寸未变，空间被对话区吸收）也无补恢复路径 → 记忆视觉
        上永久丢失。修复：监听宿主 splitter 几何变化，空间富余时恢复记忆。
        """
        w = _MiniDockHost()
        w.resize(900, 600)
        w.show()
        _pump(50)
        container = w._left_container
        mgr = w._card_manager

        mgr.show_card("card_a", w._window_id)
        _pump(300)
        _drag_splitter(w._splitter, [450, 444])
        assert abs(container.width() - 450) <= 2

        # 打开 B（不同 splitter）
        mgr.show_card("card_b", w._window_id)
        _pump(300)

        # B 展开时把窗口缩到 300（< A 记忆 450）：splitter 比例压缩 dock
        w.resize(300, 600)
        _pump(200)
        assert container.width() < 450, "窗口缩小时 dock 应被压缩"

        # 重开 A（窗口仍小，dock 只能缩到可用空间，记忆暂不可达）
        mgr.show_card("card_a", w._window_id)
        _pump(300)
        assert container.width() <= 450

        # 窗口拉回大尺寸：dock 必须恢复到记忆宽度 450
        w.resize(900, 600)
        _pump(300)
        assert abs(container.width() - 450) <= 6, f"A 记忆丢失（窗口拉大未恢复）: w={container.width()} 期望≈450"

    def test_resize_while_b_open_does_not_corrupt_a_memory_dict(self):
        """B 展开时窗口缩放不应污染 A 的记忆槽（记忆 dict 始终保持 450）"""
        w = _MiniDockHost()
        w.resize(900, 600)
        w.show()
        _pump(50)
        container = w._left_container
        mgr = w._card_manager

        mgr.show_card("card_a", w._window_id)
        _pump(300)
        _drag_splitter(w._splitter, [450, 444])
        assert abs(container.width() - 450) <= 2

        mgr.show_card("card_b", w._window_id)
        _pump(300)
        w.resize(300, 600)
        _pump(200)
        w.resize(900, 600)
        _pump(200)

        mgr.show_card("card_a", w._window_id)
        _pump(300)
        assert container._dock_card_sizes.get("card_a") == 450, f"A 记忆槽被污染: {container._dock_card_sizes}"
        assert abs(container.width() - 450) <= 6, f"A 记忆丢失: w={container.width()}"
