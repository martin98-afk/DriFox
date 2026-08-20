# -*- coding: utf-8 -*-
"""websearch 配置迁移到 E1 契约：三级优先级逐点等价 + 旧文件一次性迁移。

替代旧 test_websearch_api_key_config.py 的断言面。
"""

import json

import pytest

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    # 注册 schema（模拟 PluginManager 扫描 system 插件 manifest；default 用假值隔离）
    reg = PluginConfigRegistry.get_instance()
    reg.register(
        parse_config_schema(
            "system",
            {
                "title": "网页搜索 API Key",
                "fields": [
                    {
                        "key": "tavily_api_key",
                        "label": "Tavily 搜索",
                        "type": "password",
                        "default": "tvly-dev-DEFAULT",
                        "env": "TAVILY_API_KEY",
                        "placeholder": "TAVILY_API_KEY",
                    },
                    {
                        "key": "tinyfish_api_key",
                        "label": "TinyFish 搜索",
                        "type": "password",
                        "default": "sk-tinyfish-DEFAULT",
                        "env": "TINYFISH_API_KEY",
                        "placeholder": "TINYFISH_API_KEY",
                    },
                ],
            },
        )
    )
    yield tmp_path
    reg.unregister_plugin("system")


def _websearch_module():
    import importlib

    import plugins.system.tools.web_tools as m

    return importlib.reload(m)


class TestLegacyPriorityEquivalence:
    def test_default_when_nothing_set(self, env):
        m = _websearch_module()
        assert m._api_key(None, "TAVILY_API_KEY") == "tvly-dev-DEFAULT"
        assert m._api_key(None, "TINYFISH_API_KEY") == "sk-tinyfish-DEFAULT"

    def test_env_wins(self, env, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "env-key")
        m = _websearch_module()
        assert m._api_key(None, "TAVILY_API_KEY") == "env-key"

    def test_stored_wins_over_default(self, env):
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        PluginConfigStore().set_values("system", {"tavily_api_key": "user-key"})
        m = _websearch_module()
        assert m._api_key(None, "TAVILY_API_KEY") == "user-key"


class TestLegacyMigration:
    def test_legacy_web_search_keys_migrated_on_first_read(self, env):
        legacy = env / "tools" / "web_search_keys.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"tavily_api_key": "old-key"}), encoding="utf-8")

        m = _websearch_module()
        m._ensure_migrated()

        from app.plugins.managers.plugin_config_store import PluginConfigStore

        assert PluginConfigStore().get("system", "tavily_api_key") == "old-key"
        assert not legacy.exists()  # 已改名 .bak
        # 幂等：二次调用不重复迁移
        m._ensure_migrated()


class TestSelfContained:
    def test_no_manual_storage_boilerplate_left(self):
        """手写存储样板已删（get_api_key_config/set_api_key_config/_config_path 不复存在）"""
        src = open("plugins/system/tools/web_tools.py", encoding="utf-8").read()
        assert "def get_api_key_config" not in src
        assert "def set_api_key_config" not in src
        assert "_config_path" not in src

    def test_no_hardcoded_websearch_card(self):
        """手写 UI 卡已删（自动卡接管）"""
        import os

        ui_init = "plugins/system/ui/__init__.py"
        assert not os.path.exists(ui_init) or ("WebSearchKeySettingsCard" not in open(ui_init, encoding="utf-8").read())

    def test_no_settings_dependency(self):
        """插件配置不依赖主程序 Settings（沿用自包含决策）"""
        src = open("plugins/system/tools/web_tools.py", encoding="utf-8").read()
        assert "from app.utils.config import Settings" not in src
