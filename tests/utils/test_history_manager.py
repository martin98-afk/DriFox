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

    def __init__(self, sessions: List[Dict], projects: Optional[List[str]] = None):
        self.sessions = sessions
        # 项目列表（对齐真实 store.get_projects：sessions ∪ key_documents 已过滤排序）
        self._projects: List[str] = list(projects) if projects is not None else sorted(
            {s.get("project", "默认项目") for s in sessions}
        )
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

    def get_projects(self) -> List[str]:
        return list(self._projects)


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

    # 模拟内存消息被释放置空（发生在 _schedule_save 的延迟窗口内）。
    # 释放路径（_release_inactive_session_messages / remove_session(
    # release_messages_only=True)）会同步打 _messages_released 标记——
    # 守卫靠它区分「内存释放，无数据可写」与「显式清空，就是要写空」。
    manager._history_sessions[0]["messages"] = []
    manager._history_sessions[0]["_messages_released"] = True
    manager._pending_save_session_id = "session-1"

    manager._do_save()

    # DB 不得收到空 messages 的覆盖写；pending 需正常清空
    assert store.saved_session_ids == []
    assert manager._pending_save_session_id is None


def test_do_save_writes_explicitly_cleared_empty_messages(monkeypatch, tmp_path):
    """🔴 /compact --clear 根因回归：显式清空的空列表**必须**落库。

    修复前：_do_save 一刀切 `not messages` 跳过 → 清空只在内存生效，
    DB 保留旧消息，任何重载（切会话/重开/重启）都把旧消息读回 =
    「清不掉」。收窄判据后只有带 _messages_released 的释放态才跳过。
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
            "messages": [],  # 显式清空，且**无**释放标记
            "last_time": "2026-10-11 19:40:00",
        }
    ]
    manager._pending_save_session_id = "session-1"

    manager._do_save()

    assert store.saved_session_ids == ["session-1"], "显式清空的会话必须写库（不被空守卫吞）"
    assert store._saved_sessions["session-1"]["messages"] == []


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


# ---------- pinned 字段 + 项目列表门面（历史面板置顶分组） ----------


def _mk_record(sid, project="默认项目", pinned=False, last_time="2026-09-12 00:00:00"):
    return {
        "session_id": sid,
        "title": f"会话{sid}",
        "project": project,
        "last_time": last_time,
        "saved_at": last_time,
        "message_count": 1,
        "preview": "预览",
        "messages": [{"role": "user", "content": "hi", "timestamp": last_time}],
        "pinned": pinned,
    }


def _make_manager_with_records(monkeypatch, tmp_path, records):
    manager = _make_manager(monkeypatch, tmp_path)
    manager._history_sessions = list(records)
    manager._history_loaded = True
    manager._session_store = None  # 非 SQLite 路径：不碰真实 DB
    return manager


def test_lightweight_entry_passes_pinned(monkeypatch, tmp_path):
    manager = _make_manager_with_records(monkeypatch, tmp_path, [_mk_record("a", pinned=True), _mk_record("b")])
    by_id = {s["session_id"]: s for s in manager.get_history_list(None)}
    assert by_id["a"]["pinned"] is True
    assert by_id["b"]["pinned"] is False


def test_save_session_preserves_pinned(monkeypatch, tmp_path):
    manager = _make_manager_with_records(monkeypatch, tmp_path, [_mk_record("a", pinned=True)])
    # 重新保存同一会话（不带 pinned 参数）→ 置顶状态保留
    manager.save_session(
        [{"role": "user", "content": "hi2", "timestamp": "2026-09-12 01:00:00"}],
        title="会话a",
        session_id="a",
    )
    assert manager._history_sessions[0]["pinned"] is True


def test_set_session_pinned_updates_memory(monkeypatch, tmp_path):
    manager = _make_manager_with_records(monkeypatch, tmp_path, [_mk_record("a"), _mk_record("b")])
    assert manager.set_session_pinned("a", True) is True
    assert manager.set_session_pinned("不存在", True) is False
    by_id = {s["session_id"]: s for s in manager.get_history_list(None)}
    assert by_id["a"]["pinned"] is True
    assert by_id["b"]["pinned"] is False


def test_get_project_list_distinct_sorted(monkeypatch, tmp_path):
    records = [_mk_record("a", project="zero"), _mk_record("b", project="DriFox"), _mk_record("c", project="zero")]
    manager = _make_manager_with_records(monkeypatch, tmp_path, records)
    assert manager.get_project_list() == ["DriFox", "zero"]


def test_get_project_list_sqlite_includes_projects_without_sessions(monkeypatch, tmp_path):
    """SQLite 模式：仅有 key_documents 记录的空项目也必须出现在项目列表。

    回归（2026-09-13）：新建项目只写 key_documents（工作目录），首轮对话后才
    落 sessions；get_project_list 若只从会话聚合，新建项目 A 后再新建 B，
    A 失去「当前项目」兑底即从列表消失，且查重命中导致无法重建同名。
    """
    manager = _make_manager(monkeypatch, tmp_path)
    manager._history_loaded = True
    manager._history_sessions = [_mk_record("a", project="DriFox")]
    # 真实 store 口径：sessions ∪ key_documents，含无会话的空项目 zero
    manager._session_store = _FakeSessionStore([], projects=["DriFox", "zero"])
    manager._use_sqlite = True
    assert manager.get_project_list() == ["DriFox", "zero"]


# ---------- /compact --clear 显式清空落库 + 空守卫收窄（2026-10-11 根因修复） ----------


def _clearable_manager(monkeypatch, tmp_path, sid="session-1"):
    """构造一条内存记录含真实消息、且已挂 sqlite store 的 manager。"""
    manager = _make_manager(monkeypatch, tmp_path)
    manager._history_loaded = True
    manager._history_sessions = [
        {
            "session_id": sid,
            "title": "会话 1",
            "project": "默认项目",
            "messages": [
                {"role": "user", "content": "旧提问", "timestamp": "2026-10-11 19:00:00"},
                {"role": "assistant", "content": "旧回复", "timestamp": "2026-10-11 19:00:01"},
            ],
            "compaction_cache": {},
            "compaction_state": {},
            "last_time": "2026-10-11 19:00:01",
        }
    ]
    manager._session_store = _FakeSessionStore([_lightweight_session(1)])
    manager._use_sqlite = True
    return manager, manager._session_store


def test_save_cleared_session_writes_empty_messages(monkeypatch, tmp_path):
    """🔴 /compact --clear 核心回归：save_cleared_session 必须把空列表写进 store。

    修复前清空回调全程不落库；即便走延迟保存链也会被「空消息守卫」吞掉 →
    DB 保留旧消息 → 任何重载把旧消息读回 = 用户看到「清不掉」。
    """
    manager, store = _clearable_manager(monkeypatch, tmp_path)

    ok = manager.save_cleared_session("session-1", compaction_cache={"active": True, "kind": "auto_compact"})

    assert ok is True
    assert store.saved_session_ids == ["session-1"]
    saved = store._saved_sessions["session-1"]
    assert saved["messages"] == [], "清空后的空列表必须落库（不被空守卫吞）"
    assert saved["compaction_cache"]["active"] is True, "压缩缓存须随清空一并落库"
    # 内存记录同步替换为清空版，避免后续 _do_save 又把旧消息写回
    assert manager._history_sessions[0]["session_id"] == "session-1"
    assert manager._history_sessions[0]["messages"] == []


def test_save_cleared_session_invalidates_pending_save(monkeypatch, tmp_path):
    """清空需作废该会话排队中的延迟保存，否则旧快照会在清空写盘后再次写回。"""
    manager, store = _clearable_manager(monkeypatch, tmp_path)
    manager._pending_save_session_id = "session-1"
    manager._save_timer = object()

    manager.save_cleared_session("session-1")

    assert manager._pending_save_session_id is None
    assert manager._save_timer is None

# ---------- 懒加载查询失败：不得把「查询失败」固化成「真的没有历史」（启动空列表根因） ----------

def test_lazy_load_query_failure_keeps_retryable_state(monkeypatch, tmp_path):
    """🔴 回归（2026-10-31 启动空历史）：启动瞬时 disk I/O error 时
    get_all_lightweight 静默返回 [] → _ensure_history_loaded 把空列表当真实
    数据固化 _history_loaded=True → 整个会话期历史列表为空，直到用户对话
    触发 db 签名变化才恢复。修复后：失败返回 None，加载态保持未加载可重试，
    且重试成功后必须置脏轻量缓存（失败窗口期已缓存空快照）。"""
    manager = _make_manager(monkeypatch, tmp_path)
    store = _FakeSessionStore([_lightweight_session(1), _lightweight_session(2)])
    manager._use_sqlite = True
    manager._session_store = store

    # 模拟瞬时磁盘故障：查询失败（store 层返回 None，非合法空列表）
    monkeypatch.setattr(store, "get_sessions_lightweight", lambda limit=500: None)
    result = manager.get_history_list()
    assert manager._history_loaded is False, "查询失败不得固化已加载态（否则永不重试）"
    assert result == [], "失败窗口期对外呈现空列表（不抛异常）"

    # 磁盘恢复 → 下次读取自动重试加载全量
    monkeypatch.setattr(
        store, "get_sessions_lightweight", lambda limit=500: [dict(s) for s in store.sessions[:limit]]
    )
    result = manager.get_history_list()
    assert manager._history_loaded is True, "恢复后重试应完成加载"
    assert len(result) == 2, "重试成功后 UI 读取必须拿到全量（缓存已被置脏重建）"

def test_lazy_load_success_marks_cache_dirty_for_later_readers(monkeypatch, tmp_path):
    """空快照已入缓存后，懒加载成功必须置脏缓存，否则后续读取者命中陈旧空快照。"""
    manager = _make_manager(monkeypatch, tmp_path)
    store = _FakeSessionStore([_lightweight_session(1)])
    manager._use_sqlite = True
    manager._session_store = store

    # 第一次：失败 → 空快照入缓存
    monkeypatch.setattr(store, "get_sessions_lightweight", lambda limit=500: None)
    manager.get_history_list()
    assert manager._cached_lightweight.get(None) == []

    # 第二次：成功 → 缓存必须重建而非命中空快照
    monkeypatch.setattr(store, "get_sessions_lightweight", lambda limit=500: [dict(store.sessions[0])])
    result = manager.get_history_list()
    assert len(result) == 1, "加载成功后不得再返回缓存中的空快照"

def test_reload_query_failure_keeps_memory_intact(monkeypatch, tmp_path):
    """跨进程重载查询失败（返回 None）时不得把内存历史污染成 None。"""
    manager = _make_manager(monkeypatch, tmp_path)
    store = _FakeSessionStore([_lightweight_session(1)])
    manager._use_sqlite = True
    manager._session_store = store
    manager._history_loaded = True
    manager._history_sessions = [_lightweight_session(9)]
    manager._db_signature_stored = (0.9,)
    monkeypatch.setattr(manager, "_db_signature", lambda: (1.0, 2.0))
    monkeypatch.setattr(store, "get_sessions_lightweight", lambda limit=500: None)

    manager._reload_if_db_changed()

    assert manager._history_sessions and manager._history_sessions[0]["session_id"] == "session-9", (
        "重载失败必须保留内存现有数据（不得被 None 污染）"
    )


def test_save_cleared_session_returns_false_when_session_absent(monkeypatch, tmp_path):
    """会话不在内存记录中时跳过（返回 False），不得凭空造记录。"""
    manager, store = _clearable_manager(monkeypatch, tmp_path)
    assert manager.save_cleared_session("not-there") is False
    assert store.saved_session_ids == []


def test_save_cleared_session_requires_sqlite(monkeypatch, tmp_path):
    """未启用 SQLite 时返回 False（无落库目标）。"""
    manager, _store = _clearable_manager(monkeypatch, tmp_path)
    manager._use_sqlite = False
    assert manager.save_cleared_session("session-1") is False


def test_flush_async_skips_only_released_target(monkeypatch, tmp_path):
    """flush_async 与 _do_save 同口径：只拦释放态，显式清空照写。"""
    manager, store = _clearable_manager(monkeypatch, tmp_path)

    # flush_async 把写盘丢给后台线程；测试里换成同步执行以消除竞态
    class _SyncThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    monkeypatch.setattr(history_module.threading, "Thread", _SyncThread)

    # 释放态：被打 _messages_released 标记 → 不写
    manager._history_sessions[0]["messages"] = []
    manager._history_sessions[0]["_messages_released"] = True
    manager._pending_save_session_id = "session-1"
    manager.flush_async()
    assert store.saved_session_ids == [], "释放态不得回写空列表覆盖 DB"

    # 显式清空：无标记 → 写
    del manager._history_sessions[0]["_messages_released"]
    manager._pending_save_session_id = "session-1"
    manager.flush_async()
    assert store.saved_session_ids == ["session-1"], "显式清空必须落库"


# ---------- 轻量列表缓存：多 key 交错读取不得返回陈旧列表（2026-10-15 根因） ----------


def test_cache_key_interleave_must_not_serve_stale_list(monkeypatch, tmp_path):
    """🔴 回归：缓存多 key 共用单一脏标志 → 交错读取会返回不含最新会话的旧列表。

    根因序列（真实链路：历史页 merge_team=True，欢迎卡片/工作区树 merge_team 缺省）：
      读 key1(merge_team) → 保存新会话置脏 → 读 key2 清脏 → 再读 key1 命中旧缓存。
    修复前 key1 返回保存前的快照 → 用户「历史卡片看不到刚结束的会话」。
    """
    manager = _make_manager_with_records(monkeypatch, tmp_path, [_mk_record("a"), _mk_record("b")])

    # ① 历史页先构建 merge_team 键缓存
    first = [s["session_id"] for s in manager.get_history_list("默认项目", merge_team=True)]
    assert first == ["a", "b"], first

    # ② 一轮对话结束：保存新会话（置脏）
    manager.save_session(
        [{"role": "user", "content": "新问题", "timestamp": "2026-09-12 02:00:00"}],
        title="会话c",
        session_id="c",
    )
    assert manager._cache_dirty is True

    # ③ 欢迎卡片/工作区树读取另一个键（清掉全局脏标志）
    other = [s["session_id"] for s in manager.get_history_list("默认项目")]
    assert "c" in other, other

    # ④ 用户查看历史卡片：merge_team 键必须已重建
    final = [s["session_id"] for s in manager.get_history_list("默认项目", merge_team=True)]
    assert "c" in final, f"历史卡片返回陈旧缓存：{final}"


def test_cache_key_interleave_reverse_direction(monkeypatch, tmp_path):
    """反方向交错同样必须拿到新数据（先读普通键、后读 merge_team 键）。"""
    manager = _make_manager_with_records(monkeypatch, tmp_path, [_mk_record("a")])

    # ① 欢迎卡片先构建普通键缓存
    assert [s["session_id"] for s in manager.get_history_list("默认项目")] == ["a"]

    # ② 一轮对话结束：保存新会话（置脏）
    manager.save_session(
        [{"role": "user", "content": "新问题", "timestamp": "2026-09-12 03:00:00"}],
        title="会话b",
        session_id="b",
    )

    # ③ 历史页读 merge_team 键 → 必须已包含 b
    merged = [s["session_id"] for s in manager.get_history_list("默认项目", merge_team=True)]
    assert "b" in merged, f"历史卡片返回陈旧缓存：{merged}"
    # ④ 普通键也已刷新（整表作废后重建，不再是旧快照）
    plain = [s["session_id"] for s in manager.get_history_list("默认项目")]
    assert "b" in plain, f"普通列表返回陈旧缓存：{plain}"
