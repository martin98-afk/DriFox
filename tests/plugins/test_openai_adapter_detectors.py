# -*- coding: utf-8 -*-
"""协议判定器共享模块测试：detect_* 纯函数行为与旧方法逐字等价。

背景：判定器从旧 OpenAIAdapter._method 拆为模块级纯函数（行为零变化），
Phase C 迁入 plugins/system/model_adapters/_detectors.py 供协议家族复用。
"""

import pytest

from plugins.system.model_adapters import _detectors as det
from plugins.system.model_adapters.openai_family import OpenAIFamilyAdapter


# ---------- detect_requires_reasoning ----------


class TestDetectRequiresReasoning:
    @pytest.mark.parametrize(
        "cfg",
        [
            {"API_URL": "https://api.deepseek.com/v1", "模型名称": "deepseek-chat", "思考模式": True},
            {"API_URL": "https://opencode.ai/zen/v1", "模型名称": "deepseek-v4-flash-free", "思考模式": True},
            {"API_URL": "https://opencode.ai/zen/v1", "模型名称": "deepseek-v4", "思考模式": True, "思考等级": "medium"},
        ],
    )
    def test_deepseek_requires(self, cfg):
        """deepseek 官方/中转（模型名兜底）思考模式开启 → True"""
        assert det.detect_requires_reasoning(cfg) is True

    def test_thinking_off_false(self):
        assert det.detect_requires_reasoning({"API_URL": "https://api.deepseek.com", "模型名称": "deepseek-chat", "思考模式": False}) is False

    def test_non_deepseek_false(self):
        """opencode 中转非 deepseek（kimi）思考模式开启 → False"""
        assert det.detect_requires_reasoning({"API_URL": "https://opencode.ai/zen/v1", "模型名称": "kimi-k3", "思考模式": True}) is False

    def test_empty_config_false(self):
        assert det.detect_requires_reasoning({}) is False


# ---------- detect_is_gemini ----------


class TestDetectIsGemini:
    def test_official_gemini(self):
        """官方 Gemini provider → True"""
        assert det.detect_is_gemini({"API_URL": "https://generativelanguage.googleapis.com/v1", "模型名称": "gemini-2.5-pro"}) is True

    def test_model_name_fallback(self):
        """模型名含 gemini（models/gemini-3-flash-preview）→ True"""
        assert det.detect_is_gemini({"API_URL": "", "模型名称": "models/gemini-3-flash-preview"}) is True

    def test_non_gemini_false(self):
        assert det.detect_is_gemini({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"}) is False


# ---------- detect_use_responses ----------


class TestDetectUseResponses:
    def test_gpt5_model(self):
        """模型名以 gpt-5 开头 → True"""
        assert det.detect_use_responses({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-5.6-luna"}) is True

    def test_override_on(self):
        """使用ResponsesAPI 显式 True → True（即使非 gpt-5）"""
        assert det.detect_use_responses({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o", "使用ResponsesAPI": True}) is True

    def test_override_off(self):
        """使用ResponsesAPI 显式 False → False（即使 gpt-5）"""
        assert det.detect_use_responses({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-5.6-luna", "使用ResponsesAPI": False}) is False

    def test_plain_false(self):
        assert det.detect_use_responses({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"}) is False


# ---------- 组合：家族 protocol_flags 与旧方法逐点等价 ----------


def test_protocol_flags_composes_detectors():
    """openai-family.protocol_flags = 三判定器组合（回归：与旧 worker 方法等价）"""
    adapter = OpenAIFamilyAdapter()
    cfg = {"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"}
    flags = adapter.protocol_flags(cfg)
    assert flags.is_gemini == det.detect_is_gemini(cfg)
    assert flags.requires_reasoning_content == det.detect_requires_reasoning(cfg)
    assert flags.use_responses_api == det.detect_use_responses(cfg)


def test_detectors_are_module_level_functions():
    """判定器是模块级纯函数（可独立 import 复用，为协议家族铺路）"""
    assert callable(det.detect_is_gemini)
    assert callable(det.detect_requires_reasoning)
    assert callable(det.detect_use_responses)


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
