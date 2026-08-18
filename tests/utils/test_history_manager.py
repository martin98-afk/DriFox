# -*- coding: utf-8 -*-
"""HistoryManager 会话丢失回归测试。"""

import json
from pathlib import Path
from typing import Dict, List, Optional

import app.utils.history_manager as history_module
from app.utils.history_manager import HistoryManager


class _FakeSessionStore:
    """仅实现历史懒加载与延迟保存测试需要的存储接口。

    P2 修复（T16）：补齐 get_session——history_manager.save_session 的
    「None→保留现值」路径会调 get_session_by_session_id → _session_store.
    get_session()，fake 缺该方法导致 AttributeError（R1 1ef88f77 引入
    save_session 查询后未同步测试）。语义对齐真实 SessionStore：
    get_session 返回已保存的完整会话，未保存过返回 None。
    """

    is_initialized = True

    def __init__(self, sessions: List[Dict]):
        self.sessions = sessions
        self.saved_session_ids: List[str] = []
        # 已保存的完整会话记录（save_session 写入，get_session 回读）
        self._saved_sessions: Dict[str, Dict] = {}

    def get_sessions_lightweight(self, limit: int = 500) -> List[Dict]:
        return [dict(session) for session in self.sessions[:limit]]

    def get_session_count(self) -> int:
        return len(self.sessions)

    def save_session(self, session: Dict) -> bool:
        self.saved_session_ids.append(session["session_id"])
        self._saved_sessions[session["session_id"]] = session
        return True

    def get_session(self, session_id: str) -> Optional[Dict]:
        """返回已保存的完整会话；未保存过返回 None（真实 SQLite 语义）。"""
        return self._saved_sessions.get(session_id)


def _make_manager(monkeypatch, tmp_path: Path) -> HistoryManager:
    monkeypatch.setattr(history_module, "get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(HistoryManager, "_init_storage", lambda self: None)
    return HistoryManager()


def _lightweight_session(index: int) -> Dict:
    return {
        "session_id": f"session-{index}",
        "title": f"会话 {index}",
        "project": "默认项目",
        "messages": [],
        "last_time": f"2026-07-20 12:{index % 60:02d}:00",
    }


def test_save_session_does_not_hide_sessions_loaded_from_sqlite(monkeypatch, tmp_path):
    """保存一条会话后，不应把已加载的第 101 条历史记录从列表中截掉。"""
    manager = _make_manager(monkeypatch, tmp_path)
    manager._history_loaded = True
    manager._history_sessions = [_lightweight_session(i) for i in range(101)]
    manager._persist_session = lambda _record: None
    original_ids = {session["session_id"] for session in manager._history_sessions}

    manager.save_session(
        [{"role": "user", "content": "更新消息", "timestamp": "2026-07-20 15:00:00"}],
        session_id="session-0",
        project="默认项目",
    )

    remaining_ids = {session["session_id"] for session in manager._history_sessions}
    assert len(remaining_ids) == 101
    assert remaining_ids == original_ids


def test_lazy_load_cannot_overwrite_a_pending_new_session(monkeypatch, tmp_path):
    """首次读取历史列表不能覆盖尚在延迟保存窗口内的新会话。"""
    manager = _make_manager(monkeypatch, tmp_path)
    store = _FakeSessionStore([_lightweight_session(1)])
    manager._use_sqlite = True
    manager._session_store = store

    # 用可控 pending 标记代替 Qt 单次定时器，稳定复现保存与首次懒加载的竞态。
    manager._persist_session = lambda record: setattr(manager, "_pending_save_session_id", record["session_id"])

    manager.save_session(
        [{"role": "user", "content": "新会话", "timestamp": "2026-07-20 15:00:00"}],
        session_id="new-session",
        project="默认项目",
    )
    manager.get_history_list()
    manager._do_save()

    assert "new-session" in {session["session_id"] for session in manager._history_sessions}
    assert store.saved_session_ids == ["new-session"]


def test_do_save_does_not_overwrite_db_with_released_empty_messages(monkeypatch, tmp_path):
    """⚠️ 回归：内存记录 messages 被置空（_release_inactive_session_messages /
    remove_session release_messages_only）后，_do_save 不得用空消息覆盖
    DB 中已有完整消息的会话。

    根因：_schedule_save 延迟保存（1000ms）与内存消息释放形成竞态——
    释放逻辑把 _history_sessions 里该会话的 messages 置为 []（保留
    preview/message_count），_do_save 到时遍历捡到空记录直接写库，
    导致历史会话 messages 被覆写为 []（DB 证据：zstd([])+preview 保留）。
    """
    manager = _make_manager(monkeypatch, tmp_path)
    store = _FakeSessionStore([_lightweight_session(1)])
    manager._use_sqlite = True
    manager._session_store = store
    manager._history_loaded = True
    manager._history_sessions = [
        {
            "session_id": "session-1",
            "title": "会话 1",
            "project": "默认项目",
            "messages": [
                {"role": "user", "content": "真实消息", "timestamp": "2026-08-18 16:00:00"},
                {"role": "assistant", "content": "真实回复", "timestamp": "2026-08-18 16:00:01"},
            ],
            "preview": "真实消息",
            "message_count": 1,
            "last_time": "2026-08-18 16:00:01",
        }
    ]

    # 模拟内存消息被释放置空（发生在 _schedule_save 的延迟窗口内）
    manager._history_sessions[0]["messages"] = []
    manager._pending_save_session_id = "session-1"

    manager._do_save()

    # DB 不得收到空 messages 的覆盖写；pending 需正常清空
    assert store.saved_session_ids == []
    assert manager._pending_save_session_id is None


def test_import_before_first_history_load_survives_immediate_refresh(monkeypatch, tmp_path):
    """首次历史加载不能覆盖导入后尚在延迟保存窗口内的会话。"""
    manager = _make_manager(monkeypatch, tmp_path)
    store = _FakeSessionStore([_lightweight_session(1)])
    manager._use_sqlite = True
    manager._session_store = store
    manager._schedule_save = lambda session_id: setattr(manager, "_pending_save_session_id", session_id)

    imported_path = tmp_path / "imported-session.json"
    imported_path.write_text(
        json.dumps(
            {
                "session_id": "imported-session",
                "title": "导入会话",
                "project": "默认项目",
                "messages": [{"role": "user", "content": "导入内容", "timestamp": "2026-07-20 15:00:00"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    imported = manager.import_from_json(str(imported_path))
    manager.get_history_list()
    manager._do_save()

    assert imported is not None
    assert "imported-session" in {session["session_id"] for session in manager._history_sessions}
    assert store.saved_session_ids == ["imported-session"]
