# -*- coding: utf-8 -*-
"""InlineTodoPanel（消息卡片内嵌任务看板）回归测试

看板从右侧工作台任务区迁入消息卡片气泡内（正文下方、页脚上方）后的行为契约：
默认折叠、无任务整区隐藏、展开全量展示、进行中常驻条、条目渲染、高度变化信号。
纯离屏（offscreen）运行。
"""

import os
import sys

import pytest

sys.path.insert(0, ".")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QFrame, QLabel, QWidget  # noqa: E402

from app.widgets.inline_todo_panel import InlineTodoPanel  # noqa: E402


@pytest.fixture()
def panel(qapp):
    host = QWidget()
    host.resize(600, 400)
    host.show()
    p = InlineTodoPanel(host)
    yield p
    p.setParent(None)
    p.deleteLater()
    host.deleteLater()


def _items(panel):
    """列表区条目（objectName=taskItem 的 QFrame）"""
    lay = panel._list_layout
    return [
        w
        for i in range(lay.count())
        if isinstance(w := lay.itemAt(i).widget(), QFrame) and w.objectName() == "taskItem"
    ]


def _running_items(panel):
    """进行中常驻条条目（脱离列表的置顶区）"""
    lay = panel._running_layout
    return [
        w
        for i in range(lay.count())
        if isinstance(w := lay.itemAt(i).widget(), QFrame) and w.objectName() == "taskItem"
    ]


# ── 折叠契约 ──


def test_collapsed_by_default(panel):
    """卡片内嵌场景默认折叠（卡片空间宝贵）"""
    assert panel.is_collapsed() is True
    assert not panel._list_wrap.isVisible()


def test_hidden_when_no_tasks(panel):
    panel.update_todos([])
    assert panel.isVisible() is False


def test_visible_and_collapsed_on_first_tasks(panel):
    """首个非空任务到达：整区显示，但保持折叠（不打断用户折叠意愿）"""
    panel.update_todos([{"content": "任务一", "status": "pending", "priority": "medium"}])
    assert panel.isVisible() is True
    assert panel.is_collapsed() is True


def test_expand_shows_list_and_emits_height_changed(panel):
    seen = []
    panel.heightChanged.connect(lambda: seen.append(1))
    panel.update_todos([{"content": "todo", "status": "pending", "priority": "medium"}])
    seen.clear()
    panel._on_collapse_clicked()
    assert panel.is_collapsed() is False
    assert panel._list_wrap.isVisible()
    assert seen, "折叠切换应触发 heightChanged（宿主据此外层锚定）"


def test_header_click_toggles_collapse(panel):
    """点 header 整行任意位置切换折叠/展开"""
    from PyQt5.QtTest import QTest

    panel.update_todos([{"content": "todo", "status": "pending", "priority": "medium"}])
    assert panel.is_collapsed()
    QTest.mouseClick(panel._header, Qt.LeftButton)
    assert not panel.is_collapsed()
    QTest.mouseClick(panel._header, Qt.LeftButton)
    assert panel.is_collapsed()


def test_list_plain_container_no_scroll(panel):
    """展开态列表是普通容器（非 QScrollArea）：sizeHint 实时准确、不内滚

    QScrollArea 的 sizeHint() 依赖内部 widgetSize 缓存（仅 widget resize 时刷新），
    动态增删行后返回滞后值甚至 0，宿主高度链会拿到错误面板高度 → 裁切 + 外层
    滚动上界虚抬。普通 QWidget 容器的布局 sizeHint 永远实时。
    """
    assert not isinstance(panel._list_wrap, QScrollArea)
    panel.update_todos(
        [{"content": f"任务 {i}", "status": "pending", "priority": "medium"} for i in range(10)]
    )
    panel._set_collapsed(False)
    h10 = panel._list_wrap.sizeHint().height()
    assert h10 > 200  # 10 行应全量计入
    # 增行后 sizeHint 必须同步增长（QScrollArea 在此返回滞后值）
    panel.update_todos(
        [{"content": f"任务 {i}", "status": "pending", "priority": "medium"} for i in range(12)]
    )
    panel._set_collapsed(False)
    assert panel._list_wrap.sizeHint().height() > h10


def test_running_row_visible_when_collapsed(panel):
    """折叠只收列表，进行中常驻条保持可见"""
    panel.update_todos(
        [
            {"content": "done", "status": "completed", "priority": "medium"},
            {"content": "doing", "status": "in_progress", "priority": "high"},
        ]
    )
    assert panel.is_collapsed()
    assert not panel._list_wrap.isVisible()
    assert panel._running_wrap.isVisible()


# ── 数据契约 ──


def test_progress_bar_reflects_done(panel):
    panel.update_todos(
        [
            {"content": "a", "status": "completed", "priority": "medium"},
            {"content": "b", "status": "pending", "priority": "medium"},
        ]
    )
    assert panel._progress.value() == 50


def test_stat_label_shows_pct_and_done_over_total(panel):
    panel.update_todos([{"content": "a", "status": "pending", "priority": "medium"}])
    assert panel._header._extra_label.text() == "0% · 0/1"


def test_same_data_skips_rebuild(panel):
    """同数据重复推送不重建控件（签名比对）"""
    todos = [{"content": "a", "status": "pending", "priority": "medium"}]
    panel.update_todos(todos)
    first = _items(panel)[0]
    panel.update_todos(list(todos))
    assert _items(panel)[0] is first


def test_item_single_line_elided_with_full_tooltip(panel):
    long_text = "很长的任务描述" * 20
    panel.update_todos([{"content": long_text, "status": "pending", "priority": "medium"}])
    items = _items(panel)
    assert len(items) == 1
    label = items[0].findChild(QLabel, "taskContent")
    assert type(label).__name__ == "_ElidedLabel"
    assert label.toolTip().endswith(long_text)
    assert "优先级" in label.toolTip()


def test_in_progress_emphasized_with_accent_bar(panel):
    """仅置顶条带琥珀重点样式；列表条目一律普通行"""
    panel.update_todos(
        [
            {"content": "done", "status": "completed", "priority": "medium"},
            {"content": "doing", "status": "in_progress", "priority": "high"},
            {"content": "todo", "status": "pending", "priority": "low"},
        ]
    )
    items = _items(panel)
    assert [w.property("status") for w in items] == ["completed", "in_progress", "pending"]
    run_items = _running_items(panel)
    assert len(run_items) == 1
    assert run_items[0].property("pinned")
    assert "rgba(245, 158, 11, 0.10)" in panel._running_wrap.styleSheet()
    assert "font-weight: 600" in run_items[0].findChild(QLabel, "taskContent").styleSheet()
    list_run = next(w for w in items if w.property("status") == "in_progress")
    assert "font-weight: 500" in list_run.findChild(QLabel, "taskContent").styleSheet()


def test_pending_dot_neutral_priority_in_tooltip(panel):
    """pending 圆点恒中性灰（高优先级也不着红），优先级移到行 tooltip"""
    panel.update_todos([{"content": "待办", "status": "pending", "priority": "high"}])
    item = _items(panel)[0]
    mark = item.findChild(QLabel, "taskMark")
    assert "#6b7280" in mark.styleSheet()
    assert "#ef4444" not in mark.styleSheet()
    label = item.findChild(QLabel, "taskContent")
    assert "优先级：高" in label.toolTip()


def test_running_row_no_duplicate_after_refresh(panel):
    """连续刷新不残留旧 running 条目"""
    panel.update_todos([{"content": "a", "status": "in_progress", "priority": "medium"}])
    panel.update_todos([{"content": "b", "status": "in_progress", "priority": "medium"}])
    run_items = _running_items(panel)
    assert len(run_items) == 1
    assert run_items[0].findChild(QLabel, "taskContent").toolTip().endswith("b")


def test_no_stale_widget_after_refresh(panel):
    panel.update_todos([{"content": "旧", "status": "pending", "priority": "medium"}])
    panel.update_todos([{"content": "新", "status": "pending", "priority": "medium"}])
    labels = [
        w.text()
        for w in panel.findChildren(QLabel)
        if isinstance(w, QLabel) and w.text() in ("旧", "新")
    ]
    assert "旧" not in labels and "新" in labels


def test_dirty_content_dumped_as_json(panel):
    """content 非 str（dict/list）兜底转 JSON 文本（QLabel 只收 str）"""
    panel.update_todos([{"content": {"a": 1}, "status": "pending", "priority": "medium"}])
    labels = [
        w.text() for w in panel.findChildren(QLabel) if isinstance(w, QLabel) and "a" in w.text()
    ]
    assert any('{"a": 1}' in t for t in labels)


def test_theme_refresh_keeps_running_style(panel):
    """主题刷新后置顶条样式仍在（QSS 重放）"""
    panel.update_todos([{"content": "doing", "status": "in_progress", "priority": "medium"}])
    panel.refresh_style()
    assert "rgba(245, 158, 11, 0.10)" in panel._running_wrap.styleSheet()
