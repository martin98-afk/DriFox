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


from app.core.modelmeta.provider_profile import (
    ProviderConfigCollision,
    apply_provider_save,
    compute_provider_config_id,
)
import pytest


def test_same_url_and_key_collides():
    """spec 3.1：不同能力共用同一 (URL, KEY) 会被拒，靠插件声明不同默认 URL 规避。"""
    saved = {}
    a = {"API_URL": "https://api.example.com/v1", "API_KEY": "sk-same"}
    cid = apply_provider_save(saved, a, "示例", is_new=True)
    b = {"API_URL": "https://api.example.com/v1", "API_KEY": "sk-same"}
    with pytest.raises(ProviderConfigCollision):
        apply_provider_save(saved, b, "示例图像", is_new=True)
    assert cid in saved


def test_different_url_no_collision():
    saved = {}
    a = {"API_URL": "https://api.example.com/v1", "API_KEY": "sk-same"}
    c1 = apply_provider_save(saved, a, "示例Chat", is_new=True)
    b = {"API_URL": "https://api.example.com/v1/images", "API_KEY": "sk-same"}
    c2 = apply_provider_save(saved, b, "示例图像", is_new=True)
    assert c1 != c2
    assert compute_provider_config_id(a) != compute_provider_config_id(b)


from app.core.modelmeta.provider_profile import chat_fallback_candidate


def test_chat_fallback_candidate_only_chat():
    configs = {
        "img-1": {"provider_name": "示例图像", "capability_type": "image_generation"},
        "chat-1": {"provider_name": "示例Chat", "capability_type": "chat"},
    }
    assert chat_fallback_candidate(configs) == "chat-1"


def test_chat_fallback_candidate_empty_when_no_chat():
    assert chat_fallback_candidate({"img-1": {"capability_type": "image_generation"}}) == ""
