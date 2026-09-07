# -*- coding: utf-8 -*-
"""OpenAI 通用协议家族适配器 — 系统插件实现（id="openai-family"，兜底优先级 1）。

归属：非 gemini 非 deepseek 的所有配置（含 gpt-5 系走 Responses API）。
protocol_flags 与拆分前 openai 适配器逐点等价（判定器共享 _detectors）。
"""

from __future__ import annotations

from typing import Any, Dict

from app.plugins.contracts.model_adapter import ProtocolFlags

# 插件目录名为 kebab-case（含连字符），静态 import 语句无法书写命名空间包路径，
# 共享判定器 _detectors.py 经 importlib 按模块名字符串动态解析（sys.modules 缓存，零重复开销）
from importlib import import_module

det = import_module("plugins.system-model-adapters.model_adapters._detectors")


class OpenAIFamilyAdapter:
    """OpenAI 通用协议适配器（兜底：任何 llm_config 都匹配，优先级 1）"""

    id = "openai-family"

    def matches(self, llm_config: Dict[str, Any]) -> int:
        return 1  # 兜底：非 gemini 非 deepseek 家族时命中（resolve 取最高分）

    def protocol_flags(self, llm_config: Dict[str, Any]) -> ProtocolFlags:
        return ProtocolFlags(
            is_gemini=False,
            requires_reasoning_content=False,
            use_responses_api=det.detect_use_responses(llm_config),
        )


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。

    source 由 loader 的 _RegistryProxy 强制为 "plugin:system"，
    本函数不显式传入，保持与 tools/providers 插件约定一致。
    """
    registry.register(OpenAIFamilyAdapter())
