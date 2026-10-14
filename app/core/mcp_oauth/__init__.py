# -*- coding: utf-8 -*-
"""MCP OAuth 核心包。

把 mcp SDK 的 OAuthClientProvider 适配到 DriFox：
- token 存插件 config.json（复用 PluginConfigStore 的原子写与路径防护）
- 本地临时 HTTP server 收授权码
- 打开系统默认浏览器完成授权

对外入口统一走 McpOAuthManager.get_instance()。
"""

from app.core.mcp_oauth.manager import McpOAuthManager

__all__ = ["McpOAuthManager"]
