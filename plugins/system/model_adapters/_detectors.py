# -*- coding: utf-8 -*-
"""协议判定器共享模块（非插件，loader 跳过 _ 前缀文件）。

判定逻辑从旧单适配器 plugins/system/model_adapters/openai.py 逐字搬运，
供 openai-family / gemini-family / deepseek-family 三家族复用（行为零变化）。
"""

from __future__ import annotations

from typing import Any, Dict

from app.core.provider_profile import detect_provider_family


def detect_requires_reasoning(llm_config: Dict[str, Any]) -> bool:
    """thinking 模式下，兼容要求 tool-call assistant 保留 reasoning_content 的 provider。

    （逐字搬运 chat_worker._requires_reasoning_content 注释与逻辑）
    deepseek 系模型（含 opencode.ai 等中转平台承载的 deepseek-v4 系列）在
    thinking mode 下要求 tool_calls assistant 消息必须携带 reasoning_content
    字段（可为空串），否则上游 Console 报 400。
    """
    if llm_config.get("思考模式") is not True:
        return False
    family = detect_provider_family(llm_config)
    if family == "deepseek":
        return True
    # opencode 等中转平台承载 deepseek 系模型时（模型名以 deepseek 开头），
    # 上游协议与官方 Console 一致，同样需要 reasoning_content 回传
    model = str(llm_config.get("模型名称", "") or "").lower()
    return model.startswith("deepseek")


def detect_is_gemini(llm_config: Dict[str, Any]) -> bool:
    """是否为 Gemini 模型（需特殊处理 thought_signature）。（逐字搬运 _is_gemini_model）"""
    try:
        if detect_provider_family(llm_config) == "gemini":
            return True
    except Exception:
        pass
    # 兜底：模型名含 gemini（如 models/gemini-3-flash-preview 的 startswith 判断会漏）
    try:
        model = str((llm_config or {}).get("模型名称", "") or "").lower()
        if "gemini" in model:
            return True
    except Exception:
        pass
    return False


def detect_use_responses(llm_config: Dict[str, Any]) -> bool:
    """是否走 Responses API（/v1/responses）。（逐字搬运 _use_responses_api）"""
    try:
        override = llm_config.get("使用ResponsesAPI")
        if override is not None:
            return bool(override)
        model = str(llm_config.get("模型名称", "") or "").lower()
        return model.startswith("gpt-5")
    except Exception:
        return False
