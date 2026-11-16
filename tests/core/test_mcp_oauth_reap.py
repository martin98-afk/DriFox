# -*- coding: utf-8 -*-
"""MCP OAuth 收尾即清（A3）预写测试 — TDD 红绿分离。

裁决：授权会话进入终态（done / error / cancelled）后必须立即从
factory._sessions 移除（收尾即清）；pending 会话全程存活（get_session 可见、
cancel 可达）；revoke 幂等；provider 断连（drop_provider）移除内存态。

红/绿分类（修复前基线，2026-11-14 实测）：
- 红（功能缺失，修复后转绿）：test_error_session_reaped_after_authorize
  / test_cancelled_session_reaped_after_authorize
- 绿（语义保全，修复前后都必须过）：test_pending_session_stays_alive
  / test_revoke_idempotent / test_drop_provider_disconnect_removal

手法对齐 test_mcp_oauth.py：isolated_data_dir 隔离落盘、
_make_flow_provider 假 provider 驱动全流程、monkeypatch 顶替 get_provider。
"""

import threading
from unittest.mock import MagicMock

import pytest

from app.core.mcp_oauth.manager import McpOAuthManager
from app.core.mcp_oauth.provider import McpOAuthProviderFactory, OAuthSession

SERVER_URL = "https://reap.example/mcp"


def _make_fake_client():
    """假 httpx.AsyncClient：server_url 回 401，token 端点回 200（对齐既有手法）。"""

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, request):
            status = 401 if str(request.url).startswith(SERVER_URL) else 200
            return MagicMock(status_code=status, request=request)

    return _FakeClient


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """把 app_data_dir 指到临时目录，隔离 token 落盘（对齐既有手法）。"""
    import app.utils.utils as utils_mod

    monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def _reset_manager_singleton():
    McpOAuthManager._instance = None
    yield
    McpOAuthManager._instance = None


def _make_store(plugin: str, server: str, data_dir):
    from app.core.mcp_oauth.token_store import PluginMcpTokenStore

    store = PluginMcpTokenStore(plugin, server)
    store._store._path = lambda name: data_dir / "plugin_data" / name / "config.json"  # type: ignore[method-assign]
    return store


def _patch_httpx(monkeypatch):
    """顶替 httpx.AsyncClient，隔绝真实网络（对齐既有 _FakeClient 手法）。"""
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _make_fake_client())


def _patch_wait_cancelled(monkeypatch):
    """CallbackServer.wait 直返 cancelled 结果（复刻用户中止后的真实返回）。

    不 patch provider.context handler：authorize 内部的 _run_authorization 会
    把它们替换成产品 handler，测试侧预先赋值会被覆盖（已实测踩坑）。
    """
    import app.core.mcp_oauth.callback_server as cb_mod

    def _cancelled_wait(self, timeout):
        return cb_mod.CallbackResult(cancelled=True)

    monkeypatch.setattr(cb_mod.CallbackServer, "wait", _cancelled_wait)


def _patch_browser_noop(monkeypatch):
    """webbrowser.open 置 no-op，防真开浏览器副作用。"""
    import app.core.mcp_oauth.provider as prov

    monkeypatch.setattr(prov.webbrowser, "open", lambda url, *a, **k: True)


def _make_flow_provider(on_redirect=None, token_exchange_error=None):
    """假 provider 驱动完整授权码流程（对齐既有 _make_flow_provider 手法）。

    flow 序列：首请求（调用方回 401）→ redirect_handler（可选钩子）→
    callback_handler 拿 (code, state) → token 交换请求 → 可选抛异常收尾。
    """
    from mcp.shared.auth import OAuthClientMetadata

    class _Ctx:
        server_url = SERVER_URL

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

                await ctx.redirect_handler("https://reap.example/authorize?state=xyz&code=real-code")
                code, state = await ctx.callback_handler()

                token_req = MagicMock(name="token_request")
                token_req.url = "https://reap.example/token"
                yield token_req

                if token_exchange_error is not None:
                    raise token_exchange_error

            return _flow()

    return _FakeProvider()


def _patch_browser_to_callback(monkeypatch, holder):
    """顶替 webbrowser.open：向真实 CallbackServer 发授权回调（模拟浏览器跳回）。"""
    import urllib.request

    import app.core.mcp_oauth.provider as prov

    def _fake_browser_open(url, *a, **k):
        uri = holder.get("uri")
        if uri:
            parsed = urllib.parse.urlparse(uri)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            callback_url = f"http://127.0.0.1:{parsed.port}/callback?code={q['code'][0]}&state={q.get('state', [''])[0]}"
            with urllib.request.urlopen(callback_url, timeout=5):
                pass
        return True

    monkeypatch.setattr(prov.webbrowser, "open", _fake_browser_open)


def _patch_callback_start(monkeypatch, holder):
    """记录 authorize 内部创建的回调 server 地址（对齐既有手法）。"""
    import app.core.mcp_oauth.callback_server as cb_mod

    orig_start = cb_mod.CallbackServer.start

    def _patched_start(self):
        uri = orig_start(self)
        holder["uri"] = uri
        return uri

    monkeypatch.setattr(cb_mod.CallbackServer, "start", _patched_start)


# ═══════════════════════════════════════════════════
# A3-1【红】失败回收
# ═══════════════════════════════════════════════════


def test_error_session_reaped_after_authorize(isolated_data_dir, monkeypatch):
    """授权以 error 收尾后，session 必须从 factory._sessions 移除（收尾即清）。

    修复前：终态 session 残留 dict → get_session 返回 error 会话 → 红。
    """
    from mcp.client.auth.exceptions import OAuthTokenError

    provider_mod = __import__("app.core.mcp_oauth.provider", fromlist=["provider"])

    fake = _make_flow_provider(token_exchange_error=OAuthTokenError("Token exchange failed (401): invalid_client"))
    monkeypatch.setattr(provider_mod.McpOAuthProviderFactory, "get_provider", lambda self, *a, **k: fake)

    holder: dict = {}
    _patch_callback_start(monkeypatch, holder)
    _patch_browser_to_callback(monkeypatch, holder)
    _patch_httpx(monkeypatch)

    f = McpOAuthProviderFactory(auth_timeout=5.0)
    session = f.authorize("p", "s", SERVER_URL)

    assert session.status == "error", f"token 交换失败应 error，实际 {session.status}"
    assert session._callback is None, "回调 server 引用必须收尾释放（语义保全）"
    # 收尾即清：终态 session 不得残留
    assert f.get_session("p", "s") is None, (
        f"error 终态 session 必须收尾即清，实际残留: {f.get_session('p', 's')!r}"
    )


# ═══════════════════════════════════════════════════
# A3-2【绿】pending 存活
# ═══════════════════════════════════════════════════


def test_pending_session_stays_alive(isolated_data_dir):
    """进行中的授权会话必须全程可见：get_session 返回同一对象、cancel 可达。

    修复「收尾即清」时不得误清 pending 态（语义保全）。
    """
    from app.core.mcp_oauth.callback_server import CallbackServer

    f = McpOAuthProviderFactory()
    session = OAuthSession(key=f.make_key("p", "s"))
    session._callback = CallbackServer()  # 未 start，不占端口；cancel 只置标志
    f._sessions[session.key] = session

    got = f.get_session("p", "s")
    assert got is session, "pending 会话必须对 get_session 可见"
    assert got.status == "pending"

    # cancel 可达：真的中止了一个等待中的会话
    assert f.cancel("p", "s") is True, "pending + callback 在位时 cancel 必须生效"
    assert f.get_session("p", "s") is session, "cancel 置标志后 pending 会话仍由授权线程收尾，不得被 cancel 侧误清"

    # 对照：无会话时 cancel 返回 False
    f._sessions.clear()
    assert f.cancel("p", "s") is False


# ═══════════════════════════════════════════════════
# A3-3【红】取消回收
# ═══════════════════════════════════════════════════


def test_cancelled_session_reaped_after_authorize(isolated_data_dir, monkeypatch):
    """用户取消后（status=cancelled 收尾），session 必须收尾即清。

    修复前：cancelled 会话残留 dict → 红。
    """
    provider_mod = __import__("app.core.mcp_oauth.provider", fromlist=["provider"])
    f = McpOAuthProviderFactory(auth_timeout=5.0)

    # 真实取消路径：产品 callback_handler 从 callback.wait 拿到 cancelled=True
    # → 置 session.status="cancelled" → 返回 ("", None) → SDK 后续异常不覆盖终态。
    fake = _make_flow_provider()
    monkeypatch.setattr(provider_mod.McpOAuthProviderFactory, "get_provider", lambda self, *a, **k: fake)
    _patch_httpx(monkeypatch)
    _patch_wait_cancelled(monkeypatch)
    _patch_browser_noop(monkeypatch)  # 产品 redirect_handler 会开浏览器，置 no-op

    session = f.authorize("p", "s", SERVER_URL)

    assert session.status == "cancelled", f"取消路径应收尾为 cancelled，实际 {session.status}"
    assert f.get_session("p", "s") is None, (
        f"cancelled 终态 session 必须收尾即清，实际残留: {f.get_session('p', 's')!r}"
    )


# ═══════════════════════════════════════════════════
# A3-4【绿】revoke 幂等
# ═══════════════════════════════════════════════════


def test_revoke_idempotent(isolated_data_dir):
    """revoke 连续调用幂等：不抛异常、结果稳定、凭证与内存态一致清空。"""
    f = McpOAuthProviderFactory()
    f.get_provider("p", "s", SERVER_URL)  # 占位 provider 进缓存
    store = _make_store("p", "s", isolated_data_dir)
    store.set_tokens_sync = None  # 防 IDE 误引用；真实写入走 _write_blob
    store._store._write_raw("p", {store._key: {"token": {"access_token": "acc-1"}}})
    assert f.has_token("p", "s") is True

    assert f.revoke("p", "s") is True
    assert f.has_token("p", "s") is False
    assert store._read_blob() == {}
    assert f.make_key("p", "s") not in f._providers, "revoke 必须同时移除 provider 缓存"
    assert f.get_session("p", "s") is None

    # 幂等：第二次 revoke 不抛异常、返回 bool、状态不变
    assert f.revoke("p", "s") in (True, False), "重复 revoke 必须返回 bool 而非抛异常"
    assert f.has_token("p", "s") is False
    assert store._read_blob() == {}


# ═══════════════════════════════════════════════════
# A3-5【绿】provider 断连移除
# ═══════════════════════════════════════════════════


def test_drop_provider_disconnect_removal(isolated_data_dir):
    """连接断开 drop_provider 移除内存态；重连重建新实例（token 落盘不受影响）。"""
    f = McpOAuthProviderFactory()
    key = f.make_key("p", "s")

    p1 = f.get_provider("p", "s", SERVER_URL)
    assert key in f._providers

    f.drop_provider("p", "s")
    assert key not in f._providers, "断连后 provider 内存态必须移除"

    p2 = f.get_provider("p", "s", SERVER_URL)
    assert key in f._providers
    assert p2 is not p1, "重建应为新实例（不复活旧内存态）"

    # 对照：drop 不存在的 key 幂等无异常
    f.drop_provider("ghost", "server")
