# -*- coding: utf-8 -*-
"""
OAuth 平台抽象层 — 基类与通用授权码流程

设计目标：
  - 将「本地回调服务器 → 打开浏览器 → 等待授权码 → 换取 token」这一
    标准 Authorization Code 流程沉淀为通用实现；
  - 平台差异（端点、scope、授权后的初始化动作、凭证存储）由子类实现；
  - 新增平台时只需继承 OAuthBackend 并在 app/gateway/auth/__init__.py 注册。

新增平台步骤（示例见 gitee.py 与 docs/adr 说明文档）：
  1. 定义 OAuthAppConfig（authorize_url / token_url / scope / 回调端口等）
  2. 继承 OAuthBackend，实现 bind / unbind / is_bound / get_bound_info
  3. bind 内部可直接调用 run_authorization_code_flow() 获得 token_data
  4. 在 __init__.py 的 _BACKENDS 注册表中登记
"""

import html
import socket
import threading
import webbrowser
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from loguru import logger


# ── 平台应用配置 ──────────────────────────────────────────


@dataclass
class OAuthAppConfig:
    """一个 OAuth 应用的静态配置（平台端点 + 本应用凭证）"""

    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scope: str = ""
    redirect_port: int = 18923
    callback_path: str = "/callback"
    # 追加到授权 URL 的额外参数（如 GitHub 的 allow_signup 等）
    extra_authorize_params: Dict[str, str] = field(default_factory=dict)

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.redirect_port}{self.callback_path}"


# ── 通用本地回调服务器 ────────────────────────────────────


class _CallbackHTTPServer(HTTPServer):
    """携带授权结果状态的 HTTPServer（避免类属性带来的并发污染）"""

    def __init__(self, *args, callback_path: str = "/callback", brand: str = "DriFox", **kwargs):
        super().__init__(*args, **kwargs)
        self.callback_path = callback_path
        self.brand = brand
        self.auth_code: Optional[str] = None
        self.auth_error: Optional[str] = None


class _CallbackHandler(BaseHTTPRequestHandler):
    """处理 OAuth 回调，提取 authorization code（平台无关）"""

    def log_message(self, fmt, *args):
        pass  # 静默日志

    def do_GET(self):
        server: _CallbackHTTPServer = self.server  # type: ignore[assignment]
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == server.callback_path:
            code_list = params.get("code", [])
            error_list = params.get("error_description", params.get("error", []))

            if code_list:
                server.auth_code = code_list[0]
                self._respond(True, "授权成功！您可以关闭此页面。")
            elif error_list:
                server.auth_error = error_list[0]
                self._respond(False, f"授权失败：{html.escape(server.auth_error)}")
            else:
                server.auth_error = "未收到授权码"
                self._respond(False, "未收到授权码，请重试。")
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, success: bool, message: str):
        server: _CallbackHTTPServer = self.server  # type: ignore[assignment]
        color = "#07c160" if success else "#fa5151"
        body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(server.brand)} · 账号授权</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display:flex; align-items:center;
         justify-content:center; height:100vh; margin:0; background:#1e1e1e; }}
  .card {{ background:#2d2d2d; padding:32px 40px; border-radius:12px; text-align:center; }}
  h1 {{ color:{color}; margin:0 0 8px 0; font-size:22px; }}
  p  {{ color:#ccc; margin:0; font-size:14px; }}
</style></head>
<body><div class="card"><h1>{html.escape(message)}</h1>
<p>{html.escape(server.brand)} · 账号授权</p></div></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


def start_callback_server(port: int, callback_path: str = "/callback") -> Optional[_CallbackHTTPServer]:
    """启动本地回调服务器，失败（端口占用等）返回 None"""
    try:
        server = _CallbackHTTPServer(("127.0.0.1", port), _CallbackHandler, callback_path=callback_path)
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.timeout = 0.5

        def _serve():
            try:
                server.serve_forever()
            except OSError:
                pass  # socket 被关闭时触发，忽略

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        logger.info(f"[OAuth] 回调服务器已启动: http://localhost:{port}{callback_path}")
        return server
    except OSError as e:
        logger.error(f"[OAuth] 启动回调服务器失败 (port={port}): {e}")
        return None


def stop_callback_server(server: Optional[_CallbackHTTPServer]):
    """关闭回调服务器（仅关闭 socket，不等待 shutdown）"""
    if server:
        try:
            server.socket.close()
        except Exception:
            pass


# ── 通用授权码流程 ────────────────────────────────────────


def run_authorization_code_flow(app: OAuthAppConfig, timeout: int = 120) -> Tuple[Optional[dict], str]:
    """
    执行标准 OAuth2 Authorization Code 流程（阻塞）。

    步骤：起回调服务器 → 打开浏览器 → 等待授权码 → 换取 access_token。
    平台无关；平台特有的后续动作（拉用户信息、建仓库等）由调用方完成。

    Returns:
        (token_data, err_msg)：成功时 token_data 为 token 端点返回的 JSON dict、
        err_msg 为空串；失败时 token_data 为 None、err_msg 为错误描述。
    """
    if not app.client_id or not app.client_secret:
        return None, "OAuth 凭证未配置"

    server = start_callback_server(app.redirect_port, app.callback_path)
    if not server:
        return None, f"无法启动本地回调服务器（端口 {app.redirect_port} 被占用）"

    try:
        # 构建授权 URL 并打开浏览器
        params = {
            "client_id": app.client_id,
            "redirect_uri": app.redirect_uri,
            "response_type": "code",
        }
        if app.scope:
            params["scope"] = app.scope
        params.update(app.extra_authorize_params)
        auth_url = f"{app.authorize_url}?{urlencode(params)}"
        webbrowser.open(auth_url)

        # 等待回调
        wait_event = threading.Event()
        elapsed = 0.0
        while elapsed < timeout:
            if server.auth_code:
                break
            if server.auth_error:
                return None, f"授权失败：{server.auth_error}"
            wait_event.wait(0.5)
            elapsed += 0.5

        if not server.auth_code:
            return None, f"等待授权超时（{timeout}s），请重试"

        # 用授权码换取 access_token
        resp = requests.post(
            app.token_url,
            data={
                "grant_type": "authorization_code",
                "code": server.auth_code,
                "client_id": app.client_id,
                "client_secret": app.client_secret,
                "redirect_uri": app.redirect_uri,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None, f"换取 Token 失败：{parse_error(resp)}"

        token_data = resp.json()
        if not token_data.get("access_token"):
            return None, "换取 Token 失败：响应中缺少 access_token"

        logger.info("[OAuth] 获取 access_token 成功")
        return token_data, ""

    finally:
        stop_callback_server(server)


def parse_error(resp) -> str:
    """解析 OAuth / REST API 错误响应，尽量提取可读信息"""
    try:
        body = resp.json()
        return body.get("message", body.get("error_description", resp.text[:200]))
    except Exception:
        return resp.text[:200]


# ── 平台后端抽象基类 ──────────────────────────────────────


class OAuthBackend(ABC):
    """
    OAuth 平台后端抽象基类。

    每个云平台（Gitee / GitHub / Gitea / ...）实现一个子类，
    统一对外提供 绑定 / 解绑 / 查询绑定状态 的能力。
    UI 与业务层只依赖本接口和 get_oauth_backend() 工厂，不感知具体平台。
    """

    #: 平台唯一标识（小写，用于注册表与配置存储）
    name: str = ""
    #: 展示名（UI 用）
    display_name: str = ""

    # ── 必须实现 ──────────────────────────────────────

    @abstractmethod
    def bind(self, **options) -> Tuple[bool, str]:
        """
        执行完整绑定流程（阻塞，建议在工作线程中调用）。

        Args:
            **options: 平台特有选项（如 gitee 的 repo_private）

        Returns:
            (success, message)
        """

    @abstractmethod
    def unbind(self) -> Tuple[bool, str]:
        """解绑账号，清除本地凭证。Returns: (success, message)"""

    @abstractmethod
    def is_bound(self) -> bool:
        """当前是否已绑定该平台账号"""

    @abstractmethod
    def get_bound_info(self) -> Optional[dict]:
        """
        获取绑定信息，未绑定返回 None。

        约定至少包含: owner(账号名)、token(访问凭证)；
        存储类平台还应包含 repo(仓库名)。
        """

    # ── 通用辅助 ──────────────────────────────────────

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} bound={self.is_bound()}>"
