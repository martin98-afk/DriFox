# -*- coding: utf-8 -*-
"""
Gitee OAuth 账号绑定

通过 OAuth 网页授权绑定 Gitee 账号：
1. 启动本地 HTTP 回调服务器
2. 打开浏览器跳转 Gitee 授权页
3. 用户授权后回调到 localhost
4. 用授权码换取 access_token
5. 获取用户信息，检查/创建仓库
6. 存储 token 到本地配置
"""

import html
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from loguru import logger

from app.utils.config import Settings

# ── OAuth 常量 ────────────────────────────────────────────
GITEE_AUTHORIZE_URL = "https://gitee.com/oauth/authorize"
GITEE_TOKEN_URL = "https://gitee.com/oauth/token"
GITEE_USER_URL = "https://gitee.com/api/v5/user"
GITEE_REPO_URL = "https://gitee.com/api/v5/repos/{owner}/{repo}"

# OAuth 回调地址（必须与 Gitee 注册的 OAuth 应用回调地址完全一致）
REDIRECT_PORT = 18923
REDIRECT_URI = "http://localhost:18923/callback"

FIXED_REPO_NAME = "DriFox_uploads"


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
    启动 OAuth 授权流程（阻塞，约 30s 超时）

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

        # 4. 用授权码换取 access_token
        resp = requests.post(
            GITEE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            err = _parse_error(resp)
            return False, f"换取 Token 失败：{err}"

        token_data = resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return False, "换取 Token 失败：响应中缺少 access_token"

        logger.info("[GiteeOAuth] 获取 access_token 成功")

        # 5. 获取用户信息
        user_resp = requests.get(
            GITEE_USER_URL,
            params={"access_token": access_token},
            timeout=10,
        )
        if user_resp.status_code != 200:
            err = _parse_error(user_resp)
            return False, f"获取用户信息失败：{err}"

        user = user_resp.json()
        owner = user.get("login")
        if not isinstance(owner, str) or not owner:
            return False, "获取用户信息失败：login 字段无效"

        logger.info(f"[GiteeOAuth] 用户: {owner}")

        # 6. 检查/创建仓库
        repo_ok, repo_msg = _ensure_repo(access_token, owner, FIXED_REPO_NAME, repo_private)
        if not repo_ok:
            return False, repo_msg

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
    """确保仓库存在，不存在则创建"""
    # 先检查仓库是否存在
    check_resp = requests.get(
        GITEE_REPO_URL.format(owner=owner, repo=repo),
        params={"access_token": token},
        timeout=10,
    )
    if check_resp.status_code == 200:
        # 仓库存在 → 检查可见性是否匹配，不匹配则更新
        existing = check_resp.json()
        current_private = existing.get("private", False)
        if current_private != private:
            logger.info(f"[GiteeOAuth] 仓库已存在，更新可见性: {owner}/{repo} private={private}")
            patch_resp = requests.patch(
                GITEE_REPO_URL.format(owner=owner, repo=repo),
                data={
                    "access_token": token,
                    "name": repo,
                    "private": "true" if private else "false",
                },
                timeout=10,
            )
            if patch_resp.status_code == 200:
                return True, f"仓库已存在，可见性已更新：{owner}/{repo}"
            else:
                err = _parse_error(patch_resp)
                logger.warning(f"[GiteeOAuth] 更新可见性失败: {err}")
                return False, f"更新仓库可见性失败：{err}"
        logger.info(f"[GiteeOAuth] 仓库已存在: {owner}/{repo}，复用")
        return True, f"复用已有仓库：{owner}/{repo}"

    # 创建仓库（先创建，再通过 PATCH 设置可见性）
    logger.info(f"[GiteeOAuth] 创建仓库: {owner}/{repo}")
    create_resp = requests.post(
        "https://gitee.com/api/v5/user/repos",
        data={
            "access_token": token,
            "name": repo,
            "private": "false",  # 先以默认创建
            "description": "DriFox 自动创建的上传仓库",
            "auto_init": "true",
        },
        timeout=15,
    )
    if create_resp.status_code not in (201, 200):
        err = _parse_error(create_resp)
        return False, f"创建仓库失败：{err}"

    # 通过 PATCH 设置最终可见性
    if private:
        patch_resp = requests.patch(
            GITEE_REPO_URL.format(owner=owner, repo=repo),
            data={"access_token": token, "name": repo, "private": "true"},
            timeout=10,
        )
        if patch_resp.status_code != 200:
            err = _parse_error(patch_resp)
            logger.warning(f"[GiteeOAuth] 设置私有失败: {err}")
            return False, f"设置仓库可见性失败：{err}"

    logger.info(f"[GiteeOAuth] 仓库创建成功: {owner}/{repo} (private={private})")
    return True, f"仓库已创建：{owner}/{repo}"


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


def _parse_error(resp) -> str:
    """解析 Gitee API 错误响应"""
    try:
        body = resp.json()
        return body.get("message", body.get("error_description", resp.text[:200]))
    except Exception:
        return resp.text[:200]
