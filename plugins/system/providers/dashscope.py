# -*- coding: utf-8 -*-
"""
服务商插件 — 阿里云 (DashScope)

数据全部由本插件声明（万物为插件）：icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力 / 用量查询额外字段 / 套餐用量 fetcher。

用量：百炼控制台内部接口 zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage，
需从浏览器复制 cookie + sec_token（控制台登录会话）。仅返回 weekly（7 天限额）
—— 当前接口未提供 5 小时 / 月度窗口。
"""

import gzip
import json
import re
import time
import urllib.parse
import urllib.request
import uuid
import zlib
from typing import Any, Dict, Optional

from app.plugins.registries.provider_registry import ProviderDef, QuotaField

# ── 用量查询 endpoint（百炼控制台内部网关，非公开 OpenAPI） ──
_BAILIAN_USAGE_API = (
    "https://bailian-cs.console.aliyun.com/data/api.json"
    "?action=BroadScopeAspnGateway&product=sfm_bailian"
    "&api=zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage&_v=undefined"
)
# 浏览器完整请求头（裸 urllib 不带 sec-ch-* / sec-fetch-* 会被阿里风控拦到淘宝拦截页）
_BAILIAN_USAGE_HEADERS = {
    "accept": "*/*",
    "accept-encoding": "gzip, deflate",
    "accept-language": "zh-CN,zh;q=0.9",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://bailian.console.aliyun.com",
    "priority": "u=1, i",
    "referer": "https://bailian.console.aliyun.com/cn-beijing?tab=plan",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

# cornerstone 风控字段模板（X-Anonymous-Id 运行时从 cookie.cna 抽取，其余常量）
_BAILIAN_PARAMS_TEMPLATE: Dict[str, Any] = {
    "Api": "zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage",
    "V": "1.0",
    "Data": {
        "cornerstoneParam": {
            "feTraceId": "",  # 每次请求前用 uuid4 覆盖
            "feURL": ("https://bailian.console.aliyun.com/cn-beijing?tab=plan#/efm/subscription/token-plan/personal"),
            "protocol": "V2",
            "console": "ONE_CONSOLE",
            "productCode": "p_efm",
            "switchAgent": 12171280,
            "switchUserType": 3,
            "domain": "bailian.console.aliyun.com",
            "consoleSite": "BAILIAN_ALIYUN",
            "userNickName": "",
            "userPrincipalName": "",
            "xsp_lang": "zh-CN",
            "X-Anonymous-Id": "",  # 运行时从 cookie.cna 覆盖
        }
    },
}

_CNA_RE = re.compile(r"(?:^|;\s*)cna=([^;]+)")


def _extract_cna(cookie: str) -> str:
    """从 cookie 字符串抽取 cna 值（对应 X-Anonymous-Id）"""
    m = _CNA_RE.search(cookie)
    return m.group(1) if m else ""


def _build_form_body(cookie: str, sec_token: str) -> bytes:
    """构造百炼用量查询 form body：params JSON + region + sec_token"""
    params = json.loads(json.dumps(_BAILIAN_PARAMS_TEMPLATE))  # 深拷贝
    cp = params["Data"]["cornerstoneParam"]
    cp["feTraceId"] = str(uuid.uuid4())
    cp["X-Anonymous-Id"] = _extract_cna(cookie)
    return urllib.parse.urlencode(
        {
            "params": json.dumps(params, separators=(",", ":")),
            "region": "cn-beijing",
            "sec_token": sec_token,
        }
    ).encode("utf-8")


def _decode_body(raw_bytes: bytes, content_encoding: str, charset: str) -> str:
    if content_encoding == "gzip":
        return gzip.decompress(raw_bytes).decode(charset, errors="replace")
    if content_encoding == "deflate":
        return zlib.decompress(raw_bytes).decode(charset, errors="replace")
    if content_encoding == "br":
        try:
            import brotli  # type: ignore

            return brotli.decompress(raw_bytes).decode(charset, errors="replace")
        except ImportError:
            return raw_bytes.decode(charset, errors="replace")
    return raw_bytes.decode(charset, errors="replace")


def _fetch_bailian_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从百炼控制台获取 Token Plan 个人版 7 天限额用量。

    需要在服务商配置中额外填写 cookie（百炼控制台登录态）+ sec_token
    （form 表单里的风控令牌，从 DevTools → Payload 复制）。
    仅 weekly 一层；5 小时 / 月度窗口该接口未提供。
    """
    cookie = (config.get("cookie", "") or "").strip()
    sec_token = (config.get("sec_token", "") or "").strip()
    if not cookie or not sec_token:
        return None  # 缺凭据 → 主程序同步发 None，前端隐藏卡片

    headers = {**_BAILIAN_USAGE_HEADERS, "cookie": cookie}
    try:
        req = urllib.request.Request(_BAILIAN_USAGE_API, headers=headers, data=_build_form_body(cookie, sec_token))
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            content_encoding = (resp.headers.get("content-encoding") or "").lower()
            raw = _decode_body(resp.read(), content_encoding, charset)
    except Exception:
        return None

    try:
        payload = json.loads(raw)
    except Exception:
        return None

    try:
        inner = payload.get("data", {}).get("DataV2", {}).get("data", {}).get("data", {})
        pct_raw = inner.get("per1WeekPercentage")
        reset_ts = inner.get("per1WeekResetTime")
    except Exception:
        return None

    if pct_raw is None:
        return None

    now_ms = int(time.time() * 1000)
    reset_sec = max(0, int((int(reset_ts) - now_ms) / 1000)) if reset_ts else 0

    return {
        "weekly": {
            "percent": int(round(float(pct_raw) * 100)),
            "reset_sec": reset_sec,
        }
    }


# 用量查询额外配置字段（仅用于用量查询，不进模型参数/API 请求）
_BAILIAN_QUOTA_FIELDS = [
    QuotaField(
        key="cookie",
        label="百炼 Cookie:",
        placeholder=(
            "登录 bailian.console.aliyun.com → DevTools → Network → 任一请求 "
            "→ 复制完整 Cookie 头值（含 cna / login_aliyunid_ticket / tfstk 等）"
        ),
    ),
    QuotaField(
        key="sec_token",
        label="百炼 sec_token:",
        placeholder=(
            "DevTools → Network → 点开百炼任一请求 → Payload → Form Data "
            "→ 复制 sec_token 字段值（约 22 字符短令牌，会话级有效）"
        ),
    ),
]


def register(registry):
    """注册 阿里云 (DashScope) 服务商定义"""
    registry.register(
        ProviderDef(
            name="阿里云 (DashScope)",
            icon="qwen",
            api_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            auth_type="bearer",
            default_model="qwen3.5-plus",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://bailian.console.aliyun.com/cn-beijing?tab=model#/api-key",
            models=[
                "qwen3-max",
                "qwen3-plus",
                "qwen3.5-max",
            ],
            models_dev_id="alibaba",
            family="dashscope",
            capabilities={
                "token_ratio": 0.48,  # 本地 token 估算校正系数（除数）；通义千问 Qwen2.5 实测≈0.55 token/中文字
                "context_limit": 1000000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": True,
                "supports_thinking": False,
                "thinking_param": None,
            },
            extra_quota_fields=_BAILIAN_QUOTA_FIELDS,
            coding_plan_fetcher=_fetch_bailian_coding_plan,
        )
    )
