# -*- coding: utf-8 -*-
"""provider.list / model.enumerate 离线单测（双 surface，不依赖 Qt 与真实配置）。

背景：插件渲染「选服务商 / 选模型」下拉此前没有合规通道，四个插件各自
json.load(~/.drifox/app.config) 的 LLM.SavedProviders 抄同一份样板。
本测试锁定新增能力的契约：不含密钥、未知服务商不静默串、能力标记走声明优先。
"""

from types import SimpleNamespace

import pytest

from app.plugins.capabilities.providers import model_meta


@pytest.fixture(autouse=True)
def _fresh():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _widget():
    """伪 MainWidget：两份服务商实例（同服务商两个实例，测 display_name 回落）。"""
    valid = {
        "cid-1": {
            "provider_name": "MiniMax",
            "display_name": "MiniMax #1",
            "API_URL": "https://api.example.com/v1",
            "API_KEY": "plain-secret-key",
            "模型名称": "MiniMax-M3",
            "模型列表": ["MiniMax-M3", "MiniMax-M2.7"],
        },
        "cid-2": {
            "provider_name": "MiniMax",
            "API_KEY": "another-secret",
            "模型名称": "MiniMax-M2.7",
            "模型列表": ["MiniMax-M2.7"],
        },
    }
    return SimpleNamespace(
        _valid_configs=valid,
        _current_provider_name="cid-1",
        _current_model_name="MiniMax-M3",
        _get_model_list_for_provider=lambda cid: list((valid.get(cid) or {}).get("模型列表") or []),
        _resolve_service_provider=lambda name: {
            "cid-1": "cid-1",
            "cid-2": "cid-2",
            "MiniMax #1": "cid-1",
            "MiniMax": "cid-1",
        }.get(str(name or "").strip()),
    )


def _pool(surface, declared, host):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered(surface)
    return CapabilityRegistry.get_instance().build_pool(surface=surface, host=host, plugin="demo", declared=declared)


# ── provider.list ──


def test_provider_list_no_secret_fields():
    """绝不带 API_KEY / API_URL（URL 配合社工可劫持插件流量）"""
    out = _pool("ui", {"provider.list"}, _widget())["provider.list"]()
    assert len(out) == 2
    for item in out:
        assert "API_KEY" not in item
        assert "API_URL" not in item
        assert "plain-secret-key" not in str(item)
        assert "another-secret" not in str(item)


def test_provider_list_fields_and_current():
    out = {p["id"]: p for p in _pool("ui", {"provider.list"}, _widget())["provider.list"]()}
    assert out["cid-1"]["provider_name"] == "MiniMax"
    assert out["cid-1"]["display_name"] == "MiniMax #1"
    assert out["cid-1"]["is_current"] is True
    assert out["cid-1"]["model_count"] == 2
    assert out["cid-2"]["is_current"] is False


def test_provider_list_display_name_fallback():
    """display_name 缺失 → 回落 provider_name（cid-2 无 display_name）"""
    out = {p["id"]: p for p in _pool("ui", {"provider.list"}, _widget())["provider.list"]()}
    assert out["cid-2"]["display_name"] == "MiniMax"


def test_provider_list_sorted_by_display_name():
    """同名实例按 display_name 稳定排序（MiniMax #1 先于 MiniMax）"""
    names = [p["display_name"] for p in _pool("ui", {"provider.list"}, _widget())["provider.list"]()]
    assert names == ["MiniMax", "MiniMax #1"]


def test_provider_list_empty_when_no_configs():
    host = SimpleNamespace(_valid_configs={})
    assert _pool("ui", {"provider.list"}, host)["provider.list"]() == []


def test_provider_list_empty_when_host_none():
    """tool surface / 无活跃窗口 → 空列表，不抛错"""
    host = SimpleNamespace(_valid_configs=None)
    assert _pool("tool", {"provider.list"}, host)["provider.list"]() == []


# ── model.enumerate ──


def test_enumerate_current_provider_by_default():
    out = _pool("ui", {"model.enumerate"}, _widget())["model.enumerate"]()
    names = [m["name"] for m in out]
    assert names == ["MiniMax-M3", "MiniMax-M2.7"]
    assert out[0]["is_current"] is True
    assert out[1]["is_current"] is False


def test_enumerate_by_display_name():
    out = _pool("ui", {"model.enumerate"}, _widget())["model.enumerate"]("MiniMax #1")
    assert [m["name"] for m in out] == ["MiniMax-M3", "MiniMax-M2.7"]


def test_enumerate_unknown_provider_returns_empty():
    """显式指名的服务商不存在 → 空列表，不静默串到当前服务商"""
    out = _pool("ui", {"model.enumerate"}, _widget())["model.enumerate"]("不存在的服务商")
    assert out == []


def test_enumerate_fields_shape():
    """返回结构固定：name/is_current/supports_vision/supports_thinking/context_limit/note"""
    out = _pool("ui", {"model.enumerate"}, _widget())["model.enumerate"]("cid-1")
    for item in out:
        assert set(item) == {
            "name",
            "is_current",
            "supports_vision",
            "supports_thinking",
            "context_limit",
            "note",
        }
        assert isinstance(item["supports_vision"], bool)
        assert isinstance(item["supports_thinking"], bool)


def test_enumerate_second_instance_uses_its_own_current_model():
    """非当前服务商实例：is_current 取该实例自己的「模型名称」，而非窗口当前模型"""
    out = _pool("ui", {"model.enumerate"}, _widget())["model.enumerate"]("cid-2")
    assert [m["name"] for m in out] == ["MiniMax-M2.7"]
    assert out[0]["is_current"] is True


# ── 能力标记：声明层优先（L0）──


def test_declared_vision_beats_models_dev(monkeypatch):
    """「声明_支持图像」非空时以声明为准，不被 models.dev 推断值反向覆盖"""
    import app.plugins.capabilities.providers.model_meta as mm

    monkeypatch.setattr(
        mm,
        "_model_caps",
        lambda model, provider: {
            "supports_vision": True,
            "supports_thinking": False,
            "context_limit": 200000,
            "note": "",
        },
    )
    out = _pool("ui", {"model.enumerate"}, _widget())["model.enumerate"]("cid-1")
    assert all(m["supports_vision"] is True for m in out)


def test_model_caps_uses_resolve_chain(monkeypatch):
    """_model_caps 走 resolve_capabilities_for_model（含 ModelOverrides 声明层）"""
    import app.core.modelmeta.model_capabilities as mc
    import app.plugins.capabilities.providers.model_meta as mm

    seen = {}

    def _fake_resolve(model, provider="", sources=None):
        seen["model"] = model
        seen["provider"] = provider
        # 模拟用户在能力面板显式声明「支持图像」
        return {"supports_vision": True, "supports_thinking": False, "context_limit": 4096}

    monkeypatch.setattr(mc, "resolve_capabilities_for_model", _fake_resolve, raising=False)
    caps = mm._model_caps("gw-model", "gw-provider")
    assert caps["supports_vision"] is True
    assert caps["context_limit"] == 4096
    assert seen.get("model") == "gw-model"
    assert seen.get("provider") == "gw-provider"


def test_model_caps_tolerates_failure(monkeypatch):
    """能力查询异常不应中断枚举（降级为全 False 而非抛出）"""
    import app.core.modelmeta.model_capabilities as mc
    import app.plugins.capabilities.providers.model_meta as mm

    def _boom(model, provider="", sources=None):
        raise RuntimeError("models.dev 不可用")

    monkeypatch.setattr(mc, "resolve_capabilities_for_model", _boom, raising=False)
    caps = mm._model_caps("x", "y")
    assert caps == {
        "supports_vision": False,
        "supports_thinking": False,
        "context_limit": None,
        "note": "",
    }


# ── 授权裁剪 ──


def test_undeclared_raises_capability_denied():
    from app.plugins.capabilities.pool import CapabilityDenied

    pool = _pool("ui", {"ui.notify"}, _widget())
    with pytest.raises(CapabilityDenied):
        _ = pool["provider.list"]
    with pytest.raises(CapabilityDenied):
        _ = pool["model.enumerate"]


def test_registered_on_both_surfaces():
    """两个能力在 ui + tool 双 surface 都有 provider（工具选模型是真实场景）"""
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    for surface in ("ui", "tool"):
        CapabilityRegistry.reset_instance()
        bootstrap.reset_bootstrap()
        ensure_registered(surface)
        reg = CapabilityRegistry.get_instance()
        for cap in ("provider.list", "model.enumerate"):
            assert reg.has(cap, surface=surface), f"{cap} 未注册于 {surface}"


def test_tool_surface_callable_from_tool_pool():
    """tool surface 下 provider 可调用（host 无 _valid_configs 时返回空，不抛错）"""
    pool = _pool("tool", {"provider.list", "model.enumerate"}, SimpleNamespace(_valid_configs=None))
    assert pool["provider.list"]() == []
    assert pool["model.enumerate"]() == []
