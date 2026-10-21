# -*- coding: utf-8 -*-
"""能力池契约：等级枚举 / 能力定义 / 池视图 Protocol。"""
import pytest


def test_level_values():
    from app.plugins.contracts.capabilities import CapabilityLevel

    assert CapabilityLevel.HIGH.value == "high"
    assert CapabilityLevel.NONE.value == "none"
    assert CapabilityLevel("low") is CapabilityLevel.LOW


def test_spec_is_frozen_and_defaults():
    from app.plugins.contracts.capabilities import CapabilityLevel, CapabilitySpec

    spec = CapabilitySpec(id="session.read", group="session", level=CapabilityLevel.MEDIUM, summary="读会话")
    assert spec.surfaces == frozenset({"ui"})
    assert spec.legacy_keys == ()
    with pytest.raises(Exception):
        spec.id = "other"


def test_pool_context_fields():
    from app.plugins.contracts.capabilities import PoolContext

    ctx = PoolContext(plugin="p1", surface="tool", registry=object())
    assert ctx.plugin == "p1" and ctx.surface == "tool"


def test_pool_view_protocol_runtime_checkable():
    from app.plugins.contracts.capabilities import CapabilityPoolView

    class _Pool:
        def __contains__(self, k):
            return True

        def __getitem__(self, k):
            return None

        def names(self):
            return []

        def group(self, g):
            return {}

    assert isinstance(_Pool(), CapabilityPoolView)
    assert not isinstance(object(), CapabilityPoolView)
