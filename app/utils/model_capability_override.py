# -*- coding: utf-8 -*-
"""用户层模型能力覆盖。

查找链（model_capabilities.get_model_capabilities）：用户覆盖 > models.dev > 硬编码。
本模块负责「读覆盖」与「中文键 -> 标准键」翻译。

存哪：llm_model_overrides["服务商名||模型名"] = {中文键: 值}
     复用既有的「按模型持久化参数」机制（MODEL_LEVEL_KEYS 分流），
     key 里的服务商名天然把同名模型按服务商隔离开。

     为什么不存 saved_providers[config_id]：那需要 main_widget 的
     _on_config_applied 为嵌套 dict 加特殊分支；且能力是模型固有属性，
     不该因同一服务商的多个实例（如 "OpenCode Zen #2"）而分裂。

⚠️ 键名必须中文：chat_worker 的兜底逻辑会把匹配标识符正则
   （^[a-zA-Z_][a-zA-Z0-9_]*$）的未知键当 api_param 直发到 API，
   英文键名会让整块能力字典泄漏进 extra_body。
"""

from __future__ import annotations

from typing import Any, Dict

#: 中文覆盖键 -> 标准 caps 键。读写两侧的唯一翻译点。
OVERRIDE_KEY_MAP: Dict[str, str] = {
    "支持思考": "supports_thinking",
    "思考参数": "thinking_param",
    "思考等级可选值": "reasoning_effort_values",
    "思考启用值": "thinking_enable_value",
    "支持多模态": "supports_vision",
    "上下文窗口": "context_limit",
}

#: 值类型为列表的键（逗号分隔字符串要切成列表）
_LIST_VALUE_KEYS = frozenset({"思考等级可选值"})


def _load_model_overrides() -> Dict[str, Any]:
    """读 llm_model_overrides 配置。失败返回空 dict（测试 monkeypatch 本函数隔离）。"""
    try:
        from app.utils.config import Settings

        return dict(Settings.get_instance().llm_model_overrides.value or {})
    except Exception:
        return {}


def _resolve_provider_name(provider_name: str) -> str:
    """把可能是 config_id 的入参规范成 provider_name。

    覆盖值的 key 用 provider_name（不随 apikey 变化），保证改密钥后不丢配置。
    """
    try:
        from app.utils.config import Settings

        saved = Settings.get_instance().llm_saved_providers.value or {}
        info = saved.get(provider_name)
        if isinstance(info, dict) and info.get("provider_name"):
            return str(info["provider_name"])
    except Exception:
        pass
    return provider_name


def _split_values(value: Any) -> list:
    """把「思考等级可选值」归一成列表：支持 list 与逗号分隔字符串。"""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def to_caps_keys(override: Dict[str, Any] | None) -> Dict[str, Any]:
    """中文覆盖键 -> 标准 caps 键。未识别的键丢弃。"""
    if not override:
        return {}
    result: Dict[str, Any] = {}
    for cn_key, value in override.items():
        en_key = OVERRIDE_KEY_MAP.get(cn_key)
        if en_key is None:
            continue
        if cn_key in _LIST_VALUE_KEYS:
            values = _split_values(value)
            if values:
                result[en_key] = values
            continue
        result[en_key] = value
    return result


def get_override(provider_name: str, model_name: str) -> Dict[str, Any]:
    """读某服务商下某模型的中文覆盖值。无则空 dict。

    入参 provider_name 可以是 config_id 或服务商名，内部统一规范化。
    """
    if not provider_name or not model_name:
        return {}
    pname = _resolve_provider_name(provider_name)
    overrides = _load_model_overrides()
    entry = overrides.get(f"{pname}||{model_name}")
    if isinstance(entry, dict):
        return {k: v for k, v in entry.items() if k in OVERRIDE_KEY_MAP}
    return {}


def apply_override(caps: Dict[str, Any], override: Dict[str, Any] | None) -> Dict[str, Any]:
    """把中文覆盖值翻译后叠加到 caps 上。返回新 dict，不改入参。

    显式 False 也会覆盖（用户声明「该模型不支持思考」是有效意图）。
    """
    result = dict(caps or {})
    translated = to_caps_keys(override)
    if not translated:
        return result
    result.update(translated)
    return result
