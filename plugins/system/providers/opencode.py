# -*- coding: utf-8 -*-
"""
服务商插件 — OpenCode Zen / OpenCode Go

数据 + 套餐用量查询 fetcher + 用量查询额外配置字段，全部由本插件声明。
用量：opencode.ai/_server 接口（需 server_id / cookie / workspace_id）。
"""

import json
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from app.plugins.registries.provider_registry import ProviderDef, QuotaField

_SERVER_URL = "https://opencode.ai/_server"
_SERVER_HEADERS = {
    "X-Server-Instance": "server-fn:0",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
}


def _fetch_opencode_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 opencode.ai/_server 获取 OpenCode Zen/Go 套餐用量。

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
    url = f"{_SERVER_URL}?id={server_id}&args={args_encoded}"

    headers = {
        **_SERVER_HEADERS,
        "X-Server-Id": server_id,
        "Referer": f"https://opencode.ai/workspace/{workspace_id}/go",
        "Cookie": cookie,
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
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


# 用量查询额外配置字段（仅用于用量查询，不进模型参数/API 请求）
_QUOTA_FIELDS = [
    QuotaField(
        key="server_id",
        label="Server ID:",
        placeholder="opencode.ai/_server 请求中的 X-Server-Id",
    ),
    QuotaField(
        key="cookie",
        label="Cookie:",
        placeholder="oc_locale=zh; auth=Fe26.2**... （从浏览器复制完整的 Cookie 值）",
    ),
    QuotaField(
        key="workspace_id",
        label="Workspace ID:",
        placeholder="wrk_xxxxxxxxxxxx （无需可留空）",
    ),
]

_ZEN_CAPABILITIES = {
    "context_limit": 200000,
    "max_output_tokens": 8192,
    "absolute_limit": 65536,
    "supports_vision": True,
    "supports_thinking": True,
    "thinking_param": "reasoning_effort",
    "reasoning_effort_param": "reasoning_effort",
}


def register(registry):
    """注册 OpenCode Zen / OpenCode Go 两个服务商定义"""
    # ── OpenCode Zen（免费额度） ──
    registry.register(
        ProviderDef(
            name="OpenCode Zen",
            icon="opencode",
            api_url="https://opencode.ai/zen/v1",
            auth_type="bearer",
            default_model="deepseek-v4-flash-free",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://opencode.ai/auth",
            models=[
                "deepseek-v4-flash-free",
                "mimo-v2.5-free",
                "nemotron-3-ultra-free",
                "north-mini-code-free",
                "big-pickle",
                "glm-5.1",
                "glm-5",
                "kimi-k2.6",
                "kimi-k2.5",
                "deepseek-v4-pro",
                "deepseek-v4-flash",
                "mimo-v2.5-pro",
                "mimo-v2.5",
                "minimax-m2.7",
                "minimax-m2.5",
                "qwen3.6-plus",
                "qwen3.5-plus",
            ],
            models_dev_id="opencode",
            family="opencode",
            capabilities=_ZEN_CAPABILITIES,
            extra_quota_fields=_QUOTA_FIELDS,
            coding_plan_fetcher=_fetch_opencode_coding_plan,
        )
    )

    # ── OpenCode Go（付费） ──
    registry.register(
        ProviderDef(
            name="OpenCode Go",
            icon="opencode",
            api_url="https://opencode.ai/zen/go/v1",
            auth_type="bearer",
            default_model="deepseek-v4-flash",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://opencode.ai/auth",
            models=[
                "deepseek-v4-flash",
                "deepseek-v4-pro",
                "glm-5",
                "glm-5.1",
                "glm-5.2",
                "kimi-k2.5",
                "kimi-k2.6",
                "kimi-k2.7-code",
                "mimo-v2-omni",
                "mimo-v2-pro",
                "mimo-v2.5",
                "mimo-v2.5-pro",
                "minimax-m2.5",
                "minimax-m2.7",
                "minimax-m3",
                "qwen3.5-plus",
                "qwen3.6-plus",
                "qwen3.7-max",
                "qwen3.7-plus",
            ],
            models_dev_id="opencode-go",
            family="opencode",
            capabilities=_ZEN_CAPABILITIES,
            extra_quota_fields=_QUOTA_FIELDS,
            coding_plan_fetcher=_fetch_opencode_coding_plan,
        )
    )