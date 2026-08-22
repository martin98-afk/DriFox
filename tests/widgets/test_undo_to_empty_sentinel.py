"""回归测试：流式撤销至空会话后，old worker finalize 不得复活已撤销消息

症状（P052）：用户消息卡片撤销后 UI 已消失，但消息仍留在消息列表（重进会话可见）。

根因（H1，已证实）：
1. 流式中用户点「撤销到这里」撤销第一轮（round 0）→ session 截断至空
2. `_persist_session_after_mutation` 空会话分支早退 → 不设置 _truncation_sentinel
3. old worker 的 finalize 完成 → `_on_finalize_complete` / `_apply_interrupted_messages_to_session`
   无哨兵拦截 → 用 worker 的完整旧快照覆写空会话 → 消息复活 + save+flush 落盘

修复：
- F1: `_undo_from_message` 在 `_on_stop_clicked()` 之前设置截断哨兵（对齐 _delete_message 模式）
- F2: `_persist_session_after_mutation` 空会话分支 return 前同样设置哨兵（防御纵深，
  调用方仅 _truncate_session_from_user_round / _delete_user_round，均为截断路径）

运行: uv run pytest tests/widgets/test_undo_to_empty_sentinel.py -v
"""

import inspect
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import app.main_widget as mw
from app.core.chat_session import ChatSession, SessionManager


def _make_widget(initial_messages: list) -> mw.OpenAIChatToolWindow:
    widget = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    widget.session_manager = SessionManager()
    session = ChatSession(messages=list(initial_messages))
    widget.session_manager.sessions.append(session)
    widget.session_manager.current_index = 0
    widget.history_manager = MagicMock()
    widget.history_manager.flush = MagicMock()
    widget._truncation_sentinel = None
    widget._history_preview_messages = None
    widget._current_session_id = session.session_id
    widget._current_project = None
    widget._session_dirty = False
    return widget


def test_empty_session_mutation_sets_sentinel():
    """F2：撤销至空后 _persist_session_after_mutation 必须设置截断哨兵"""
    widget = _make_widget([
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ])
    session = widget.session_manager.get_current_session()

    # 模拟 _truncate_session_from_user_round 的截断：round 0 → cutoff 0 → 清空
    session.set_messages([], preserve_compaction=False)
    widget._persist_session_after_mutation()

    sentinel = widget._truncation_sentinel
    assert sentinel is not None, (
        "撤销至空会话后必须设置截断哨兵，否则 old worker finalize 会复活已撤销消息"
    )
    assert sentinel.get("session_id") == session.session_id
    assert sentinel.get("messages_len") == 0


def test_old_worker_finalize_blocked_after_undo_to_empty():
    """端到端：撤销至空 + 持久化后，old worker 快照必须被哨兵拦截"""
    old_snapshot = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "partial reply"},
    ]
    widget = _make_widget(old_snapshot)
    session = widget.session_manager.get_current_session()

    # 撤销至空 + 持久化（应设哨兵）
    session.set_messages([], preserve_compaction=False)
    widget._persist_session_after_mutation()

    # old worker finalize 回调到达
    applied = widget._apply_interrupted_messages_to_session(list(old_snapshot))

    assert applied is False, "哨兵必须拦截 old worker 快照"
    revived = widget.session_manager.get_current_session().messages
    assert len(revived) == 0, (
        f"已撤销消息被复活：{len(revived)} 条重新写回 session —— UI 卡片已删但消息列表仍存在"
    )


def test_undo_from_message_sets_sentinel_before_stop():
    """F1（源码契约）：_undo_from_message 中哨兵设置必须先于 _on_stop_clicked

    防止 stop 触发的 deferred finalize 在 FileUndoCard.exec_() 嵌套事件循环期间
    到达时无哨兵保护（此时截断尚未发生，_persist_session_after_mutation 未执行）。
    """
    src = inspect.getsource(mw.OpenAIChatToolWindow._undo_from_message)
    stop_pos = src.find("self._on_stop_clicked()")
    sentinel_pos = src.find("self._truncation_sentinel = {")
    assert stop_pos != -1, "_undo_from_message 应包含 _on_stop_clicked 调用"
    assert sentinel_pos != -1, "_undo_from_message 应在 stop 前设置截断哨兵"
    assert sentinel_pos < stop_pos, "哨兵设置必须领先于 _on_stop_clicked（对齐 _delete_message）"
