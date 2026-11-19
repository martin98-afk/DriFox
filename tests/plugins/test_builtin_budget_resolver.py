# -*- coding: utf-8 -*-
"""卡G 等价性回归：BuiltInBudgetResolver._cached_system_tokens 前置身份短路。

效果口径：
- 同一 str 对象重复查询：短路命中，不再付 encode+md5 与查表，返回值一致
- 不同对象同内容：md5 指纹缓存命中，返回值一致（与无短路时同口径）
- 内容变化：哨兵与指纹缓存双 miss，重算并返回新值
- 64 条 LRU 淘汰行为不受短路影响
"""

import sys
from hashlib import md5
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from importlib import import_module  # noqa: E402

_builtin = import_module("plugins.system-context.budget_resolvers.builtin")
BuiltInBudgetResolver = _builtin.BuiltInBudgetResolver


@pytest.fixture()
def counter(monkeypatch):
    """替换 builtin 模块命名空间的 count_messages_tokens：按内容长度计值并计数。"""

    calls = {"n": 0}

    def fake_count(messages):
        calls["n"] += 1
        return len(messages[0]["content"]) // 4

    monkeypatch.setattr(_builtin, "count_messages_tokens", fake_count)
    return calls


def test_identity_shortcut_hit_skips_recompute(counter):
    """同一 str 对象二次查询：短路命中，真实计算只发生一次。"""
    r = BuiltInBudgetResolver()
    prompt = "system prompt " * 100
    v1 = r._cached_system_tokens(prompt)
    assert counter["n"] == 1
    v2 = r._cached_system_tokens(prompt)
    assert v1 == v2
    assert counter["n"] == 1, "身份短路命中不应再触发真实计算"
    assert len(r._system_tokens_cache) == 1


def test_same_content_new_object_hits_md5_cache(counter):
    """不同对象、相同内容：md5 指纹缓存命中，值一致且不重算。"""
    r = BuiltInBudgetResolver()
    p1 = "stable content " * 50
    p2 = p1.encode("utf-8").decode("utf-8")  # 等值但非同一对象（运行时构造）
    assert p1 == p2 and p1 is not p2
    v1 = r._cached_system_tokens(p1)
    v2 = r._cached_system_tokens(p2)
    assert v1 == v2
    assert counter["n"] == 1, "同内容不同对象应命中 md5 缓存而非重算"


def test_changed_content_recomputes(counter):
    """内容变化：哨兵与指纹缓存双 miss，重算并返回新值。"""
    r = BuiltInBudgetResolver()
    v1 = r._cached_system_tokens("short")
    v2 = r._cached_system_tokens("a much longer system prompt " * 20)
    assert counter["n"] == 2
    assert v1 != v2


def test_exception_fallback_zero_cached(counter, monkeypatch):
    """count_messages_tokens 抛异常 → 记 0 且短路缓存同样记 0（与原实现一致）。"""

    def boom(messages):
        raise RuntimeError("boom")

    monkeypatch.setattr(_builtin, "count_messages_tokens", boom)
    r = BuiltInBudgetResolver()
    prompt = "will fail"
    assert r._cached_system_tokens(prompt) == 0
    assert r._cached_system_tokens(prompt) == 0  # 短路命中仍返回 0


def test_lru_eviction_intact(counter):
    """短路不改变 LRU 淘汰行为：65 条逐出最久未用，容量恒 64。"""
    r = BuiltInBudgetResolver()
    prompts = [f"prompt-{i}" for i in range(65)]
    for p in prompts:
        r._cached_system_tokens(p)
    assert len(r._system_tokens_cache) == 64
    assert counter["n"] == 65
    assert md5(prompts[0].encode("utf-8")).hexdigest() not in r._system_tokens_cache
    assert md5(prompts[64].encode("utf-8")).hexdigest() in r._system_tokens_cache
