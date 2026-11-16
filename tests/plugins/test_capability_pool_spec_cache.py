# -*- coding: utf-8 -*-
"""S3：host 无关池规格缓存（_pool_specs）行为护栏。

钉死三个不变量：
- spec 复用（同 plugin/surface/declared 只预算一次）但池实例随 host 现建重绑；
- declared 三态（缺省解析 / None 兼容全量 / 显式集合）在缓存键中互相独立；
- 注册面变化（版本戳递增）与精细失效（invalidate_declared）按行为生效。
断言写行为/计数（_select 调用数、池身份、names()），不写容器内部结构。
"""
from types import SimpleNamespace

import pytest


@pytest.fixture()
def reg():
    from app.plugins.capabilities.registry import CapabilityRegistry

    r = CapabilityRegistry()
    yield r
    r.clear()


def _spy_select(reg, monkeypatch) -> list:
    """给 reg._select 套计数壳，返回调用记录列表（spec 未命中才调用）。"""
    calls: list = []
    real = reg._select

    def _spy(surface, declared):
        calls.append((surface, None if declared is None else tuple(sorted(declared))))
        return real(surface, declared)

    monkeypatch.setattr(reg, "_select", _spy)
    return calls


def test_spec_reused_but_pool_rebound_per_host(reg, monkeypatch):
    """M3-D1：spec 复用（_select 仅 1 次）但池按 host 现建重绑，惰性值不固化首 host。"""
    reg.register("session.read", lambda h: f"v-{h}")
    calls = _spy_select(reg, monkeypatch)

    p1 = reg.build_pool(surface="ui", host="h1", plugin="p", declared={"session.read"})
    p2 = reg.build_pool(surface="ui", host="h2", plugin="p", declared={"session.read"})

    assert p1 is not p2
    assert p1._host == "h1"
    assert p2._host == "h2"
    assert len(calls) == 1  # 第二次 spec 命中，不再调 _select
    # 65723bfa3 红线钉死：池内惰性求值必须用各自 host，防闭包固化首 host
    assert p1["session.read"] == "v-h1"
    assert p2["session.read"] == "v-h2"


def test_spec_key_discriminates_declared_tri_state(reg, monkeypatch):
    """M3-D2：declared 缺省（走解析）/ None / 显式集合三态缓存键互相独立。"""
    from app.plugins.managers.plugin_manager import PluginManager

    # 缺省态插件不在索引 → 兼容期 None（与显式 None 同值，但 plugin 名不同 → 键不同）
    monkeypatch.setattr(
        PluginManager, "get_instance", classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: None))
    )
    reg.register("session.read", lambda h: 1)

    p_unset = reg.build_pool(surface="ui", host="h", plugin="p_unset")
    p_none = reg.build_pool(surface="ui", host="h", plugin="p_none", declared=None)
    p_set = reg.build_pool(surface="ui", host="h", plugin="p_set", declared={"session.read"})

    # 行为计数：三份独立规格（键数=3，含插件名区分）
    assert len(reg._pool_specs) == 3
    assert p_unset is not p_none
    assert p_none is not p_set
    assert p_unset is not p_set
    # None 态（兼容全量）⊇ 集合态（按清单裁剪）
    assert set(p_none.names()) >= set(p_set.names())


def test_register_bumps_version_invalidates_specs(reg, monkeypatch):
    """M3-F1（失效路径）：注册面变化 → 版本戳递增 → 旧 spec 不复用。"""
    reg.register("session.read", lambda h: 1)
    calls = _spy_select(reg, monkeypatch)
    p1 = reg.build_pool(surface="ui", host="h", plugin="p", declared=None)
    assert len(calls) == 1

    v0 = reg._entries_version
    reg.register("session.write", lambda h: 2)
    assert reg._entries_version == v0 + 1

    p2 = reg.build_pool(surface="ui", host="h", plugin="p", declared=None)
    assert "session.write" in p2  # 新能力对同一声明可见
    assert len(calls) >= 2  # 旧 spec 未被复用，重新 _select


def test_invalidate_declared_rebuilds_only_affected_plugin_pool(reg, monkeypatch):
    """M3-F2（失效路径）：A 声明失效后 A 随新 manifest 重建，B 功能等价不受影响。

    断言只看行为（授权随新 manifest 正确 / B names 一致），不钉「精细 vs 全清」
    的容器细节，避免把实现钉死成重构障碍。
    """
    from app.plugins.managers.plugin_manager import PluginManager

    infos = {"A": SimpleNamespace(manifest={"capabilities": ["session.read"]}, path="")}
    monkeypatch.setattr(
        PluginManager, "get_instance", classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: infos.get(n)))
    )
    reg.register("session.read", lambda h: 1)
    reg.register("model.config", lambda h: 2)

    pa1 = reg.build_pool(surface="ui", host="h", plugin="A")
    pb1 = reg.build_pool(surface="ui", host="h", plugin="B", declared={"session.read", "model.config"})
    assert pa1.names() == ["session.read"]

    # A 的 manifest 窄→宽，diff 点精细失效 A
    infos["A"].manifest = {"capabilities": ["session.read", "model.config"]}
    reg.invalidate_declared("A")

    pa2 = reg.build_pool(surface="ui", host="h", plugin="A")
    assert set(pa2.names()) == {"session.read", "model.config"}
    pb2 = reg.build_pool(surface="ui", host="h", plugin="B", declared={"session.read", "model.config"})
    assert pb2.names() == pb1.names()
