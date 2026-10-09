# -*- coding: utf-8 -*-
"""GitHub Copilot 认证客户端 — Device Flow 登录 / Copilot token 换取（纯标准库）。

协议来源：GitHub Copilot 官方客户端（VSCode copilot-chat）公开使用的 OAuth
device flow，与社区多个开源实现（copilot.vim / copilot-gpt4-service 系）一致：

- Device Flow  POST https://github.com/login/device/code（client_id 为 Copilot
               Plugin 公开 client_id）→ 返回 device_code / user_code / interval
- 用户授权     打开 https://github.com/login/device，输入 user_code
- 轮询令牌     POST https://github.com/login/oauth/access_token（grant_type=
               device_code）；authorization_pending 继续等，slow_down +5s
- 换取会话令牌 GET https://api.github.com/copilot_internal/v2/token
               （Authorization: token <gho_...>）→ 短效 Copilot token（约 30 分钟）
- 聊天端点     https://api.githubcopilot.com/chat/completions（OpenAI 兼容）

风险提示：非官方客户端协议实现，存在违反服务条款与封号风险，请自行评估。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Dict, Optional

GITHUB_ENDPOINT = "https://github.com"
COPILOT_API_ENDPOINT = "https://api.githubcopilot.com"
COPILOT_TOKEN_ENDPOINT = "https://api.github.com/copilot_internal/v2/token"

# Copilot Plugin 的公开 OAuth client_id（社区实现通用值）
CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_CODE_PATH = "/login/device/code"
TOKEN_PATH = "/login/oauth/access_token"
DEVICE_AUTH_URI = "https://github.com/login/device"

USER_AGENT = "GitHubCopilotChat/0.20.2"
EDITOR_VERSION = "vscode/1.95.0"
EDITOR_PLUGIN_VERSION = "copilot-chat/0.20.2"
COPILOT_INTEGRATION_ID = "vscode-chat"

DEFAULT_TIMEOUT = 30.0
LOGIN_TIMEOUT = 280.0  # 编辑卡 InfoBar 文案承诺 5 分钟内
LOGIN_POLL_INTERVAL = 5.0
SLOW_DOWN_EXTRA = 5.0


def build_chat_headers(copilot_token: str) -> Dict[str, str]:
    """构造 Copilot 网关聊天请求头（身份伪装 + 鉴权；缺 Integration-Id 会 403）。"""
    return {
        "Authorization": f"Bearer {copilot_token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Editor-Version": EDITOR_VERSION,
        "Editor-Plugin-Version": EDITOR_PLUGIN_VERSION,
        "Copilot-Integration-Id": COPILOT_INTEGRATION_ID,
    }


def _post_form(url: str, form: Dict[str, str], timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"error": "http_error", "error_description": f"HTTP {exc.code}: {raw[:200]}"}
    except (urllib.error.URLError, OSError) as exc:
        raise _wrap_net_error(exc) from exc


def start_device_flow() -> Dict[str, Any]:
    """发起 Device Flow：返回 {device_code, user_code, verification_uri, interval}。"""
    payload = _post_form(
        f"{GITHUB_ENDPOINT}{DEVICE_CODE_PATH}",
        {"client_id": CLIENT_ID, "scope": "read:user"},
    )
    if not payload.get("device_code"):
        raise RuntimeError(f"device/code 响应异常: {payload}")
    return payload


def _wrap_net_error(exc: Exception) -> RuntimeError:
    """把底层网络异常包装成人话（URLError 原文对用户不友好且被 InfoBar 截断）"""
    return RuntimeError(f"GitHub 连接失败（{exc.__class__.__name__}）：请检查网络/代理是否能访问 github.com")


def poll_access_token(device_code: str, timeout_sec: float = LOGIN_TIMEOUT) -> str:
    """轮询换取 GitHub access token（gho_）；用户取消/超时抛 TimeoutError。"""
    deadline = time.time() + timeout_sec
    interval = LOGIN_POLL_INTERVAL
    while time.time() < deadline:
        payload = _post_form(
            f"{GITHUB_ENDPOINT}{TOKEN_PATH}",
            {
                "client_id": CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        err = payload.get("error", "")
        if err == "authorization_pending":
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval += SLOW_DOWN_EXTRA
            time.sleep(interval)
            continue
        if err:
            raise RuntimeError(f"GitHub 授权失败: {payload.get('error_description') or err}")
        token = payload.get("access_token", "")
        if not token:
            raise RuntimeError(f"access_token 响应异常: {payload}")
        return token
    raise TimeoutError("等待 GitHub 授权超时（未在期限内完成浏览器授权）")


def fetch_copilot_token(github_token: str, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """按 GitHub access token 换取短效 Copilot token。

    返回 {"token", "expires_at", "endpoints"}；401 = gho 失效需重新登录。
    """
    req = urllib.request.Request(
        COPILOT_TOKEN_ENDPOINT,
        method="GET",
        headers={
            "Authorization": f"token {github_token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Editor-Version": EDITOR_VERSION,
            "Editor-Plugin-Version": EDITOR_PLUGIN_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"换取 Copilot token 失败: HTTP {exc.code}（401 需重新登录）") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise _wrap_net_error(exc) from exc
    if not data.get("token"):
        raise RuntimeError(f"Copilot token 响应异常: {data}")
    return data


def login(timeout_sec: float = LOGIN_TIMEOUT, open_browser: bool = True) -> Dict[str, Any]:
    """完整 Device Flow 登录：打开授权页 + 复制 user_code 到剪贴板 + 轮询。

    优先打开 verification_uri_complete（URL 内嵌 user_code，页面自动填好，
    用户只需点 Continue）；剪贴板作兑底（编码带 BOM，修复 clip.exe 无 BOM
    时静默失败的坑）。返回 {"github_token", "user_code", "verification_uri"}；
    超时抛 TimeoutError。
    """
    flow = start_device_flow()
    user_code = flow.get("user_code", "")
    verification_uri = flow.get("verification_uri") or DEVICE_AUTH_URI
    # 标准 verification_uri_complete：URL 带 code 打开即自动填入输入框
    verification_uri_complete = flow.get("verification_uri_complete") or (
        f"{verification_uri}?user_code={urllib.parse.quote(user_code)}"
    )
    if open_browser:
        webbrowser.open(verification_uri_complete)
    _copy_to_clipboard(user_code)
    github_token = poll_access_token(flow.get("device_code", ""), timeout_sec=timeout_sec)
    return {"github_token": github_token, "user_code": user_code, "verification_uri": verification_uri_complete}


def _copy_to_clipboard(text: str) -> bool:
    """跨平台剪贴板复制（纯外部命令，逐个尝试；全部失败返回 False 不阻塞登录）。

    Windows clip.exe 需带 BOM 的 UTF-16 才能正确识别宽字符（utf-16-le 无 BOM
    会静默失败，用户实测卡在授权页输入框）。"""
    import subprocess

    commands = [
        (["clip"], text.encode("utf-16")),  # Windows clip.exe（UTF-16 带 BOM）
        (["pbcopy"], text.encode("utf-8")),  # macOS
        (["xclip", "-selection", "clipboard"], text.encode("utf-8")),  # X11
        (["wl-copy"], text.encode("utf-8")),  # Wayland
    ]
    for cmd, data in commands:
        try:
            subprocess.run(cmd, input=data, check=True, timeout=5)
            return True
        except Exception:
            continue
    return False
