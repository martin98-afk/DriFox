# -*- coding: utf-8 -*-
"""卡片关闭/移除后对话区恢复填充测试（A3 min 锁回归修复）

背景：c8cd2ccc 的 A3 改动在 _do_expand() 非停靠分支展开完成后锁
min=natural_h，依赖"折叠路径开头 _set_axis_min(0) 解锁"。若卡片关闭/
移除时未走折叠路径（如 todo 卡片 closed 信号未连接、remove_card、
直接 setVisible(False)），min 锁残留 → 容器仍占 natural_h 空间 →
对话区（chat_scroll_area）填不回来。

覆盖：
- todo 卡片真实链路：展开 → 点关闭 → 容器折叠 + min 锁释放 + 对话区恢复
- remove_card：展开后移除卡片 → min 锁释放
- 直接 setVisible(False)（如空 todo 列表）：→ 折叠 + min 锁释放
- sub_agent 链路（closed 已连接 hide_card）：回归不破坏
- 回归：展开后 min 锁仍生效（A3 行为不变）
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

from PyQt5.QtCore import QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from app.widgets.cards.card_container import BottomCardContainer, TopCardContainer
from app.widgets.cards.card_manager import CardManager, ContainerType
from app.widgets.cards.floating.todo_floating_widget import TodoFloatingWidget


def _app():
    return QApplication.instance() or QApplication(sys.argv)


def _pump(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class _MiniMainWidget(QWidget):
    """迷你 MainWidget：TopContainer + chat_scroll_area(stretch=1) + BottomContainer"""

    def __init__(self):
        super().__init__()
        self._card_manager = CardManager.get_instance()
        self._window_id = "test_close_restore"
        self._card_manager.register_window(self._window_id)

        self._top_card_container = TopCardContainer()
        self._bottom_card_container = BottomCardContainer()
        self._top_card_container.bind_card_manager(self._card_manager, self._window_id)
        self._bottom_card_container.bind_card_manager(self._card_manager, self._window_id)

        self.chat_scroll_area = QScrollArea(self)
        self.chat_scroll_area.setWidgetResizable(True)
        self.chat_scroll_area.setMinimumHeight(0)
        self.chat_scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)

        lay = QVBoxLayout(self)
        lay.setSpacing(0)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._top_card_container)
        lay.addWidget(self.chat_scroll_area, 1)
        lay.addWidget(self._bottom_card_container)

        # todo 卡片（真实组件）
        self._todo_floating_widget = TodoFloatingWidget(self)
        self._todo_floating_widget.setVisible(False)
        # 模拟 main_widget 修复：连接 closed → hide_card（与 sub_agent 对称）
        self._todo_floating_widget.closed.connect(lambda: self._card_manager.hide_card("todo", self._window_id))
        self._card_manager.register_card(self._window_id, ContainerType.TOP, "todo", self._todo_floating_widget)
        self._top_card_container.add_card("todo", self._todo_floating_widget)


def _setup():
    CardManager.reset_instance()
    _app()
    w = _MiniMainWidget()
    w.resize(600, 500)
    w.show()
    _pump(50)
    return w


# ═══════════════════════════════════════════════════════════════
# todo 真实链路：展开 → 关闭 → 对话区恢复
# ═══════════════════════════════════════════════════════════════


class TestTodoCloseRestore:
    def test_close_button_collapses_container_and_restores_chat(self):
        """todo 关闭按钮：容器折叠 + min 锁释放 + 对话区恢复撑满"""
        w = _setup()
        top = w._top_card_container
        chat = w.chat_scroll_area
        mgr = w._card_manager

        # 展开 todo
        w._todo_floating_widget.update_todos([{"status": "in_progress", "content": "任务A", "priority": "high"}])
        mgr.show_card("todo", w._window_id)
        _pump(350)
        assert top.maximumHeight() >= top._EXPAND_MAX, "todo 展开失败"
        assert top.minimumHeight() > 0, "A3 min 锁未生效"
        expanded_chat_h = chat.height()

        # 模拟用户点关闭按钮（真实 _on_close 路径：setVisible(False) + closed.emit()）
        w._todo_floating_widget._on_close()
        _pump(350)

        assert top.maximumHeight() == 0, f"容器未折叠: maxH={top.maximumHeight()}"
        assert top.minimumHeight() == 0, f"min 锁残留: minH={top.minimumHeight()}"
        assert top.height() == 0, f"容器仍占空间: h={top.height()}"
        assert chat.height() > expanded_chat_h, f"对话区未恢复: chat={chat.height()} 展开时={expanded_chat_h}"
        assert not mgr.is_card_visible("todo", w._window_id) or not w._todo_floating_widget.isVisible()

    def test_direct_set_visible_false_releases_min_lock(self):
        """直接 setVisible(False)（如空 todo 列表自动隐藏）：min 锁释放"""
        w = _setup()
        top = w._top_card_container
        w._todo_floating_widget.update_todos([{"status": "in_progress", "content": "任务A", "priority": "high"}])
        w._card_manager.show_card("todo", w._window_id)
        _pump(350)
        assert top.minimumHeight() > 0

        w._todo_floating_widget.setVisible(False)
        _pump(350)
        assert top.minimumHeight() == 0, f"min 锁残留: minH={top.minimumHeight()}"
        assert top.maximumHeight() == 0, f"容器未折叠: maxH={top.maximumHeight()}"

    def test_remove_card_releases_min_lock(self):
        """remove_card 移除卡片：min 锁释放，容器不占空间"""
        w = _setup()
        top = w._top_card_container
        w._todo_floating_widget.update_todos([{"status": "in_progress", "content": "任务A", "priority": "high"}])
        w._card_manager.show_card("todo", w._window_id)
        _pump(350)
        assert top.minimumHeight() > 0

        top.remove_card("todo")
        _pump(350)  # timer 折叠 + 200ms 动画
        assert top.minimumHeight() == 0, f"remove_card 后 min 锁残留: minH={top.minimumHeight()}"
        assert top.maximumHeight() == 0, f"remove_card 后容器未折叠: maxH={top.maximumHeight()}"


# ═══════════════════════════════════════════════════════════════
# 回归：展开后 min 锁仍生效（A3 行为不变）
# ═══════════════════════════════════════════════════════════════


class TestExpandRegression:
    def test_expand_still_locks_min_to_natural(self):
        """A3 行为回归：展开后 min 锁仍生效（窗口缩小时卡片不被压缩）"""
        w = _setup()
        top = w._top_card_container
        w._todo_floating_widget.update_todos([{"status": "in_progress", "content": "任务A", "priority": "high"}])
        w._card_manager.show_card("todo", w._window_id)
        _pump(350)
        assert top.minimumHeight() > 0, "展开后 min 锁应生效"

    def test_close_then_reopen(self):
        """关闭后可再次展开（min 锁释放后二次展开正常）"""
        w = _setup()
        top = w._top_card_container
        mgr = w._card_manager
        w._todo_floating_widget.update_todos([{"status": "in_progress", "content": "任务A", "priority": "high"}])
        mgr.show_card("todo", w._window_id)
        _pump(350)

        w._todo_floating_widget._on_close()
        _pump(350)
        assert top.minimumHeight() == 0

        # 再次显示
        w._todo_floating_widget.update_todos([{"status": "in_progress", "content": "任务B", "priority": "high"}])
        mgr.show_card("todo", w._window_id)
        _pump(350)
        assert top.maximumHeight() >= top._EXPAND_MAX, "二次展开失败"
        assert top.minimumHeight() > 0, "二次展开后 min 锁未生效"
