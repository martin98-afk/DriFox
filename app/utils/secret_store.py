# -*- coding: utf-8 -*-
"""密钥存储桥接层（keyring / 密码加密 / 明文）

三种加密方式由 Settings.secret_mode 决定，本模块是唯一落盘/回填入口：

- keyring：把服务商 API_KEY 迁入操作系统凭证库（Windows Credential Locker /
  macOS Keychain / Linux Secret Service），配置文件落盘不含密钥。
- password：API_KEY 用用户密码（scrypt KDF + AES-GCM）加密后以密文形式留在
  app.config，随 config_sync 同步到其它机器，换机输入同一密码即可解出。
- none：明文落盘。

降级策略（fail-open，行为与旧版一致）：
- keyring 未安装 / 无可用后端 / 读写异常 / 模式为 none → 旁路，明文照旧落盘。

保护边界：OS 凭证库与密码加密都只防「配置文件/云同步/备份泄露」，不防本机
同用户进程读取；密码模式下内存明文同样可被同机进程读到。

跨设备说明：keyring 模式密钥不随 config_sync 上云（落盘即无密钥），新设备
拉到配置后本机凭证库无对应条目，密钥保持为空由用户重输；password 模式密文
随配置上云，换机只需输入一次密码。
"""

import base64
import hashlib
import os
from typing import Any, Dict, Optional

from loguru import logger

# keyring service 名：Windows 凭据管理器显示为「普通凭据 DriFox:provider/<id>」
SERVICE = "DriFox"

# 仅服务商 API_KEY 进 keyring。扁平 ConfigItem（Gitee OAuth token / GitHub token）
# 已退出 keyring 范围：它们的 value 是不可变 str，toDict(serialize=False) 外壳上的
# 回填写不回 item.value（v0.5.11 曾因此丢失 Gitee 绑定 token），且用户决策 Gitee
# token 不参与加密（config_sync 上传剔除/下载合并已覆盖其云同步面）。

_PROVIDER_ACCOUNT_PREFIX = "provider/"

# ── 加密模式 ──
MODE_KEYRING = "keyring"
MODE_PASSWORD = "password"
MODE_NONE = "none"
SECRET_MODES = (MODE_KEYRING, MODE_PASSWORD, MODE_NONE)

# 「记住本机密码」在 keyring 中的 account 名（仅 password 模式使用）
MASTER_PASSWORD_ACCOUNT = "secret/master_password"

# 密文格式：enc:v1:<urlsafe_b64(salt|nonce|ciphertext+tag)>
CIPHER_PREFIX = "enc:v1:"
_SALT_LEN = 16
_NONCE_LEN = 12
_KEY_LEN = 32
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

# v0.5.11 keyring 化误剥的扁平项：(keyring account, 说明)，供一次性回迁使用
LEGACY_FLAT_ACCOUNTS = (
    "gitee/user_token",
    "gitee/user_refresh_token",
    "github/patch_token",
)


def provider_account(config_id: str) -> str:
    """服务商条目在 keyring 中的 account 名"""
    return f"{_PROVIDER_ACCOUNT_PREFIX}{config_id}"


def _load_keyring():
    """延迟导入 keyring 并校验后端可用；不可用返回 None（调用方旁路）"""
    try:
        import keyring
        from keyring.backends.fail import Keyring as _FailKeyring
    except Exception as e:
        logger.warning(f"[SecretStore] keyring 不可用，密钥走明文落盘: {e}")
        return None
    backend = keyring.get_keyring()
    if isinstance(backend, _FailKeyring):
        logger.warning("[SecretStore] 无系统凭证后端，密钥走明文落盘")
        return None
    return keyring


class SecretStore:
    """keyring 单例封装。available=False 时所有操作为无操作（旁路）"""

    _instance: Optional["SecretStore"] = None

    def __new__(cls) -> "SecretStore":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._kr = _load_keyring()  # type: ignore[attr-defined]
        return cls._instance

    @property
    def available(self) -> bool:
        return self._kr is not None

    def get(self, account: str) -> str:
        if not self.available:
            return ""
        try:
            return self._kr.get_password(SERVICE, account) or ""
        except Exception as e:
            logger.warning(f"[SecretStore] 读取失败 account={account}: {e}")
            return ""

    def set(self, account: str, value: str) -> bool:
        if not self.available or not value:
            return False
        try:
            self._kr.set_password(SERVICE, account, value)
            return True
        except Exception as e:
            logger.warning(f"[SecretStore] 写入失败 account={account}: {e}")
            return False

    def delete(self, account: str) -> None:
        if not self.available:
            return
        try:
            self._kr.delete_password(SERVICE, account)
        except Exception:
            pass  # 条目不存在时部分后端抛异常，视为已删除


def _saved_providers(data: Dict[str, Any]) -> Dict[str, Any]:
    """取 data 中的 SavedProviders 字典（类型不安全时返回空 dict）"""
    saved = (data.get("LLM") or {}).get("SavedProviders")
    return saved if isinstance(saved, dict) else {}


# ── 密码模式：加解密原语 ──


class SecretDecryptError(Exception):
    """密码解密失败（密码错误 / 密文损坏 / 密码为空）"""


def _aesgcm_cls():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM


def password_crypto_available() -> bool:
    """AES-GCM 后端是否可用（cryptography 未安装时 password 模式不可用）"""
    try:
        _aesgcm_cls()
        return True
    except Exception:
        return False


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
    )


def is_ciphertext(value: Any) -> bool:
    """是否为本模块产出的密文串"""
    return isinstance(value, str) and value.startswith(CIPHER_PREFIX)


def encrypt_secret(plain: str, password: str) -> str:
    """用密码加密明文，返回 enc:v1: 前缀密文串"""
    if not password:
        raise SecretDecryptError("加密密码为空")
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ct = _aesgcm_cls()(_derive_key(password, salt)).encrypt(nonce, plain.encode("utf-8"), None)
    return CIPHER_PREFIX + base64.urlsafe_b64encode(salt + nonce + ct).decode("ascii")


def decrypt_secret(token: str, password: str) -> str:
    """用密码解密密文串；密码错误或密文损坏抛 SecretDecryptError"""
    if not is_ciphertext(token) or not password:
        raise SecretDecryptError("密文格式不合法或密码为空")
    try:
        raw = base64.urlsafe_b64decode(token[len(CIPHER_PREFIX) :].encode("ascii"))
    except Exception as e:
        raise SecretDecryptError(f"密文 base64 损坏: {e}") from e
    if len(raw) <= _SALT_LEN + _NONCE_LEN:
        raise SecretDecryptError("密文长度不足")
    salt = raw[:_SALT_LEN]
    nonce = raw[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
    ct = raw[_SALT_LEN + _NONCE_LEN :]
    try:
        plain = _aesgcm_cls()(_derive_key(password, salt)).decrypt(nonce, ct, None)
    except Exception as e:
        raise SecretDecryptError(f"解密失败（密码错误或密文损坏）: {e}") from e
    return plain.decode("utf-8")


def collect_ciphertexts(data: Dict[str, Any]) -> Dict[str, str]:
    """收集 data 中仍处于密文态的 API_KEY（config_id → 密文），供 locked 期间原样回写。

    必须在 unwrap_secrets 之前调用：unwrap 会把解密失败的密文清空，
    而落盘时需要这份备份，避免把云端密文覆盖成空值。
    """
    out: Dict[str, str] = {}
    for cfg_id, info in _saved_providers(data).items():
        if not isinstance(info, dict):
            continue
        key = info.get("API_KEY")
        if is_ciphertext(key):
            out[str(cfg_id)] = str(key)
    return out


def seal_secrets(data: Dict[str, Any], password: str, backup: Optional[Dict[str, str]] = None) -> None:
    """password 模式落盘前处理：就地加密 data 中的服务商 API_KEY。

    - 内存明文 + 有密码 → 加密写回；
    - 内存空（locked 未解锁）+ 有备份密文 → 写回备份（严禁丢密钥）；
    - 已是密文 → 原样保留；
    - 明文但无密码（极端：解锁后又丢了密码）→ 有备份用备份，无备份保留明文并告警，
      绝不写空覆盖。
    """
    backup = backup or {}
    for cfg_id, info in _saved_providers(data).items():
        if not isinstance(info, dict):
            continue
        key = str(info.get("API_KEY") or "")
        if is_ciphertext(key):
            continue
        if key:
            if password:
                info["API_KEY"] = encrypt_secret(key, password)
            elif str(cfg_id) in backup:
                info["API_KEY"] = backup[str(cfg_id)]
            else:
                logger.warning(f"[SecretStore] 无密码且无备份密文，cfg={cfg_id} 按明文落盘")
        elif str(cfg_id) in backup:
            info["API_KEY"] = backup[str(cfg_id)]


def strip_secrets(data: Dict[str, Any], store, mode: str = MODE_KEYRING) -> None:
    """就地剥出 data（Settings.toDict() 的深拷贝）中的服务商 API_KEY，送 keyring 后清空。

    store 不可用时整体旁路（明文落盘，与旧版一致）；写入 keyring 失败时
    保留明文（fail-open），严禁双丢。仅 keyring 模式有动作。
    """
    if mode != MODE_KEYRING or not store.available:
        return
    for cfg_id, info in _saved_providers(data).items():
        if not isinstance(info, dict):
            continue
        key = str(info.get("API_KEY") or "")
        if key and store.set(provider_account(str(cfg_id)), key):
            info["API_KEY"] = ""


def unwrap_secrets(data: Dict[str, Any], store, mode: str = MODE_KEYRING, password: str = "") -> None:
    """load 后就地回填 data 中的服务商 API_KEY（data 内层须为 Settings 内存态原引用）。

    仅适用于 value 为可变 dict 的 ConfigItem（SavedProviders）：dict 内部就地改
    可穿透到 item.value；不可变 str 类扁平项写不回 item.value，禁止加入本函数范围。

    keyring 模式：
    - 文件值非空（旧版明文残留）→ 迁入 keyring，内存保留明文，运行不中断；
    - 文件值空 → 从 keyring 取回填内存；
    - keyring 无条目（新机器）→ 保持空，由用户重输；
    - store 不可用 → 整体旁路。

    password 模式：
    - 密文 + 有密码 → 解密回填明文；
    - 密文 + 无密码 / 解密失败 → 内存置空（locked），密文由 collect_ciphertexts
      提前备份，落盘时原样回写，绝不把密文当 key 用；
    - 明文（旧版残留）→ 原样保留，下次保存时加密。
    """
    if mode == MODE_KEYRING:
        if not store.available:
            return
        for cfg_id, info in _saved_providers(data).items():
            if not isinstance(info, dict):
                continue
            account = provider_account(str(cfg_id))
            key = str(info.get("API_KEY") or "")
            if key:
                store.set(account, key)
            else:
                back = store.get(account)
                if back:
                    info["API_KEY"] = back
        return

    if mode != MODE_PASSWORD:
        return
    for _cfg_id, info in _saved_providers(data).items():
        if not isinstance(info, dict):
            continue
        key = info.get("API_KEY")
        if not is_ciphertext(key):
            continue
        if not password:
            info["API_KEY"] = ""
            continue
        try:
            info["API_KEY"] = decrypt_secret(str(key), password)
        except SecretDecryptError as e:
            logger.warning(f"[SecretStore] 密文解密失败，本次按未解锁处理: {e}")
            info["API_KEY"] = ""
