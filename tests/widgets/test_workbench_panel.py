# -*- coding: utf-8 -*-
"""WorkbenchPanel（右侧工作台浮层）回归测试

覆盖：任务坞常驻/进度/空态、产物页（插件化）/去重/空态、双页签切换、
滑入滑出动画接口、主题刷新、宽度边界、WA_NativeWindow 穿透根治、
模态避让、产物页插件槽位替换。纯离屏（offscreen）运行，不依赖真实显示环境。

★ 产物页已完全插件化：面板不内置产物实现，测试统一加载系统插件的
``SystemArtifactsPage``（plugins/system/ui/_artifacts_page.py）作为产物页注入。
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QFrame, QLabel, QWidget  # noqa: E402

from app.widgets.workbench_panel import (  # noqa: E402
    PANEL_WIDTH_DEFAULT,
    PANEL_WIDTH_MAX,
    PANEL_WIDTH_MIN,
    WorkbenchPanel,
)

# 系统插件 ui 目录（产物页实现所在）
_SYSTEM_UI_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "system" / "ui"


def _load_system_artifacts_cls():
    """加载系统插件的产物页类 SystemArtifactsPage

    plugins/system/ui 不在包路径上（由 UIPluginRegistry 动态注入 sys.path），
    测试里显式用 importlib 加载。
    """
    ui_dir = str(_SYSTEM_UI_DIR)
    if ui_dir not in sys.path:
        sys.path.insert(0, ui_dir)
    spec = importlib.util.spec_from_file_location(
        "_test_system_artifacts_page", _SYSTEM_UI_DIR / "_artifacts_page.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SystemArtifactsPage


@pytest.fixture()
def system_artifacts_cls():
    return _load_system_artifacts_cls()


@pytest.fixture()
def artifacts_plugins(system_artifacts_cls):
    """构造"系统插件已注册产物页"的 tabs 列表（供 sync_plugin_pages 使用）"""
    from types import SimpleNamespace

    return [
        SimpleNamespace(
            page_id="artifacts",
            label="产物",
            widget_class=system_artifacts_cls,
            plugin_name="system",
        )
    ]


@pytest.fixture()
def panel(qapp):
    host = QWidget()
    host.resize(1200, 800)
    host.show()  # 顶层窗口 isVisible 依赖父链可见
    p = WorkbenchPanel(host)
    p.setWindowFlags(p.windowFlags())  # 与宿主用法一致（默认 flags 下行为等价）
    p.show()  # 让子 widget 的 isVisible 依赖父链可见（部分用例依赖）
    yield p
    p.setParent(None)
    p.deleteLater()
    host.deleteLater()


@pytest.fixture()
def panel_with_artifacts(panel, artifacts_plugins):
    """已注入系统插件产物页的工作台（产物页行为类用例用）"""
    panel.sync_plugin_pages(artifacts_plugins)
    yield panel


def test_width_bounds_applied(panel):
    """嵌入式：宽度交给外层 splitter 拖拽，panel 自身只给 min/max 约束"""
    assert panel.minimumWidth() == PANEL_WIDTH_MIN
    assert panel.maximumWidth() == PANEL_WIDTH_MAX
    assert PANEL_WIDTH_MIN < PANEL_WIDTH_DEFAULT < PANEL_WIDTH_MAX


def test_artifacts_dedup_and_empty(panel_with_artifacts):
    """产物页（系统插件版）：按文件路径去重 + 空态"""
    panel = panel_with_artifacts
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


def test_visible_toggle_is_immediate(panel):
    """显隐是直接的 show/hide —— 用户要求不做折叠动画"""
    panel.set_panel_visible(True)
    assert panel.is_panel_visible() is True
    assert panel.isVisible()
    panel.set_panel_visible(False)
    assert panel.is_panel_visible() is False
    assert not panel.isVisible()
    # 无动画残留：不应存在滑入滑出动画接口
    assert not hasattr(panel, "slide_in")
    assert not hasattr(panel, "slide_out")


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


# ── 新行为：任务区 splitter / 折叠按钮 / 类任务清单样式 ──


def test_splitter_present_and_tasks_outside_stack(panel):
    """QSplitter 应已挂载，任务区仍不在 stack 中（置顶常驻）"""
    assert panel._body_splitter is not None
    assert panel._stack.indexOf(panel.tasks_page) == -1


def test_tasks_collapse_button_hidden_when_empty(panel):
    """无任务时折叠按钮不显示（避免视觉噪声）"""
    assert panel.tasks_page._collapse_btn.isVisible() is False
    panel.update_todos([])
    assert panel.tasks_page._collapse_btn.isVisible() is False


def test_tasks_show_when_populated(panel):
    """有任务时 tasks_page 可见，整区出现并显示折叠按钮"""
    panel.update_todos([
        {"status": "pending", "content": "搞事情", "priority": "high"},
        {"status": "in_progress", "content": "写代码", "priority": "medium"},
        {"status": "completed", "content": "提交", "priority": "low"},
    ])
    assert panel.tasks_page.isVisible() is True
    assert panel.tasks_page._collapse_btn.isVisible() is True


def test_tasks_hide_when_cleared(panel):
    """任务清空后 tasks_page 应隐藏，splitter 上半高度收敛为 0"""
    panel.update_todos([{"status": "pending", "content": "临时任务", "priority": "low"}])
    assert panel.tasks_page.isVisible() is True
    panel.update_todos([])
    assert panel.tasks_page.isVisible() is False
    # splitter 上半高度应已收敛
    sizes = panel._body_splitter.sizes()
    assert sizes[0] == 0


def test_tasks_collapse_toggle(panel):
    """折叠按钮可切换 _scroll 显隐"""
    panel.update_todos([{"status": "pending", "content": "X", "priority": "medium"}])
    assert panel.tasks_page._scroll.isVisible() is True
    panel.tasks_page._collapse_btn.click()
    assert panel.tasks_page._scroll.isVisible() is False
    panel.tasks_page._collapse_btn.click()
    assert panel.tasks_page._scroll.isVisible() is True


def _task_items(page):
    """取出任务列表中的条目 widget（按 objectName 精确过滤）"""
    items = [page._list_layout.itemAt(i).widget() for i in range(page._list_layout.count())]
    return [w for w in items if w is not None and w.objectName() == "taskItem"]


def test_tasks_tag_only_when_worth_showing(panel):
    """右侧标签只在噪声最值得暴露时出现：in_progress「进行中」、pending+high「高」

    medium/low 的 pending 与 completed 一律不带标签（降噪）。
    """
    panel.update_todos([
        {"status": "pending", "content": "A", "priority": "high"},
        {"status": "in_progress", "content": "B", "priority": "medium"},
        {"status": "pending", "content": "C", "priority": "medium"},
        {"status": "completed", "content": "D", "priority": "low"},
    ])
    items = _task_items(panel.tasks_page)
    assert len(items) == 4
    tags = [it.findChild(QLabel, "taskTag") for it in items]
    assert tags[0] is not None and tags[0].text() == "高"
    assert tags[1] is not None and tags[1].text() == "进行中"
    assert tags[2] is None
    assert tags[3] is None


def test_tasks_no_stale_widget_after_refresh(panel):
    """★ 回归：刷新后不得残留旧条目

    旧实现清理时只 deleteLater() 未 setParent(None)，旧 widget 在事件循环
    处理前仍挂 parent 继续绘制 → 新列表第一项被上一次的最后一项残影覆盖。
    """
    page = panel.tasks_page
    panel.update_todos([{"status": "pending", "content": "第一版任务", "priority": "medium"}])
    assert len(_task_items(page)) == 1

    # 刷新为不同的任务
    panel.update_todos([
        {"status": "pending", "content": "新任务1", "priority": "medium"},
        {"status": "pending", "content": "新任务2", "priority": "medium"},
    ])
    items = _task_items(page)
    assert len(items) == 2, f"应恰好 2 条，实际 {len(items)}（含残影则说明未彻底清理）"
    texts = [it.findChild(QLabel, "taskContent").text() for it in items]
    assert texts == ["新任务1", "新任务2"], f"顺序/内容异常: {texts}"
    # 旧条目必须已从 parent 摘除（不再挂 children 链）
    # 注意：QLabel 继承自 QFrame，故必须按 objectName 精确过滤，
    # 否则会把 _EmptyHint（QLabel 子类）误判成残留条目。
    stale = [
        w
        for w in page._list_wrap.children()
        if isinstance(w, QFrame) and w.objectName() == "taskItem" and w not in items
    ]
    assert not stale, f"存在未摘除的残留条目: {stale}"


def test_tasks_collapse_shrinks_splitter(panel):
    """★ 折叠后 splitter 上半应收缩到 header 高度（否则留空白、title 跑中间）"""
    panel.update_todos([{"status": "pending", "content": "A", "priority": "medium"}])
    expanded_h = panel._body_splitter.sizes()[0]
    assert expanded_h >= panel.tasks_page.header_height()

    panel.tasks_page._collapse_btn.click()
    collapsed_h = panel._body_splitter.sizes()[0]
    assert collapsed_h < expanded_h, f"折叠后高度应收缩: {collapsed_h} vs {expanded_h}"
    assert collapsed_h <= panel.tasks_page.header_height() + 2

    # 展开恢复
    panel.tasks_page._collapse_btn.click()
    assert panel._body_splitter.sizes()[0] == expanded_h


def test_tasks_progress_bar_reflects_done(panel):
    """进度条应反映完成比例"""
    page = panel.tasks_page
    panel.update_todos([
        {"status": "completed", "content": "A", "priority": "medium"},
        {"status": "completed", "content": "B", "priority": "medium"},
        {"status": "pending", "content": "C", "priority": "medium"},
        {"status": "pending", "content": "D", "priority": "medium"},
    ])
    assert page._progress.value() == 50
    assert page._header._extra_label.text() == "2/4"
    panel.update_todos([])
    assert page._progress.value() == 0
    assert page._header._extra_label.text() == ""


# ── 产物页差异入口 + panel.diff_requested 信号（产物页由插件提供） ──


def test_artifacts_diff_all_button_emits_signal(panel_with_artifacts):
    """产物页 header 的「查看所有产物差异」按钮应触发 panel.diff_requested(None)"""
    panel = panel_with_artifacts
    panel.update_artifacts([
        {"file_path": "D:/x/a.py", "tool_name": "edit", "created_at": "2026-08-31 13:00:00"},
    ])
    captured = []
    panel.diff_requested.connect(lambda p: captured.append(p))
    panel.artifacts_page._header._action_btn.click()
    assert captured == [None]


def test_artifacts_item_diff_emits_signal(panel_with_artifacts):
    """单条目「差异」按钮应触发 panel.diff_requested([file_path])"""
    panel = panel_with_artifacts
    panel.update_artifacts([
        {"file_path": "D:/x/b.md", "tool_name": "write", "created_at": "2026-08-31 12:30:00"},
    ])
    captured = []
    panel.diff_requested.connect(lambda p: captured.append(p))
    page = panel.artifacts_page
    frames = [page._list_layout.itemAt(i).widget() for i in range(page._list_layout.count())]
    items = [w for w in frames if isinstance(w, QFrame)]
    assert items
    items[0]._diff_btn.click()
    assert captured == [["D:/x/b.md"]]


def test_artifacts_diff_all_button_hidden_when_empty(panel_with_artifacts):
    """无产物时「查看所有产物差异」按钮应隐藏"""
    panel = panel_with_artifacts
    panel.update_artifacts([])
    action_btn = panel.artifacts_page._header._action_btn
    assert action_btn is None or not action_btn.isVisible()


def test_artifacts_placeholder_before_plugin_registered(panel):
    """插件未注册产物页时，index 0 应是占位页（面板不再内置产物实现）"""
    assert panel.artifacts_page is panel._artifacts_placeholder
    # 占位页有宿主契约所需的空实现，update_artifacts 不会抛错
    panel.update_artifacts([{"file_path": "D:/x/a.py"}])


# ── 嵌入式形态约束（悬浮方案已废弃） ──


def test_panel_not_native_window(panel):
    """★ 嵌入式：panel **不能**是原生窗口

    悬浮期曾用 WA_NativeWindow 盖住 QWebEngineView，但原生 HWND 会吞掉主窗口
    边缘的 WM_NCHITTEST → 主窗口边框无法 resize，且面板内点击命中异常。
    改为嵌入式（与对话区并列不重叠）后不再需要原生窗口。
    """
    assert not panel.testAttribute(Qt.WA_NativeWindow), (
        "嵌入式 WorkbenchPanel 不应是原生窗口（会吞掉主窗口边缘 resize 命中）"
    )
    # 必须是普通 child widget（由外层 #workbenchFrame 容器 + splitter 管理几何）
    assert panel.parentWidget() is not None
    assert not panel.isWindow()


def test_panel_has_no_raise_timer(panel):
    """嵌入式不需要 z-order 维持定时器（无遮挡问题）"""
    assert not hasattr(panel, "_raise_timer")
    assert not hasattr(panel, "_keep_on_top")


def test_panel_transparent_background(panel):
    """嵌入式：背景透明，由外层 #workbenchFrame 圆角矩形提供背景"""
    assert "transparent" in panel.styleSheet()


def test_no_modal_suspend_needed(panel):
    """嵌入式与 MaskDialog 不重叠，无需模态避让逻辑"""
    assert not hasattr(panel, "_suspended_by_modal")
    assert not hasattr(panel, "_wanted_visible")


# ── 产物页槽位（page_id="artifacts" 是保留 id） ──


def test_plugin_artifacts_fills_slot_no_duplicate_tab(panel, artifacts_plugins):
    """插件用 page_id="artifacts" 注册时，应填产物页槽位而非新增第二个 tab"""
    before_tab_count = len(panel._tab_buttons)
    panel.sync_plugin_pages(artifacts_plugins)
    # tab 数量不变（填槽位而非新增），artifacts 只出现一次
    assert len(panel._tab_buttons) == before_tab_count
    tab_ids = [b.tab_id for b in panel._tab_buttons]
    assert tab_ids.count("artifacts") == 1, f"artifacts tab 应只有一个，实际: {tab_ids}"
    # panel.artifacts_page 指向插件版
    assert panel.artifacts_page is panel._plugin_artifacts_widget


def test_plugin_artifacts_keeps_stack_index_aligned(panel, artifacts_plugins):
    """★ 回归：填槽位后 stack 索引仍与 tab 顺序一致（0=产物 / 1=记忆）

    早期实现用 insertWidget(0, w) 会把原 index 0 及其后所有页整体后移，
    导致 TAB_MEMORY(=1) 落到产物页上 —— 表现为「记忆」tab 显示出产物内容。
    """
    panel.sync_plugin_pages(artifacts_plugins)
    assert panel._stack.indexOf(panel.artifacts_page) == WorkbenchPanel.TAB_ARTIFACTS
    assert panel._stack.indexOf(panel.memory_page) == WorkbenchPanel.TAB_MEMORY
    assert panel._stack.count() == 2
    # 切到记忆页，确认拿到的确实是记忆页
    panel.set_current_tab(WorkbenchPanel.TAB_MEMORY)
    assert panel._stack.currentWidget() is panel.memory_page


def test_plugin_artifacts_unload_falls_back_to_placeholder(panel, artifacts_plugins):
    """插件卸载（不再注册 artifacts）后，应回落到占位页"""
    panel.sync_plugin_pages(artifacts_plugins)
    assert panel.artifacts_page is panel._plugin_artifacts_widget

    panel.sync_plugin_pages([])  # 插件卸载
    assert panel.artifacts_page is panel._artifacts_placeholder
    tab_ids = [b.tab_id for b in panel._tab_buttons]
    assert tab_ids.count("artifacts") == 1
    # 索引仍对齐
    assert panel._stack.indexOf(panel.memory_page) == WorkbenchPanel.TAB_MEMORY


def test_other_plugin_page_id_still_appended(panel):
    """非 artifacts 的 page_id 仍作为新 tab 追加（不被当产物页填槽）"""
    from types import SimpleNamespace

    class FakePage(QWidget):
        def __init__(self, parent=None, context=None):
            super().__init__(parent)

    info = SimpleNamespace(page_id="mystats", label="统计", widget_class=FakePage, plugin_name="x")
    panel.sync_plugin_pages([info])
    tab_ids = [b.tab_id for b in panel._tab_buttons]
    assert tab_ids == ["artifacts", "memory", "mystats"], f"实际 tab: {tab_ids}"
    assert panel.artifacts_page is panel._artifacts_placeholder


# ── 新行为：系统插件 register_workbench_tab 通道（导入校验） ──


def test_system_plugin_ui_module_importable():
    """plugins/system/ui/__init__.py 应可被 import 且暴露 register_ui 函数"""
    import importlib.util
    from pathlib import Path

    ui_init = Path("plugins/system/ui/__init__.py").resolve()
    assert ui_init.exists(), f"system plugin ui module missing: {ui_init}"
    spec = importlib.util.spec_from_file_location("system_ui_test", ui_init)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(getattr(module, "register_ui", None))
