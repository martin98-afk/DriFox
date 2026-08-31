# -*- coding: utf-8 -*-
"""WorkbenchPanel（右侧工作台浮层）回归测试

覆盖：任务坞常驻/进度/空态、产物去重/空态、双页签切换、滑入滑出动画接口、
主题刷新、宽度边界。纯离屏（offscreen）运行，不依赖真实显示环境。
"""
import os
import sys

import pytest

sys.path.insert(0, ".")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QWidget  # noqa: E402

from app.widgets.workbench_panel import (  # noqa: E402
    PANEL_WIDTH_DEFAULT,
    PANEL_WIDTH_MAX,
    PANEL_WIDTH_MIN,
    WorkbenchPanel,
)


@pytest.fixture(scope="module")
def qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def panel(qapp):
    host = QWidget()
    host.resize(1200, 800)
    host.show()  # 顶层窗口 isVisible 依赖父链可见
    p = WorkbenchPanel(host)
    p.setWindowFlags(p.windowFlags())  # 与宿主用法一致（默认 flags 下行为等价）
    yield p
    p.setParent(None)
    p.deleteLater()
    host.deleteLater()


def test_default_width(panel):
    assert panel.width() == PANEL_WIDTH_DEFAULT


def test_tasks_dock_hidden_when_empty(panel):
    assert not panel.tasks_dock.isVisibleTo(panel)
    todos = [
        {"id": "1", "content": "A", "status": "completed", "priority": "high"},
        {"id": "2", "content": "B", "status": "in_progress", "priority": "medium"},
        {"id": "3", "content": "C", "status": "pending", "priority": "low"},
    ]
    panel.update_todos(todos)
    assert panel.tasks_dock.isVisibleTo(panel)
    assert panel.tasks_dock._header._extra_label.text() == "1/3 已完成"
    panel.update_todos([])
    assert not panel.tasks_dock.isVisibleTo(panel)
    assert panel.tasks_dock._header._extra_label.text() == ""


def test_tasks_dock_collapse(panel):
    panel.update_todos([{"id": "1", "content": "A", "status": "pending", "priority": "low"}])
    assert panel.tasks_dock._scroll.isVisibleTo(panel.tasks_dock)
    panel.tasks_dock._toggle()  # 折叠
    assert not panel.tasks_dock._scroll.isVisibleTo(panel.tasks_dock)
    panel.tasks_dock._toggle()  # 展开
    assert panel.tasks_dock._scroll.isVisibleTo(panel.tasks_dock)


def test_artifacts_dedup_and_empty(panel):
    ops = [
        {"file_path": "D:/x/a.py", "tool_name": "edit", "created_at": "2026-08-31 13:00:00"},
        {"file_path": "D:/x/b.md", "tool_name": "write", "created_at": "2026-08-31 12:30:00"},
        {"file_path": "D:/x/a.py", "tool_name": "edit", "created_at": "2026-08-31 13:20:00"},
    ]
    panel.update_artifacts(ops)
    page = panel.artifacts_page
    frames = [
        page._list_layout.itemAt(i).widget()
        for i in range(page._list_layout.count())
    ]
    frames = [w for w in frames if w is not page._empty_hint and w is not None]
    assert len(frames) == 2  # 按文件路径去重
    assert page._header._extra_label.text() == "2 个文件"
    # 最新在前 + 条目在 stretch 之前（贴顶，不被 stretch 压底）
    assert frames[0]._file_path.endswith("a.py")
    last = page._list_layout.itemAt(page._list_layout.count() - 1)
    assert last.widget() is None  # 末尾是 stretch
    panel.update_artifacts([])
    assert page._header._extra_label.text() == ""


def test_two_tabs_switch(panel):
    assert panel.current_tab() == WorkbenchPanel.TAB_ARTIFACTS
    panel.set_current_tab(WorkbenchPanel.TAB_MEMORY)
    assert panel.current_tab() == WorkbenchPanel.TAB_MEMORY
    assert panel._stack.currentIndex() == WorkbenchPanel.TAB_MEMORY
    panel.set_current_tab(WorkbenchPanel.TAB_ARTIFACTS)
    assert panel._stack.currentIndex() == WorkbenchPanel.TAB_ARTIFACTS


def test_slide_animation_lifecycle(panel):
    panel.show()
    panel.slide_in()
    assert panel.is_sliding
    panel._stop_slide()
    assert not panel.is_sliding
    # 模拟滑入完成态（贴主窗口右缘内），再滑出才有动画距离
    panel.move(720, 0)
    panel.slide_out()
    assert panel.is_sliding
    panel._stop_slide()
    panel.hide()


def test_refresh_style_idempotent(panel):
    panel.refresh_style()
    panel.refresh_style()


def test_width_bounds():
    assert PANEL_WIDTH_MIN < PANEL_WIDTH_DEFAULT < PANEL_WIDTH_MAX
