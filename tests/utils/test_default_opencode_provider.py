# -*- coding: utf-8 -*-
"""测试 Settings 自动注入默认 OpenCode 免费服务商配置。"""

import sys
from pathlib import Path

import pytest

# 确保仓库根目录在 sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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
    # 「模型名称」继承 providers 插件声明的 ProviderDef.default_model（单一数据源），
    # 不再由 config.py 硬编码，故此处断言与插件声明一致而非写死字面量。
    from app.constants import provider_default_config

    declared = provider_default_config("OpenCode Zen") or {}
    assert info["模型名称"] == declared.get("模型名称"), "默认模型名应来自 providers 插件声明"
    assert info["模型名称"], "插件应声明非空 default_model"
    assert info["API_KEY"] == "", "默认配置应为免 key（空 API_KEY，走剥离 Authorization 头调用）"
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


def test_upgrade_legacy_default_model():
    """「模型名称」仍是插件历史默认值时，应升级为当前声明的 default_model。

    场景：插件换了 default_model，但老用户配置还指向已下线的旧默认模型
    （异步刷新只回填「模型列表」，不碰「模型名称」，故必须在此迁移）。
    """
    from app.constants import provider_default_config

    declared = str((provider_default_config("OpenCode Zen") or {}).get("模型名称", ""))
    legacy = "deepseek-v4-flash-free"
    assert declared and declared != legacy, "前置：当前声明值应已异于历史默认值"

    saved = {
        "abc123": {
            "name": "opencode免费模型",
            "API_URL": "https://opencode.ai/zen/v1",
            "API_KEY": "",
            "模型名称": legacy,
        }
    }
    instance = _FakeInstance(saved_providers=saved, injected=False)
    Settings._ensure_default_opencode_provider(instance)

    assert instance.llm_default_opencode_injected.value is True
    assert instance.llm_saved_providers.value["abc123"]["模型名称"] == declared
    assert instance._saved is True, "升级后应持久化"
    # 其他字段不受影响
    assert instance.llm_saved_providers.value["abc123"]["API_KEY"] == ""


def test_keep_user_selected_model():
    """「模型名称」是用户自选（白名单外）时，迁移不得覆盖。"""
    from app.constants import provider_default_config

    declared = str((provider_default_config("OpenCode Zen") or {}).get("模型名称", ""))
    user_choice = "mimo-v2.6-flash-free"
    assert user_choice != declared, "前置：用户选择应异于当前声明值"

    saved = {
        "abc123": {
            "name": "opencode免费模型",
            "API_URL": "https://opencode.ai/zen/v1",
            "API_KEY": "",
            "模型名称": user_choice,
        }
    }
    instance = _FakeInstance(saved_providers=saved, injected=False)
    Settings._ensure_default_opencode_provider(instance)

    assert instance.llm_default_opencode_injected.value is True
    assert instance.llm_saved_providers.value["abc123"]["模型名称"] == user_choice, "用户选择必须保留"
    assert instance._saved is False, "无需变更时不应写盘"


def test_no_upgrade_when_already_current():
    """「模型名称」已等于当前声明值时，不产生写盘。"""
    from app.constants import provider_default_config

    declared = str((provider_default_config("OpenCode Zen") or {}).get("模型名称", ""))
    saved = {
        "abc123": {
            "name": "opencode免费模型",
            "API_URL": "https://opencode.ai/zen/v1",
            "API_KEY": "",
            "模型名称": declared,
        }
    }
    instance = _FakeInstance(saved_providers=saved, injected=False)
    Settings._ensure_default_opencode_provider(instance)

    assert instance.llm_saved_providers.value["abc123"]["模型名称"] == declared
    assert instance._saved is False


def test_create_when_same_url_key_but_different_name():
    """已存在同 (URL, key) 但不同 name 的配置时，仍会创建默认配置。"""
    key = "some-other-key"
    saved = {"abc123": {"provider_name": "OpenCode Zen", "API_URL": "https://opencode.ai/zen/v1", "API_KEY": key}}
    instance = _FakeInstance(saved_providers=saved, injected=False)
    Settings._ensure_default_opencode_provider(instance)

    assert instance.llm_default_opencode_injected.value is True
    assert len(instance.llm_saved_providers.value) == 2
    assert instance._saved is True
    assert _find_opencode_config(instance.llm_saved_providers.value) is not None
