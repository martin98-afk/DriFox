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


def _make_data(api_key="sk-test-123", gitee_token="", github_token=""):
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
        "Gitee": {"UserToken": gitee_token, "UserRefreshToken": ""},
        "Patch": {"GitHub/Token": github_token},
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


def test_strip_flat_items():
    data = _make_data(gitee_token="gt-1", github_token="gh-1")
    store = _FakeStore()
    ss.strip_secrets(data, store)
    assert data["Gitee"]["UserToken"] == ""
    assert data["Patch"]["GitHub/Token"] == ""
    assert store.data["gitee/user_token"] == "gt-1"
    assert store.data["github/patch_token"] == "gh-1"


def test_strip_bypass_when_unavailable():
    data = _make_data()
    store = _FakeStore()
    store.available = False
    ss.strip_secrets(data, store)
    # 旁路：明文保留（与旧版行为一致）
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == "sk-test-123"
    assert store.data == {}


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


def test_unwrap_flat_items_roundtrip():
    store = _FakeStore()
    data1 = _make_data(gitee_token="gt-1", github_token="gh-1")
    ss.strip_secrets(data1, store)
    data2 = _make_data(gitee_token="", github_token="")
    ss.unwrap_secrets(data2, store)
    assert data2["Gitee"]["UserToken"] == "gt-1"
    assert data2["Patch"]["GitHub/Token"] == "gh-1"


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
