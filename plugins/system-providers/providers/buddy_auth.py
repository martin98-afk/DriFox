# -*- coding: utf-8 -*-
"""CodeBuddy 认证客户端 — 登录 / 刷新 / 签到（纯标准库，可独立 CLI 运行）。

协议逆向来源：CodeBuddyIDE 1.106.1 客户端（经 deepseek-harness-codearts
项目抓包逆向 + 真实请求实测校准，2026-09）。端点一览：

- 登录  POST /v2/plugin/auth/state?platform=ide → 浏览器授权 →
        轮询 GET /v2/plugin/auth/token?state=...（业务码 11217 = 未就绪）→
        轮询 GET /v2/plugin/login/account?state=...（业务码 12151 = 未就绪）
- 刷新  POST /v2/plugin/auth/token/refresh（refresh_token 走 X-Refresh-Token 头）
- 签到  POST /v2/billing/meter/checkin-activity-status（权威状态源，勿用返回
        占位数据的 checkin-status）→ POST /v2/billing/meter/daily-checkin；
        实测无需图灵盾 X-Device-Token；重复领取 = HTTP 400 + 业务码 10001

响应统一包裹为 {code, message, data}，业务码以响应体为准（不能只看 HTTP 状态）。

风险提示：本模块为非官方客户端协议实现，存在违反服务条款与封号风险，
请自行评估；data 段内字段名未经真机全量校准，异常时优先核对该处。
"""
import json
import time
import webbrowser
from typing import Any, Dict, Optional

ENDPOINT = "https://copilot.tencent.com"
DOMAIN = "copilot.tencent.com"
DEPLOYMENT_TYPE = "SaaS"
PRODUCT_CODE = "codebuddy"
PLATFORM = "ide"
USER_AGENT = "CodeBuddyIDE/1.106.1"

DEFAULT_TIMEOUT = 30.0
LOGIN_TIMEOUT = 300.0
LOGIN_POLL_INTERVAL = 1.0

CODE_OK = 0
CODE_TOKEN_NOT_READY = 11217
CODE_ACCOUNT_NOT_READY = 12151
CODE_ALREADY_CLAIMED = 10001
CODE_ALREADY_CLAIMED_ALT = 1001

AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
LOGIN_ACCOUNT_PATH = "/v2/plugin/login/account"
TOKEN_REFRESH_PATH = "/v2/plugin/auth/token/refresh"
CHECKIN_ACTIVITY_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
DAILY_CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
USER_RESOURCE_PATH = "/v2/billing/meter/get-user-resource"


def build_headers(access_token: str = "", refresh_token: str = "") -> Dict[str, str]:
    """构造 CodeBuddy 网关请求头（身份伪装四件套 + 可选鉴权）。"""
    headers = {
        "X-Domain": DOMAIN,
        "X-Product": DEPLOYMENT_TYPE,
        "X-Product-Code": PRODUCT_CODE,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if refresh_token:
        headers["X-Refresh-Token"] = refresh_token
    return headers


def _request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    access_token: str = "",
    refresh_token: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """发请求并解析 JSON；HTTP 4xx/5xx 也读 body（签到幂等判定依赖 400+10001）。"""
    import urllib.error  # 延迟导入：对齐插件 AST 审计约束（对齐 opencode.py 先例）
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{ENDPOINT}{path}",
        data=data,
        method=method,
        headers=build_headers(access_token, refresh_token),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"code": -1, "message": f"HTTP {exc.code}: {raw[:200]}"}


def _payload_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """取 {code, message, data} 包裹的 data 段；兼容顶层平铺（字段路径待真机校准）。"""
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def login(
    timeout_sec: float = LOGIN_TIMEOUT,
    poll_interval: float = LOGIN_POLL_INTERVAL,
    open_browser: bool = True,
) -> Dict[str, Any]:
    """轮询式登录：弹浏览器授权后本地轮询换取凭据（不起本地端口）。

    返回 {"access_token", "refresh_token", "expires_in", "account"}；
    超时抛 TimeoutError，state 异常抛 RuntimeError。
    """
    state_payload = _request("POST", f"{AUTH_STATE_PATH}?platform={PLATFORM}", body={})
    state_data = _payload_data(state_payload)
    state = state_data.get("state", "")
    auth_url = state_data.get("authUrl", "")
    if not state or not auth_url:
        raise RuntimeError(f"auth/state 响应异常: {state_payload}")
    if open_browser:
        webbrowser.open(auth_url)

    deadline = time.monotonic() + timeout_sec
    access_token = refresh_token = ""
    token: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _request("GET", f"{AUTH_TOKEN_PATH}?state={state}")
        if payload.get("code") == CODE_TOKEN_NOT_READY:
            time.sleep(poll_interval)
            continue
        token = _payload_data(payload)
        # 真机实测（2026-09）：字段为驼峰 accessToken/refreshToken（非蛇形）；
        # token 一次性：code=0 只闪现一次，state 随即失效，取空后无法重试
        access_token = token.get("accessToken") or token.get("access_token", "")
        refresh_token = token.get("refreshToken") or token.get("refresh_token", "")
        if access_token:
            break
        time.sleep(poll_interval)
    if not access_token:
        raise TimeoutError("登录超时：未在限时内取到 token")

    # account 未就绪不阻塞登录（12151 容错）；实测该端点需带 Authorization，
    # 否则网关 401（失败只影响展示信息）
    account: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _request(
            "GET", f"{LOGIN_ACCOUNT_PATH}?state={state}", access_token=access_token
        )
        if payload.get("code") == CODE_ACCOUNT_NOT_READY:
            time.sleep(poll_interval)
            continue
        account = _payload_data(payload)
        break

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": token.get("expiresIn") or token.get("expires_in"),
        "account": account,
    }


def refresh(refresh_token: str) -> Dict[str, Any]:
    """用 refresh_token 静默换新凭据；服务端不回新 refresh_token 时沿用旧值。"""
    payload = _request("POST", TOKEN_REFRESH_PATH, body={}, refresh_token=refresh_token)
    if payload.get("code", CODE_OK) != CODE_OK:
        raise RuntimeError(f"刷新失败: {payload}")
    data = _payload_data(payload)
    # 真机实测（2026-09）：字段为驼峰 accessToken/refreshToken
    return {
        "access_token": data.get("accessToken") or data.get("access_token", ""),
        "refresh_token": data.get("refreshToken") or data.get("refresh_token", "") or refresh_token,
    }


def checkin_status(access_token: str) -> Dict[str, Any]:
    """查询签到活动状态（active / todayCheckedIn / streakDays）。"""
    return _payload_data(
        _request("POST", CHECKIN_ACTIVITY_STATUS_PATH, body={}, access_token=access_token)
    )


def checkin_claim(access_token: str) -> Dict[str, Any]:
    """领取今日积分；幂等：业务码 10001/1001（今日已签）返回 already_claimed。"""
    payload = _request("POST", DAILY_CHECKIN_PATH, body={}, access_token=access_token)
    code = payload.get("code", -1)
    if code in (CODE_ALREADY_CLAIMED, CODE_ALREADY_CLAIMED_ALT):
        return {"already_claimed": True, "raw": payload}
    if code != CODE_OK:
        raise RuntimeError(f"签到失败: {payload}")
    data = _payload_data(payload)
    return {"already_claimed": False, "credit": data.get("credit"), "raw": payload}


def user_resources(access_token: str) -> list:
    """查询积分/套餐资源明细（IDE 积分面板同款接口，逆向自 WorkBuddy 1.106）。

    响应路径 data.Response.Data.Accounts[]，每条含 PackageCode / PackageName /
    CycleCapacitySizePrecise（总量）/ CycleCapacityRemainPrecise（剩余）/
    DeductionEndTime（到期）；签到 bonus 包与套餐包同列。发送积分 = 剩余。
    """
    import datetime

    now = datetime.datetime.now()
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],  # AccountStatus: valid=0 / usedUp=3
        "PackageStartTimeRangeBegin": "2024-12-01 00:00:00",
        "PackageStartTimeRangeEnd": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    payload = _request("POST", USER_RESOURCE_PATH, body=body, access_token=access_token)
    root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return (((root.get("Response") or {}).get("Data") or {}).get("Accounts")) or []


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="CodeBuddy 认证客户端（登录/刷新/签到）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="弹浏览器授权，打印凭据 JSON")
    p_refresh = sub.add_parser("refresh", help="用 refresh_token 换新 access_token")
    p_refresh.add_argument("refresh_token")
    p_checkin = sub.add_parser("checkin", help="查询状态并领取今日积分")
    p_checkin.add_argument("access_token")
    p_credits = sub.add_parser("credits", help="查询积分/套餐余量明细")
    p_credits.add_argument("access_token")
    args = parser.parse_args()

    if args.cmd == "login":
        print(json.dumps(login(), ensure_ascii=False, indent=2))
    elif args.cmd == "refresh":
        print(json.dumps(refresh(args.refresh_token), ensure_ascii=False, indent=2))
    elif args.cmd == "checkin":
        status = checkin_status(args.access_token)
        print("状态:", json.dumps(status, ensure_ascii=False))
        # 真机实测（2026-09）：字段为蛇形 today_checked_in（非驼峰）
        checked = status.get("today_checked_in") or status.get("todayCheckedIn")
        if isinstance(status, dict) and status.get("active") and not checked:
            print("领取:", json.dumps(checkin_claim(args.access_token), ensure_ascii=False))
        else:
            print("跳过领取（活动未开启或今日已签）")
    elif args.cmd == "credits":
        total_left = 0.0
        for r in user_resources(args.access_token):
            total = float(r.get("CycleCapacitySizePrecise") or 0)
            left = float(r.get("CycleCapacityRemainPrecise") or 0)
            if left <= 0:
                continue
            total_left += left
            print(f"{r.get('PackageName') or r.get('PackageCode')}: 剩余 {left} / {total}")
        print(f"合计可用积分: {round(total_left, 1)}")


if __name__ == "__main__":
    _main()
