# -*- coding: utf-8 -*-
"""
服务商插件 — OpenCode Zen / OpenCode Go

数据由本插件声明。套餐用量查询仅 OpenCode Go 提供：
GET https://opencode.ai/zen/go/v1/usage（官方接口，Bearer API Key 鉴权，
复用服务商配置里已有的 API Key，无需 server_id / cookie / workspace_id）。
OpenCode Zen 是按量 API 型（非订阅），无套餐用量，不注册 fetcher。
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.plugins.registries.provider_registry import ProviderDef

_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
_REQUEST_HEADERS = {
    "User-Agent": "DriFox/1.0",
    "Accept": "application/json",
}

class OpenCodeUsageError(RuntimeError):
    """用量查询失败，异常文本即归因说明（由 UsageService 捕获后写 warning 日志）"""

_SSL_CONTEXT = None  # 进程级缓存；False 哨兵 = 已探测且 truststore 不可用

def _system_ssl_context():
    """系统证书链 SSLContext（信任 macOS 钥匙串/Windows 证书存储）；不可用回退 None。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        try:
            import ssl

            import truststore

            _SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            _SSL_CONTEXT = False
    return _SSL_CONTEXT or None

def _fetch_opencode_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从官方 /zen/go/v1/usage 获取 OpenCode Go 套餐用量。

    用服务商配置中的 API Key（Bearer）鉴权，无额外配置字段。
    """
    # 延迟导入：避免模块级网络库触发 AST 危险 import 审计告警
    import urllib.error
    import urllib.request

    api_key = (config.get("API_KEY", "") or "").strip()
    if not api_key:
        return None

    headers = {**_REQUEST_HEADERS, "Authorization": f"Bearer {api_key}"}

    try:
        req = urllib.request.Request(_USAGE_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=_system_ssl_context()) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise OpenCodeUsageError("鉴权失败：API Key 无效或已撤销") from e
        if e.code == 403:
            raise OpenCodeUsageError("无 Go 订阅：该 API Key 所属账号未订阅 OpenCode Go") from e
        raise OpenCodeUsageError(f"用量请求失败：HTTP {e.code}") from e
    except OSError as e:
        raise OpenCodeUsageError(f"用量请求异常：{e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise OpenCodeUsageError("响应非 JSON：接口结构可能变更") from e

    result = _parse_usage(data)
    if not any(v is not None for v in result.values()):
        raise OpenCodeUsageError("响应结构变更：未解析到任何窗口用量，请核对接口返回体")
    return result

def _parse_usage(data: dict) -> Dict[str, Any]:
    """解析用量响应，兼容两种已知布局：
    - 实测（2026-08，cc-switch #6433）：{"usage": {"rolling": {"percent", "resetsAt"}, ...}}
    - PR #16513 原始：{"rollingUsage": {"usagePercent", "resetInSec"}, ...}
    """
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
    # (结果键, 响应键)：实测布局在前，PR 原始布局兜底
    groups = [
        ("rolling", "rolling"),
        ("weekly", "weekly"),
        ("monthly", "monthly"),
        ("rolling", "rollingUsage"),
        ("weekly", "weeklyUsage"),
        ("monthly", "monthlyUsage"),
    ]
    result: Dict[str, Any] = {"rolling": None, "weekly": None, "monthly": None}
    for key, field in groups:
        if result[key] is not None:
            continue
        block = usage.get(field)
        if isinstance(block, dict):
            result[key] = _parse_block(block)
    return result

def _parse_block(block: dict) -> Optional[Dict[str, Any]]:
    """从单个窗口块取 percent 与 reset_sec，缺任一则视为不可用。"""
    pct = block.get("percent", block.get("usagePercent"))
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    sec = block.get("resetInSec")
    if not isinstance(sec, (int, float)) or isinstance(sec, bool):
        sec = _resets_at_to_sec(block.get("resetsAt"))
        if sec is None:
            return None
    return {"percent": float(pct), "reset_sec": max(0, int(sec))}

def _resets_at_to_sec(iso: Any) -> Optional[int]:
    """resetsAt（ISO8601 时间）→ 距重置秒数；过去时刻取 0。"""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        resets_at = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if resets_at.tzinfo is None:
        resets_at = resets_at.replace(tzinfo=timezone.utc)
    return int((resets_at - datetime.now(timezone.utc)).total_seconds())

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
            # Zen 是按量 API 型（非订阅）：显式声明列表分组，
            # 避免被「有 coding_plan_fetcher → Coding Plan」自动判据误归
            capabilities={**_ZEN_CAPABILITIES, "list_group": "API"},
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
            capabilities={**_ZEN_CAPABILITIES, "list_group": "Coding Plan"},
            coding_plan_fetcher=_fetch_opencode_coding_plan,
        )
    )
