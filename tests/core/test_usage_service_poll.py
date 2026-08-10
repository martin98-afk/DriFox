# -*- coding: utf-8 -*-
"""
UsageService 轮询回归测试

Bug 背景：_poll_timer 是 singleShot QTimer，request_coding_plan 只有
「发起后台抓取」路径重启 timer；缓存命中 / in_flight / 无 fetcher 路径
直接 return，导致 tick 触发后 timer 永久死亡 → 用量圆环只有新建标签页
（重新走刷新路径且缓存已过期）时才刷新一次，之后再也不变。

修复：缓存命中 / in_flight 路径保持 timer 存活，_on_poll_tick 尾部兜底
重启。本文件用假 fetcher 验证三种场景下 timer 都能续轮。
"""

import threading
import time

import pytest
from PyQt5.QtCore import QTimer

from app.core.usage_service import UsageService

PROVIDER = "test-provider"
CONFIG_ID = "cfg-1"
CONFIG = {"API_KEY": "test-key"}

EMPTY_PLAN = {"rolling": {"percent": 10, "reset_sec": 1000}}


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个测试独立单例，避免轮询 timer / 缓存串扰"""
    UsageService._instance = None
    yield
    UsageService._instance = None


def _make_svc(monkeypatch, fetcher):
    """构造单例并注入假 fetcher"""
    monkeypatch.setattr(UsageService, "_resolve_fetcher", staticmethod(lambda name: fetcher))
    return UsageService.get_instance()


def _wait(fn, timeout=3.0):
    """轮询等待条件成立（后台线程写缓存/注册 active key）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.01)
    raise AssertionError("等待超时")


def test_poll_timer_survives_cache_hit(monkeypatch):
    """核心回归：tick 时缓存未过期（命中分支）→ timer 必须续轮"""
    svc = _make_svc(monkeypatch, lambda config: dict(EMPTY_PLAN))

    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    _wait(lambda: (PROVIDER, CONFIG_ID) in svc._active_plan_keys)
    assert svc._poll_timer is not None
    assert svc._poll_timer.isActive()

    # 模拟 singleShot 触发完毕（触发后即 inactive）
    svc._poll_timer.stop()
    assert not svc._poll_timer.isActive()

    # tick：抓取刚完成，缓存 age < TTL → 走缓存命中分支
    svc._on_poll_tick()

    # 修复后：timer 存活，下一轮 60s 后继续
    assert svc._poll_timer.isActive(), "缓存命中后轮询 timer 必须续轮（回归）"


def test_new_request_cache_hit_recovers_poll_after_unregister(monkeypatch):
    """窗口关闭(unregister)后新窗口命中缓存 → 恢复 active 轮询"""
    svc = _make_svc(monkeypatch, lambda config: dict(EMPTY_PLAN))
    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    _wait(lambda: (PROVIDER, CONFIG_ID) in svc._active_plan_keys)

    # 窗口关闭：active key 被清理，缓存保留
    svc.unregister(CONFIG_ID)
    assert (PROVIDER, CONFIG_ID) not in svc._active_plan_keys
    # timer 停掉模拟已死亡
    svc._poll_timer.stop()

    # 新窗口/新标签页请求：缓存命中
    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)

    assert (PROVIDER, CONFIG_ID) in svc._active_plan_keys, "缓存命中应恢复 active 注册"
    assert svc._poll_timer.isActive(), "缓存命中后轮询 timer 必须重启（回归）"


def test_inflight_keeps_timer_alive(monkeypatch):
    """并发去重路径（in_flight）不杀死轮询 timer"""
    gate = threading.Event()
    calls = []

    def slow_fetcher(config):
        calls.append(1)
        gate.wait(timeout=2.0)  # 挂起，制造 in_flight 窗口
        return dict(EMPTY_PLAN)

    svc = _make_svc(monkeypatch, slow_fetcher)

    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    _wait(lambda: (PROVIDER, CONFIG_ID) in svc._in_flight)

    # 第二次请求：命中 in_flight 分支
    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    assert len(calls) == 1, "in_flight 去重后不应重复发起抓取"
    assert svc._poll_timer.isActive(), "in_flight 去重路径也必须保持轮询 timer 存活"

    gate.set()


def test_poll_tick_tail_backstop(monkeypatch):
    """兜底：tick 尾部只要有 active key 就一定续轮（即使 tick 内全 return）"""
    svc = _make_svc(monkeypatch, lambda config: dict(EMPTY_PLAN))
    svc.request_coding_plan(PROVIDER, CONFIG_ID, CONFIG)
    _wait(lambda: (PROVIDER, CONFIG_ID) in svc._active_plan_keys)

    QTimer.singleShot(0, lambda: None)  # 确保 timer 对象就绪
    svc._poll_timer.stop()
    svc._on_poll_tick()
    assert svc._poll_timer.isActive()
