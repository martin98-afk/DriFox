# -*- coding: utf-8 -*-
"""S2 验收：每会话 token 投影 = 实际发送（工具结果截断后）

验收标准：
1. snapshot 的 used_tokens 基于截断后的工具结果（与实际 build_messages 发送一致）
2. pruned_tokens 返回节省量（raw - pruned），供 UI 展示
3. 原始 session.messages 不被修改（可追溯）
4. 短结果 / 无会话场景不破坏
"""
import pytest

from app.core.context_builder import TOOL_RESULT_MAX_LEN, prune_tool_result
from app.core.engines.ui.engine import UIEngine
from app.core.token_estimator import per_message_tokens


class _FakeCore:
    class _Compactor:
        def _make_state(self):
            return {}

        def get_budget(self, llm_config):
            return 200000

    class _ContextBuilder:
        def get_context_budget(self, llm_config):
            return 200000

    def __init__(self):
        self.compactor = self._Compactor()
        self.context_builder = self._ContextBuilder()
        self.permission_cache = None


class _FakeSession:
    def __init__(self, messages, system_prompt=""):
        self.messages = messages
        self.system_prompt = system_prompt
        self.session_id = "s2-test"


def _engine(session, model="gpt-4o"):
    engine = UIEngine.__new__(UIEngine)
    engine._conversation_core = _FakeCore()
    engine._session_manager = type("SM", (), {"get_current_session": lambda s: session})()
    engine._get_model_config = lambda: {"model": model, "max_tokens": 8000}
    engine._current_agent = "build"
    engine._get_agent_manager = lambda: _FakeAgentMgr()
    engine._tool_executor = type("TE", (), {"_builtin_tools": None})()
    engine._backend = None
    engine._callbacks = {}
    engine._api_mode = False
    engine._worker_callbacks = {}
    engine._tools_schema_cache = {"timestamp": 0.0, "tools": [], "tokens": 0}
    return engine


class _FakeAgentMgr:
    def get_agent_tools_schema(self, *a, **k):
        return []


def _session_with_long_tool(long_chars=30000):
    return _FakeSession([
        {"role": "user", "content": "执行命令"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "bash", "content": "T" * long_chars, "success": True},
        {"role": "assistant", "content": "完成"},
    ])


class TestSnapshotPruneProjection:
    def test_snapshot_uses_pruned_tokens(self):
        """投影：used_tokens 应基于截断后的工具结果（≈实际发送）"""
        session = _session_with_long_tool()
        engine = _engine(session)
        snap = engine.get_context_usage_snapshot(session=session, llm_config={"model": "gpt-4o", "max_tokens": 8000})
        # 计算「截断后」的期望：原始 messages 全部 pruned 后算 token
        import copy

        expected_msgs = copy.deepcopy(session.messages)
        for m in expected_msgs:
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                m["content"] = prune_tool_result(m["content"])
        expected_tool = sum(
            per_message_tokens(m, "gpt-4o")
            for m in expected_msgs
            if m.get("role") == "tool"
        )
        # 快照里的 tool 占比应等于截断后的期望值（而非原始 30k 字符）
        tool_entry = [b for b in snap["breakdown"] if b["key"] == "tool"]
        assert tool_entry, "应有工具结果占比"
        # 容差：per_message_tokens 对同一消息应一致（用 < 原始值的严格断言）
        raw_tool = sum(
            per_message_tokens(m, "gpt-4o")
            for m in session.messages
            if m.get("role") == "tool"
        )
        assert tool_entry[0]["tokens"] < raw_tool, "投影后工具结果 token 应小于原始"
        assert tool_entry[0]["tokens"] == expected_tool, "投影应等于截断后实际发送值"

    def test_pruned_tokens_reported(self):
        """pruned_tokens：返回节省量（>0）"""
        session = _session_with_long_tool(50000)
        engine = _engine(session)
        snap = engine.get_context_usage_snapshot(session=session, llm_config={"model": "gpt-4o", "max_tokens": 8000})
        assert snap["pruned_tokens"] > 0, "长工具结果应报告节省量"
        # 与 unit 量化口径一致：原始 tool token - 截断后 tool token
        raw_tool = sum(per_message_tokens(m, "gpt-4o") for m in session.messages if m.get("role") == "tool")
        assert snap["pruned_tokens"] < raw_tool

    def test_session_messages_not_mutated(self):
        """原始存储不被修改（可追溯）"""
        session = _session_with_long_tool(30000)
        original_content = session.messages[2]["content"]
        engine = _engine(session)
        engine.get_context_usage_snapshot(session=session, llm_config={"model": "gpt-4o", "max_tokens": 8000})
        assert session.messages[2]["content"] == original_content, "session.messages 不应被截断修改"

    def test_short_tool_result_no_prune(self):
        """短工具结果：pruned_tokens=0，投影=原始"""
        session = _FakeSession([
            {"role": "tool", "tool_call_id": "c1", "name": "bash", "content": "ok", "success": True},
        ])
        engine = _engine(session)
        snap = engine.get_context_usage_snapshot(session=session, llm_config={"model": "gpt-4o", "max_tokens": 8000})
        assert snap["pruned_tokens"] == 0
        tool_entry = [b for b in snap["breakdown"] if b["key"] == "tool"]
        assert tool_entry[0]["tokens"] == per_message_tokens(session.messages[0], "gpt-4o")

    def test_empty_session_safe(self):
        """无会话：安全返回且含 pruned_tokens 键"""
        engine = _engine(None)
        snap = engine.get_context_usage_snapshot(session=None, llm_config=None)
        assert snap["pruned_tokens"] == 0
        assert snap["used_tokens"] == 0
