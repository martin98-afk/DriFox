# -*- coding: utf-8 -*-
"""能力 provider 契约测试：能力面声明必须与宿主真实属性对齐。

回归背景：`team.read` provider 曾手写 `{别名: 宿主方法}` 字典，引用了不存在的
`TeamManager.get_member_agent_name`。因为字典在取用瞬间一次性构造，单个 typo
就让整个能力抛 AttributeError——system-hooks 两个注入 hook 静默空转、
`team_list_members` 工具直接报 Execution error。

能力面现改为声明式（`app/plugins/capabilities/facets.py`），本测试守着三条线：
1. provider 都能构造成功（内部引用的单例属性真实存在）
2. 声明的每个方法名在目标类上真实存在（typo 在 CI 炸，不到运行时）
3. 门面只暴露白名单内的名字，名单外（尤其写操作）访问得到 AttributeError
"""

import pytest
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _fresh_registry():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _iter_capabilities():
    """遍历两个 surface 的全部能力，产出 (surface, cap_id, 值, 异常)。"""
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    reg = CapabilityRegistry.get_instance()
    for surface in ("tool", "ui"):
        ensure_registered(surface)
        for cap_id in reg.registered(surface):
            pool = reg.build_pool(surface=surface, host=MagicMock(), plugin="", declared=None)
            try:
                yield surface, cap_id, pool[cap_id], None
            except Exception as e:  # 记录而非中断，便于一次看全
                yield surface, cap_id, None, e


def test_all_capability_providers_build():
    """每个能力都能构造成功（provider 内部引用的宿主属性真实存在）。"""
    failures = []
    for surface, cap_id, _value, err in _iter_capabilities():
        if err is not None:
            failures.append(f"{surface}:{cap_id} -> {type(err).__name__}: {err}")
    assert not failures, "能力 provider 构造失败:\n" + "\n".join(failures)


def test_facet_methods_exist_on_target():
    """facet 声明的每个方法名必须在目标类上真实存在（typo 直接炸 CI）。"""
    from app.plugins.capabilities.facets import FACETS

    missing = []
    for facet_id, spec in FACETS.items():
        module_name, _, cls_name = spec.target.partition(":")
        module = __import__(module_name, fromlist=[cls_name])
        target_cls = getattr(module, cls_name)
        for name in spec.methods:
            if not callable(getattr(target_cls, name, None)):
                missing.append(f"{facet_id} -> {cls_name}.{name}")
    assert not missing, "facet 声明了不存在的宿主方法:\n" + "\n".join(missing)


def test_facet_registered_and_in_catalog():
    """facet 声明的每个能力 ID 都要在 catalog 登记，且在至少一个 surface 注册。"""
    from app.plugins.capabilities import catalog
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.facets import FACETS
    from app.plugins.capabilities.registry import CapabilityRegistry

    reg = CapabilityRegistry.get_instance()
    for surface in ("tool", "ui"):
        ensure_registered(surface)

    for facet_id in FACETS:
        assert catalog.get(facet_id) is not None, f"{facet_id} 未在 catalog 登记"
        surfaces = {s for s in ("tool", "ui") if reg.has(facet_id, s)}
        assert surfaces, f"{facet_id} 未在任一 surface 注册"


def test_team_read_facade_hides_write_methods():
    """team.read 门面只给只读方法，写操作（send_task / leave_team）一律 AttributeError。"""
    from app.plugins.capabilities.facets import build_facet

    facade = build_facet("team.read")()
    assert callable(facade.get_members)
    assert callable(facade.get_member_agent_name)
    for write_op in ("send_task", "leave_team", "mark_mail_done", "send_reply"):
        with pytest.raises(AttributeError):
            getattr(facade, write_op)


def test_team_send_facet_exposes_only_send_task():
    """team.send 只给派单入口，不泄漏只读查询。"""
    from app.plugins.capabilities.facets import build_facet

    facade = build_facet("team.send")()
    assert callable(facade.send_task)
    with pytest.raises(AttributeError):
        getattr(facade, "get_members")


def test_facade_degrades_when_target_lacks_method():
    """目标缺方法时抛 AttributeError，调用方 getattr(..., None) 回退照常生效。"""
    from app.plugins.capabilities.facets import ReadOnlyFacade

    class _Partial:
        def get_members(self):
            return []

    facade = ReadOnlyFacade(_Partial(), ("get_members", "get_member_agent_name"), "probe")
    assert getattr(facade, "get_member_agent_name", None) is None
    assert facade.get_members() == []
