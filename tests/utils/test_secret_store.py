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


# ── 密码模式 ──


def test_password_roundtrip_and_prefix():
    token = ss.encrypt_secret("sk-live-abc", "pwd-123")
    assert ss.is_ciphertext(token)
    assert "sk-live-abc" not in token
    assert ss.decrypt_secret(token, "pwd-123") == "sk-live-abc"
    # 每次加密密文不同（salt/nonce 随机），但都能解出
    assert ss.encrypt_secret("sk-live-abc", "pwd-123") != token


def test_decrypt_wrong_password_raises():
    token = ss.encrypt_secret("sk-live-abc", "pwd-123")
    with pytest.raises(ss.SecretDecryptError):
        ss.decrypt_secret(token, "wrong")
    with pytest.raises(ss.SecretDecryptError):
        ss.decrypt_secret("not-a-cipher", "pwd-123")


def test_password_mode_locks_without_password_and_keeps_ciphertext():
    """换机器首次启动：无密码 → 内存置空（不把密文当 key 用），密文进备份"""
    data = _make_data(api_key=ss.encrypt_secret("sk-live-abc", "pwd-123"))
    backup = ss.collect_ciphertexts(data)
    assert backup == {"abc12345": data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"]}
    ss.unwrap_secrets(data, _FakeStore(), mode=ss.MODE_PASSWORD, password="")
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == ""
    # locked 期间保存：靠备份原样回写，严禁丢密文
    ss.seal_secrets(data, "", backup)
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == backup["abc12345"]


def test_password_mode_unlocks_with_password():
    token = ss.encrypt_secret("sk-live-abc", "pwd-123")
    data = _make_data(api_key=token)
    ss.unwrap_secrets(data, _FakeStore(), mode=ss.MODE_PASSWORD, password="pwd-123")
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == "sk-live-abc"
    # 密码错误：内存置空（不把密文当 key 用）
    bad = _make_data(api_key=token)
    ss.unwrap_secrets(bad, _FakeStore(), mode=ss.MODE_PASSWORD, password="bad")
    assert bad["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == ""


def test_password_mode_seals_plaintext_and_leaves_ciphertext():
    data = _make_data(api_key="sk-plain")
    ss.seal_secrets(data, "pwd-123")
    sealed = data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"]
    assert ss.is_ciphertext(sealed)
    assert ss.decrypt_secret(sealed, "pwd-123") == "sk-plain"
    # 已是密文 → 原样保留（不二次加密）
    ss.seal_secrets(data, "pwd-123")
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == sealed


def test_password_mode_seal_without_password_falls_back_to_backup():
    """明文但无密码（解锁后又丢密码）：有备份用备份，无备份保留明文并告警"""
    data = _make_data(api_key="sk-plain")
    backup = {"abc12345": ss.encrypt_secret("sk-backup", "pwd-old")}
    ss.seal_secrets(data, "", backup)
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == backup["abc12345"]
    data2 = _make_data(api_key="sk-plain")
    ss.seal_secrets(data2, "")
    assert data2["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == "sk-plain"
