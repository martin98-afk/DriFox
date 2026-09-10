# -*- coding: utf-8 -*-
"""事件分发四语义测试。"""

import asyncio

from zero import create_context


def test_emit_isolates_listener_exception():
    ctx = create_context()
    calls = []

    def _boom():
        raise RuntimeError("boom")

    ctx.on("tick", _boom)
    ctx.on("tick", lambda: calls.append(1))

    ctx.emit("tick")
    assert calls == [1]  # 坏监听器不影响后续


def test_waterfall_short_circuit():
    ctx = create_context()
    ctx.on("wrap", lambda v, nxt: None)  # 不调 next，短路
    ctx.on("wrap", lambda v, nxt: nxt("downstream"))
    assert ctx.waterfall("wrap", "origin") == "origin"


def test_parallel_runs_concurrently():
    ctx = create_context()
    order = []

    async def _slow():
        await asyncio.sleep(0.02)
        order.append("slow")
        return "slow"

    def _fast():
        order.append("fast")
        return "fast"

    ctx.on("go", _slow)
    ctx.on("go", _fast)

    results = asyncio.run(ctx.parallel("go"))
    assert sorted(results) == ["fast", "slow"]
    assert order == ["fast", "slow"]  # 并发：快的先完成


def test_serial_runs_in_order():
    ctx = create_context()
    order = []

    async def _slow():
        await asyncio.sleep(0.02)
        order.append("slow")
        return "slow"

    def _fast():
        order.append("fast")
        return "fast"

    ctx.on("go", _slow)
    ctx.on("go", _fast)

    results = asyncio.run(ctx.serial("go"))
    assert results == ["slow", "fast"]
    assert order == ["slow", "fast"]  # 串行：按注册顺序等待


def test_parallel_collects_coroutine_and_sync_results():
    ctx = create_context()

    async def _coro():
        return 1

    ctx.on("go", _coro)
    ctx.on("go", lambda: 2)
    assert sorted(asyncio.run(ctx.parallel("go"))) == [1, 2]


def test_listener_count_and_unsubscribe():
    ctx = create_context()
    off = ctx.on("tick", lambda: None)
    assert ctx.scope.pending == 1  # 订阅登记为副作用

    off()
    assert ctx.scope.pending == 1  # 手动退订不清栈（幂等销毁时再退订一次）
    ctx.dispose()
