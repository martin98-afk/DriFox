# -*- coding: utf-8 -*-
"""
套餐用量获取器注册表

不同服务商可以注册各自的套餐用量获取方式。
系统根据当前选中的服务商名称自动选择对应的获取器。

内置获取器：
- OpenCode Zen / Go: 从 opencode.ai/_server 获取（需 server_id/cookie/workspace_id）

如需添加新服务商，只需实现一个 fetcher 函数并 register()。
"""
import json
import time
import urllib.request
import urllib.parse
import re
from typing import Dict, Any, Optional, Callable

# ── 类型 ────────────────────────────────────────────
# fetcher 签名: (provider_config: dict) -> dict | None
# 返回格式: {"rolling": {"percent": int, "reset_sec": int} or None,
#            "weekly": 同上,
#            "monthly": 同上}
# 返回 None 表示该服务商不支持套餐用量查询
CodingPlanFetcher = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]

# ── 注册表 ──────────────────────────────────────────
_fetchers: Dict[str, CodingPlanFetcher] = {}


def register(provider_name: str, fetcher: CodingPlanFetcher):
    """注册一个服务商的套餐用量获取器"""
    _fetchers[provider_name] = fetcher


def get(provider_name: str) -> Optional[CodingPlanFetcher]:
    """获取指定服务商的获取器"""
    return _fetchers.get(provider_name)


def fetch(provider_name: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按服务商名称查询套餐用量（同步，可能阻塞网络）"""
    if provider_name in _fetchers:
        return _fetchers[provider_name](config)

    # 回退：按 family 匹配（如 "OpenCode Zen #2" → "OpenCode Zen"）
    for registered_name in _fetchers:
        if registered_name in provider_name:
            return _fetchers[registered_name](config)

    return None


# ── OpenCode Go 获取器 ─────────────────────────────

def _fetch_opencode_go(config: dict) -> Optional[Dict[str, Any]]:
    """从 opencode.ai/_server 获取 OpenCode Go 套餐用量。

    需要在服务商配置中额外填写 server_id / cookie / workspace_id。
    这些字段不会影响正常的 API 调用，仅用于用量查询。
    """
    server_id = (config.get("server_id", "") or "").strip()
    cookie = (config.get("cookie", "") or "").strip()
    workspace_id = (config.get("workspace_id", "") or "").strip()

    if not server_id or not cookie or not workspace_id:
        return None

    args_obj = {
        "t": {"t": 9, "i": 0, "l": 1, "a": [{"t": 1, "s": workspace_id}], "o": 0},
        "f": 31,
        "m": [],
    }
    args_encoded = urllib.parse.quote(json.dumps(args_obj, separators=(",", ":")))
    url = f"https://opencode.ai/_server?id={server_id}&args={args_encoded}"

    headers = {
        "X-Server-Id": server_id,
        "X-Server-Instance": "server-fn:0",
        "Referer": f"https://opencode.ai/workspace/{workspace_id}/go",
        "Cookie": cookie,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            # 检测编码，兜底用 utf-8 并替换非法字符
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except Exception:
        return None

    # 标准 JSON
    try:
        data = json.loads(raw)
        return _parse_json(data)
    except json.JSONDecodeError:
        pass

    # JavaScript 风格响应
    parsed = _parse_js(raw)
    if parsed.get("rolling") or parsed.get("weekly") or parsed.get("monthly"):
        return parsed

    return None


def _parse_json(data: dict) -> Dict[str, Any]:
    result = {}
    for key, api_key in [("rolling", "rollingUsage"),
                          ("weekly", "weeklyUsage"),
                          ("monthly", "monthlyUsage")]:
        usage = data.get(api_key, {})
        pct = usage.get("usagePercent")
        sec = usage.get("resetInSec")
        result[key] = {"percent": int(pct), "reset_sec": int(sec)} if pct is not None and sec is not None else None
    return result


def _parse_js(raw: str) -> Dict[str, Any]:
    result = {}
    for key, api_key in [("rolling", "rollingUsage"),
                          ("weekly", "weeklyUsage"),
                          ("monthly", "monthlyUsage")]:
        pat = re.compile(
            rf'{api_key}:\$R\[\d+\]=\{{status:"[^"]+",resetInSec:(\d+),usagePercent:(\d+)\}}'
        )
        m = pat.search(raw)
        result[key] = {"percent": int(m.group(2)), "reset_sec": int(m.group(1))} if m else None
    return result


# 注册内置获取器
register("OpenCode Zen", _fetch_opencode_go)
register("OpenCode Go", _fetch_opencode_go)


# ── 火山方舟获取器 ────────────────────────────────

def _fetch_volcengine_ark(config: dict) -> Optional[Dict[str, Any]]:
    """从 console.volcengine.com 获取火山方舟套餐用量。

    需要在服务商配置中额外填写：
    - cookie: 浏览器 Cookie（完整值）
    - csrf_token: x-csrf-token
    - x_web_id: x-web-id
    """
    cookie = (config.get("cookie", "") or "").strip()
    csrf_token = (config.get("csrf_token", "") or "").strip()
    x_web_id = (config.get("x_web_id", "") or "").strip()

    if not cookie or not csrf_token:
        return None

    url = "https://console.volcengine.com/api/top/ark/cn-beijing/2024-01-01/GetCodingPlanUsage?"
    headers = {
        "Content-Type": "application/json",
        "Cookie": cookie,
        "x-csrf-token": csrf_token,
        "Origin": "https://console.volcengine.com",
        "Referer": "https://console.volcengine.com/ark/region:ark+cn-beijing/openManagement",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh",
    }
    if x_web_id:
        headers["x-web-id"] = x_web_id

    body = b"{}"

    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except Exception:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    result_data = data.get("Result", {})
    quota_list = result_data.get("QuotaUsage", [])
    if not quota_list:
        return None

    now = int(time.time())
    result = {"rolling": None, "weekly": None, "monthly": None}

    level_map = {
        "session": "rolling",
        "weekly": "weekly",
        "monthly": "monthly",
    }

    for item in quota_list:
        level = item.get("Level", "")
        key = level_map.get(level)
        if not key:
            continue
        pct = item.get("Percent")
        reset_ts = item.get("ResetTimestamp")
        if pct is not None and reset_ts is not None and reset_ts > 0:
            result[key] = {
                "percent": max(0, min(100, round(pct))),
                "reset_sec": max(0, reset_ts - now),
            }

    if any(v is not None for v in result.values()):
        return result
    return None


register("火山方舟", _fetch_volcengine_ark)


# ── MiniMax 获取器 ─────────────────────────────────

def _fetch_minimax(config: dict) -> Optional[Dict[str, Any]]:
    """从 www.minimaxi.com 获取 MiniMax Token Plan 用量。

    使用服务商的 API_KEY（Bearer token）直接请求，不需要额外配置。
    API 返回 coding plan 的滚动/每周剩余额度，自动换算为用量百分比。
    """
    api_key = (config.get("API_KEY", "") or "").strip()
    if not api_key:
        return None

    url = "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except Exception:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    base_resp = data.get("base_resp", {})
    if base_resp.get("status_code", -1) != 0:
        return None

    model_remains = data.get("model_remains", [])
    if not model_remains:
        return None

    result = {"rolling": None, "weekly": None, "monthly": None}

    for item in model_remains:
        model_name = item.get("model_name", "")
        if model_name != "general":
            continue

        # ── 滚动限额（5h rolling window） ──
        interval_status = item.get("current_interval_status", 3)
        if interval_status == 1:  # 1 = 有限额
            remaining_pct = item.get("current_interval_remaining_percent", 0)
            usage_pct = max(0, min(100, 100 - remaining_pct))
            remains_ms = item.get("remains_time", 0)
            reset_sec = max(0, int(remains_ms / 1000))
            if reset_sec > 0:
                result["rolling"] = {"percent": usage_pct, "reset_sec": reset_sec}

        # ── 每周限额 ──
        weekly_status = item.get("current_weekly_status", 3)
        if weekly_status == 1:  # 1 = 有限额
            weekly_remaining_pct = item.get("current_weekly_remaining_percent", 0)
            weekly_usage_pct = max(0, min(100, 100 - weekly_remaining_pct))
            weekly_remains_ms = item.get("weekly_remains_time", 0)
            weekly_reset_sec = max(0, int(weekly_remains_ms / 1000))
            if weekly_reset_sec > 0:
                result["weekly"] = {"percent": weekly_usage_pct, "reset_sec": weekly_reset_sec}
        # weekly 无限额时保持 None

        # ── 月限额：该接口没有月度数据 ──
        break  # 只处理 "general" 一条

    if any(v is not None for v in result.values()):
        return result
    return None


register("MiniMax", _fetch_minimax)


# ── OpenAI 获取器 ──────────────────────────────────

def _fetch_openai(config: dict) -> Optional[Dict[str, Any]]:
    """从 OpenAI 获取 API 用量信息。

    OpenAI 是 Pay-as-you-go 模式，通过 dashboard/billing API 查询
    用户设置的月度账单上限和已使用额度。

    使用服务商的 API_KEY（Bearer token）直接请求，不需要额外配置。
    需要账号已设置月度消费上限（hard_limit），否则无法计算百分比。

    响应映射为 monthly 维度（按自然月重置）。
    """
    api_key = (config.get("API_KEY", "") or "").strip()
    if not api_key:
        return None

    now = int(time.time())

    # ── 1. 获取订阅信息（总额度） ──
    sub_url = "https://api.openai.com/v1/dashboard/billing/subscription"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
    }

    try:
        req = urllib.request.Request(sub_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except Exception:
        return None

    try:
        sub_data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    hard_limit_usd = sub_data.get("hard_limit_usd")
    if not hard_limit_usd or hard_limit_usd <= 0:
        # 没有设置月度消费上限，无法计算百分比
        return None

    # ── 2. 获取过去 ~30 天的用量 ──
    import datetime
    end_date = datetime.date.today() + datetime.timedelta(days=1)
    start_date = end_date - datetime.timedelta(days=30)

    usage_url = (
        f"https://api.openai.com/v1/dashboard/billing/usage"
        f"?start_date={start_date.isoformat()}&end_date={end_date.isoformat()}"
    )

    try:
        req = urllib.request.Request(usage_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except Exception:
        return None

    try:
        usage_data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # total_usage 以美分为单位，需转为美元
    total_usage_cents = usage_data.get("total_usage", 0)
    if total_usage_cents is None:
        return None

    total_usage_usd = total_usage_cents / 100.0

    # ── 3. 计算使用百分比 ──
    usage_pct = max(0, min(100, round(total_usage_usd / hard_limit_usd * 100)))

    # ── 4. 计算到月底的秒数作为重置时间 ──
    import calendar
    today = datetime.date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    end_of_month = datetime.datetime(
        today.year, today.month, last_day, 23, 59, 59,
        tzinfo=datetime.timezone.utc,
    )
    reset_sec = max(0, int(end_of_month.timestamp() - now))

    result = {"rolling": None, "weekly": None, "monthly": None}
    result["monthly"] = {"percent": usage_pct, "reset_sec": reset_sec}

    if any(v is not None for v in result.values()):
        return result
    return None


register("OpenAI", _fetch_openai)


# ── 异步包装 ────────────────────────────────────────

def fetch_async(
    provider_name: str,
    config: Dict[str, Any],
    callback: Callable[[Optional[Dict[str, Any]]], None],
):
    """异步查询套餐用量，结果通过 callback 返回"""
    import threading

    def _run():
        try:
            result = fetch(provider_name, config)
            callback(result)
        except Exception:
            callback(None)

    threading.Thread(target=_run, daemon=True).start()
