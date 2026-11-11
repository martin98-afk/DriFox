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


def test_resolve_capability_type_prefers_disk_key_for_custom_provider():
    """自定义服务商（无 ProviderDef）：磁盘 capability_type 键是权威来源，反查失败不得回退 chat。"""
    assert resolve_capability_type({
        "provider_name": "我的图像服务商",
        "capability_type": "image_generation",
    }) == "image_generation"


def test_resolve_capability_type_disk_key_overrides_registry():
    """自定义服务商（反查 miss）：磁盘 capability_type 键是权威来源（编辑卡下拉落盘）。"""
    # 该 provider_name 反查不到定义（未注册），磁盘键生效
    assert resolve_capability_type({
        "provider_name": "未知服务商",
        "capability_type": "video_generation",
    }) == "video_generation"


def test_resolve_capability_type_declaration_wins_over_stale_disk_snapshot(monkeypatch):
    """已注册实例声明优先于磁盘快照：插件升级改声明后，旧落盘快照不得压死新声明。"""
    from app.plugins.registries import provider_registry as mod

    class _P:
        capability_type = "video_generation"

    class _R:
        @staticmethod
        def get_instance():
            return _R()

        @staticmethod
        def get(name):
            return _P() if name == "某视频服务商" else None

    monkeypatch.setattr(mod, "ProviderRegistry", _R)
    # 磁盘键是 preset 实例保存时落的旧快照（image_generation），新声明已改为 video_generation
    assert resolve_capability_type({
        "provider_name": "某视频服务商",
        "capability_type": "image_generation",
    }) == "video_generation"
