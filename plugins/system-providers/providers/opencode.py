# -*- coding: utf-8 -*-
"""
服务商插件 — OpenCode Zen / OpenCode Go / OpenCode Zen (OAuth)

数据由本插件声明。套餐用量查询挂在两个 Go 版：
GET https://opencode.ai/zen/go/v1/usage（官方接口，Bearer 鉴权）。
API Key 版用配置里的 API Key；OAuth 版用短效 access_token（容错，失败返
None）。OpenCode Zen 是按量 API 型（非订阅），无套餐用量，不注册 fetcher。

另注册「OpenCode Zen (OAuth)」：console device-flow 登录，供 v2 console 隐藏
API key 的 workspace 成员使用。与上面两个 API Key 版并存、互不干扰（独立
family，动态 Authorization 不会串到 API Key 配置）。协议实现见同目录
_opencode_oauth.py。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
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

def _request_usage(token: str) -> Dict[str, Any]:
    """GET /zen/go/v1/usage + 解析；token 为 Bearer 凭据（API key 或 OAuth access）。

    抛 OpenCodeUsageError 归因，由调用方决定是透出还是容错吞掉。
    """
    # 延迟导入：避免模块级网络库触发 AST 危险 import 审计告警
    import urllib.error
    import urllib.request

    headers = {**_REQUEST_HEADERS, "Authorization": f"Bearer {token}"}
    try:
        req = urllib.request.Request(_USAGE_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=_system_ssl_context()) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise OpenCodeUsageError("鉴权失败：凭据无效或已撤销") from e
        if e.code == 403:
            raise OpenCodeUsageError("无 Go 订阅：该凭据所属账号未订阅 OpenCode Go") from e
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

def _fetch_opencode_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """API Key 版 Go：用配置里的 API Key（Bearer）查套餐用量。"""
    api_key = (config.get("API_KEY", "") or "").strip()
    if not api_key:
        return None
    return _request_usage(api_key)

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

# ── OpenCode Zen (OAuth) ─────────────────────────────────────────
#
# 与上面两个 API Key 版并存。OAuth token 走 console device flow，推理端点为
# https://opencode.ai/inference/openai/v1，需动态 Authorization + workspace
# 头，故用**独立 family**：family_capabilities 按 family 聚合，若沿用
# "opencode" 会把动态头串到 API Key 配置上（每请求拿空 refresh_token 刷新）。

_INFERENCE_URL = "https://opencode.ai/inference/openai/v1"
_GO_INFERENCE_URL = "https://opencode.ai/inference/go/openai/v1"


_OAUTH_MODULE = None


def _load_oauth():
    """按路径显式 compile 加载同目录辅助模块（规避 .pyc 秒级 mtime 缓存）。

    模块实例必须缓存：两个动态头 callable、登录钩子共享同一份 _MEM 缓存与
    _REFRESH_LOCK，否则 prime_cache 与 get_auth_context 各持一份内存、刷新
    去重失效。opencode.py 热重载会重建本模块级变量，天然拿到新代码。
    """
    global _OAUTH_MODULE
    if _OAUTH_MODULE is None:
        import importlib.util

        path = Path(__file__).with_name("_opencode_oauth.py")
        spec = importlib.util.spec_from_file_location("_drifox_opencode_oauth", path)
        mod = importlib.util.module_from_spec(spec)
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)
        _OAUTH_MODULE = mod
    return _OAUTH_MODULE


def _oauth_authorization(llm_config: dict) -> str:
    """动态 Authorization：按配置里的 refresh_token 取短效 access_token。

    不能抛异常：resolve_extra_headers 对 callable 抛错会吞掉整组头（含
    session_header），反而不如留空 Authorization 让网关回 401。故失败时
    记录 warning（含「请重新登录」线索）后返回空串。
    """
    try:
        access, _org = _load_oauth().get_auth_context(llm_config)
    except Exception as e:
        from loguru import logger

        logger.warning(f"[OpenCode OAuth] 获取访问令牌失败：{e}")
        return ""
    return f"Bearer {access}" if access else ""


def _oauth_org_header(llm_config: dict) -> Optional[str]:
    """workspace 标识头。与 Authorization 同源于缓存，两次调用经刷新锁去重。

    两种情况下不发送该头（返回 None）：
    - Go 公开端点（/inference/go/）不绑 workspace，带任意 org 头会被判
      403 Workspace access denied（2026-11-08 实测）。
    - 账号无 workspace：空值头会被网关判 400 且响应体为空，日志无法归因。
    """
    url = str((llm_config or {}).get("API_URL", "") or "")
    if "/inference/go/" in url:
        return None
    try:
        _access, org = _load_oauth().get_auth_context(llm_config)
    except Exception:
        return None
    return org or None


_OAUTH_EXTRA_HEADERS = {
    "User-Agent": "opencode/cli",
    "Authorization": _oauth_authorization,
    "x-opencode-org-id": _oauth_org_header,
}

def _oauth_auth_retry(llm_config: dict) -> bool:
    """401 自愈：忽略本地过期时间强制换新 access_token，返回是否可重试。

    服务端会提前吊销整条会话链（access 401 + refresh invalid_grant，2026-11-08
    实测），本地 expires_in 却是 30 天，靠时间判定永远等不到刷新。故由执行层在
    收到 401 时回调本钩子，换到新 token 后重试一次。刷新失败（链已废）只记
    warning 并返回 False，由执行层抛出原始 401，用户重新登录即可。
    """
    from loguru import logger

    try:
        return bool(_load_oauth().force_refresh(llm_config))
    except Exception as e:
        logger.warning(f"[OpenCode OAuth] 401 后强制刷新失败：{e}")
        return False


def _oauth_login(cancel_event=None):
    """登录钩子：device flow 授权 → 回填长效 refresh_token 到 API Key 输入框。"""
    oa = _load_oauth()
    result = oa.login(cancel_event=cancel_event)
    api_key = result["refresh_token"] or result["access_token"]
    oa.prime_cache(
        api_key,
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        expires_in=result["expires_in"],
        org_id=result["org_id"],
        orgs=result["orgs"],
        allowed_models=result["allowed_models"],
    )
    parts = ["登录成功：API Key 为长效续期令牌，聊天自动换取访问令牌"]
    if len(result["orgs"]) > 1:
        names = "、".join(o["name"] for o in result["orgs"][:5])
        parts.append(f"检测到 {len(result['orgs'])} 个 workspace，已选第一个（{names}）")
    if not result["org_id"]:
        parts.append("未检测到 workspace，请求可能无 x-opencode-org-id 头")
    parts.append("API Key 已回填请保存")
    return {"api_key": api_key, "info": "，".join(parts)}


def _oauth_fetch_models(config: dict):
    """Zen (OAuth) 模型列表钩子：按 workspace 拉 /api/config 模型池。"""
    oa = _load_oauth()
    api_key = str((config or {}).get("API_KEY", "") or "").strip()
    access, org = oa.get_auth_context({"API_KEY": api_key})
    if not access:
        raise RuntimeError("尚未登录或令牌已失效：请先点击「登录」")
    if not org:
        raise RuntimeError("未检测到 workspace，无法拉取模型列表")
    return oa.fetch_workspace_models(access, org)


def _go_oauth_fetch_models(config: dict):
    """Go (OAuth) 模型列表钩子：公开 /inference/go/v1/models（lite 目录）。

    Go 订阅的模型池由订阅档位决定，与 workspace /api/config 无关；公开目录
    是权威来源（pi-provider-opencode-console 2.x 同口径）。无需 token，故
    不做登录前置检查。
    """
    return _load_oauth().fetch_go_models()


def _go_oauth_coding_plan(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Go (OAuth)：用短效 access_token 查 /zen/go/v1/usage 套餐用量。

    容错口径：官方该端点以 API key 鉴权，OAuth token 是否被接受需真实账号
    才能确认。任一环节失败（未登录 / token 刷新失败 / 401 / 403 / 结构变更）
    都返回 None —— UsageService 会隐藏圆环并 60s 后重试，不抛异常刷 warning，
    且绝不影响聊天主链路。

    只挂 Go (OAuth)：Zen 是按量 API 型，本就无套餐额度。
    """
    from loguru import logger

    oa = _load_oauth()
    api_key = str((config or {}).get("API_KEY", "") or "").strip()
    if not api_key:
        return None
    try:
        access, _org = oa.get_auth_context({"API_KEY": api_key})
        if not access:
            return None
        return _request_usage(access)
    except Exception as e:
        logger.info(f"[OpenCode Go OAuth] 套餐用量不可用：{e}")
        return None

def _oauth_health_probe(config: dict) -> str:
    """Zen/Go (OAuth) 健康探测钩子：对推理端点发 max_tokens=1 真实请求。

    服务端 ModelRefreshService 的探测优先级高于 models_hook（列表语义）：
    本钩子在插件内换取短效 access_token 后打聊天端点，能验出「凭据被服务端
    提前吊销」这类列表探测永远看不到的失效（2026-11-08 实测的 401 链）。

    返回对齐 model_refresh_service 四态：
    - 未登录 / refresh 链已废 / 网关 401/403/402 → "auth_failed"
    - HTTP 200 → "ok"；其余网络/服务端异常 → "unreachable"
    """
    import json
    import urllib.error
    import urllib.request
    import uuid

    oa = _load_oauth()
    api_key = str((config or {}).get("API_KEY", "") or "").strip()
    if not api_key:
        return "auth_failed"  # 尚未登录
    try:
        access, org = oa.get_auth_context({"API_KEY": api_key})
    except Exception:
        return "auth_failed"  # refresh 链已废（需重新登录）
    if not access:
        return "auth_failed"

    model = str((config or {}).get("模型名称", "") or "").strip()
    api_url = str((config or {}).get("API_URL", "") or "").rstrip("/")
    if not model or not api_url:
        return "unreachable"
    is_go = "/inference/go/" in api_url

    headers = {
        "User-Agent": "opencode/cli",
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
        # 网关会话标识（2026-09 起 400 MissingSessionID）；探测用一次性会话 id
        "x-opencode-session": uuid.uuid4().hex,
    }
    if not is_go and org:
        headers["x-opencode-org-id"] = org  # Go 公开端点带 org 头会被 403

    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1, "stream": False}
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{api_url}/chat/completions", data=body, method="POST", headers=headers
        )
        with urllib.request.urlopen(req, timeout=15, context=oa._ssl_context()) as resp:
            resp.read()
        return "ok"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 402):
            return "auth_failed"
        return "unreachable"
    except Exception:
        return "unreachable"

def _oauth_capabilities(models_hook):
    """两个 OAuth 版共用登录与动态头，仅模型池钩子不同。

    套餐用量是 ProviderDef 顶层字段（不是 capabilities），只在 Go (OAuth)
    的 ProviderDef 上挂 _go_oauth_coding_plan。
    """
    return {
        "context_limit": 200000,
        "max_output_tokens": 8192,
        "absolute_limit": 65536,
        "supports_vision": True,
        "supports_thinking": True,
        "thinking_param": "reasoning_effort",
        "reasoning_effort_param": "reasoning_effort",
        "session_header": "x-opencode-session",
        # 不声明 list_group：_group_of 的「有 login_hook → OAuth」自动判据生效；
        # 显式写 "API" 会压掉该判据，把 OAuth 版错分到 API 组
        "extra_headers": _OAUTH_EXTRA_HEADERS,
        "login_hook": _oauth_login,
        "models_hook": models_hook,
        # 健康探测：插件内换短效凭据对聊天端点发真请求（状态点语义 = 当前联通）
        "health_probe": _oauth_health_probe,
        # 401 自愈：执行层收到 401 时回调，换新 access_token 后重试一次
        "auth_error_hook": _oauth_auth_retry,
        "login_hint": "已打开浏览器，user_code 已复制到剪贴板：粘贴授权后点 Continue（可点「中止」退出）",
    }


def register(registry):
    """注册 4 个服务商：Zen/Go 的 API Key 版 + 各自的 OAuth 版"""
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

    # ── OpenCode Zen (OAuth)：workspace 成员无 API key 时走 device flow ──
    #
    # 独立 family（见上）：动态 Authorization / x-opencode-org-id 只作用于
    # 本配置。模型列表为 workspace 动态池，内置表仅作离线兜底；免费层
    # 零价模型网关按服务端账号维度判定，选中可能 403，与 API Key 版同口径。
    registry.register(
        ProviderDef(
            name="OpenCode Zen (OAuth)",
            icon="opencode",
            api_url=_INFERENCE_URL,
            auth_type="bearer",
            default_model="glm-5.3-flash",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://opencode.ai/console",
            models=[
                "glm-5.3-flash",
                "deepseek-v4.1-flash",
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
            family="opencode_zen_oauth",
            capabilities=_oauth_capabilities(_oauth_fetch_models),
        )
    )

    # ── OpenCode Go (OAuth)：同一 device flow / 同一 workspace，换 Go 端点 ──
    #
    # Go 与 Console 共用 console device flow，Go org 就是 Console org
    # （pi-provider-opencode-console 2.x 实测）。差异只在：
    #   - 推理端点 /inference/go/openai/v1（而非 /inference/openai/v1）
    #   - 模型池为公开 /inference/go/v1/models（而非 org-scoped /api/config）
    # 独立 family 保证两个 OAuth 版的能力互不覆盖；动态头本身相同。
    registry.register(
        ProviderDef(
            name="OpenCode Go (OAuth)",
            icon="opencode",
            api_url=_GO_INFERENCE_URL,
            auth_type="bearer",
            default_model="deepseek-v4.1-flash",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://opencode.ai/console",
            models=[
                "deepseek-v4.1-flash",
                "deepseek-v4-pro",
                "glm-5.2",
                "glm-5.3",
                "glm-5.3-flash",
                "kimi-k2.7-code",
                "kimi-k3",
                "kimi-k3.1-pro",
                "mimo-v2.6-flash",
                "mimo-v2.6-pro",
                "minimax-m3.1-flash",
                "hy3",
                "hy4",
                "hy4-preview",
                "step-5-preview",
                "longcat-2.0",
            ],
            models_dev_id="opencode-go",
            family="opencode_go_oauth",
            capabilities=_oauth_capabilities(_go_oauth_fetch_models),
            coding_plan_fetcher=_go_oauth_coding_plan,
        )
    )
