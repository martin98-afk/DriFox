# -*- coding: utf-8 -*-
"""发送前处理缓存契约测试（PERF T33）

背景：ContextBudgetAllocator.build_messages 的发送前处理
（consolidate_messages + HistoryCompactor.compact 内含 token 估算）在长会话
（数百条消息）下是 100-300ms 级开销，且一次发送会经多次调用
（主发送 + 工具迭代回环）。T33 引入 session 级 prep 缓存：
prep_key 8 元组全等时直接复用 history/state/cache，两步全归零。

本测试锁定其失效契约——任何会让结果变化的输入都必须 miss：
- 消息追加 / set_messages（含截断变短）/ hook 注入后 miss
- budget / model / 截断阈值 / allow_llm_summary 变化后 miss
- 同长度同首尾时间戳但内容指纹不同 → 靠 version 兜住 miss
- 线程读写 smoke

运行: python -m pytest tests/core/test_send_prep_cache.py -v
"""
import os
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.conversation.chat_session import ChatSession  # noqa: E402
from app.core.context.builder import ContextBudgetAllocator  # noqa: E402


class _CountingCompactor:
    """compact / get_budget 调用计数（验证缓存命中时零调用）"""

    def __init__(self, budget=200000):
        self.compact_calls = 0
        self.budget_calls = 0
        self._budget = budget

    def compact(self, history, budget, existing_cache=None, allow_llm_summary=True, prenormalized=None):
        self.compact_calls += 1
        # 模拟真实 compact 语义：传了 prenormalized 就不再自己 consolidate
        normalized = prenormalized if prenormalized is not None else history
        return list(normalized), {"calls": self.compact_calls}, {}

    def get_budget(self, llm_config):
        self.budget_calls += 1
        return self._budget


class _FakeAgentManager:
    def get_agent_system_prompt(self, *a, **k):
        return "system prompt"


def _session_with(messages):
    """构造带版本字段的真实 ChatSession（T33 缓存挂在它身上）"""
    s = ChatSession(messages=messages)
    s.system_prompt = "cached system"
    return s


def _history(n=4):
    msgs = [{"role": "user", "content": "问题 1", "timestamp": "2026-09-16 10:00:00"}]
    for i in range(1, n):
        msgs.append({"role": "assistant", "content": f"回答 {i}", "timestamp": f"2026-09-16 10:0{i}:00"})
        msgs.append({"role": "user", "content": f"问题 {i + 1}", "timestamp": f"2026-09-16 10:0{i}:30"})
    return msgs


def _allocator(compactor):
    """注入 compactor 与包它的 pipeline 桩。

    压缩现在由 tier 链上的 tail_retain/llm_summary 执行（用户插件 context-compaction）。
    为让本测试继续锁定「_send_prep_cache 缓存失效契约」，把假 compactor 包成
    单 tier 注入 pipeline —— compact_calls 计数语义保持不变。
    """
    from app.core.context.pipeline import ContextPipeline
    from app.plugins.contracts.context_policy import CACHE_INVALIDATE, STAGE_SEND, TierOutcome
    from app.plugins.registries.context_policy_registry import ContextPolicyRegistry

    class _CompactorTier:
        id = "compactor_tier"
        label = "压缩（测试桩）"
        order = 70
        stages = frozenset({STAGE_SEND})
        cache_impact = CACHE_INVALIDATE

        def should_apply(self, view):
            return True

        def apply(self, view):
            messages, state, cache = compactor.compact(
                view.messages,
                view.budget,
                existing_cache=view.compaction_cache or None,
                allow_llm_summary=True,
                prenormalized=view.messages,
            )
            view.compaction_state = state
            view.compaction_cache = cache
            return TierOutcome(messages=messages, saved_tokens=0, note="compact")

    reg = ContextPolicyRegistry()
    reg.register_tier(_CompactorTier(), "test")
    return ContextBudgetAllocator(_FakeAgentManager(), compactor=compactor, pipeline=ContextPipeline(reg))


class TestCacheHit:
    """同输入重复调用必须命中（compact 零调用）"""

    def test_second_call_hits_cache(self):
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())

        alloc.build_messages(session, {"model": "m1", "max_tokens": 8000}, current_agent="build")
        assert compactor.compact_calls == 1, "首次应走 compact"
        alloc.build_messages(session, {"model": "m1", "max_tokens": 8000}, current_agent="build")
        assert compactor.compact_calls == 1, "同输入第二次应命中缓存（compact 不得再调用）"

    def test_cache_stores_history_and_state(self):
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        first = alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        cached = session._send_prep_cache
        assert cached is not None and "key" in cached
        second = alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert [m.get("content") for m in first] == [m.get("content") for m in second]


class TestInvalidationOnMessageChange:
    """消息内容变化必须 miss"""

    def test_append_assistant_message_misses(self):
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 1

        session.add_assistant_message("新回答")
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 2, "add_assistant_message 后应 miss"

    def test_append_user_message_misses(self):
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")

        session.add_user_message("新问题")
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 2, "add_user_message 后应 miss"

    def test_set_messages_truncation_misses(self):
        """set_messages 变短（截断/回退）后必须 miss"""
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history(5))
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 1

        session.set_messages(_history(2))
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 2, "set_messages 截断后应 miss"

    def test_direct_messages_assignment_misses(self):
        """property setter 直写路径（main_widget 分支会话创建）也必须 miss"""
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")

        session.messages = _history(3)
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 2, "messages setter 直写后应 miss"

    def test_clear_misses(self):
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")

        session.clear()
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 2, "clear 后应 miss"


class TestInvalidationOnHookInjection:
    """hook 注入（backend._inject_hook_to_session）必须 miss"""

    def test_inject_hook_to_session_misses(self):
        from app.core.conversation.backend import _inject_hook_to_session

        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 1

        _inject_hook_to_session(session, "SessionStart", "hook 输出内容")
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 2, "hook 注入后应 miss（唯一绕过写方法的追加点）"


class TestInvalidationOnParamsChange:
    """budget / model / 阈值 / summary 开关变化必须 miss"""

    def test_budget_change_misses(self):
        compactor = _CountingCompactor(budget=200000)
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")

        compactor._budget = 50000  # 预算变化 → budget 入 key → miss
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 2, "budget 变化后应 miss"

    def test_model_change_misses(self):
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")

        alloc.build_messages(session, {"model": "m2"}, current_agent="build")
        assert compactor.compact_calls == 2, "model 变化后应 miss"

    def test_tool_result_max_len_change_misses(self):
        """截断阈值（随模型上下文容量缩放）变化 → key 变化 → miss"""
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1", "上下文长度": 32000}, current_agent="build")

        alloc.build_messages(session, {"model": "m1", "上下文长度": 128000}, current_agent="build")
        assert compactor.compact_calls == 2, "工具截断阈值变化后应 miss"

    def test_allow_llm_summary_change_misses(self):
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build", allow_llm_summary=False)

        alloc.build_messages(session, {"model": "m1"}, current_agent="build", allow_llm_summary=True)
        assert compactor.compact_calls == 2, "allow_llm_summary 变化后应 miss"


class TestFingerprintSafety:
    """同长度同首尾时间戳但内容不同 → 靠 version 兜住"""

    def test_same_len_same_ts_different_content_misses(self):
        """构造 len / first_ts / last_ts 全等、仅中间内容不同的两组消息。

        key 的 5 个内容位全部相同，只有 version 不同 → 必须 miss
        （这正是 key 里放 version 的意义：长度+首尾指纹不足以判定内容等价）。
        """
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)

        def build(inner):
            msgs = [
                {"role": "user", "content": "首", "timestamp": "2026-09-16 10:00:00"},
                {"role": "assistant", "content": inner, "timestamp": "2026-09-16 10:01:00"},
                {"role": "user", "content": "尾", "timestamp": "2026-09-16 10:02:00"},
            ]
            return msgs

        session = _session_with(build("内容 A"))
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 1

        # 同长度 / 同首尾 ts / 中间内容不同（写入走 set_messages → version +1）
        session.set_messages(build("内容 B"))
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 2, "同 len 同 ts 不同内容必须靠 version 兜住 miss"

    def test_manual_bump_misses(self):
        """外部直接改消息对象（不经过写方法）时可手动 bump 失效"""
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")

        session.messages[1]["content"] = "被外部改写的回答"
        session.bump_messages_version()
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert compactor.compact_calls == 2, "手动 bump 后应 miss"

    def test_prenormalized_passed_to_compactor(self):
        """miss 路径必须把已 consolidate 的 history 作为 prenormalized 传给 compact"""
        seen = {}

        class _SpyCompactor(_CountingCompactor):
            def compact(self, history, budget, existing_cache=None, allow_llm_summary=True, prenormalized=None):
                seen["prenormalized_is_history"] = prenormalized is history
                return super().compact(history, budget, existing_cache, allow_llm_summary, prenormalized)

        alloc = _allocator(_SpyCompactor())
        session = _session_with(_history())
        alloc.build_messages(session, {"model": "m1"}, current_agent="build")
        assert seen.get("prenormalized_is_history") is True, "compact 应收到 prenormalized（跳过重复 consolidate）"


class TestThreadSmoke:
    """多线程读写 smoke：不得抛异常，缓存不串味"""

    def test_concurrent_build_no_exception(self):
        compactor = _CountingCompactor()
        alloc = _allocator(compactor)
        session = _session_with(_history())
        errors = []

        def worker():
            try:
                for _ in range(10):
                    alloc.build_messages(session, {"model": "m1"}, current_agent="build")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        assert not errors, f"并发 build_messages 异常: {errors}"

    def test_version_monotonic_on_write_paths(self):
        """四条写路径各自 +1（version 单调，不因缓存与否而变化）"""
        session = ChatSession(messages=[{"role": "user", "content": "x", "timestamp": "t"}])
        v0 = session._messages_version

        session.add_assistant_message("a")
        assert session._messages_version == v0 + 1
        session.add_user_message("u")
        assert session._messages_version == v0 + 2
        session.set_messages([{"role": "user", "content": "y", "timestamp": "t"}])
        assert session._messages_version == v0 + 3
        session.clear()
        assert session._messages_version == v0 + 4


@pytest.mark.parametrize("field", ["history", "state", "cache", "key"])
def test_cache_payload_shape(field):
    """缓存载荷结构稳定（下游按 key 读取）"""
    compactor = _CountingCompactor()
    alloc = _allocator(compactor)
    session = _session_with(_history())
    alloc.build_messages(session, {"model": "m1"}, current_agent="build")
    assert field in session._send_prep_cache
