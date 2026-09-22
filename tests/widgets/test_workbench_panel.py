# -*- coding: utf-8 -*-
"""WorkbenchPanel（右侧工作台）回归测试 — 零保留槽位 / 插件页模型

★ 面板对页面**零语义**：不内置任何页实现、不保留 page_id 槽位，全部页由插件
通过 ``register_workbench_tab`` 注册，顺序 = ``(metadata["order_hint"], 注册序)``，
默认落点 = ``metadata["default_landing"]`` 标记页。页签一律按 tab_id 定位。

覆盖：任务坞常驻/进度/空态、插件页 mount/unmount、order_hint 排序、默认落点、
tab_id 定位、通用 ``refresh_data()`` 协议、卡片页签追加、主题刷新、宽度边界、
WA_NativeWindow 穿透根治、空态页。纯离屏（offscreen）运行。

产物页/历史页实现分别在 ``plugins/artifacts-manager`` / ``plugins/history-manager``，
其自身行为由各自插件测试覆盖；本文件只验证面板的通用装配语义。
"""

import os
import sys
from types import SimpleNamespace

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

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class _DummyPage(QWidget):
    """最小插件页替身：带面板可选协议（refresh_style / refresh_data）"""

    def __init__(self, parent=None, context=None):
        super().__init__(parent)
        self._context = context or {}
        self.style_calls = 0
        self.data_calls = 0

    def refresh_style(self) -> None:
        self.style_calls += 1

    def refresh_data(self) -> None:
        self.data_calls += 1


class _NoProtocolPage(QWidget):
    """无 refresh_style / refresh_data 的裸页（面板不得因缺失协议报错）"""

    def __init__(self, parent=None, context=None):
        super().__init__(parent)
        self._context = context or {}


def _info(page_id: str, label: str, widget_class=_DummyPage, order_hint: int = 0, default: bool = False):
    meta = {"order_hint": order_hint}
    if default:
        meta["default_landing"] = True
    return SimpleNamespace(
        page_id=page_id,
        label=label,
        widget_class=widget_class,
        plugin_name="test",
        metadata=meta,
    )


@pytest.fixture()
def panel(qapp):
    host = QWidget()
    host.resize(1200, 800)
    host.show()  # 顶层窗口 isVisible 依赖父链可见
    p = WorkbenchPanel(host)
    p.show()
    yield p
    p.setParent(None)
    p.deleteLater()
    host.deleteLater()


# ── 基本几何 / 形态 ──


def test_width_bounds_applied(panel):
    """嵌入式：宽度交给外层 splitter 拖拽，panel 自身只给 min/max 约束"""
    assert panel.minimumWidth() == PANEL_WIDTH_MIN
    assert panel.maximumWidth() == PANEL_WIDTH_MAX
    assert PANEL_WIDTH_MIN < PANEL_WIDTH_DEFAULT < PANEL_WIDTH_MAX


def test_panel_transparent_background(panel):
    assert "background: transparent" in panel.styleSheet()


def test_panel_not_native_window(panel):
    """WA_NativeWindow 会吞 WM_NCHITTEST（主窗口无法 resize），必须未设置"""
    assert not panel.testAttribute(Qt.WA_NativeWindow)


def test_panel_has_no_raise_timer(panel):
    """嵌入式方案不需要 raise 定时器（悬浮方案的历史包袱）"""
    assert not hasattr(panel, "_raise_timer")


def test_refresh_style_idempotent(panel):
    panel.refresh_style()
    panel.refresh_style()
    panel.refresh_theme()  # ThemeManager 协议入口


# ── 空态 / 插件页装配 ──


def test_empty_state_has_no_tabs(panel):
    """未注册任何插件页：无页签，stack 内为空态页"""
    assert panel._tab_ids == []
    assert panel.default_page_id() is None
    assert panel._stack.indexOf(panel._empty_page) == 0


def test_plugin_pages_ordered_by_order_hint(panel):
    """页序 = (order_hint, 注册序)：注册顺序与展示顺序解耦"""
    panel.sync_plugin_pages([_info("c", "C页", order_hint=30), _info("a", "A页", order_hint=0), _info("b", "B页", order_hint=10)])
    assert panel._tab_ids == ["a", "b", "c"]
    # stack 顺序与页签顺序严格一致
    for idx, pid in enumerate(["a", "b", "c"]):
        assert panel._stack.indexOf(panel._plugin_widgets[pid]) == idx


def test_default_landing_prefers_flag_then_first(panel):
    panel.sync_plugin_pages([_info("a", "A页", order_hint=0), _info("b", "B页", order_hint=10, default=True)])
    assert panel.default_page_id() == "b"
    # 无显式标记时回落顺序第一页
    panel.sync_plugin_pages([_info("a", "A页", order_hint=0), _info("b", "B页", order_hint=10)])
    assert panel.default_page_id() == "a"


def test_plugin_page_mount_and_unmount(panel):
    panel.sync_plugin_pages([_info("plug1", "插件页", order_hint=0)])
    assert panel._tab_id_index("plug1") == 0
    # 注销 → 页销毁，回落空态
    panel.sync_plugin_pages([])
    assert "plug1" not in panel._plugin_widgets
    assert panel._tab_ids == []


def test_mount_injects_context_and_parent(panel):
    seen = {}

    class _P(QWidget):
        def __init__(self, parent=None, context=None):
            super().__init__(parent)
            seen["parent"] = parent
            seen["context"] = context

    panel.sync_plugin_pages([_info("ctx", "上下文页", widget_class=_P)])
    assert seen["parent"] is panel._stack
    assert "diff_requested_callback" in seen["context"]


def test_page_without_protocol_is_tolerated(panel):
    """页面缺 refresh_style / refresh_data 时面板不得抛错"""
    panel.sync_plugin_pages([_info("bare", "裸页", widget_class=_NoProtocolPage)])
    panel.refresh_style()
    panel.refresh_current_page_data()


# ── tab_id 定位 ──


def test_set_current_tab_by_id(panel):
    panel.sync_plugin_pages([_info("a", "A页", order_hint=0), _info("b", "B页", order_hint=10)])
    assert panel.current_tab_id() == "a"
    assert panel.set_current_tab_by_id("b") is True
    assert panel.current_tab_id() == "b"
    # 未知 id：不切页
    assert panel.set_current_tab_by_id("missing") is False
    assert panel.current_tab_id() == "b"


def test_set_current_tab_out_of_range_keeps_highlight(panel):
    panel.sync_plugin_pages([_info("a", "A页", order_hint=0)])
    panel.set_current_tab_by_id("a")
    panel.set_current_tab(99)  # 越界：不强行跳页，高亮与 stack 对齐
    assert panel.current_tab_id() == "a"
    assert [b._active for b in panel._tab_buttons] == [True]


# ── 通用数据刷新协议 ──


def test_refresh_current_page_data_calls_page_protocol(panel):
    panel.sync_plugin_pages([_info("a", "A页", order_hint=0), _info("b", "B页", order_hint=10)])
    page_a = panel._plugin_widgets["a"]
    page_b = panel._plugin_widgets["b"]
    panel.set_current_tab_by_id("a")
    before = page_a.data_calls
    panel.refresh_current_page_data()
    assert page_a.data_calls == before + 1
    assert page_b.data_calls == before  # 非当前页不刷新


def test_switch_tab_triggers_page_refresh(panel):
    panel.sync_plugin_pages([_info("a", "A页", order_hint=0), _info("b", "B页", order_hint=10)])
    page_b = panel._plugin_widgets["b"]
    assert page_b.data_calls == 0
    panel.set_current_tab_by_id("b")
    assert page_b.data_calls == 1  # 切页自动触发当前页自拉


def test_force_rebuild_recreates_pages(panel):
    """force=True（热重载）：销毁并重建页实例"""
    panel.sync_plugin_pages([_info("a", "A页", order_hint=0)])
    first = panel._plugin_widgets["a"]
    panel.sync_plugin_pages([_info("a", "A页", order_hint=0)], force=True)
    assert panel._plugin_widgets["a"] is not first


# ── 卡片页签（right 容器 UI 插件卡片） ──


def test_card_tab_appended_after_plugin_pages(panel):
    panel.sync_plugin_pages([_info("a", "A页", order_hint=0), _info("b", "B页", order_hint=10)])
    card = QWidget()
    panel.open_card_tab("my-card", "卡片页", card)
    assert panel._tab_ids == ["a", "b", "my-card"]
    assert panel._tab_id_index("my-card") == 2


def test_card_tab_close_signal(panel):
    card = QWidget()
    panel.open_card_tab("my-card", "卡片页", card)
    seen = []
    panel.card_tab_close_requested.connect(seen.append)
    # 关闭钮点击 → 面板发信号（清理归 registry）
    assert panel.has_card_tab("my-card")
    panel.close_card_tab("my-card")
    assert not panel.has_card_tab("my-card")
    assert seen == []


# ── 任务坞（窗口级，不进页签） ──


def test_tasks_pinned_outside_stack(panel):
    assert panel._stack.indexOf(panel.tasks_page) == -1


def test_tasks_hide_when_empty_and_show_when_populated(panel):
    panel.update_todos([])
    assert panel.tasks_page.isVisible() is False
    panel.update_todos([{"content": "任务一", "status": "pending", "priority": "medium"}])
    assert panel.tasks_page.isVisible() is True


def test_tasks_progress_bar_reflects_done(panel):
    panel.update_todos(
        [
            {"content": "a", "status": "completed", "priority": "medium"},
            {"content": "b", "status": "pending", "priority": "medium"},
        ]
    )
    assert panel.tasks_page._progress.value() == 50


def test_tasks_stat_label_shows_done_over_total(panel):
    """任务区统计标签显示百分比 + done/total（单条任务时为 0% · 0/1）"""
    panel.update_todos([{"content": "a", "status": "pending", "priority": "medium"}])
    assert panel.tasks_page._header._extra_label.text() == "0% · 0/1"


def _task_items(panel):
    """取当前任务条目（objectName=taskItem 的 QFrame 列表）"""
    lay = panel.tasks_page._list_layout
    return [
        w
        for i in range(lay.count())
        if isinstance(w := lay.itemAt(i).widget(), QFrame) and w.objectName() == "taskItem"
    ]


def _running_items(panel):
    """取进行中常驻条条目（脱离列表的置顶区）"""
    lay = panel.tasks_page._running_layout
    return [
        w
        for i in range(lay.count())
        if isinstance(w := lay.itemAt(i).widget(), QFrame) and w.objectName() == "taskItem"
    ]


def test_tasks_item_single_line_elided_with_full_tooltip(panel):
    """任务条目单行省略（不换行撑高），全文进 tooltip"""
    long_text = "很长的任务描述" * 20
    panel.update_todos([{"content": long_text, "status": "pending", "priority": "medium"}])
    items = _task_items(panel)
    assert len(items) == 1
    label = items[0].findChild(QLabel, "taskContent")
    assert type(label).__name__ == "_ElidedLabel"
    # 单一 tooltip：优先级 + 全文合一
    assert label.toolTip().endswith(long_text)
    assert "优先级" in label.toolTip()


def test_tasks_in_progress_emphasized_with_accent_bar(panel):
    """仅置顶条带琥珀重点样式；列表条目一律普通行（样式不受置顶影响）"""
    panel.update_todos(
        [
            {"content": "done", "status": "completed", "priority": "medium"},
            {"content": "doing", "status": "in_progress", "priority": "high"},
            {"content": "todo", "status": "pending", "priority": "low"},
        ]
    )
    # 置顶为额外快照，列表保持完整原序
    items = _task_items(panel)
    assert [w.property("status") for w in items] == ["completed", "in_progress", "pending"]
    run_items = _running_items(panel)
    assert len(run_items) == 1
    assert run_items[0].property("status") == "in_progress"
    assert run_items[0].property("pinned")
    # 置顶容器淡琥珀底卡，与普通列表视觉区分
    assert "rgba(245, 158, 11, 0.10)" in panel.tasks_page._running_wrap.styleSheet()
    # 竖条强调靠 [pinned="true"] 属性选择器限域，仅置顶条生效
    for w in items + run_items:
        assert '[pinned="true"]' in w.styleSheet()
    # 字重区分：置顶 600，列表内同任务行保持普通 500
    assert "font-weight: 600" in run_items[0].findChild(QLabel, "taskContent").styleSheet()
    list_run = next(w for w in items if w.property("status") == "in_progress")
    assert "font-weight: 500" in list_run.findChild(QLabel, "taskContent").styleSheet()


def test_tasks_running_row_visible_when_collapsed(panel):
    """折叠只收列表，进行中常驻条保持可见"""
    page = panel.tasks_page
    page.update_todos(
        [
            {"content": "done", "status": "completed", "priority": "medium"},
            {"content": "doing", "status": "in_progress", "priority": "high"},
        ]
    )
    page._set_collapsed(True)
    assert page._collapsed
    assert not page._scroll.isVisible()
    assert page._running_wrap.isVisible()


def test_tasks_header_height_includes_running_row_when_collapsed(panel):
    """折叠态高度计算包含可见的进行中常驻条（否则被 splitter 截掉）"""
    page = panel.tasks_page
    page.update_todos([{"content": "doing", "status": "in_progress", "priority": "high"}])
    page._set_collapsed(True)
    with_running = page.header_height()
    page._running_wrap.hide()  # 模拟无进行中：折叠高度应变小
    assert with_running > page.header_height()


def test_tasks_header_click_toggles_collapse(panel):
    """点 header 整行任意位置切换折叠/展开"""
    from PyQt5.QtTest import QTest

    page = panel.tasks_page
    page.update_todos([{"content": "todo", "status": "pending", "priority": "medium"}])
    assert not page._collapsed
    QTest.mouseClick(page._header, Qt.LeftButton)
    assert page._collapsed
    QTest.mouseClick(page._header, Qt.LeftButton)
    assert not page._collapsed


def test_tasks_running_row_no_duplicate_after_refresh(panel):
    """连续刷新不残留旧 running 条目"""
    page = panel.tasks_page
    page.update_todos([{"content": "a", "status": "in_progress", "priority": "medium"}])
    page.update_todos([{"content": "b", "status": "in_progress", "priority": "medium"}])
    run_items = _running_items(panel)
    assert len(run_items) == 1
    assert run_items[0].findChild(QLabel, "taskContent").toolTip().endswith("b")


def test_tasks_pending_dot_neutral_priority_in_tooltip(panel):
    """pending 圆点恒中性灰（高优先级也不着红），优先级移到行 tooltip"""
    panel.update_todos([{"content": "待办", "status": "pending", "priority": "high"}])
    item = _task_items(panel)[0]
    mark = item.findChild(QLabel, "taskMark")
    assert "#6b7280" in mark.styleSheet()
    assert "#ef4444" not in mark.styleSheet()
    label = item.findChild(QLabel, "taskContent")
    assert "优先级：高" in label.toolTip()
    assert "待办" in label.toolTip()


def test_tasks_no_stale_widget_after_refresh(panel):
    panel.update_todos([{"content": "旧", "status": "pending", "priority": "medium"}])
    panel.update_todos([{"content": "新", "status": "pending", "priority": "medium"}])
    labels = [
        w.text()
        for w in panel.tasks_page.findChildren(QLabel)
        if isinstance(w, QLabel) and w.text() in ("旧", "新")
    ]
    assert "旧" not in labels and "新" in labels


# ── 插件 UI 模块导入校验（页签来自真实插件） ──


@pytest.mark.parametrize(
    "plugin_dir",
    ["plugins/worktree-manager", "plugins/artifacts-manager", "plugins/history-manager"],
)
def test_plugin_ui_module_importable(plugin_dir):
    """插件 ui/__init__.py 应存在且暴露 register_ui 函数"""
    import importlib.util

    ui_init = os.path.join(_REPO_ROOT, plugin_dir, "ui", "__init__.py")
    assert os.path.exists(ui_init), f"plugin ui module missing: {ui_init}"
    spec = importlib.util.spec_from_file_location(
        f"_probe_{os.path.basename(plugin_dir)}", ui_init, submodule_search_locations=[os.path.dirname(ui_init)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert callable(getattr(module, "register_ui", None))
