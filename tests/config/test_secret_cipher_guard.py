# -*- coding: utf-8 -*-
"""密钥落盘/回填路径的密文保护测试。

背景（真实故障）：一台 keyring 模式的机器从云端拉到 password 模式的密文后，
``strip_secrets`` 不区分明文与密文，把 ``enc:v2:...`` 原样写进系统凭证库并把
文件里的 API_KEY 清空；下次 ``unwrap_secrets`` 又因 ``is_ciphertext(back)``
拒绝回填。结果密钥永久丢失且无任何错误提示，只能重新填写。

本文件锁定三处保护：
1. ``strip_secrets`` 绝不把密文写入凭证库，也不清空密文原值；
2. ``seal_secrets`` 在密码模式缺密码时不得落明文（fail-closed）；
3. 明文未变时密文可复用，避免每次保存重加密产生无意义的新密文。
"""

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.utils import secret_store as ss
from app.utils.secret_store import (
    MODE_KEYRING,
    MODE_PASSWORD,
    collect_ciphertexts,
    is_ciphertext,
    seal_secrets,
    strip_secrets,
)


class _FakeStore:
    """内存版凭证库，记录所有写入，供断言使用"""

    def __init__(self):
        self.available = True
        self.written = {}

    def set(self, account, value):
        self.written[account] = value
        return True

    def get(self, account):
        return self.written.get(account, "")

    def delete(self, account):
        self.written.pop(account, None)


def _payload(api_key):
    """构造一份带单条服务商的 toDict 形状数据"""
    return {"LLM": {"SavedProviders": {"cid1": {"provider_name": "测试", "API_KEY": api_key}}}}


CIPHER = "enc:v2:ZmFrZS1jaXBoZXJ0ZXh0LWZvci10ZXN0aW5n"


# ── 1. strip_secrets 的密文保护 ──────────────────────────────


def test_strip_secrets_skips_ciphertext():
    """密文不得写入凭证库（写进去就再也取不回来）"""
    store = _FakeStore()
    data = _payload(CIPHER)
    strip_secrets(data, store, mode=MODE_KEYRING)
    assert store.written == {}, f"密文被写进了凭证库: {store.written}"


def test_strip_secrets_keeps_ciphertext_on_disk():
    """密文不得被清空：清空后云端会被空值覆盖，密钥永久丢失"""
    store = _FakeStore()
    data = _payload(CIPHER)
    strip_secrets(data, store, mode=MODE_KEYRING)
    assert data["LLM"]["SavedProviders"]["cid1"]["API_KEY"] == CIPHER


def test_strip_secrets_still_strips_plaintext():
    """明文仍照旧进凭证库并清空文件值（原行为不能回退）"""
    store = _FakeStore()
    data = _payload("sk-plain-123")
    strip_secrets(data, store, mode=MODE_KEYRING)
    assert store.written == {ss.provider_account("cid1"): "sk-plain-123"}
    assert data["LLM"]["SavedProviders"]["cid1"]["API_KEY"] == ""


# ── 2. seal_secrets 缺密码时 fail-closed ─────────────────────


def test_seal_secrets_never_writes_plaintext_when_password_missing():
    """密码模式缺密码且无备份密文时，绝不能把明文写进落盘数据"""
    data = _payload("sk-must-not-leak")
    seal_secrets(data, password="", backup={}, kdf_salt="")
    written = data["LLM"]["SavedProviders"]["cid1"]["API_KEY"]
    assert "sk-must-not-leak" not in written, f"明文被落盘: {written}"


def test_seal_secrets_uses_backup_when_password_missing():
    """缺密码但有备份密文时，回写备份（原行为，保持不变）"""
    data = _payload("sk-plain")
    seal_secrets(data, password="", backup={"cid1": CIPHER}, kdf_salt="")
    assert data["LLM"]["SavedProviders"]["cid1"]["API_KEY"] == CIPHER


def test_seal_secrets_encrypts_with_password():
    """有密码时正常加密，明文不出现在结果里"""
    data = _payload("sk-encrypt-me")
    seal_secrets(data, password="pwd", backup={}, kdf_salt="")
    out = data["LLM"]["SavedProviders"]["cid1"]["API_KEY"]
    assert is_ciphertext(out)
    assert "sk-encrypt-me" not in out


# ── 3. 密文复用：明文未变则不重新加密 ────────────────────────


def test_seal_secrets_reuses_ciphertext_for_unchanged_plaintext():
    """同一明文二次落盘不得产生新密文，否则每次保存都制造一次云端变更"""
    first = _payload("sk-stable")
    seal_secrets(first, password="pwd", backup={}, kdf_salt="")
    cipher1 = first["LLM"]["SavedProviders"]["cid1"]["API_KEY"]

    # 第二次：内存里是同一明文，带上上一次的密文作为备份
    second = _payload("sk-stable")
    backup = collect_ciphertexts(first)
    seal_secrets(second, password="pwd", backup=backup, kdf_salt="")
    cipher2 = second["LLM"]["SavedProviders"]["cid1"]["API_KEY"]

    assert cipher2 == cipher1, "同一明文被重新加密，会产生无意义的云端提交"


def test_seal_secrets_still_encrypts_changed_plaintext():
    """明文变了必须重新加密，不能因为复用逻辑而写回旧密文"""
    first = _payload("sk-old")
    seal_secrets(first, password="pwd", backup={}, kdf_salt="")
    old_cipher = first["LLM"]["SavedProviders"]["cid1"]["API_KEY"]

    second = _payload("sk-new")
    backup = collect_ciphertexts(first)
    seal_secrets(second, password="pwd", backup=backup, kdf_salt="")
    new_cipher = second["LLM"]["SavedProviders"]["cid1"]["API_KEY"]

    assert new_cipher != old_cipher
    assert ss.decrypt_secret(new_cipher, "pwd") == "sk-new"


def test_reuse_requires_verified_backup_match():
    """备份密文解不回当前明文时（密码已换），必须重新加密"""
    first = _payload("sk-original")
    seal_secrets(first, password="old-pwd", backup={}, kdf_salt="")
    old_backup = collect_ciphertexts(first)

    second = _payload("sk-original")
    seal_secrets(second, password="new-pwd", backup=old_backup, kdf_salt="")
    out = second["LLM"]["SavedProviders"]["cid1"]["API_KEY"]

    assert is_ciphertext(out)
    assert ss.decrypt_secret(out, "new-pwd") == "sk-original"
