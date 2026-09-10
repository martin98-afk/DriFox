# -*- coding: utf-8 -*-
"""zero-bridge 桥接核心测试（宿主无关，用假事件源）。

覆盖：状态喂入（弱引用）、事件转发、退订、窗口域、幂等启动。
DriFox 特定胶水（ui/__init__.py）需实机验证，不在本文件范围。
"""

import gc
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins" / "zero-bridge" / "ui"))

from bridge import ZeroBridge  # noqa: E402


class _Obj:
    """可弱引用对象。"""


class FakeBus:
    """满足事件源契约的假总线。"""

    def __init__(self, events):
        self.events_list = list(events)
        self._subs = {}

    def events(self):
        return list(self.events_list)

    def subscribe(self, name, cb):
        self._subs.setdefault(name, []).append(cb)

        def _unsub():
            self._subs[name] = [c for c in self._subs[name] if c is not cb]

        return _unsub

    def publish(self, name, payload):
        for cb in list(self._subs.get(name, [])):
            cb(payload)

    def sub_count(self, name):
        return len(self._subs.get(name, []))


@pytest.fixture
def bridge():
    b = ZeroBridge("testhost")
    yield b
    b.stop()


def test_start_feeds_state_snapshot(bridge):
    marker = _Obj()
    bridge.start(state_provider=lambda: {"widget": marker, "version": "1.0"})

    assert bridge.root.get("host.widget") is marker  # 弱引用解引用后即原对象
    assert bridge.root.get("host.version") == "1.0"


def test_weak_state_expires_with_host_object(bridge):
    obj = _Obj()
    bridge.start(state_provider=lambda: {"widget": obj})

    del obj
    gc.collect()
    assert bridge.root.get("host.widget") is None  # 宿主对象回收 → 条目失效


def test_events_forwarded_into_zero(bridge):
    bus = FakeBus(["theme_changed", "tab_switched"])
    seen = []
    bridge.start(event_source=bus)
    bridge.root.on("host.theme_changed", lambda payload: seen.append(payload))

    bus.publish("theme_changed", {"is_dark": True})
    bus.publish("tab_switched", {"tab_index": 1})  # 未订阅的事件不影响

    assert seen == [{"is_dark": True}]


def test_stop_unsubscribes_from_host_bus(bridge):
    bus = FakeBus(["ping"])
    seen = []
    bridge.start(event_source=bus)
    bridge.root.on("host.ping", lambda payload: seen.append(payload))

    bridge.stop()
    bus.publish("ping", {})

    assert seen == []  # 停桥后事件不再流入
    assert bus.sub_count("ping") == 0


def test_window_contexts_are_isolated(bridge):
    bridge.start()
    w1 = bridge.window("w1")
    w2 = bridge.window("w2")
    assert w1 is not w2
    assert w1.get("session.active") is None

    w1.set("session.active", "chat-a")
    assert w1.get("session.active") == "chat-a"
    assert w2.get("session.active") is None  # 窗口域互不可见

    bridge.close_window("w1")
    assert bridge.window_ids() == ["w2"]


def test_start_is_idempotent(bridge):
    bus = FakeBus(["ping"])
    bridge.start(event_source=bus)
    bridge.start(event_source=bus)  # 二次启动不重复订阅

    assert bus.sub_count("ping") == 1


def test_end_to_end_zero_plugin_receives_host_events(bridge):
    """端到端：zero 插件挂上桥，收到宿主事件。"""
    bus = FakeBus(["theme_changed"])

    def _plugin(ctx):
        ctx.on("host.theme_changed", lambda payload: ctx.set("last_theme", payload))

    bridge.start(event_source=bus)
    fork = bridge.root.use(_plugin)

    bus.publish("theme_changed", {"theme_id": "dark"})
    assert fork.get("last_theme") == {"theme_id": "dark"}
