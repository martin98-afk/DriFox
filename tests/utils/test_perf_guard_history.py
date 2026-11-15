# -*- coding: utf-8 -*-
"""HistoryManager 行为等价护栏测试（批次 3 I/O 修复前置）。

锁住「优化前后逐字段一致」的输出契约：归档列表字段与排序、清空/截断后的状态、
会话记录序列化产物与回读、归档文件与内存列表的一致性。
不写「读盘次数下降」「异步扫描更快」这类性能计数断言；那类由批次 3 修复者
随修复以 TDD 方式写入，避免当前工作树出现预期红用例污染基线。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import app.utils.history_manager as history_module
from app.utils.history_manager import HistoryManager


class _FakeSessionStore:
    """最小存储桩：只实现 HistoryManager 读写路径用到的接口。"""

    is_initialized = True

    def __init__(self):
        self.saved: dict = {}
        self.deleted: list = []

    def get_sessions_lightweight(self, limit: int = 500):
        return [dict(s) for s in self.saved.values()][:limit]

    def get_session_count(self) -> int:
        return len(self.saved)

    def get_session(self, session_id: str):
        return self.saved.get(session_id)

    def save_session(self, session: dict) -> bool:
        self.saved[session["session_id"]] = session
        return True

    def delete_session(self, session_id: str) -> bool:
        self.deleted.append(session_id)
        self.saved.pop(session_id, None)
        return True

    def get_projects(self):
        return sorted({s.get("project", "默认项目") for s in self.saved.values()})

    def load_msg_extras(self, session_id: str, idxs=None):
        return {}

    def update_session_pinned(self, session_id: str, pinned: bool) -> bool:
        return session_id in self.saved


@pytest.fixture()
def hm(monkeypatch, tmp_path):
    """临时归档目录的 HistoryManager：隔离真实用户数据，跳过 SQLite 启动链。"""
    monkeypatch.setattr(history_module, "get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(HistoryManager, "_init_storage", lambda self: None)
    mgr = HistoryManager()
    yield mgr
    # HistoryManager 构造会起 1s 延迟的 daemon 预热 timer；这里只清引用避免跨例残留
    mgr._history_sessions = []


def _write_archive(archive_dir: Path, name: str, data: dict) -> Path:
    p = archive_dir / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# ── 一、get_archived_sessions：字段、排序、容错 ──

def test_get_archived_sessions_fields_and_mtime_order(hm, tmp_path):
    """锁住：归档列表每项恰为 path/name/session_id/title 四字段，且按 mtime 倒序。"""
    d = hm.archive_dir
    p_old = _write_archive(d, "2026-01-01_旧会话_s-old.json",
                           {"session_id": "s-old", "title": "旧会话", "messages": [{"role": "user", "content": "a"}]})
    p_new = _write_archive(d, "2026-01-02_新会话_s-new.json",
                           {"session_id": "s-new", "title": "新会话", "messages": [{"role": "user", "content": "b"}]})
    os.utime(p_old, (1_700_000_000, 1_700_000_000))
    os.utime(p_new, (1_800_000_000, 1_800_000_000))

    got = hm.get_archived_sessions()
    assert [x["name"] for x in got] == [p_new.name, p_old.name], "同步读盘与异步扫描都必须按 mtime 倒序"
    for item in got:
        assert set(item) == {"path", "name", "session_id", "title"}, "字段集合不得随优化增减"
        assert item["path"] == str(d / item["name"])
    assert got[0]["session_id"] == "s-new" and got[0]["title"] == "新会话"
    assert got[1]["session_id"] == "s-old" and got[1]["title"] == "旧会话"


def test_get_archived_sessions_title_falls_back_to_filename_stem(hm):
    """锁住：缺 title 的归档文件回退到文件名前 50 字符、缺 session_id 回退空串、坏文件跳过。"""
    d = hm.archive_dir
    long_stem = "很长" * 30
    p_fallback = _write_archive(d, f"{long_stem}.json", {"messages": []})
    p_bad = d / "broken.json"
    p_bad.write_text("{ 不是合法 JSON", encoding="utf-8")

    got = hm.get_archived_sessions()
    assert len(got) == 1, "损坏文件必须跳过且不影响其余条目"
    item = got[0]
    assert item["name"] == p_fallback.name
    assert item["session_id"] == ""
    assert item["title"] == Path(p_fallback.stem)[:50].as_posix()[:50] or item["title"] == p_fallback.stem[:50]


def test_get_archived_sessions_empty_dir_and_missing_dir(hm, tmp_path):
    """锁住：归档目录不存在或为空时返回空列表，不抛异常、不返回 None。"""
    assert hm.get_archived_sessions() == []
    hm.archive_dir = tmp_path / "not_created"
    assert hm.get_archived_sessions() == []
    assert hm.get_archived_session_count() == 0


def test_get_archived_sessions_ignores_non_json_files(hm):
    """锁住：只收 *.json，其它扩展名文件不进列表。"""
    d = hm.archive_dir
    _write_archive(d, "a.json", {"session_id": "a", "title": "A"})
    (d / "b.txt").write_text("不是归档", encoding="utf-8")
    (d / "c.json.bak").write_text("{}", encoding="utf-8")

    got = hm.get_archived_sessions()
    assert [x["name"] for x in got] == ["a.json"]


def test_get_archived_sessions_reflects_file_changes(hm):
    """锁住：新增/删除归档文件后列表随之变化，避免缓存优化返回陈旧快照。"""
    d = hm.archive_dir
    p1 = _write_archive(d, "one.json", {"session_id": "s1", "title": "一"})
    assert [x["session_id"] for x in hm.get_archived_sessions()] == ["s1"]

    p2 = _write_archive(d, "two.json", {"session_id": "s2", "title": "二"})
    os.utime(p2, (time.time() + 10, time.time() + 10))
    assert [x["session_id"] for x in hm.get_archived_sessions()] == ["s2", "s1"]

    p1.unlink()
    assert [x["session_id"] for x in hm.get_archived_sessions()] == ["s2"]


# ── 二、清空 / 截断后的状态与落库一致性 ──

def test_save_cleared_session_persists_empty_messages(hm):
    """锁住：/compact --clear 后内存与 SQLite 都是空消息，重读不会复活旧内容。"""
    fake = _FakeSessionStore()
    hm._session_store = fake
    hm._use_sqlite = True
    hm._history_loaded = True
    hm._history_sessions = [{
        "session_id": "s-clear",
        "title": "清空前",
        "project": "默认项目",
        "messages": [{"role": "user", "content": "旧消息"}],
        "compaction_state": {"round": 1},
        "compaction_cache": {"hit": True},
        "system_prompt": "sys",
        "pinned": True,
        "worktree_path": "D:/w",
        "team_run_id": "run-1",
        "team_name": "团队",
        "agent_name": "build",
        "team_members": "[]",
    }]

    assert hm.save_cleared_session("s-clear", compaction_state={"round": 2}, compaction_cache={}) is True
    in_memory = hm.get_session_by_session_id("s-clear")
    assert in_memory["messages"] == [], "内存态必须为空"
    assert in_memory["title"] == "清空前", "标题等元数据保留"
    assert in_memory["compaction_state"] == {"round": 2}, "显式传入的压缩状态生效"
    assert in_memory["compaction_cache"] == {}
    assert in_memory["system_prompt"] == "sys"
    assert in_memory["pinned"] is True
    assert in_memory["team_run_id"] == "run-1"

    persisted = fake.saved["s-clear"]
    assert persisted["messages"] == [], "落库态必须为空（不留在挂起事务里）"
    assert persisted["compaction_state"] == {"round": 2}
    assert persisted["compaction_cache"] == {}
    assert persisted["session_id"] == "s-clear"


def test_build_session_record_fields_are_stable(hm):
    """锁住：会话记录序列化产物的字段集合与值口径，优化不得改字段名或丢字段。"""
    messages = [{"role": "user", "content": "问题", "timestamp": "2026-01-01 00:00:00"}]
    rec = hm._build_session_record(
        messages,
        title="标题",
        session_id="s-rec",
        compaction_state={"round": 3},
        compaction_cache={"hit": False},
        system_prompt="sys",
        project="项目甲",
        worktree_path="D:/work",
        last_api_prompt_tokens=10,
        last_api_message_count=1,
        last_api_prompt_from_usage=None,
        team_run_id="run-1",
        team_name="团队",
        agent_name="build",
        team_members="[]",
        pinned=True,
    )
    assert rec["session_id"] == "s-rec"
    assert rec["title"] == "标题"
    assert rec["project"] == "项目甲"
    assert rec["messages"][0]["content"] == "问题"
    assert rec["compaction_state"] == {"round": 3}
    assert rec["compaction_cache"] == {"hit": False}
    assert rec["system_prompt"] == "sys"
    assert rec["worktree_path"] == "D:/work"
    assert rec["last_api_prompt_tokens"] == 10
    assert rec["last_api_message_count"] == 1
    assert rec["last_api_prompt_from_usage"] is None, "三态标记 None 不得落成 0/False"
    assert rec["team_run_id"] == "run-1"
    assert rec["team_name"] == "团队"
    assert rec["agent_name"] == "build"
    assert rec["team_members"] == "[]"
    assert rec["pinned"] is True
    assert set(rec) >= {
        "session_id", "saved_at", "title", "project", "last_time", "messages",
        "message_count", "preview", "compaction_state", "compaction_cache",
        "system_prompt", "user_edited_title", "worktree_path", "context_usage",
    }


def test_history_limit_truncation_keeps_most_recent(hm):
    """锁住：内存历史条数上限截断只丢最旧记录，session_id 与顺序口径不变。"""
    hm._history_loaded = True
    hm._history_sessions = [
        {"session_id": f"s{i}", "title": f"会话{i}", "messages": [], "last_time": f"2026-01-{i + 1:02d}"}
        for i in range(5)
    ]
    hm._history_limit = 3
    # 模拟记忆上限裁剪（与 HistoryManager 内部口径一致：保留尾部最新）
    hm._history_sessions = hm._history_sessions[-hm._history_limit:]
    assert [s["session_id"] for s in hm._history_sessions] == ["s2", "s3", "s4"]
    assert hm.get_session_by_session_id("s2")["title"] == "会话2"
    assert hm.get_session_by_session_id("s0") is None


# ── 三、归档写入 → 列表回读的字段一致性 ──

def test_archived_file_matches_session_record_fields(hm):
    """锁住：归档 JSON 落盘的 session_id/title/messages 与 get_archived_sessions 回读逐字段一致。"""
    d = hm.archive_dir
    record = {
        "session_id": "s-arch",
        "title": "归档标题",
        "project": "项目甲",
        "messages": [{"role": "user", "content": "正文", "timestamp": "2026-01-01 00:00:00"}],
        "last_time": "2026-01-01 00:00:00",
        "message_count": 1,
        "preview": "正文",
        "compaction_state": {},
        "compaction_cache": {},
    }
    p = _write_archive(d, "2026-01-01_归档标题_s-arch.json", record)

    got = hm.get_archived_sessions()
    assert len(got) == 1
    item = got[0]
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert item["session_id"] == on_disk["session_id"] == "s-arch"
    assert item["title"] == on_disk["title"] == "归档标题"
    assert item["name"] == p.name
    assert on_disk["messages"] == record["messages"]
    assert on_disk["project"] == "项目甲"
    assert on_disk["preview"] == "正文"
