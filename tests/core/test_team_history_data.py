# -*- coding: utf-8 -*-
"""团队会话数据层能力测试（子任务 M3）

覆盖三个新增能力（均在数据层，不改变 get_history_list 默认行为）：
- get_history_list(merge_team=True)：团队会话按 run_id 合并为单一条目
- archive_sessions_by_run_id：按 run_id 归档全部成员会话
- get_team_first_question：团队首问预览（供合并条目 preview 使用）

测试策略：
- 会话通过 store.save_session 直接写入 SQLite（绕开 HistoryManager.save_session
  内部的 consolidate_messages id 复用缓存，避免跨测试脏命中）
- merge 逻辑基于内存 _history_sessions（轻量记录），测试中手动注入带区分度
  last_time 的轻量记录，保证排序断言确定
"""

import pytest

from app.core.store.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    """用临时目录构造全新 SessionStore（重置单例，避免污染真实数据库）。"""
    SessionStore._instance = None
    s = SessionStore(str(tmp_path))
    assert s.is_initialized, "SessionStore 应初始化成功"
    yield s
    try:
        s.close()
    except Exception:
        pass
    SessionStore._instance = None
    from app.utils.db_manager import DatabaseManager

    try:
        DatabaseManager._instance = None
    except Exception:
        pass


@pytest.fixture
def hm(store, tmp_path):
    """构造 HistoryManager，归档目录重定向到临时目录避免污染真实数据。"""
    from app.utils.history_manager import HistoryManager

    manager = HistoryManager()
    manager.archive_dir = tmp_path / "archived"
    manager.archive_dir.mkdir(parents=True, exist_ok=True)
    yield manager
    manager.flush()


def _full_session(
    session_id: str,
    title: str,
    content: str,
    ts: str,
    team_run_id: str = "",
    team_name: str = "",
    agent_name: str = "",
) -> dict:
    """构造可直写 SQLite 的完整会话记录。"""
    return {
        "session_id": session_id,
        "title": title,
        "project": "默认项目",
        "messages": [{"role": "user", "content": content, "timestamp": ts}],
        "system_prompt": "",
        "compaction_state": {},
        "compaction_cache": {},
        "message_count": 1,
        "user_edited_title": False,
        "worktree_path": "",
        "preview": content,
        "context_usage": 0,
        "team_run_id": team_run_id,
        "team_name": team_name,
        "agent_name": agent_name,
    }


def _light(
    session_id: str, title: str, last_time: str, team_run_id: str = "", team_name: str = "", agent_name: str = ""
) -> dict:
    """构造内存轻量记录（与 _to_lightweight_entry 字段结构一致）。"""
    return {
        "session_id": session_id,
        "saved_at": last_time,
        "title": title,
        "project": "默认项目",
        "last_time": last_time,
        "message_count": 1,
        "preview": "",
        "user_edited_title": False,
        "worktree_path": "",
        "team_run_id": team_run_id,
        "team_name": team_name,
        "agent_name": agent_name,
    }


def _seed(hm, sessions: list, lights: list):
    """写入 SQLite 并注入内存轻量记录，_history_loaded 置 True 避免懒加载覆盖。"""
    for s in sessions:
        hm._session_store.save_session(s)
    hm._history_loaded = True
    hm._history_sessions = list(lights)
    hm._cache_dirty = True


def test_default_list_unchanged(hm):
    """默认行为不变：无 merge_team 参数时团队会话逐条出现（N 成员 N 条）。"""
    _seed(
        hm,
        [
            _full_session("s1", "a", "hello", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            _full_session("s2", "b", "hi", "2026-01-01 00:00:02", "run-1", "dev", "plan"),
        ],
        [
            _light("s1", "a", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            _light("s2", "b", "2026-01-01 00:00:02", "run-1", "dev", "plan"),
        ],
    )

    rows = hm.get_history_list()
    assert len(rows) == 2
    assert {r["session_id"] for r in rows} == {"s1", "s2"}
    # 团队元数据仍透传（老行为）
    row = next(r for r in rows if r["session_id"] == "s1")
    assert row["team_run_id"] == "run-1"
    assert row["agent_name"] == "build"


def test_merge_team_single_entry(hm):
    """merge_team=True：同 run_id 的成员会话合并为单一条目，字段完整。"""
    _seed(
        hm,
        [
            _full_session("s1", "a", "首问：实现登录", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            _full_session("s2", "b", "收到", "2026-01-01 00:00:02", "run-1", "dev", "plan"),
            _full_session("s3", "c", "普通会话", "2026-01-01 00:00:03"),
        ],
        [
            _light("s1", "a", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            _light("s2", "b", "2026-01-01 00:00:02", "run-1", "dev", "plan"),
            _light("s3", "c", "2026-01-01 00:00:03"),
        ],
    )

    rows = hm.get_history_list(merge_team=True)
    # s1/s2 合并为 1 条 + s3 普通 = 2 条
    assert len(rows) == 2

    merged = next(r for r in rows if r.get("team_merged"))
    assert merged["team_run_id"] == "run-1"
    assert merged["team_name"] == "dev"
    assert set(merged["agent_names"]) == {"build", "plan"}
    assert merged["member_count"] == 2
    assert merged["team_merged"] is True
    # session_id / last_time 取组内 last_time 最新会话
    assert merged["session_id"] == "s2"
    assert merged["last_time"] == "2026-01-01 00:00:02"
    # preview = 团队首问（最早会话的第一条 user 消息）
    assert merged["preview"] == "首问：实现登录"

    normal = next(r for r in rows if not r.get("team_merged"))
    assert normal["session_id"] == "s3"
    assert normal["team_run_id"] == ""


def test_merge_team_keeps_multiple_runs(hm):
    """merge_team=True：多个不同 run_id 各自合并，普通会话保持独立。"""
    _seed(
        hm,
        [
            _full_session("s1", "a", "run1 首问", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            _full_session("s2", "b", "run2 首问", "2026-01-01 00:00:02", "run-2", "qa", "tester"),
        ],
        [
            _light("s1", "a", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            _light("s2", "b", "2026-01-01 00:00:02", "run-2", "qa", "tester"),
        ],
    )

    rows = hm.get_history_list(merge_team=True)
    assert len(rows) == 2
    run_ids = {r["team_run_id"] for r in rows}
    assert run_ids == {"run-1", "run-2"}
    assert all(r["team_merged"] for r in rows)
    assert all(r["member_count"] == 1 for r in rows)


def test_archive_sessions_by_run_id(hm, store):
    """按 run_id 归档：该 run 下全部会话写 JSON + 从内存/SQLite 删除，其他不受影响。"""
    _seed(
        hm,
        [
            _full_session("s1", "团队A", "hello", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            _full_session("s2", "团队B", "hi", "2026-01-01 00:00:02", "run-1", "dev", "plan"),
            _full_session("s3", "其他团队", "other", "2026-01-01 00:00:03", "run-2", "qa", "tester"),
        ],
        [
            _light("s1", "团队A", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            _light("s2", "团队B", "2026-01-01 00:00:02", "run-1", "dev", "plan"),
            _light("s3", "其他团队", "2026-01-01 00:00:03", "run-2", "qa", "tester"),
        ],
    )

    count = hm.archive_sessions_by_run_id("run-1")
    assert count == 2

    # SQLite：run-1 两条已删除，run-2 保留
    assert store.get_session("s1") is None
    assert store.get_session("s2") is None
    assert store.get_session("s3") is not None

    # 内存：run-1 两条已移除
    ids = {s.get("session_id") for s in hm._history_sessions}
    assert "s1" not in ids
    assert "s2" not in ids
    assert "s3" in ids

    # 归档区：逐条显示成员会话（2 个 JSON 文件，不做合并）
    archived = list(hm.archive_dir.glob("*.json"))
    assert len(archived) == 2


def test_archive_sessions_by_run_id_empty(hm):
    """未知 run_id 归档返回 0，不抛异常。"""
    assert hm.archive_sessions_by_run_id("no-such-run") == 0
    assert hm.archive_sessions_by_run_id("") == 0


def test_get_team_first_question(hm):
    """首问取最早 last_time 会话的第一条 user 消息，跳过 _hook_event。"""
    _seed(
        hm,
        [
            {
                "session_id": "s1",
                "title": "a",
                "project": "默认项目",
                "messages": [
                    {
                        "role": "user",
                        "content": "[system] 团队启动",
                        "timestamp": "2026-01-01 00:00:00",
                        "_hook_event": "SessionStart",
                    },
                    {"role": "user", "content": "团队目标是什么", "timestamp": "2026-01-01 00:00:01"},
                ],
                "system_prompt": "",
                "compaction_state": {},
                "compaction_cache": {},
                "message_count": 1,
                "user_edited_title": False,
                "worktree_path": "",
                "preview": "",
                "context_usage": 0,
                "team_run_id": "run-1",
                "team_name": "dev",
                "agent_name": "build",
            },
            _full_session("s2", "b", "hello", "2026-01-01 00:00:03", "run-1", "dev", "plan"),
        ],
        [
            _light("s1", "a", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            _light("s2", "b", "2026-01-01 00:00:03", "run-1", "dev", "plan"),
        ],
    )

    assert hm.get_team_first_question("run-1") == "团队目标是什么"


def test_get_team_first_question_truncate(hm):
    """首问超长时按 max_len 截断并加省略号。"""
    _seed(
        hm,
        [
            _full_session("s1", "a", "x" * 100, "2026-01-01 00:00:01", "run-1", "dev", "build"),
        ],
        [
            _light("s1", "a", "2026-01-01 00:00:01", "run-1", "dev", "build"),
        ],
    )

    assert hm.get_team_first_question("run-1", max_len=10) == "x" * 10 + "..."


def test_get_team_first_question_empty(hm):
    """无会话 / 无 user 消息时返回空串。"""
    assert hm.get_team_first_question("no-such-run") == ""
    assert hm.get_team_first_question("") == ""


def test_get_team_first_question_uses_message_ts_not_updated_at(hm):
    """I-1 回归：light last_time（updated_at 保存时刻）相同/接近时，
    首问必须用完整记录的消息时间戳区分最早成员，而非轻量记录 last_time。

    场景：s1 消息时间戳 00:00:01（最早），s2 消息时间戳 00:00:30（更晚）；
    但两者 updated_at（保存时刻）相同。若误用 updated_at 会选错成员。
    """
    import time as _time

    # 直接构造完整记录（不经 HistoryManager.save_session），
    # updated_at 由 store.save_session 写入（同一秒 → 相同）
    s1 = _full_session("s1", "a", "首问内容", "2026-01-01 00:00:01", "run-1", "dev", "build")
    s2 = _full_session("s2", "b", "后续消息", "2026-01-01 00:00:30", "run-1", "dev", "plan")
    hm._session_store.save_session(s1)
    hm._session_store.save_session(s2)

    # 轻量记录 last_time 均为空串 → 之前实现会退化；现在必须走消息时间戳
    hm._history_loaded = True
    hm._history_sessions = [
        _light("s1", "a", "", "run-1", "dev", "build"),
        _light("s2", "b", "", "run-1", "dev", "plan"),
    ]
    hm._cache_dirty = True

    assert hm.get_team_first_question("run-1") == "首问内容"


def test_get_team_first_question_same_updated_at_distinct_msg_ts(hm):
    """I-1 补充：updated_at 完全相同（同秒保存）但消息时间戳区分明显。"""
    s1 = _full_session("s1", "a", "最早", "2026-01-01 00:00:01", "run-1", "dev", "build")
    s2 = _full_session("s2", "b", "较晚", "2026-01-01 00:00:02", "run-1", "dev", "plan")
    hm._session_store.save_session(s1)
    hm._session_store.save_session(s2)

    # 构造相同 updated_at 的轻量记录（模拟同轮保存）
    light1 = _light("s1", "a", "2026-01-01 00:00:00", "run-1", "dev", "build")
    light2 = _light("s2", "b", "2026-01-01 00:00:00", "run-1", "dev", "plan")
    hm._history_loaded = True
    hm._history_sessions = [light1, light2]
    hm._cache_dirty = True

    assert hm.get_team_first_question("run-1") == "最早"


class TestTeamFirstQuestionI1:
    """I-1 补测核对（plan 蓝图场景 6）"""

    def test_first_question_uses_message_ts_when_light_last_time_tied(self):
        """light last_time（updated_at）相同而消息时间戳区分明显 → 取真正最早的。

        M4 已补同语义测试（test_get_team_first_question_same_updated_at_distinct_msg_ts），
        此处显式声明核对：轻量记录 last_time 并列时首问必须用消息时间戳区分。
        """
        s1 = _full_session("s1", "a", "最早消息", "2026-01-01 00:00:01", "run-1", "dev", "build")
        s2 = _full_session("s2", "b", "较晚消息", "2026-01-01 00:00:02", "run-1", "dev", "plan")
        hm = _hm_fresh()
        hm._session_store.save_session(s1)
        hm._session_store.save_session(s2)
        # 两条轻量记录 last_time 相同（模拟同轮保存 updated_at 并列）
        hm._history_loaded = True
        hm._history_sessions = [
            _light("s1", "a", "2026-01-01 00:00:00", "run-1", "dev", "build"),
            _light("s2", "b", "2026-01-01 00:00:00", "run-1", "dev", "plan"),
        ]
        hm._cache_dirty = True
        assert hm.get_team_first_question("run-1") == "最早消息"

    def test_first_question_same_message_ts_returns_first(self):
        """两会话消息时间戳完全相同 → 返回确定的首个（防止实现任意选择）。

        按 get_team_sessions_by_run_id 返回顺序取首个会话，保证结果稳定。
        """
        s1 = _full_session("s1", "a", "消息一", "2026-01-01 00:00:01", "run-1", "dev", "build")
        s2 = _full_session("s2", "b", "消息二", "2026-01-01 00:00:01", "run-1", "dev", "plan")
        hm = _hm_fresh()
        hm._session_store.save_session(s1)
        hm._session_store.save_session(s2)
        hm._history_loaded = True
        hm._history_sessions = [
            _light("s1", "a", "2026-01-01 00:00:00", "run-1", "dev", "build"),
            _light("s2", "b", "2026-01-01 00:00:00", "run-1", "dev", "plan"),
        ]
        hm._cache_dirty = True
        result = hm.get_team_first_question("run-1")
        # 消息时间戳相同：min 取首个（按 candidates 顺序），返回确定值且不抛异常
        assert result in ("消息一", "消息二"), "时间戳相同时应返回确定的某个首问"
        # 再次调用结果稳定
        assert hm.get_team_first_question("run-1") == result, "结果应稳定可复现"


def _hm_fresh():
    """构造独立 HistoryManager（复用 session_store 单例临时库）。"""
    from app.utils.history_manager import HistoryManager

    hm = HistoryManager()
    hm.archive_dir = None  # 归档测试才需要；本类不用
    return hm


class TestSaveBranchPreserveTeamMeta:
    """R-fix I-1：save_session 分支 None→保留现值语义（数据损坏防护）

    会话被 _history_limit 挤出内存（find_index_by_session_id 返回 None）后，
    _auto_save_current_session 走 save_session 分支（INSERT OR REPLACE）。
    若 save_session 用空串覆盖团队元数据，团队会话从历史分组消失（不可逆）。
    save_session 必须对 None 参数保留现值，显式空串才清空。
    """

    def test_save_none_preserves_existing_team_meta(self, hm):
        """已有团队会话：save_session 不传 team 参数（None）→ 保留现值。"""
        # 先落库一个团队会话（经 save_session 传 team 字段）
        hm.save_session(
            [{"role": "user", "content": "hi", "timestamp": "2026-01-01 00:00:01"}],
            title="团队会话",
            session_id="s-team",
            team_run_id="run-1",
            team_name="dev",
            agent_name="build",
        )
        hm.flush()
        # 模拟会话被挤出内存（_history_sessions 清空，仅 SQLite 有记录）
        hm._history_sessions = []
        hm._cache_dirty = True

        # 非团队窗口普通编辑 → save_session 不传 team 参数（None）
        hm.save_session(
            [
                {"role": "user", "content": "hi", "timestamp": "2026-01-01 00:00:01"},
                {"role": "assistant", "content": "ok", "timestamp": "2026-01-01 00:00:02"},
            ],
            title="团队会话",
            session_id="s-team",
        )
        hm.flush()

        # 团队元数据必须保留（未被空串覆盖）
        saved = hm.get_session_by_session_id("s-team")
        assert saved is not None
        assert saved["team_run_id"] == "run-1", "save 分支不应清空 team_run_id"
        assert saved["team_name"] == "dev", "save 分支不应清空 team_name"
        assert saved["agent_name"] == "build", "save 分支不应清空 agent_name"

    def test_save_explicit_empty_clears_team_meta(self, hm):
        """显式传空串 "" → 仍清空团队元数据（保留显式清空能力）。"""
        hm.save_session(
            [{"role": "user", "content": "hi", "timestamp": "2026-01-01 00:00:01"}],
            title="团队会话",
            session_id="s-team2",
            team_run_id="run-1",
            team_name="dev",
            agent_name="build",
        )
        hm.flush()

        # 显式传空串（确有清空需求的场景）→ 覆盖为空
        hm.save_session(
            [{"role": "user", "content": "hi", "timestamp": "2026-01-01 00:00:01"}],
            title="团队会话",
            session_id="s-team2",
            team_run_id="",
            team_name="",
            agent_name="",
        )
        hm.flush()

        saved = hm.get_session_by_session_id("s-team2")
        assert saved["team_run_id"] == ""
        assert saved["team_name"] == ""
        assert saved["agent_name"] == ""

    def test_save_none_new_session_defaults_empty(self, hm):
        """全新会话：save_session 不传 team 参数 → 回落空串（默认非团队）。"""
        hm.save_session(
            [{"role": "user", "content": "hi", "timestamp": "2026-01-01 00:00:01"}],
            title="普通会话",
            session_id="s-new",
        )
        hm.flush()
        saved = hm.get_session_by_session_id("s-new")
        assert saved["team_run_id"] == ""
        assert saved["team_name"] == ""
        assert saved["agent_name"] == ""
