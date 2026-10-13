# -*- coding: utf-8 -*-
"""测试 Settings 移除内置「opencode免费模型」服务商配置的迁移。

背景：OpenCode 免费模型已不可用，主程序删除自动注入/自动刷新逻辑后，
存量用户配置里仍留有自动注入的条目，由一次性迁移负责清理。
"""

import sys
from pathlib import Path

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

    def __init__(self, saved_providers=None, selected="", cipher_backup=None):
        self.llm_saved_providers = _FakeConfigItem(saved_providers or {})
        self.llm_selected_model = _FakeConfigItem(selected)
        self._cipher_backup = dict(cipher_backup or {})
        self._saved = False

    def save(self):
        self._saved = True


def test_remove_injected_config():
    """存量自动注入条目应被删除并落盘。"""
    saved = {
        "abc123": {
            "provider_name": "OpenCode Zen",
            "name": "opencode免费模型",
            "API_URL": "https://opencode.ai/zen/v1",
            "API_KEY": "",
        }
    }
    instance = _FakeInstance(saved_providers=saved)
    changed = Settings._migrate_remove_opencode_free_provider(instance)

    assert changed is True
    assert instance._saved is True
    assert instance.llm_saved_providers.value == {}


def test_keep_user_renamed_config():
    """用户改名/自建的 OpenCode 配置不受影响。"""
    saved = {
        "abc123": {
            "provider_name": "OpenCode Zen",
            "name": "我的 OpenCode",
            "API_URL": "https://opencode.ai/zen/v1",
            "API_KEY": "sk-real",
        },
        "def456": {
            "provider_name": "OpenCode Go",
            "name": "opencode免费模型 Go",
            "API_URL": "https://opencode.ai/zen/go/v1",
            "API_KEY": "sk-real2",
        },
    }
    instance = _FakeInstance(saved_providers=saved)
    changed = Settings._migrate_remove_opencode_free_provider(instance)

    assert changed is False
    assert instance._saved is False
    assert len(instance.llm_saved_providers.value) == 2


def test_clear_selected_model_when_pointing_to_removed():
    """llm_selected_model 指向被删条目时置空。"""
    saved = {
        "abc123": {
            "name": "opencode免费模型",
            "API_URL": "https://opencode.ai/zen/v1",
            "API_KEY": "",
        },
        "def456": {
            "name": "DeepSeek",
            "API_URL": "https://api.deepseek.com",
            "API_KEY": "sk-ds",
        },
    }
    instance = _FakeInstance(saved_providers=saved, selected="abc123")
    changed = Settings._migrate_remove_opencode_free_provider(instance)

    assert changed is True
    assert instance.llm_selected_model.value == ""
    assert list(instance.llm_saved_providers.value.keys()) == ["def456"]


def test_keep_selected_model_when_pointing_to_other():
    """llm_selected_model 指向其他配置时不动。"""
    saved = {
        "abc123": {"name": "opencode免费模型", "API_URL": "https://opencode.ai/zen/v1", "API_KEY": ""},
        "def456": {"name": "DeepSeek", "API_URL": "https://api.deepseek.com", "API_KEY": "sk-ds"},
    }
    instance = _FakeInstance(saved_providers=saved, selected="def456")
    Settings._migrate_remove_opencode_free_provider(instance)

    assert instance.llm_selected_model.value == "def456"
    assert list(instance.llm_saved_providers.value.keys()) == ["def456"]


def test_clean_orphan_cipher_backup():
    """密码模式下同步清理被删条目的孤儿密文。"""
    saved = {
        "abc123": {"name": "opencode免费模型", "API_URL": "https://opencode.ai/zen/v1", "API_KEY": "ENC:xxx"},
        "def456": {"name": "DeepSeek", "API_URL": "https://api.deepseek.com", "API_KEY": "ENC:yyy"},
    }
    instance = _FakeInstance(
        saved_providers=saved,
        cipher_backup={"abc123": "ENC:xxx", "def456": "ENC:yyy"},
    )
    changed = Settings._migrate_remove_opencode_free_provider(instance)

    assert changed is True
    assert instance._cipher_backup == {"def456": "ENC:yyy"}, "孤儿密文应被清理，其他密文保留"


def test_noop_when_empty_or_invalid():
    """空配置/非法形态：不动作不写盘。"""
    for saved in (None, {}, [], "not-a-dict"):
        instance = _FakeInstance(saved_providers=saved)
        assert Settings._migrate_remove_opencode_free_provider(instance) is False
        assert instance._saved is False


def test_idempotent():
    """跑两次：第二次无变化不写盘。"""
    saved = {"abc123": {"name": "opencode免费模型", "API_URL": "https://opencode.ai/zen/v1", "API_KEY": ""}}
    instance = _FakeInstance(saved_providers=saved)

    assert Settings._migrate_remove_opencode_free_provider(instance) is True
    instance._saved = False
    assert Settings._migrate_remove_opencode_free_provider(instance) is False
    assert instance._saved is False


def test_registered_in_migration_table():
    """迁移必须注册进 _MIGRATIONS（启动链自动执行）。"""
    from app.utils import config as config_mod

    assert config_mod._migrate_remove_opencode_free_provider_entry in config_mod._MIGRATIONS
    assert config_mod._MIGRATIONS[0] is config_mod._migrate_saved_providers_entry, "顺序：先 config_id 迁移再清理"
