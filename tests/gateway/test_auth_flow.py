# -*- coding: utf-8 -*-
"""
OAuth 通用抽象层测试（base.py）

覆盖:
  - parse_error（错误响应解析）
  - _CallbackHandler（回调 HTTP 服务器处理）
  - start_callback_server / stop_callback_server（启停）
  - run_authorization_code_flow（标准授权码流程，mock 网络）

平台无关逻辑集中在此；Gitee 特有的 bind/unbind/仓库管理见
tests/gateway/test_gitee_backend.py。

Run: pytest tests/gateway/test_auth_flow.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("app.gateway.auth.base")

from app.gateway.auth.base import (  # noqa: E402
    OAuthAppConfig,
    _CallbackHandler,
    parse_error,
    run_authorization_code_flow,
    start_callback_server,
    stop_callback_server,
)


# =============================================================================
# 0. parse_error
# =============================================================================


class TestParseError:
    """错误响应解析（base.parse_error）"""

    def test_parse_error_json(self):
        """JSON 响应应提取 message"""
        resp = MagicMock()
        resp.json.return_value = {"message": "Repository not found"}
        assert parse_error(resp) == "Repository not found"

    def test_parse_error_fallback(self):
        """JSON 解析失败时回退到 text"""
        resp = MagicMock()
        resp.json.side_effect = ValueError("bad json")
        resp.text = "plain text error"
        assert parse_error(resp) == "plain text error"

    def test_parse_error_empty(self):
        """空响应也应有回退"""
        resp = MagicMock()
        resp.json.return_value = {}
        resp.text = ""
        result = parse_error(resp)
        assert isinstance(result, str)


# =============================================================================
# 1. _CallbackHandler
# =============================================================================


class TestCallbackHandler:
    """回调 HTTP 服务器处理（用 mock 实例避免 BaseHTTPRequestHandler 初始化）"""

    @staticmethod
    def _make_handler(path: str = ""):
        """构造一个 _CallbackHandler 实例，server 为轻量替身"""
        server = MagicMock()
        server.auth_code = None
        server.auth_error = None
        server.callback_path = "/callback"
        server.brand = "DriFox"

        with patch.object(_CallbackHandler, "__init__", return_value=None):
            inst = _CallbackHandler.__new__(_CallbackHandler)
            inst.server = server
            inst.path = path
            inst.send_response = MagicMock()
            inst.send_header = MagicMock()
            inst.end_headers = MagicMock()
            inst.wfile = MagicMock()
        return inst, server

    def test_handle_callback_with_code(self):
        """收到授权码时应保存到 server.auth_code"""
        handler, server = self._make_handler("/callback?code=secret_auth_code")
        handler.do_GET()
        assert server.auth_code == "secret_auth_code"
        assert server.auth_error is None

    def test_handle_callback_with_error(self):
        """收到错误时应保存到 server.auth_error"""
        handler, server = self._make_handler(
            "/callback?error=access_denied&error_description=User+cancelled"
        )
        handler.do_GET()
        assert server.auth_error is not None
        assert "access_denied" in server.auth_error or "cancelled" in server.auth_error

    def test_handle_callback_no_code_no_error(self):
        """未收到授权码和错误时应设置 server.auth_error"""
        handler, server = self._make_handler("/callback?some_other_param=value")
        handler.do_GET()
        assert server.auth_error is not None
        assert server.auth_code is None

    def test_handle_unknown_path(self):
        """非 /callback 路径应返回 404"""
        handler, _ = self._make_handler("/other")
        handler.do_GET()
        handler.send_response.assert_called_with(404)


# =============================================================================
# 2. 回调服务器启停
# =============================================================================


class TestCallbackServer:
    """回调服务器的启动和停止（mock 端口层，不操作真实 socket）"""

    def test_start_callback_server_success(self):
        """启动回调服务器应成功"""
        server = start_callback_server(0)
        if server:
            assert server.server_port > 0
            server.server_close()

    def test_start_callback_server_port_busy(self):
        """端口被占用（OSError）时返回 None"""
        with patch("app.gateway.auth.base._CallbackHTTPServer") as mock_cls:
            mock_cls.side_effect = OSError("Address already in use")
            server = start_callback_server(18999)
            assert server is None

    def test_start_callback_server_generic_error(self):
        """非 OSError 异常应向上传播"""
        with patch("app.gateway.auth.base._CallbackHTTPServer") as mock_cls:
            mock_cls.side_effect = RuntimeError("unexpected error")
            with pytest.raises(RuntimeError):
                start_callback_server(18999)

    def test_stop_callback_server_none(self):
        """stop None 应静默跳过"""
        stop_callback_server(None)

    def test_stop_callback_server(self):
        """正常停止服务器（mock 避免真实 socket）"""
        mock_server = MagicMock()
        mock_server.socket.close = MagicMock()
        stop_callback_server(mock_server)
        mock_server.socket.close.assert_called_once()
        stop_callback_server(mock_server)
        assert mock_server.socket.close.call_count == 2


# =============================================================================
# 3. run_authorization_code_flow（通用授权码流程）
# =============================================================================


class TestRunAuthorizationCodeFlow:
    """标准 OAuth2 授权码流程（mock 回调服务器 + 网络）"""

    @staticmethod
    def _app(client_id: str = "cid", client_secret: str = "csecret") -> OAuthAppConfig:
        return OAuthAppConfig(
            client_id=client_id,
            client_secret=client_secret,
            authorize_url="https://example.com/oauth/authorize",
            token_url="https://example.com/oauth/token",
            scope="user_info",
            redirect_port=18923,
            callback_path="/callback",
        )

    def test_missing_credentials(self):
        """凭证未配置应返回错误"""
        token_data, err = run_authorization_code_flow(self._app("", ""))
        assert token_data is None
        assert "凭证未配置" in err

    def test_success(self):
        """完整成功：回调拿到 code → 换取 token_data"""
        mock_server = MagicMock()
        mock_server.auth_code = "auth_code_123"
        mock_server.auth_error = None

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok_xyz", "token_type": "bearer"}

        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server") as mock_stop, \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None), \
             patch("app.gateway.auth.base.requests.post", return_value=token_resp):
            token_data, err = run_authorization_code_flow(self._app())
            assert token_data is not None
            assert token_data["access_token"] == "tok_xyz"
            assert err == ""
            mock_stop.assert_called_once_with(mock_server)

    def test_timeout(self):
        """等待授权超时应返回错误"""
        mock_server = MagicMock()
        mock_server.auth_code = None
        mock_server.auth_error = None

        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server"), \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None):
            token_data, err = run_authorization_code_flow(self._app(), timeout=1)
            assert token_data is None
            assert "超时" in err

    def test_callback_error(self):
        """用户拒绝授权（回调带 error）应返回错误"""
        mock_server = MagicMock()
        mock_server.auth_code = None
        mock_server.auth_error = "user denied"

        with patch("app.gateway.auth.base.start_callback_server", return_value=mock_server), \
             patch("app.gateway.auth.base.webbrowser.open"), \
             patch("app.gateway.auth.base.stop_callback_server"), \
             patch("app.gateway.auth.base.threading.Event.wait", return_value=None):
            token_data, err = run_authorization_code_flow(self._app())
            assert token_data is None
            assert "授权失败" in err
