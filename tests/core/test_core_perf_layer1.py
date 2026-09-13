# -*- coding: utf-8 -*-
"""T6-3 核心层性能包第一层的行为测试。

覆盖：
1. ContextBudgetAllocator._get_cached_system_tokens 的 LRU 语义
   （原 FIFO dict 淘汰 → OrderedDict LRU + md5 键）
2. context_usage.snapshot_usage_for_hooks 的快照签名缓存
   （同签名复用 / 消息追加失效重算）
"""

from hashlib import md5

import app.core.context_usage as cu
from app.core.context_builder import ContextBudgetAllocator


# ============================================================
# 1. _get_cached_system_tokens：LRU + md5 键
# ============================================================


def _make_allocator() -> ContextBudgetAllocator:
    """_get_cached_system_tokens 只依赖缓存字典与 count_messages_tokens，
    构造参数可安全传 None。"""
    return ContextBudgetAllocator(None)


def test_lru_hit_consistent_value():
    """命中一致性：同 prompt 多次查询返回同值，且不重复计算。"""
    alloc = _make_allocator()
    prompt = "system prompt A" * 10
    v1 = alloc._get_cached_system_tokens(prompt)
    v2 = alloc._get_cached_system_tokens(prompt)
    assert v1 == v2
    assert len(alloc._system_tokens_cache) == 1


def test_lru_evicts_least_recently_used_at_65():
    """容量 65 溢出时被逐的是「最久未用」而非「最早写入」——区分 LRU 与旧 FIFO。"""
    alloc = _make_allocator()
    prompts = [f"prompt-{i}" for i in range(65)]

    # 依次写入 64 个（未超限）
    for p in prompts[:64]:
        alloc._get_cached_system_tokens(p)
    assert len(alloc._system_tokens_cache) == 64

    # 访问最老的 prompts[0]（FIFO 下它下一个被逐；LRU 下它被救活）
    alloc._get_cached_system_tokens(prompts[0])

    # 写第 65 个 → 淘汰最久未用者 = prompts[1]（非 prompts[0]）
    alloc._get_cached_system_tokens(prompts[64])
    keys = list(alloc._system_tokens_cache.keys())
    key0 = md5(prompts[0].encode("utf-8")).hexdigest()
    key1 = md5(prompts[1].encode("utf-8")).hexdigest()
    key64 = md5(prompts[64].encode("utf-8")).hexdigest()

    assert len(keys) == 64
    assert key1 not in keys, "被逐的应是最久未用的 prompts[1]"
    assert key0 in keys, "刚访问过的 prompts[0] 不应被逐（LRU 语义）"
    assert key64 in keys


def test_md5_key_stable_across_instances():
    """md5 键跨实例/跨进程稳定：同内容在不同 allocator 实例产生相同缓存键。"""
    a1, a2 = _make_allocator(), _make_allocator()
    prompt = "stable key prompt"
    a1._get_cached_system_tokens(prompt)
    a2._get_cached_system_tokens(prompt)

    expected = md5(prompt.encode("utf-8")).hexdigest()
    assert expected in a1._system_tokens_cache
    assert expected in a2._system_tokens_cache
    assert set(a1._system_tokens_cache.keys()) == set(a2._system_tokens_cache.keys())
    assert a1._system_tokens_cache[expected] == a2._system_tokens_cache[expected]


# ============================================================
# 2. snapshot_usage_for_hooks：快照签名缓存
# ============================================================


class _FakeSession:
    def __init__(self, sid="sess-1"):
        self.session_id = sid
        self.messages = []
        self.last_api_prompt_tokens = 0


class _FakeBackend:
    """get_context_usage_snapshot 计数桩：每次真实调用计一次。"""

    def __init__(self):
        self.snapshot_calls = 0

    def get_context_usage_snapshot(self, session, llm_config, **kwargs):
        self.snapshot_calls += 1
        return {"used_tokens": 100 * self.snapshot_calls, "budget_tokens": 1000}


def _reset_cache():
    cu._snap_cache.clear()


def test_snapshot_cache_same_sig_returns_cached():
    """同签名两次调用：backend 快照只真实计算一次，返回值一致。"""
    _reset_cache()
    backend = _FakeBackend()
    session = _FakeSession()
    session.messages = [{"role": "user", "content": "hi"}]

    r1 = cu.snapshot_usage_for_hooks(backend, session=session)
    r2 = cu.snapshot_usage_for_hooks(backend, session=session)

    assert r1 == r2 == (100, 1000)
    assert backend.snapshot_calls == 1, "同签名第二次调用应命中缓存"


def test_snapshot_cache_invalidated_on_message_append():
    """追加消息后签名变化：缓存失效并重新计算。"""
    _reset_cache()
    backend = _FakeBackend()
    session = _FakeSession()
    session.messages = [{"role": "user", "content": "hi"}]

    cu.snapshot_usage_for_hooks(backend, session=session)
    session.messages.append({"role": "assistant", "content": "hello"})

    r2 = cu.snapshot_usage_for_hooks(backend, session=session)
    assert backend.snapshot_calls == 2, "签名变化后应重算"
    assert r2 == (200, 1000)


def test_snapshot_cache_invalidated_on_api_tokens_change():
    """last_api_prompt_tokens 变化也构成失效源（API 口径修正值）。"""
    _reset_cache()
    backend = _FakeBackend()
    session = _FakeSession()
    session.messages = [{"role": "user", "content": "hi"}]

    cu.snapshot_usage_for_hooks(backend, session=session)
    session.last_api_prompt_tokens = 123

    cu.snapshot_usage_for_hooks(backend, session=session)
    assert backend.snapshot_calls == 2, "API tokens 变化后应重算"


def test_snapshot_cache_no_pollution_on_empty_or_fallback():
    """空消息早退与 legacy 回退路径均不写缓存。"""
    _reset_cache()
    backend = _FakeBackend()
    empty_session = _FakeSession()
    # 空消息：早退，不写缓存
    assert cu.snapshot_usage_for_hooks(backend, session=empty_session) == (0, 0)
    assert cu._snap_cache == {}

    # 快照抛异常 → legacy 回退，不写缓存
    class _BoomBackend:
        def get_context_usage_snapshot(self, *a, **k):
            raise RuntimeError("boom")

    session = _FakeSession()
    session.messages = [{"role": "user", "content": "hi"}]
    cu.snapshot_usage_for_hooks(_BoomBackend(), session=session)
    assert cu._snap_cache == {}
