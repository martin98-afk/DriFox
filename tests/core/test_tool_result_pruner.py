# -*- coding: utf-8 -*-
"""S1 验收：工具结果截断（DSH tool-result-pruner 对齐）

验收标准：
1. prune_tool_result 单元行为：>8192 截断、头 4096 + 尾 1024、省略标记含被裁字符数
2. 集成：build_messages 组装后超长 tool 结果不全量进上下文
3. 量化：同会话 token 用量可测下降（截断前后对比）
4. 边界：不超阈值 / 非字符串 / 空串不受影响
"""
import pytest

from app.core.context_builder import (
    TOOL_RESULT_HEAD_KEEP,
    TOOL_RESULT_MAX_LEN,
    TOOL_RESULT_TAIL_KEEP,
    ContextBudgetAllocator,
    prune_tool_result,
)
from app.core.token_estimator import count_messages_tokens


class TestPruneToolResultUnit:
    def test_short_content_untouched(self):
        s = "x" * 100
        assert prune_tool_result(s) == s

    def test_exact_threshold_untouched(self):
        s = "x" * TOOL_RESULT_MAX_LEN
        assert prune_tool_result(s) == s

    def test_over_threshold_truncated(self):
        s = "A" * 10000
        out = prune_tool_result(s)
        assert len(out) < TOOL_RESULT_MAX_LEN
        assert "已截断中间" in out
        assert "A" * TOOL_RESULT_HEAD_KEEP in out  # 头部保留
        assert out.endswith("A" * TOOL_RESULT_TAIL_KEEP)  # 尾部保留

    def test_ellipsis_reports_dropped_count(self):
        s = "x" * 20000
        out = prune_tool_result(s)
        dropped = 20000 - TOOL_RESULT_HEAD_KEEP - TOOL_RESULT_TAIL_KEEP
        assert f"{dropped} 字符" in out

    def test_head_tail_custom_params(self):
        s = "x" * 1000
        out = prune_tool_result(s, max_len=100, head_keep=40, tail_keep=10)
        assert out.startswith("x" * 40)
        assert out.endswith("x" * 10)
        assert "已截断中间" in out

    def test_non_string_untouched(self):
        assert prune_tool_result(None) is None
        assert prune_tool_result(12345) == 12345
        assert prune_tool_result("") == ""

    def test_head_tail_overlap_fallback(self):
        """head+tail 超过原文长度时保底只留 head"""
        s = "x" * 200
        out = prune_tool_result(s, max_len=10, head_keep=150, tail_keep=100)
        assert len(out) <= 150


# ==================== 集成测试 ====================
class _FakeAgentManager:
    def get_agent_system_prompt(self, *a, **k):
        return "system prompt"


class _FakeCompactor:
    def compact(self, history, budget, **k):
        return history, {"state": "ok"}, {}

    def get_budget(self, llm_config):
        return 200000


class _FakeSession:
    system_prompt = ""
    _system_prompt_agent = None
    compaction_cache = None

    def __init__(self, messages):
        self.messages = messages

    def get_context_messages(self):
        return self.messages

    def set_compaction_state(self, s):
        pass

    def set_compaction_cache(self, c):
        pass


def _allocator():
    return ContextBudgetAllocator(_FakeAgentManager(), compactor=_FakeCompactor())


def _tool_history(long_content: str):
    return [
        {"role": "user", "content": "请执行命令"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "bash",
         "content": long_content, "success": True},
        {"role": "assistant", "content": "完成"},
    ]


class TestBuildMessagesPrune:
    def test_long_tool_result_truncated_in_context(self):
        long_content = "R" * 30000
        session = _FakeSession(_tool_history(long_content))
        messages = _allocator().build_messages(session, {"max_tokens": 8000}, current_agent="build")
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert tool_msgs, "应保留 tool 消息"
        assert len(tool_msgs[0]["content"]) < TOOL_RESULT_MAX_LEN
        assert "已截断中间" in tool_msgs[0]["content"]

    def test_short_tool_result_untouched_in_context(self):
        short_content = "ok"
        history = _tool_history(short_content)
        # 消息条数故意与 long 测试不同，规避 consolidate_messages (id,len) 缓存脏命中
        history.append({"role": "user", "content": "附加消息避免缓存撞键"})
        session = _FakeSession(history)
        messages = _allocator().build_messages(session, {"max_tokens": 8000}, current_agent="build")
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert tool_msgs[0]["content"] == "ok"

    def test_token_usage_drops_quantified(self):
        """量化：同会话组装前后 token 对比，超长 tool 结果触发截断后显著下降"""
        long_content = "T" * 50000
        session = _FakeSession(_tool_history(long_content))
        allocator = _allocator()
        raw_msgs = _tool_history(long_content)
        raw_tokens = count_messages_tokens(raw_msgs)
        pruned_msgs = allocator.build_messages(session, {"max_tokens": 8000}, current_agent="build")
        pruned_tokens = count_messages_tokens(pruned_msgs)
        # 截断后整个上下文 token 应低于原始（含 system prompt 也远小于 50k 字符差异）
        assert pruned_tokens < raw_tokens, f"截断后 token 应下降: raw={raw_tokens}, pruned={pruned_tokens}"
        saved = raw_tokens - pruned_tokens
        assert saved > 0
        # 记录量化数字（供报告）
        assert pruned_tokens < raw_tokens * 0.5, "截断应至少省一半 token"
