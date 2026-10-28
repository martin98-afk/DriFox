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


class LoginCancelled(Exception):
    """用户在 DriFox 界面主动中止了登录（区别于超时与服务端报错）。"""


def _sleep_or_cancel(seconds: float, cancel_event: Any) -> None:
    """可中断睡眠：cancel_event 置位后立即抛 LoginCancelled。

    用 wait() 而不是 sleep()：中止后能马上退出轮询循环，
    否则一个 poll_interval 内点了中止也得等完这一轮。
    """
    if cancel_event is None:
        time.sleep(seconds)
        return
    if cancel_event.wait(seconds):
        raise LoginCancelled("登录已中止")

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


_SSL_CONTEXT = None  # 进程级缓存；False 哨兵 = 已探测且 truststore 不可用


def _system_ssl_context():
    """系统证书链 SSLContext（信任 macOS 钥匙串/Windows 证书存储，兼容独立 CLI 运行）；
    truststore 不可用时回退 None（走默认验证）。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        try:
            import ssl

            import truststore

            _SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            _SSL_CONTEXT = False
    return _SSL_CONTEXT or None


def _friendly_ssl_error(exc: Exception) -> str:
    """把证书校验类网络错误翻译成带指引的人话；其余原样透出。"""
    reason = str(getattr(exc, "reason", "") or exc)
    if "CERTIFICATE_VERIFY_FAILED" in reason or "SSL" in reason.upper():
        return (
            f"TLS 证书校验失败（{reason}）。常见于企业网络/代理/抓包工具拦截 HTTPS"
            "（自签名证书未受信任）；请在系统信任库信任对应证书或更换网络重试"
        )
    return str(exc)


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
        with urllib.request.urlopen(req, timeout=timeout, context=_system_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"code": -1, "message": f"HTTP {exc.code}: {raw[:200]}"}
    except urllib.error.URLError as exc:
        raise RuntimeError(_friendly_ssl_error(exc)) from exc


def _payload_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """取 {code, message, data} 包裹的 data 段；兼容顶层平铺（字段路径待真机校准）。"""
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def login(
    timeout_sec: float = LOGIN_TIMEOUT,
    poll_interval: float = LOGIN_POLL_INTERVAL,
    open_browser: bool = True,
    cancel_event: Any = None,
) -> Dict[str, Any]:
    """轮询式登录：弹浏览器授权后本地轮询换取凭据（不起本地端口）。

    Args:
        cancel_event: 可选 threading.Event。置位后立即抛 LoginCancelled，
            不必等到 timeout_sec（用户关掉授权页时 UI 能马上退出等待）。

    返回 {"access_token", "refresh_token", "expires_in", "account"}；
    超时抛 TimeoutError，state 异常抛 RuntimeError，中止抛 LoginCancelled。
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
        if cancel_event is not None and cancel_event.is_set():
            raise LoginCancelled("登录已中止")
        payload = _request("GET", f"{AUTH_TOKEN_PATH}?state={state}")
        if payload.get("code") == CODE_TOKEN_NOT_READY:
            _sleep_or_cancel(poll_interval, cancel_event)
            continue
        token = _payload_data(payload)
        # 真机实测（2026-09）：字段为驼峰 accessToken/refreshToken（非蛇形）；
        # token 一次性：code=0 只闪现一次，state 随即失效，取空后无法重试
        access_token = token.get("accessToken") or token.get("access_token", "")
        refresh_token = token.get("refreshToken") or token.get("refresh_token", "")
        if access_token:
            break
        _sleep_or_cancel(poll_interval, cancel_event)
    if not access_token:
        raise TimeoutError("登录超时：未在限时内取到 token")

    # account 未就绪不阻塞登录（12151 容错）；实测该端点需带 Authorization，
    # 否则网关 401（失败只影响展示信息）
    account: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise LoginCancelled("登录已中止")
        payload = _request(
            "GET", f"{LOGIN_ACCOUNT_PATH}?state={state}", access_token=access_token
        )
        if payload.get("code") == CODE_ACCOUNT_NOT_READY:
            _sleep_or_cancel(poll_interval, cancel_event)
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


# ── 成长任务 / 活跃上报 / 猫旅行（2026-10 真机校准）─────────────────────────
#
# 协议来源：workbuddy2api-panel（Go，MIT 精神参照）逆向成果 + 本机真机验证：
#   - 报活跃 POST billing/v2/report（事件数组，eventCode=chat_request_send，必带 userId）
#     → 点亮连登 + 解锁 first_buddy；一条即可，多报只对 chat_5 计数有用
#       （本机实测：1 条 200 后 streak 0→1，5 条后 chat_5 5/5）
#   - 任务列表 GET chat/v2/activity/growth/tasks；accept 同域 POST
#   - 领奖必须走 Web 域 POST web/activity/growth/tasks/<code>/claim + x-client-platform: web
#     （chat 域同名路径 400 task not completed，这是领奖长期失败的真正原因）
#   - 桌面端任务（RichMeow/Buddy_App/automation/Library_read/template/playbook/canvas/
#     Hp_Appearance）靠同一 /v2/report 通道换客户端指纹（WorkBuddy 桌面 UA +
#     extName=workbuddy-desktop 事件族）点亮
#   - 专家类（expert_5 / Expert_team_use_3 / skill_1 / Expert_lighthouse）要求真实
#     专家 id + 真实对话的服务端 requestId（自造 id/UUID 一律不计数）
#
# 身份口径：X-User-Id 取 access_token 的 JWT sub（本机实测报活跃即生效）。

BILLING_ENDPOINT = "https://www.codebuddy.cn"
WEB_ENDPOINT = "https://www.workbuddy.cn"
CLI_UA = "WorkBuddy/5.5.4"
DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

REPORT_PATH = "/v2/report"
GROWTH_TASKS_PATH = "/v2/activity/growth/tasks"
GROWTH_ACCEPT_PATH = "/v2/activity/growth/tasks/accept"
GROWTH_CLAIM_TMPL = "/activity/growth/tasks/{code}/claim"  # Web 域，code 在路径
GROWTH_STREAK_PATH = "/activity/growth/streak"
GROWTH_REDEEM_PATH = "/activity/growth/redeem"
LOTTERY_SUMMARY_PATH = "/activity/growth/lottery/summary"
LOTTERY_DRAW_PATH = "/activity/growth/lottery/draw"
BUDDY_INFO_PATH = "/activity/growth/buddy/info"
BUDDY_AGREEMENT_PATH = "/activity/growth/buddy/agreement"
BUDDY_FIRST_PATH = "/activity/growth/buddy/first"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"
CLAIM_GIFT_PATH = "/v2/billing/meter/claim-gift"
APPEARANCE_SET_PATH = "/v2/user-asset/appearance/set"
EXPERT_LIST_PATH = "/portal/operation-platform/market/expert/list"
CHAT_COMPLETIONS_PATH = "/v2/chat/completions"

# 领养门槛未达标（HTTP 400）的非错误形态：当日活跃没落定，等下轮即可
_BUDDY_GATE_MARKER = "first_buddy task not completed yet"


def parse_jwt_claims(access_token: str) -> Dict[str, Any]:
    """解 JWT payload 段（不验签，仅供身份字段提取）；失败返回空表。"""
    import base64

    try:
        part = access_token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part.encode()).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (IndexError, ValueError, TypeError):
        return {}


def uid_from_access_token(access_token: str) -> str:
    """从 access_token 取账号 uid（JWT sub）；取不到返回空串。

    登录接口也会返回 account.uid，但 DriFox 只缓存 token；实测 sub 与 uid 等价
    （报活跃 userId 用 sub 立即点亮连登）。
    """
    claims = parse_jwt_claims(access_token)
    return str(claims.get("sub") or claims.get("user_id") or claims.get("uid") or "")


def _cli_headers(access_token: str, uid: str, origin: str = BILLING_ENDPOINT) -> Dict[str, str]:
    """growth/billing 域 CLI 口径头（伪装 WorkBuddy 桌面 UID 归属）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": CLI_UA,
        "X-CodeBuddy-Request": "1",
        "Accept-Language": "zh-CN",
        "Authorization": f"Bearer {access_token}",
        "X-User-Id": uid,
        "X-Domain": DOMAIN,
    }


def _web_headers(access_token: str, uid: str, page_url: str) -> Dict[str, str]:
    """Web 成长中心口径头：领奖/页面行为事件专用（缺 x-client-platform 会 400）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": WEB_ENDPOINT,
        "Referer": page_url or (WEB_ENDPOINT + "/profile/growth-center"),
        "x-client-platform": "web",
        "User-Agent": WEB_UA,
        "Authorization": f"Bearer {access_token}",
        "X-User-Id": uid,
        "X-Domain": DOMAIN,
    }


def _api_request(
    method: str,
    url: str,
    body: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """发请求并解析 JSON；HTTP 4xx/5xx 也读 body（业务码判定依赖 400 响应体）。

    与 _request 的区别：url 完整给出（跨三个域），headers 由调用方构造。
    """
    import urllib.error  # 延迟导入：对齐插件 AST 审计约束
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_system_ssl_context()) as resp:
            raw = resp.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {"code": -1, "message": f"非 JSON 响应: {raw[:200]!r}"}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"code": exc.code, "message": raw[:200]}
        if isinstance(payload, dict):
            payload.setdefault("code", exc.code)
        return payload
    except urllib.error.URLError as exc:
        raise RuntimeError(_friendly_ssl_error(exc)) from exc


def _ok(payload: Any) -> bool:
    """业务成功判定：{code: 0} 为成功（HTTP 层错误已被 _api_request 折成非 0）。"""
    return isinstance(payload, dict) and payload.get("code", -1) == 0


def _data(payload: Any) -> Dict[str, Any]:
    """取 data 段；缺失返回空表（领奖/签到响应形状不一时容错）。"""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
    return {}


# ── 事件构造 ──────────────────────────────────────────────


def chat_activity_event(
    uid: str,
    conversation_id: str,
    request_id: str = "",
    model_id: str = "deepseek-v4-flash",
    model_name: str = "DeepSeek V4 Flash",
) -> Dict[str, Any]:
    """CLI 口径 chat_request_send 事件（字段全给，勿精简：少字段会被上游后续加严）。"""
    now = int(time.time() * 1000)
    req_id = request_id or conversation_id
    return {
        "eventCode": "chat_request_send",
        "timestamp": now,
        "reportDelay": 0,
        "mode": "craft",
        "conversationId": conversation_id,
        "requestId": req_id,
        "inputLength": 12,
        "requestModelId": model_id,
        "requestModelName": model_name,
        "isPlan": False,
        "isAutoExecuteTerminal": False,
        "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0,
        "maxSteps": 0,
        "temperature": 0,
        "maxRetries": 0,
        "mentionContexts": [],
        "knowledgeId": [],
        "knowledgeName": [],
        "codebaseId": "",
        "mentionContextCount": 0,
        "command": "",
        "expertId": "",
        "recommendId": "",
        "skillId": "",
        "skillCount": 0,
        "totalCount": 0,
        "fileUri": "",
        "presentAt": now,
        "traceId": "",
        "rootRequestId": req_id,
        "parentConversationId": conversation_id,
        "agentName": "default",
        "agentType": "conversation",
        "userId": uid,
    }


def _derive_id(uid: str, salt: str) -> str:
    """按 uid 稳定派生 36 hex 设备标识（同账号跨重启恒定，模拟固定机器）。"""
    import hashlib

    return hashlib.sha256(f"drifox:{salt}:{uid}".encode("utf-8")).hexdigest()[:36]


def desktop_fingerprint(uid: str, nickname: str = "") -> Dict[str, Any]:
    """桌面客户端公共指纹（注入每个桌面事件；业务字段可覆盖同名字段）。"""
    now = int(time.time() * 1000)
    return {
        "timezone": "Asia/Shanghai",
        "reportDelay": 2000,
        "userId": uid,
        "username": nickname,
        "userNickname": nickname,
        "product": "SaaS",
        "releaseDate": 1789036585355,
        "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
        "ideName": "WorkBuddy",
        "ideType": "WorkBuddy",
        "ideVersion": "5.5.6",
        "machineId": _derive_id(uid, "machine"),
        "sessionId": _derive_id(uid, "session"),
        "extName": "workbuddy-desktop",
        "extVersion": "5.5.6",
        "os": "win32",
        "arch": "x64",
        "osVersion": "10.0.26220",
        "cpuCores": 20,
        "memorySize": 24,
        "timestamp": now,
        "presentAt": now,
    }


def desktop_chat_sequence(
    conversation_id: str,
    request_id: str,
    message_id: str,
    model_id: str = "fast-model",
    model_name: str = "fast-model",
) -> list:
    """桌面端一次完整对话事件链（agent_task_created → chat_response，6 事件）。

    点亮 RichMeow_Chat（桌面端对话 1 次），也是 template/playbook/canvas/expert
    事件组的 JOIN 基础。
    """
    now = int(time.time() * 1000)

    def mk(code: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        ev = {"eventCode": code}
        ev.update(extra)
        return ev

    return [
        mk(
            "agent_task_created",
            {
                "source": "LOCAL",
                "name": "working",
                "task_target": "local",
                "mode": "craft",
                "requestModelId": model_id,
                "requestModelName": model_name,
                "has_repo": False,
                "repo_type": "none",
                "workspace_type": "empty",
                "has_connector": False,
                "connector_types": [],
                "has_mention": False,
                "mention_types": [],
                "has_template": False,
                "action": "",
                "template_name": "",
                "has_expert": False,
                "expert_id": "",
                "expert_name": "",
                "expert_industry_id": "",
                "has_skill": False,
                "skill_names": [],
                "conversationId": conversation_id,
                "messageId": message_id,
                "buddyId": "",
                "buddyName": "",
            },
        ),
        mk(
            "chat_message_send",
            {
                "messageId": message_id + "-assistant",
                "historyCount": 0,
                "isContextTruncated": False,
                "currentStepCount": 1,
                "traceId": request_id,
                "rootRequestId": request_id,
                "parentConversationId": conversation_id,
                "agentName": "cli",
                "agentType": "main",
            },
        ),
        mk(
            "chat_request_send",
            {
                "inputLength": 24,
                "isPlan": False,
                "isAutoExecuteTerminal": False,
                "isAutoModify": False,
                "codebaseEnable": False,
                "maxToken": 0,
                "maxSteps": 500,
                "temperature": 0,
                "maxRetries": 0,
                "mentionContexts": [],
                "knowledgeId": [],
                "knowledgeName": [],
                "codebaseId": "",
                "mentionContextCount": 0,
                "command": "",
                "recommendId": "",
                "skillId": "",
                "skillCount": 0,
                "totalCount": 0,
                "traceId": request_id,
                "rootRequestId": request_id,
                "parentConversationId": conversation_id,
                "agentName": "cli",
                "agentType": "main",
                "codebuddy.session_id": conversation_id,
                "codebuddy.conversation_request_id": request_id,
            },
        ),
        mk(
            "chat_message_response",
            {
                "messageId": message_id + "-assistant",
                "responseModelId": model_id,
                "inputToken": 120,
                "outputToken": 80,
                "totalToken": 200,
                "cachedTokens": 0,
                "cachedWriteTokens": 0,
                "cachedMissTokens": 0,
                "isSuccessful": True,
                "messageErrorCode": "",
                "finishReason": "stop",
                "firstTokenAt": now,
                "traceId": request_id,
                "conversationId": conversation_id,
                "rootRequestId": request_id,
                "parentConversationId": conversation_id,
                "agentName": "cli",
                "agentType": "main",
                "codebuddy.session_id": conversation_id,
                "codebuddy.conversation_request_id": request_id,
            },
        ),
        mk(
            "chat_message_status",
            {
                "messageId": message_id + "-assistant",
                "messageErrorCode": "0",
                "traceId": request_id,
                "rootRequestId": request_id,
                "parentConversationId": conversation_id,
                "agentName": "cli",
                "agentType": "main",
            },
        ),
        mk(
            "chat_request_response",
            {
                "mode": "craft",
                "toolCallCount": 0,
                "inputToken": 120,
                "outputToken": 80,
                "totalToken": 200,
                "cachedTokens": 0,
                "cachedWriteTokens": 0,
                "cachedMissTokens": 0,
                "isSuccessful": True,
                "messageErrorCode": "",
                "finishReason": "stop",
                "rootRequestId": request_id,
                "parentConversationId": conversation_id,
            },
        ),
    ]


def desktop_buddy_app_sequence(buddy_id: str, buddy_name: str) -> list:
    """Buddy 应用进入五连事件（同时满足 Buddy_App 与 Buddy_App_QQ 判据）。"""

    def mk(code: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ev: Dict[str, Any] = {
            "eventCode": code,
            "mode": "LOCAL",
            "buddyId": buddy_id,
            "buddyName": buddy_name,
        }
        ev.update(extra or {})
        return ev

    return [
        mk("buddyapp_discover_click"),
        mk("buddyapp_show", {"elementId": buddy_id, "elementName": buddy_name, "position": 2}),
        mk(
            "buddyapp_enter_click",
            {
                "elementId": buddy_id,
                "elementName": buddy_name,
                "position": 2,
                "isFirstPage": "1",
            },
        ),
        mk("buddyapp_auth_confirm_click", {"elementId": buddy_id, "elementName": buddy_name}),
        mk("buddyapp_bindaccount_skip_click", {"elementId": buddy_id, "elementName": buddy_name}),
    ]


def desktop_automation_event(name: str) -> Dict[str, Any]:
    """「定时任务创建成功」事件（点亮 automation_1，无需真建任务）。"""
    return {
        "eventCode": "automated_task_create_suc",
        "name": name,
        "source": "manually",
        "modelId": "fast-model",
        "modelIsThinking": True,
        "connectorCount": 0,
        "skills": "",
        "skillCount": 0,
        "scheduleType": "once",
        "mode": "LOCAL",
    }


def desktop_template_sequence(
    conversation_id: str, request_id: str, template_id: str, template_name: str
) -> list:
    """「用模板创建任务」事件组（chat 链 + with_template + template_used）。"""
    events = desktop_chat_sequence(
        conversation_id, request_id, "msg-" + template_id, "fast-model", "fast-model"
    )
    events.append(
        {
            "eventCode": "agent_task_created_with_template",
            "mode": "working",
            "isCustomModel": False,
            "id": template_id,
            "name": template_name,
            "requestId": request_id,
        }
    )
    events.append({"eventCode": "template_used", "template_id": template_id, "task_mode": "working"})
    return events


def desktop_playbook_sequence(
    conversation_id: str, request_id: str, case_id: str, case_name: str
) -> list:
    """「灵感案例做同款」事件组（判据是 playbook_prompt_send）。"""
    events = desktop_chat_sequence(
        conversation_id, request_id, "msg-pb", "fast-model", "fast-model"
    )
    payload = {
        "id": case_id,
        "name": case_name,
        "type": "document",
        "categoryId": "",
        "categoryName": "",
    }
    events.append(
        {
            "eventCode": "web_element_click",
            "pageName": "playbook_detail",
            "elementId": "playbook_ctaClick",
            "elementName": case_name,
            "source": "discover",
        }
    )
    ev = {"eventCode": "playbook_cta_click", "source": "discover", "position": 0}
    ev.update(payload)
    events.append(ev)
    ev = {
        "eventCode": "playbook_prompt_send",
        "conversationId": conversation_id,
        "requestId": request_id,
    }
    ev.update(payload)
    events.append(ev)
    return events


def desktop_canvas_sequence(conversation_id: str, request_id: str) -> list:
    """「设计创意画布」事件组（点亮 create_canvas，+300）。"""
    events = desktop_chat_sequence(
        conversation_id, request_id, "msg-canvas", "fast-model", "fast-model"
    )
    events.append(
        {
            "eventCode": "wbx_design_canvas_task_create",
            "conversationId": conversation_id,
            "requestId": request_id,
            "source": "summon_keyword",
            "cost": 12000,
            "isSuccessful": True,
        }
    )
    events.append(
        {
            "eventCode": "wbx_design_canvas_open",
            "conversationId": conversation_id,
            "requestId": request_id,
            "id": "ardot-file-" + request_id[-8:],
            "source": "summon_keyword",
            "type": "page",
            "cost": 13000,
            "isSuccessful": True,
        }
    )
    return events


def _first_category(expert: Dict[str, Any]) -> str:
    cats = expert.get("categories")
    if isinstance(cats, list) and cats and isinstance(cats[0], str):
        return cats[0]
    return "expert-all"


def desktop_expert_summon_sequence(expert: Dict[str, Any]) -> list:
    """召唤平台专家事件组（web_element_click + summon_click + summoned）。"""
    expert_id = str(expert.get("expert_id") or "")
    name = str(expert.get("display_name_zh") or "")
    title = str(expert.get("profession_zh") or "")
    ver = str(expert.get("version") or "1.0.0")
    cat = _first_category(expert)
    return [
        {
            "eventCode": "web_element_click",
            "source": expert_id,
            "type": cat,
            "version": ver,
            "elementId": "expert_summon_click",
            "elementName": "立即召唤",
            "pageURL": "/C:/Program%20Files/WorkBuddy/resources/app.asar/renderer/index.html",
        },
        {
            "eventCode": "expert_summon_click",
            "id": expert_id,
            "name": name,
            "expertTitle": title,
            "type": "expert-all",
            "position": 0,
            "expertType": str(expert.get("expert_type") or ""),
            "version": ver,
            "mode": "LOCAL",
        },
        {
            "eventCode": "expert_summoned",
            "id": expert_id,
            "name": name,
            "expertTitle": title,
            "type": "expert-all",
        },
    ]


def desktop_expert_actual_use(
    expert: Dict[str, Any], conversation_id: str, request_id: str, mode: str = "craft"
) -> Dict[str, Any]:
    """「专家真实使用」事件；requestId 必须是真实对话的服务端 id（自造不计数）。"""
    expert_id = str(expert.get("expert_id") or "")
    return {
        "eventCode": "expert_actual_use",
        "id": expert_id,
        "name": str(expert.get("display_name_zh") or ""),
        "expertTitle": str(expert.get("profession_zh") or ""),
        "type": _first_category(expert),
        "expertType": str(expert.get("expert_type") or ""),
        "source": "builtin",
        "version": str(expert.get("version") or "1.0.0"),
        "cost": 9000,
        "characterCount": 14,
        "conversationId": conversation_id,
        "requestId": request_id,
        "messageId": "msg-" + request_id[-8:],
        "requestModelId": "fast-model",
        "requestModelName": "fast-model",
        "mode": mode,
    }


# ── 上报 / 任务 / 成长中心 API ────────────────────────────


def report_activity(
    access_token: str,
    uid: str,
    count: int = 1,
    model_id: str = "deepseek-v4-flash",
    model_name: str = "DeepSeek V4 Flash",
    gap: float = 1.2,
) -> Dict[str, Any]:
    """上报 N 条对话活跃事件（CLI 口径，billing 域）。

    一条点亮当日连登 + 解锁 first_buddy；chat_5 要 5 条，故按需补条。
    返回 {"ok": 成功条数, "last": 末次响应}。
    """
    ok = 0
    last: Dict[str, Any] = {}
    for i in range(max(1, count)):
        ms = int(time.time() * 1000)
        conv = f"drifox-{ms}-{i}"
        last = _api_request(
            "POST",
            BILLING_ENDPOINT + REPORT_PATH,
            [chat_activity_event(uid, conv, conv, model_id, model_name)],
            _cli_headers(access_token, uid),
        )
        if _ok(last):
            ok += 1
        if i < count - 1:
            time.sleep(gap)
    return {"ok": ok, "last": last}


def report_desktop_events(
    access_token: str, uid: str, nickname: str, events: list
) -> Dict[str, Any]:
    """以桌面客户端指纹上报事件组（chat 域 /v2/report）。"""
    if not events:
        return {"code": -1, "message": "no events"}
    fp = desktop_fingerprint(uid, nickname)
    payload = []
    for ev in events:
        merged = dict(fp)
        merged.update(ev)
        payload.append(merged)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": DESKTOP_UA,
        "X-Domain": DOMAIN,
        "X-Product": "SaaS",
        "X-User-Id": uid,
    }
    return _api_request("POST", ENDPOINT + REPORT_PATH, payload, headers)


def report_web_event(
    access_token: str,
    uid: str,
    event_code: str,
    page_url: str,
    element_id: str,
    element_name: str,
) -> Dict[str, Any]:
    """以 Web 端指纹上报页面行为事件（点亮 Library_read 等）。"""
    ev = {
        "eventCode": event_code,
        "timestamp": int(time.time() * 1000),
        "reportDelay": 0,
        "pageURL": page_url,
        "elementId": element_id,
        "elementName": element_name,
        "os": "Win32",
        "arch": "",
        "osVersion": "10.0",
        "userAgent": WEB_UA,
        "machineId": _derive_id(uid, "webmachine"),
        "userId": uid,
    }
    return _api_request(
        "POST",
        WEB_ENDPOINT + REPORT_PATH,
        [ev],
        _web_headers(access_token, uid, page_url),
    )


def growth_tasks(access_token: str, uid: str) -> list:
    """拉取成长任务全量列表（含 accept_status 与 progress）。"""
    payload = _api_request(
        "GET", ENDPOINT + GROWTH_TASKS_PATH, None, _cli_headers(access_token, uid)
    )
    tasks = _data(payload).get("tasks")
    return tasks if isinstance(tasks, list) else []


def accept_tasks(access_token: str, uid: str, codes: list) -> Dict[str, Any]:
    """批量接受任务（报名，不产生进度）；返回 {code: status} 映射。"""
    if not codes:
        return {}
    payload = _api_request(
        "POST",
        ENDPOINT + GROWTH_ACCEPT_PATH,
        {"task_codes": list(codes)},
        _cli_headers(access_token, uid),
    )
    raw = _data(payload).get("results")
    out: Dict[str, Any] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("task_code"):
                out[str(item["task_code"])] = item
    return out


def claim_reward(access_token: str, uid: str, task_code: str) -> Dict[str, Any]:
    """领取任务奖励（Web 域，task_code 在路径）；already_claimed 视为成功。

    返回 {"ok": bool, "credit": int, "energy": int, "already_claimed": bool}。
    """
    payload = _api_request(
        "POST",
        WEB_ENDPOINT + GROWTH_CLAIM_TMPL.format(code=task_code),
        None,
        _web_headers(access_token, uid, WEB_ENDPOINT + "/profile/growth-center"),
    )
    data = _data(payload)
    return {
        "ok": _ok(payload) or bool(data.get("already_claimed")),
        "credit": int(data.get("credit") or 0),
        "energy": int(data.get("energy") or 0),
        "already_claimed": bool(data.get("already_claimed")),
        "raw": payload,
    }


def growth_streak(access_token: str, uid: str) -> Dict[str, Any]:
    """连登状态（days / 档位解锁情况 / 抽奖次数来源）。"""
    return _data(
        _api_request("GET", ENDPOINT + GROWTH_STREAK_PATH, None, _cli_headers(access_token, uid))
    )


def redeem_tier(access_token: str, uid: str, tier: str) -> Dict[str, Any]:
    """兑换连登档位（tier: 7d/14d/28d）；未解锁返回非 0 业务码，调用方跳过。"""
    token = _uuid_token()
    return _api_request(
        "POST",
        ENDPOINT + GROWTH_REDEEM_PATH,
        {"tier": tier, "client_token": token},
        _cli_headers(access_token, uid),
    )


def lottery_summary(access_token: str, uid: str) -> Dict[str, Any]:
    """抽奖次数与模块开关。"""
    return _data(
        _api_request("GET", ENDPOINT + LOTTERY_SUMMARY_PATH, None, _cli_headers(access_token, uid))
    )


def lottery_draw(access_token: str, uid: str) -> Dict[str, Any]:
    """抽奖一次（消耗 1 次机会）。"""
    return _api_request(
        "POST",
        ENDPOINT + LOTTERY_DRAW_PATH,
        {"client_token": _uuid_token()},
        _cli_headers(access_token, uid),
    )


def buddy_info(access_token: str, uid: str) -> Dict[str, Any]:
    """当前 Buddy 档案；data.buddy 为 null 表示尚未领养。"""
    return _data(
        _api_request("GET", ENDPOINT + BUDDY_INFO_PATH, None, _cli_headers(access_token, uid))
    )


def buddy_agreement(access_token: str, uid: str) -> Dict[str, Any]:
    """同意领养协议（幂等）。"""
    return _api_request(
        "POST", ENDPOINT + BUDDY_AGREEMENT_PATH, {"agree": True}, _cli_headers(access_token, uid)
    )


def buddy_adopt(access_token: str, uid: str) -> Dict[str, Any]:
    """领养首只 Buddy（+300 积分 +8 能量）；前置是当日活跃已落定。"""
    payload = _api_request(
        "POST", ENDPOINT + BUDDY_FIRST_PATH, {}, _cli_headers(access_token, uid)
    )
    data = _data(payload)
    return {
        "ok": _ok(payload),
        "credit": int(data.get("credit") or 0),
        "energy": int(data.get("energy") or 0),
        "gated": _BUDDY_GATE_MARKER in json.dumps(payload, ensure_ascii=False),
        "raw": payload,
    }


def travel_status(access_token: str, uid: str) -> Dict[str, Any]:
    """猫猫旅行状态（state: idle/traveling/arrived + record_id + 奖励）。"""
    return _data(
        _api_request("GET", ENDPOINT + TRAVEL_STATUS_PATH, None, _cli_headers(access_token, uid))
    )


def travel_depart(access_token: str, uid: str, location_id: int = 1) -> Dict[str, Any]:
    """派出猫旅行（每日一次，location_id 实测 1~4 收益相同）。"""
    return _api_request(
        "POST",
        ENDPOINT + TRAVEL_DEPART_PATH,
        {"location_id": location_id},
        _cli_headers(access_token, uid),
    )


def travel_claim(access_token: str, uid: str, record_id: int) -> Dict[str, Any]:
    """领取旅行到站奖励。"""
    payload = _api_request(
        "POST",
        ENDPOINT + TRAVEL_CLAIM_PATH,
        {"record_id": record_id},
        _cli_headers(access_token, uid),
    )
    return {"ok": _ok(payload), "credit": int(_data(payload).get("reward_credit") or 0)}


def claim_gift(access_token: str, uid: str) -> Dict[str, Any]:
    """领取新手礼包（每号一次，已领返回业务错误，按幂等处理）。"""
    payload = _api_request(
        "POST", BILLING_ENDPOINT + CLAIM_GIFT_PATH, {}, _cli_headers(access_token, uid)
    )
    return {"ok": _ok(payload), "credit": int(_data(payload).get("credit") or 0)}


def set_appearance_theme(access_token: str, uid: str, resource_key: str) -> Dict[str, Any]:
    """应用外观主题（Hp_Appearance 判据的留痕动作，需配 appearance_skin_apply 事件）。"""
    body = {"kind": "theme", "resource_key": resource_key}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": DESKTOP_UA,
        "X-Domain": DOMAIN,
        "X-Product": "SaaS",
        "X-User-Id": uid,
    }
    return _api_request("POST", ENDPOINT + APPEARANCE_SET_PATH, body, headers)


def market_expert_list(
    access_token: str, uid: str, expert_type: str = "", keyword: str = ""
) -> list:
    """拉取专家市场真实专家列表（expert_actual_use 要求 id 真实存在）。

    keyword 为服务端搜索词（实测有效：按专家名精确搜索返回 1 条），
    用于按任务标题定位「无畏契约拼豆专家」这类没有 expertId 的指定专家。
    """
    body: Dict[str, Any] = {
        "page": 1,
        "page_size": 20,
        "sort_by": "reco_rank",
        "sort_order": "desc",
    }
    if expert_type:
        body["expert_type"] = expert_type
    if keyword:
        body["keyword"] = keyword
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": DESKTOP_UA,
        "X-Domain": DOMAIN,
        "X-Product": "SaaS",
        "X-User-Id": uid,
    }
    payload = _api_request("POST", ENDPOINT + EXPERT_LIST_PATH, body, headers)
    experts = _data(payload).get("experts")
    return experts if isinstance(experts, list) else []


def chat_once(
    access_token: str,
    uid: str,
    prompt: str = "1+1等于几？直接回答。",
    model_id: str = "fast-model",
    expert_id: str = "",
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """发一条真实对话并从 SSE 抓服务端 requestId（专家/夜间任务判据依赖它）。

    返回 {"ok": bool, "conversation_id": str, "request_id": str, "error": str}。
    requestId 必须是服务端返回的真实 id，自造 UUID 不计数。
    """
    import re
    import urllib.error
    import urllib.request

    conv = f"drifox-conv-{int(time.time() * 1000)}"
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": DESKTOP_UA,
        "X-Domain": DOMAIN,
        "X-Product": "SaaS",
        "X-User-Id": uid,
        "X-Conversation-ID": conv,
        "X-Request-ID": str(int(time.time() * 1e9)),
        "X-Agent-Intent": "craft",
        "X-Agent-Type": "main",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Version": "5.5.6",
        "x-codebuddy-request": "1",
    }
    if expert_id:
        headers["X-Expert-Id"] = expert_id
    req = urllib.request.Request(
        ENDPOINT + CHAT_COMPLETIONS_PATH,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    id_re = re.compile(r"^(cmb-)?[0-9a-f]{32}$")
    buf = b""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_system_ssl_context()) as resp:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                buf += chunk
                for match in re.finditer(rb'"id":"([^"]+)"', buf):
                    cand = match.group(1).decode("utf-8", "replace")
                    if id_re.match(cand):
                        return {"ok": True, "conversation_id": conv, "request_id": cand, "error": ""}
                if len(buf) > 1 << 20:
                    break
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return {"ok": False, "conversation_id": conv, "request_id": "", "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:  # 网络中断/超时都不该让整个每日流程崩
        return {"ok": False, "conversation_id": conv, "request_id": "", "error": str(exc)}
    return {"ok": False, "conversation_id": conv, "request_id": "", "error": "SSE 未返回 requestId"}


def _uuid_token() -> str:
    """前端同款 16 字节随机 token（兑换/抽奖的幂等令牌）。"""
    import uuid

    return str(uuid.uuid4())



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
