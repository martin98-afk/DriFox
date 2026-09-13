# -*- coding: utf-8 -*-
"""系统密钥库桥接层（keyring）

把 app.config 中的敏感值（服务商 API_KEY / Gitee OAuth token / GitHub token）
迁入操作系统凭证库（Windows Credential Locker / macOS Keychain /
Linux Secret Service），配置文件落盘时不再含明文密钥。

降级策略（fail-open，行为与旧版一致）：
- keyring 未安装 / 无可用后端 / 读写异常 / 开关关闭 → 完全旁路，明文照旧落盘。

保护边界：OS 凭证库防「配置文件/云同步/备份泄露」，不防本机同用户进程
读取，这是三平台凭证库的统一物理边界。

跨设备说明：密钥不随 config_sync 上云（落盘即无密钥），新设备拉到配置后
本机凭证库无对应条目，密钥保持为空由用户重输。
"""

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# keyring service 名：Windows 凭据管理器显示为「普通凭据 DriFox:provider/<id>」
SERVICE = "DriFox"

# app.config 顶层扁平敏感项：(组, 键, keyring account)
FLAT_SECRET_ITEMS: List[Tuple[str, str, str]] = [
    ("Gitee", "UserToken", "gitee/user_token"),
    ("Gitee", "UserRefreshToken", "gitee/user_refresh_token"),
    ("Patch", "GitHub/Token", "github/patch_token"),
]

_PROVIDER_ACCOUNT_PREFIX = "provider/"


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


def strip_secrets(data: Dict[str, Any], store) -> None:
    """就地剥出 data（Settings.toDict() 的深拷贝）中的敏感值，送 keyring 后清空。

    store 不可用时整体旁路（明文落盘，与旧版一致）。
    """
    if not store.available:
        return
    saved = (data.get("LLM") or {}).get("SavedProviders")
    if isinstance(saved, dict):
        for cfg_id, info in saved.items():
            if not isinstance(info, dict):
                continue
            key = str(info.get("API_KEY") or "")
            if key:
                store.set(provider_account(str(cfg_id)), key)
                info["API_KEY"] = ""
    for group, name, account in FLAT_SECRET_ITEMS:
        g = data.get(group)
        if isinstance(g, dict) and g.get(name):
            store.set(account, str(g[name]))
            g[name] = ""


def unwrap_secrets(data: Dict[str, Any], store) -> None:
    """load 后就地回填 data 中的敏感值（data 内层须为 Settings 内存态原引用）。

    - 文件值非空（旧版明文残留）→ 迁入 keyring，内存保留明文，运行不中断；
    - 文件值空 → 从 keyring 取回填内存；
    - keyring 无条目（新机器）→ 保持空，由用户重输；
    - store 不可用 → 整体旁路。
    """
    if not store.available:
        return
    saved = (data.get("LLM") or {}).get("SavedProviders")
    if isinstance(saved, dict):
        for cfg_id, info in saved.items():
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
    for group, name, account in FLAT_SECRET_ITEMS:
        g = data.get(group)
        if not isinstance(g, dict):
            continue
        value = str(g.get(name) or "")
        if value:
            store.set(account, value)
        else:
            back = store.get(account)
            if back:
                g[name] = back
