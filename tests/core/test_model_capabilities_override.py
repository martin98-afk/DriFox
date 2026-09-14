# -*- coding: utf-8 -*-
"""get_model_capabilities 用户覆盖层测试。

查找链：用户覆盖 > models.dev > 硬编码。
关键回归：不传 provider_name 时行为与改造前完全一致。
"""

import pytest

from app.core import model_capabilities as mc


@pytest.fixture
def fake_override(monkeypatch):
    """注入假的覆盖读取，避免依赖真实配置。"""

    def _install(mapping):
        # mapping: {(provider, model): {中文键: 值}}
        monkeypatch.setattr(
            mc,
            "_read_user_override",
            lambda provider, model: mapping.get((provider, model), {}),
            raising=False,
        )

    return _install


class TestBackwardCompatibility:
    def test_no_provider_name_skips_override(self, monkeypatch):
        """不传 provider_name 时不查覆盖（20 处旧调用零回归）"""
        called = {"n": 0}

        def spy(provider, model):
            called["n"] += 1
            return {}

        monkeypatch.setattr(mc, "_read_user_override", spy, raising=False)
        mc.get_model_capabilities("glm-5")
        assert called["n"] == 0, "缺省 provider_name 不应触发覆盖查询"

    def test_empty_model_name(self):
        assert mc.get_model_capabilities("") == {}
        assert mc.get_model_capabilities("", "MyProvider") == {}


class TestOverridePrecedence:
    def test_override_beats_hardcoded(self, fake_override, monkeypatch):
        """用户覆盖 > 硬编码"""
        fake_override({("MyProvider", "glm-5"): {"支持思考": False}})
        monkeypatch.setitem(
            mc.MODEL_CAPABILITIES,
            "glm-5",
            {"supports_thinking": True, "context_limit": 204800, "source": "test"},
        )
        caps = mc.get_model_capabilities("glm-5", "MyProvider")
        assert caps["supports_thinking"] is False
        assert caps["context_limit"] == 204800, "未覆盖的字段应保留"

    def test_override_applies_to_unknown_model(self, fake_override, monkeypatch):
        """自定义模型（硬编码与 models.dev 都查不到）也能拿到覆盖能力"""
        monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda name: None)
        fake_override({("MyProvider", "my-custom-model"): {"支持思考": True, "上下文窗口": 999}})
        caps = mc.get_model_capabilities("my-custom-model", "MyProvider")
        assert caps["supports_thinking"] is True
        assert caps["context_limit"] == 999

    def test_override_beats_dynamic(self, fake_override, monkeypatch):
        """用户覆盖 > models.dev 动态数据"""
        monkeypatch.setattr(
            mc,
            "_get_dynamic_model_capabilities",
            lambda name: {"supports_thinking": False, "context_limit": 128000},
        )
        fake_override({("MyProvider", "glm-5"): {"支持思考": True}})
        caps = mc.get_model_capabilities("glm-5", "MyProvider")
        assert caps["supports_thinking"] is True

    def test_no_override_entry_unchanged(self, fake_override):
        """有 provider_name 但该模型无覆盖 -> 结果与非覆盖路径一致"""
        fake_override({})
        assert mc.get_model_capabilities("glm-5", "MyProvider") == mc.get_model_capabilities("glm-5")


class TestApplyModelDefaultsThinking:
    def test_unknown_model_with_override_gets_thinking(self, fake_override, monkeypatch):
        """自定义模型：勾选支持思考后应注入思考默认值。

        回归：旧实现把思考注入嵌在 if caps.get("context_limit") 内，
        未知模型（无 context_limit）整块跳过，思考永远不注入。
        """
        monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda name: None)
        fake_override(
            {("MyProvider", "custom-1"): {"支持思考": True, "思考参数": "reasoning_effort", "上下文窗口": 100000}}
        )
        result = mc.apply_model_defaults({}, "custom-1", "MyProvider")
        assert result.get("思考模式") is True
        assert result.get("最大Token") == 100000

    def test_toggle_type_no_effort_level(self, fake_override, monkeypatch):
        """thinking 型（非 reasoning_effort）不注入思考等级"""
        monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda name: None)
        fake_override({("P", "custom-2"): {"支持思考": True, "思考参数": "thinking", "上下文窗口": 64000}})
        result = mc.apply_model_defaults({}, "custom-2", "P")
        assert result.get("思考模式") is True
        assert "思考等级" not in result

    def test_effort_type_injects_level(self, fake_override, monkeypatch):
        """reasoning_effort 型且有可选值时注入思考等级"""
        monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda name: None)
        fake_override(
            {
                ("P", "custom-3"): {
                    "支持思考": True,
                    "思考参数": "reasoning_effort",
                    "思考等级可选值": ["none", "low", "high"],
                    "上下文窗口": 64000,
                }
            }
        )
        result = mc.apply_model_defaults({}, "custom-3", "P")
        assert result.get("思考等级") == "low", "应取第一个非关闭档位（跳过 none）"

    def test_no_override_keeps_old_behavior(self, fake_override, monkeypatch):
        """无覆盖时行为与改造前一致：不支持思考的模型不注入思考字段"""
        fake_override({})
        monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda name: None)
        result = mc.apply_model_defaults({}, "unknown-model-xyz")
        assert "思考模式" not in result
        assert "思考等级" not in result


class TestSupportsVisionOverride:
    def test_override_enables_vision(self, monkeypatch):
        """用户声明支持多模态 -> 即使模型名不含视觉关键词也返回 True"""
        from app.core import model_capabilities as mc
        from app.core import provider_profile as pp

        monkeypatch.setattr(mc, "get_model_capabilities", lambda model, provider="": {"supports_vision": True})
        assert pp.supports_vision({"模型名称": "my-custom-model", "provider_name": "P"}) is True

    def test_override_does_not_disable_keyword_match(self, monkeypatch):
        """关键词命中仍为 True（覆盖层不反向关闭，保持既有行为）"""
        from app.core import model_capabilities as mc
        from app.core import provider_profile as pp

        monkeypatch.setattr(mc, "get_model_capabilities", lambda model, provider="": {})
        assert pp.supports_vision({"模型名称": "glm-4v", "provider_name": "P"}) is True

    def test_no_override_no_keyword_returns_false(self, monkeypatch):
        from app.core import model_capabilities as mc
        from app.core import provider_profile as pp

        monkeypatch.setattr(mc, "get_model_capabilities", lambda model, provider="": {})
        assert pp.supports_vision({"模型名称": "plain-model", "provider_name": "P"}) is False
