# -*- coding: utf-8 -*-
"""模型能力声明（声明_* 六键）的纯函数读写层（P1-9 L0）。

落盘位置：``LLM.ModelOverrides["<provider>||<model>"]``，与用户调过的模型参数
同一张表（键格式与 ``main_widget._save_model_override`` 一致，provider 为
``ProviderDef.name`` 而非 config_id 哈希）。

本模块只做 dict 变换：不 import Qt、不碰 cfg，便于单测。发送链侧的消费点
是 ``model_capabilities.resolve_model_capabilities`` 的 L0 声明层。
"""

from __future__ import annotations

from typing import Any, Dict

from app.core.modelmeta.model_capabilities import DECLARED_CAPABILITY_KEYS

# 空值等价物：写 None/空串/空列表 = 删除该声明（回到自动链）
_EMPTY_VALUES = (None, "", [])

# 生效层 → 展示文案（对话框与模型列表共用，口径必须一致）
SOURCE_LABELS = {
    "declared": "自定义",
    "caps": "模型能力",
    "family": "服务商族",
    "default": "默认值",
}


def describe_source(source: Any) -> str:
    """生效层（resolve_model_capabilities 的 sources 值）→ 中文文案；未知返回空串"""
    return SOURCE_LABELS.get(str(source or ""), "")


def override_key(provider: str, model: str) -> str:
    """ModelOverrides 条目键：``服务商名||模型名``（与服务商隔离同名模型）"""
    return f"{provider}||{model}"


def _lookup_entry(overrides: Dict[str, Any], provider: str, model: str) -> Dict[str, Any]:
    """取该模型的覆盖条目：先精确键，再兜旧格式（纯模型名）"""
    if not isinstance(overrides, dict):
        return {}
    entry = overrides.get(override_key(provider, model))
    if entry is None:
        entry = overrides.get(model)
    return entry if isinstance(entry, dict) else {}


def read_declarations(overrides: Dict[str, Any], provider: str, model: str) -> Dict[str, Any]:
    """读该模型的六键声明子集（空值键不返回，缺键 = 未声明）"""
    entry = _lookup_entry(overrides, provider, model)
    return {key: entry[key] for key in DECLARED_CAPABILITY_KEYS if key in entry and entry[key] not in _EMPTY_VALUES}


def apply_declarations(
    overrides: Dict[str, Any],
    provider: str,
    model: str,
    declared: Dict[str, Any],
) -> Dict[str, Any]:
    """返回**新** overrides：把 declared 六键写入该模型条目。

    语义是**六键全量覆盖**（弹窗保存的就是完整声明集）：declared 里缺席的
    声明键等于删除（回到自动链），值为 None/空串/空列表同样删除。

    其余规则：
    - 六键全部清空且条目无其他参数 → 删除整个条目（不留空壳）
    - 旧格式（纯模型名键）里的六键声明一并清理：读链有旧键兜底，只清新键会让
      「重置为自动」被旧键复活。旧键里的其他参数（温度等）原样保留。
    """
    data = dict(overrides or {})
    key = override_key(provider, model)
    entry = dict(data.get(key) or {})

    for decl_key in DECLARED_CAPABILITY_KEYS:
        value = (declared or {}).get(decl_key)
        if value in _EMPTY_VALUES:
            entry.pop(decl_key, None)
        else:
            entry[decl_key] = value

    if entry:
        data[key] = entry
    else:
        data.pop(key, None)

    legacy = data.get(model)
    if isinstance(legacy, dict) and any(decl_key in legacy for decl_key in DECLARED_CAPABILITY_KEYS):
        legacy_clean = dict(legacy)
        for decl_key in DECLARED_CAPABILITY_KEYS:
            legacy_clean.pop(decl_key, None)
        if legacy_clean:
            data[model] = legacy_clean
        else:
            data.pop(model, None)

    return data
