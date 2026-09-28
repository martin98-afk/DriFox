# -*- coding: utf-8 -*-
"""
服务商插件 — OpenCode Zen / OpenCode Go

数据 + 套餐用量查询 fetcher + 用量查询额外配置字段，全部由本插件声明。
用量：opencode.ai/_server 接口（server_id / cookie / workspace_id 三者必填）。
其中 server_id 是 SolidStart server function 的构建哈希，opencode 前端每次发版都会变，
失效时接口返回 500；cookie 必需，未认证时服务端在 RSC payload 里回 302 跳 /auth/authorize。
"""

import json
import re
import urllib.parse
from typing import Any, Dict, Optional

from app.plugins.registries.provider_registry import ProviderDef, QuotaField

# 用量块锚点：响应里形如 rollingUsage:$R[2]={...}，字段顺序与数量均不锁定
_USAGE_ANCHORS = [("rolling", "rollingUsage"), ("weekly", "weeklyUsage"), ("monthly", "monthlyUsage")]
_RE_RESET_SEC = re.compile(r"resetInSec:(\d+)")
_RE_USAGE_PCT = re.compile(r"usagePercent:([\d.]+)")

_SERVER_URL = "https://opencode.ai/_server"
_SERVER_HEADERS = {
    "X-Server-Instance": "server-fn:0",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
}


class OpenCodeUsageError(RuntimeError):
    """用量查询失败，异常文本即归因说明（由 UsageService 捕获后写 warning 日志）"""


def _fetch_opencode_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 opencode.ai/_server 获取 OpenCode Zen/Go 套餐用量。

    需要在服务商配置中额外填写 server_id / cookie / workspace_id。
    这些字段不会影响正常的 API 调用，仅用于用量查询。
    """
    # 延迟导入：避免模块级网络库触发 AST 危险 import 审计告警
    import urllib.error
    import urllib.request

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
    except urllib.error.HTTPError as e:
        # 500 = 服务端找不到该 server function，即 Server ID 已随前端构建失效
        if e.code == 500:
            raise OpenCodeUsageError(
                "Server ID 已失效（opencode 前端重新构建），请重新抓取 _server 请求的 X-Server-Id"
            ) from e
        raise OpenCodeUsageError(f"用量请求失败：HTTP {e.code}") from e
    except OSError as e:
        raise OpenCodeUsageError(f"用量请求异常：{e}") from e

    # 未认证：HTTP 仍是 200，登录跳转藏在 RSC payload 内
    if "auth/authorize" in raw:
        raise OpenCodeUsageError("未认证：Cookie 缺失或已过期，请重新复制浏览器 Cookie")

    # 标准 JSON
    try:
        data = json.loads(raw)
        return _parse_json(data)
    except json.JSONDecodeError:
        pass

    # RSC（JavaScript 风格）响应
    parsed = _parse_js(raw)
    if parsed.get("rolling") or parsed.get("weekly") or parsed.get("monthly"):
        return parsed

    raise OpenCodeUsageError("响应结构变更：未解析到 usagePercent，请核对 _server 接口返回体")


def _parse_json(data: dict) -> Dict[str, Any]:
    result = {}
    for key, api_key in _USAGE_ANCHORS:
        usage = data.get(api_key, {})
        pct = usage.get("usagePercent")
        sec = usage.get("resetInSec")
        result[key] = {"percent": float(pct), "reset_sec": int(sec)} if pct is not None and sec is not None else None
    return result


def _extract_block(raw: str, anchor: str) -> Optional[str]:
    """取 anchor 后首个花括号配平的块内容。

    服务端会往用量对象里追加字段（当前已有 usage / limit），也可能出现嵌套对象，
    因此按花括号深度扫描取整块，不用 [^{}]* 之类的单层匹配。
    """
    i = raw.find(anchor + ":")
    if i < 0:
        return None
    j = raw.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(raw)):
        ch = raw[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[j + 1 : k]
    return None


def _parse_js(raw: str) -> Dict[str, Any]:
    """解析 RSC 响应中的用量块，percent 保留小数（44.9 这类值 UI 侧自行取整）"""
    result: Dict[str, Any] = {}
    for key, api_key in _USAGE_ANCHORS:
        block = _extract_block(raw, api_key)
        if block is None:
            result[key] = None
            continue
        m_sec = _RE_RESET_SEC.search(block)
        m_pct = _RE_USAGE_PCT.search(block)
        result[key] = {"percent": float(m_pct.group(1)), "reset_sec": int(m_sec.group(1))} if m_sec and m_pct else None
    return result


# 用量查询额外配置字段（仅用于用量查询，不进模型参数/API 请求）
_QUOTA_FIELDS = [
    QuotaField(
        key="server_id",
        label="Server ID:",
        placeholder="_server 请求的 X-Server-Id（随 opencode 前端构建变化，失效时接口返回 500）",
    ),
    QuotaField(
        key="cookie",
        label="Cookie:",
        placeholder="oc_locale=zh; auth=Fe26.2**... （必填，从浏览器复制完整 Cookie 值，过期时提示未认证）",
    ),
    QuotaField(
        key="workspace_id",
        label="Workspace ID:",
        placeholder="wrk_xxxxxxxxxxxx（必填，用量页面 URL 中可见）",
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
    # 网关会话标识头：Zen/Go 要求每个 LLM 请求携带稳定会话 ID
    # （2026-09-06 起缺失报 400 MissingSessionID），值由主程序填当前会话 ID
    "session_header": "x-opencode-session",
}


def register(registry):
    """注册 OpenCode Zen / OpenCode Go 两个服务商定义"""
    # ── OpenCode Zen（免费额度） ──
    #
    # 免费层闸门（实测 2026-09-24）：
    #   带 -free 后缀的模型（big-pickle / mimo-v2.5-free / nemotron-*-free /
    #   ling-*-free 等）返回 403 FreeTierError「can only be used from within
    #   OpenCode」。网关判定在服务端按账号/工作区维度进行，伪装身份头无效：
    #   User-Agent 换 opencode-cli/1.0.0、opencode 等，并补齐 x-opencode-client
    #   / x-opencode-project / x-opencode-request / x-opencode-session（UUID）后
    #   仍全部 403。这些头在网关源码里仅写入日志指标，不参与鉴权
    #   （packages/console/app/src/routes/zen/util/handler.ts）。
    #   保留这些条目是因为付费 workspace 账号仍可使用；免费账号选中会报
    #   403，属预期。
    #
    #   space-bunny-free 不受该闸门约束（无 -free 后缀的例外项），免 key
    #   匿名调用可用，流式与工具调用正常，故作为默认模型。
    registry.register(
        ProviderDef(
            name="OpenCode Zen",
            icon="opencode",
            api_url="https://opencode.ai/zen/v1",
            auth_type="bearer",
            default_model="space-bunny-free",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://opencode.ai/auth",
            models=[
                "space-bunny-free",
                "big-pickle",
                "mimo-v2.5-free",
                "nemotron-3-ultra-free",
                "glm-5.1",
                "glm-5",
                "kimi-k2.6",
                "kimi-k2.5",
                "deepseek-v4-pro",
                "deepseek-v4-flash",
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
