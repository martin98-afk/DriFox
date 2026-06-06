# -*- coding: utf-8 -*-
"""
历史会话卡片归档标签测试。

覆盖内容：
- 切换到归档标签不应把缓存历史卡片变成顶层空白窗口
"""

# 1. 标准库
import os

# 2. 第三方库（按字母排序）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget

# 3. 本地导入（相对导入）
from app.widgets.cards.settings.history_card import HistoryCard


def _process_events() -> None:
    app = QApplication.instance()
    assert app is not None
    app.processEvents()


def test_switching_to_archived_tab_does_not_create_top_level_windows() -> None:
    """从历史切换到归档 tab 时，不应弹出缓存卡片窗口。"""
    app = QApplication.instance() or QApplication([])

    host = QWidget()
    layout = QVBoxLayout(host)
    card = HistoryCard(host)
    layout.addWidget(card)
    host.show()

    history = [
        {
            "session_id": f"session-{i}",
            "title": f"历史会话 {i}",
            "last_time": "2026-06-06 12:00:00",
            "message_count": 1,
            "preview": "hello",
        }
        for i in range(3)
    ]
    archived = [
        {
            "path": f"/tmp/archive-{i}.json",
            "session_id": f"archived-{i}",
            "title": f"归档会话 {i}",
            "last_time": "2026-06-06 12:00:00",
            "message_count": 1,
            "preview": "archived",
            "project": "DriFoxx",
        }
        for i in range(2)
    ]

    card.set_history(history, current_index=0)
    for _ in range(3):
        _process_events()

    top_levels_before = len(app.topLevelWidgets())

    card.set_archived_sessions(archived)
    card.switch_tab("archived")
    for _ in range(3):
        _process_events()

    top_levels_after = len(app.topLevelWidgets())

    host.close()
    host.deleteLater()
    _process_events()

    assert top_levels_after == top_levels_before
