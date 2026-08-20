# -*- coding: utf-8 -*-
"""插件配置契约：dataclass 与 plugin.json config_schema 解析。"""

import pytest

from app.plugins.contracts.plugin_config import (
    PluginConfigField,
    PluginConfigSchema,
    parse_config_schema,
)


class TestPluginConfigField:
    def test_text_field_defaults(self):
        f = PluginConfigField(key="k", label="显示名", type="text")
        assert f.default == ""
        assert f.env == ""

    def test_frozen(self):
        f = PluginConfigField(key="k", label="n", type="text")
        with pytest.raises(Exception):
            f.key = "other"


class TestParseConfigSchema:
    def test_parse_full_schema(self):
        raw = {
            "title": "网页搜索 API Key",
            "fields": [
                {
                    "key": "tavily_api_key",
                    "label": "Tavily 搜索",
                    "type": "password",
                    "default": "tvly-xxx",
                    "env": "TAVILY_API_KEY",
                    "placeholder": "TAVILY_API_KEY",
                },
                {"key": "require_proxy", "label": "走代理", "type": "bool", "default": True},
            ],
        }
        schema = parse_config_schema("system", raw)
        assert schema is not None
        assert schema.plugin_name == "system"
        assert schema.title == "网页搜索 API Key"
        assert len(schema.fields) == 2
        f0 = schema.fields[0]
        assert f0.type == "password"
        assert f0.env == "TAVILY_API_KEY"
        f1 = schema.fields[1]
        assert f1.default is True
        assert f1.type == "bool"

    def test_get_field(self):
        raw = {"title": "t", "fields": [{"key": "a", "label": "A", "type": "text"}]}
        schema = parse_config_schema("p", raw)
        assert schema.get_field("a") is not None
        assert schema.get_field("missing") is None

    def test_unknown_type_returns_none(self):
        raw = {"title": "t", "fields": [{"key": "a", "label": "A", "type": "json"}]}
        assert parse_config_schema("p", raw) is None

    def test_missing_required_key_returns_none(self):
        # 字段缺 key → 整个 schema 视为无效（宁缺毋滥，配置项解析失败静默跳过）
        raw = {"title": "t", "fields": [{"label": "A", "type": "text"}]}
        assert parse_config_schema("p", raw) is None

    def test_none_raw_returns_none(self):
        assert parse_config_schema("p", None) is None

    def test_empty_fields_returns_none(self):
        raw = {"title": "t", "fields": []}
        assert parse_config_schema("p", raw) is None

    def test_type_defaulted_to_text(self):
        raw = {"title": "t", "fields": [{"key": "a", "label": "A"}]}
        schema = parse_config_schema("p", raw)
        assert schema is not None and schema.fields[0].type == "text"
