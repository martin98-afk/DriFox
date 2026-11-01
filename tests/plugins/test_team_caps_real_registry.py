# -*- coding: utf-8 -*-
"""团队四能力的「半真实注册链」形态测试（#23 ③）。

三层互补关系（与 test_capability_pool_contract / test_capability_value_style_ast）：
- ① pool 契约测试锁「值就是 provider 返回值」
- ② AST 检查锁「调用侧不多调」
- ③ 本测试锁「真实注册链下四个能力的形态符合 catalog 的 value_style 声明」
但 spawn_members 的真实入队/开窗只有人工冒烟能验，三层不替代冒烟。

前提（review 实测坑）：
- build_pool 必须显式传 declared（缺省走真实 manifest 解析）
- facet 三件调用会触发 TeamManager.get_instance()（mkdir 真实 teams）→
  fixture 必须 patch _get_teams_dir 到临时目录
- plugin 名独立（test_caps），避开真实 system-tools 池缓存
"""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def caps_env(monkeypatch, tmp_path):
    from app.core.team import team_manager as tmm

    monkeypatch.setattr(tmm.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path))
    tmm.TeamManager.reset_instance()

    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()

    from app.plugins.capabilities.facets import ReadOnlyFacade

    ensure_registered = bootstrap.ensure_registered
    registry = CapabilityRegistry.get_instance()
    declared = {"team.spawn", "team.read", "team.send", "team.manage", "action.queue"}

    def build(host=None, plugin="test_caps"):
        ensure_registered("tool")
        return registry.build_pool(surface="tool", host=host, plugin=plugin, declared=set(declared))

    yield {"build": build, "facade_cls": ReadOnlyFacade, "registry": registry, "bootstrap": bootstrap}

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    tmm.TeamManager.reset_instance()


def test_team_spawn_value_style_dict_structured_reject(caps_env):
    """team.spawn 一段式：dict + 两键；host=None 时结构化拒绝（不抛异常）。"""
    pool = caps_env["build"](host=None)
    v = pool["team.spawn"]
    assert isinstance(v, dict)
    assert set(v) >= {"spawn_members", "ensure_leader_watch"}
    receipt = v["spawn_members"]([{"agent_name": "build"}], "run-x")
    assert receipt.get("submitted") is False and receipt.get("error")
    # 不抛异常即可到达此处


def test_team_facets_factory_style_readonly_facade(caps_env):
    """team.read/send/manage 两段式：callable 且调用后是 ReadOnlyFacade。"""
    pool = caps_env["build"](host=SimpleNamespace())
    for cap_id in ("team.read", "team.send", "team.manage"):
        v = pool[cap_id]
        assert callable(v), cap_id
        facade = v()
        assert isinstance(facade, caps_env["facade_cls"]), cap_id


def test_action_queue_factory_exposes_only_peek(caps_env):
    pool = caps_env["build"](host=SimpleNamespace())
    v = pool["action.queue"]
    assert callable(v)
    facade = v()
    assert isinstance(facade, caps_env["facade_cls"])
    assert sorted(facade.__dir__()) == ["peek"]


def test_pool_shape_matches_catalog_value_style(caps_env):
    """pool 值形态 ↔ catalog.value_style 一致（catalog 标错在此被抓）。"""
    from app.plugins.capabilities.catalog import CATALOG
    from app.plugins.capabilities.facets import ReadOnlyFacade

    pool = caps_env["build"](host=SimpleNamespace())
    for cap_id in ("team.spawn", "team.read", "team.send", "team.manage", "action.queue"):
        spec = CATALOG[cap_id]
        v = pool[cap_id]
        if spec.value_style == "value":
            assert not callable(v) or isinstance(v, dict), f"{cap_id} 标 value 但 pool 值是可调用非 dict"
        else:
            assert callable(v), f"{cap_id} 标 factory 但 pool 值不可调用"
            if cap_id != "team.send":  # send 的返回对象类型随 facade 集合，无需逐一断言
                assert isinstance(v(), ReadOnlyFacade), cap_id
