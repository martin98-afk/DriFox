# -*- coding: utf-8 -*-
"""排队消息卡片增量刷新回归测试（行复用 / 序号重排 / 编辑态保留 / snap 豁免）

== 背景 ==
set_entries 旧实现全量销毁重建行控件，每次入队整卡重刷：hover 态丢失、
编辑被打断、配合容器 200ms 高度动画在真实环境（流式挤压 BOTTOM 停靠区）
下表现为「每加一条消息卡片消失再出现」的视觉跳闪。

== 改造 ==
1. set_entries 按 msg_id 增量 diff：已有行复用不重建，只增删变化行；
2. 卡片声明 NO_ANIMATION_PROP：容器高度 snap 直落终值，无动画中间态；
3. 编辑器拆装走 _teardown_editor：行控件保留可复用，编辑态跨刷新保留。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import Qt

Qt.AA_ShareOpenGLContexts = Qt.AA_ShareOpenGLContexts
try:
    from PyQt5.QtWebEngineWidgets import (  # noqa: F401
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineView,
    )
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtWidgets import QLabel

from app.widgets.cards.card_container import CardContainer
from app.widgets.cards.floating.queue_message_card import QueueMessageCard


def _entries(n: int):
    return [{"id": f"q{i}", "text": f"消息内容 {i}"} for i in range(1, n + 1)]


def _make_card():
    card = QueueMessageCard()
    card.resize(600, 200)
    card.show()
    return card


def test_reuses_row_widgets_on_append(qapp):
    """追加条目时已有行控件实例复用（不销毁重建）"""
    card = _make_card()
    card.set_entries(_entries(2))
    old_rows = card._rows()
    card.set_entries(_entries(3))
    new_rows = card._rows()
    assert len(new_rows) == 3
    assert new_rows[:2] == old_rows, "已有行必须复用实例（append 场景零重建）"
    card.deleteLater()


def test_reindexes_and_keeps_order_after_middle_removal(qapp):
    """删除中间条目：顺序保持 + 序号重排"""
    card = _make_card()
    card.set_entries(_entries(3))
    kept = [_entries(3)[0], _entries(3)[2]]
    card.set_entries(kept)
    rows = card._rows()
    assert [r.property("msg_id") for r in rows] == ["q1", "q3"], "顺序必须等于 entries 顺序"
    idx_texts = [r.findChild(QLabel, "queueRowIndex").text() for r in rows]
    assert idx_texts == ["1", "2"], f"序号必须重排，实际 {idx_texts}"
    card.deleteLater()


def test_updates_text_for_reused_row(qapp):
    """同 id 文本变化（编辑保存路径）：复用行文本与 tooltip 同步更新"""
    card = _make_card()
    card.set_entries(_entries(1))
    card.set_entries([{"id": "q1", "text": "短文本"}])
    label = card._rows()[0].findChild(QLabel, "queueRowText")
    assert label.text() == "短文本", "短文本不被省略，显示文本即原文"
    assert label.toolTip() == "短文本", "tooltip 必须同步原文（悬浮看全文契约）"
    card.deleteLater()


def test_editing_survives_entries_refresh(qapp):
    """编辑中的行在 set_entries 后编辑态保留（旧全量重建会打断编辑）"""
    card = _make_card()
    card.set_entries(_entries(2))
    card._begin_edit(card._rows()[0], "q1", "消息内容 1")
    editor = card._editor
    assert editor is not None
    # 追加第 3 条：编辑态不受影响
    card.set_entries(_entries(3))
    assert card._editor is editor, "追加条目不得打断进行中的编辑"
    # 编辑行被移除：编辑器拆除，无悬挂引用
    card.set_entries([e for e in _entries(3) if e["id"] != "q1"])
    assert card._editor is None
    assert card._editing_id is None
    card.deleteLater()


def test_cancel_edit_restores_label_and_reuses_row(qapp):
    """取消编辑：label 还原显示，行控件仍可被后续 set_entries 复用"""
    card = _make_card()
    card.set_entries(_entries(1))
    row = card._rows()[0]
    label = row.findChild(QLabel, "queueRowText")
    card._begin_edit(row, "q1", "消息内容 1")
    assert label.isHidden(), "进入编辑后文本 label 应隐藏"
    card._cancel_edit()
    assert card._editor is None
    assert not label.isHidden(), "取消编辑后 label 必须还原显示"
    # 行仍可复用（不因编辑残留而强制重建）
    card.set_entries(_entries(2))
    assert card._rows()[0] is row
    card.deleteLater()


def test_declares_no_container_animation(qapp):
    """卡片必须声明 NO_ANIMATION_PROP：容器高度 snap 直落，无动画中间态"""
    card = _make_card()
    assert card.property(CardContainer.NO_ANIMATION_PROP) is True, (
        "排队卡必须声明 noContainerAnimation（跳闪修复的容器侧前提）"
    )
    card.deleteLater()
