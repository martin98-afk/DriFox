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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 下 SecretStore 直连凭证库，不依赖 keyring 后端")
def test_secret_store_fallback_when_no_backend(monkeypatch):
    """无 keyring 后端时 SecretStore.available=False（降级旁路；仅非 Windows 平台适用）"""
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


def test_hkdf_expand_rfc5869_vector():
    """HKDF-Expand 实现用 RFC 5869 Test Case 1 官方向量锁定（SHA-256）"""
    prk = bytes.fromhex("077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    okm = bytes.fromhex(
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
        "34007208d5b887185865"
    )
    assert ss._hkdf_expand(prk, info, 42) == okm


def test_v1_legacy_prefix_is_ciphertext_but_undecryptable():
    """旧 v1（AES-GCM）密文：识别为密文（防当明文用/被覆盖），解密报错提示重填"""
    legacy = "enc:v1:AAAA"
    assert ss.is_ciphertext(legacy)
    with pytest.raises(ss.SecretDecryptError):
        ss.decrypt_secret(legacy, "pwd-123")


def test_seal_batch_shares_salt_and_roundtrips():
    """同批加密共享 salt（只做一次 KDF），各自都能解开"""
    data = {
        "LLM": {
            "SavedProviders": {
                "id1": {"API_KEY": "sk-one"},
                "id2": {"API_KEY": "sk-two"},
            }
        }
    }
    ss.seal_secrets(data, "pwd-123")
    toks = [data["LLM"]["SavedProviders"][i]["API_KEY"] for i in ("id1", "id2")]
    assert all(ss.is_ciphertext(t) for t in toks)
    # 同 salt：从密文头解出 salt 应一致
    import base64 as _b64

    salts = {_b64.urlsafe_b64decode(t[len(ss.CIPHER_PREFIX) :])[: ss._SALT_LEN] for t in toks}
    assert len(salts) == 1
    assert ss.decrypt_secret(toks[0], "pwd-123") == "sk-one"
    assert ss.decrypt_secret(toks[1], "pwd-123") == "sk-two"


# ── 超长密钥分片（凭证库单条 Blob 上限 2560 字节，utf-16 下 1280 字符） ──


class _QuotaStore:
    """模拟 Windows 凭证库 2560 字节 Blob 上限；超限写入失败（err 1783）"""

    def __init__(self, limit_bytes=2560):
        self.data = {}
        self.limit = limit_bytes
        self.available = True

    def get(self, account):
        return self.data.get(account, "")

    def set(self, account, value):
        if not value or len(value.encode("utf-16-le")) > self.limit:
            return False
        self.data[account] = value
        return True

    def delete(self, account):
        self.data.pop(account, None)


def test_split_chunks_respects_byte_budget():
    """分片不超预算，且拼回等于原值"""
    value = "A" * 3000
    chunks = ss._split_chunks(value)
    assert len(chunks) >= 3
    assert all(len(c.encode("utf-16-le")) <= ss._WIN_BLOB_MAX for c in chunks)
    assert "".join(chunks) == value


def test_split_chunks_does_not_break_surrogate_pairs():
    """代理对字符（emoji，4 字节）不被切断，拼回后仍是合法 utf-16"""
    value = "🔑" * 1000
    chunks = ss._split_chunks(value)
    assert all(len(c.encode("utf-16-le")) <= ss._WIN_BLOB_MAX for c in chunks)
    joined = "".join(chunks)
    assert joined == value
    assert joined.encode("utf-16-le").decode("utf-16-le") == value


def test_win_write_value_splits_overlong_and_reads_back(monkeypatch):
    """超长值走分片：主条目存标记，读取拼回原文"""
    store = _QuotaStore()
    monkeypatch.setattr(ss, "_win_write", lambda a, v: store.set(a, v))
    monkeypatch.setattr(ss, "_win_read", lambda a: store.get(a))
    monkeypatch.setattr(ss, "_win_delete", lambda a: store.delete(a) or True)

    key = "x" * 2944  # CodeBuddy access_token 量级
    assert ss._win_write_value("provider/abc", key) is True
    assert store.get("provider/abc").startswith(ss._CHUNK_MARKER)
    assert ss._win_read_value("provider/abc") == key


def test_win_write_value_keeps_short_value_intact():
    """短值不分片，主条目就是原值（老数据形态不变）"""
    store = _QuotaStore()
    assert store.set("provider/short", "sk-123") is True
    assert ss._chunk_count(store.get("provider/short")) == 0


def test_win_write_value_rolls_back_on_partial_failure(monkeypatch):
    """分片写入中途失败：回滚已写分片，主条目不留标记，旧值不被破坏"""
    store = _QuotaStore()
    monkeypatch.setattr(ss, "_win_write", lambda a, v: store.set(a, v))
    monkeypatch.setattr(ss, "_win_read", lambda a: store.get(a))
    monkeypatch.setattr(ss, "_win_delete", lambda a: store.delete(a) or True)

    store.set("provider/abc", "OLD-KEY")
    calls = {"n": 0}
    real_set = store.set

    def _flaky(account, value):
        if account.startswith("provider/abc#"):
            calls["n"] += 1
            if calls["n"] == 2:
                return False  # 第 2 片失败
        return real_set(account, value)

    monkeypatch.setattr(ss, "_win_write", _flaky)
    assert ss._win_write_value("provider/abc", "y" * 3000) is False
    assert store.get("provider/abc") == "OLD-KEY"
    assert not any(k.startswith("provider/abc#") for k in store.data)


def test_win_read_value_returns_empty_when_chunk_missing(monkeypatch):
    """分片缺失：按未找到处理，绝不返回半截密钥"""
    store = _QuotaStore()
    monkeypatch.setattr(ss, "_win_read", lambda a: store.get(a))
    monkeypatch.setattr(ss, "_win_delete", lambda a: store.delete(a) or True)

    store.data["provider/abc"] = f"{ss._CHUNK_MARKER}3"
    store.data["provider/abc#0"] = "part0"
    # #1、#2 缺失
    assert ss._win_read_value("provider/abc") == ""


def test_win_write_value_shrinks_and_drops_stale_chunks(monkeypatch):
    """值改短：旧分片被清理，避免脏数据残留"""
    store = _QuotaStore()
    monkeypatch.setattr(ss, "_win_write", lambda a, v: store.set(a, v))
    monkeypatch.setattr(ss, "_win_read", lambda a: store.get(a))
    monkeypatch.setattr(ss, "_win_delete", lambda a: store.delete(a) or True)

    long_key = "z" * 4000
    assert ss._win_write_value("provider/abc", long_key) is True
    assert any(k.startswith("provider/abc#") for k in store.data)

    assert ss._win_write_value("provider/abc", "short-now") is True
    assert store.get("provider/abc") == "short-now"
    assert not any(k.startswith("provider/abc#") for k in store.data)


def test_win_delete_value_removes_main_and_chunks(monkeypatch):
    """删除：主条目 + 全部分片一并清理"""
    store = _QuotaStore()
    monkeypatch.setattr(ss, "_win_write", lambda a, v: store.set(a, v))
    monkeypatch.setattr(ss, "_win_read", lambda a: store.get(a))
    monkeypatch.setattr(ss, "_win_delete", lambda a: store.delete(a) or True)

    assert ss._win_write_value("provider/abc", "q" * 4000) is True
    ss._win_delete_value("provider/abc")
    assert not any(k.startswith("provider/abc") for k in store.data)


def test_win_delete_value_sweeps_orphan_chunks(monkeypatch):
    """主条目标记缺失但存在脏分片：探测清理（超长写入中断的历史残留）"""
    store = _QuotaStore()
    monkeypatch.setattr(ss, "_win_read", lambda a: store.get(a))
    monkeypatch.setattr(ss, "_win_delete", lambda a: store.delete(a) or True)

    store.data["provider/abc#0"] = "orphan0"
    store.data["provider/abc#1"] = "orphan1"
    ss._win_delete_value("provider/abc")
    assert not any(k.startswith("provider/abc") for k in store.data)


def test_strip_secrets_warns_when_store_write_fails(monkeypatch):
    """写入失败必须留痕：fail-open 明文落盘不再静默"""

    class _BrokenStore(_FakeStore):
        def set(self, account, value):
            return False

    warns = []
    monkeypatch.setattr(ss.logger, "warning", lambda msg: warns.append(str(msg)))
    data = _make_data(api_key="sk-too-long")
    ss.strip_secrets(data, _BrokenStore())
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == "sk-too-long"
    assert any("明文落盘" in w for w in warns)


def test_end_to_end_overlong_key_roundtrip_through_chunked_store(monkeypatch):
    """端到端：1472 字符 JWT 级别的 key 经 strip/unwrap 后完整闭环，不再退回明文"""
    store = _QuotaStore()
    monkeypatch.setattr(ss, "_win_write", lambda a, v: store.set(a, v))
    monkeypatch.setattr(ss, "_win_read", lambda a: store.get(a))
    monkeypatch.setattr(ss, "_win_delete", lambda a: store.delete(a) or True)

    class _WinStore:
        available = True

        def get(self, account):
            return ss._win_read_value(account)

        def set(self, account, value):
            return bool(value) and ss._win_write_value(account, value)

        def delete(self, account):
            ss._win_delete_value(account)

    jwt = "eyJhbGciOiJSUzI1NiIsImtpZCI6Im15ZkV6cDc4M0tp" + "A" * 1420
    assert len(jwt) > 1280  # 超过 2560 字节 Blob 上限（utf-16 下 1280 字符）

    data = _make_data(api_key=jwt)
    ss.strip_secrets(data, _WinStore(), mode=ss.MODE_KEYRING)
    assert data["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == ""
    assert all(len(v.encode("utf-16-le")) <= ss._WIN_BLOB_MAX for v in store.data.values())

    # 重载：从凭证库拼回内存
    reloaded = _make_data(api_key="")
    ss.unwrap_secrets(reloaded, _WinStore(), mode=ss.MODE_KEYRING)
    assert reloaded["LLM"]["SavedProviders"]["abc12345"]["API_KEY"] == jwt
