# -*- coding: utf-8 -*-
"""openai_chat transport 的 OpenAI 系参数形态判定（P1-22）。

契约归属：判定逻辑是 transport 插件的私有实现（模型契约归插件），主程序不
持有同名符号。本文件直接 import 插件模块的私有函数测试，防判定漂移。

背景：gpt-5 起 chat/completions 弃用 max_tokens，只认 max_completion_tokens；
推理模型还拒绝 temperature / top_p（携带即 400 unknown_parameter）。中转站
私有 ID（如 OAIPRO 的 gpt-6.1-sol）在 models.dev 无覆盖、caps 链落空，只能
按模型名版本号判定。
"""

import importlib.util
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent.parent / "plugins" / "system-transports"
_MODULE_PATH = _PLUGIN / "transports" / "openai_chat.py"


def _load():
    spec = importlib.util.spec_from_file_location("openai_chat_param_shape_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


# ---------- 版本号形态 ----------


@pytest.mark.parametrize(
    "model, expected",
    [
        ("gpt-6.1-sol", True),
        ("gpt-6-astra", True),
        ("gpt-6", True),
        ("gpt-5.6-terra", True),
        ("gpt-5", True),
        ("gpt-5-nano", True),
        ("gpt-4o", False),
        ("gpt-4.1", False),
        ("gpt-3.5-turbo", False),
        ("o1-preview", True),
        ("o3-mini", True),
    ],
)
def test_uses_max_completion_tokens(mod, model, expected):
    assert mod._uses_max_completion_tokens(model) is expected


@pytest.mark.parametrize(
    "model",
    ["chat-latest", "claude-sonnet-5", "glm-5.3-flash", "deepseek-v4.1", "MiniMax-M3", "", "gpt-"],
)
def test_non_openai_models_keep_legacy_key(mod, model):
    """非 gpt-/o 系形态不做判定，保持旧 max_tokens（不能误伤其他厂商）。"""
    assert mod._uses_max_completion_tokens(model) is False
    assert mod._max_tokens_param_key(model) == "max_tokens"


def test_max_tokens_param_key_mapping(mod):
    assert mod._max_tokens_param_key("gpt-6.1-sol") == "max_completion_tokens"
    assert mod._max_tokens_param_key("gpt-4o") == "max_tokens"


# ---------- 采样参数拒绝集 ----------


def test_rejects_temperature_and_top_p_on_gpt6(mod):
    assert mod._rejects_sampling_param("gpt-6.1-sol", "temperature") is True
    assert mod._rejects_sampling_param("gpt-6.1-sol", "top_p") is True


def test_penalties_not_rejected_on_gpt6(mod):
    """frequency_penalty / presence_penalty 实测正常受理，不得误伤。"""
    assert mod._rejects_sampling_param("gpt-6.1-sol", "frequency_penalty") is False
    assert mod._rejects_sampling_param("gpt-6.1-sol", "presence_penalty") is False


def test_old_models_accept_sampling(mod):
    assert mod._rejects_sampling_param("gpt-4o", "temperature") is False
    assert mod._rejects_sampling_param("gpt-4o", "top_p") is False


def test_o_series_rejects_sampling(mod):
    assert mod._rejects_sampling_param("o1-preview", "temperature") is True
    assert mod._rejects_sampling_param("o3-mini", "top_p") is True


# ---------- 思考档位 ----------


def test_effort_values_use_declared_when_present(mod):
    declared = ["high", "max"]
    assert mod._effort_values_for("gpt-6.1-sol", declared) == declared


def test_effort_values_fall_back_for_gpt6(mod):
    """中转站无 models.dev 数据时回退内置档位，避免 max/minimal 直发 400。"""
    assert mod._effort_values_for("gpt-6.1-sol", None) == mod._OPENAI_REASONING_EFFORTS
    assert "max" not in mod._OPENAI_REASONING_EFFORTS
    assert "minimal" not in mod._OPENAI_REASONING_EFFORTS


def test_effort_values_empty_for_non_reasoning_model(mod):
    assert mod._effort_values_for("gpt-4o", None) == []
