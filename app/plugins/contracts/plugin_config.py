# -*- coding: utf-8 -*-
"""插件配置契约 — plugin.json 声明式配置 schema 的数据结构与解析。

设计（万物即插件 E1）：插件在 plugin.json 里声明 "config_schema"：
    "config_schema": {
        "title": "网页搜索 API Key",
        "fields": [
            {"key": "tavily_api_key", "label": "Tavily 搜索", "type": "password",
             "default": "<内置默认>", "env": "TAVILY_API_KEY", "placeholder": "TAVILY_API_KEY"}
        ]
    }
主程序据此自动提供：统一存储（PluginConfigStore）+ 设置面板渲染（PluginConfigCard）。
插件不再手写存储与 UI 样板（参照 websearch 迁移前 ~164 行手写代码 → 0 行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from loguru import logger

# 支持的字段类型（渲染映射见 plugin_config_card.py）
FIELD_TYPES = ("text", "password", "bool")


@dataclass(frozen=True)
class PluginConfigField:
    """单个配置字段声明

    Attributes:
        key: 存储键（插件内唯一；对应 config.json 的顶层键）
        label: 设置面板显示名
        type: text（单行输入）/ password（密码输入）/ bool（开关）
        default: 默认值（bool 字段为 bool，其余为 str）
        env: 环境变量名（非空时环境变量优先级高于存储值）
        placeholder: 输入框占位文本
        description: 字段说明（渲染为卡片 content）
    """

    key: str
    label: str
    type: str = "text"
    default: Any = ""
    env: str = ""
    placeholder: str = ""
    description: str = ""


@dataclass(frozen=True)
class PluginConfigSchema:
    """一个插件的完整配置声明"""

    plugin_name: str
    title: str
    fields: List[PluginConfigField] = field(default_factory=list)

    def get_field(self, key: str) -> Optional[PluginConfigField]:
        for f in self.fields:
            if f.key == key:
                return f
        return None


def parse_config_schema(plugin_name: str, raw: Optional[dict]) -> Optional[PluginConfigSchema]:
    """解析 plugin.json 的 config_schema 对象。

    容错原则：任一字段非法 → 整个 schema 返回 None（记 warning，不抛异常），
    插件照常加载，只是没有配置 UI。
    """
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or plugin_name)
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        logger.warning(f"[PluginConfig] {plugin_name} config_schema.fields 为空或缺失，忽略")
        return None
    fields: List[PluginConfigField] = []
    for item in raw_fields:
        if not isinstance(item, dict):
            logger.warning(f"[PluginConfig] {plugin_name} 字段非对象，忽略整个 schema: {item!r}")
            return None
        key = str(item.get("key") or "").strip()
        if not key:
            logger.warning(f"[PluginConfig] {plugin_name} 字段缺 key，忽略整个 schema: {item!r}")
            return None
        ftype = str(item.get("type") or "text")
        if ftype not in FIELD_TYPES:
            logger.warning(f"[PluginConfig] {plugin_name} 字段 {key} 类型未知({ftype})，忽略整个 schema")
            return None
        default = item.get("default", "" if ftype != "bool" else False)
        if ftype == "bool" and not isinstance(default, bool):
            default = bool(default)
        fields.append(
            PluginConfigField(
                key=key,
                label=str(item.get("label") or key),
                type=ftype,
                default=default,
                env=str(item.get("env") or ""),
                placeholder=str(item.get("placeholder") or ""),
                description=str(item.get("description") or ""),
            )
        )
    return PluginConfigSchema(plugin_name=plugin_name, title=title, fields=fields)
