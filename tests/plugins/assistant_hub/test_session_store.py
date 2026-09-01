# -*- coding: utf-8 -*-
"""test_session_store.py — assistant_hub core/session_store 单元测试。"""
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "session_store.py"


def _load():
    spec = importlib.util.spec_from_file_location("test_session_store_mod", str(_MODULE))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("test_session_store_mod", mod)
    spec.loader.exec_module(mod)
    return mod


m = _load()


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT, project TEXT,"
        " message_count INTEGER, created_at TEXT, updated_at TEXT, preview TEXT, messages BLOB)"
    )
    msgs = [
        {"role": "user", "content": "帮我看看这段代码", "timestamp": "2026-09-01 10:00:00"},
        {"role": "assistant", "content": "<think>内部推理</think>好的，问题在第三行", "timestamp": "2026-09-01 10:00:05"},
        {"role": "system", "content": "<system-reminder>注入</system-reminder>", "timestamp": "2026-09-01 10:00:06"},
        {"role": "user", "content": [{"type": "text", "text": "谢谢"}], "timestamp": "2026-09-01 10:01:00"},
    ]
    blob = b"JSON\x01" + json.dumps(msgs).encode()
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
        ("s1", "调试会话", "DriFox", 4, "2026-09-01 10:00:00", "2026-09-01 10:01:00", "调试", blob),
    )
    conn.commit()
    conn.close()
    return db


def test_deserialize_and_clean(tmp_path):
    db = _make_db(tmp_path)
    conn = m.connect_ro(db)
    row = conn.execute("SELECT messages FROM sessions WHERE session_id='s1'").fetchone()
    raw = m.deserialize_messages(row["messages"])
    assert isinstance(raw, list) and len(raw) == 4
    cleaned = m.clean_messages(raw)
    # system 被滤掉、<think> 被剥、分片 content 被拼文本
    assert [c["role"] for c in cleaned] == ["user", "assistant", "user"]
    assert "内部推理" not in cleaned[1]["text"]
    assert "好的" in cleaned[1]["text"]
    assert cleaned[2]["text"] == "谢谢"
    conn.close()


def test_sessions_between_and_turns_text(tmp_path):
    db = _make_db(tmp_path)
    conn = m.connect_ro(db)
    got = m.sessions_between(conn, "2026-09-01 00:00:00", "2026-09-02 00:00:00")
    assert len(got) == 1 and got[0]["session_id"] == "s1"
    assert got[0]["title"] == "调试会话"
    empty = m.sessions_between(conn, "2026-08-01 00:00:00", "2026-08-02 00:00:00")
    assert empty == []
    text = m.turns_text(got, max_chars=5000)
    assert "user:" in text and "assistant:" in text
    conn.close()


def test_logical_day_boundary():
    before = datetime(2026, 9, 1, 3, 59)
    at = datetime(2026, 9, 1, 4, 0)
    late = datetime(2026, 9, 1, 23, 30)
    assert m.logical_day(before) == "2026-08-31"
    assert m.logical_day(at) == "2026-09-01"
    assert m.logical_day(late) == "2026-09-01"


def test_connect_ro_missing_returns_none(tmp_path):
    assert m.connect_ro(tmp_path / "nope.db") is None
