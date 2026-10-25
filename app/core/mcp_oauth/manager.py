# -*- coding: utf-8 -*-
"""MCP OAuth 门面（单例）。

对外只暴露同步接口，供 UI 线程与 MCP 连接层调用：
- get_provider  取得 provider 实例（连接层用）
- authorize     驱动一次完整授权（设置卡按钮用）
- has_token     是否已有 token（设置卡回显用）
- revoke        登出

内部状态（provider 缓存 / 授权会话）都收敛在 McpOAuthProviderFactory，
本模块只做单例包装，避免调用方各自 new factory 导致缓存失效。
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from app.core.mcp_oauth.provider import OAuthSession, McpOAuthProviderFactory


class McpOAuthManager:
    """MCP OAuth 全局门面（进程级单例）。"""

    _instance: Optional["McpOAuthManager"] = None

    @classmethod
    def get_instance(cls) -> "McpOAuthManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        if McpOAuthManager._instance is not None and McpOAuthManager._instance is not self:
            raise RuntimeError("请使用 McpOAuthManager.get_instance() 获取单例")
        self._factory = McpOAuthProviderFactory()

    # ── 连接层用 ───────────────────────────────────────

    def get_provider(self, plugin_name: str, server_name: str, server_url: str, auth_cfg: dict):
        """取得该 server 的 OAuthClientProvider，供 streamablehttp_client(auth=...) 使用。

        Args:
            auth_cfg: .mcp.json 里的 auth 段，读取 client_name 与 scope。
        """
        return self._factory.get_provider(
            plugin_name,
            server_name,
            server_url,
            client_name=auth_cfg.get("client_name") or "DriFox",
            scope=auth_cfg.get("scope") or "",
            token_auth_method=auth_cfg.get("token_endpoint_auth_method") or "",
        )

    def drop_provider(self, plugin_name: str, server_name: str) -> None:
        """连接断开时丢弃 provider 内存态，下次连接重新从存储加载。"""
        self._factory.drop_provider(plugin_name, server_name)

    # ── UI 用 ──────────────────────────────────────────

    def authorize(self, plugin_name: str, server_name: str, server_url: str, auth_cfg: dict) -> OAuthSession:
        """驱动一次完整授权。同步阻塞直到拿到 token 或超时/失败。"""
        return self._factory.authorize(
            plugin_name,
            server_name,
            server_url,
            client_name=auth_cfg.get("client_name") or "DriFox",
            scope=auth_cfg.get("scope") or "",
            token_auth_method=auth_cfg.get("token_endpoint_auth_method") or "",
        )

    def get_session(self, plugin_name: str, server_name: str) -> Optional[OAuthSession]:
        return self._factory.get_session(plugin_name, server_name)

    def cancel(self, plugin_name: str, server_name: str) -> bool:
        """中止进行中的授权（UI 线程可调，立即返回）。

        只置取消标记并唤醒等待方，回调 server 的关闭与线程收尾由授权线程完成，
        因此不会阻塞界面。返回是否真的中止了一个等待中的会话。
        """
        ok = self._factory.cancel(plugin_name, server_name)
        if ok:
            logger.info(f"[MCP-OAuth] {plugin_name}/{server_name} 授权中止请求已接受")
        return ok

    def has_token(self, plugin_name: str, server_name: str) -> bool:
        return self._factory.has_token(plugin_name, server_name)

    def revoke(self, plugin_name: str, server_name: str) -> bool:
        """登出：清本地 token + client info + provider 缓存。"""
        ok = self._factory.revoke(plugin_name, server_name)
        if ok:
            logger.info(f"[MCP-OAuth] {plugin_name}/{server_name} 已登出")
        return ok
