# -*- coding: utf-8 -*-
"""测试 secret_store：密钥剥出/回填/迁移/降级旁路。"""

import sys
from pathlib import Path

import pytest

# 确保仓库根目录在 sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.utils import secret_store as ss
from app.utils.config import Settings


class _FakeStore:
    """内存 SecretStore 替身（不依赖 OS 凭证库），接口与 SecretStore 一致。"""

    def __init__(self):
        self.data = {}
        self.available = True

    def get(self, account):
        return self.data.get(account, "")

    def set(self, account, value):
        if not value:
            return False
        self.data[account] = value
        return True

    def delete(self, account):
        self.data.pop(account, None)


def _make_data(api_key="sk-test-123"):
    """构造 app.config 形态的 dict（与 toDict() 输出同构）"""
    return {
        "LLM": {
            "SavedProviders": {
                "abc12345": {
                    "provider_name": "测试服务商",
                    "API_URL": "https://api.example.com/v1",
                    "API_KEY": api_key,
                    "模型名称": "gpt-4o",
                }
            }
        },
        "General": {"AutoStart": False},
    }


def test_provider_account_format():
    assert ss.provider_account("abc12345") == "provider/abc12345"


def test_strip_moves_provider_key_to_store():
    data = _make_data()
    store = _FakeStore()
    ss.strip_secrets(data, store)
    # 落盘副本不再含明文
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == ""
    # keyring 里有值
    assert store.data["provider/abc12345"] == "sk-test-123"
    # 非敏感字段不受影响
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_URL"] == "https://api.example.com/v1"


def test_strip_keeps_plaintext_when_set_fails():
    """keyring 写入失败时保留明文（fail-open），严禁双丢"""

    class _BrokenStore(_FakeStore):
        def set(self, account, value):
            return False

    data = _make_data()
    ss.strip_secrets(data, _BrokenStore())
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == "sk-test-123"


def test_unwrap_migrates_plaintext_and_keeps_memory():
    """旧版升级场景：文件里已有明文 → 迁入 keyring，内存保留明文（不中断运行）"""
    data = _make_data()
    store = _FakeStore()
    ss.unwrap_secrets(data, store)
    assert store.data["provider/abc12345"] == "sk-test-123"
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == "sk-test-123"


def test_unwrap_backfills_empty_from_store():
    """常规启动场景：文件空 → 从 keyring 回填内存"""
    data = _make_data(api_key="")
    store = _FakeStore()
    store.data["provider/abc12345"] = "sk-from-store"
    ss.unwrap_secrets(data, store)
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == "sk-from-store"


def test_unwrap_bypass_when_unavailable():
    data = _make_data(api_key="")
    store = _FakeStore()
    store.available = False
    ss.unwrap_secrets(data, store)
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == ""


def test_unwrap_no_entry_in_store_keeps_empty():
    """新机器：keyring 无条目 → 保持空（用户重输，密钥不跨设备同步）"""
    data = _make_data(api_key="")
    store = _FakeStore()
    ss.unwrap_secrets(data, store)
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == ""


def test_secret_store_fallback_when_no_backend(monkeypatch):
    """无 keyring 后端时 SecretStore.available=False（降级旁路）"""
    monkeypatch.setattr(ss, "_load_keyring", lambda: None)
    ss.SecretStore._instance = None
    try:
        store = ss.SecretStore()
        assert store.available is False
        assert store.get("provider/x") == ""
        assert store.set("provider/x", "v") is False
    finally:
        ss.SecretStore._instance = None


def test_recover_flat_secrets_migrates_back_and_deletes(monkeypatch):
    """v0.5.11 误剥的扁平 token 从凭证库回迁文件并删除凭证库条目。

    背景：扁平 ConfigItem 的 str 值不可变，toDict 外壳回填写不回 item.value，
    曾导致 Gitee 绑定 token 丢失；且 Gitee token 已按决策退出 keyring 范围。
    """

    class _FakeItem:
        def __init__(self, value):
            self.value = value

    class _FakeCfg:
        def __init__(self):
            self.gitee_user_token = _FakeItem("")
            self.gitee_user_refresh_token = _FakeItem("")
            self.github_token = _FakeItem("")
            self.saved = False

        def save(self):
            self.saved = True

    store = _FakeStore()
    store.data = {"gitee/user_token": "tok-x", "github/patch_token": "gh-x"}
    monkeypatch.setattr(ss, "SecretStore", lambda: store)  # 隔离真实凭证库
    cfg = _FakeCfg()
    Settings._recover_flat_secrets_from_keyring(cfg)
    assert cfg.gitee_user_token.value == "tok-x"
    assert cfg.github_token.value == "gh-x"
    assert cfg.gitee_user_refresh_token.value == ""
    assert store.data == {}
    assert cfg.saved is True
