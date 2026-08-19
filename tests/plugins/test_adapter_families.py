# -*- coding: utf-8 -*-
"""协议家族适配器测试：openai-family / gemini-family / deepseek-family。

等价矩阵：拆分后 resolve 选中家族的 flags == 拆分前 openai 适配器 flags
（= 三判定器组合，逐字搬运）；优先级 deepseek 3 > gemini 2 > openai 1。
"""

import pytest

from app.plugins.contracts.model_adapter import ProtocolFlags
from plugins.system.model_adapters import _detectors as det

_CASES = [
    # (llm_config, 预期选中家族, 预期 flags 三判定)
    ({"API_URL": "https://api.deepseek.com/v1", "模型名称": "deepseek-chat", "思考模式": True}, "deepseek-family", (False, True, False)),
    ({"API_URL": "https://opencode.ai/zen/v1", "模型名称": "deepseek-v4-flash-free", "思考模式": True}, "deepseek-family", (False, True, False)),
    ({"API_URL": "https://opencode.ai/zen/v1", "模型名称": "deepseek-v4", "思考模式": True, "思考等级": "medium"}, "deepseek-family", (False, True, False)),
    ({"API_URL": "https://api.deepseek.com", "模型名称": "deepseek-chat", "思考模式": False}, "openai-family", (False, False, False)),
    ({"API_URL": "https://generativelanguage.googleapis.com/v1", "模型名称": "gemini-2.5-pro"}, "gemini-family", (True, False, False)),
    ({"API_URL": "", "模型名称": "models/gemini-3-flash-preview"}, "gemini-family", (True, False, False)),
    ({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-5.6-luna"}, "openai-family", (False, False, True)),
    ({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"}, "openai-family", (False, False, False)),
    ({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o", "使用ResponsesAPI": True}, "openai-family", (False, False, True)),
    ({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-5.6-luna", "使用ResponsesAPI": False}, "openai-family", (False, False, False)),
    ({"API_URL": "https://api.siliconflow.cn/v1", "模型名称": "Qwen/Qwen3-Coder", "思考模式": True}, "openai-family", (False, False, False)),
    ({"API_URL": "https://opencode.ai/zen/v1", "模型名称": "kimi-k3", "思考模式": True}, "openai-family", (False, False, False)),
    # 交叉边界：deepseek 配置带 responses override（多判定命中，以家族为归属）
    ({"API_URL": "https://api.deepseek.com/v1", "模型名称": "deepseek-chat", "思考模式": True, "使用ResponsesAPI": True}, "deepseek-family", (False, True, True)),
]


@pytest.fixture()
def fresh_registry(monkeypatch):
    """每用例独立 registry + warmup 注册三家族（系统插件）"""
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    reg = ModelAdapterRegistry()
    monkeypatch.setattr(ModelAdapterRegistry, "get_instance", staticmethod(lambda: reg))
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

    warmup_runtime_components()
    return reg


@pytest.mark.parametrize("cfg,family_id,expected", _CASES)
def test_family_equivalence_matrix(fresh_registry, cfg, family_id, expected):
    """等价矩阵：选中家族 id + flags 与拆分前逐点等价"""
    adapter = fresh_registry.resolve(cfg)
    assert adapter is not None
    assert adapter.id == family_id, f"{cfg} 应命中 {family_id}"
    flags = adapter.protocol_flags(cfg)
    assert (
        flags.is_gemini,
        flags.requires_reasoning_content,
        flags.use_responses_api,
    ) == expected, f"flags 不等价: {cfg}"


def test_family_priorities(fresh_registry):
    """matches 优先级：deepseek 3 > gemini 2 > openai 1（兜底）"""
    from plugins.system.model_adapters.deepseek_family import DeepSeekFamilyAdapter
    from plugins.system.model_adapters.gemini_family import GeminiFamilyAdapter
    from plugins.system.model_adapters.openai_family import OpenAIFamilyAdapter

    deepseek_cfg = {"API_URL": "https://api.deepseek.com/v1", "模型名称": "deepseek-chat", "思考模式": True}
    gemini_cfg = {"API_URL": "https://generativelanguage.googleapis.com/v1", "模型名称": "gemini-2.5-pro"}
    plain_cfg = {"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"}

    assert DeepSeekFamilyAdapter().matches(deepseek_cfg) == 3
    assert GeminiFamilyAdapter().matches(deepseek_cfg) == 0  # 家族正交：deepseek 不命中 gemini
    assert GeminiFamilyAdapter().matches(gemini_cfg) == 2
    assert OpenAIFamilyAdapter().matches(plain_cfg) == 1  # 恒兜底
    assert DeepSeekFamilyAdapter().matches(plain_cfg) == 0

    # resolve 选最高分
    assert fresh_registry.resolve(deepseek_cfg).id == "deepseek-family"
    assert fresh_registry.resolve(gemini_cfg).id == "gemini-family"
    assert fresh_registry.resolve(plain_cfg).id == "openai-family"


def test_shared_detectors_still_exposed():
    """判定器迁移到 _detectors.py（共享模块，loader 跳过 _ 前缀不当作插件）"""
    assert callable(det.detect_is_gemini)
    assert callable(det.detect_requires_reasoning)
    assert callable(det.detect_use_responses)
    # 逻辑与拆分前逐字等价（deepseek 中转 case）
    assert det.detect_requires_reasoning({"API_URL": "https://opencode.ai/zen/v1", "模型名称": "deepseek-v4", "思考模式": True}) is True


def test_no_legacy_openai_module():
    """旧单适配器 openai.py 已删除（无残留 import）"""
    import importlib.util

    spec = importlib.util.find_spec("plugins.system.model_adapters.openai")
    assert spec is None


def test_serializer_id_default_openai():
    """家族 adapter 的 serializer_id 保持默认 openai（暂无专属序列化器）"""
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry
    from plugins.system.model_adapters.openai_family import OpenAIFamilyAdapter

    flags = OpenAIFamilyAdapter().protocol_flags({"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"})
    assert flags.serializer_id == "openai"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
