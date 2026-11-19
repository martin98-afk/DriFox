# -*- coding: utf-8 -*-
"""Google Gemini 服务商插件的请求参数黑名单声明。

transport 侧通用剔除（openai_chat._unsupported_params）的输入契约：
Gemini 原生 generateContent 没有 frequency/presence penalty 概念，OpenAI 兼容层
会把未知字段透传给 protobuf 解析并整请求 400（实测 2026-11-19：
Invalid JSON payload received. Unknown name "frequency_penalty": Cannot find field）。
"""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "plugins" / "system-providers" / "providers" / "gemini.py"


def _load_provider_def():
    spec = importlib.util.spec_from_file_location("gemini_provider_under_test", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured = {}

    class _Registry:
        def register(self, provider_def):
            captured["def"] = provider_def

    module.register(_Registry())
    return captured["def"]


def test_gemini_declares_unsupported_penalty_params():
    """penalty 两参必须声明为不支持，否则请求必被上游整条拒绝。"""
    provider = _load_provider_def()
    assert provider.family == "gemini"
    assert set(provider.capabilities.get("unsupported_params") or ()) == {
        "frequency_penalty",
        "presence_penalty",
    }


def test_gemini_declaration_is_transport_readable():
    """声明落在 capabilities 顶层：get_provider_profile 按 family 聚合时原样透出。"""
    provider = _load_provider_def()
    raw = provider.capabilities["unsupported_params"]
    assert all(isinstance(item, str) for item in raw)
