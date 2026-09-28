# -*- coding: utf-8 -*-
"""PluginConfigStore：统一存储 + 三级优先级 + 迁移。"""

import json

import pytest

from app.plugins.contracts.plugin_config import parse_config_schema
from app.plugins.registries.plugin_config_registry import PluginConfigRegistry
from app.plugins.managers.plugin_config_store import PluginConfigStore


@pytest.fixture()
def store_env(tmp_path, monkeypatch):
    """把 app_data_dir 指到临时目录，隔离真实用户数据。"""
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    reg = PluginConfigRegistry.get_instance()
    reg.register(
        parse_config_schema(
            "plug-x",
            {
                "title": "X",
                "fields": [
                    {
                        "key": "api_key",
                        "label": "Key",
                        "type": "password",
                        "default": "built-in-default",
                        "env": "PLUG_X_KEY",
                    },
                    {"key": "verbose", "label": "详细", "type": "bool", "default": False},
                ],
            },
        )
    )
    yield tmp_path, monkeypatch
    reg.unregister_plugin("plug-x")


class TestPriorityChain:
    def test_falls_back_to_schema_default(self, store_env):
        s = PluginConfigStore()
        assert s.get("plug-x", "api_key") == "built-in-default"

    def test_stored_value_wins_over_default(self, store_env):
        s = PluginConfigStore()
        assert s.set_values("plug-x", {"api_key": "user-key"})
        assert s.get("plug-x", "api_key") == "user-key"

    def test_env_wins_over_stored(self, store_env):
        tmp_path, monkeypatch = store_env
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "user-key"})
        monkeypatch.setenv("PLUG_X_KEY", "env-key")
        assert s.get("plug-x", "api_key") == "env-key"

    def test_empty_string_clears_back_to_default(self, store_env):
        # 与旧 websearch 语义等价：空串 = 清除用户配置 → 回退内置默认
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "user-key"})
        s.set_values("plug-x", {"api_key": ""})
        assert s.get("plug-x", "api_key") == "built-in-default"

    def test_bool_normalization(self, store_env):
        s = PluginConfigStore()
        s.set_values("plug-x", {"verbose": "true"})  # UI/JSON 里可能是字符串
        assert s.get("plug-x", "verbose") is True

    def test_unknown_key_without_schema_returns_none(self, store_env):
        s = PluginConfigStore()
        assert s.get("no-such-plugin", "k") is None


class TestPersistence:
    def test_get_all_effective_values(self, store_env):
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "k1"})
        vals = s.get_all("plug-x")
        assert vals["api_key"] == "k1"
        assert vals["verbose"] is False  # 未配置 → 默认

    def test_reset(self, store_env):
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "k1"})
        assert s.reset("plug-x") is True
        assert s.get("plug-x", "api_key") == "built-in-default"

    def test_corrupt_json_tolerated(self, store_env):
        tmp_path, _ = store_env
        cfg_dir = tmp_path / "plugins" / "plug-x"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text("{ not json", encoding="utf-8")
        s = PluginConfigStore()
        assert s.get("plug-x", "api_key") == "built-in-default"  # 损坏容错回默认
        # 坏文件先被 legacy 迁移搬到 plugin_data，读坏后改名 .corrupt 保留现场
        new_cfg = tmp_path / "plugin_data" / "plug-x" / "config.json"
        assert (tmp_path / "plugin_data" / "plug-x" / "config.json.corrupt").exists()
        assert not new_cfg.exists()
        assert s.set_values("plug-x", {"api_key": "rebuilt"})
        assert s.get("plug-x", "api_key") == "rebuilt"

    def test_write_atomic_no_partial_read(self, store_env):
        """并发 get/set 大配置：终态 JSON 可解析、全部字段在（无半截文件）"""
        import concurrent.futures

        tmp_path, _ = store_env
        s = PluginConfigStore()
        big = {f"key_{i:03d}": f"value-{i}-" + "x" * 200 for i in range(60)}
        s.set_values("plug-x", big)

        def worker(n):
            for i in range(100):
                if n % 2 == 0:
                    s.set_values("plug-x", {f"key_{i % 60:03d}": f"w{n}-{i}"})
                else:
                    s.get_all("plug-x")
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            assert all(ex.map(worker, range(4)))

        final = json.loads((tmp_path / "plugin_data" / "plug-x" / "config.json").read_text(encoding="utf-8"))
        assert len(final) == 60  # 键集合收敛（不丢不半截）
        assert not (tmp_path / "plugin_data" / "plug-x" / "config.json.tmp").exists()

    def test_set_values_preserves_unknown_keys(self, store_env):
        """set_values 合并写：不在本次 values 与 schema 里的孤儿键原样保留"""
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "k1", "orphan_key": "keep-me"})
        s.set_values("plug-x", {"verbose": "true"})
        raw = json.loads(
            (store_env[0] / "plugin_data" / "plug-x" / "config.json").read_text(encoding="utf-8")
        )
        assert raw["orphan_key"] == "keep-me"
        assert raw["api_key"] == "k1"
        assert raw["verbose"] == "true"


class TestMigration:
    def test_migrate_legacy_file(self, store_env):
        tmp_path, _ = store_env
        legacy = tmp_path / "tools" / "web_search_keys.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"tavily_api_key": "legacy-key"}), encoding="utf-8")
        s = PluginConfigStore()
        ok = s.migrate("plug-x", legacy, key_map={"tavily_api_key": "api_key"})
        assert ok is True
        assert s.get("plug-x", "api_key") == "legacy-key"
        # 旧文件改名 .bak（不再参与读取，保留现场）
        assert not legacy.exists()
        assert (tmp_path / "tools" / "web_search_keys.json.bak").exists()

    def test_migrate_noop_when_target_exists(self, store_env):
        tmp_path, _ = store_env
        legacy = tmp_path / "old.json"
        legacy.write_text(json.dumps({"tavily_api_key": "old"}), encoding="utf-8")
        s = PluginConfigStore()
        s.set_values("plug-x", {"api_key": "already-set"})
        ok = s.migrate("plug-x", legacy, key_map={"tavily_api_key": "api_key"})
        assert ok is False  # 已有新配置不覆盖
        assert s.get("plug-x", "api_key") == "already-set"

    def test_migrate_missing_file(self, store_env):
        s = PluginConfigStore()
        assert s.migrate("plug-x", "/no/such/file.json", key_map={}) is False
