# -*- coding: utf-8 -*-
"""PluginConfigStore 对 type=password 字段透明走系统密钥库。

设计意图：插件与设置卡都经本类读写，密钥字段加密放这里做一次，插件代码
零改动；gateway 这类拿不到能力池的插件也自动受益。
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


def test_password_field_goes_to_secret_store(store):
    s, tmp_path, keyring = store

    assert s.set_values("demo", {"name": "n", "api_key": "sk-123"}) is True

    assert keyring.data == {"plugin:demo:api_key": "sk-123"}
    raw = json.loads((tmp_path / "plugin_data" / "demo" / "config.json").read_text(encoding="utf-8"))
    assert raw == {"name": "n"}  # 明文不落盘
    assert s.get("demo", "api_key") == "sk-123"


def test_password_field_reads_back_from_secret_store(store):
    s, _tmp_path, keyring = store
    keyring.data["plugin:demo:api_key"] = "from-keyring"

    assert s.get("demo", "api_key") == "from-keyring"
    assert s.effective_values("demo")["api_key"] == "from-keyring"


def test_legacy_plaintext_migrates_on_read(store):
    s, tmp_path, keyring = store
    path = tmp_path / "plugin_data" / "demo" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"name": "n", "api_key": "legacy"}), encoding="utf-8")

    assert s.get("demo", "api_key") == "legacy"
    assert keyring.data["plugin:demo:api_key"] == "legacy"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "api_key" not in raw  # 迁移后清掉明文


def test_clearing_password_deletes_secret(store):
    s, _tmp_path, keyring = store

    s.set_values("demo", {"api_key": "sk-123"})
    s.set_values("demo", {"api_key": ""})

    assert keyring.data.get("plugin:demo:api_key") is None
    assert s.get("demo", "api_key") == ""


def test_falls_back_to_plaintext_when_keyring_unavailable(store):
    s, tmp_path, keyring = store
    keyring.available = False

    assert s.set_values("demo", {"api_key": "sk-123"}) is True

    raw = json.loads((tmp_path / "plugin_data" / "demo" / "config.json").read_text(encoding="utf-8"))
    assert raw["api_key"] == "sk-123"  # 不可用时回退明文，凭据不能丢
    assert s.get("demo", "api_key") == "sk-123"
