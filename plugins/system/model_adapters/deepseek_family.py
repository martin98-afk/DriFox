# -*- coding: utf-8 -*-
"""DeepSeek 协议家族适配器 — 系统插件实现（id="deepseek-family"，优先级 3）。

归属：deepseek 系模型 thinking mode（detect_requires_reasoning 命中——官方/中转，
模型名以 deepseek 开头兜底）。与拆分前 openai 适配器逐点等价：
requires_reasoning_content=True（家族保证），is_gemini / use_responses_api 仍走
共享判定器（交叉边界可被命中）。
"""

from __future__ import annotations

from typing import Any, Dict

from app.plugins.contracts.model_adapter import ProtocolFlags
from plugins.system.model_adapters import _detectors as det


class DeepSeekFamilyAdapter:
    """DeepSeek 协议适配器（thinking mode 需 reasoning_content 回传，优先级 3）"""

    id = "deepseek-family"

    def matches(self, llm_config: Dict[str, Any]) -> int:
        return 3 if det.detect_requires_reasoning(llm_config) else 0

    def protocol_flags(self, llm_config: Dict[str, Any]) -> ProtocolFlags:
        return ProtocolFlags(
            is_gemini=det.detect_is_gemini(llm_config),
            requires_reasoning_content=True,
            use_responses_api=det.detect_use_responses(llm_config),
        )


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。

    source 由 loader 的 _RegistryProxy 强制为 "plugin:system"，
    本函数不显式传入，保持与 tools/providers 插件约定一致。
    """
    registry.register(DeepSeekFamilyAdapter())
