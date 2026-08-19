# -*- coding: utf-8 -*-
"""OpenAI 标准协议适配器 — 系统插件实现（id="openai"，兜底优先级 1）。

行为零变化原则：判定逻辑从 chat_worker 旧 _requires_reasoning_content /
_is_gemini_model / _use_responses_api 逐字搬运，仅做 self.llm_config → llm_config
的机械变换。
"""

from __future__ import annotations

from typing import Any, Dict

from app.core.provider_profile import detect_provider_family
from app.plugins.contracts.model_adapter import ProtocolFlags


class OpenAIAdapter:
    """OpenAI 标准协议适配器（含 gemini/reasoning/responses 分支检测，兜底优先级 1）"""

    id = "openai"

    def matches(self, llm_config: Dict[str, Any]) -> int:
        return 1  # 兜底：任何 llm_config 都可走本适配器

    def protocol_flags(self, llm_config: Dict[str, Any]) -> ProtocolFlags:
        return ProtocolFlags(
            is_gemini=self._is_gemini(llm_config),
            requires_reasoning_content=self._requires_reasoning(llm_config),
            use_responses_api=self._use_responses(llm_config),
        )

    def _requires_reasoning(self, llm_config: Dict[str, Any]) -> bool:
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

    def _is_gemini(self, llm_config: Dict[str, Any]) -> bool:
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

    def _use_responses(self, llm_config: Dict[str, Any]) -> bool:
        """是否走 Responses API（/v1/responses）。（逐字搬运 _use_responses_api）"""
        try:
            override = llm_config.get("使用ResponsesAPI")
            if override is not None:
                return bool(override)
            model = str(llm_config.get("模型名称", "") or "").lower()
            return model.startswith("gpt-5")
        except Exception:
            return False


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。

    source 由 loader 的 _RegistryProxy 强制为 "plugin:system"，
    本函数不显式传入，保持与 tools/providers 插件约定一致。
    """
    registry.register(OpenAIAdapter())