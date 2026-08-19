# -*- coding: utf-8 -*-
"""
服务商插件 — OpenAI

数据 + 套餐用量查询 fetcher 全部由本插件声明（万物为插件）。
用量：OpenAI dashboard/billing API（Pay-as-you-go，需设置月度消费上限）。
"""

import calendar
import datetime
import json
import time
import urllib.request
from typing import Any, Dict, Optional

from app.plugins.registries.provider_registry import ProviderDef


def _fetch_openai_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 OpenAI 获取 API 用量信息（按自然月重置，映射为 monthly 维度）。

    使用服务商的 API_KEY（Bearer token）直接请求，不需要额外配置。
    需要账号已设置月度消费上限（hard_limit），否则无法计算百分比。
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


def register(registry):
    """注册 OpenAI 服务商定义"""
    registry.register(
        ProviderDef(
            name="OpenAI",
            icon="大模型",
            api_url="https://api.openai.com/v1",
            auth_type="bearer",
            default_model="gpt-4o-mini",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://platform.openai.com/api-keys",
            models=[
                "gpt-4o",
                "gpt-4o-mini",
                "gpt-4-turbo",
                "gpt-4",
                "gpt-3.5-turbo",
            ],
            models_dev_id="openai",
            family="openai",
            capabilities={
                "context_limit": 200000,
                "max_output_tokens": 16384,
                "absolute_limit": 65536,
                "supports_vision": True,
                "supports_thinking": False,
                "thinking_param": None,
            },
            coding_plan_fetcher=_fetch_openai_coding_plan,
        )
    )