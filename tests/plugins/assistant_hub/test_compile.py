# -*- coding: utf-8 -*-
"""test_compile.py — 四段传送带编译器测试（fake LLM + tmp 目录）。"""

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "compile.py"

spec = importlib.util.spec_from_file_location("test_compile_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_compile_mod", m)
spec.loader.exec_module(m)


class FakeLLM:
    """固定回复的 fake chat_once：记录调用并按序返回。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, **kw):
        self.calls.append(messages[-1]["content"][:40])
        if not self.replies:
            return "（无更多回复）"
        return self.replies.pop(0)


def _seed_sessions(tmp_path, session_rows):
    """造一个只读会话库，返回 (db_path, 帮助函数)。"""
    import sqlite3

    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT, project TEXT,"
        " message_count INTEGER, created_at TEXT, updated_at TEXT, preview TEXT, messages BLOB)"
    )
    for sid, updated, user_text in session_rows:
        msgs = [
            {"role": "user", "content": user_text, "timestamp": updated},
            {"role": "assistant", "content": "好的", "timestamp": updated},
        ]
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            (sid, f"标题{sid}", "proj", 2, updated, updated, "预览", b"JSON\x01" + json.dumps(msgs).encode()),
        )
    conn.commit()
    conn.close()
    return db


def test_compile_today_watermark_increment(tmp_path, monkeypatch):
    db = _seed_sessions(
        tmp_path,
        [
            ("s1", "2026-09-01 10:00:00", "第一轮对话：我在做脱硫项目"),
            ("s2", "2026-09-01 15:00:00", "第二轮对话：记住我喜欢简洁回复"),
        ],
    )
    store = m._core("session_store")
    monkeypatch.setattr(store, "find_db", lambda: db)
    from datetime import datetime as _dt

    aid = tmp_path / "a1"
    aid.mkdir()
    llm = FakeLLM(["- 用户在做脱硫项目\n- 用户偏好简洁回复\n- 新增：记住 Ctrl+S 习惯"])

    # 第一轮：只看到 s1（水位线截取）
    r1 = m.compile_today(aid, llm=llm, now=_dt(2026, 9, 1, 20, 0), _session_filter=lambda s: s["session_id"] == "s1")
    assert r1["changed"] is True
    today_md = (aid / "memory" / "today.md").read_text(encoding="utf-8")
    assert "脱硫" in today_md

    # 第二轮：水位线推进，只编译 s2 增量
    llm2 = FakeLLM(["- 用户偏好简洁回复（强调）"])
    r2 = m.compile_today(aid, llm=llm2, now=_dt(2026, 9, 1, 21, 0), _session_filter=lambda s: s["session_id"] == "s2")
    assert r2["changed"] is True
    # fake llm 只收到增量合并的输入（不再含第一轮文本）
    assert "第一轮对话" not in llm2.calls[0]
    state = json.loads((aid / "memory" / "today-state.json").read_text(encoding="utf-8"))
    assert state["last_msg_cursor"]["s2"] >= 1


def test_compile_today_no_new_content(tmp_path, monkeypatch):
    db = _seed_sessions(tmp_path, [])
    store = m._core("session_store")
    monkeypatch.setattr(store, "find_db", lambda: db)
    aid = tmp_path / "a2"
    aid.mkdir()
    llm = FakeLLM([])
    from datetime import datetime as _dt

    r = m.compile_today(aid, llm=llm, now=_dt(2026, 9, 1, 20, 0))
    assert r["changed"] is False and llm.calls == []


def test_roll_daily_window(tmp_path):
    aid = tmp_path / "a3"
    daily = aid / "memory" / "daily"
    daily.mkdir(parents=True)
    for d in range(1, 8):  # 7 份
        (daily / f"2026-08-{d:02d}.md").write_text(f"- 8月{d}日的事", encoding="utf-8")
    lt = aid / "memory" / "longterm.md"
    lt.write_text("# 长期记忆\n\n- 原有条目", encoding="utf-8")

    folded = m.roll_daily_window(aid, keep=6)
    assert folded == ["2026-08-01"]
    assert not (daily / "2026-08-01.md").exists()
    assert "8月1日的事" in lt.read_text(encoding="utf-8")


def test_assemble_sections(tmp_path):
    aid = tmp_path / "a4"
    mem = aid / "memory"
    (mem / "daily").mkdir(parents=True)
    (mem / "facts.md").write_text("- 用户名是马丁", encoding="utf-8")
    (mem / "today.md").write_text("- 今天在开发助手中心", encoding="utf-8")
    (mem / "daily" / "2026-08-30.md").write_text("- 8月30日重构了网关", encoding="utf-8")
    (mem / "longterm.md").write_text("- 长期事实：用户做工业算法", encoding="utf-8")

    text = m.assemble(aid)
    assert "## 重要事实" in text and "## 今日" in text
    assert "## 近期" in text and "## 长期记忆" in text
    assert "马丁" in text and "工业算法" in text
    assert (mem / "memory.md").read_text(encoding="utf-8") == text


def test_assemble_empty(tmp_path):
    aid = tmp_path / "a5"
    (aid / "memory").mkdir(parents=True)
    text = m.assemble(aid)
    assert text == ""
    assert not (aid / "memory" / "memory.md").exists()
