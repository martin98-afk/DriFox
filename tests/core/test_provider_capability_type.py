# -*- coding: utf-8 -*-
"""能力类型：ProviderDef 字段与注册表动态收集。"""

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


def test_provider_def_default_is_chat():
    assert ProviderDef(name="X").capability_type == "chat"


def test_capability_types_collects_distinct_sorted():
    reg = ProviderRegistry()
    reg.ensure_loaded = lambda: None  # 屏蔽懒加载：本测试手动塞数据，不触发插件扫描
    reg._providers["a"] = ProviderDef(name="A", capability_type="chat")
    reg._providers["b"] = ProviderDef(name="B", capability_type="image_generation")
    reg._providers["c"] = ProviderDef(name="C", capability_type="image_generation")
    reg._providers["d"] = ProviderDef(name="D", capability_type="video_generation")
    assert reg.capability_types() == ["image_generation", "video_generation"]


from app.core.modelmeta.provider_profile import resolve_capability_type


def test_resolve_capability_type_unknown_provider_defaults_chat():
    assert resolve_capability_type({"provider_name": "不存在的服务商"}) == "chat"


def test_resolve_capability_type_empty_info_defaults_chat():
    assert resolve_capability_type({}) == "chat"


def test_resolve_capability_type_reads_registered_def(monkeypatch):
    from app.plugins.registries import provider_registry as mod

    class _P:
        capability_type = "speech_generation"

    class _R:
        @staticmethod
        def get_instance():
            return _R()

        @staticmethod
        def get(name):
            return _P() if name == "豆包语音合成" else None

    monkeypatch.setattr(mod, "ProviderRegistry", _R)
    assert resolve_capability_type({"provider_name": "豆包语音合成"}) == "speech_generation"
