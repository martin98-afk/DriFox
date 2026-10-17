# -*- coding: utf-8 -*-
"""MCP OAuth token 存储（实现 mcp SDK 的 TokenStorage 协议）。

设计要点：
- 复用 PluginConfigStore 的原子写与路径穿越防护，不另开存储文件。
  token 落在 <app_data_dir>/plugin_data/<plugin_name>/config.json，
  键名 _mcp_oauth_<server_name>，下划线前缀与用户配置键区分。
- 四个 async 方法内部全是同步 IO（PluginConfigStore 本身是同步的）。
  async 只是满足 SDK 协议签名，不引入额外线程或事件循环。
- 安全：token 为明文存储。能读该文件的进程即可取得 access token，
  备份/同步该文件时 token 会随之流转。此风险已在设计文档中标注。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from loguru import logger
from mcp.client.auth.oauth2 import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

# token 键前缀。server_name 由调用方清洗后拼接。
_TOKEN_KEY_PREFIX = "_mcp_oauth_"

# 早于该时间戳的 token 视为已过期（秒）。与 SDK 的 is_token_valid 口径一致。
_EXPIRY_SKEW = 0


def oauth_storage_key(server_name: str) -> str:
    """生成该 server 在插件 config.json 中的 token 存储键。"""
    return f"{_TOKEN_KEY_PREFIX}{server_name}"


class PluginMcpTokenStore(TokenStorage):
    """按 (plugin, server) 读写 OAuth token 与 client info。"""

    def __init__(self, plugin_name: str, server_name: str):
        # 延迟导入：PluginConfigStore 位于 app.plugins.managers，
        # 而本模块被 app.tools.mcp_tools 引用，避免顶层循环依赖。
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        self._plugin_name = plugin_name
        self._server_name = server_name
        self._key = oauth_storage_key(server_name)
        self._store = PluginConfigStore()

    # ── 内部：整块读改写 ────────────────────────────────

    def _read_blob(self) -> Dict[str, Any]:
        """读出该 server 的整块凭证（token + client_info），无则空 dict。"""
        raw = self._store._read_raw(self._plugin_name)
        blob = raw.get(self._key)
        return blob if isinstance(blob, dict) else {}

    def _write_blob(self, blob: Dict[str, Any]) -> bool:
        """整块写回。空 dict = 清除该键（登出）。"""
        if blob:
            return self._store.set_values(self._plugin_name, {self._key: blob})
        return self._store.set_values(self._plugin_name, {self._key: ""})

    # ── TokenStorage 协议 ──────────────────────────────

    async def get_tokens(self) -> Optional[OAuthToken]:
        blob = self._read_blob()
        token = blob.get("token")
        if not isinstance(token, dict):
            return None
        try:
            return OAuthToken.model_validate(token)
        except Exception as e:
            logger.warning(f"[MCP-OAuth] {self._plugin_name}/{self._server_name} token 解析失败: {e}")
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        blob = self._read_blob()
        blob["token"] = tokens.model_dump(mode="json")
        # SDK 只持久化 expires_in，过期时刻由内存按 expires_in 现算。
        # 这里额外存一个绝对过期时刻，供 UI 展示"是否已授权"与排查。
        if tokens.expires_in is not None:
            blob["expires_at"] = time.time() + int(tokens.expires_in)
        else:
            blob.pop("expires_at", None)
        ok = self._write_blob(blob)
        if ok:
            logger.info(f"[MCP-OAuth] {self._plugin_name}/{self._server_name} token 已保存")
        else:
            logger.warning(f"[MCP-OAuth] {self._plugin_name}/{self._server_name} token 保存失败")

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        blob = self._read_blob()
        info = blob.get("client_info")
        if not isinstance(info, dict):
            return None
        try:
            return OAuthClientInformationFull.model_validate(info)
        except Exception as e:
            logger.warning(f"[MCP-OAuth] {self._plugin_name}/{self._server_name} client_info 解析失败: {e}")
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        blob = self._read_blob()
        blob["client_info"] = client_info.model_dump(mode="json")
        ok = self._write_blob(blob)
        if ok:
            logger.info(
                f"[MCP-OAuth] {self._plugin_name}/{self._server_name} client_info 已保存"
                f"（client_id={client_info.client_id}）"
            )
        else:
            logger.warning(f"[MCP-OAuth] {self._plugin_name}/{self._server_name} client_info 保存失败")
