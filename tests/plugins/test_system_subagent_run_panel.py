# -*- coding: utf-8 -*-
"""system-subagent 插件：运行面板与窗口桥接

覆盖：行增删/完成折叠/历史补行/高度上报；桥接层的 manager 绑定、事件投影、
迁卡补齐、解绑。插件 ui 包按宿主同款方式（spec_from_file_location + 包搜索路径）
加载，保证相对导入路径与生产一致。
"""

import importlib
import importlib.util
import os
import sys

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("qfluentwidgets")

from PyQt5.QtCore import QObject, pyqtSignal  # noqa: E402

_PKG = "system_subagent_ui_pkg"


def _load_ui_pkg():
    root = os.path.abspath(".")
    ui_path = os.path.join(root, "plugins", "system-subagent", "ui")
    init_path = os.path.join(ui_path, "__init__.py")
    if _PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PKG, init_path, submodule_search_locations=[ui_path])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = module
        spec.loader.exec_module(module)
    return (
        importlib.import_module(f"{_PKG}.run_panel"),
        importlib.import_module(f"{_PKG}.run_bridge"),
    )


_run_panel_mod, _run_bridge_mod = _load_ui_pkg()
SubAgentRunPanel = _run_panel_mod.SubAgentRunPanel
SubAgentRunBridge = _run_bridge_mod.SubAgentRunBridge
PANEL_ID = _run_bridge_mod.PANEL_ID


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def panel(qapp):
    p = SubAgentRunPanel()
    yield p
    p.deleteLater()


# ──────────────────── 面板 ────────────────────


def test_panel_hidden_until_first_task(panel):
    assert panel.isVisible() is False
    assert panel.has_task("t1") is False

    panel.add_task("t1", "coder", "写一段代码")

    assert panel.isVisible() is True
    assert panel.has_task("t1") is True


def test_panel_add_task_idempotent(panel):
    panel.add_task("t1", "coder", "任务", model_name="gpt-4")
    panel.add_task("t1", "coder", "任务", model_name="claude")

    assert panel.task_ids() == ["t1"]


def test_panel_finish_all_collapses_but_keeps_rows(panel):
    panel.add_task("t1", "coder", "任务一")
    panel.add_task("t2", "reviewer", "任务二")

    panel.finish_task("t1", success=True)
    assert panel.is_collapsed() is False

    panel.finish_task("t2", success=False)

    assert panel.is_collapsed() is True
    assert sorted(panel.task_ids()) == ["t1", "t2"]
    assert panel.isVisible() is True


def test_panel_add_task_expands_after_completion(panel):
    panel.add_task("t1", "coder", "任务一")
    panel.finish_task("t1", success=True)
    assert panel.is_collapsed() is True

    panel.add_task("t2", "coder", "任务二")

    assert panel.is_collapsed() is False
    assert panel.task_ids() == ["t1", "t2"]


def test_panel_clear_hides_and_drops_rows(panel):
    panel.add_task("t1", "coder", "任务一")
    panel.clear()

    assert panel.task_ids() == []
    assert panel.isVisible() is False


def test_panel_show_completed_task_is_idempotent(panel):
    panel.show_completed_task("t9", "reviewer", "历史任务", tool_call_count=3, elapsed_seconds=75)
    first = panel._rows["t9"]

    panel.show_completed_task("t9", "reviewer", "历史任务")

    assert panel._rows["t9"] is first
    assert first.is_running is False
    assert first._tool_count == 3
    assert first.time_label.text() == "⏱01:15"


def test_panel_emits_height_on_data_change(panel):
    hits = []
    panel.heightChanged.connect(lambda: hits.append(1))

    panel.add_task("t1", "coder", "任务")
    panel.finish_task("t1", success=True)
    panel.clear()

    assert len(hits) >= 3


# ──────────────────── 主题着色 ────────────────────


def test_detail_labels_have_text_color_rule(panel):
    """详情面板四个 label 必须命中 Row 级聚合 QSS 的文字色规则

    回归：这些 label 原先没有 objectName，聚合表里的 color 规则一条都命中不了，
    深色主题下落到应用调色板默认黑字（截图里模型名/调用次数/token/耗时全黑）。
    """
    import re

    from PyQt5.QtGui import QPalette

    from app.utils.design_tokens import Colors

    panel.add_task("t1", "coder", "任务", model_name="gpt-4")
    row = panel._rows["t1"]
    qss = row.styleSheet()
    assert "QLabel#agentDetailLabel" in qss
    assert Colors.REALTIME_TEXT_SECONDARY in qss

    # token 可能是 CSS rgba() 语法（QColor 解析不了），只比 RGB 三通道
    token = Colors.REALTIME_TEXT_SECONDARY
    m = re.search(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", token)
    expected_rgb = tuple(int(g) for g in m.groups()) if m else None

    for label in (
        row._model_label_detail,
        row._tool_label_detail,
        row._ctx_label_detail,
        row._elapsed_label_detail,
    ):
        assert label.objectName() == "agentDetailLabel"
        assert label.styleSheet() == ""  # 默认路径不靠单控件兜底
        got = label.palette().color(QPalette.WindowText)
        if expected_rgb is not None:
            assert (got.red(), got.green(), got.blue()) == expected_rgb
        else:
            assert got.name() == token.lower()


# ──────────────────── 桥接 ────────────────────


class _FakeExecutor(QObject):
    tool_call_started = pyqtSignal(str, str, dict)
    token_usage_updated = pyqtSignal(str, int, int, int)

    def __init__(self, agent_name: str, task_description: str, llm_config=None, execution_error=""):
        super().__init__()
        self.agent_name = agent_name
        self.task_description = task_description
        self.llm_config = llm_config or {}
        self._execution_error = execution_error


class _FakeManager(QObject):
    task_started = pyqtSignal(str, str, str)
    task_finished = pyqtSignal(str, str)
    batch_finished = pyqtSignal(str, int)
    permission_requested = pyqtSignal(str, str, str, dict)

    def __init__(self):
        super().__init__()
        self._running_tasks = {}
        self._finished_tasks = {}


class _FakeSubAgentHost:
    """假窗口宿主：按 window_id 提供管理器 + 记录动作（替代真实 SubAgentHost）。"""

    def __init__(self):
        self.managers = {}
        self.entered = []
        self.stopped = []

    def manager_for(self, window_id: str):
        return self.managers.get(window_id)

    def show_session(self, window_id: str, task_id: str, agent_name: str):
        self.entered.append((task_id, agent_name))

    def stop_task(self, window_id: str, task_id: str):
        self.stopped.append(task_id)


class _FakeHost(QObject):
    """最小宿主窗口：卡片查找（动作记在 subagent 宿主上，与生产同构）。"""

    def __init__(self, card, manager, window_id: str = "w1", sub=None):
        super().__init__()
        self._window_id = window_id
        self._current_assistant_card = card
        self._find_latest_assistant_card = lambda: self._current_assistant_card
        self.subagent = sub or _FakeSubAgentHost()
        self.subagent.managers[window_id] = manager
        self.entered = self.subagent.entered
        self.stopped = self.subagent.stopped


@pytest.fixture()
def card(qapp):
    from app.widgets.message_card import MessageCard

    c = MessageCard(role="assistant")
    yield c
    c.deleteLater()


@pytest.fixture()
def bridge_env(fresh_registry, card):
    """registry + 假宿主 + 假 manager，注册插件面板并连桥。"""
    manager = _FakeManager()
    host = _FakeHost(card, manager)
    fresh_registry._window_main_widgets["w1"] = host
    fresh_registry.register_card_inline_panel("system-subagent", PANEL_ID, SubAgentRunPanel)

    bridge = SubAgentRunBridge(fresh_registry, host.subagent)
    bridge._sync_windows()
    yield bridge, fresh_registry, host, manager, card
    bridge.stop()
    bridge.deleteLater()
    fresh_registry._window_main_widgets.pop("w1", None)


def test_bridge_binds_manager_and_projects_task(bridge_env):
    bridge, _reg, host, manager, card = bridge_env
    executor = _FakeExecutor("coder", "写代码", {"模型名称": "gpt-4o"})
    manager._running_tasks["t1"] = executor

    manager.task_started.emit("t1", "coder", "写代码")

    panel = card.get_inline_panel(PANEL_ID)
    assert panel is not None
    assert panel.has_task("t1") is True
    assert panel._rows["t1"].model_name == "gpt-4o"


def test_bridge_forwards_executor_progress(bridge_env):
    bridge, _reg, host, manager, card = bridge_env
    executor = _FakeExecutor("coder", "写代码")
    manager._running_tasks["t1"] = executor
    manager.task_started.emit("t1", "coder", "写代码")
    panel = card.get_inline_panel(PANEL_ID)

    executor.tool_call_started.emit("t1", "read_file", {})
    executor.token_usage_updated.emit("t1", 100, 200, 1500)

    row = panel._rows["t1"]
    assert row._tool_count == 1
    assert "tokens" in row._context_info


def test_bridge_finishes_task_with_error_state(bridge_env):
    bridge, _reg, host, manager, card = bridge_env
    executor = _FakeExecutor("coder", "写代码")
    manager._running_tasks["t1"] = executor
    manager.task_started.emit("t1", "coder", "写代码")
    panel = card.get_inline_panel(PANEL_ID)

    # 主程序归档槽先跑：pop 出运行表并写归档表
    manager._finished_tasks["t1"] = {"error": "用户已手动中止子智能体"}
    manager._running_tasks.pop("t1")
    manager.task_finished.emit("t1", "中止")

    row = panel._rows["t1"]
    assert row.is_running is False
    assert hasattr(row, "_error_label")


def test_bridge_migrates_rows_to_new_card(bridge_env, qapp):
    bridge, reg, host, manager, card = bridge_env
    manager._running_tasks["t1"] = _FakeExecutor("coder", "写代码")
    manager.task_started.emit("t1", "coder", "写代码")

    from app.widgets.message_card import MessageCard

    new_card = MessageCard(role="assistant")
    host._current_assistant_card = new_card

    # 迁卡：下一个事件（工具调用）触发 attach → 旧卡摘除、新卡补齐
    bridge._on_tool_call("w1", "t1")

    assert card.get_inline_panel(PANEL_ID) is None
    new_panel = new_card.get_inline_panel(PANEL_ID)
    assert new_panel is not None
    assert new_panel.has_task("t1") is True
    assert new_panel._rows["t1"]._tool_count == 1
    new_card.deleteLater()


def test_bridge_forwards_row_actions(bridge_env):
    bridge, _reg, host, manager, card = bridge_env
    manager._running_tasks["t1"] = _FakeExecutor("coder", "写代码")
    manager.task_started.emit("t1", "coder", "写代码")
    panel = card.get_inline_panel(PANEL_ID)

    panel.enterSessionRequested.emit("t1", "coder")
    panel.stopRequested.emit("t1")

    assert host.entered == [("t1", "coder")]
    assert host.stopped == ["t1"]


def test_bridge_stop_detaches_manager(bridge_env):
    bridge, reg, host, manager, card = bridge_env
    manager._running_tasks["t1"] = _FakeExecutor("coder", "写代码")
    manager.task_started.emit("t1", "coder", "写代码")

    bridge.stop()
    manager.task_started.emit("t2", "coder", "第二个")

    assert card.get_inline_panel(PANEL_ID) is None


def test_bridge_keeps_windows_isolated(fresh_registry, qapp):
    """两个窗口各自 manager：任务行不串到对方的卡片上。"""
    from app.widgets.message_card import MessageCard

    card1, card2 = MessageCard(role="assistant"), MessageCard(role="assistant")
    mgr1, mgr2 = _FakeManager(), _FakeManager()
    sub = _FakeSubAgentHost()
    host1, host2 = _FakeHost(card1, mgr1, "w1", sub), _FakeHost(card2, mgr2, "w2", sub)
    fresh_registry._window_main_widgets["w1"] = host1
    fresh_registry._window_main_widgets["w2"] = host2
    fresh_registry.register_card_inline_panel("system-subagent", PANEL_ID, SubAgentRunPanel)

    bridge = SubAgentRunBridge(fresh_registry, sub)
    bridge._sync_windows()
    try:
        mgr1._running_tasks["t1"] = _FakeExecutor("coder", "窗口一")
        mgr1.task_started.emit("t1", "coder", "窗口一")
        mgr2._running_tasks["t2"] = _FakeExecutor("reviewer", "窗口二")
        mgr2.task_started.emit("t2", "reviewer", "窗口二")

        assert card1.get_inline_panel(PANEL_ID).task_ids() == ["t1"]
        assert card2.get_inline_panel(PANEL_ID).task_ids() == ["t2"]
    finally:
        bridge.stop()
        bridge.deleteLater()
        fresh_registry._window_main_widgets.clear()
        card1.deleteLater()
        card2.deleteLater()


# ──────────────────── 插件入口 ────────────────────



def test_register_ui_registers_panel_slot(fresh_registry):
    pkg = sys.modules[_PKG]

    pkg.register_ui(fresh_registry)
    try:
        info = fresh_registry.get_card_inline_panel(PANEL_ID)
        assert info is not None
        assert info.plugin_name == "system-subagent"
        assert info.widget_class is SubAgentRunPanel
        assert info.order == 110
    finally:
        pkg.unload_ui(fresh_registry)

    assert pkg._bridge is None
