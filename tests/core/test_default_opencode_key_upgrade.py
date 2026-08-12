# -*- coding: utf-8 -*-
"""内置 OpenCode 共享 key 迁移为免 key 配置的回归测试

背景：
- 旧版本注入内置共享 key（OPENCODE_SHARED_API_KEY）作为默认"opencode免费模型"配置；
  实测该共享 key 已被上游吊销（401），且 OpenCode Zen 支持免 key 匿名调用
  （空 key 时剥离 Authorization 头）。
- 新逻辑：默认配置不再带任何 key（API_KEY=""），启动时把历史内置 key 配置
  迁移为免 key 配置（config_id 重算，已选模型映射迁移）。

覆盖：
- 历史内置 key（含 legacy key）→ 迁移为免 key，config_id 重算，已选模型映射迁移
- 用户自定义 key → 保持不动
- 无同名配置 → 注入免 key 配置
- 新 config_id 与其他配置冲突 → 跳过迁移，不覆盖用户数据
"""

from types import SimpleNamespace

from app.core.provider_profile import compute_provider_config_id
from app.utils.config import Settings, _LEGACY_OPENCODE_BUILTIN_KEYS

CONFIG_NAME = "opencode免费模型"
API_URL = "https://opencode.ai/zen/v1"
OLD_KEY = next(iter(_LEGACY_OPENCODE_BUILTIN_KEYS))


class FakeSettings:
    """轻量替身：只暴露 _ensure_default_opencode_provider 用到的属性"""

    def __init__(self, saved_providers=None, selected=""):
        self.llm_saved_providers = SimpleNamespace(value=dict(saved_providers or {}))
        self.llm_selected_model = SimpleNamespace(value=selected)
        self.llm_default_opencode_injected = SimpleNamespace(value=False)
        self.save_called = False

    def save(self):
        self.save_called = True


def _make_default_entry(api_key: str) -> dict:
    """构造与内置注入格式一致的条目"""
    info = {
        "provider_name": "OpenCode Zen",
        "name": CONFIG_NAME,
        "API_URL": API_URL,
        "API_KEY": api_key,
        "模型名称": "deepseek-v4-flash-free",
        "温度": 0.7,
        "最大Token": 200000,
        "认证方式": "bearer",
    }
    info["config_id"] = compute_provider_config_id(info)
    return info


class TestLegacyKeyMigration:
    """历史内置共享 key 自动迁移为免 key"""

    def test_legacy_key_migrated_and_selected_updated(self):
        """内置 key → 空 key；config_id 重算；llm_selected_model 同步迁移"""
        old_entry = _make_default_entry(OLD_KEY)
        old_cid = old_entry["config_id"]
        fake = FakeSettings({old_cid: old_entry}, selected=old_cid)

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        assert old_cid not in saved, "旧条目应被移除"
        assert fake.save_called, "迁移后应触发保存"

        new_cid = compute_provider_config_id({**old_entry, "API_KEY": ""})
        assert new_cid in saved, "免 key 条目应存在"
        assert saved[new_cid]["API_KEY"] == ""
        assert saved[new_cid]["name"] == CONFIG_NAME
        # 其他用户字段保留
        assert saved[new_cid]["模型名称"] == "deepseek-v4-flash-free"
        # 已选模型迁移
        assert fake.llm_selected_model.value == new_cid

    def test_migration_keeps_user_customized_fields(self):
        """迁移只清空 key，保留用户改过的模型名称"""
        old_entry = _make_default_entry(OLD_KEY)
        old_entry["模型名称"] = "my-custom-model"
        old_cid = old_entry["config_id"]
        fake = FakeSettings({old_cid: old_entry})

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        new_cid = compute_provider_config_id({**old_entry, "API_KEY": ""})
        assert saved[new_cid]["模型名称"] == "my-custom-model"
        assert saved[new_cid]["API_KEY"] == ""


class TestNoMigration:
    """不该迁移的场景"""

    def test_empty_key_untouched(self):
        """已是免 key 配置 → 不动（config_id 不变，不触发保存）"""
        entry = _make_default_entry("")
        cid = entry["config_id"]
        fake = FakeSettings({cid: entry}, selected=cid)

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        assert cid in saved and len(saved) == 1
        assert saved[cid]["API_KEY"] == ""
        assert fake.llm_selected_model.value == cid
        assert not fake.save_called, "无变化不应触发保存"

    def test_user_custom_key_untouched(self):
        """用户自定义 key（非历史内置 key）→ 不动"""
        custom_key = "user-custom-key-abc123"
        entry = _make_default_entry(custom_key)
        cid = entry["config_id"]
        fake = FakeSettings({cid: entry}, selected=cid)

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        assert saved[cid]["API_KEY"] == custom_key
        assert not fake.save_called

    def test_conflict_skips_migration(self):
        """新 config_id 撞到已有条目 → 跳过迁移，保留旧条目"""
        old_entry = _make_default_entry(OLD_KEY)
        old_cid = old_entry["config_id"]
        new_entry = _make_default_entry("")
        new_cid = new_entry["config_id"]
        # 用户已手动添加过免 key 条目
        fake = FakeSettings({old_cid: old_entry, new_cid: new_entry}, selected=old_cid)

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        assert old_cid in saved, "冲突时旧条目应保留"
        assert saved[old_cid]["API_KEY"] == OLD_KEY
        assert new_cid in saved
        assert not fake.save_called, "冲突跳过不应触发保存"


class TestInject:
    """首次注入"""

    def test_inject_when_missing(self):
        """无同名配置 → 注入免 key 配置"""
        fake = FakeSettings({})

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        assert len(saved) == 1
        cid = next(iter(saved))
        assert saved[cid]["name"] == CONFIG_NAME
        assert saved[cid]["API_KEY"] == ""
        assert fake.llm_default_opencode_injected.value is True
        assert fake.save_called

    def test_inject_when_deleted_recovers(self):
        """用户删除后下次启动自动恢复（同名配置不存在 → 重新注入）"""
        other_cid = "deadbeef"
        other_entry = {
            "provider_name": "DeepSeek",
            "name": "我的DeepSeek",
            "API_URL": "https://api.deepseek.com/v1",
            "API_KEY": "other-key-123",
            "模型名称": "deepseek-chat",
        }
        fake = FakeSettings({other_cid: other_entry})

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        names = [v.get("name") for v in saved.values()]
        assert CONFIG_NAME in names, "应重新注入默认配置"
        assert "我的DeepSeek" in names, "用户其他配置不受影响"
