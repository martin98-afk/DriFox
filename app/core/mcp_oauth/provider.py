# -*- coding: utf-8 -*-
"""MCP OAuth provider 组装。

把 mcp SDK 的 OAuthClientProvider 接到 DriFox 的生态上：
- storage 用 PluginMcpTokenStore（落插件 config.json）
- redirect_handler 打开系统默认浏览器
- callback_handler 等待本地回调 server 收码

SDK 已实现 SEP-985 protected resource metadata 发现、OAuth Authorization
Server Metadata 发现、PKCE、RFC 7591 动态客户端注册、401 重授权与 token
自动刷新。本模块只做适配，不重复实现协议。
"""

from __future__ import annotations

import asyncio
import json
import webbrowser
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata

from app.core.mcp_oauth.callback_server import CallbackServer
from app.core.mcp_oauth.token_store import PluginMcpTokenStore

# 授权等待上限（秒）。超时后关闭回调 server 并报错。
DEFAULT_AUTH_TIMEOUT = 300.0

# 驱动 OAuth 流程的首请求：一个真实的最小 MCP initialize 请求。
# SDK 只在 401/403 响应上才进入授权流程，因此这个请求必须让服务端有机会
# 判定「未授权」。裸 GET 会被只接受 POST 的服务回 405（figma、context7），
# flow 静默收尾，最终只剩一句「授权未完成」。
_INITIALIZE_METHOD = "initialize"


def _build_probe_request(server_url: str):
    """构造驱动 OAuth 流程的首请求（POST JSON-RPC initialize）。"""
    import httpx
    from mcp.types import LATEST_PROTOCOL_VERSION

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": _INITIALIZE_METHOD,
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "DriFox", "version": "1.0"},
            },
        }
    ).encode("utf-8")
    return httpx.Request(
        "POST",
        server_url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-protocol-version": LATEST_PROTOCOL_VERSION,
        },
    )


def _reject_unexpected_status(response) -> Optional[str]:
    """首请求没回 401/403 时给出可读原因；正常则返回 None。

    SDK 的授权入口只对 401（完整授权）和 403（insufficient_scope 升级）生效。
    其它状态码下 SDK 会静默收尾，调用方只能看到「授权未完成」，真实原因
    （服务端不认 GET、端点写错、服务端该路径需要别的前缀等）被彻底吞掉。
    """
    status = response.status_code
    if status in (401, 403):
        return None
    if status == 200:
        return "服务端对该请求直接返回 200，未要求授权（可能不需要 OAuth，或 url 指向了非 MCP 端点）"
    return f"首请求返回 HTTP {status}，服务端未按 MCP 规范要求授权（应按需 401）"


@dataclass
class OAuthSession:
    """一次授权会话的状态。"""

    key: str
    status: str = "pending"  # pending / done / error / cancelled
    error: Optional[str] = None
    auth_url: Optional[str] = None
    _callback: Optional[CallbackServer] = field(default=None, repr=False)


class McpOAuthProviderFactory:
    """按 (plugin, server) 构造 OAuthClientProvider 并驱动一次授权。"""

    def __init__(self, auth_timeout: float = DEFAULT_AUTH_TIMEOUT):
        self._auth_timeout = auth_timeout
        # 同一 key 同时只允许一个授权会话，防重复点击起多个回调 server。
        self._sessions: dict[str, OAuthSession] = {}
        # provider 实例缓存：连接的整个生命周期复用同一实例，
        # 让 SDK 内部的 token 内存态在多次请求间保持（自动刷新才有意义）。
        self._providers: dict[str, OAuthClientProvider] = {}

    @staticmethod
    def make_key(plugin_name: str, server_name: str) -> str:
        return f"{plugin_name}/{server_name}"

    # ── provider 获取 ──────────────────────────────────

    def get_provider(
        self,
        plugin_name: str,
        server_name: str,
        server_url: str,
        client_name: str = "DriFox",
        scope: str = "",
        token_auth_method: str = "",
    ) -> OAuthClientProvider:
        """取得（或新建）该 server 的 provider 实例。

        新建时 redirect_uri 用一个占位端口无法确定的值，真正的端口在首次
        授权时由 callback server 决定。SDK 只在 401 走完整授权流程时才会
        用到 redirect_uris，因此首次连接前可以是占位值。

        Args:
            token_auth_method: DCR 时显式声明的 token_endpoint_auth_method
                （如 "none"）。某些授权服务器（如 Apify）对未声明该字段的
                client 会生成 secret，但 DCR 响应不回显该字段，SDK 解析为
                None 后 token 交换不带 secret，服务端报 invalid_client
                "Invalid secret"。声明 "none" 后服务端不再要求 secret。
        """
        key = self.make_key(plugin_name, server_name)
        cached = self._providers.get(key)
        if cached is not None:
            meta = cached.context.client_metadata
            current = getattr(meta, "token_endpoint_auth_method", None) or ""
            if current == (token_auth_method or ""):
                return cached
            # 配置里的 auth 方法变了：丢弃旧 provider 强制重建（client_info
            # 在 storage 持久化不受影响，但 DCR 需要用新声明重跑）。
            logger.info(f"[MCP-OAuth] {key} token_endpoint_auth_method 变更（{current or '未声明'} -> {token_auth_method or '未声明'}），重建 provider")
            self._providers.pop(key, None)

        storage = PluginMcpTokenStore(plugin_name, server_name)
        metadata = OAuthClientMetadata(
            # 占位端口 0 不合法，用 1 只是为了让 pydantic 校验通过；
            # 真正授权前会被 authorize() 改写为 callback server 的实际端口。
            redirect_uris=["http://127.0.0.1:1/callback"],
            client_name=client_name or "DriFox",
            scope=scope or None,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method=token_auth_method or None,
        )
        provider = OAuthClientProvider(
            server_url=server_url,
            client_metadata=metadata,
            storage=storage,
        )
        self._patch_token_auth(provider)
        self._providers[key] = provider
        if client_name and client_name != "DriFox":
            # 少数授权服务器（如 Figma）只按 client_name 白名单放行动态注册，
            # 插件配置里会填第三方客户端名绕过。它不是 DriFox 的真实身份，
            # 必须在日志与设置卡上明示，避免误以为是官方支持。
            logger.warning(
                f"[MCP-OAuth] {key} 以「{client_name}」身份注册（非 DriFox），属绕过授权服务器白名单，风险由使用者自担"
            )
        logger.info(f"[MCP-OAuth] {key} provider 已创建")
        return provider

    @staticmethod
    def _patch_token_auth(provider: OAuthClientProvider) -> None:
        """修正 SDK 在 client_secret_basic 下的请求体（兼容性补丁）。

        RFC 6749 §2.3.1：用 basic 认证时客户端凭据只能出现在 Authorization
        header，请求体不得再带 client_id / client_secret。SDK 的
        prepare_token_auth 只从 body 删了 client_secret，client_id 仍留在
        body 里，导致严格实现（Notion 等）返回 400
        "Client must not use multiple authentication methods"。

        这里包一层：basic 模式下把 client_id 也从 body 移除，其余行为不变。
        token_endpoint_auth_method 为 none / client_secret_post 时不干预。
        """
        original = provider.context.prepare_token_auth

        def patched(data, headers=None):
            data, headers = original(data, headers)
            info = provider.context.client_info
            if (
                headers
                and str(headers.get("Authorization", "")).startswith("Basic ")
                and info
                and info.token_endpoint_auth_method == "client_secret_basic"
            ):
                data = {k: v for k, v in data.items() if k not in ("client_id", "client_secret")}
            return data, headers

        provider.context.prepare_token_auth = patched

    def drop_provider(self, plugin_name: str, server_name: str) -> None:
        """丢弃缓存的 provider（登出/重新授权时调用）。"""
        key = self.make_key(plugin_name, server_name)
        self._providers.pop(key, None)

    # ── 授权 ───────────────────────────────────────────

    def authorize(
        self,
        plugin_name: str,
        server_name: str,
        server_url: str,
        client_name: str = "DriFox",
        scope: str = "",
        token_auth_method: str = "",
    ) -> OAuthSession:
        """驱动一次完整授权（同步接口，UI 线程可调）。

        流程：起回调 server → 改写 redirect_uris → 开浏览器 → 等回调 → 收尾。

        Returns:
            OAuthSession。status=done 表示已拿到 token。
        """
        key = self.make_key(plugin_name, server_name)
        existing = self._sessions.get(key)
        if existing is not None and existing.status == "pending":
            logger.info(f"[MCP-OAuth] {key} 已有进行中的授权会话，直接复用")
            return existing

        session = OAuthSession(key=key)
        self._sessions[key] = session
        callback = CallbackServer()
        session._callback = callback

        try:
            redirect_uri = callback.start()
        except OSError as e:
            session.status = "error"
            session.error = f"无法启动本地回调服务: {e}"
            logger.error(f"[MCP-OAuth] {key} 回调 server 启动失败: {e}")
            return session

        provider = self.get_provider(plugin_name, server_name, server_url, client_name, scope, token_auth_method)
        # 把占位 redirect_uri 改写成实际端口，DCR 注册时才会提交正确地址。
        # 重新构造 metadata 而非原地赋值：OAuthClientMetadata 未开
        # validate_assignment，原地赋裸字符串会让后续序列化报 pydantic 警告。
        meta = provider.context.client_metadata
        provider.context.client_metadata = type(meta).model_validate(
            {**meta.model_dump(mode="json", exclude_none=True), "redirect_uris": [redirect_uri]}
        )

        try:
            url = self._run_authorization(provider, callback, session)
            session.auth_url = url
        finally:
            callback.close()
            session._callback = None
            if session.status == "pending":
                session.status = "error"
                session.error = "授权未完成"

        if session.status == "done":
            logger.info(f"[MCP-OAuth] {key} 授权成功")
        elif session.status == "cancelled":
            logger.info(f"[MCP-OAuth] {key} 授权已中止")
        else:
            logger.warning(f"[MCP-OAuth] {key} 授权失败: {session.error}")
        return session

    def cancel(self, plugin_name: str, server_name: str) -> bool:
        """中止该 server 进行中的授权。返回是否真的中止了一个等待中的会话。

        UI 线程可调：只置取消标记 + 唤醒等待方，关 server 与线程收尾由授权线程
        自己的 finally 完成，不会阻塞界面。
        """
        key = self.make_key(plugin_name, server_name)
        session = self._sessions.get(key)
        if session is None or session.status != "pending" or session._callback is None:
            return False
        session._callback.cancel()
        return True

    def _run_authorization(
        self,
        provider: OAuthClientProvider,
        callback: CallbackServer,
        session: OAuthSession,
    ) -> str:
        """在 SDK 的授权流程里插入本地回调等待。

        SDK 的 redirect_handler 只负责打开浏览器，callback_handler 负责
        等待并返回 (code, state)。这里替换这两个 handler 把流程串起来，
        再用 httpx.MockTransport 喂响应，让 SDK 自己跑完
        discovery → DCR → 授权码 → token 交换全流程。
        """
        opened = {"url": ""}

        async def redirect_handler(auth_url: str) -> None:
            opened["url"] = auth_url
            logger.info(f"[MCP-OAuth] 打开浏览器授权: {auth_url}")
            try:
                webbrowser.open(auth_url)
            except Exception as e:  # pragma: no cover - 无浏览器环境
                logger.warning(f"[MCP-OAuth] 打开浏览器失败: {e}")

        async def callback_handler() -> tuple[str, Optional[str]]:
            result = callback.wait(self._auth_timeout)
            if result.cancelled:
                session.status = "cancelled"
                session.error = "授权已中止"
                return "", None
            if result.error:
                session.status = "error"
                session.error = result.error_description or result.error
                return "", None
            if not result.code:
                session.status = "error"
                session.error = "未收到授权码"
                return "", None
            # 不在这里置 done：token 交换可能仍失败（如 Apify invalid_client）。
            # done 只能由 drive() 在流程真正收尾时置位，否则失败被假报成成功。
            return result.code, result.state

        # 保存原 handler（必须在 patch 前），授权结束后恢复。provider 实例被
        # 工厂缓存、连接流程复用同一实例：若残留本流程的 closure，连接时
        # SDK 的 OAuth 会拿到已关闭 CallbackServer 的旧回调，必然
        # State parameter mismatch（apify 2026-10-25 事故根因之一）。
        saved_redirect = getattr(provider.context, "redirect_handler", None)
        saved_callback = getattr(provider.context, "callback_handler", None)

        def restore_handlers() -> None:
            provider.context.redirect_handler = saved_redirect  # type: ignore[union-attr]
            provider.context.callback_handler = saved_callback  # type: ignore[union-attr]

        provider.context.redirect_handler = redirect_handler  # type: ignore[union-attr]
        provider.context.callback_handler = callback_handler  # type: ignore[union-attr]

        async def drive() -> None:
            """模拟 httpx 的角色，真正发送 SDK yield 出来的请求。

            SDK 的 async_auth_flow 是纯 yield 协议：每个 yield 都是一个待发送的
            httpx.Request，调用方负责发出去并把 Response asend 回去。discovery /
            DCR / token 交换都以 yield 形式出现，若像早期版本那样一律回 401，
            discovery 阶段就会抛 OAuthFlowError。因此这里必须真实发送。

            首请求必须是服务端会回 401 的真实 MCP 请求（POST initialize）。
            SDK 只在收到 401 时才进入 OAuth 流程，裸 GET 会被 figma /
            context7 这类只接受 POST 的服务回 405，flow 静默收尾、authorize
            最终只剩一句「授权未完成」，掩盖真实原因。
            """
            import httpx

            server_url = str(provider.context.server_url)
            async with httpx.AsyncClient(timeout=30) as client:
                flow = provider.async_auth_flow(_build_probe_request(server_url))
                checked = False
                try:
                    pending = await flow.__anext__()
                    while True:
                        response = await client.send(pending)
                        # 只校验发往 server_url 的首个请求。SDK 在 token 可刷新时
                        # 会先 yield refresh 请求，那不是授权入口，不能按同一口径判。
                        if not checked and str(pending.url).split("?")[0] == server_url.split("?")[0]:
                            checked = True
                            # 已存在有效 token 时 SDK 会直接带 Authorization 发出，
                            # 服务端回 200 属正常（不必重新授权），不能当成异常。
                            if response.status_code == 200 and "authorization" in pending.headers:
                                session.status = "done"
                                logger.info(f"[MCP-OAuth] {session.key} 已有有效 token，无需重新授权")
                                return
                            reason = _reject_unexpected_status(response)
                            if reason:
                                session.status = "error"
                                session.error = reason
                                logger.warning(f"[MCP-OAuth] {session.key} 首请求未要求授权：{reason}")
                                return
                        pending = await flow.asend(response)
                except StopAsyncIteration:
                    # 流程正常收尾（最后一次 yield 是带上 token 的重试请求）。
                    if session.status == "pending":
                        if opened["url"]:
                            # 走完浏览器授权且 token 交换成功，此刻才算真成功。
                            # 日志由 authorize() 尾部统一打，这里只置状态。
                            session.status = "done"
                        else:
                            # 全程没走到浏览器授权，说明服务端从未要求授权，
                            # 不能只报「授权未完成」。
                            session.status = "error"
                            session.error = "服务端未要求授权（未返回 401），请确认该地址是 MCP 端点且需要 OAuth"
                except Exception as e:
                    # SDK 抛的 OAuthFlowError / OAuthRegistrationError 带真实原因
                    # （如注册端点 403），必须落进 session，否则只剩笼统的「授权未完成」。
                    # 回调层已给出更精确原因时（如「未收到授权码」）不覆盖。
                    if session.status == "pending":
                        session.status = "error"
                        session.error = f"{type(e).__name__}: {e}"
                    logger.warning(f"[MCP-OAuth] {session.key} 授权流程异常: {type(e).__name__}: {e}")
                finally:
                    # 提前 return / 抛异常时显式收尾 generator，避免悬挂的 async generator 警告。
                    await flow.aclose()

        try:
            asyncio.run(drive())
        finally:
            restore_handlers()
        return opened["url"]

    # ── 状态查询与登出 ─────────────────────────────────

    def get_session(self, plugin_name: str, server_name: str) -> Optional[OAuthSession]:
        return self._sessions.get(self.make_key(plugin_name, server_name))

    def has_token(self, plugin_name: str, server_name: str) -> bool:
        """该 server 是否已有可用 token（用于设置卡显示「已授权」）。"""
        storage = PluginMcpTokenStore(plugin_name, server_name)
        blob = storage._read_blob()
        token = blob.get("token")
        if not isinstance(token, dict) or not token.get("access_token"):
            return False
        expires_at = blob.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at <= 0:
            return False
        return True

    def revoke(self, plugin_name: str, server_name: str) -> bool:
        """清除本地 token 与 client info（登出）。

        注意：这只是删本地凭证，不会通知授权服务器作废 token。
        """
        key = self.make_key(plugin_name, server_name)
        self._providers.pop(key, None)
        self._sessions.pop(key, None)
        storage = PluginMcpTokenStore(plugin_name, server_name)
        ok = storage._write_blob({})
        logger.info(f"[MCP-OAuth] {key} 凭证已清除" if ok else f"[MCP-OAuth] {key} 凭证清除失败")
        return ok
