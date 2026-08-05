# -*- coding: utf-8 -*-
"""内置 OpenCode 免费 key 自动升级回归测试

覆盖：
- 旧内置 key → 自动升级为新 key，config_id 重算，已选模型映射迁移
- 当前 key / 用户自定义 key → 保持不动
- 无同名配置 → 正常注入
- 新 config_id 与其他配置冲突 → 跳过升级，不覆盖用户数据
"""

from types import SimpleNamespace

from app.constants import OPENCODE_LEGACY_KEYS, OPENCODE_SHARED_API_KEY
from app.core.provider_profile import compute_provider_config_id
from app.utils.config import Settings

CONFIG_NAME = "opencode免费模型"
API_URL = "https://opencode.ai/zen/v1"
OLD_KEY = next(iter(OPENCODE_LEGACY_KEYS))
NEW_KEY = OPENCODE_SHARED_API_KEY


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


class TestLegacyKeyUpgrade:
    """旧内置 key 自动升级"""

    def test_legacy_key_upgraded_and_selected_migrated(self):
        """旧 key → 新 key；config_id 重算；llm_selected_model 同步迁移"""
        old_entry = _make_default_entry(OLD_KEY)
        old_cid = old_entry["config_id"]
        fake = FakeSettings({old_cid: old_entry}, selected=old_cid)

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        assert old_cid not in saved, "旧条目应被移除"
        assert fake.save_called, "升级后应触发保存"

        new_cid = compute_provider_config_id({**old_entry, "API_KEY": NEW_KEY})
        assert new_cid in saved, "新 key 条目应存在"
        assert saved[new_cid]["API_KEY"] == NEW_KEY
        assert saved[new_cid]["name"] == CONFIG_NAME
        # 其他用户字段保留
        assert saved[new_cid]["模型名称"] == "deepseek-v4-flash-free"
        # 已选模型迁移
        assert fake.llm_selected_model.value == new_cid

    def test_upgrade_keeps_user_customized_fields(self):
        """升级只换 key，保留用户改过的模型名称"""
        old_entry = _make_default_entry(OLD_KEY)
        old_entry["模型名称"] = "my-custom-model"
        old_cid = old_entry["config_id"]
        fake = FakeSettings({old_cid: old_entry})

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        new_cid = compute_provider_config_id({**old_entry, "API_KEY": NEW_KEY})
        assert saved[new_cid]["模型名称"] == "my-custom-model"
        assert saved[new_cid]["API_KEY"] == NEW_KEY


class TestNoUpgrade:
    """不该升级的场景"""

    def test_current_key_untouched(self):
        """key 已是当前常量 → 不动（config_id 不变）"""
        entry = _make_default_entry(NEW_KEY)
        cid = entry["config_id"]
        fake = FakeSettings({cid: entry}, selected=cid)

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        assert cid in saved and len(saved) == 1
        assert saved[cid]["API_KEY"] == NEW_KEY
        assert fake.llm_selected_model.value == cid
        assert not fake.save_called, "无变化不应触发保存"

    def test_user_custom_key_untouched(self):
        """用户自定义 key（非内置历史 key）→ 不动"""
        custom_key = "user-custom-key-abc123"
        entry = _make_default_entry(custom_key)
        cid = entry["config_id"]
        fake = FakeSettings({cid: entry}, selected=cid)

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        assert saved[cid]["API_KEY"] == custom_key
        assert not fake.save_called

    def test_conflict_skips_upgrade(self):
        """新 config_id 撞到已有条目 → 跳过升级，保留旧条目"""
        old_entry = _make_default_entry(OLD_KEY)
        old_cid = old_entry["config_id"]
        new_entry = _make_default_entry(NEW_KEY)
        new_cid = new_entry["config_id"]
        # 用户已手动添加过新 key 的条目
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
        """无同名配置 → 用当前常量注入新配置"""
        fake = FakeSettings({})

        Settings._ensure_default_opencode_provider(fake)

        saved = fake.llm_saved_providers.value
        assert len(saved) == 1
        cid = next(iter(saved))
        assert saved[cid]["name"] == CONFIG_NAME
        assert saved[cid]["API_KEY"] == NEW_KEY
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
