# -*- coding: utf-8 -*-
"""
服务商插件 — 火山方舟（Volcengine Ark）

数据 + 套餐用量查询 fetcher + 用量查询额外配置字段，全部由本插件声明。
用量：console.volcengine.com GetCodingPlanUsage 接口
（需 cookie / csrf_token / x_web_id 浏览器凭据）。
"""

import json
import time
import urllib.request
from typing import Any, Dict, Optional

from app.plugins.registries.provider_registry import ProviderDef, QuotaField


def _fetch_volcengine_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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


_MY_QUOTA_FIELDS = [
    QuotaField(
        key="cookie",
        label="Cookie:",
        placeholder="console.volcengine.com 浏览器 Cookie（完整值）",
    ),
    QuotaField(
        key="csrf_token",
        label="CSRF Token:",
        placeholder="x-csrf-token（从请求头复制）",
    ),
    QuotaField(
        key="x_web_id",
        label="X-Web-ID:",
        placeholder="x-web-id（可选）",
    ),
]


def register(registry):
    """注册 火山方舟 服务商定义"""
    registry.register(
        ProviderDef(
            name="火山方舟",
            icon="火山引擎",
            api_url="https://ark.cn-beijing.volces.com/api/coding/v3",
            auth_type="bearer",
            default_model="doubao-pro-32k",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
            models=[
                "doubao-seed-code",
                "kimi-k2.6 ",
                "kimi-k2.5",
                "minimax-m2.7",
                "glm-4.7",
                "glm5.1",
            ],
            models_dev_id="",
            family="volcengine",
            capabilities={
                "token_ratio": 1.00,  # 本地 token 估算校正系数（除数）；cl100k_base 基线经 OpenCode 验证已准确，统一 1.0；见 token_estimator._MODEL_TOKEN_RATIOS
                "context_limit": 1000000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": False,
                "supports_thinking": False,
                "thinking_param": None,
            },
            extra_quota_fields=_MY_QUOTA_FIELDS,
            coding_plan_fetcher=_fetch_volcengine_coding_plan,
        )
    )