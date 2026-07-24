# -*- coding: utf-8 -*-
"""
Gitee OAuth 账号绑定（流程编排层）

通过 OAuth 网页授权绑定 Gitee 账号：
1. 启动本地 HTTP 回调服务器
2. 打开浏览器跳转 Gitee 授权页
3. 用户授权后回调到 localhost
4. 用授权码换取 access_token
5. 获取用户信息，检查/创建仓库
6. 存储 token 到本地配置

分层说明：
  - 本模块只负责流程编排（回调服务器、浏览器、等待循环）与配置读写；
  - 所有 Gitee 网络/API 调用位于 app.gateway.auth.gitee（抽象层的平台实现）；
  - 对外统一入口请使用 app.gateway.auth.get_oauth_backend("gitee")。
"""

import html
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

from loguru import logger

from app.gateway.auth import gitee as gitee_api
from app.gateway.auth.base import parse_error as _parse_error  # noqa: F401  向后兼容导出
from app.gateway.auth.gitee import (  # noqa: F401  向后兼容导出
    FIXED_REPO_NAME,
    GITEE_AUTHORIZE_URL,
    GITEE_REPO_URL,
    GITEE_TOKEN_URL,
    GITEE_USER_URL,
    SETTINGS_REPO_NAME,
)
from app.utils.config import Settings

# OAuth 回调地址（必须与 Gitee 注册的 OAuth 应用回调地址完全一致）
REDIRECT_PORT = 18923
REDIRECT_URI = "http://localhost:18923/callback"


# ── 回调 HTTP 服务器 ──────────────────────────────────────


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """处理 Gitee OAuth 回调，提取 authorization code"""

    server_code: Optional[str] = None
    server_error: Optional[str] = None

    def log_message(self, fmt, *args):
        pass  # 静默日志

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/callback":
            code_list = params.get("code", [])
            error_list = params.get("error_description", params.get("error", []))

            if code_list:
                _OAuthCallbackHandler.server_code = code_list[0]
                self._respond(True, "授权成功！您可以关闭此页面。")
            elif error_list:
                _OAuthCallbackHandler.server_error = error_list[0]
                self._respond(False, f"授权失败：{html.escape(_OAuthCallbackHandler.server_error)}")
            else:
                _OAuthCallbackHandler.server_error = "未收到授权码"
                self._respond(False, "未收到授权码，请重试。")
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, success: bool, message: str):
        color = "#07c160" if success else "#fa5151"
        body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>DriFox · Gitee 授权</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display:flex; align-items:center;
         justify-content:center; height:100vh; margin:0; background:#1e1e1e; }}
  .card {{ background:#2d2d2d; padding:32px 40px; border-radius:12px; text-align:center; }}
  h1 {{ color:{color}; margin:0 0 8px 0; font-size:22px; }}
  p  {{ color:#ccc; margin:0; font-size:14px; }}
</style></head>
<body><div class="card"><h1>{html.escape(message)}</h1>
<p>DriFox · Gitee 账号绑定</p></div></body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


# ── OAuth 流程核心 ────────────────────────────────────────


def start_oauth_flow(repo_private: bool) -> Tuple[bool, str]:
    """
    启动 OAuth 授权流程（阻塞，约 120s 超时）

    Args:
        repo_private: 仓库是否私有

    Returns:
        (success, message)
    """
    cfg = Settings.get_instance()
    client_id = cfg.gitee_oauth_client_id.value
    client_secret = cfg.gitee_oauth_client_secret.value

    if not client_id or not client_secret:
        return False, "OAuth 凭证未配置"

    # 1. 启动本地回调服务器
    server = _start_callback_server(REDIRECT_PORT)
    if not server:
        return False, f"无法启动本地回调服务器（端口 {REDIRECT_PORT} 被占用）"

    _OAuthCallbackHandler.server_code = None
    _OAuthCallbackHandler.server_error = None

    try:
        # 2. 构建授权 URL 并打开浏览器
        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "user_info projects",
        }
        auth_url = f"{GITEE_AUTHORIZE_URL}?{urlencode(params)}"
        webbrowser.open(auth_url)

        # 3. 等待回调（最多 120 秒）
        wait_event = threading.Event()
        timeout = 120
        elapsed = 0.0
        while elapsed < timeout:
            if _OAuthCallbackHandler.server_code:
                break
            if _OAuthCallbackHandler.server_error:
                err = _OAuthCallbackHandler.server_error
                return False, f"授权失败：{err}"
            wait_event.wait(0.5)
            elapsed += 0.5

        if not _OAuthCallbackHandler.server_code:
            return False, "等待授权超时（120s），请重试"

        code = _OAuthCallbackHandler.server_code

        # 4. 用授权码换取 access_token（API 层）
        access_token, err = gitee_api.exchange_token(code, client_id, client_secret, REDIRECT_URI)
        if not access_token:
            return False, err

        # 5. 获取用户信息（API 层）
        owner, err = gitee_api.fetch_user_login(access_token)
        if not owner:
            return False, err

        # 6. 检查/创建上传仓库
        repo_ok, repo_msg = _ensure_repo(access_token, owner, FIXED_REPO_NAME, repo_private)
        if not repo_ok:
            return False, repo_msg

        # 6b. 创建配置备份仓库（强制私有，失败不阻断绑定流程）
        settings_ok, settings_msg = _ensure_repo(access_token, owner, SETTINGS_REPO_NAME, private=True)
        if not settings_ok:
            logger.warning(f"[GiteeOAuth] {SETTINGS_REPO_NAME} 创建失败: {settings_msg}（不影响绑定）")

        # 7. 存储到配置
        cfg.gitee_user_token.value = access_token
        cfg.gitee_user_owner.value = owner
        cfg.gitee_user_repo.value = FIXED_REPO_NAME
        cfg.gitee_bound.value = True
        cfg.save()

        logger.info(f"[GiteeOAuth] 绑定完成: {owner}/{FIXED_REPO_NAME}")
        return True, f"绑定成功！仓库：{owner}/{FIXED_REPO_NAME}"

    finally:
        _stop_callback_server(server)


def unbind_account() -> Tuple[bool, str]:
    """解绑 Gitee 账号，清除本地 token"""
    cfg = Settings.get_instance()
    cfg.gitee_bound.value = False
    cfg.gitee_user_token.value = ""
    cfg.gitee_user_owner.value = ""
    cfg.gitee_user_repo.value = FIXED_REPO_NAME
    cfg.save()
    logger.info("[GiteeOAuth] 已解绑")
    return True, "已解绑 Gitee 账号"


def get_bound_info() -> Optional[dict]:
    """获取当前绑定信息，未绑定返回 None"""
    cfg = Settings.get_instance()
    if not cfg.gitee_bound.value:
        return None
    return {
        "owner": cfg.gitee_user_owner.value,
        "repo": cfg.gitee_user_repo.value,
        "token": cfg.gitee_user_token.value,
    }


# ── 内部辅助 ──────────────────────────────────────────────


def _ensure_repo(token: str, owner: str, repo: str, private: bool) -> Tuple[bool, str]:
    """确保仓库存在（委托 API 层实现，保留旧函数名以兼容既有调用与测试）"""
    return gitee_api.ensure_repo(token, owner, repo, private)


def _start_callback_server(port: int) -> Optional[HTTPServer]:
    """启动本地回调服务器"""
    try:
        server = HTTPServer(("127.0.0.1", port), _OAuthCallbackHandler)
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.timeout = 0.5

        def _serve():
            try:
                server.serve_forever()
            except OSError:
                pass  # socket 被关闭时触发，忽略

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        logger.info(f"[GiteeOAuth] 回调服务器已启动: http://localhost:{port}/callback")
        return server
    except OSError as e:
        logger.error(f"[GiteeOAuth] 启动回调服务器失败 (port={port}): {e}")
        return None


def _stop_callback_server(server: Optional[HTTPServer]):
    """关闭回调服务器（仅关闭 socket，不等待 shutdown）"""
    if server:
        try:
            server.socket.close()
        except Exception:
            pass
