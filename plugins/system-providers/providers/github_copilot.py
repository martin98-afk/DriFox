# -*- coding: utf-8 -*-
"""服务商插件 — GitHub Copilot（api.githubcopilot.com，OAuth 登录制）。

接入形态对齐 CodeBuddy 先例：
    API Key 字段存 GitHub access token（gho_，长效数月 → config_id 稳定、原生加密链）。
    Authorization 头为可调用值：chat_worker 每次请求传入 llm_config，按 gho 从
    本地缓存取短效 Copilot token（约 30 分钟，临期 5 分钟自动刷新）。

登录：OAuth Device Flow（copilot_auth.login）——弹浏览器打开 github.com/login/device，
user_code 自动复制到剪贴板，用户粘贴后轮询回填 gho。

伪装头：Copilot-Integration-Id / Editor-Version / Editor-Plugin-Version 为社区
实现必备（缺 Copilot-Integration-Id 网关 403）。

模型池：按订阅而定，权威来源为 /models 端点（models_hook）；内置列表仅兜底。

风险提示：非官方客户端协议实现，存在违反服务条款与封号风险，自行评估。
"""
import hashlib
import json
import threading
import time
from pathlib import Path

from app.plugins.registries.provider_registry import ProviderDef
from app.utils.utils import get_app_data_dir

_API_ENDPOINT = "https://api.githubcopilot.com"
_CACHE_DIR = get_app_data_dir() / "github_copilot" / "cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_REFRESH_SKEW_SEC = 5 * 60  # Copilot token 约 30 分钟有效，剩 5 分钟即视为临期

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}  # github_token -> {"copilot_token", "expires_at"}


def _load_auth_module():
    """按路径加载同目录认证模块（providers 目录非包，loader 按文件加载）。"""
    import importlib.util

    auth_path = Path(__file__).with_name("copilot_auth.py")
    spec = importlib.util.spec_from_file_location("github_copilot_copilot_auth", auth_path)
    ca = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ca)
    return ca


def _cache_path(github_token: str) -> Path:
    digest = hashlib.sha1(github_token.encode("utf-8")).hexdigest()[:12]
    return _CACHE_DIR / f"{digest}.json"


def _cache_get(github_token: str) -> dict:
    with _CACHE_LOCK:
        if github_token in _CACHE:
            return _CACHE[github_token]
    try:
        entry = json.loads(_cache_path(github_token).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        entry = {}
    with _CACHE_LOCK:
        _CACHE[github_token] = entry
    return entry


def _cache_put(github_token: str, copilot_token: str, expires_at: float) -> None:
    entry = {"copilot_token": copilot_token, "expires_at": expires_at}
    with _CACHE_LOCK:
        _CACHE[github_token] = entry
    _cache_path(github_token).write_text(json.dumps(entry), encoding="utf-8")


def _exchange_copilot_token(github_token: str) -> str:
    """按 gho 取可用 Copilot token：缓存命中（未临期）直接返回，否则重新换取。"""
    entry = _cache_get(github_token)
    if entry.get("copilot_token") and entry.get("expires_at", 0) > time.time() + _REFRESH_SKEW_SEC:
        return entry["copilot_token"]
    ca = _load_auth_module()
    data = ca.fetch_copilot_token(github_token)
    expires_at = float(data.get("expires_at") or (time.time() + 25 * 60))
    _cache_put(github_token, data["token"], expires_at)
    return data["token"]


def _auth_header(llm_config: dict) -> str:
    """动态 Authorization 头：按当前配置的 API_KEY（gho）换取短效 Copilot token。"""
    github_token = str((llm_config or {}).get("API_KEY", "") or "").strip()
    if not github_token:
        return ""
    try:
        return f"Bearer {_exchange_copilot_token(github_token)}"
    except Exception:
        return ""  # 换取失败回退空头（报错由请求层透出）


# 伪装头（社区实现必备：缺 Copilot-Integration-Id 网关 403）。
# Authorization 为可调用值：chat_worker 每次请求传入 llm_config 按配置各号取 token。
_EXTRA_HEADERS = {
    "Authorization": _auth_header,
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "vscode/1.95.0",
    "Editor-Plugin-Version": "copilot-chat/0.20.2",
}


def _auto_login():
    """登录钩子（编辑卡后台线程调用）：Device Flow 授权，回填长效 gho token。"""
    from loguru import logger

    ca = _load_auth_module()
    logger.info("[GitHub Copilot] 发起 Device Flow 登录（请求 github.com/login/device_code）")
    try:
        result = ca.login(timeout_sec=280)
    except Exception as e:
        logger.warning(f"[GitHub Copilot] 登录失败: {e}")
        raise
    github_token = result["github_token"]
    logger.info("[GitHub Copilot] 授权成功，预热短效 Copilot token")
    # 预热：立即换一次 Copilot token，保存配置后立即可聊天
    _exchange_copilot_token(github_token)
    info = (
        "登录成功（GitHub）：API Key 为长效 gho 令牌，聊天自动换取短效 Copilot token；"
        "user_code 已复制到剪贴板"
    )
    return {"api_key": github_token, "info": info + "，API Key 已回填请保存"}


def _fetch_models(config):
    """模型列表获取钩子：按本配置的 gho 拉取 /models（权威池，随订阅而异）。

    过滤规则：policy.state == "disabled" 的剔除（订阅/管理员未启用）。

    ⚠ 不能用 model_picker_enabled 过滤（2026-10-09 实测修正）：免费版
    （sku=free_limited_copilot）可用模型 gpt-4o / gpt-4o-mini 等该字段均为
    False，而 picker=True 的新模型 claude-fable-5 / kimi-k3 等反而 400
    model_not_supported。订阅级可用性无静态字段可判别（policy / endpoints /
    vendor 均试过无效），只能实测。每项的 vendor / id 记入日志，便于诊断
    「选了仍不支持」类问题。
    """
    from loguru import logger

    import urllib.request

    github_token = str((config or {}).get("API_KEY", "") or "").strip()
    if not github_token:
        raise RuntimeError("尚未登录：请先点击「自动登录」")
    ca = _load_auth_module()
    token = _exchange_copilot_token(github_token)
    req = urllib.request.Request(
        f"{_API_ENDPOINT}/models", headers=ca.build_chat_headers(token)
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    models = (payload.get("data") or []) if isinstance(payload, dict) else []

    out: list = []
    skipped: list = []
    for x in models:
        if not isinstance(x, dict):
            continue
        mid = x.get("id")
        if not mid:
            continue
        policy = x.get("policy") or {}
        if str(policy.get("state", "enabled")).lower() == "disabled":
            skipped.append(f"{mid}(disabled)")
            continue
        out.append(mid)
        logger.info(f"[GitHub Copilot] 可用模型: {mid} vendor={x.get('vendor')}")
    if skipped:
        logger.info(f"[GitHub Copilot] 过滤不可用模型 {len(skipped)} 个: {', '.join(skipped[:20])}")
    logger.info(f"[GitHub Copilot] 模型池: 共 {len(models)} 项，可用 {len(out)} 个")
    return out


# 内置兜底（权威池以 models_hook 拉取的 /models 为准）
# 实测修正（2026-10-09，免费版 sku=free_limited_copilot）：原列表 o1 / o3-mini /
# claude-3.5-sonnet / claude-3.7-sonnet / gemini-2.0-flash-001 已全部 400
# model_not_supported，换成下列实测 200 的稳定项。
_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4o-2024-11-20",
    "gpt-4o-mini-2024-07-18",
]


def register(registry):
    registry.register(
        ProviderDef(
            name="GitHub Copilot",
            icon="github",
            api_url=_API_ENDPOINT,
            auth_type="bearer",
            default_model="gpt-4o",
            default_params={"温度": 0.7, "最大Token": 8192},
            register_url="https://github.com/features/copilot",
            models=_MODELS,
            family="github_copilot",
            capabilities={
                "extra_headers": _EXTRA_HEADERS,
                "login_hook": _auto_login,
                "models_hook": _fetch_models,
                # 编辑卡「登录」InfoBar 的提示文案：GitHub device 页强制手输 code
                #（不认 URL 预填，安全设计），告知 code 已在剪贴板
                "login_hint": "已打开 GitHub 授权页，user_code 已复制到剪贴板：粘贴后点 Continue（最长等待 5 分钟）",
            },
        )
    )
