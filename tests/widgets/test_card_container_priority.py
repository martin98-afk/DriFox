# -*- coding: utf-8 -*-
"""卡片槽位小窗口优先显示优化测试（A1+A2+A3）

覆盖：
- A1: _visible_cards_min_axis() 返回可见卡片最小轴向尺寸（隐藏卡片不计）
- A1: 停靠模式展开后 min 锁 = max(_dock_min, 可见卡片最小轴)，
      窗口缩小时 splitter 优先保证卡片内容完整可见
- A3: 非停靠（普通布局）展开后锁 min=自然高度
- 动态收缩：min 锁定后卡片收缩 → heightChanged → _schedule_expand → _do_expand
      开头解锁重算，不被旧 min 锁卡死
- A2: main_widget chat_scroll_area 初始最小宽 320 + resizeEvent 极小窗口兜底（AST）
"""

import ast
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtCore import QEventLoop, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QSplitter, QWidget

from app.widgets.cards.card_container import CardContainer
from app.widgets.cards.card_manager import ContainerType


def _app():
    return QApplication.instance() or QApplication(sys.argv)


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _card(min_w=0, min_h=0, fixed_h=None):
    """构造测试卡片：可设最小宽/高或固定高度

    无布局裸 QWidget 的 minimumSizeHint() 返回无效尺寸 (-1,-1)，
    真实卡片（file-tree/tool_control 等）都有布局，故此处重写
    minimumSizeHint 模拟真实卡片的最小内容尺寸。
    """
    card = QWidget()
    card._min_hint_w, card._min_hint_h = min_w, min_h
    if fixed_h is not None:
        card.setFixedHeight(fixed_h)
        card._min_hint_h = fixed_h
    if min_w:
        card.setMinimumWidth(min_w)
    if min_h:
        card.setMinimumHeight(min_h)

    def _minimum_size_hint():
        from PyQt5.QtCore import QSize

        return QSize(card._min_hint_w, card._min_hint_h)

    card.minimumSizeHint = _minimum_size_hint
    return card


class _DynamicCard(QWidget):
    """带 heightChanged 信号的测试卡片（模拟 todo 等动态高度卡片）"""

    heightChanged = pyqtSignal()

    def __init__(self, h: int = 150):
        super().__init__()
        self.setFixedHeight(h)

    def minimumSizeHint(self):
        from PyQt5.QtCore import QSize

        return QSize(0, self.height())

    def set_height(self, h: int):
        self.setFixedHeight(h)
        self.heightChanged.emit()


# ═══════════════════════════════════════════════════════════════
# A1: _visible_cards_min_axis()
# ═══════════════════════════════════════════════════════════════


class TestVisibleCardsMinAxis:
    def test_horizontal_takes_width_of_visible_cards(self):
        _app()
        c = CardContainer(ContainerType.LEFT)
        c.add_card("small", _card(min_w=120, min_h=40))
        c.add_card("big", _card(min_w=220, min_h=40))
        c.add_card("hidden", _card(min_w=999, min_h=40))
        c._cards["small"].show()
        c._cards["big"].show()
        c._cards["hidden"].hide()
        assert c._visible_cards_min_axis() == 220  # 隐藏卡片不计入

    def test_vertical_takes_height_of_visible_cards(self):
        _app()
        c = CardContainer(ContainerType.BOTTOM)
        c.add_card("a", _card(min_w=40, min_h=120))
        c.add_card("b", _card(min_w=40, min_h=300))
        c.add_card("hidden", _card(min_w=40, min_h=999))
        c._cards["a"].show()
        c._cards["b"].show()
        c._cards["hidden"].hide()
        assert c._visible_cards_min_axis() == 300

    def test_no_visible_cards_returns_zero(self):
        _app()
        c = CardContainer(ContainerType.LEFT)
        assert c._visible_cards_min_axis() == 0


# ═══════════════════════════════════════════════════════════════
# A1: 停靠模式展开后 min 锁升级
# ═══════════════════════════════════════════════════════════════


class TestDockMinLockUpgrade:
    def _make_dock(self, container, orientation):
        splitter = QSplitter(orientation)
        splitter.addWidget(container)
        splitter.addWidget(QWidget())  # 占位邻居（模拟内容区）
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        container.enable_dock_mode(splitter)
        splitter.resize(900, 600)
        splitter.show()
        return splitter

    def test_left_container_min_width_covers_card(self):
        """LEFT 停靠区展开后 min 宽 ≥ 可见卡片最小宽（≥ _DOCK_MIN_H=240）"""
        _app()
        c = CardContainer(ContainerType.LEFT)
        card = _card(min_w=240, min_h=60)
        c.add_card("card", card)
        self._make_dock(c, Qt.Horizontal)
        card.show()
        c.show()
        c._do_expand()
        _pump(300)  # 等待展开动画完成
        assert c.minimumWidth() >= 240, f"min width={c.minimumWidth()} 未覆盖卡片最小宽"
        assert c._axis_max() >= c._EXPAND_MAX

    def test_dock_min_h_fixed_floor_240(self):
        """横向 dock 有固定下限 240：即使卡片 minimumSizeHint 很小也不依赖"""
        _app()
        c = CardContainer(ContainerType.LEFT)
        card = _card(min_w=10, min_h=60)  # 卡片自身 min 很小
        c.add_card("card", card)
        self._make_dock(c, Qt.Horizontal)
        card.show()
        c.show()
        c._do_expand()
        _pump(300)
        assert c.minimumWidth() >= 240, f"固定下限未生效: min width={c.minimumWidth()}"

    def test_splitter_overflow_clamps_dock(self):
        """用户拖大 dock 后窗口缩小：溢出兜底把 dock 压回 min，不裁切"""
        _app()
        c = CardContainer(ContainerType.LEFT)
        card = _card(min_w=200, min_h=60)
        c.add_card("card", card)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(c)
        content = QWidget()
        splitter.addWidget(content)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        c.enable_dock_mode(splitter)
        splitter.resize(900, 600)
        splitter.show()
        card.show()
        c.show()
        c._do_expand()
        _pump(300)
        assert c.minimumWidth() >= 240
        # 模拟用户把 dock 拖大
        splitter.setSizes([500, 400])
        _pump(50)
        # 缩小窗口到 600：dock 500 + content 无法容纳 → 溢出兜底压 dock 回 min
        splitter.resize(600, 450)
        _pump(300)
        sizes = splitter.sizes()
        assert sum(sizes) <= splitter.width(), f"splitter 仍溢出: sizes={sizes} width={splitter.width()}"
        assert c.width() >= c.minimumWidth(), f"dock 被压到 min 以下: w={c.width()} min={c.minimumWidth()}"

    def test_bottom_container_min_height_covers_card(self):
        """BOTTOM 停靠区展开后 min 高 ≥ 可见卡片最小高（> _DOCK_MIN_V=80）"""
        _app()
        c = CardContainer(ContainerType.BOTTOM)
        card = _card(min_w=60, min_h=160)
        c.add_card("card", card)
        self._make_dock(c, Qt.Vertical)
        card.show()
        c.show()
        c._do_expand()
        _pump(300)
        assert c.minimumHeight() >= 160, f"min height={c.minimumHeight()} 未覆盖卡片最小高"
        assert c._axis_max() >= c._EXPAND_MAX

    def test_collapse_releases_min_lock(self):
        """折叠路径开头 _set_axis_min(0) 解锁，min 锁不残留"""
        _app()
        c = CardContainer(ContainerType.LEFT)
        card = _card(min_w=240, min_h=60)
        c.add_card("card", card)
        self._make_dock(c, Qt.Horizontal)
        card.show()
        c.show()
        c._do_expand()
        _pump(300)
        assert c.minimumWidth() >= 240
        # 隐藏卡片 → 折叠 → min 解锁归 0
        card.hide()
        c._do_expand()
        _pump(300)
        assert c.minimumWidth() == 0
        assert c._axis_max() == 0


# ═══════════════════════════════════════════════════════════════
# A3: 非停靠（普通布局）展开后锁 min=自然高度
# ═══════════════════════════════════════════════════════════════


class TestNormalModeMinLock:
    def test_expand_locks_min_to_natural(self):
        """普通布局展开后 min ≥ 卡片自然高度"""
        _app()
        c = CardContainer(ContainerType.BOTTOM)
        card = _DynamicCard(h=150)
        c.add_card("card", card)
        c.resize(400, 50)
        c.show()
        card.show()
        c._do_expand()
        _pump(300)
        assert c.minimumHeight() >= 150, f"min height={c.minimumHeight()}"

    def test_shrink_not_blocked_by_min_lock(self):
        """动态收缩：min 锁定后卡片收缩 → heightChanged → 解锁重算，不被旧锁卡死"""
        _app()
        c = CardContainer(ContainerType.BOTTOM)
        card = _DynamicCard(h=150)
        c.add_card("card", card)
        c.resize(400, 50)
        c.show()
        card.show()
        c._do_expand()
        _pump(300)
        assert c.minimumHeight() >= 150  # 旧锁已生效

        # 卡片收缩 → heightChanged → _schedule_expand → _do_expand
        # _do_expand 开头 _set_axis_min(0) 解锁 → 重算 natural≈60 → 重新锁新 min
        card.set_height(60)
        _pump(300)
        assert c.minimumHeight() < 150, f"旧锁卡死: min height={c.minimumHeight()}"
        assert c.minimumHeight() >= 60, f"新锁未生效: min height={c.minimumHeight()}"

    def test_dock_shrink_not_blocked_by_min_lock(self):
        """停靠模式动态收缩：min 锁升级后卡片收缩仍能解锁重算"""
        _app()
        c = CardContainer(ContainerType.BOTTOM)
        card = _DynamicCard(h=160)
        c.add_card("card", card)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(c)
        splitter.addWidget(QWidget())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        c.enable_dock_mode(splitter)
        splitter.resize(600, 500)
        splitter.show()
        card.show()
        c.show()
        c._do_expand()
        _pump(300)
        assert c.minimumHeight() >= 160  # 升级后的 min 锁生效

        card.set_height(80)
        _pump(300)
        # 解锁重算后 min 锁回落到新卡片高度附近，不被旧锁卡死
        assert c.minimumHeight() < 160, f"旧锁卡死: min height={c.minimumHeight()}"
        assert c.minimumHeight() >= 80, f"新锁未生效: min height={c.minimumHeight()}"


# ═══════════════════════════════════════════════════════════════
# A2: chat_scroll_area 最小宽 320 + resizeEvent 极小窗口兜底（AST 静态检查）
# ═══════════════════════════════════════════════════════════════


class TestChatScrollAreaMinWidth:
    def _main_widget_src(self) -> str:
        return (_REPO_ROOT / "app" / "main_widget.py").read_text(encoding="utf-8")

    def _resize_event_src(self) -> str:
        src = self._main_widget_src()
        tree = ast.parse(src, filename="main_widget.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "OpenAIChatToolWindow":
                for m in node.body:
                    if isinstance(m, ast.FunctionDef) and m.name == "resizeEvent":
                        return ast.get_source_segment(src, m)
        raise AssertionError("未找到 OpenAIChatToolWindow.resizeEvent")

    def test_initial_min_width_320(self):
        """chat_scroll_area 初始最小宽与卡片渲染下限 320 对齐"""
        assert "self.chat_scroll_area.setMinimumWidth(320)" in self._main_widget_src()

    def test_resize_event_fallback(self):
        """Tab 模式下按窗口总宽 - dock 最小需求让位；无 dock 按自身可用宽"""
        body = self._resize_event_src()
        # Tab 模式：dock splitter 存在时按窗口总宽与 dock min 需求判断
        assert 'findChild(QSplitter, "dockSplitter")' in body
        assert "win_w - dock_min < 320" in body
        # 无 dock（多窗口模式）：按自身可用宽 <320 让位
        assert "self.width() < 320" in body
        assert "setMinimumWidth(target_min_w)" in body
