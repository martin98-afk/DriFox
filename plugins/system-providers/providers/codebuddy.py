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
每日任务：签到后顺带跑成长中心闭环 —— 活跃上报 → 领养 → 任务 accept → 行为事件链
    （桌面指纹 / 真实对话）→ Web 域领奖 → 连登兑换与抽奖 → 猫旅行 → 新手礼包。
    幂等靠任务进度判定；DRIFOX_CODEBUDDY_TASKS=0 可整体关闭。

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
from app.plugins.sdk import get_app_data_dir

_ENDPOINT = "https://copilot.tencent.com"
_CACHE_DIR = get_app_data_dir() / "codebuddy" / "cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_REFRESH_INTERVAL_SEC = 6 * 3600  # access_token 实测 72h，6h 一轮足够
_TOKEN_TTL_SEC = 72 * 3600  # 服务端未回过期时间，按实测有效期记

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {}  # refresh_token -> {"access_token","expires_at","refresh_token"}


# ── 在用号注册表 ─────────────────────────────────────────
#
# 缓存目录里会累积上百个历史号（每次登录一份），全部跑每日任务既慢又徒增
# 风控暴露面。真正要维护的是「配置里在用的号」——判定依据是它们被主程序
# 摸到过：聊天取 token、余额查询、模型列表拉取、自动登录，这些钩子收到的
# 都是**已解密的明文 API_KEY**（refresh_token），登记进 active.json 即可。
#
# 签到保持既有行为（全量缓存号），只有每日任务收窄到活跃集。
_ACTIVE_FILE = _CACHE_DIR.parent / "active.json"
_ACTIVE_TTL_DAYS = 30
_ACTIVE_SAVE_INTERVAL_SEC = 60.0
_FORCE_ALL = os.environ.get("DRIFOX_CODEBUDDY_TASKS_ALL", "").strip() == "1"

_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict = {}
_ACTIVE_LOADED = False
_ACTIVE_SAVED_AT = [0.0]


def _load_active() -> dict:
    """读活跃号注册表（{缓存文件 stem: 最后登记时间}），失败按空表。"""
    global _ACTIVE, _ACTIVE_LOADED
    if _ACTIVE_LOADED:
        return _ACTIVE
    data: dict = {}
    try:
        raw = json.loads(_ACTIVE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = {str(k): float(v) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        data = {}
    _ACTIVE = data
    _ACTIVE_LOADED = True
    return _ACTIVE


def _save_active(force: bool = False) -> None:
    """落盘活跃号注册表（节流：默认 60s 最多一次，退出丢失最后几十秒无妨）。"""
    now = time.time()
    if not force and now - _ACTIVE_SAVED_AT[0] < _ACTIVE_SAVE_INTERVAL_SEC:
        return
    _ACTIVE_SAVED_AT[0] = now
    try:
        _ACTIVE_FILE.write_text(json.dumps(_load_active()), encoding="utf-8")
    except OSError:
        pass


def _mark_active(refresh_token: str) -> None:
    """登记在用号：被聊天/余额/模型/登录任一钩子摸到即算。"""
    if not refresh_token:
        return
    try:
        stem = _cache_path(refresh_token).stem
    except Exception:
        return
    with _ACTIVE_LOCK:
        _load_active()[stem] = time.time()
        _save_active()


def _config_refresh_tokens() -> list:
    """从主程序配置读本 provider 的在用号 refresh_token。

    Settings 启动时已把 enc:v2 密文就地解密成内存明文（keyring 或密码模式解锁后），
    因此这里能直接拿到 refresh_token。读不到（CLI 无 Settings / 密码模式未解锁 /
    接口变更）返回空表，由活跃号注册表兜底。
    """
    try:
        from app.utils.config import Settings

        getter = getattr(Settings, "get_instance", None)
        settings = getter() if callable(getter) else Settings()
        saved = settings.llm_saved_providers.value or {}
    except Exception:
        return []
    if not isinstance(saved, dict):
        return []
    out = []
    for cfg in saved.values():
        if not isinstance(cfg, dict):
            continue
        if "copilot.tencent.com" not in str(cfg.get("API_URL") or ""):
            continue
        key = str(cfg.get("API_KEY") or "").strip()
        if key and not key.startswith("enc:"):
            out.append(key)
    return out


_CONFIG_TOKENS: list = []  # 主线程预热的配置号 refresh_token（守护线程只读副本）


def _warm_config_tokens() -> None:
    """主线程预热配置号清单：QConfig 是 QObject，跨线程读有风险，只在此取。

    由 _bootstrap（主线程）与 llm_saved_providers.valueChanged（主线程信号）
    调用；守护线程只读 _CONFIG_TOKENS 的副本。
    """
    try:
        tokens = _config_refresh_tokens()
    except Exception:
        return
    if tokens:
        _CONFIG_TOKENS[:] = tokens  # 切片赋值：读侧 list() 复制无撕裂


def _active_cache_files() -> list:
    """每日任务要维护的缓存文件：活跃集内 TTL 内的号。

    活跃集为空（新装 / 重启后还没摸过任何号）时回退最近 3 个缓存文件，
    避免一个都不跑；DRIFOX_CODEBUDDY_TASKS_ALL=1 可强制全量。
    """
    if _FORCE_ALL:
        return sorted(_CACHE_DIR.glob("*.json"))
    found: dict = {}
    # 1) 配置里的号（权威来源：主线程预热进 _CONFIG_TOKENS 的明文 refresh_token）
    for rt in list(_CONFIG_TOKENS):
        path = _cache_path(rt)
        if path.exists():
            found[path] = True
    # 2) 活跃集：被聊天/余额/模型/登录钩子摸过、但配置读取不可用时的兜底
    with _ACTIVE_LOCK:
        data = dict(_load_active())
    cutoff = time.time() - _ACTIVE_TTL_DAYS * 86400
    for stem, ts in data.items():
        if ts < cutoff:
            continue
        path = _CACHE_DIR / f"{stem}.json"
        if path.exists():
            found[path] = True
    if found:
        return sorted(found)
    # 3) 都没读到（新装 / 未解锁 / 首次启动）：只挑最近 3 个，绝不扫历史全量
    recent = sorted(_CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return recent[:3]


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


def _cache_put(refresh_token: str, access_token: str, uid: str = "", nickname: str = "") -> dict:
    """写缓存条目（合并已有附加字段：uid / nickname / daily 不能被刷新覆盖）。"""
    with _CACHE_LOCK:
        entry = dict(_CACHE.get(refresh_token) or {})
    if not entry:
        try:
            entry = json.loads(_cache_path(refresh_token).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            entry = {}
    entry.update(
        {
            "refresh_token": refresh_token,
            "access_token": access_token,
            "expires_at": time.time() + _TOKEN_TTL_SEC,
        }
    )
    if uid:
        entry["uid"] = uid
    if nickname:
        entry["nickname"] = nickname
    with _CACHE_LOCK:
        _CACHE[refresh_token] = entry
    _cache_path(refresh_token).write_text(json.dumps(entry), encoding="utf-8")
    return entry


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
    _mark_active(refresh_token)
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


def _auto_login(cancel_event=None):
    """自动登录钩子（编辑卡后台线程调用）：弹浏览器授权，回填长效 refresh_token。

    Args:
        cancel_event: 可选 threading.Event。用户点「中止」时置位，登录立即退出
            而非干等到超时（旧签名无参调用仍兼容）。
    """
    ba = _load_auth_module()
    try:
        result = ba.login(timeout_sec=280, cancel_event=cancel_event)
    except Exception as e:
        if type(e).__name__ == "LoginCancelled":
            raise RuntimeError("登录已中止") from e
        raise
    access_token = result["access_token"]
    refresh_token = result.get("refresh_token", "")
    account = result.get("account") or {}
    nickname = account.get("nickname") or "CodeBuddy"

    api_key = refresh_token or access_token
    if refresh_token:
        # 预热：保存后立即可聊天；顺带落 uid/nickname（每日任务上报以 uid 作 X-User-Id）
        _cache_put(
            refresh_token,
            access_token,
            uid=str(account.get("uid") or account.get("userId") or ""),
            nickname=nickname,
        )
        # 不等守护线程轮次：登录完成即补跑该号的签到与每日任务
        _mark_active(refresh_token)
        _kickoff_daily(_load_auth_module(), refresh_token)

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


def _fetch_models(config):
    """模型列表获取钩子（签名对齐 balance_fetcher）：按本配置的号拉 /v3/config。"""
    import urllib.request

    refresh_token = str((config or {}).get("API_KEY", "") or "").strip()
    if not refresh_token:
        raise RuntimeError("尚未登录：请先点击「自动登录」")
    _mark_active(refresh_token)
    token = _exchange_access_token(refresh_token)
    req = urllib.request.Request(
        f"{_ENDPOINT}/v3/config", headers=_load_auth_module().build_headers(token)
    )
    with urllib.request.urlopen(req, timeout=30, context=_system_ssl_context()) as resp:
        cfg = json.loads(resp.read().decode())
    models = (cfg.get("data") or {}).get("models") or []
    return [x.get("id") for x in models if isinstance(x, dict) and x.get("id")]


def _fetch_balance(config):
    """余额查询（DeepSeek 余额同款通道）：按本配置的号返回可用积分合计。"""
    refresh_token = str((config or {}).get("API_KEY", "") or "").strip()
    if not refresh_token:
        return None
    _mark_active(refresh_token)
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


# ── 每日任务编排（成长中心 / 活跃上报 / 领奖 / 旅行，2026-10 真机校准）─────
#
# 顺序有硬依赖：活跃上报解锁 first_buddy → 领养后才允许 accept 其余任务
# （本机实测：未领养时 accept 全部返回 prerequisite not met: first_buddy）
# → 行为事件链推进进度 → 达标项走 Web 域领奖。
# 幂等：动作前先读任务进度，已 claimed / 已达标即跳过，跨天自动恢复。
# 开关：环境变量 DRIFOX_CODEBUDDY_TASKS=0 可整体关闭（默认开启）。
_AUTO_TASK_DISABLED = os.environ.get("DRIFOX_CODEBUDDY_TASKS", "").strip() == "0"

_THEME_KEY = "theme-tkmw7j"  # 和平精英激战金秋：Hp_Appearance 判据主题
_BUDDY_APP = ("cb_y5Dy46tPQGGWtueMxXbe", "企鹅教师助手")
_LIBRARY_URL = "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm"
_PLAYBOOK_CASE = ("pm-gtm-launch-plan", "新产品上市 GTM 发布计划一页纸")
_TEMPLATES = (
    ("1", "深度研究"),
    ("2", "周报生成"),
    ("3", "竞品分析"),
    ("4", "活动策划"),
    ("5", "代码评审"),
)
_LIGHTHOUSE_EXPERT_ID = "ex_2cvvUZQhDyeJ"  # 腾讯轻量云专家（固定 id）
_REPORT_GAP = 1.2
_EXPERT_GAP = 6.0
_LOTTERY_MAX_DRAW = 20  # 单轮抽奖上限（防上游异常返回无限次数时打空）
_CLAIM_SETTLE_SEC = 3.0  # 行为事件后等上游异步计分落定的时间（领奖前置）

# 专家使用类任务的单次真实对话提示（越短越省积分；判据只认请求落库）
_CHAT_PROMPT = "1+1等于几？直接回答。"
# 真实对话用免费额度模型（fast-model 计费）；glm-5.2 的两处是任务判据要求，勿改
_CHAT_MODEL = "hy3"



def _save_entry(entry: dict) -> None:
    """把带附加字段（uid / nickname / daily）的缓存条目写回磁盘。"""
    rt = str(entry.get("refresh_token") or "")
    if not rt:
        return
    try:
        _cache_path(rt).write_text(json.dumps(entry), encoding="utf-8")
    except OSError:
        pass


def _task_map(ba, at: str, uid: str) -> dict:
    """任务列表转 {task_code: task}；接口异常返回空表（当日按无任务处理）。"""
    try:
        return {str(t.get("task_code")): t for t in ba.growth_tasks(at, uid) if isinstance(t, dict)}
    except Exception:
        return {}


def _task_progress(task: dict) -> tuple:
    """取 (current, target)；progress 缺失按 0/1 处理。"""
    prog = task.get("progress") or {}
    try:
        cur = int(prog.get("current") or 0)
        tgt = int(prog.get("target") or 0)
    except (TypeError, ValueError):
        cur, tgt = 0, 0
    return cur, (tgt if tgt > 0 else 1)


def _needs_action(task: dict) -> bool:
    """是否还需要做行为动作（已达标或已领的跳过）。"""
    if not task:
        return False
    if str(task.get("accept_status") or "") == "claimed":
        return False
    if str(task.get("status") or "") == "completed":
        return False
    cur, tgt = _task_progress(task)
    return cur < tgt


def _claimable(task: dict) -> bool:
    """是否可领奖（达标但未领）。"""
    if not task:
        return False
    if str(task.get("accept_status") or "") == "claimed":
        return False
    if str(task.get("status") or "") == "completed":
        return True
    cur, tgt = _task_progress(task)
    return cur >= tgt


def _run_desktop_chain(ba, at: str, uid: str, nickname: str, tasks: dict, done: dict) -> list:
    """桌面指纹事件链（RichMeow / Buddy_App / automation / template / playbook / canvas / 主题）。"""
    from loguru import logger

    acted: list = []
    ms = int(time.time() * 1000)

    def fire(key: str, events: list) -> bool:
        try:
            resp = ba.report_desktop_events(at, uid, nickname, events)
            if int(resp.get("code", -1)) == 0:
                done[key] = True
                acted.append(key)
                return True
            logger.warning(f"[CodeBuddy] {key} 上报失败: {str(resp)[:160]}")
        except Exception as e:
            logger.warning(f"[CodeBuddy] {key} 上报异常: {e}")
        return False

    if _needs_action(tasks.get("RichMeow_Chat")) and not done.get("RichMeow_Chat"):
        conv, req = f"drifox-rm-{ms}", f"drifox-rm-req-{ms}"
        fire("RichMeow_Chat", ba.desktop_chat_sequence(conv, req, f"req-{ms}-user"))

    if (
        _needs_action(tasks.get("Buddy_App")) or _needs_action(tasks.get("Buddy_App_QQ"))
    ) and not done.get("Buddy_App"):
        fire("Buddy_App", ba.desktop_buddy_app_sequence(*_BUDDY_APP))

    if _needs_action(tasks.get("automation_1")) and not done.get("automation_1"):
        fire("automation_1", [ba.desktop_automation_event("drifox 每日维护")])

    if _needs_action(tasks.get("Library_read")) and not done.get("Library_read"):
        try:
            resp = ba.report_web_event(
                at, uid, "web_element_click", _LIBRARY_URL, "library_doc_intro_click", "WorkBuddy资料库介绍"
            )
            if int(resp.get("code", -1)) == 0:
                done["Library_read"] = True
                acted.append("Library_read")
        except Exception as e:
            logger.warning(f"[CodeBuddy] Library_read 上报异常: {e}")

    if _needs_action(tasks.get("template_5")) and not done.get("template_5"):
        fired = 0
        for i, (tid, tname) in enumerate(_TEMPLATES):
            conv = f"drifox-tpl-{ms}-{i}"
            req = f"drifox-tpl-req-{ms}-{i}"
            if fire(f"template_5_{i}", ba.desktop_template_sequence(conv, req, tid, tname)):
                fired += 1
            time.sleep(0.3)
        if fired >= len(_TEMPLATES):
            done["template_5"] = True

    if _needs_action(tasks.get("playbook_prompt")) and not done.get("playbook_prompt"):
        conv, req = f"drifox-pb-{ms}", f"drifox-pb-req-{ms}"
        fire("playbook_prompt", ba.desktop_playbook_sequence(conv, req, *_PLAYBOOK_CASE))

    if _needs_action(tasks.get("create_canvas")) and not done.get("create_canvas"):
        conv, req = f"drifox-canvas-{ms}", f"drifox-canvas-req-{ms}"
        fire("create_canvas", ba.desktop_canvas_sequence(conv, req))

    if _needs_action(tasks.get("Hp_Appearance")) and not done.get("Hp_Appearance"):
        try:
            ba.set_appearance_theme(at, uid, _THEME_KEY)
            time.sleep(2)
            fire(
                "Hp_Appearance",
                [
                    {
                        "eventCode": "appearance_skin_apply",
                        "action": "apply",
                        "source": "settings_close",
                        "id": _THEME_KEY,
                        "vipLevel": 0,
                        "series": "",
                        "type": "unknown",
                    }
                ],
            )
        except Exception as e:
            logger.warning(f"[CodeBuddy] Hp_Appearance 异常: {e}")

    return acted


# 已在专属分支里实现、不能被通用专家形态识别重复接管的任务 code。
# _EXCLUDED_* 是「长得像专家任务但判据不同」的两类：轻量云要 mode=LOCAL 变体，
# 专家团按 expert_type=team 批量走另一条路。
_EXPERT_HANDLED_ELSEWHERE = frozenset({"Expert_lighthouse", "Expert_team_use_3"})


def _is_expert_task(task: dict) -> bool:
    """按任务**形态**判定是否为「召唤某个指定专家并完成一次对话」类任务。

    不依赖 task_code 命名：上游的活动任务 code 是随活动起的（今天叫
    expert_use_maidehao，明天可能叫别的），认命名必然漏。判据是三件套齐全：
    ① jump_url 里有 expertId；② 文案里点了专家名（「」）；③ 文案含「召唤/对话」。

    这样新上线的同类活动任务无需改代码即可自动接管；真出意外时
    _resolve_expert 定位不到真实条目会跳过并告警，不会误做其它任务。
    """
    code = str(task.get("task_code") or "")
    if code in _EXPERT_HANDLED_ELSEWHERE:
        return False
    eid, name = _expert_hint(task)
    if not eid or not name:
        return False
    text = str(task.get("task_desc") or "") + str(task.get("title") or "")
    return "召唤" in text or "对话" in text


def _expert_hint(task: dict) -> tuple:
    """从任务里取 (expert_id, task_name)：jump_url 的 expertId + 「」里的任务用名。

    注意 task_name 是**任务标题里的叫法**，与市场里的真实专家名不一定相同
    （实测：「直播电商教练」的真实名是「播旺旺」、「抖音策略师」是「斗音音」、
    「中国电商运营专家」是「卖得好」）。所以它只能当搜索词，绝不能当匹配键。
    """
    import re

    url = str(task.get("jump_url") or "")
    match = re.search(r"expertId(?:%3D|=)(ex_[A-Za-z0-9]+)", url)
    eid = match.group(1) if match else ""
    name = ""
    for text in (task.get("task_desc"), task.get("title")):
        match = re.search(r"「([^」]+)」", str(text or ""))
        if match:
            name = match.group(1)
            break
    return eid, name


def _resolve_expert(ba, at: str, uid: str, task: dict, pool: list) -> dict:
    """定位任务对应的真实专家条目：id 命中优先，其次用任务名当关键词搜再按 id 取。

    返回市场原始条目（含真实 name/version/categories，服务端计分依赖这些字段）；
    定位不到返回空表 —— 绝不本地伪造条目，伪造的上报会 200 但不计分。
    """
    eid, task_name = _expert_hint(task)
    if eid:
        for item in pool:
            if str(item.get("expert_id") or "") == eid:
                return item
    # 任务名只是搜索词：搜到结果后仍按 expert_id 认人，不按名字字符串比
    if eid and task_name:
        try:
            hits = ba.market_expert_list(at, uid, "agent", keyword=task_name)
        except Exception:
            hits = []
        for item in hits:
            if str(item.get("expert_id") or "") == eid:
                return item
    return {}


def _task_confirmed(ba, at: str, uid: str, code: str) -> bool:
    """回读服务端进度确认任务真的推进了（上报 200 不等于计分）。"""
    task = _task_map(ba, at, uid).get(code) or {}
    if not task:
        return False
    return str(task.get("status") or "") == "completed" or not _needs_action(task)


def _run_chat_tasks(ba, at: str, uid: str, nickname: str, tasks: dict, done: dict) -> list:
    """真实对话类任务（Model_chat_GLM5.2 / skill_1 / expert_5 / 专家团 / 轻量云 / 夜猫子）。

    判据要点：专家类必须用市场拉到的真实专家 id，且 requestId 取服务端返回的真值；
    自造 id/UUID 一律不计数（上游踩坑结论）。
    """
    from loguru import logger

    acted: list = []
    ms = int(time.time() * 1000)

    def chat_and_report(expert_id: str = "", model_id: str = _CHAT_MODEL, mode: str = "craft",
                        expert: dict | None = None, lighthouse: bool = False) -> bool:
        """真实对话 → 事件链 → 使用事件；返回是否全部上报成功。"""
        res = ba.chat_once(at, uid, _CHAT_PROMPT, model_id=model_id, expert_id=expert_id)
        if not res.get("ok"):
            logger.warning(f"[CodeBuddy] 真实对话失败（{expert_id or model_id}）: {res.get('error')}")
            return False
        conv, req = res["conversation_id"], res["request_id"]
        events = ba.desktop_chat_sequence(conv, req, "msg-" + req[-8:])
        if lighthouse:
            for ev in events:
                if ev.get("eventCode") == "agent_task_created":
                    ev["has_expert"] = True
                    ev["expert_id"] = expert_id
                    ev["expert_name"] = str((expert or {}).get("display_name_zh") or "")
                    ev["expert_industry_id"] = ""
        if expert:
            use_ev = ba.desktop_expert_actual_use(expert, conv, req, mode=mode)
            if lighthouse:
                use_ev["type"] = ""
                use_ev["cost"] = 0
            events.append(use_ev)
        resp = ba.report_desktop_events(at, uid, nickname, events)
        return int(resp.get("code", -1)) == 0

    # skill_1：真实对话 + skill_info（对话 finishReason 改 tool_calls 语义）
    if _needs_action(tasks.get("skill_1")) and not done.get("skill_1"):
        res = ba.chat_once(at, uid, _CHAT_PROMPT, model_id=_CHAT_MODEL)
        if res.get("ok"):
            conv, req = res["conversation_id"], res["request_id"]
            msg = "msg-" + req[-8:]
            events = ba.desktop_chat_sequence(conv, req, msg)
            for ev in events:
                if ev.get("eventCode") == "chat_message_response":
                    ev["finishReason"] = "tool_calls"
            events.append(
                {
                    "eventCode": "skill_info",
                    "id": "润泽小馆·日报撰写",
                    "skillId": "skill_2097350077599879168",
                    "skillVersion": "1.0.0",
                    "toolStatus": "success",
                    "fileCount": 56,
                    "source": "workbuddy-desktop",
                    "conversationId": conv,
                    "requestId": req,
                    "messageId": msg,
                    "requestModelId": "fast-model",
                }
            )
            try:
                if int(ba.report_desktop_events(at, uid, nickname, events).get("code", -1)) == 0:
                    time.sleep(_CLAIM_SETTLE_SEC)
                    if _task_confirmed(ba, at, uid, "skill_1"):
                        done["skill_1"] = True
                        acted.append("skill_1")
                    else:
                        logger.warning("[CodeBuddy] skill_1 上报成功但进度未推进，稍后重试")
            except Exception as e:
                logger.warning(f"[CodeBuddy] skill_1 上报异常: {e}")

    # Model_chat_GLM5.2：真实 glm-5.2 对话 + 对齐模型的上报
    if _needs_action(tasks.get("Model_chat_GLM5.2")) and not done.get("Model_chat_GLM5.2"):
        res = ba.chat_once(at, uid, _CHAT_PROMPT, model_id="glm-5.2")
        if res.get("ok"):
            time.sleep(_REPORT_GAP)
            r = ba.report_activity(at, uid, 1, model_id="glm-5.2", model_name="GLM-5.2")
            if r.get("ok"):
                time.sleep(_CLAIM_SETTLE_SEC)
                if _task_confirmed(ba, at, uid, "Model_chat_GLM5.2"):
                    done["Model_chat_GLM5.2"] = True
                    acted.append("Model_chat_GLM5.2")
                else:
                    logger.warning("[CodeBuddy] Model_chat_GLM5.2 上报成功但进度未推进")

    # expert_5 / Expert_team_use_3：逐个真实专家（召唤链 + 真实对话 + 使用事件）
    expert_jobs = (
        ("expert_5", "agent", 5),
        ("Expert_team_use_3", "team", 3),
    )
    for code, etype, count in expert_jobs:
        task = tasks.get(code)
        if not _needs_action(task) or done.get(code):
            continue
        cur, tgt = _task_progress(task)
        need = max(0, min(count, tgt - cur))
        if need <= 0:
            continue
        try:
            experts = ba.market_expert_list(at, uid, etype)
        except Exception as e:
            logger.warning(f"[CodeBuddy] {code} 专家列表拉取失败: {e}")
            continue
        ok = 0
        for expert in experts:
            if ok >= need:
                break
            try:
                ba.report_desktop_events(at, uid, nickname, ba.desktop_expert_summon_sequence(expert))
                time.sleep(1.0)
                if chat_and_report(expert_id=str(expert.get("expert_id") or ""), expert=expert):
                    ok += 1
            except Exception as e:
                logger.warning(f"[CodeBuddy] {code} 专家链异常: {e}")
                continue
            if ok < need:
                time.sleep(_EXPERT_GAP)
        if ok >= need:
            time.sleep(_CLAIM_SETTLE_SEC)
            if _task_confirmed(ba, at, uid, code):
                done[code] = True
                acted.append(code)
            else:
                logger.warning(f"[CodeBuddy] {code} 上报成功但进度未推进（{ok}/{need}）")

    # 指定专家任务（expert_use_* / WorkBuddy_Valorant）：jump_url 带 expertId，
    # 或缺 id 时按标题专家名走服务端 keyword 搜索（实测「无畏契约拼豆专家」可搜到）。
    named_jobs = [
        (code, task)
        for code, task in tasks.items()
        if _needs_action(task) and not done.get(code) and _is_expert_task(task)
    ]
    if named_jobs:
        pool: list = []
        pool_loaded = False
        for code, task in named_jobs:
            eid, name = _expert_hint(task)
            if not pool_loaded:
                try:
                    pool = ba.market_expert_list(at, uid, "agent")
                except Exception as e:
                    logger.warning(f"[CodeBuddy] 专家市场列表失败: {e}")
                pool_loaded = True
            expert = _resolve_expert(ba, at, uid, task, pool)
            if not expert:
                logger.warning(
                    f"[CodeBuddy] {code} 未定位到真实专家条目（{name or '无 hint'}），"
                    "本轮跳过（不伪造条目，否则上报 200 也不计分）"
                )
                continue
            try:
                ba.report_desktop_events(at, uid, nickname, ba.desktop_expert_summon_sequence(expert))
                time.sleep(1.0)
                if chat_and_report(expert_id=str(expert.get("expert_id") or ""), expert=expert):
                    # 上报 200 只是事件收下了：回读进度才算数，否则当天会被 done 挡住不重试
                    time.sleep(_CLAIM_SETTLE_SEC)
                    if _task_confirmed(ba, at, uid, code):
                        done[code] = True
                        acted.append(code)
                    else:
                        logger.warning(f"[CodeBuddy] {code} 上报成功但进度未推进，稍后重试")
            except Exception as e:
                logger.warning(f"[CodeBuddy] {code} 专家链异常: {e}")
            time.sleep(_EXPERT_GAP)

    # Expert_lighthouse：轻量云专家（mode=LOCAL + agent_task_created 带 has_expert）
    if _needs_action(tasks.get("Expert_lighthouse")) and not done.get("Expert_lighthouse"):
        lh = {
            "expert_id": _LIGHTHOUSE_EXPERT_ID,
            "expert_type": "agent",
            "display_name_zh": "腾讯轻量云专家",
            "profession_zh": "腾讯轻量云专家",
            "version": "1.0.2",
        }
        try:
            for item in ba.market_expert_list(at, uid, "agent"):
                if str(item.get("expert_id") or "") == _LIGHTHOUSE_EXPERT_ID:
                    lh = item
                    break
            ba.report_desktop_events(at, uid, nickname, ba.desktop_expert_summon_sequence(lh))
            time.sleep(1.0)
            if chat_and_report(expert_id=_LIGHTHOUSE_EXPERT_ID, expert=lh, lighthouse=True):
                time.sleep(_CLAIM_SETTLE_SEC)
                if _task_confirmed(ba, at, uid, "Expert_lighthouse"):
                    done["Expert_lighthouse"] = True
                    acted.append("Expert_lighthouse")
                else:
                    logger.warning("[CodeBuddy] Expert_lighthouse 上报成功但进度未推进")
        except Exception as e:
            logger.warning(f"[CodeBuddy] Expert_lighthouse 异常: {e}")

    # black_cat：23:00-08:00 窗口内补足夜间对话次数
    task = tasks.get("black_cat")
    if _needs_action(task) and not done.get("black_cat"):
        hour = time.localtime().tm_hour
        if hour >= 23 or hour < 8:
            cur, tgt = _task_progress(task)
            need = max(0, min(tgt - cur, 10))
            ok = 0
            for _ in range(need):
                res = ba.chat_once(at, uid, _CHAT_PROMPT, model_id="glm-5.2")
                if res.get("ok"):
                    ba.report_activity(at, uid, 1, model_id="glm-5.2", model_name="GLM-5.2")
                    ok += 1
                time.sleep(4)
            if ok >= need:
                time.sleep(_CLAIM_SETTLE_SEC)
                if _task_confirmed(ba, at, uid, "black_cat"):
                    done["black_cat"] = True
                    acted.append("black_cat")
                else:
                    logger.warning("[CodeBuddy] black_cat 上报成功但进度未推进")

    return acted


def _run_growth_side(ba, at: str, uid: str, done: dict) -> dict:
    """成长中心侧的非任务收益：连登兑换、抽奖、猫旅行、新手礼包。"""
    from loguru import logger

    result: dict = {"credit": 0, "energy": 0, "notes": []}

    # 连登档位兑换（7d/14d/28d）：档位未解锁时上游 403，按状态预判跳过
    try:
        streak = ba.growth_streak(at, uid)
        days = int((streak.get("streak") or {}).get("days") or 0)
        status = streak.get("redemption_status") or {}
        for tier, need_days in (("7d", 7), ("14d", 14), ("28d", 28)):
            if days < need_days:
                continue
            state = str(status.get(f"tier_{need_days}d_status") or "")
            if state in ("locked", "redeemed", "claimed"):
                continue
            resp = ba.redeem_tier(at, uid, tier)
            if int(resp.get("code", -1)) == 0:
                result["notes"].append(f"兑换 {tier} 档位")
    except Exception as e:
        logger.warning(f"[CodeBuddy] 连登兑换异常: {e}")

    # 抽奖（次数来自连登兑换）
    try:
        summary = ba.lottery_summary(at, uid)
        chances = int(summary.get("chances") or 0)
        for _ in range(min(chances, _LOTTERY_MAX_DRAW)):
            resp = ba.lottery_draw(at, uid)
            if int(resp.get("code", -1)) != 0:
                break
            data = (resp.get("data") if isinstance(resp.get("data"), dict) else {}) or {}
            result["credit"] += int(data.get("credit") or 0)
            result["notes"].append("抽奖一次")
    except Exception as e:
        logger.warning(f"[CodeBuddy] 抽奖异常: {e}")

    # 猫猫旅行：到站领奖 / 空闲派出（每日一趟闭环）
    try:
        state = ba.travel_status(at, uid)
        st = str(state.get("state") or "")
        if st == "arrived" and state.get("record_id"):
            claim = ba.travel_claim(at, uid, int(state["record_id"]))
            if claim.get("ok"):
                result["credit"] += int(claim.get("credit") or 0)
                result["notes"].append(f"旅行领奖 +{claim.get('credit')}")
        elif st == "idle" and not state.get("daily_limit_reached"):
            if int(ba.travel_depart(at, uid, 1).get("code", -1)) == 0:
                result["notes"].append("旅行派出")
                time.sleep(1.0)
                again = ba.travel_status(at, uid)
                if str(again.get("state") or "") == "arrived" and again.get("record_id"):
                    claim = ba.travel_claim(at, uid, int(again["record_id"]))
                    if claim.get("ok"):
                        result["credit"] += int(claim.get("credit") or 0)
    except Exception as e:
        logger.warning(f"[CodeBuddy] 旅行异常: {e}")

    # 新手礼包（一次性，已领返回业务错误按幂等忽略）
    if not done.get("gift"):
        try:
            gift = ba.claim_gift(at, uid)
            if gift.get("ok"):
                result["credit"] += int(gift.get("credit") or 0)
                done["gift"] = True
                result["notes"].append(f"新手礼包 +{gift.get('credit')}")
        except Exception as e:
            logger.warning(f"[CodeBuddy] 礼包异常: {e}")

    return result


def _run_daily_tasks(ba, at: str, uid: str, nickname: str, entry: dict) -> None:
    """单号每日维护：活跃 → 领养 → accept → 行为链 → 领奖 → 成长侧收益。

    全程分段 try：任何一段失败都不影响后续（守护线程不允许退出）。
    """
    from loguru import logger

    today = time.strftime("%Y-%m-%d")
    daily = dict(entry.get("daily") or {})
    if daily.get("date") != today:
        daily = {"date": today, "done": {}}
    done: dict = daily.setdefault("done", {})
    entry["daily"] = daily

    gained_credit = 0
    gained_energy = 0

    # 1) 每日活跃上报（连登必需；顺带点亮 first_buddy 前置）
    try:
        chat5 = _task_map(ba, at, uid).get("chat_5") or {}
        cur, tgt = _task_progress(chat5) if chat5 else (0, 5)
        need = 1 if not done.get("report_daily") else 0
        if _needs_action(chat5):
            need = max(1, min(tgt - cur, 5))
        if need:
            r = ba.report_activity(at, uid, need)
            if r.get("ok"):
                done["report_daily"] = True
    except Exception as e:
        logger.warning(f"[CodeBuddy] 活跃上报异常: {e}")

    # 2) 首次领养（+300 积分 +8 能量，且是其余任务 accept 的前置）
    try:
        if not done.get("adopt"):
            info = ba.buddy_info(at, uid)
            if info.get("buddy"):
                done["adopt"] = True
            else:
                ba.buddy_agreement(at, uid)
                time.sleep(_REPORT_GAP)
                adopt = ba.buddy_adopt(at, uid)
                if adopt.get("ok"):
                    gained_credit += int(adopt.get("credit") or 0)
                    gained_energy += int(adopt.get("energy") or 0)
                    done["adopt"] = True
                elif adopt.get("gated"):
                    logger.info("[CodeBuddy] 领养前置未落定（当日活跃处理中），下轮重试")
    except Exception as e:
        logger.warning(f"[CodeBuddy] 领养异常: {e}")

    # 3) 任务清单 + accept（领养后前置才满足）
    tasks = _task_map(ba, at, uid)
    try:
        pending = [c for c, t in tasks.items() if str(t.get("accept_status") or "") == "not_accepted"]
        if pending:
            ba.accept_tasks(at, uid, pending)
            time.sleep(1.5)
            tasks = _task_map(ba, at, uid)
    except Exception as e:
        logger.warning(f"[CodeBuddy] accept 异常: {e}")

    # 4) 行为链（桌面指纹 + 真实对话）
    try:
        acted = _run_desktop_chain(ba, at, uid, nickname, tasks, done)
        if acted:
            logger.info(f"[CodeBuddy] 桌面事件链完成: {', '.join(acted)}")
    except Exception as e:
        logger.warning(f"[CodeBuddy] 桌面事件链异常: {e}")
    try:
        time.sleep(_CLAIM_SETTLE_SEC)
        acted = _run_chat_tasks(ba, at, uid, nickname, tasks, done)
        if acted:
            logger.info(f"[CodeBuddy] 对话类任务完成: {', '.join(acted)}")
    except Exception as e:
        logger.warning(f"[CodeBuddy] 对话类任务异常: {e}")

    # 5) 领奖（Web 域；先给上游一点异步计分落定时间）
    try:
        time.sleep(_CLAIM_SETTLE_SEC)
        for code, task in _task_map(ba, at, uid).items():
            if not _claimable(task) or done.get(f"claim_{code}"):
                continue
            claim = ba.claim_reward(at, uid, code)
            if claim.get("ok"):
                gained_credit += int(claim.get("credit") or 0)
                gained_energy += int(claim.get("energy") or 0)
                done[f"claim_{code}"] = True
                if claim.get("credit"):
                    logger.info(f"[CodeBuddy] 领奖 {code} +{claim['credit']} 积分")
    except Exception as e:
        logger.warning(f"[CodeBuddy] 领奖异常: {e}")

    # 6) 成长侧收益（连登兑换 / 抽奖 / 旅行 / 礼包）
    try:
        side = _run_growth_side(ba, at, uid, done)
        gained_credit += int(side.get("credit") or 0)
        gained_energy += int(side.get("energy") or 0)
        if side.get("notes"):
            logger.info(f"[CodeBuddy] 成长侧: {', '.join(side['notes'])}")
    except Exception as e:
        logger.warning(f"[CodeBuddy] 成长侧异常: {e}")

    _save_entry(entry)
    if gained_credit or gained_energy:
        logger.info(f"[CodeBuddy] 本轮累计 +{gained_credit} 积分 / +{gained_energy} 能量")


def _refresh_loop():
    """守护循环：遍历缓存内全部号 —— 临期刷新 access_token + 每日签到/成长任务（幂等）。

    每日任务分两类：必修（活跃上报 / 旅行 / 领奖 / 连登兑换）每轮补，
    重活（桌面事件链 / 真实对话类）按任务进度判断，已达标即跳过。
    """
    try:
        _load_auth_module()  # 预热：退出期不再首次加载 auth 模块
    except Exception:
        pass
    while not _STOP_EVENT.is_set():
        try:
            from loguru import logger

            ba = _load_auth_module()
            paths = sorted(_CACHE_DIR.glob("*.json"))
            active = set(_active_cache_files())
            # 在用号排最前：历史缓存号上百个，配置号不能排在它们后面干等
            ordered = [p for p in paths if p in active] + [p for p in paths if p not in active]
            if paths:
                logger.info(
                    f"[CodeBuddy] 开始维护 {len(paths)} 个号（签到全量，"
                    f"每日任务 {len(active)} 个在用号优先）"
                )
            for path in ordered:
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
                        if path in active:
                            _run_daily_for_entry(ba, at, entry)
                except Exception:
                    continue  # 单号失败不影响其余号
        except Exception:
            pass  # 守护线程不允许退出
        # 分段睡眠：退出信标置位后最快 1s 内退出（整段 6h 会挡住退出）
        for _ in range(_REFRESH_INTERVAL_SEC):
            if _STOP_EVENT.is_set():
                return
            time.sleep(1)


def _kickoff_daily(ba, refresh_token: str) -> None:
    """登录成功后异步补跑该号每日任务（不必等 6h 轮次）。"""
    if _AUTO_TASK_DISABLED or not refresh_token:
        return

    def _run() -> None:
        try:
            entry = _cache_get(refresh_token)
            at = str(entry.get("access_token") or "")
            if at:
                _run_daily_for_entry(ba, at, entry)
        except Exception:
            pass  # 后台线程不允许把异常抛给解释器

    threading.Thread(target=_run, daemon=True).start()


def _run_daily_for_entry(ba, at: str, entry: dict) -> None:
    """单号入口：解析 uid 后跑每日任务；任何失败都不能影响签到主循环。"""
    if _AUTO_TASK_DISABLED:
        return
    from loguru import logger

    try:
        uid = str(entry.get("uid") or "") or ba.uid_from_access_token(at)
        if not uid:
            logger.info("[CodeBuddy] 每日任务跳过：未取到账号 uid（重新自动登录一次即可补齐）")
            return
        if not entry.get("uid"):
            entry["uid"] = uid
            _save_entry(entry)
        label = str(entry.get("nickname") or uid)[:12]
        logger.info(f"[CodeBuddy] 号 {label} 开始每日任务")
        _run_daily_tasks(ba, at, uid, str(entry.get("nickname") or ""), entry)
    except Exception as e:
        logger.warning(f"[CodeBuddy] 每日任务异常: {e}")


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
    # 主线程预热配置号（守护线程不能碰 QConfig）；配置变化时在主线程刷新缓存
    _warm_config_tokens()
    try:
        from app.utils.config import Settings

        settings = Settings.get_instance()
        settings.llm_saved_providers.valueChanged.connect(lambda *_: _warm_config_tokens())
    except Exception:
        pass
    threading.Thread(target=_refresh_loop, daemon=True).start()
    try:
        from PySide6.QtCore import QCoreApplication

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
