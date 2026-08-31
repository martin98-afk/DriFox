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

from PyQt5.QtWidgets import QFrame, QWidget  # noqa: E402

from app.widgets.workbench_panel import (  # noqa: E402
    PANEL_WIDTH_DEFAULT,
    PANEL_WIDTH_MAX,
    PANEL_WIDTH_MIN,
    WorkbenchPanel,
)


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


def test_artifacts_dedup_and_empty(panel):
    ops = [
        {"file_path": "D:/x/a.py", "tool_name": "edit", "created_at": "2026-08-31 13:00:00"},
        {"file_path": "D:/x/b.md", "tool_name": "write", "created_at": "2026-08-31 12:30:00"},
        {"file_path": "D:/x/a.py", "tool_name": "edit", "created_at": "2026-08-31 13:20:00"},
    ]
    panel.update_artifacts(ops)
    page = panel.artifacts_page
    frames = [page._list_layout.itemAt(i).widget() for i in range(page._list_layout.count())]
    frames = [w for w in frames if w is not page._empty_hint and w is not None]
    assert len(frames) == 2  # 按文件路径去重
    assert page._header._extra_label.text() == "2 个文件"
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
    panel.slide_out()
    assert panel.is_sliding
    panel._stop_slide()
    panel.hide()


def test_tasks_pinned_outside_stack(panel):
    # 任务区置顶常驻：不进内容栈，无对应页签
    assert panel._stack.indexOf(panel.tasks_page) == -1
    assert "任务" not in [b.tab_id for b in panel._tab_buttons]


def test_plugin_page_mount_and_unmount(panel):
    from types import SimpleNamespace

    class FakePage(QWidget):
        def __init__(self, parent=None, context=None):
            super().__init__(parent)

    info = SimpleNamespace(page_id="plug1", label="插件页", widget_class=FakePage, plugin_name="x")
    panel.sync_plugin_pages([info])
    assert panel._tab_id_index("plug1") == 2
    assert panel._stack.count() == 3
    assert panel._stack.indexOf(panel._plugin_widgets["plug1"]) == 2
    panel.set_current_tab(2)
    assert panel.current_tab() == 2
    # 卸载后回落
    panel.sync_plugin_pages([])
    assert panel._stack.count() == 2
    assert panel._tab_id_index("plug1") is None
    assert panel.current_tab() == 0


def test_refresh_style_idempotent(panel):
    panel.refresh_style()
    panel.refresh_style()


def test_width_bounds():
    assert PANEL_WIDTH_MIN < PANEL_WIDTH_DEFAULT < PANEL_WIDTH_MAX
