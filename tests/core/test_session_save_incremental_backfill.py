# -*- coding: utf-8 -*-
"""[E8-机2] 保存链增量回填与会话级布尔态回归。

守卫：
1. provider_name 兜底回填等价：无 provider_name 的旧会话消息仍被回填（精确匹配）
2. 游标增量：已回填消息不重扫；新尾部消息正常回填
3. has_user_message 布尔态：首扫确认置 True 后不再全量扫；切会话（新 session 对象）自然重置
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.main_widget as mw  # noqa: E402
from app.core.conversation.chat_session import ChatSession, SessionManager  # noqa: E402

@pytest.fixture()
def widget():
    """最小 OpenAIChatToolWindow stub（照 test_force_stop_message_persistence 先例）。"""
    w = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    w.session_manager = SessionManager()
    session = ChatSession()
    w.session_manager.sessions.append(session)
    w.session_manager.current_index = 0
    w.history_manager = MagicMock()
    w._session_dirty = True
    w._current_provider_name = "prov-a"
    w._valid_configs = {
        "prov-a": {"display_name": "服务商A", "模型名称": "model-a", "模型列表": ["model-a", "model-b"]},
        "prov-b": {"display_name": "服务商B", "模型名称": "model-x", "模型列表": []},
    }
    w._current_session_id = None
    w._current_project = ""
    w._team_run_id = ""
    w._team_name = ""
    w._team_agent_name = ""
    w._last_known_worktree = ""  # 跳过实时 worktree 探测
    w._resolve_session_project_fallback = lambda *a, **k: ""
    w._notify_history_data_changed = lambda *a, **k: None  # 保存链尾部 UI 通知（裸实例无 Qt）
    return w

def _current_session(widget) -> ChatSession:
    return widget.session_manager.get_current_session()

def test_provider_backfill_for_legacy_message(widget):
    """旧会话消息（有 model_name 缺 provider_name）保存链兜底回填仍生效。"""
    session = _current_session(widget)
    session.messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "model_name": "model-a"},  # 无 provider_name
    ]
    widget._auto_save_current_session()
    assert session.messages[1]["provider_name"] == "服务商A", "缺字段旧消息应被兜底回填"

def test_provider_backfill_multi_match_skipped(widget):
    """精确匹配失败 + 多服务商模糊命中 → 不回填（与改造前「跳过」语义一致）。"""
    # 当前选中 prov-c（模型与消息不同 → 精确匹配不命中）；prov-a/prov-b 都含该模型
    widget._current_provider_name = "prov-c"
    widget._valid_configs["prov-c"] = {"display_name": "服务商C", "模型名称": "model-c", "模型列表": ["model-c"]}
    widget._valid_configs["prov-b"]["模型名称"] = "model-a"
    session = _current_session(widget)
    session.messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "model_name": "model-a"},
    ]
    widget._auto_save_current_session()
    assert "provider_name" not in session.messages[1], "多匹配不明确不得回填"

def test_backfill_cursor_incremental(widget):
    """游标增量：首轮扫过后，二次保存只扫尾部；新尾部消息正常回填。"""
    session = _current_session(widget)
    session.messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "model_name": "model-a"},
    ]
    widget._auto_save_current_session()
    assert session.messages[1]["provider_name"] == "服务商A"
    assert getattr(session, "_provider_backfill_upto", 0) == 2

    # append 新消息（模拟下一轮）→ 二次保存只扫尾部，新消息被回填
    session.messages.append({"role": "assistant", "content": "a2", "model_name": "model-b"})
    widget._session_dirty = True
    widget._auto_save_current_session()
    assert session.messages[2]["provider_name"] == "服务商A", "尾部新消息应回填"
    assert getattr(session, "_provider_backfill_upto", 0) == 3

def test_has_user_message_cached_after_first_scan(widget):
    """布尔态：首扫确认置 True；切会话（新 session 对象）属性不存在 → 自然重置。"""
    session = _current_session(widget)
    session.messages = [{"role": "user", "content": "q"}]
    assert not getattr(session, "_has_user_message_cached", False)
    widget._auto_save_current_session()
    assert getattr(session, "_has_user_message_cached", False) is True, "首扫确认后应置 True"

    other = ChatSession()
    assert not getattr(other, "_has_user_message_cached", False), "切会话新对象应重置为未确认"

def test_save_skips_session_without_user_message(widget):
    """无用户消息会话仍被跳过（守卫行为保持）；扫到 False 不置位（后续出现可放行）。"""
    session = _current_session(widget)
    session.messages = [
        {"role": "user", "content": "hook 注入", "_hook_event": "SessionStart"},
        {"role": "assistant", "content": "自动回复", "model_name": "model-a"},
    ]
    assert widget._auto_save_current_session() is None, "仅 hook 消息的会话应被跳过"
    assert not getattr(session, "_has_user_message_cached", False), "扫到 False 不得置位"

    session.messages.append({"role": "user", "content": "真实问题"})  # 后来出现的用户消息
    widget._session_dirty = True
    result = widget._auto_save_current_session()
    assert result is not None, "后续出现的用户消息应放行保存（行为等价）"
    assert getattr(session, "_has_user_message_cached", False) is True

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
