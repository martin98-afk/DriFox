# -*- coding: utf-8 -*-
"""测试 Settings 自动注入默认 OpenCode 免费服务商配置。"""

import sys
from pathlib import Path

import pytest

# 确保仓库根目录在 sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.constants import FREE_PROVIDERS, OPENCODE_SHARED_API_KEY
from app.utils.config import Settings


class _FakeConfigItem:
    """模拟 ConfigItem，用于不依赖 Qt 的单元测试。"""

    def __init__(self, value):
        self.value = value


class _FakeInstance:
    """模拟 Settings 实例，只包含本测试关心的字段。"""

    def __init__(self, saved_providers=None, injected=False):
        self.llm_saved_providers = _FakeConfigItem(saved_providers or {})
        self.llm_default_opencode_injected = _FakeConfigItem(injected)
        self._saved = False

    def save(self):
        self._saved = True


def _find_opencode_config(saved_providers):
    for info in saved_providers.values():
        if info.get("name") == "opencode免费模型":
            return info
    return None


def test_inject_when_empty():
    """空配置时应自动注入 OpenCode 默认配置。

    注意（2026-07-18 測試體系整改）：
        ``Settings._ensure_default_opencode_provider()`` 当前实现**故意不写**
        ``"模型列表"`` 字段——空列表会让模型选择器显示为空。
        不写此键时回退到 ``merged_provider_models``（硬编码 + models.dev +
        异步刷新），异步刷新完成后再写入实际列表。
        因此本测试断言 ``info["模型列表"]`` *不* 出现，而非枚举具体值。
    """
    instance = _FakeInstance(saved_providers={}, injected=False)
    Settings._ensure_default_opencode_provider(instance)

    assert instance.llm_default_opencode_injected.value is True
    assert instance._saved is True

    info = _find_opencode_config(instance.llm_saved_providers.value)
    assert info is not None
    assert info["provider_name"] == "OpenCode Zen"
    assert info["API_URL"] == "https://opencode.ai/zen/v1"
    assert info["模型名称"] == "deepseek-v4-flash-free"
    assert info["API_KEY"] == OPENCODE_SHARED_API_KEY
    # 模型列表字段被故意省略（由异步刷新回填，见上方说明）
    assert "模型列表" not in info, f"info 应不含「模型列表」键，实际为 {list(info.get('模型列表', []))!r}"
    assert "config_id" in info


def test_recreate_after_deleted():
    """flag 已置位但配置被删除后，应重新注入。"""
    instance = _FakeInstance(saved_providers={}, injected=True)
    Settings._ensure_default_opencode_provider(instance)

    info = _find_opencode_config(instance.llm_saved_providers.value)
    assert info is not None
    assert instance._saved is True


def test_skip_when_same_name_exists():
    """已存在同名配置时，只置 flag 不注入。"""
    saved = {"abc123": {"name": "opencode免费模型", "API_URL": "https://opencode.ai/zen/v1", "API_KEY": "other"}}
    instance = _FakeInstance(saved_providers=saved, injected=False)
    Settings._ensure_default_opencode_provider(instance)

    assert instance.llm_default_opencode_injected.value is True
    # 保持原有配置，不应被覆盖
    assert instance.llm_saved_providers.value["abc123"]["API_KEY"] == "other"
    assert instance._saved is False


def test_create_when_same_url_key_but_different_name():
    """已存在同 (URL, key) 但不同 name 的配置时，仍会创建默认配置。"""
    key = OPENCODE_SHARED_API_KEY
    saved = {"abc123": {"provider_name": "OpenCode Zen", "API_URL": "https://opencode.ai/zen/v1", "API_KEY": key}}
    instance = _FakeInstance(saved_providers=saved, injected=False)
    Settings._ensure_default_opencode_provider(instance)

    assert instance.llm_default_opencode_injected.value is True
    assert len(instance.llm_saved_providers.value) == 2
    assert instance._saved is True
    assert _find_opencode_config(instance.llm_saved_providers.value) is not None
