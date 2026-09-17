# -*- coding: utf-8 -*-
"""服务商插件 — 腾讯 CodeBuddy（copilot.tencent.com 模型池）。

伪装 CodeBuddyIDE 客户端接入：身份标识（X-Domain / X-Product / X-Product-Code /
User-Agent）经 capabilities["extra_headers"] 注入每个 LLM 请求，主程序
（chat_worker._provider_extra_headers）通用消费，无需感知具体服务商。

多账号模型：一条服务商配置 = 一个号。
    API Key 字段存该号的 refresh_token（长效不变 → config_id 稳定、原生加密链）。
    Authorization 头为可调用值：chat_worker 每次请求传入 llm_config，按
    refresh_token 从本地缓存取 access_token（临期自动刷新）——各配置各号，
    余额各显，没余额用户自行切换配置。
自动签到：守护线程遍历缓存内全部号，临期刷新 + 每日自动签到（status 幂等）。

凭据缓存：<app_data>/codebuddy/cache/<hash>.json，不落仓库目录。
    CLI：python buddy_auth.py {login,refresh,checkin,credits}。

风险提示：非官方客户端协议实现，存在违反服务条款与封号风险，自行评估。
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from app.plugins.registries.provider_registry import ProviderDef
from app.utils.utils import get_app_data_dir

_ENDPOINT = "https://copilot.tencent.com"
_CACHE_DIR = get_app_data_dir() / "codebuddy" / "cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_REFRESH_INTERVAL_SEC = 6 * 3600  # access_token 实测 72h，6h 一轮足够
_TOKEN_TTL_SEC = 72 * 3600  # 服务端未回过期时间，按实测有效期记

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}  # refresh_token -> {"access_token","expires_at","refresh_token"}


def _load_auth_module():
    """按路径加载同目录认证模块（providers 目录非包，loader 按文件加载）。"""
    import importlib.util

    auth_path = Path(__file__).with_name("buddy_auth.py")
    spec = importlib.util.spec_from_file_location("codebuddy_buddy_auth", auth_path)
    ba = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ba)
    return ba


def _cache_path(refresh_token: str) -> Path:
    digest = hashlib.sha1(refresh_token.encode("utf-8")).hexdigest()[:12]
    return _CACHE_DIR / f"{digest}.json"


def _cache_get(refresh_token: str) -> dict:
    with _CACHE_LOCK:
        if refresh_token in _CACHE:
            return _CACHE[refresh_token]
    try:
        entry = json.loads(_cache_path(refresh_token).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        entry = {}
    entry.setdefault("refresh_token", refresh_token)
    with _CACHE_LOCK:
        _CACHE[refresh_token] = entry
    return entry


def _cache_put(refresh_token: str, access_token: str) -> None:
    entry = {
        "refresh_token": refresh_token,
        "access_token": access_token,
        "expires_at": time.time() + _TOKEN_TTL_SEC,
    }
    with _CACHE_LOCK:
        _CACHE[refresh_token] = entry
    _cache_path(refresh_token).write_text(json.dumps(entry), encoding="utf-8")


def _exchange_access_token(refresh_token: str, max_skew_sec: float = 3600.0) -> str:
    """按 refresh_token 取可用 access_token：缓存命中（未临期）直接返回，否则刷新。"""
    entry = _cache_get(refresh_token)
    if entry.get("access_token") and entry.get("expires_at", 0) > time.time() + max_skew_sec:
        return entry["access_token"]
    new = _load_auth_module().refresh(refresh_token)
    if not new.get("access_token"):
        raise RuntimeError("刷新未返回 access_token")
    _cache_put(refresh_token, new["access_token"])
    return new["access_token"]


def _auth_header(llm_config: dict) -> str:
    """动态 Authorization 头：按当前配置的 API_KEY（refresh_token）换取。"""
    refresh_token = str((llm_config or {}).get("API_KEY", "") or "").strip()
    if not refresh_token:
        return ""
    try:
        return f"Bearer {_exchange_access_token(refresh_token)}"
    except Exception:
        return ""  # 换取失败回退空头（SDK 用 API_KEY 鉴权，报错由请求层透出）


# 伪装身份头（真机校准 2026-09；值取自 CodeBuddyIDE 1.106.1）。
# Authorization 为可调用值：chat_worker 每次请求传入 llm_config，按配置各号取 token。
_EXTRA_HEADERS = {
    "X-Domain": "copilot.tencent.com",
    "X-Product": "SaaS",
    "X-Product-Code": "codebuddy",
    "User-Agent": "CodeBuddyIDE/1.106.1",
    "Authorization": _auth_header,
}

# 内置兜底模型目录（远端不可用时可用；正式列表以远端 /v3/config 为准）
_MODELS = [
    "auto",
    "hy4-preview",
    "hy3",
    "hy3-x",
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "glm-5.3",
    "glm-5.3-flash",  # benefit 免费额度模型
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "kimi-k3-2",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "minimax-m3-pay",
]


def _auto_login():
    """自动登录钩子（编辑卡后台线程调用）：弹浏览器授权，回填长效 refresh_token。"""
    ba = _load_auth_module()
    result = ba.login(timeout_sec=280)
    access_token = result["access_token"]
    refresh_token = result.get("refresh_token", "")
    account = result.get("account") or {}
    nickname = account.get("nickname") or "CodeBuddy"

    api_key = refresh_token or access_token
    if refresh_token:
        _cache_put(refresh_token, access_token)  # 预热：保存后立即可聊天

    info = f"登录成功（{nickname}）：API Key 为长效续期令牌，聊天自动换取访问令牌"

    # 顺带带出签到状态
    try:
        status = ba.checkin_status(access_token)
        streak = status.get("streak_days")
        today = status.get("today_checked_in")
        daily = status.get("daily_credit")
        if isinstance(streak, int):
            state = "今日已签" if today else f"今日未签（领 {daily} 积分）"
            info += f"，连签 {streak} 天，{state}"
    except Exception:
        pass
    return {"api_key": api_key, "info": info + "，API Key 已回填请保存"}


def _fetch_models(config):
    """模型列表获取钩子（签名对齐 balance_fetcher）：按本配置的号拉 /v3/config。"""
    import urllib.request

    refresh_token = str((config or {}).get("API_KEY", "") or "").strip()
    if not refresh_token:
        raise RuntimeError("尚未登录：请先点击「自动登录」")
    token = _exchange_access_token(refresh_token)
    req = urllib.request.Request(
        f"{_ENDPOINT}/v3/config", headers=_load_auth_module().build_headers(token)
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        cfg = json.loads(resp.read().decode())
    models = (cfg.get("data") or {}).get("models") or []
    return [x.get("id") for x in models if isinstance(x, dict) and x.get("id")]


def _fetch_balance(config):
    """余额查询（DeepSeek 余额同款通道）：按本配置的号返回可用积分合计。"""
    refresh_token = str((config or {}).get("API_KEY", "") or "").strip()
    if not refresh_token:
        return None
    try:
        token = _exchange_access_token(refresh_token)
    except Exception:
        return {"hide": True, "tooltip": "登录凭据失效，请重新自动登录"}
    ba = _load_auth_module()
    left = sum(
        float(r.get("CycleCapacityRemainPrecise") or 0) for r in ba.user_resources(token)
    )
    return {"balance": round(left, 1), "currency": ""}


# daemon 守护线程与解释器拆解竞态的防护：
# - 退出信标：aboutToQuit 置位，线程在分段睡眠间隙退出，不再 exec auth 模块
# - 预热：启动时先加载一次 buddy_auth（sys.modules 缓存），退出期 exec
#   命中缓存 import，避免「拆解中首次 import email/urllib」的 Windows
#   fatal exception 0x8001010D（pytest/开发进程退出必现）
_STOP_EVENT = threading.Event()
_THREAD_STARTED = False


def _refresh_loop():
    """守护循环：遍历缓存内全部号 —— 临期刷新 access_token + 每日自动签到（幂等）。"""
    try:
        _load_auth_module()  # 预热：退出期不再首次加载 auth 模块
    except Exception:
        pass
    while not _STOP_EVENT.is_set():
        try:
            ba = _load_auth_module()
            for path in sorted(_CACHE_DIR.glob("*.json")):
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    rt = entry.get("refresh_token", "")
                    if not rt:
                        continue
                    at = entry.get("access_token", "")
                    if entry.get("expires_at", 0) <= time.time() + 24 * 3600:
                        new = ba.refresh(rt)
                        if new.get("access_token"):
                            at = new["access_token"]
                            rt = new.get("refresh_token") or rt
                            _cache_put(rt, at)
                            if _cache_path(entry.get("refresh_token", "")) != _cache_path(rt):
                                path.unlink(missing_ok=True)
                            path = _cache_path(rt)
                    if at:
                        status = ba.checkin_status(at)
                        if (
                            isinstance(status, dict)
                            and status.get("active")
                            and not (
                                status.get("today_checked_in")
                                or status.get("todayCheckedIn")
                            )
                        ):
                            ba.checkin_claim(at)
                except Exception:
                    continue  # 单号失败不影响其余号
        except Exception:
            pass  # 守护线程不允许退出
        # 分段睡眠：退出信标置位后最快 1s 内退出（整段 6h 会挡住退出）
        for _ in range(_REFRESH_INTERVAL_SEC):
            if _STOP_EVENT.is_set():
                return
            time.sleep(1)


def _bootstrap():
    """注册时启动守护线程（热重载重复启动无害：daemon 且幂等写）。

    测试/CI 环境（DRIFOX_NO_CODEBUDDY_REFRESH=1）不启动：守护线程的
    urlopen/DNS 解析与解释器退出存在竞态（Windows fatal 0x8001010D），
    测试进程退出期会被它击中。
    """
    global _THREAD_STARTED
    if _THREAD_STARTED:
        return
    if os.environ.get("DRIFOX_NO_CODEBUDDY_REFRESH") == "1":
        return
    _THREAD_STARTED = True
    _STOP_EVENT.clear()
    threading.Thread(target=_refresh_loop, daemon=True).start()
    try:
        from PyQt5.QtCore import QCoreApplication

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(_STOP_EVENT.set)
    except Exception:
        pass


def register(registry):
    _bootstrap()
    registry.register(
        ProviderDef(
            name="CodeBuddy",
            icon="codebuddy",
            api_url="https://copilot.tencent.com/v2",
            auth_type="bearer",
            default_model="glm-5.3-flash",
            default_params={"温度": 0.7, "最大Token": 8192},
            register_url="https://www.codebuddy.cn/",
            models=_MODELS,
            family="codebuddy",
            capabilities={
                "extra_headers": _EXTRA_HEADERS,
                "login_hook": _auto_login,
                "models_hook": _fetch_models,
            },
            balance_fetcher=_fetch_balance,
        )
    )
