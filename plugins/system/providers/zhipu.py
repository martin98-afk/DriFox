# -*- coding: utf-8 -*-
"""
服务商插件 — 智谱AI

数据 + 套餐用量查询 fetcher 全部由本插件声明（万物为插件）。
用量：BigModel / Z.ai monitor 接口（Bearer token，按 API_URL 自动区分国内/国际）。
"""

import json
import time
import urllib.request
from typing import Any, Dict, Optional

from app.plugins.registries.provider_registry import ProviderDef


def _fetch_zhipu_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从智谱 BigModel / Z.ai 获取 GLM Coding Plan 积分用量。

    使用服务商的 API_KEY（Bearer token）直接请求 monitor 接口，不需要额外配置。
    新版套餐按积分计算：5 小时窗口（unit=3）+ 每周窗口（unit=6），无月度额度。
    端点按 API_URL 自动区分国内版（open.bigmodel.cn）与国际版（api.z.ai）。
    """
    api_key = (config.get("API_KEY", "") or "").strip()
    if not api_key:
        return None

    api_url = (config.get("API_URL", "") or "").strip()
    url = (
        "https://api.z.ai/api/monitor/usage/quota/limit"
        if "z.ai" in api_url
        else "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    )
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

    # {"code":200, "success":true, "data":{"limits":[...], "level":"lite"}}
    if data.get("code") != 200 or not data.get("success"):
        return None

    limits = (data.get("data") or {}).get("limits", [])
    if not limits:
        return None

    now_ms = int(time.time() * 1000)
    result = {"rolling": None, "weekly": None, "monthly": None}

    for item in limits:
        if item.get("type") != "CREDIT_LIMIT":
            continue
        pct = item.get("percentage")
        reset_ms = item.get("nextResetTime", 0)
        if pct is None or not reset_ms:
            continue
        entry = {
            "percent": max(0, min(100, int(pct))),
            "reset_sec": max(0, int((reset_ms - now_ms) / 1000)),
        }
        # unit=3 → 5 小时积分窗口；unit=6 → 每周积分窗口；无月度额度
        if item.get("unit") == 3:
            result["rolling"] = entry
        elif item.get("unit") == 6:
            result["weekly"] = entry

    if any(v is not None for v in result.values()):
        return result
    return None


def register(registry):
    """注册 智谱AI 服务商定义"""
    registry.register(
        ProviderDef(
            name="智谱AI",
            icon="智谱",
            api_url="https://open.bigmodel.cn/api/coding/paas/v4",
            auth_type="bearer",
            default_model="glm-4-flash",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
                "思考模式": True,
            },
            register_url="https://open.bigmodel.cn/apikey/platform",
            models=[
                "glm-5.1",
                "glm-5-turbo",
                "glm-4-pro",
                "glm-4-flash",
                "glm-4-flashx",
                "glm-4-plus",
                "glm-4",
            ],
            models_dev_id="zhipuai",
            family="zhipu",
            capabilities={
                "token_ratio": 0.50,  # 本地 token 估算校正系数（除数）；智谱 GLM 略低于 Qwen
                "context_limit": 200000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": True,
                "supports_thinking": True,
                "thinking_param": "thinking",
                "reasoning_effort_param": None,
            },
            coding_plan_fetcher=_fetch_zhipu_coding_plan,
        )
    )