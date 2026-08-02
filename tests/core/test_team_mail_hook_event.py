# -*- coding: utf-8 -*-
"""团队任务邮件 _hook_event 打标与首问预览跳过测试（子任务 #A）

覆盖两个修复维度：
- R1 源头打标：非流式任务邮件（_process_team_task → _on_send_clicked）以
  hook_event="TeamMail" 写入 session.messages（带 _hook_event 字段）
- R3 防御：get_team_first_question 跳过 "📨 **来自" 前缀（兼容未打标旧记录）
  与 _hook_event 标记（含 TeamMail 场景）两种邮件，取真实用户首问

数据层会话通过 store.save_session 直写 SQLite（绕开 HistoryManager.save_session
内部 consolidate_messages id 复用缓存，避免跨测试脏命中）。
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
    messages: list,
    team_run_id: str = "",
    team_name: str = "",
    agent_name: str = "",
) -> dict:
    """构造可直写 SQLite 的完整会话记录（messages 由调用方自定义）。"""
    last_ts = messages[-1].get("timestamp", "") if messages else ""
    return {
        "session_id": session_id,
        "title": title,
        "project": "默认项目",
        "messages": messages,
        "system_prompt": "",
        "compaction_state": {},
        "compaction_cache": {},
        "message_count": len(messages),
        "user_edited_title": False,
        "worktree_path": "",
        "preview": messages[0].get("content", "") if messages else "",
        "context_usage": 0,
        "team_run_id": team_run_id,
        "team_name": team_name,
        "agent_name": agent_name,
        "last_time": last_ts,
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


def _mail_msg(ts: str, body: str = "请完成登录功能", sender: str = "leader@w0", **extra) -> dict:
    """构造任务邮件消息（默认无 _hook_event，模拟未打标旧记录）。"""
    msg = {
        "role": "user",
        "content": f"📨 **来自 [{sender}] 的任务邮件：**\n\n{body}",
        "timestamp": ts,
    }
    msg.update(extra)
    return msg


# ═══════════════════════════════════════════════════════════════
# R1 源头打标：add_user_message 支持 _hook_event 字段
# ═══════════════════════════════════════════════════════════════


class TestAddUserMessageHookEvent:
    """ChatSession.add_user_message 支持 _hook_event 写入（None 时不写）。"""

    def test_with_hook_event_writes_field(self):
        """传 _hook_event="TeamMail" → 消息带 _hook_event 字段。"""
        from app.core.chat_session import ChatSession

        s = ChatSession()
        s.add_user_message("你好", _hook_event="TeamMail")
        assert s.messages[-1]["_hook_event"] == "TeamMail"
        assert s.messages[-1]["role"] == "user"
        assert s.messages[-1]["content"] == "你好"

    def test_without_hook_event_no_field(self):
        """不传 _hook_event → 消息不带该字段（历史行为不变）。"""
        from app.core.chat_session import ChatSession

        s = ChatSession()
        s.add_user_message("你好")
        assert "_hook_event" not in s.messages[-1]

    def test_with_params_and_hook_event(self):
        """params 与 _hook_event 可共存，互不影响。"""
        from app.core.chat_session import ChatSession

        s = ChatSession()
        s.add_user_message("你好", params={"a": 1}, _hook_event="TeamMail")
        assert s.messages[-1]["params"] == {"a": 1}
        assert s.messages[-1]["_hook_event"] == "TeamMail"


# ═══════════════════════════════════════════════════════════════
# R1 源头打标：非流式任务邮件走 _on_send_clicked(hook_event="TeamMail")
# ═══════════════════════════════════════════════════════════════


class TestProcessTeamTaskHookEvent:
    """_process_team_task 非流式路径以 hook_event="TeamMail" 调用 _on_send_clicked。"""

    def test_non_stream_mail_passes_hook_event(self):
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._window_id = "w1"
        win._get_team_manager.return_value = MagicMock()
        mail = {"id": "m1", "body": "请完成登录功能", "from_agent": "leader", "from_window": "w0"}

        OpenAIChatToolWindow._process_team_task(win, mail)

        win._on_send_clicked.assert_called_once()
        args, kwargs = win._on_send_clicked.call_args
        assert kwargs.get("hook_event") == "TeamMail", "非流式任务邮件必须带 hook_event='TeamMail'"
        assert "📨 **来自 [leader@w0] 的任务邮件：**" in args[0]
        assert "请完成登录功能" in args[0]

    def test_preset_question_no_hook_event(self):
        """预设问题路径不传 hook_event（None）→ 行为不变。"""
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        OpenAIChatToolWindow.send_preset_question(win, "你好")
        win._on_send_clicked.assert_called_once()
        _args, kwargs = win._on_send_clicked.call_args
        assert kwargs.get("hook_event") is None
        assert kwargs.get("user_text") == "你好"


# ═══════════════════════════════════════════════════════════════
# R3 防御：get_team_first_question 跳过邮件内容
# ═══════════════════════════════════════════════════════════════


class TestFirstQuestionSkipsMail:
    """首问预览跳过任务邮件：兼容未打标（📨 前缀）与打标（_hook_event）两种场景。"""

    def test_skips_mail_without_hook_event(self, hm):
        """旧记录无 _hook_event 但以 '📨 **来自' 开头 → 跳过，取真实用户首问。"""
        _seed(
            hm,
            [
                _full_session(
                    "s1",
                    "a",
                    [
                        _mail_msg("2026-01-01 00:00:00", body="请完成登录功能"),
                        {"role": "user", "content": "团队目标是什么", "timestamp": "2026-01-01 00:00:01"},
                    ],
                    "run-1",
                    "dev",
                    "build",
                ),
                _full_session(
                    "s2",
                    "b",
                    [{"role": "user", "content": "hello", "timestamp": "2026-01-01 00:00:03"}],
                    "run-1",
                    "dev",
                    "plan",
                ),
            ],
            [
                _light("s1", "a", "2026-01-01 00:00:01", "run-1", "dev", "build"),
                _light("s2", "b", "2026-01-01 00:00:03", "run-1", "dev", "plan"),
            ],
        )

        assert hm.get_team_first_question("run-1") == "团队目标是什么"

    def test_skips_mail_with_hook_event(self, hm):
        """打标场景：邮件带 _hook_event='TeamMail' → 跳过（与 R1 新写入路径一致）。"""
        _seed(
            hm,
            [
                _full_session(
                    "s1",
                    "a",
                    [
                        _mail_msg("2026-01-01 00:00:00", body="请完成登录功能", _hook_event="TeamMail"),
                        {"role": "user", "content": "真实首问", "timestamp": "2026-01-01 00:00:01"},
                    ],
                    "run-1",
                    "dev",
                    "build",
                ),
                _full_session(
                    "s2",
                    "b",
                    [{"role": "user", "content": "hello", "timestamp": "2026-01-01 00:00:03"}],
                    "run-1",
                    "dev",
                    "plan",
                ),
            ],
            [
                _light("s1", "a", "2026-01-01 00:00:01", "run-1", "dev", "build"),
                _light("s2", "b", "2026-01-01 00:00:03", "run-1", "dev", "plan"),
            ],
        )

        assert hm.get_team_first_question("run-1") == "真实首问"

    def test_mail_only_session_returns_empty(self, hm):
        """会话只有邮件消息（无真实用户消息）→ 首问为空串。"""
        _seed(
            hm,
            [
                _full_session(
                    "s1",
                    "a",
                    [
                        _mail_msg("2026-01-01 00:00:00", body="请完成登录功能"),
                        _mail_msg("2026-01-01 00:00:01", body="请继续", sender="plan@w2"),
                    ],
                    "run-1",
                    "dev",
                    "build",
                ),
            ],
            [
                _light("s1", "a", "2026-01-01 00:00:01", "run-1", "dev", "build"),
            ],
        )

        assert hm.get_team_first_question("run-1") == ""
