# -*- coding: utf-8 -*-
"""OpenCode Zen/Go console device-flow OAuth 辅助模块（纯标准库）。

文件名以下划线开头：provider loader 会跳过（不当作服务商定义加载），由
``opencode.py`` 按路径显式 ``compile`` 加载，规避 SourceFileLoader 的 .pyc
秒级 mtime 缓存坑。

协议参考 ``opencode-zen-oauth``（MIT, Naab2k3/pi-opencode-zen），并经
2026-11-07 端点探活核对：

- POST /console/auth/device/code  → 200 {device_code,user_code,verification_uri_complete,expires_in,interval}
- POST /console/auth/device/token → 200 凭证 / 400 authorization_pending、slow_down
- GET  /console/api/orgs          → [{id,name}] 或 {orgs:[...]}
- GET  /console/api/config        → workspace 模型池（鉴权 Broker + x-org-id）
- 推理 https://opencode.ai/inference/openai/v1（OpenAI 兼容 chat/completions）

凭证分层：服务商配置的 API_KEY 存长效 refresh_token（config_id 稳定、走原生
加密链），短效 access_token / org_id / 模型池存本地缓存，请求时按 refresh
自动续期。与 CodeBuddy / GitHub Copilot 的 login_hook 三件套范式一致。

范围：只走 workspace 付费模型（inference 端点，参考实现 toAuth 付费路径）。
免费层零价模型另有客户端身份闸门，需 x-opencode-session 与
-session-id 同值且为 ses_ 格式，而宿主 extra_headers 的 callable 拿不到
session_id，无法稳定对齐；且 opencode.py 2026-09-24 实测该闸门按服务端
账号维度判定、伪装头无效。故免费层模型保留在列表但不特殊补头，选中 403
属预期（与现有 Zen/Go -free 模型同口径）。

风险：非官方客户端复用 console device flow，官方可能收紧；本模块经端点
探活与参考实现逐行核对，但未经真实账号端到端聊天验证。
"""

import hashlib
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

CONSOLE = "https://opencode.ai/console"
CLIENT_ID = "opencode-cli"
UA = "opencode/cli"  # versionless，console 接受，无版本漂移
SLOW_DOWN_PENALTY_SEC = 5
REFRESH_SKEW_SEC = 5 * 60
# 本地 access_token 寿命上限。服务端 device/token 返回 expires_in=2592000（30 天，
# 2026-11-08 实测），但整条会话链可能被服务端提前吊销（access 401 且 refresh
# invalid_grant，同批实测）。只信 expires_in 会让刷新分支永不触发，吊销后就是
# 永久 401，只能手动重新登录。故本地强制每小时换一次，把自愈窗口压到 1 小时。
ACCESS_MAX_TTL_SEC = 60 * 60
DEFAULT_TIMEOUT = 30.0

# gpt/grok/muse 走 Responses API，DriFox 单一 chat/completions 通道不支持
_RESPONSES_MODEL = re.compile(r"^(gpt|grok|muse)-", re.I)
_TEST_MODEL = re.compile(r"^(test|jev)([-_.]|$)", re.I)


class LoginCancelled(Exception):
    """用户在界面主动中止登录（区别于超时与服务端报错）。"""


# ── 进程级缓存（磁盘为准，内存仅加速）─────────────────────────────
_CACHE_DIR = None
_MEM: Dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_SSL_CONTEXT = None


def _cache_dir():
    global _CACHE_DIR
    if _CACHE_DIR is None:
        from app.plugins.sdk import get_app_data_dir

        _CACHE_DIR = get_app_data_dir() / "opencode_oauth" / "cache"
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def _cache_path(api_key: str):
    digest = hashlib.sha1(api_key.encode("utf-8")).hexdigest()[:12]
    return _cache_dir() / f"{digest}.json"


def cache_get(api_key: str) -> dict:
    with _CACHE_LOCK:
        if api_key in _MEM:
            return dict(_MEM[api_key])
    try:
        path = _cache_path(api_key)
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("access_token") and not entry.get("issued_at"):
            # 旧缓存无签发时刻：用文件 mtime 近似（写盘即签发），否则升级后会被
            # 误判成「刚签发」而一直不刷新
            entry["issued_at"] = path.stat().st_mtime
    except (OSError, json.JSONDecodeError, ValueError):
        entry = {}
    with _CACHE_LOCK:
        _MEM[api_key] = entry
    return dict(entry)


def cache_put(api_key: str, **fields) -> dict:
    """merge 写入（保留已有的 org_id / allowed_models 等字段）。"""
    if "access_token" in fields:
        # 每次换到新 access 都刷新签发时刻：_fresh 的本地 TTL 上限依赖它
        fields["issued_at"] = time.time()
    with _CACHE_LOCK:
        entry = dict(_MEM.get(api_key) or {})
        entry.update(fields)
        _MEM[api_key] = entry
    _cache_path(api_key).write_text(json.dumps(entry), encoding="utf-8")
    return dict(entry)


def _ssl_context():
    """系统证书链 SSLContext（信任 Windows/macOS 证书存储）；不可用回退 None。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        try:
            import ssl

            import truststore

            _SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            _SSL_CONTEXT = False
    return _SSL_CONTEXT or None


def _request(
    url: str,
    *,
    method: str = "GET",
    body: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    allow_400: bool = False,
) -> Any:
    """JSON 请求。device/token 的 pending 是 HTTP 400 + JSON body，需按 allow_400 解析。"""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        if not (allow_400 and e.code == 400):
            raise RuntimeError(f"{url} HTTP {e.code}: {raw[:200]}") from e
    except OSError as e:
        raise RuntimeError(f"{url} 请求异常：{e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{url} 响应非 JSON：{raw[:200]}") from e


# ── device flow ─────────────────────────────────────────────────

def start_device_flow(timeout: float = DEFAULT_TIMEOUT) -> dict:
    # supports_org_scope：让 Console 授权页在浏览器里绑定 workspace，token
    # 交换时回带 org_id；不支持该 flag 的服务端会忽略（退回 fetchOrgs）。
    # 参考 pi-provider-opencode-console 2.x。
    return _request(
        f"{CONSOLE}/auth/device/code",
        method="POST",
        body={"client_id": CLIENT_ID, "supports_org_scope": True},
        timeout=timeout,
    )


def device_token(body: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    return _request(
        f"{CONSOLE}/auth/device/token",
        method="POST",
        body={"client_id": CLIENT_ID, **body},
        timeout=timeout,
        allow_400=True,
    )


def verification_url(start: dict) -> str:
    import urllib.parse

    return urllib.parse.urljoin(
        CONSOLE + "/",
        start.get("verification_uri_complete") or start.get("verification_uri") or "",
    )


def _copy_to_clipboard(text: str) -> bool:
    """跨平台剪贴板（windows clip.exe 需 UTF-16 带 BOM，否则静默失败）。"""
    import subprocess

    for cmd, data in (
        (["clip"], text.encode("utf-16")),
        (["pbcopy"], text.encode("utf-8")),
        (["xclip", "-selection", "clipboard"], text.encode("utf-8")),
        (["wl-copy"], text.encode("utf-8")),
    ):
        try:
            subprocess.run(cmd, input=data, check=True, timeout=5)
            return True
        except Exception:
            continue
    return False


def fetch_orgs(access: str, timeout: float = DEFAULT_TIMEOUT) -> List[dict]:
    data = _request(
        f"{CONSOLE}/api/orgs",
        headers={"Authorization": f"Bearer {access}"},
        timeout=timeout,
    )
    items = data if isinstance(data, list) else (data.get("orgs") or [])
    return [
        {"id": str(o["id"]), "name": str(o.get("name") or o["id"])}
        for o in items
        if isinstance(o, dict) and o.get("id")
    ]


def _is_zen_gateway(url: Any) -> bool:
    return isinstance(url, str) and "opencode.ai/inference" in url


def _routable_chat_completions(model_id: str, model: dict, block: dict) -> bool:
    """该模型能否走 DriFox 单一 OpenAI chat/completions 端点。

    Zen 网关按模型分四种 wire protocol（openai-completions / openai-responses /
    anthropic-messages / google-generative-ai），DriFox 请求层只有一条 OpenAI
    兼容通道，故只收 openai-completions 可路由的模型，其余过滤掉（选了会 4xx）。
    """
    override = model.get("provider") or {}
    if not override:
        override = {"npm": block.get("npm"), "api": block.get("api")}
    npm = override.get("npm")
    api = override.get("api")
    if npm:
        if npm in ("@ai-sdk/anthropic", "@ai-sdk/google"):
            return False
        if npm == "@ai-sdk/openai":
            # gpt/grok/muse 走 Responses API，chat/completions 不支持
            return not _RESPONSES_MODEL.match(model_id)
        if npm == "@ai-sdk/openai-compatible":
            return True
        return False  # vertex/bedrock/azure 等需自带鉴权，不可经 Zen 路由
    if api:
        if "anthropic" in api or "google" in api:
            return False
        return _is_zen_gateway(api) or "openai" in api
    return True  # 无 override：Zen 默认 openai-completions


def fetch_workspace_models(access: str, org_id: str, timeout: float = DEFAULT_TIMEOUT) -> List[str]:
    """按 workspace 拉模型池，过滤出 chat/completions 可路由且未被禁用的模型。"""
    data = _request(
        f"{CONSOLE}/api/config",
        headers={
            "Authorization": f"Bearer {access}",
            "x-org-id": org_id,
            "User-Agent": UA,
        },
        timeout=timeout,
    )
    providers = ((data.get("config") or {}).get("provider") or {}) if isinstance(data, dict) else {}
    out: List[str] = []
    for _pid, block in providers.items():
        if not isinstance(block, dict):
            continue
        opts = block.get("options") or {}
        base = opts.get("baseURL")
        # BYOK：自带非 inference 端点或自带 key 的条目，OAuth token 无法鉴权
        if (base or opts.get("apiKey")) and not _is_zen_gateway(base) and not _is_zen_gateway(block.get("api")):
            continue
        whitelist = block.get("whitelist")
        blacklist = block.get("blacklist")
        for mid, model in (block.get("models") or {}).items():
            if not isinstance(model, dict) or _TEST_MODEL.match(mid):
                continue
            if model.get("disabled") is True or model.get("status") == "deprecated":
                continue
            if isinstance(whitelist, list) and mid not in whitelist:
                continue
            if isinstance(blacklist, list) and mid in blacklist:
                continue
            if _routable_chat_completions(mid, model, block):
                out.append(mid)
    return list(dict.fromkeys(out))


def fetch_go_models(timeout: float = DEFAULT_TIMEOUT) -> List[str]:
    """Go 公开模型目录（无需鉴权）：GET /inference/go/v1/models。

    Go 的模型池不是 org-scoped /api/config，而是公开的 lite 目录
    （pi-provider-opencode-console 实测口径）。每项带 api 字段标注 wire
    protocol，DriFox 单一 chat/completions 通道只收 chat-completions 项。
    """
    data = _request(
        "https://opencode.ai/inference/go/v1/models",
        headers={"User-Agent": UA},
        timeout=timeout,
    )
    items = (data.get("data") or []) if isinstance(data, dict) else []
    out: List[str] = []
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "")
        api = m.get("api") or {}
        if mid and isinstance(api, dict) and "chat-completions" in api:
            out.append(mid)
    return list(dict.fromkeys(out))


def login(cancel_event=None, timeout_sec: float = 280.0) -> dict:
    """device flow 全流程：开浏览器 → 可中断轮询 → 取 workspace → 拉模型池。

    login_hook 在编辑卡后台线程调用，返回 dict 供 opencode.py 回填 api_key。
    cancel_event 置位时立即抛 LoginCancelled（用户偏好：卡住能退出）。
    """
    import webbrowser

    start = start_device_flow()
    url = verification_url(start)
    user_code = str(start.get("user_code") or "")
    webbrowser.open(url)
    _copy_to_clipboard(user_code)

    interval = float(start.get("interval") or 5)
    deadline = time.time() + min(float(start.get("expires_in") or 600), timeout_sec)
    while time.time() < deadline:
        # 可中断睡眠：置位即退出，不干等到设备码过期
        if cancel_event is not None and cancel_event.wait(interval):
            raise LoginCancelled("登录已中止")
        if cancel_event is None:
            time.sleep(interval)
        poll = device_token(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": start["device_code"],
            }
        )
        if poll.get("access_token"):
            access = str(poll["access_token"])
            refresh = str(poll.get("refresh_token") or "")
            orgs = fetch_orgs(access)
            # 服务端若在授权页绑定了 workspace（supports_org_scope），以 token
            # 交换回带的 org_id 为准；否则 login_hook 无交互通道，多 workspace
            # 取第一个并在 info 里说明
            granted_org = str(poll.get("org_id") or "")
            if granted_org and any(o["id"] == granted_org for o in orgs):
                org_id = granted_org
            else:
                org_id = orgs[0]["id"] if orgs else ""
            allowed: List[str] = []
            if org_id:
                try:
                    allowed = fetch_workspace_models(access, org_id)
                except Exception:
                    allowed = []
            return {
                "access_token": access,
                "refresh_token": refresh,
                "expires_in": float(poll.get("expires_in") or 1800),
                "org_id": org_id,
                "orgs": orgs,
                "allowed_models": allowed,
                "user_code": user_code,
                "verification_uri": url,
            }
        err = poll.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += SLOW_DOWN_PENALTY_SEC
            continue
        raise RuntimeError(f"登录失败：{err or 'unknown'} {poll.get('error_description') or ''}")
    raise RuntimeError("登录超时：设备码已过期，请重新登录")


# ── 请求时凭证 ───────────────────────────────────────────────────

def prime_cache(
    api_key: str,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: float,
    org_id: str,
    orgs: List[dict],
    allowed_models: List[str],
) -> None:
    """登录后立即落缓存：保存配置后立即可聊天，无需等首次请求刷新。"""
    cache_put(
        api_key,
        access_token=access_token,
        refresh_token=refresh_token or api_key,
        expires_at=time.time() + float(expires_in or 1800),
        org_id=org_id,
        orgs=orgs,
        allowed_models=allowed_models,
    )


def _fresh(llm_config: dict) -> Tuple[str, str]:
    """读缓存里的 (access_token, org_id)；临期或超本地 TTL 上限返回空串待刷新。"""
    api_key = str((llm_config or {}).get("API_KEY", "") or "").strip()
    if not api_key:
        return ("", "")
    entry = cache_get(api_key)
    access = str(entry.get("access_token") or "")
    expires_at = float(entry.get("expires_at") or 0)
    issued_at = float(entry.get("issued_at") or 0)
    org_id = str(entry.get("org_id") or "")
    now = time.time()
    if access and expires_at > now + REFRESH_SKEW_SEC and now - issued_at < ACCESS_MAX_TTL_SEC:
        return (access, org_id)
    return ("", org_id)


def _do_refresh(api_key: str) -> Tuple[str, str]:
    """换新 access_token 并落缓存。调用前必须持有 _REFRESH_LOCK。"""
    entry = cache_get(api_key)
    refresh = str(entry.get("refresh_token") or api_key)
    data = device_token({"grant_type": "refresh_token", "refresh_token": refresh})
    new_access = str(data.get("access_token") or "")
    if not new_access:
        raise RuntimeError(f"令牌刷新失败：{data.get('error') or '未返回 access_token'}，请重新登录")
    cache_put(
        api_key,
        access_token=new_access,
        refresh_token=str(data.get("refresh_token") or refresh),
        expires_at=time.time() + float(data.get("expires_in") or 1800),
    )
    return (new_access, str(entry.get("org_id") or ""))

def get_auth_context(llm_config: dict) -> Tuple[str, str]:
    """返回 (access_token, org_id)；access 临期则按 refresh_token 自动续期。

    API_KEY 恒为用户配置里的 refresh_token（缓存 key 稳定）；服务端若轮转
    refresh_token，只更新缓存字段，不动配置，config_id 保持稳定。

    一次请求里 Authorization / x-opencode-org-id 两个头各调一次本函数，
    故刷新段加锁 + double-check，避免并发重复刷新同一号。
    """
    access, org_id = _fresh(llm_config)
    if access:
        return (access, org_id)
    api_key = str((llm_config or {}).get("API_KEY", "") or "").strip()
    if not api_key:
        return ("", org_id)
    with _REFRESH_LOCK:
        access, org_id = _fresh(llm_config)
        if access:
            return (access, org_id)
        return _do_refresh(api_key)

def force_refresh(llm_config: dict) -> bool:
    """401 后强制换新 access_token（忽略本地 expires_at），返回是否可直接重试。

    与 get_auth_context 共用刷新锁：并发 401 只换一次；10 秒内刚换过说明另一
    线程已完成刷新，直接返回 True 让调用方重试。整链已废（refresh invalid_grant）
    时抛 RuntimeError，由调用方归因并提示重新登录。
    """
    api_key = str((llm_config or {}).get("API_KEY", "") or "").strip()
    if not api_key:
        return False
    with _REFRESH_LOCK:
        entry = cache_get(api_key)
        if time.time() - float(entry.get("issued_at") or 0) < 10:
            return bool(entry.get("access_token"))
        _do_refresh(api_key)
        return True
