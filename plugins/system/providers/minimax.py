# -*- coding: utf-8 -*-
"""
服务商插件 — MiniMax

数据 + 套餐用量查询 fetcher 全部由本插件声明（万物为插件）。
用量：www.minimaxi.com coding_plan remains 接口（Bearer token）。
"""

import json
import time
import urllib.request
from typing import Any, Dict, Optional

from app.plugins.registries.provider_registry import ProviderDef


def _fetch_minimax_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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

        # ── 月限额：该接口没有月度数据 ──
        break  # 只处理 "general" 一条

    if any(v is not None for v in result.values()):
        return result
    return None


def register(registry):
    """注册 MiniMax 服务商定义"""
    registry.register(
        ProviderDef(
            name="MiniMax",
            icon="MiniMax",
            api_url="https://api.minimax.chat/v1",
            auth_type="bearer",
            default_model="MiniMax-M2.5",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
            models=[
                "MiniMax-M2.7",
                "MiniMax-M2.7-highspeed",
                "MiniMax-M2.5",
                "MiniMax-M2.5-highspeed",
                "MiniMax-M2.1",
                "MiniMax-M2.1-highspeed",
            ],
            models_dev_id="minimax",
            family="minimax",
            capabilities={
                "context_limit": 1000000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": False,
                "supports_thinking": False,
                "thinking_param": None,
            },
            coding_plan_fetcher=_fetch_minimax_coding_plan,
        )
    )