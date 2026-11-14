# -*- coding: utf-8 -*-
"""PluginConfigStore 对 type=password 字段的明文存储行为。

用户决策（2026-11-14）：不要钥匙串，插件密码一律明文存 config.json。
本组测试锁定三件事：明文写入读取、清空即删、钥匙串时代旧值读时迁回。
"""
import json
from types import SimpleNamespace

import pytest

from app.plugins.managers.plugin_config_store import PluginConfigStore

class _Field:
    def __init__(self, key, type="text", default="", env=""):
        self.key = key
        self.type = type
        self.default = default
        self.env = env

class _Schema:
    def __init__(self, fields):
        self.fields = fields

    def get_field(self, key):
        return next((f for f in self.fields if f.key == key), None)

@pytest.fixture()
def store(tmp_path, monkeypatch, isolated_secret_store):
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: str(tmp_path))
    schema = _Schema([_Field("name"), _Field("api_key", type="password")])
    monkeypatch.setattr(PluginConfigStore, "_schema", lambda self, name: schema)
    return PluginConfigStore(), tmp_path, isolated_secret_store

def test_stored_values_readable_without_schema(store, monkeypatch):
    """schema 未注册时仍能读出用户已写入的原始配置。

    网关插件加载时序可能早于 PluginManager 注册 config_schema；此时
    ``effective_values`` 返回空，消费方（plugin.config.read 无 key 形态）
    若照此判「未配置」会静默不连接。``stored_values`` 提供原始兜底。
    """
    s, tmp_path, _keyring = store
    s.set_values("gw", {"enabled": True, "name": "x"})
    monkeypatch.setattr(PluginConfigStore, "_schema", lambda self, name: None)
    assert s.effective_values("gw") == {}
    assert s.stored_values("gw") == {"enabled": True, "name": "x"}

def test_get_accepts_caller_default(store, monkeypatch):
    """``get(plugin, key, default)`` 三参形态是插件作者常见写法，必须收。

    gateway-feishu 的 config_builder 用 ``store.get(plugin, "require_mention", True)``，
    旧签名只收两参 → manager.get_status() 抛 TypeError → 设置卡状态整片读不出来。
    无 schema 字段（或字段未声明）时返回调用方默认值。
    """
    s, _tmp, _keyring = store
    assert s.get("gw", "require_mention", True) is True
    assert s.get("gw", "label", "默认") == "默认"
    # schema 声明的字段仍以 schema 默认值优先
    assert s.get("gw", "name") == ""

def test_password_field_stored_plaintext(store):
    """password 字段一律明文落 config.json，不写钥匙串"""
    s, tmp_path, keyring = store

    assert s.set_values("demo", {"name": "n", "api_key": "sk-123"}) is True

    raw = json.loads((tmp_path / "plugin_data" / "demo" / "config.json").read_text(encoding="utf-8"))
    assert raw == {"name": "n", "api_key": "sk-123"}
    assert keyring.data == {}  # 不进钥匙串
    assert s.get("demo", "api_key") == "sk-123"
    assert s.effective_values("demo")["api_key"] == "sk-123"

def test_legacy_plaintext_stays_in_json(store):
    """历史明文保持明文，不再迁进钥匙串"""
    s, tmp_path, keyring = store
    path = tmp_path / "plugin_data" / "demo" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"name": "n", "api_key": "legacy"}), encoding="utf-8")

    assert s.get("demo", "api_key") == "legacy"
    assert keyring.data == {}  # 不迁移进密钥库
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["api_key"] == "legacy"  # 明文留在原地

def test_keyring_legacy_migrates_back_on_read(store):
    """钥匙串时代的旧值首次读取时迁回 JSON 并删钥匙串条目"""
    s, tmp_path, keyring = store
    keyring.data["plugin:demo:api_key"] = "from-keyring"

    assert s.get("demo", "api_key") == "from-keyring"

    raw = json.loads((tmp_path / "plugin_data" / "demo" / "config.json").read_text(encoding="utf-8"))
    assert raw["api_key"] == "from-keyring"  # 已回迁明文
    assert keyring.data == {}  # 钥匙串条目已清
    # 再读走纯明文路径
    assert s.get("demo", "api_key") == "from-keyring"

def test_keyring_legacy_migrates_back_via_effective_values(store):
    """effective_values 同样触发钥匙串旧值回迁"""
    s, tmp_path, keyring = store
    keyring.data["plugin:demo:api_key"] = "old-secret"

    assert s.effective_values("demo")["api_key"] == "old-secret"

    raw = json.loads((tmp_path / "plugin_data" / "demo" / "config.json").read_text(encoding="utf-8"))
    assert raw["api_key"] == "old-secret"
    assert keyring.data == {}

def test_clearing_password_removes_key_and_keyring_legacy(store):
    """空串/None 清除明文键，同时清掉钥匙串时代残留条目"""
    s, tmp_path, keyring = store
    keyring.data["plugin:demo:api_key"] = "old-secret"

    s.set_values("demo", {"api_key": ""})

    raw = json.loads((tmp_path / "plugin_data" / "demo" / "config.json").read_text(encoding="utf-8"))
    assert "api_key" not in raw
    assert keyring.data == {}
    assert s.get("demo", "api_key") == ""
