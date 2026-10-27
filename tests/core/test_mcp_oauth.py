# -*- coding: utf-8 -*-
"""MCP OAuth 核心回归测试。

覆盖三块：
1. token_store —— OAuthToken / OAuthClientInformationFull 的存取往返、损坏数据降级、
   键名前缀隔离（不污染用户配置键）
2. callback_server —— 起停、redirect_uri 端口分配、GET /callback 收码、
   错误分支、超时、非 /callback 路径 404
3. mcp_tools 接线 —— _plugin_name_from_source 反推、_get_oauth_provider 对
   非 OAuth 配置返回 None

不覆盖真实 OAuth 网络流程（需要授权服务器与浏览器），那部分靠手动联调。
"""

import json
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.mcp_oauth.callback_server import CallbackServer
from app.core.mcp_oauth.manager import McpOAuthManager
from app.core.mcp_oauth.provider import McpOAuthProviderFactory
from app.core.mcp_oauth.token_store import PluginMcpTokenStore, oauth_storage_key
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """把 app_data_dir 指到临时目录，隔离 token 落盘。"""
    import app.utils.utils as utils_mod

    monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: tmp_path)
    yield tmp_path


def _make_store(plugin: str, server: str, data_dir: Path) -> PluginMcpTokenStore:
    """构造 store 并手动指定路径，绕过 get_app_data_dir 的导入期绑定。"""
    store = PluginMcpTokenStore(plugin, server)
    store._store._path = lambda name: data_dir / "plugin_data" / name / "config.json"  # type: ignore[method-assign]
    return store


# ═══════════════════════════════════════════════════
# token_store
# ═══════════════════════════════════════════════════


class TestTokenStore:
    @pytest.mark.asyncio
    async def test_get_tokens_empty(self, isolated_data_dir):
        store = _make_store("p1", "s1", isolated_data_dir)
        assert await store.get_tokens() is None
        assert await store.get_client_info() is None

    @pytest.mark.asyncio
    async def test_token_roundtrip(self, isolated_data_dir):
        store = _make_store("p1", "s1", isolated_data_dir)
        token = OAuthToken(
            access_token="acc-123",
            token_type="Bearer",
            expires_in=3600,
            refresh_token="ref-456",
            scope="read write",
        )
        await store.set_tokens(token)

        loaded = await store.get_tokens()
        assert loaded is not None
        assert loaded.access_token == "acc-123"
        assert loaded.refresh_token == "ref-456"
        assert loaded.expires_in == 3600

        # expires_at 由本模块额外记录，供 UI 展示与排查
        blob = store._read_blob()
        assert blob["expires_at"] > time.time()

    @pytest.mark.asyncio
    async def test_token_no_expiry(self, isolated_data_dir):
        store = _make_store("p1", "s1", isolated_data_dir)
        await store.set_tokens(OAuthToken(access_token="x"))
        assert "expires_at" not in store._read_blob()

    @pytest.mark.asyncio
    async def test_client_info_roundtrip(self, isolated_data_dir):
        store = _make_store("p1", "s1", isolated_data_dir)
        info = OAuthClientInformationFull(
            client_id="cid-1",
            client_secret="sec-1",
            redirect_uris=["http://127.0.0.1:1234/callback"],
        )
        await store.set_client_info(info)

        loaded = await store.get_client_info()
        assert loaded is not None
        assert loaded.client_id == "cid-1"
        assert loaded.client_secret == "sec-1"

    @pytest.mark.asyncio
    async def test_token_and_client_info_coexist(self, isolated_data_dir):
        """同一 server 的 token 与 client_info 存同一 blob，互不覆盖。"""
        store = _make_store("p1", "s1", isolated_data_dir)
        await store.set_tokens(OAuthToken(access_token="a"))
        await store.set_client_info(
            OAuthClientInformationFull(client_id="c", redirect_uris=["http://127.0.0.1:1/callback"])
        )

        assert (await store.get_tokens()).access_token == "a"
        assert (await store.get_client_info()).client_id == "c"

    @pytest.mark.asyncio
    async def test_corrupt_token_degrades(self, isolated_data_dir):
        """token 字段损坏 → get_tokens 返回 None 而不抛，连接层据此重新授权。"""
        store = _make_store("p1", "s1", isolated_data_dir)
        store._store._write_raw("p1", {store._key: {"token": {"access_token": 123, "bad": "x"}}})
        assert await store.get_tokens() is None

    @pytest.mark.asyncio
    async def test_servers_isolated(self, isolated_data_dir):
        """同插件不同 server 的 token 互不串。"""
        s1 = _make_store("p1", "alpha", isolated_data_dir)
        s2 = _make_store("p1", "beta", isolated_data_dir)
        await s1.set_tokens(OAuthToken(access_token="a"))
        await s2.set_tokens(OAuthToken(access_token="b"))

        assert (await s1.get_tokens()).access_token == "a"
        assert (await s2.get_tokens()).access_token == "b"

    @pytest.mark.asyncio
    async def test_plugins_isolated(self, isolated_data_dir):
        s1 = _make_store("pluginA", "s", isolated_data_dir)
        s2 = _make_store("pluginB", "s", isolated_data_dir)
        await s1.set_tokens(OAuthToken(access_token="a"))
        assert await s2.get_tokens() is None

    @pytest.mark.asyncio
    async def test_does_not_pollute_user_keys(self, isolated_data_dir):
        """token 键带下划线前缀，不与用户配置键混淆。"""
        store = _make_store("p1", "s1", isolated_data_dir)
        store._store.set_values("p1", {"api_key": "user-value"})
        await store.set_tokens(OAuthToken(access_token="a"))

        raw = json.loads((isolated_data_dir / "plugin_data" / "p1" / "config.json").read_text(encoding="utf-8"))
        assert raw["api_key"] == "user-value"
        assert raw[oauth_storage_key("s1")]["token"]["access_token"] == "a"

    def test_storage_key_prefix(self):
        assert oauth_storage_key("notion").startswith("_mcp_oauth_")
        assert oauth_storage_key("notion") == "_mcp_oauth_notion"


# ═══════════════════════════════════════════════════
# callback_server
# ═══════════════════════════════════════════════════


class TestCallbackServer:
    def test_start_returns_redirect_uri_with_port(self):
        server = CallbackServer()
        try:
            uri = server.start()
            assert uri.startswith("http://127.0.0.1:")
            assert uri.endswith("/callback")
            assert server.port > 0
        finally:
            server.close()

    def test_random_port_avoids_collision(self):
        """port=0 由 OS 分配，两个实例不应拿到同一端口。"""
        a, b = CallbackServer(), CallbackServer()
        try:
            a.start()
            b.start()
            assert a.port != b.port
        finally:
            a.close()
            b.close()

    def test_callback_receives_code_and_state(self):
        server = CallbackServer()
        try:
            server.start()
            target = f"{server.redirect_uri}?code=THE_CODE&state=THE_STATE"
            # 在新线程发请求，主线程等待结果
            threading.Thread(
                target=lambda: urllib.request.urlopen(target, timeout=5).read(),
                daemon=True,
            ).start()

            result = server.wait(timeout=5)
            assert result.code == "THE_CODE"
            assert result.state == "THE_STATE"
            assert result.error is None
        finally:
            server.close()

    def test_callback_error_branch(self):
        server = CallbackServer()
        try:
            server.start()
            target = f"{server.redirect_uri}?error=access_denied&error_description=user+refused"
            try:
                urllib.request.urlopen(target, timeout=5).read()
            except urllib.error.HTTPError:
                pass  # 400 是预期

            result = server.wait(timeout=5)
            assert result.code is None
            assert result.error == "access_denied"
            assert result.error_description == "user refused"
        finally:
            server.close()

    def test_callback_missing_code(self):
        server = CallbackServer()
        try:
            server.start()
            try:
                urllib.request.urlopen(server.redirect_uri, timeout=5).read()
            except urllib.error.HTTPError:
                pass

            result = server.wait(timeout=5)
            assert result.code is None
            assert result.error == "missing_code"
        finally:
            server.close()

    def test_non_callback_path_404(self):
        server = CallbackServer()
        try:
            server.start()
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"http://127.0.0.1:{server.port}/other", timeout=5)
            assert exc.value.code == 404
        finally:
            server.close()

    def test_wait_timeout_returns_empty(self):
        server = CallbackServer()
        try:
            server.start()
            start = time.monotonic()
            result = server.wait(timeout=0.3)
            assert time.monotonic() - start >= 0.3
            assert result.code is None
        finally:
            server.close()

    def test_close_is_idempotent(self):
        server = CallbackServer()
        server.start()
        server.close()
        server.close()  # 不应抛

    def test_close_stops_listening(self):
        server = CallbackServer()
        server.start()
        port = server.port
        server.close()
        with pytest.raises(Exception):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/callback", timeout=2)


# ═══════════════════════════════════════════════════
# provider factory / manager
# ═══════════════════════════════════════════════════


class TestProviderFactory:
    def test_provider_cached_per_server(self, isolated_data_dir):
        f = McpOAuthProviderFactory()
        p1 = f.get_provider("p", "s", "https://example.com/mcp")
        p2 = f.get_provider("p", "s", "https://example.com/mcp")
        assert p1 is p2

    def test_provider_distinct_per_server(self, isolated_data_dir):
        f = McpOAuthProviderFactory()
        assert f.get_provider("p", "a", "https://x/mcp") is not f.get_provider("p", "b", "https://x/mcp")

    def test_drop_provider_forces_recreate(self, isolated_data_dir):
        f = McpOAuthProviderFactory()
        p1 = f.get_provider("p", "s", "https://x/mcp")
        f.drop_provider("p", "s")
        assert f.get_provider("p", "s", "https://x/mcp") is not p1

    def test_client_name_and_scope_applied(self, isolated_data_dir):
        f = McpOAuthProviderFactory()
        p = f.get_provider("p", "s", "https://x/mcp", client_name="MyApp", scope="read")
        assert p.context.client_metadata.client_name == "MyApp"
        assert p.context.client_metadata.scope == "read"

    def test_basic_auth_strips_client_id_from_body(self, isolated_data_dir):
        """basic 模式下请求体不得带 client_id/client_secret（RFC 6749 §2.3.1）。

        Notion 等严格实现的授权服务器会因 body 与 header 同时出现凭据
        而返回 400 "Client must not use multiple authentication methods"。
        """
        f = McpOAuthProviderFactory()
        p = f.get_provider("p", "s", "https://x/mcp")
        p.context.client_info = OAuthClientInformationFull(
            client_id="CID",
            client_secret="SEC",
            token_endpoint_auth_method="client_secret_basic",
            redirect_uris=["http://127.0.0.1:1/callback"],
        )
        data = {
            "grant_type": "authorization_code",
            "code": "X",
            "code_verifier": "v" * 128,
            "client_id": "CID",
            "redirect_uri": "http://127.0.0.1:1/callback",
        }
        body, headers = p.context.prepare_token_auth(dict(data), {"Content-Type": "application/x-www-form-urlencoded"})

        assert str(headers.get("Authorization", "")).startswith("Basic ")
        assert "client_id" not in body
        assert "client_secret" not in body
        # PKCE 必需字段必须保留
        assert {"code", "code_verifier", "grant_type", "redirect_uri"} <= set(body)

    def test_post_auth_keeps_client_id_in_body(self, isolated_data_dir):
        """client_secret_post 是另一种合法形态，body 带 client_id 不应被干预。

        注意：client_id 由 SDK 的 _exchange_token_authorization_code 先放进
        body，prepare_token_auth 只在此基础上追加认证信息。这里模拟完整链路。
        """
        f = McpOAuthProviderFactory()
        p = f.get_provider("p", "s", "https://x/mcp")
        p.context.client_info = OAuthClientInformationFull(
            client_id="CID",
            client_secret="SEC",
            token_endpoint_auth_method="client_secret_post",
            redirect_uris=["http://127.0.0.1:1/callback"],
        )
        data = {
            "grant_type": "authorization_code",
            "code": "X",
            "code_verifier": "v" * 128,
            "client_id": "CID",
            "redirect_uri": "http://127.0.0.1:1/callback",
        }
        body, headers = p.context.prepare_token_auth(dict(data), {})

        assert "Authorization" not in headers
        assert body["client_id"] == "CID"
        assert body["client_secret"] == "SEC"

    def test_none_auth_untouched(self, isolated_data_dir):
        """公开客户端（无 secret）时 body 保持原样。"""
        f = McpOAuthProviderFactory()
        p = f.get_provider("p", "s", "https://x/mcp")
        p.context.client_info = OAuthClientInformationFull(
            client_id="CID",
            token_endpoint_auth_method="none",
            redirect_uris=["http://127.0.0.1:1/callback"],
        )
        data = {
            "grant_type": "authorization_code",
            "code": "X",
            "code_verifier": "v" * 128,
            "client_id": "CID",
            "redirect_uri": "http://127.0.0.1:1/callback",
        }
        body, headers = p.context.prepare_token_auth(dict(data), {})

        assert headers == {}
        assert body["client_id"] == "CID"

    def test_has_token_false_without_auth(self, isolated_data_dir):
        f = McpOAuthProviderFactory()
        assert f.has_token("noplugin", "noserver") is False

    @pytest.mark.asyncio
    async def test_has_token_true_after_store(self, isolated_data_dir):
        f = McpOAuthProviderFactory()
        store = _make_store("p", "s", isolated_data_dir)
        await store.set_tokens(OAuthToken(access_token="a"))
        assert f.has_token("p", "s") is True

    def test_revoke_clears_everything(self, isolated_data_dir):
        f = McpOAuthProviderFactory()
        f.get_provider("p", "s", "https://x/mcp")
        store = _make_store("p", "s", isolated_data_dir)
        store._store._write_raw("p", {store._key: {"token": {"access_token": "a"}}})

        assert f.revoke("p", "s") is True
        assert store._read_blob() == {}
        assert f.has_token("p", "s") is False


class TestAuthorizationDrive:
    """授权驱动请求与失败原因回归。

    figma / context7 只接受 POST：用裸 GET 驱动时服务端回 405，SDK 静默收尾，
    最终只剩一句「授权未完成」。这里锁定两件事：
    1. 驱动请求必须是带 JSON-RPC initialize body 的 POST
    2. 首请求非 401/403 时必须报出真实状态码，不能退化成「授权未完成」
    """

    def test_probe_request_is_post_initialize(self):
        from app.core.mcp_oauth.provider import _build_probe_request

        req = _build_probe_request("https://mcp.figma.com/mcp")

        assert req.method == "POST"
        assert req.headers["content-type"] == "application/json"
        assert "text/event-stream" in req.headers["accept"]
        body = json.loads(req.content)
        assert body["method"] == "initialize"
        assert body["params"]["protocolVersion"]
        assert body["params"]["clientInfo"]["name"] == "DriFox"

    def test_reject_status_passes_401_and_403(self):
        from app.core.mcp_oauth.provider import _reject_unexpected_status

        assert _reject_unexpected_status(MagicMock(status_code=401)) is None
        assert _reject_unexpected_status(MagicMock(status_code=403)) is None

    def test_reject_status_reports_real_code(self):
        """405 是 figma 裸 GET 时的真实状态码，必须原样出现在原因里。"""
        from app.core.mcp_oauth.provider import _reject_unexpected_status

        reason = _reject_unexpected_status(MagicMock(status_code=405))
        assert reason and "405" in reason

        reason_200 = _reject_unexpected_status(MagicMock(status_code=200))
        assert reason_200 and "200" in reason_200

    def test_authorize_reports_non_401_status(self, isolated_data_dir, monkeypatch):
        """端到端：服务端回 405 时 session.error 必须写明 405。"""
        provider_mod = __import__("app.core.mcp_oauth.provider", fromlist=["provider"])

        class _FakeProvider:
            class _Ctx:
                server_url = "https://only-post.example/mcp"

            def __init__(self):
                from mcp.shared.auth import OAuthClientMetadata

                self.context = _FakeProvider._Ctx()
                self.context.client_metadata = OAuthClientMetadata(
                    redirect_uris=["http://127.0.0.1:1/callback"],
                    client_name="DriFox",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                )

            def async_auth_flow(self, request):
                async def _flow():
                    yield request

                return _flow()

        monkeypatch.setattr(provider_mod.McpOAuthProviderFactory, "get_provider", lambda self, *a, **k: _FakeProvider())

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def send(self, request):
                return MagicMock(status_code=405, request=request)

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        f = McpOAuthProviderFactory(auth_timeout=0.1)
        session = f.authorize("p", "s", "https://only-post.example/mcp")

        assert session.status == "error"
        assert "405" in (session.error or ""), f"错误应写明真实状态码，实际: {session.error}"

    # ── 回归：apify state mismatch 事故（2026-10-25）──────────
    # 根因链：callback_handler 提前置 done 掩盖 token 交换失败 +
    # patch 的 handler 不恢复泄漏给连接流程 + CallbackServer 残留旧回调。

    @staticmethod
    def _make_flow_provider(token_exchange_error: Exception | None = None):
        """构造可驱动完整授权码流程的假 provider。

        flow 序列：首请求(401) → 调 redirect_handler 打开浏览器 →
        调 callback_handler 拿 (code, state) → yield token 请求 →
        可选抛 token_exchange_error → 正常收尾。
        """
        from mcp.shared.auth import OAuthClientMetadata

        class _Ctx:
            server_url = "https://apify.example/mcp"

        class _FakeProvider:
            def __init__(self):
                self.context = _Ctx()
                self.context.client_metadata = OAuthClientMetadata(
                    redirect_uris=["http://127.0.0.1:1/callback"],
                    client_name="DriFox",
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                )

            def async_auth_flow(self, request):
                ctx = self.context

                async def _flow():
                    yield request  # 首请求，调用方回 401

                    await ctx.redirect_handler("https://apify.example/authorize?state=xyz")
                    code, state = await ctx.callback_handler()

                    token_req = MagicMock(name="token_request")
                    token_req.url = "https://apify.example/token"
                    yield token_req  # token 交换请求

                    if token_exchange_error is not None:
                        raise token_exchange_error

                return _flow()

        return _FakeProvider()

    def test_token_exchange_failure_reports_error(self, isolated_data_dir, monkeypatch):
        """token 交换失败（如 Apify invalid_client）必须报 error，不能假报「授权成功」。

        走真实 CallbackServer：webbrowser.open 被顶替为「向回调 server 发
        授权回调请求」，模拟浏览器跳回，让 flow 拿到 code 后进入 token 交换。
        """
        from mcp.client.auth.exceptions import OAuthTokenError

        provider_mod = __import__("app.core.mcp_oauth.provider", fromlist=["provider"])
        fake = self._make_flow_provider(token_exchange_error=OAuthTokenError("Token exchange failed (401): invalid_client"))

        monkeypatch.setattr(provider_mod.McpOAuthProviderFactory, "get_provider", lambda self, *a, **k: fake)

        # 记录 authorize 内部创建的回调 server 地址
        cb_mod = __import__("app.core.mcp_oauth.callback_server", fromlist=["callback_server"])
        holder: dict = {}
        orig_start = cb_mod.CallbackServer.start

        def _patched_start(self):
            uri = orig_start(self)
            holder["uri"] = uri
            return uri

        monkeypatch.setattr(cb_mod.CallbackServer, "start", _patched_start)

        # 顶替 webbrowser.open：模拟浏览器完成授权后跳回回调 server
        import urllib.request

        import app.core.mcp_oauth.provider as prov

        def _fake_browser_open(url, *a, **k):
            callback_url = holder["uri"] + "?code=real-code&state=any"
            with urllib.request.urlopen(callback_url, timeout=5):
                pass
            return True

        monkeypatch.setattr(prov.webbrowser, "open", _fake_browser_open)

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def send(self, request):
                status = 401 if str(request.url).startswith("https://apify.example/mcp") else 200
                return MagicMock(status_code=status, request=request)

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        f = McpOAuthProviderFactory(auth_timeout=10)
        session = f.authorize("p", "s", "https://apify.example/mcp")

        assert session.status == "error", f"token 交换失败必须报 error，实际: {session.status}"
        assert "OAuthTokenError" in (session.error or ""), f"错误须含真实原因，实际: {session.error}"

    def test_authorize_restores_context_handlers(self, isolated_data_dir, monkeypatch):
        """authorize 结束后 provider.context 的 handler 必须恢复原值。

        泄漏的 redirect/callback handler 会让连接流程的 SDK OAuth
        读到已关闭 CallbackServer 的残留回调，复现 apify state mismatch。
        """
        provider_mod = __import__("app.core.mcp_oauth.provider", fromlist=["provider"])
        fake = self._make_flow_provider()

        async def _sentinel_redirect(url):
            pass

        async def _sentinel_callback():
            return "", None

        fake.context.redirect_handler = _sentinel_redirect
        fake.context.callback_handler = _sentinel_callback

        monkeypatch.setattr(provider_mod.McpOAuthProviderFactory, "get_provider", lambda self, *a, **k: fake)

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def send(self, request):
                return MagicMock(status_code=401, request=request)

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        f = McpOAuthProviderFactory(auth_timeout=0.5)
        f.authorize("p", "s", "https://apify.example/mcp")

        assert fake.context.redirect_handler is _sentinel_redirect, "redirect_handler 被残留 patch"
        assert fake.context.callback_handler is _sentinel_callback, "callback_handler 被残留 patch"

    def test_callback_server_close_clears_state(self):
        """close 后必须清掉 result 与 event，残留旧回调会误导复用方。"""
        cb = CallbackServer()
        cb.start()
        cb.result.code = "old-code"
        cb.result.state = "old-state"
        cb._event.set()

        cb.close()

        assert cb.result.code is None
        assert cb.result.state is None
        assert not cb._event.is_set()


class TestManagerSingleton:
    def test_singleton(self):
        a = McpOAuthManager.get_instance()
        assert a is McpOAuthManager.get_instance()

    def test_revoke_delegates(self, isolated_data_dir, monkeypatch):
        m = McpOAuthManager.get_instance()
        store = _make_store("p", "s", isolated_data_dir)
        m._factory._providers[m._factory.make_key("p", "s")] = object()  # type: ignore[assignment]
        store._store._write_raw("p", {store._key: {"token": {"access_token": "a"}}})

        assert m.revoke("p", "s") is True
        assert m._factory.make_key("p", "s") not in m._factory._providers


# ═══════════════════════════════════════════════════
# mcp_tools 接线
# ═══════════════════════════════════════════════════


class TestMcpToolsWiring:
    def test_plugin_name_from_source(self):
        from app.tools.mcp_tools import _plugin_name_from_source

        cfg = {"_source": r"D:\plugins\my-plugin\.mcp.json"}
        assert _plugin_name_from_source(cfg) == "my-plugin"

    def test_plugin_name_from_source_missing(self):
        from app.tools.mcp_tools import _plugin_name_from_source

        assert _plugin_name_from_source({}) == ""
        assert _plugin_name_from_source({"_source": "claude_desktop"}) == ""

    def test_get_oauth_provider_none_for_non_oauth(self):
        from app.tools.mcp_tools import _get_oauth_provider

        assert _get_oauth_provider({}) is None
        assert _get_oauth_provider({"auth": {}}) is None
        assert _get_oauth_provider({"auth": {"type": "bearer"}}) is None
        assert _get_oauth_provider({"auth": "oauth"}) is None

    def test_get_oauth_provider_requires_plugin_source(self):
        from app.tools.mcp_tools import _get_oauth_provider

        with pytest.raises(ValueError, match="必须来自插件"):
            _get_oauth_provider({"auth": {"type": "oauth"}, "url": "https://x/mcp"})

    def test_get_oauth_provider_builds_provider(self, isolated_data_dir):
        from app.tools.mcp_tools import _get_oauth_provider

        cfg = {
            "name": "notion",
            "url": "https://mcp.notion.com/mcp",
            "auth": {"type": "oauth"},
            "_source": r"D:\plugins\my-plugin\.mcp.json",
        }
        provider = _get_oauth_provider(cfg)
        assert provider is not None
        assert provider.context.server_url == "https://mcp.notion.com/mcp"
