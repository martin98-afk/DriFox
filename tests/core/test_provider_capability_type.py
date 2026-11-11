# -*- coding: utf-8 -*-
"""能力类型：ProviderDef 字段与注册表动态收集。"""

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


def test_provider_def_default_is_chat():
    assert ProviderDef(name="X").capability_type == "chat"


def test_capability_types_collects_distinct_sorted():
    reg = ProviderRegistry()
    reg._providers["a"] = ProviderDef(name="A", capability_type="chat")
    reg._providers["b"] = ProviderDef(name="B", capability_type="image_generation")
    reg._providers["c"] = ProviderDef(name="C", capability_type="image_generation")
    reg._providers["d"] = ProviderDef(name="D", capability_type="video_generation")
    assert reg.capability_types() == ["image_generation", "video_generation"]
