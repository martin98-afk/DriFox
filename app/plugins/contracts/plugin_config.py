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

字段类型（渲染映射见 plugin_config_card.py）：
    text      单行输入
    password  密码输入（PasswordLineEdit）
    bool      开关（SwitchButton）
    select    下拉选择（ComboBox），必须声明 options
    number    整数输入（SpinBox），可选 min/max/step
    textarea  多行文本（TextEdit），可选 rows（显示行数）
    link      外链按钮（可点击超链接，必须声明 url；无存储值，纯展示）

select 的 options 声明（value 为存储值，label 为显示名）：
    "options": {"a": "选项A", "b": "选项B"}            # dict: value → label
    "options": ["a", "b"]                              # list[str]: value=label
    "options": [{"value": "a", "label": "选项A"}, ...]  # list[dict]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# 支持的字段类型（渲染映射见 plugin_config_card.py）
FIELD_TYPES = ("text", "password", "bool", "select", "number", "textarea", "link")

# number 默认范围（SpinBox 默认 0~2^31-1，与 qfluentwidgets SpinBox 一致）
_NUMBER_DEFAULT_MIN = 0
_NUMBER_DEFAULT_MAX = 2147483647


@dataclass(frozen=True)
class PluginConfigField:
    """单个配置字段声明

    Attributes:
        key: 存储键（插件内唯一；对应 config.json 的顶层键）
        label: 设置面板显示名
        type: text / password / bool / select / number / textarea
        default: 默认值（bool 为 bool；number 为 int；select 为 str(value)；其余为 str）
        env: 环境变量名（非空时环境变量优先级高于存储值）
        placeholder: 输入框占位文本
        description: 字段说明（渲染为卡片 content）
        options: select 专用，有序 (value, label) 列表（value 为存储值）
        min: number 专用，最小值（None=默认 0）
        max: number 专用，最大值（None=默认 2^31-1）
        step: number 专用，步长（默认 1）
        rows: textarea 专用，显示行数（默认 3）
        url: link 专用，点击跳转的外部地址（必填）
    """

    key: str
    label: str
    type: str = "text"
    default: Any = ""
    env: str = ""
    placeholder: str = ""
    description: str = ""
    options: Tuple[Tuple[str, str], ...] = ()
    min: Optional[int] = None
    max: Optional[int] = None
    step: int = 1
    rows: int = 3
    url: str = ""


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


def _parse_select_options(raw: Any) -> Tuple[Tuple[str, str], ...]:
    """解析 select 的 options 声明 → 有序 (value, label) 列表。

    支持三种形态（value 为存储值，label 为显示名）：
      dict[str, str]            {"a": "选项A"}            → [("a", "选项A")]
      list[str]                 ["a", "b"]                → [("a", "a")]
      list[dict[str, str]]      [{"value": "a", "label": "A"}] → [("a", "A")]
    解析失败返回空元组（调用方据此忽略整个 schema）。
    """
    result: List[Tuple[str, str]] = []
    if isinstance(raw, dict):
        for value, label in raw.items():
            result.append((str(value), str(label)))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                result.append((item, item))
            elif isinstance(item, dict) and "value" in item:
                value = str(item.get("value") or "")
                if not value:
                    return ()
                label = str(item.get("label") or value)
                result.append((value, label))
            else:
                return ()
    else:
        return ()
    return tuple(result)


def _parse_optional_int(raw: Any, default: Optional[int]) -> Optional[int]:
    """宽容解析可选整数属性（min/max/step/rows）：非法时回退默认，不炸整个 schema。"""
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(f"[PluginConfig] 可选整数属性非法({raw!r})，使用默认 {default}")
        return default


def parse_config_schema(plugin_name: str, raw: Optional[dict]) -> Optional[PluginConfigSchema]:
    """解析 plugin.json 的 config_schema 对象。

    容错原则：关键属性（key/type/select.options）非法 → 整个 schema 返回 None
    （记 warning，不抛异常）；非关键属性（min/max/step/rows）非法 → 宽容回退默认。
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
        options: Tuple[Tuple[str, str], ...] = ()
        if ftype == "select":
            options = _parse_select_options(item.get("options"))
            if not options:
                logger.warning(f"[PluginConfig] {plugin_name} 字段 {key} select 缺 options，忽略整个 schema")
                return None
        url = ""
        if ftype == "link":
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                logger.warning(f"[PluginConfig] {plugin_name} 字段 {key} link 缺合法 url(http/https)，忽略整个 schema")
                return None
        fields.append(
            PluginConfigField(
                key=key,
                label=str(item.get("label") or key),
                type=ftype,
                default=default,
                env=str(item.get("env") or ""),
                placeholder=str(item.get("placeholder") or ""),
                description=str(item.get("description") or ""),
                options=options,
                min=_parse_optional_int(item.get("min"), None) if ftype == "number" else None,
                max=_parse_optional_int(item.get("max"), None) if ftype == "number" else None,
                step=_parse_optional_int(item.get("step"), 1) if ftype == "number" else 1,
                rows=_parse_optional_int(item.get("rows"), 3) if ftype == "textarea" else 3,
                url=url,
            )
        )
    return PluginConfigSchema(plugin_name=plugin_name, title=title, fields=fields)
