# -*- coding: utf-8 -*-
"""Gemini 协议家族适配器 — 系统插件实现（id="gemini-family"，优先级 2）。

归属：Gemini 模型（detect_is_gemini 命中——官方 provider 或模型名含 gemini）。
与拆分前 openai 适配器逐点等价：is_gemini=True（家族保证），
requires_reasoning_content / use_responses_api 仍走共享判定器（交叉边界可被命中）。
"""

from __future__ import annotations

from typing import Any, Dict

from app.plugins.contracts.model_adapter import ProtocolFlags

# 插件目录名为 kebab-case（含连字符），静态 import 语句无法书写命名空间包路径，
# 共享判定器 _detectors.py 经 importlib 按模块名字符串动态解析（sys.modules 缓存，零重复开销）
from importlib import import_module

det = import_module("plugins.system-model-adapters.model_adapters._detectors")


class GeminiFamilyAdapter:
    """Gemini 协议适配器（thought_signature 兜底注入，优先级 2）"""

    id = "gemini-family"

    def matches(self, llm_config: Dict[str, Any]) -> int:
        return 2 if det.detect_is_gemini(llm_config) else 0

    def protocol_flags(self, llm_config: Dict[str, Any]) -> ProtocolFlags:
        return ProtocolFlags(
            is_gemini=True,
            requires_reasoning_content=det.detect_requires_reasoning(llm_config),
            use_responses_api=det.detect_use_responses(llm_config),
        )


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。

    source 由 loader 的 _RegistryProxy 强制为 "plugin:system"，
    本函数不显式传入，保持与 tools/providers 插件约定一致。
    """
    registry.register(GeminiFamilyAdapter())
