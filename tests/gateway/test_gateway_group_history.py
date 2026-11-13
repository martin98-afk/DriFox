# -*- coding: utf-8 -*-
"""Gateway 群聊历史存档单测。

覆盖：
- 只存群聊、空文本与重复 message_id 忽略
- recent 时间正序、limit、排除当前消息、14 天过期过滤
- 行数超限时压缩保留最近消息
- format_group_context 优先保留最近几条且总字数受控
"""
import json
import time

from app.gateway.group_history import (
    GroupHistoryStore,
    format_group_context,
)


def _store(tmp_path, **kwargs):
    return GroupHistoryStore(tmp_path / "group_history", **kwargs)


def _capture(store, text, message_id, **kwargs):
    return store.capture(
        "feishu", "chat_1", text=text, message_id=message_id, chat_type="group", **kwargs
    )


# ==================== 写入 ====================


def test_capture_ignores_dm_and_empty(tmp_path):
    store = _store(tmp_path)
    assert store.capture("feishu", "chat_1", text="私聊", chat_type="dm") is False
    assert _capture(store, "   ", "m0") is False
    assert _capture(store, "群消息", "m1") is True
    assert len(store.recent("feishu", "chat_1")) == 1


def test_capture_dedupes_message_id(tmp_path):
    store = _store(tmp_path)
    assert _capture(store, "第一条", "m1", user_name="张三") is True
    assert _capture(store, "第一条", "m1", user_name="张三") is False
    assert len(store.recent("feishu", "chat_1")) == 1


def test_chats_are_isolated(tmp_path):
    store = _store(tmp_path)
    _capture(store, "群 A", "m1")
    store.capture("telegram", "chat_2", text="群 B", message_id="m2", chat_type="group")
    assert [r["text"] for r in store.recent("feishu", "chat_1")] == ["群 A"]
    assert [r["text"] for r in store.recent("telegram", "chat_2")] == ["群 B"]


# ==================== 读取 ====================


def test_recent_is_ordered_and_limited(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        _capture(store, f"消息{i}", f"m{i}")
    rows = store.recent("feishu", "chat_1", limit=3)
    assert [r["text"] for r in rows] == ["消息2", "消息3", "消息4"]


def test_recent_excludes_current_message(tmp_path):
    store = _store(tmp_path)
    _capture(store, "上一条", "m1")
    _capture(store, "当前", "m2")
    rows = store.recent("feishu", "chat_1", exclude_message_id="m2")
    assert [r["text"] for r in rows] == ["上一条"]


def test_recent_skips_expired(tmp_path):
    store = _store(tmp_path, retention_days=14)
    _capture(store, "新消息", "m2")
    path = store._path("feishu", "chat_1")
    old = {
        "platform": "feishu",
        "chat_id": "chat_1",
        "message_id": "m1",
        "user_name": "张三",
        "text": "半个月前",
        "ts": time.time() - 15 * 86400,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(old, ensure_ascii=False) + "\n")
    rows = store.recent("feishu", "chat_1")
    assert [r["text"] for r in rows] == ["新消息"]


def test_compact_keeps_latest_lines(tmp_path):
    store = _store(tmp_path, max_lines=100)
    for i in range(130):
        _capture(store, f"消息{i}", f"m{i}")
    rows = store.recent("feishu", "chat_1", limit=500)
    assert len(rows) == 100
    assert rows[-1]["text"] == "消息129"
    # 压缩后文件行数与内存计数一致，继续写入不会被反复压缩
    assert store._line_count(store._path("feishu", "chat_1")) == 100


# ==================== 上下文渲染 ====================


def test_format_empty_returns_blank():
    assert format_group_context([]) == ""


def test_format_keeps_recent_and_marks_background():
    rows = [
        {"user_name": "张三", "text": "第一句"},
        {"user_name": "李四", "text": "第二句"},
    ]
    text = format_group_context(rows, max_chars=200)
    assert "仅作背景" in text
    assert text.index("张三") < text.index("李四")
    assert text.endswith("李四: 第二句")


def test_format_prefers_recent_messages_under_budget():
    rows = [{"user_name": f"用户{i}", "text": "内容" * 40} for i in range(20)]
    text = format_group_context(rows, max_chars=300)
    assert len(text) <= 300
    assert "用户19" in text  # 最近一条必须保留
    assert "用户0" not in text


def test_format_collapses_newlines_and_truncates_line():
    rows = [{"user_name": "张三", "text": "第一行\n第二行" + "长" * 500}]
    text = format_group_context(rows, max_chars=500)
    assert "第一行 第二行" in text
    assert len(text) <= 500
