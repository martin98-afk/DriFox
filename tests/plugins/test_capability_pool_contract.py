# -*- coding: utf-8 -*-
"""能力池取值契约：pool[cap] 必须逐字等于 provider(host) 的返回值。

背景（#23）：team.spawn 的 provider 返回 dict（一段式），调用方误按
facade 两段式再调一层 → 'dict' object is not callable。本测试把 pool
取值契约双向锁死：
- 一段式 provider（返回终值对象）→ pool["a"] is marker
- 两段式 provider（返回闭包）→ callable(pool["b"]) 且 pool["b"]() is marker
任何「pool 改成返回 provider 本体」或「调用侧多调一层（配合 value_style
AST 检查）」都会在此炸出。
"""

from types import SimpleNamespace

from app.plugins.capabilities.pool import CapabilityPool


def _make_pool(providers):
    return CapabilityPool(
        plugin="test_pool",
        surface="tool",
        host=SimpleNamespace(),
        declared=None,  # None = 兼容期全量放行
        providers=providers,
        ungated=set(),
    )


def test_pool_value_is_provider_return_value():
    """一段式：pool 值就是 provider 返回的对象本体（is 身份断言）。"""
    marker = {"k": "v"}

    def p_value(host):
        return marker

    pool = _make_pool({"a": p_value})
    assert pool["a"] is marker


def test_pool_factory_is_callable_closure_returning_marker():
    """两段式：pool 值是闭包，调用后拿到 marker（is 身份断言）。"""
    marker = {"k2": "v2"}

    def p_factory(host):
        return lambda: marker

    pool = _make_pool({"b": p_factory})
    value = pool["b"]
    assert callable(value)
    assert value() is marker


def test_pool_caches_provider_result():
    """惰性求值且缓存：两次取值同一对象（provider 只跑一次）。"""
    marker = object()

    calls = []

    def p_once(host):
        calls.append(1)
        return marker

    pool = _make_pool({"c": p_once})
    assert pool["c"] is marker
    assert pool["c"] is marker
    assert len(calls) == 1
