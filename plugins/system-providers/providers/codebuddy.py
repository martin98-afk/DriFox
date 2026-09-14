# -*- coding: utf-8 -*-
"""服务商插件 — 腾讯 CodeBuddy（copilot.tencent.com 模型池）。

伪装 CodeBuddyIDE 客户端接入：身份标识（X-Domain / X-Product / X-Product-Code /
User-Agent）经 capabilities["extra_headers"] 静态头通道注入每个 LLM 请求，
主程序（chat_worker._provider_extra_headers）通用消费，无需感知具体服务商。

模型池：capabilities["models_hook"] 拉远端 /v3/config 实时列表。
自动登录：capabilities["login_hook"]，弹浏览器授权后回填 API Key 输入框。
自动续期：access_token 有效期 72h，守护线程每 6h 用 refresh_token 刷新，
    新 token 直写 _EXTRA_HEADERS["Authorization"]（family_capabilities 浅
    合并同引用，chat 请求实时生效；不回写 SavedProviders，config_id 不漂移）。
积分余额：balance_fetcher 汇总 get-user-resource 各资源包剩余（IDE 积分面板
    同款接口），每日自动签到在刷新循环内完成（status 幂等）。

凭据缓存：<app_data>/codebuddy/（access_token.txt / refresh_token.txt），
    不落仓库目录。CLI：python buddy_auth.py {login,refresh,checkin,credits}。

风险提示：非官方客户端协议实现，存在违反服务条款与封号风险，自行评估。
"""
import json
import threading
import time
from pathlib import Path

from app.plugins.registries.provider_registry import ProviderDef
from app.utils.utils import get_app_data_dir

_ENDPOINT = "https://copilot.tencent.com"
_CRED_DIR = get_app_data_dir() / "codebuddy"
_CRED_DIR.mkdir(parents=True, exist_ok=True)
_TOKEN_FILE = _CRED_DIR / "access_token.txt"
_REFRESH_FILE = _CRED_DIR / "refresh_token.txt"
_REFRESH_INTERVAL_SEC = 6 * 3600  # token 72h 有效期，6h 刷一次足够

# 伪装身份头（真机校准 2026-09；值取自 CodeBuddyIDE 1.106.1）。
# Authorization 由登录/续期链路动态维护：None = 不注入（回退 API_KEY 鉴权）。
_EXTRA_HEADERS = {
    "X-Domain": "copilot.tencent.com",
    "X-Product": "SaaS",
    "X-Product-Code": "codebuddy",
    "User-Agent": "CodeBuddyIDE/1.106.1",
    "Authorization": None,
}

# 内置兜底模型目录（远端不可用时可用；正式列表以 models_hook 实时拉取为准）
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


def _current_token() -> str:
    """当前可用 access_token：内存优先，回退落盘文件。"""
    auth = _EXTRA_HEADERS.get("Authorization")
    if isinstance(auth, str) and auth.startswith("Bearer "):
        return auth[7:]
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text(encoding="utf-8").strip()
    return ""


def _set_token(access_token: str) -> None:
    """更新内存头 + 落盘（chat 请求经 extra_headers 注入实时生效）。"""
    _EXTRA_HEADERS["Authorization"] = f"Bearer {access_token}"
    _TOKEN_FILE.write_text(access_token, encoding="utf-8")


def _load_auth_module():
    """按路径加载同目录认证模块（providers 目录非包，loader 按文件加载）。"""
    import importlib.util

    auth_path = Path(__file__).with_name("buddy_auth.py")
    spec = importlib.util.spec_from_file_location("codebuddy_buddy_auth", auth_path)
    ba = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ba)
    return ba


def _auto_login():
    """自动登录钩子（编辑卡后台线程调用）：弹浏览器授权，返回回填凭据。"""
    ba = _load_auth_module()
    result = ba.login(timeout_sec=280)
    access_token = result["access_token"]
    refresh_token = result.get("refresh_token", "")
    nickname = (result.get("account") or {}).get("nickname") or "CodeBuddy"
    info = f"登录成功（{nickname}）"

    _set_token(access_token)
    if refresh_token:
        _REFRESH_FILE.write_text(refresh_token, encoding="utf-8")

    # 顺带带出签到状态（积分无余额接口，连签天数是唯一可见口径）
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
    return {"api_key": access_token, "info": info + "，API Key 已回填请保存"}


def _fetch_models():
    """模型列表获取钩子（编辑卡「获取模型列表」调用）：拉 /v3/config 实时模型池。"""
    import urllib.request

    token = _current_token()
    if not token:
        raise RuntimeError("尚未登录：请先点击「自动登录」")
    req = urllib.request.Request(
        f"{_ENDPOINT}/v3/config", headers=_load_auth_module().build_headers(token)
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        cfg = json.loads(resp.read().decode())
    models = (cfg.get("data") or {}).get("models") or []
    return [x.get("id") for x in models if isinstance(x, dict) and x.get("id")]


def _fetch_balance(config):
    """余额查询（DeepSeek 余额同款通道）：返回当前可用积分合计。"""
    token = _current_token() or (config or {}).get("API_KEY", "")
    if not token:
        return None
    ba = _load_auth_module()
    left = sum(
        float(r.get("CycleCapacityRemainPrecise") or 0) for r in ba.user_resources(token)
    )
    return {"balance": round(left, 1), "currency": ""}


def _refresh_loop():
    """守护循环：启动即刷新一次，之后每 6h 用 refresh_token 换新 access_token。"""
    while True:
        try:
            rt = _REFRESH_FILE.read_text(encoding="utf-8").strip() if _REFRESH_FILE.exists() else ""
            if rt:
                ba = _load_auth_module()
                new = ba.refresh(rt)
                if new["access_token"]:
                    _set_token(new["access_token"])
                    if new.get("refresh_token"):
                        _REFRESH_FILE.write_text(new["refresh_token"], encoding="utf-8")
                # 每日自动签到（status 幂等：已签/未开启直接跳过）
                token = _current_token()
                if token:
                    status = ba.checkin_status(token)
                    if (
                        isinstance(status, dict)
                        and status.get("active")
                        and not (status.get("today_checked_in") or status.get("todayCheckedIn"))
                    ):
                        ba.checkin_claim(token)
        except Exception:
            pass  # 网络/凭据异常下轮再试，守护线程不允许退出
        time.sleep(_REFRESH_INTERVAL_SEC)


def _bootstrap():
    """注册时恢复 token 与守护线程（热重载重复启动无害：daemon 且幂等写）。"""
    token = _current_token()
    if token:
        _EXTRA_HEADERS["Authorization"] = f"Bearer {token}"
    threading.Thread(target=_refresh_loop, daemon=True).start()


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
