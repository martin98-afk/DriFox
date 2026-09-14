# -*- coding: utf-8 -*-
"""模型能力覆盖模块测试。

覆盖：中文键 -> 标准键映射、apply_override 覆盖优先级、空覆盖不改变原 caps、
provider/model 解析键格式。
"""

from app.utils.model_capability_override import (
    OVERRIDE_KEY_MAP,
    apply_override,
    get_override,
    to_caps_keys,
)


class TestOverrideKeyMap:
    def test_all_keys_are_chinese(self):
        """覆盖值必须用中文键名。

        chat_worker 的兜底逻辑会把匹配 _VALID_IDENTIFIER_PATTERN
        （^[a-zA-Z_][a-zA-Z0-9_]*$）的未知键当 api_param 直发，
        英文键名会导致整块能力字典泄漏进 extra_body。
        """
        import re

        pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
        for cn_key in OVERRIDE_KEY_MAP:
            assert not pattern.match(cn_key), f"覆盖键名 {cn_key!r} 匹配标识符正则，会被当 api_param 直发到 API"


class TestToCapsKeys:
    def test_thinking_fields(self):
        result = to_caps_keys({"支持思考": True, "思考参数": "thinking", "思考启用值": "adaptive"})
        assert result == {
            "supports_thinking": True,
            "thinking_param": "thinking",
            "thinking_enable_value": "adaptive",
        }

    def test_vision_and_context(self):
        result = to_caps_keys({"支持多模态": True, "上下文窗口": 200000})
        assert result == {"supports_vision": True, "context_limit": 200000}

    def test_effort_values(self):
        result = to_caps_keys({"思考等级可选值": ["low", "high"]})
        assert result == {"reasoning_effort_values": ["low", "high"]}

    def test_effort_values_from_comma_string(self):
        """逗号分隔字符串切成列表（模型配置卡里是 line 控件）"""
        result = to_caps_keys({"思考等级可选值": "low, medium ,high"})
        assert result == {"reasoning_effort_values": ["low", "medium", "high"]}

    def test_empty_input(self):
        assert to_caps_keys({}) == {}
        assert to_caps_keys(None) == {}

    def test_unknown_keys_ignored(self):
        assert to_caps_keys({"不认识的键": 1}) == {}


class TestApplyOverride:
    def test_override_wins_over_base(self):
        """用户覆盖 > models.dev/硬编码（查找链最高优先级）"""
        caps = {"supports_thinking": False, "context_limit": 128000}
        result = apply_override(caps, {"支持思考": True, "上下文窗口": 200000})
        assert result["supports_thinking"] is True
        assert result["context_limit"] == 200000

    def test_base_fields_preserved(self):
        """未被覆盖的字段原样保留"""
        caps = {"supports_thinking": False, "cost": {"input": 1.0}, "note": "x"}
        result = apply_override(caps, {"支持思考": True})
        assert result["cost"] == {"input": 1.0}
        assert result["note"] == "x"

    def test_empty_override_returns_copy(self):
        """空覆盖不改动（仍返回副本，避免调用方误改缓存）"""
        caps = {"supports_thinking": True}
        result = apply_override(caps, {})
        assert result == {"supports_thinking": True}
        assert result is not caps

    def test_false_override_is_applied(self):
        """显式 False 也要覆盖（用户声明该模型不支持思考）"""
        caps = {"supports_thinking": True}
        assert apply_override(caps, {"支持思考": False})["supports_thinking"] is False

    def test_does_not_mutate_base(self):
        caps = {"supports_thinking": False}
        apply_override(caps, {"支持思考": True})
        assert caps == {"supports_thinking": False}


class TestGetOverride:
    def test_returns_empty_when_key_missing(self, monkeypatch):
        """无该模型覆盖时返回空 dict，不抛异常"""
        monkeypatch.setattr(
            "app.utils.model_capability_override._load_model_overrides",
            lambda: {},
        )
        monkeypatch.setattr(
            "app.utils.model_capability_override._resolve_provider_name",
            lambda p: p,
        )
        assert get_override("MyProvider", "glm-5") == {}

    def test_key_format(self, monkeypatch):
        """覆盖值按「服务商名||模型名」索引"""
        monkeypatch.setattr(
            "app.utils.model_capability_override._load_model_overrides",
            lambda: {"MyProvider||glm-5": {"支持思考": True}},
        )
        monkeypatch.setattr(
            "app.utils.model_capability_override._resolve_provider_name",
            lambda p: p,
        )
        assert get_override("MyProvider", "glm-5") == {"支持思考": True}

    def test_config_id_resolved_to_provider_name(self, monkeypatch):
        """入参是 config_id 时先规范成 provider_name（改密钥后覆盖不丢）"""
        monkeypatch.setattr(
            "app.utils.model_capability_override._load_model_overrides",
            lambda: {"MyProvider||glm-5": {"支持思考": True}},
        )
        monkeypatch.setattr(
            "app.utils.model_capability_override._resolve_provider_name",
            lambda p: "MyProvider" if p == "abc12345" else p,
        )
        assert get_override("abc12345", "glm-5") == {"支持思考": True}

    def test_non_capability_keys_filtered(self, monkeypatch):
        """同一 key 下混着普通参数（温度等）时只取能力键"""
        monkeypatch.setattr(
            "app.utils.model_capability_override._load_model_overrides",
            lambda: {"P||m": {"支持思考": True, "温度": 0.5}},
        )
        monkeypatch.setattr(
            "app.utils.model_capability_override._resolve_provider_name",
            lambda p: p,
        )
        assert get_override("P", "m") == {"支持思考": True}

    def test_missing_model_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.model_capability_override._load_model_overrides",
            lambda: {"P||other": {"支持思考": True}},
        )
        monkeypatch.setattr(
            "app.utils.model_capability_override._resolve_provider_name",
            lambda p: p,
        )
        assert get_override("P", "glm-5") == {}

    def test_empty_provider_or_model(self):
        assert get_override("", "glm-5") == {}
        assert get_override("MyProvider", "") == {}


class TestParamSchemaRegistration:
    def test_capability_fields_in_schema(self):
        """能力字段必须在 PARAM_SCHEMA 中登记（否则模型配置卡不渲染）"""
        from app.constants import PARAM_SCHEMA

        for key in ("支持思考", "思考参数", "思考等级可选值", "思考启用值", "支持多模态"):
            assert key in PARAM_SCHEMA, f"{key} 未登记到 PARAM_SCHEMA"

    def test_capability_fields_are_model_level(self):
        """能力字段按模型名持久化（MODEL_LEVEL_KEYS）"""
        from app.constants import MODEL_LEVEL_KEYS

        for key in ("支持思考", "思考参数", "思考等级可选值", "思考启用值", "支持多模态"):
            assert key in MODEL_LEVEL_KEYS, f"{key} 未加入 MODEL_LEVEL_KEYS"

    def test_capability_fields_no_api_param(self):
        """能力字段是 DriFox 内部元数据，不能映射成 API 参数"""
        from app.constants import PARAM_SCHEMA

        for key in ("支持思考", "思考参数", "思考等级可选值", "思考启用值", "支持多模态"):
            assert "api_param" not in PARAM_SCHEMA[key], f"{key} 不应有 api_param"
