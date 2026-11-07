# -*- coding: utf-8 -*-
"""思考增量多厂商字段归一：兼容函数与子智能体聚合共用一套识别规则。

取证来源（2025-2026 官方文档 / 网关实测）：
- reasoning_content：DeepSeek、DashScope Qwen3、GLM、Kimi、硅基流动、vLLM 旧版
- reasoning：OpenRouter、vLLM 新版（旧 reasoning_content 已改名）、兼容网关
- reasoning_details：OpenRouter / Vercel AI Gateway 结构化数组（text / summary）
- thinking：Anthropic thinking_delta 经 OpenAI 兼容网关透传的常见别名

明确不识别：reasoning.encrypted 密文、thought_signature 签名（无可读文本）。
"""

from types import SimpleNamespace

import pytest

from app.core.conversation.message_content import extract_reasoning_delta


@pytest.mark.parametrize(
    "obj, expected",
    [
        ({"reasoning_content": "A"}, "A"),
        (SimpleNamespace(reasoning_content="A", reasoning=None, reasoning_details=None), "A"),
        ({"reasoning": "B"}, "B"),
        (SimpleNamespace(reasoning="B", reasoning_content=None), "B"),
        # OpenRouter 结构化数组：text / summary 两类明文
        ({"reasoning_details": [{"type": "reasoning.text", "text": "C"}]}, "C"),
        ({"reasoning_details": [{"type": "reasoning.summary", "summary": "D"}]}, "D"),
        ({"reasoning_details": [{"type": "reasoning.text", "text": "C"}, {"type": "reasoning.summary", "summary": "D"}]}, "CD"),
        # 网关把 reasoning 归一成对象而非标量
        ({"reasoning": {"text": "E"}}, "E"),
        ({"reasoning": {"content": "F"}}, "F"),
        ({"reasoning": {"summary": "G"}}, "G"),
        # details 元素为 SDK 对象或裸字符串
        (SimpleNamespace(reasoning_details=[SimpleNamespace(text="H", summary=None)]), "H"),
        ({"reasoning_details": ["I"]}, "I"),
        # Anthropic thinking_delta 经兼容层的别名
        ({"thinking": "J"}, "J"),
        (SimpleNamespace(thinking="J"), "J"),
    ],
)
def test_vendor_reasoning_shapes(obj, expected):
    assert extract_reasoning_delta(obj) == expected


@pytest.mark.parametrize(
    "obj",
    [
        {"reasoning_details": [{"type": "reasoning.encrypted", "data": "cipher"}]},
        {"reasoning_details": [{"type": "reasoning.text", "signature": "sig-only"}]},
        {"reasoning": {"signature": "sig-only"}},
        {"thought_signature": "sig-only"},
        {"reasoning_details": []},
        {"reasoning": None, "reasoning_content": None},
        {},
        None,
    ],
)
def test_non_displayable_shapes_return_empty(obj):
    """密文 / 签名 / 空形状不得塞进思考框"""
    assert extract_reasoning_delta(obj) == ""


@pytest.mark.parametrize(
    "obj, expected",
    [
        # 网关同时下发同义字段：只取其一，避免思考内容重复计入
        ({"reasoning": "X", "reasoning_details": [{"text": "X"}]}, "X"),
        ({"reasoning_content": "Y", "reasoning": "Y"}, "Y"),
        ({"thinking": "Z", "reasoning_content": "Z"}, "Z"),
    ],
)
def test_no_duplicate_when_fields_coexist(obj, expected):
    assert extract_reasoning_delta(obj) == expected
