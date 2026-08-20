# -*- coding: utf-8 -*-
"""Gateway 平台契约与注册表测试（E2 Task 1）。

registry.register 必须接 source= kwarg（对齐 serializer_registry / runtime_component_loader）。
GatewayPlatformDef frozen，因此 registry 内部若需重打 source 必须用 dataclasses.replace。
"""

import pytest

from app.plugins.contracts.gateway_platform import GatewayPlatformDef
from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry


def _fake_adapter(cfg):
    class _A:
        def __init__(self, config):
            self.config = config

    return _A(cfg)


def _make_def(pid="fake-pt", source="", ui_order=100):
    return GatewayPlatformDef(
        platform_id=pid,
        display_name="Fake PT",
        adapter_factory=_fake_adapter,
        source=source,
        ui_order=ui_order,
    )


class TestDefDefaults:
    def test_defaults(self):
        d = _make_def()
        assert d.check_requirements() is True
        assert d.config_builder is None
        assert d.ui_order == 100

    def test_frozen(self):
        d = _make_def()
        with pytest.raises(Exception):
            d.platform_id = "x"

    def test_id_property_aliases_platform_id(self):
        # runtime loader 覆盖判定用 getattr(item, "id", None)
        d = _make_def(pid="abc")
        assert d.id == "abc"
        assert d.id == d.platform_id


class TestRegistry:
    def test_register_get_list_order(self):
        reg = GatewayPlatformRegistry()
        reg.register(_make_def("b", ui_order=20))
        reg.register(_make_def("a", ui_order=10))
        assert [d.platform_id for d in reg.list_platforms()] == ["a", "b"]
        assert reg.get("a").display_name == "Fake PT"

    def test_same_id_overwrites(self):
        reg = GatewayPlatformRegistry()
        reg.register(_make_def("x"), source="plugin:p1")
        reg.register(_make_def("x"), source="plugin:p2")  # user 覆盖 system 同 id
        assert len(reg.list_platforms()) == 1
        assert reg.get("x").source == "plugin:p2"

    def test_unregister_source(self):
        reg = GatewayPlatformRegistry()
        reg.register(_make_def("x"), source="plugin:p1")
        reg.register(_make_def("y"), source="plugin:p1")
        reg.register(_make_def("z"), source="plugin:p2")
        removed = reg.unregister_source("plugin:p1")
        assert sorted(removed) == ["x", "y"]
        assert reg.get("z") is not None
        assert reg.unregister_source("plugin:none") == []

    def test_singleton(self):
        assert (
            GatewayPlatformRegistry.get_instance()
            is GatewayPlatformRegistry.get_instance()
        )