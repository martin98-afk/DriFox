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
    spec = importlib.util.spec_from_file_location("_test_system_artifacts_page", _SYSTEM_UI_DIR / "_artifacts_page.py")
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


def test_three_tabs_switch(panel):
    """默认落工作树页；工作树/记忆/产物三内置页签可切换"""
    assert panel.current_tab() == WorkbenchPanel.TAB_WORKTREE
    panel.set_current_tab(WorkbenchPanel.TAB_MEMORY)
    assert panel.current_tab() == WorkbenchPanel.TAB_MEMORY
    assert panel._stack.currentIndex() == WorkbenchPanel.TAB_MEMORY
    panel.set_current_tab(WorkbenchPanel.TAB_ARTIFACTS)
    assert panel._stack.currentIndex() == WorkbenchPanel.TAB_ARTIFACTS
    panel.set_current_tab(WorkbenchPanel.TAB_WORKTREE)
    assert panel._stack.currentIndex() == WorkbenchPanel.TAB_WORKTREE


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


def test_first_open_defaults_to_worktree_then_remembers(panel):
    """页签记忆：首次打开默认工作树；之后恢复上次关闭时的页签（不强制重置）"""
    # 首次打开：尚未记录过关闭页签 → 默认工作树
    assert panel._last_tab_index is None
    panel.restore_last_tab()
    assert panel.current_tab() == WorkbenchPanel.TAB_WORKTREE
    # 用户切到产物页后关闭 → 记录
    panel.set_current_tab(WorkbenchPanel.TAB_ARTIFACTS)
    panel.remember_closed_tab()
    assert panel._last_tab_index == WorkbenchPanel.TAB_ARTIFACTS
    # 再次打开：恢复上次关闭时的产物页，而非强制重置工作树
    panel.restore_last_tab()
    assert panel.current_tab() == WorkbenchPanel.TAB_ARTIFACTS
    # 越界兜底（卡片/插件页已卸载）：回落工作树
    panel._last_tab_index = 99
    panel.restore_last_tab()
    assert panel.current_tab() == WorkbenchPanel.TAB_WORKTREE


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
    assert panel._tab_id_index("plug1") == 3
    assert panel._stack.count() == 4
    assert panel._stack.indexOf(panel._plugin_widgets["plug1"]) == 3
    panel.set_current_tab(3)
    assert panel.current_tab() == 3
    # 卸载后回落（默认落点 = 工作树页）
    panel.sync_plugin_pages([])
    assert panel._stack.count() == 3
    assert panel._tab_id_index("plug1") is None
    assert panel.current_tab() == WorkbenchPanel.TAB_WORKTREE


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
    panel.update_todos(
        [
            {"status": "pending", "content": "搞事情", "priority": "high"},
            {"status": "in_progress", "content": "写代码", "priority": "medium"},
            {"status": "completed", "content": "提交", "priority": "low"},
        ]
    )
    assert panel.tasks_page.isVisible() is True
    assert panel.tasks_page._collapse_btn.isVisible() is True


def test_tasks_hide_when_cleared(panel):
    """任务清空后 tasks_page 应隐藏，splitter 下半（任务区）高度收敛为 0"""
    panel.update_todos([{"status": "pending", "content": "临时任务", "priority": "low"}])
    assert panel.tasks_page.isVisible() is True
    panel.update_todos([])
    assert panel.tasks_page.isVisible() is False
    # splitter 下半高度应已收敛（新布局：index0=内容栈，index1=任务区）
    sizes = panel._body_splitter.sizes()
    assert sizes[1] == 0


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
    panel.update_todos(
        [
            {"status": "pending", "content": "A", "priority": "high"},
            {"status": "in_progress", "content": "B", "priority": "medium"},
            {"status": "pending", "content": "C", "priority": "medium"},
            {"status": "completed", "content": "D", "priority": "low"},
        ]
    )
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
    panel.update_todos(
        [
            {"status": "pending", "content": "新任务1", "priority": "medium"},
            {"status": "pending", "content": "新任务2", "priority": "medium"},
        ]
    )
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
    """★ 折叠后 splitter 下半（任务区）应收缩到 header 高度（否则留空白）"""
    panel.update_todos([{"status": "pending", "content": "A", "priority": "medium"}])
    expanded_h = panel._body_splitter.sizes()[1]
    assert expanded_h >= panel.tasks_page.header_height()

    panel.tasks_page._collapse_btn.click()
    collapsed_h = panel._body_splitter.sizes()[1]
    assert collapsed_h < expanded_h, f"折叠后高度应收缩: {collapsed_h} vs {expanded_h}"
    assert collapsed_h <= panel.tasks_page.header_height() + 2

    # 展开恢复
    panel.tasks_page._collapse_btn.click()
    assert panel._body_splitter.sizes()[1] == expanded_h


def test_tasks_progress_bar_reflects_done(panel):
    """进度条应反映完成比例"""
    page = panel.tasks_page
    panel.update_todos(
        [
            {"status": "completed", "content": "A", "priority": "medium"},
            {"status": "completed", "content": "B", "priority": "medium"},
            {"status": "pending", "content": "C", "priority": "medium"},
            {"status": "pending", "content": "D", "priority": "medium"},
        ]
    )
    assert page._progress.value() == 50
    assert page._header._extra_label.text() == "2/4"
    panel.update_todos([])
    assert page._progress.value() == 0
    assert page._header._extra_label.text() == ""


# ── 产物页差异入口 + panel.diff_requested 信号（产物页由插件提供） ──


def test_artifacts_diff_all_button_emits_signal(panel_with_artifacts):
    """产物页 header 的「查看所有产物差异」按钮应触发 panel.diff_requested(None)"""
    panel = panel_with_artifacts
    panel.update_artifacts(
        [
            {"file_path": "D:/x/a.py", "tool_name": "edit", "created_at": "2026-08-31 13:00:00"},
        ]
    )
    captured = []
    panel.diff_requested.connect(lambda p: captured.append(p))
    panel.artifacts_page._header._action_btn.click()
    assert captured == [None]


def test_artifacts_item_diff_emits_signal(panel_with_artifacts):
    """单条目「差异」按钮应触发 panel.diff_requested([file_path])"""
    panel = panel_with_artifacts
    panel.update_artifacts(
        [
            {"file_path": "D:/x/b.md", "tool_name": "write", "created_at": "2026-08-31 12:30:00"},
        ]
    )
    captured = []
    panel.diff_requested.connect(lambda p: captured.append(p))
    page = panel.artifacts_page
    frames = [page._list_layout.itemAt(i).widget() for i in range(page._list_layout.count())]
    # 不能用 isinstance(QFrame)：占位 hint 也是 QFrame 子类，且现在固定留在布局 index 0
    items = [w for w in frames if hasattr(w, "_diff_btn")]
    assert items
    items[0]._diff_btn.click()
    assert captured == [["D:/x/b.md"]]


# ── 动态卡片 tab（right 容器 UI 插件卡片，v2 迁移工作台） ──


def test_card_tab_open_close_and_activate(panel):
    """open → 追加 tab 并激活；重复 open → 幂等激活；close → 摘除不销毁 widget"""
    card = QLabel("卡片内容")
    panel.open_card_tab("my-card", "我的卡片", card)
    assert panel.has_card_tab("my-card")
    assert panel.current_tab() == panel._stack.indexOf(card)
    assert panel._tab_ids[-1] == "my-card"  # 卡片 tab 追加在内置页之后

    # 重复 open 同一 widget：不重复挂载，仅激活
    panel.set_current_tab(WorkbenchPanel.TAB_MEMORY)
    panel.open_card_tab("my-card", "我的卡片", card)
    assert panel._stack.indexOf(card) >= 0
    assert panel.current_tab() == panel._stack.indexOf(card)

    # 关闭：从 stack 与页签条摘除，widget 不销毁（交还调用方）
    assert panel.close_card_tab("my-card") is True
    assert not panel.has_card_tab("my-card")
    assert panel._stack.indexOf(card) == -1
    assert card.parent() is None
    # 幂等：再关返回 False
    assert panel.close_card_tab("my-card") is False
    card.deleteLater()


def test_card_tab_close_signal_emitted(panel):
    """tab × 关闭钮 → panel 发射 card_tab_close_requested(card_id)，宿主 registry 接管"""
    card = QLabel("卡片内容")
    panel.open_card_tab("my-card", "我的卡片", card)
    captured = []
    # 与 registry 的真实接线一致：信号 → close_card_tab
    panel.card_tab_close_requested.connect(panel.close_card_tab)
    panel.card_tab_close_requested.connect(lambda cid: captured.append(cid))
    btn = panel._tab_buttons[-1]._close_btn
    assert btn is not None
    btn.click()
    assert captured == ["my-card"]
    assert not panel.has_card_tab("my-card")
    card.deleteLater()


def test_card_tab_rebuild_keeps_builtin_and_plugin_order(panel_with_artifacts):
    """sync_plugin_pages 与卡片 tab 共存：tab 顺序 = 内置(工作树/记忆/产物) + 插件 + 卡片"""
    panel = panel_with_artifacts
    card = QLabel("卡片内容")
    panel.open_card_tab("my-card", "我的卡片", card)
    assert panel._tab_ids == ["worktree", "memory", "artifacts", "my-card"]
    for i, tab_id in enumerate(panel._tab_ids):
        assert panel._tab_buttons[i].tab_id == tab_id
        assert panel._stack.widget(i) is not None
    # 卡片 tab 带 × 关闭钮，内置页不带
    assert panel._tab_buttons[3]._close_btn is not None
    assert panel._tab_buttons[0]._close_btn is None
    card.deleteLater()


def test_artifacts_diff_all_button_hidden_when_empty(panel_with_artifacts):
    """无产物时「查看所有产物差异」按钮应隐藏"""
    panel = panel_with_artifacts
    panel.update_artifacts([])
    action_btn = panel.artifacts_page._header._action_btn
    assert action_btn is None or not action_btn.isVisible()


def test_artifacts_placeholder_before_plugin_registered(panel):
    """插件未注册产物页时，index 2（TAB_ARTIFACTS）应是占位页（面板不再内置产物实现）"""
    assert panel.artifacts_page is panel._artifacts_placeholder
    assert panel._stack.indexOf(panel._artifacts_placeholder) == WorkbenchPanel.TAB_ARTIFACTS
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
    """★ 回归：填槽位后 stack 索引仍与 tab 顺序一致（0=工作树 / 1=记忆 / 2=产物）

    早期实现用 insertWidget(0, w) 会把原 index 0 及其后所有页整体后移，
    导致 tab 与内容错位 —— 表现为「记忆」tab 显示出产物内容。
    """
    panel.sync_plugin_pages(artifacts_plugins)
    assert panel._stack.indexOf(panel.worktree_page) == WorkbenchPanel.TAB_WORKTREE
    assert panel._stack.indexOf(panel.memory_page) == WorkbenchPanel.TAB_MEMORY
    assert panel._stack.indexOf(panel.artifacts_page) == WorkbenchPanel.TAB_ARTIFACTS
    assert panel._stack.count() == 3
    # 切到工作树页，确认拿到的确实是工作树页
    panel.set_current_tab(WorkbenchPanel.TAB_WORKTREE)
    assert panel._stack.currentWidget() is panel.worktree_page


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
    assert tab_ids == ["worktree", "memory", "artifacts", "mystats"], f"实际 tab: {tab_ids}"
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


# ── 多标签页 tab 状态回归（2026-09-02：选中残留 / 选中丢失修复） ──


def test_set_current_tab_out_of_range_keeps_highlight(panel):
    """★ 越界 set_current_tab 不再把全部按钮熄灭（「tab 选中丢失」根因）

    按窗口记忆恢复的页签可能已被卸载（卡片 tab 关闭 / 插件卸载 / 历史页摘除）。
    旧实现：setCurrentIndex 越界是空操作，但下方按钮循环把所有按钮置非激活 →
    内容停在旧页、高亮却全灭。
    """
    panel.set_current_tab(WorkbenchPanel.TAB_WORKTREE)
    assert panel.current_tab() == WorkbenchPanel.TAB_WORKTREE
    panel.set_current_tab(99)  # 越界
    assert panel.current_tab() == WorkbenchPanel.TAB_WORKTREE  # 不强行跳页
    active = [i for i, b in enumerate(panel._tab_buttons) if b._active]
    assert active == [WorkbenchPanel.TAB_WORKTREE], f"越界后高亮应保留在工作树页: {active}"
    panel.set_current_tab(-1)  # 负越界同样兜底
    assert panel.current_tab() == WorkbenchPanel.TAB_WORKTREE
    assert panel._tab_buttons[WorkbenchPanel.TAB_WORKTREE]._active


def test_attach_history_preserves_current_page(panel):
    """挂载历史页时保持用户当前页签（插件/卡片页不被历史页挤走）

    ★ 行为锁定：Qt 的 insertWidget 前插会递增 currentIndex（当前 widget 不变），
    但换挂组合路径（remove+insert）依赖 Qt 的 index 记账，语义脆弱。无论实现
    走 was_current 特判还是按 tab_id 还原，最终行为都必须是：停在插件页时
    挂载历史页，页面与高亮仍留在插件页（历史页插入后其 index 平移到 4）。
    """
    from types import SimpleNamespace

    class FakePage(QWidget):
        def __init__(self, parent=None, context=None):
            super().__init__(parent)

    info = SimpleNamespace(page_id="plug1", label="插件页", widget_class=FakePage, plugin_name="x")
    panel.sync_plugin_pages([info])  # 插件页在 index 3
    panel.set_current_tab(3)
    assert panel.current_tab() == 3
    history = QLabel("历史")
    panel.attach_history_page(history)
    # 历史页插入后插件页移到 index 4；用户此前所在的插件页必须保持显示
    assert panel._stack.indexOf(history) == WorkbenchPanel.TAB_HISTORY
    assert panel.current_tab() == 4
    assert panel._stack.currentWidget() is panel._plugin_widgets["plug1"]
    active_ids = [b.tab_id for i, b in enumerate(panel._tab_buttons) if b._active]
    assert active_ids == ["plug1"], f"插件页应保持选中: {active_ids}"
    history.deleteLater()


def test_attach_history_stays_when_on_history(panel):
    """挂载历史页时若正显示历史页 → 延续显示新历史页（跨窗口切换不跳页）"""
    h1 = QLabel("历史A")
    h2 = QLabel("历史B")
    panel.attach_history_page(h1)
    panel.set_current_tab(WorkbenchPanel.TAB_HISTORY)
    panel.attach_history_page(h2)  # 换挂到另一窗口的历史页
    assert panel.current_tab() == WorkbenchPanel.TAB_HISTORY
    assert panel._stack.currentWidget() is h2
    h1.deleteLater()
    h2.deleteLater()


def test_card_tab_replace_keeps_position(panel):
    """★ 同 id 卡片换新实例：原位替换，不把卡片移到栈尾（「tab 与内容错位」根因）

    旧实现 remove+append：两张卡片 A/B 时替换 A → 栈序变 [B, A] 而页签序
    仍为 [A, B] → 点击 A tab 实际切到 B 的内容、高亮落错位。
    """
    card_a1 = QLabel("A-旧")
    card_b = QLabel("B")
    panel.open_card_tab("card-a", "A", card_a1)
    panel.open_card_tab("card-b", "B", card_b)
    assert panel._tab_ids == ["worktree", "memory", "artifacts", "card-a", "card-b"]
    # 换 A 的新实例
    card_a2 = QLabel("A-新")
    panel.open_card_tab("card-a", "A", card_a2)
    # 栈序保持 [.., A2, B]，与页签序一致
    assert panel._stack.indexOf(card_a2) == 3
    assert panel._stack.indexOf(card_b) == 4
    assert panel._tab_id_index("card-a") == panel._stack.indexOf(card_a2)
    assert panel._tab_id_index("card-b") == panel._stack.indexOf(card_b)
    # 点击 A tab 切到 A2 内容（不是 B）
    panel.set_current_tab(panel._tab_id_index("card-a"))
    assert panel._stack.currentWidget() is card_a2
    card_a1.deleteLater()
    card_a2.deleteLater()
    card_b.deleteLater()


def test_artifacts_label_updates_on_registration(panel):
    """★ 产物页标签跟随插件注册更新（label 未入 reconcile sig 的残留）

    产物页 label 不在 sync_plugin_pages 的 sig 比较范围内，仅 artifacts 插件
    注册/改名时集合未变 → 早退跳过重建 → tab 标签残留旧值。补一次 reconcile。
    """
    from types import SimpleNamespace

    class FakePage(QWidget):
        def __init__(self, parent=None, context=None):
            super().__init__(parent)

    assert [b._label.text() for b in panel._tab_buttons] == ["工作树", "记忆", "产物"]
    info = SimpleNamespace(page_id="artifacts", label="文件产物", widget_class=FakePage, plugin_name="x")
    panel.sync_plugin_pages([info])
    labels = [b._label.text() for b in panel._tab_buttons]
    assert labels == ["工作树", "记忆", "文件产物"], f"产物页标签应更新，实际: {labels}"


def test_card_tab_activate_false_mounts_without_switching(panel):
    """★ activate=False：挂载卡片 tab 但不改变当前页签（对话标签页投影恢复语义）

    旧实现 open_card_tab 无条件激活 → 切换对话标签页恢复卡片时，工作台
    当前页签被恢复的卡片抢走（用户停留页签丢失）。
    """
    panel.set_current_tab(WorkbenchPanel.TAB_MEMORY)
    card = QLabel("卡片内容")
    panel.open_card_tab("my-card", "我的卡片", card, activate=False)
    assert panel.has_card_tab("my-card")
    assert panel.current_tab() == WorkbenchPanel.TAB_MEMORY, "不激活时当前页签不得被抢走"
    assert panel._stack.indexOf(card) >= 0
    # 已挂载的卡片再以 activate=False 恢复：同样不跳页
    panel.open_card_tab("my-card", "我的卡片", card, activate=False)
    assert panel.current_tab() == WorkbenchPanel.TAB_MEMORY
    card.deleteLater()


def test_card_tab_activate_true_emits_user_signal(panel):
    """★ activate=True：激活卡片页且走用户路径（写 per-window 页签记忆）

    用户主动打开卡片 = 切页，应经 user 路径发射 current_tab_changed，宿主
    据此把卡片记为该窗口的页签记忆（切走再切回才停得住）。
    """
    fired = []
    panel.current_tab_changed.connect(lambda i: fired.append(i))
    card = QLabel("卡片内容")
    panel.open_card_tab("my-card", "我的卡片", card, activate=True)
    idx = panel._stack.indexOf(card)
    assert panel.current_tab() == idx
    assert fired == [idx], f"应经 user 路径发射 current_tab_changed: {fired}"
    card.deleteLater()


def test_remember_workbench_tab_stores_tab_id(panel):
    """★ 宿主页签记忆按 tab_id 而非裸 index（跨窗口 tab 集合不同时的根因修复）

    各窗口 tab 集合可能不同（卡片 tab per-tab 投影 / 历史页懒挂载 / 插件页），
    裸 index 在切窗恢复时会撞到别的页签或越界；改存 tab_id 后 restore 侧按 id
    重新定位。直接以轻量 stub 绑定宿主方法验证存储语义。
    """
    from types import SimpleNamespace

    from app.widgets.tab_manager_window import TabManagerWindow

    class FakePage(QWidget):
        def __init__(self, parent=None, context=None):
            super().__init__(parent)

    info = SimpleNamespace(page_id="plug1", label="插件页", widget_class=FakePage, plugin_name="x")
    panel.sync_plugin_pages([info])  # _tab_ids = [worktree, memory, artifacts, plug1]

    win = SimpleNamespace()
    host = SimpleNamespace(workbench_panel=panel)
    host.get_current_window = lambda: win
    TabManagerWindow._remember_workbench_tab(host, 3)
    assert win._workbench_tab_memory == "plug1", f"应存 tab_id 而非 index: {win._workbench_tab_memory!r}"
    # 越界 index 不写脏数据
    TabManagerWindow._remember_workbench_tab(host, 99)
    assert win._workbench_tab_memory is None
