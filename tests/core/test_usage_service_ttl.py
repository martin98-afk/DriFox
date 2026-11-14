# -*- coding: utf-8 -*-
"""UsageService 惰性过期（A2）预写测试 — TDD 红绿分离。

裁决：缓存 TTL 60s → 300s（5 分钟），过期语义为「读取时惰性判过期」，
unregister 不主动清缓存条目（供其它窗口复用），死 config（快照已删）
不触发后台抓取。

红/绿分类（修复前基线，2026-11-14 实测）：
- 红（功能缺失，修复后转绿）：test_ttl_constants_and_boundary_hit
- 绿（语义保全，修复前后都必须过）：
  test_expired_refetches_at_boundary / test_unregister_lazy_keeps_cache_other_window_hits
  / test_dead_config_zero_fetch_after_unregister

时间操控手法：直接回拨 _plan_cache 内的 monotonic 时间戳（不真等 300s），
与既有测试 stop timer / 直改内部态的手法同风格。
"""

import threading
import time

import pytest

from app.core.infra.usage_service import UsageService

PROVIDER = "test-provider"
CONFIG_ID = "cfg-1"
CONFIG = {"API_KEY": "test-key"}

PLAN = {"rolling": {"percent": 10, "reset_sec": 1000}}


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个测试独立单例，避免轮询 timer / 缓存串扰（对齐既有手法）"""
    UsageService._instance = None
    yield
    UsageService._instance = None


def _make_svc(monkeypatch, fetcher):
    monkeypatch.setattr(UsageService, "_resolve_fetcher", staticmethod(lambda name: fetcher))
    return UsageService.get_instance()


def _wait(fn, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.01)
    raise AssertionError("等待超时")


def test_ttl_constants_and_boundary_hit(monkeypatch):
    """A2-1【红】TTL 常量=300s；age=299s（<TTL）的缓存必须命中不重拉。

    修复前：PLAN_TTL_S/BALANCE_TTL_S=60 → age=299s 判过期重拉 → 双断言皆红。
    """
    assert UsageService.PLAN_TTL_S == 300.0, f"PLAN_TTL_S 应为 300（5 分钟），实际 {UsageService.PLAN_TTL_S}"
    assert UsageService.BALANCE_TTL_S == 300.0, f"BALANCE_TTL_S 应为 300，实际 {UsageService.BALANCE_TTL_S}"

    calls = []

    def counting_fetcher(config):
        calls.append(1)
        return dict(PLAN)

    svc = _make_svc(monkeypatch, counting_fetcher)

    # 首次请求填充缓存，然后把时间戳回拨到 age=299s（< 300s）
    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    _wait(lambda: len(calls) == 1)
    key = (PROVIDER, CONFIG_ID)
    assert key in svc._plan_cache
    svc._plan_cache[key] = (time.monotonic() - 299.0, svc._plan_cache[key][1])

    # 非 force 请求：惰性判过期 → age < TTL 命中，不重拉
    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    time.sleep(0.1)
    assert len(calls) == 1, f"age=299s < TTL=300s 应命中缓存不重拉，实际 fetch 次数={len(calls)}"


def test_expired_refetches_at_boundary(monkeypatch):
    """A2-2【绿】age=301s（>=TTL）缓存过期，非 force 请求必须重拉（语义保全）。"""
    calls = []

    def counting_fetcher(config):
        calls.append(1)
        return dict(PLAN)

    svc = _make_svc(monkeypatch, counting_fetcher)

    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    _wait(lambda: len(calls) == 1)
    key = (PROVIDER, CONFIG_ID)
    svc._plan_cache[key] = (time.monotonic() - 301.0, svc._plan_cache[key][1])

    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    _wait(lambda: len(calls) == 2)
    assert len(calls) == 2, "age=301s >= TTL 必须重拉（惰性过期边界另一侧）"


def test_unregister_lazy_keeps_cache_other_window_hits(monkeypatch):
    """A2-3【绿】unregister 惰性不清：缓存条目保留；他窗（同 config_id）请求命中缓存不重拉。

    对齐既有 unregister 语义：清 active key + config 快照，缓存供复用。
    断言用内部态（对齐既有测试风格；跨线程 emit 走 queued 投递，测试主线程
    无事件循环，信号 spy 不可靠）。
    """
    calls = []

    def counting_fetcher(config):
        calls.append(1)
        return dict(PLAN)

    svc = _make_svc(monkeypatch, counting_fetcher)

    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    _wait(lambda: len(calls) == 1)

    key = (PROVIDER, CONFIG_ID)
    assert key in svc._plan_cache
    cached_value = svc._plan_cache[key][1]

    svc.unregister(CONFIG_ID)
    # 惰性语义：unregister 不清缓存条目（其它窗口复用）
    assert key in svc._plan_cache, "unregister 必须保留缓存条目（惰性过期，不主动清）"
    assert key not in svc._active_plan_keys
    assert CONFIG_ID not in svc._configs

    # 他窗（新标签页）同 config 请求：命中缓存 → 不重拉
    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    time.sleep(0.1)
    assert len(calls) == 1, "他窗请求应命中缓存不重拉"
    assert svc._plan_cache[key][1] is cached_value, "命中分支不得改写缓存值"
    assert key in svc._active_plan_keys, "缓存命中应恢复 active 注册（对齐既有轮询语义）"


def test_dead_config_zero_fetch_after_unregister(monkeypatch):
    """A2-4【绿】死 config 零 fetch：unregister 后 tick 不得对该 config 发起抓取。

    两个分支：①active key 已清（常规路径）→ tick 无 key 可遍历；
    ②残留 key（异常场景）→ config 快照缺失分支 discard 且不 fetch。
    """
    calls = []

    def counting_fetcher(config):
        calls.append(1)
        return dict(PLAN)

    svc = _make_svc(monkeypatch, counting_fetcher)
    key = (PROVIDER, CONFIG_ID)

    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    _wait(lambda: len(calls) == 1)

    # 分支①：常规 unregister → active 清空 → tick 零遍历
    svc.unregister(CONFIG_ID)
    svc._poll_timer.stop()
    svc._on_poll_tick()
    time.sleep(0.1)
    assert len(calls) == 1, "active 清空后 tick 不得发起任何抓取"

    # 分支②：残留 active key（config 快照已删）→ tick 应 discard 且不 fetch
    svc._active_plan_keys.add(key)
    svc._on_poll_tick()
    time.sleep(0.1)
    assert key not in svc._active_plan_keys, "config 缺失的残留 key 必须被 tick 清理"
    assert len(calls) == 1, "死 config（快照已删）不得触发后台抓取"
