# -*- coding: utf-8 -*-
"""
MCP 工具模块 - 管理 MCP Server 连接、工具发现与调用

核心设计：
- MCPClientManager 是全局单例，多窗口共享连接池
- 所有 MCP 异步操作在一个专用后台线程的事件循环中执行
- 每个连接由一个持久 Task 管理，__aenter__/__aexit__ 在同一 Task 中
- 断开连接通过 Event 信号通知 Task 自然退出 async with 块
"""

import asyncio
import copy
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from app.tools.result import ToolResult


class MCPState:
    """MCP 服务器连接状态（与 UI 指示灯颜色一一对应）

    DISABLED   → 黑色：用户关闭 / 未启动
    CONNECTING → 黄色：正在启动（进程拉起、握手、list_tools 全过程）
    CONNECTED  → 绿色：握手成功且工具已发现
    FAILED     → 红色：启动失败（超时 / 进程崩溃 / 协议错误）
    """

    DISABLED = "disabled"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


def _extract_real_error(exc: Exception) -> Exception:
    """
    从嵌套的 ExceptionGroup/BaseExceptionGroup 中提取最底层的真实错误。
    如果无法提取，返回原异常。
    """
    # Python 3.11+ ExceptionGroup
    if hasattr(exc, "exceptions"):
        group = exc
        # 遍历所有层级，找到第一个非 ExceptionGroup 的异常
        while hasattr(group, "exceptions") and group.exceptions:
            for e in group.exceptions:
                if not hasattr(e, "exceptions") or not e.exceptions:
                    return e
            # 所有子异常都是 ExceptionGroup → 继续深入第一支
            group = group.exceptions[0]
    return exc


def _stderr_log_path(name: str) -> Optional[str]:
    """返回该 server 的 stderr 日志文件路径（用于捕获子进程启动错误）

    子进程的 stderr 必须是真实文件描述符，因此不能直接接管到 loguru，
    只能落到文件，失败时再读取尾部内容拼进错误信息。
    """
    try:
        from app.utils.utils import get_app_data_dir

        log_dir = get_app_data_dir() / "logs" / "mcp"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-]", "_", name) or "unnamed"
        return str(log_dir / f"{safe}.stderr.log")
    except Exception as e:  # pragma: no cover - 目录不可写等极端情况
        logger.debug(f"[MCP] 无法创建 stderr 日志目录: {e}")
        return None


def _read_stderr_tail(path: Optional[str], max_chars: int = 600) -> str:
    """读取 stderr 日志尾部，用于把子进程真实报错带回 UI"""
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        content = content.strip()
        if not content:
            return ""
        if len(content) > max_chars:
            content = "..." + content[-max_chars:]
        return content
    except OSError:
        return ""


# Windows 下单个环境变量值上限约 32767 字符（含结尾 \0）。超出时
# subprocess.Popen 会直接抛 ValueError: the environment variable is longer
# than 32767 characters，导致整个 stdio MCP 子进程创建失败 → 服务起不来。
# 宿主（CodeBuddy/WorkBuddy）可能注入 ACC_PRODUCT_CONFIG_V3 等超大变量，
# 必须丢弃，避免“经常有些 MCP 启动不起来”。
_MAX_ENV_VALUE_LEN = 32766


def _build_stdio_env(env: Optional[dict]) -> dict:
    """构造 stdio 子进程环境变量

    MCP SDK 默认只继承 DEFAULT_INHERITED_ENV_VARS（PATH/APPDATA 等十来个），
    会丢掉 HTTP_PROXY / HTTPS_PROXY / NODE_EXTRA_CA_CERTS / npm 镜像等关键变量，
    导致 npx / uvx 拉包失败 → 启动超时。这里显式继承完整父进程环境；
    但丢弃超长变量（见 _MAX_ENV_VALUE_LEN 说明）避免子进程创建失败。
    """
    merged = {}
    for k, v in os.environ.items():
        if not isinstance(v, str):
            continue
        # 跳过 bash 导出的函数定义（安全风险，SDK 同样过滤）
        if v.startswith("()"):
            continue
        # 跳过超长变量（Windows 子进程创建硬限制，见 _MAX_ENV_VALUE_LEN）
        if len(v) > _MAX_ENV_VALUE_LEN:
            continue
        merged[k] = v
    if env:
        for k, v in env.items():
            if v is None:
                continue
            s = str(v)
            if len(s) <= _MAX_ENV_VALUE_LEN:
                merged[k] = s
    return merged


# ── MCP stdio 子进程安全校验 ─────────────────────────────
# 参考 AstrBot(AGPL-3.0) core/agent/mcp_client.py 的 validate_mcp_stdio_config
# 设计移植（GPL-3.0-or-later 协议下的独立实现）：
#   1. 命令名白名单 — 只允许可信的运行时（python/node/deno/uv 等）拉起 MCP server
#   2. 危险命令黑名单 — 禁止用 shell / 网络 / 文件破坏类命令当 launcher
#   3. Shell 元字符检查 — 防止 command 字段里夹带 `;`/`>`/`$()` 等注入
#   4. 参数安全 — 禁止 inline 代码标志（python -c / node -e）、拒绝控制字符
# 校验在任何子进程 spawn 之前执行，失败时连接直接置 FAILED（不启动进程）。

# 允许作为 MCP stdio launcher 的命令（不含扩展名，小写比较）
_STDIO_ALLOWED_COMMANDS = frozenset(
    {
        "python",
        "python3",
        "py",
        "node",
        "npx",
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "bunx",
        "deno",
        "uv",
        "uvx",
    }
)

# 明确禁止的 stdio launcher（shell / 网络 / 危险文件操作 / 提权 / 关机类）
_STDIO_DENIED_COMMANDS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "fish",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "osascript",
        "open",
        "curl",
        "wget",
        "nc",
        "netcat",
        "telnet",
        "ssh",
        "scp",
        "sftp",
        "rm",
        "mv",
        "cp",
        "dd",
        "mkfs",
        "sudo",
        "su",
        "chmod",
        "chown",
        "kill",
        "killall",
        "pkill",
        "shutdown",
        "reboot",
        "poweroff",
        "halt",
        "docker",
        "podman",
    }
)

# 命令中出现的 shell 元字符：包含这些字符说明 command 本身不可信
# （注意：stdio MCP 的 command 是"可执行文件"字段，正常情况下不会含这些）
_STDIO_SHELL_META_RE = re.compile(r"[\r\n\x00;&|<>\`$]")

# python -c / node -e 等 inline 代码标志
_STDIO_INLINE_PYTHON_FLAGS = frozenset({"-c"})  # 只禁 -c；-m 合法（模块启动）
_STDIO_INLINE_JS_FLAGS = frozenset({"-e", "--eval", "-p", "--print", "eval"})


def _normalize_stdio_command_name(command: str) -> str:
    """归一化命令名为小写裸名（去路径、去 Windows 扩展名）

    "C:\\Python312\\python.exe" → "python"
    "uvx" → "uvx"
    "npx.cmd" → "npx"
    """
    name = command.strip().replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    lower = name.lower()
    for ext in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if lower.endswith(ext):
            lower = lower[: -len(ext)]
    return lower


def _validate_stdio_config(config: dict) -> str:
    """校验 stdio 型 MCP 配置，返回空串表示通过，否则返回错误原因。

    只对「含 command 字段的配置」做校验（stdio / http_from_stdio 都走子进程）；
    纯 url 型（sse/streamable http）不涉及本地子进程，直接放行。
    """
    command = config.get("command")
    if not command:
        return ""  # 无 command → 非 stdio（url 型），放行

    if not isinstance(command, str) or not command.strip():
        return "MCP stdio server 必须提供非空 command。"

    # 1) shell 元字符检查（command 字段本身不能被当成 shell 脚本执行）
    if _STDIO_SHELL_META_RE.search(command):
        return "MCP stdio command 含不安全的 shell 元字符（# 示例: `;` `&` `|` `>` `$()` 等）。"

    cmd_name = _normalize_stdio_command_name(command)

    # 2) 危险命令黑名单
    if cmd_name in _STDIO_DENIED_COMMANDS:
        return f"MCP stdio command `{cmd_name}` 被安全策略禁止。"

    # 3) 白名单
    if cmd_name not in _STDIO_ALLOWED_COMMANDS:
        allowed = ", ".join(sorted(_STDIO_ALLOWED_COMMANDS))
        return (
            f"MCP stdio command `{cmd_name}` 不在允许列表中。"
            f"允许的命令: {allowed}。建议改用 uvx / npx / python 等方式启动。"
        )

    # 4) args 校验：控制字符 + inline 代码标志
    args = config.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return "MCP stdio args 必须为字符串列表。"

    for arg in args:
        if "\x00" in arg or "\r" in arg or "\n" in arg:
            return "MCP stdio args 不能包含控制字符（\\x00 / \\r / \\n）。"

    if cmd_name.startswith("python") or cmd_name == "py":
        if any(flag in args for flag in _STDIO_INLINE_PYTHON_FLAGS):
            return "MCP stdio Python server 禁止使用 `-c` inline 代码；应从模块或文件启动。"

    if cmd_name in {"node", "npx", "npm", "pnpm", "yarn", "bun", "bunx", "deno"}:
        if any(flag in args for flag in _STDIO_INLINE_JS_FLAGS):
            return "MCP stdio 禁止使用 inline eval 标志启动 server（应使用包或文件入口）。"

    return ""


# ── 插件路径占位符兜底解析 ──────────────────────────────
# PluginManager.get_mcp_servers() 已在读取 .mcp.json 时把 ${CLAUDE_PLUGIN_ROOT} /
# ${CLAUDE_PLUGIN_DATA} 占位符展开为绝对路径。但以下情况下配置可能以字面量占位符到达
# 启动层（导致子进程报 "can't open file '${CLAUDE_PLUGIN_ROOT}/...'"）：
#   1. 运行中的旧进程（热重载前加载的缓存）尚未失效；
#   2. 用户通过 UI 直接新增含占位符的服务器（未走 get_mcp_servers）；
#   3. 旧版打包产物（dist）不含展开逻辑。
# 在真正拉起子进程前做一次兜底解析，保证子进程永远拿到绝对路径。
#
# ${env.VAR} 展开：docs/mcp.md 的参考示例一直写着 headers 里用
# ${env.GITHUB_TOKEN}，但代码侧从未实现，该写法会带着字面量发往服务端，
# 鉴权必然失败。这里补上：命中环境变量则替换，未设置则保留原样，
# 让下游按字面量处理，便于识别是漏配而非静默变成空串。
_ENV_PLACEHOLDER = re.compile(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_placeholder(value: str) -> str:
    """把字符串中的 ${env.VAR} 替换为环境变量的值；未设置则原样保留。"""
    if not isinstance(value, str) or "${env." not in value:
        return value
    return _ENV_PLACEHOLDER.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def _expand_mcp_placeholder(value: str, plugin_root: Path) -> str:
    """展开单个字符串中的 ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PLUGIN_DATA} / ${env.VAR}。"""
    if not isinstance(value, str) or not value:
        return value
    root = plugin_root.as_posix()
    data = (plugin_root / "data").as_posix()
    normalized = value.replace("\\", "/")
    # 先替换长的（DATA 含 ROOT 前缀），避免 ${CLAUDE_PLUGIN_ROOT}/data 被部分替换
    normalized = normalized.replace("${CLAUDE_PLUGIN_DATA}", data).replace("${CLAUDE_PLUGIN_ROOT}", root)
    return _expand_env_placeholder(normalized)


def _resolve_plugin_paths(config: dict) -> dict:
    """启动前兜底解析 config 中的占位符（防御旧缓存 / 热重载竞态 / UI 直增）。

    两类占位符分两条路：
    - ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PLUGIN_DATA}：需经 config["_source"]
      反推 plugin_root，拿不到就跳过（原样返回）。
    - ${env.VAR}：只依赖进程环境变量，与插件根无关，任何时候都能展开。
    """
    source = config.get("_source", "")
    plugin_root = None
    if source and source.endswith(".json"):
        p = Path(source)
        if p.parent.exists():
            plugin_root = p.parent

    cfg = copy.deepcopy(config)
    if plugin_root is not None:
        cfg["command"] = _expand_mcp_placeholder(cfg.get("command", ""), plugin_root)
        cfg["args"] = [_expand_mcp_placeholder(a, plugin_root) for a in cfg.get("args", [])]
        cfg["url"] = _expand_mcp_placeholder(cfg.get("url", ""), plugin_root)
        cfg["env"] = {
            k: _expand_mcp_placeholder(v, plugin_root) if isinstance(v, str) else v
            for k, v in cfg.get("env", {}).items()
        }
        cfg["headers"] = {
            k: _expand_mcp_placeholder(v, plugin_root) if isinstance(v, str) else v
            for k, v in cfg.get("headers", {}).items()
        }
        return cfg

    # 无 plugin_root：至少把 env 占位符展开掉，避免字面量 "${env.X}" 发往服务端
    cfg["url"] = _expand_env_placeholder(cfg.get("url", ""))
    cfg["headers"] = {
        k: _expand_env_placeholder(v) if isinstance(v, str) else v for k, v in cfg.get("headers", {}).items()
    }
    return cfg


# ── MCP 凭据（API Key）存储与注入 ───────────────────────────
# .mcp.json 可声明 credential 段，让用户在设置界面直接填 Key 并一键打开申请页，
# 不必去配环境变量。形态与 Hana 的 connector.json 对齐：
#
#   "credential": {
#     "label": "API Key",           # 输入框前的标签（缺省 "Key"）
#     "placeholder": "sk-...",      # 输入框占位提示
#     "getUrl": "https://...",      # 「获取 Key」按钮打开的申请页；缺省不显示按钮
#     "inject": {
#       "type": "header",           #   header → 写成 headers[name]
#       "name": "Authorization",    #     header 名
#       "scheme": "Bearer"          #     可选前缀；缺省原样写入
#     },
#     "inject": {                   #   query → 追加为 URL 查询参数
#       "type": "query",
#       "name": "key"
#     },
#     "inject": {                   #   url → 整条端点由用户提供
#       "type": "url"
#     }
#   }
#
# 存储走 PluginConfigStore（与 OAuth token 同一处），键名 _mcp_cred_<server>。
# 同为明文存储，风险口径与 OAuth token 一致。
_CRED_KEY_PREFIX = "_mcp_cred_"


def credential_storage_key(server_name: str) -> str:
    """该 server 在插件 config.json 中的 Key 存储键。"""
    return f"{_CRED_KEY_PREFIX}{server_name}"


def get_credential(plugin_name: str, server_name: str) -> str:
    """读取已保存的 Key；无则空串。异常一律按「无凭据」处理，不阻断连接。"""
    if not plugin_name:
        return ""
    try:
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        raw = PluginConfigStore()._read_raw(plugin_name)
        val = raw.get(credential_storage_key(server_name))
        return val if isinstance(val, str) else ""
    except Exception as e:
        logger.debug(f"[MCP] 读取凭据失败 {plugin_name}/{server_name}: {e}")
        return ""


def set_credential(plugin_name: str, server_name: str, value: str) -> bool:
    """保存 Key；空串表示清除。"""
    if not plugin_name:
        return False
    try:
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        return PluginConfigStore().set_values(plugin_name, {credential_storage_key(server_name): value or ""})
    except Exception as e:
        logger.warning(f"[MCP] 保存凭据失败 {plugin_name}/{server_name}: {e}")
        return False


def _inject_credential(config: dict, cred: dict) -> dict:
    """把已保存的 Key 按 credential.inject 注入 config，返回新 dict。

    无 credential 声明或无已存 Key 时原样返回。优先级高于 ${env.VAR}：
    用户在界面显式填的 Key 应覆盖占位符。
    """
    if not isinstance(cred, dict):
        return config
    key = get_credential(_plugin_name_from_source(config), config.get("name", ""))
    if not key:
        return config

    cfg = copy.deepcopy(config)
    inject = cred.get("inject") or {}
    kind = inject.get("type", "header")

    if kind == "url":
        # 整条端点由用户提供（服务方签发的专属链接），Key 即 URL
        cfg["url"] = key
    elif kind == "query":
        name = inject.get("name") or "key"
        base, _, query = cfg.get("url", "").partition("?")
        # 剥掉同名旧参数与 ${cred.url} 占位，避免重复追加
        parts = [p for p in query.split("&") if p and not p.startswith(f"{name}=")] if query else []
        parts = [p for p in parts if p != "${cred.url}"]
        url = f"{base}?{'&'.join(parts)}" if parts else base
        sep = "&" if "?" in url else "?"
        cfg["url"] = f"{url}{sep}{name}={key}"
    else:
        header = inject.get("name") or "Authorization"
        scheme = inject.get("scheme")
        headers = dict(cfg.get("headers") or {})
        headers[header] = f"{scheme} {key}" if scheme else key
        cfg["headers"] = headers
    return cfg


def _resolve_mcp_credentials(config: dict) -> dict:
    """连接前注入 credential 声明的 Key（若用户已保存）。"""
    return _inject_credential(config, config.get("credential") or {})


def _find_unresolved_env(config: dict) -> list:
    """列出 config 的 url / headers 中仍未解析的 ${env.VAR} 变量名。

    供 _connect_single 在连接前预检：变量未设置时提前失败，避免远端把
    缺凭据响应成 200 + 业务错误 JSON，导致 mcp SDK 挂到超时才报错。
    """
    missing: list = []

    def _scan(value) -> None:
        if not isinstance(value, str):
            return
        for name in _ENV_PLACEHOLDER.findall(value):
            if name not in missing:
                missing.append(name)

    _scan(config.get("url"))
    for v in (config.get("headers") or {}).values():
        _scan(v)
    return missing


# ── MCP OAuth 支持 ────────────────────────────────────
# .mcp.json 里声明 "auth": {"type": "oauth", ...} 的 server 走 OAuth 授权码流程。
# 核心实现见 app/core/mcp_oauth/，这里只负责从 config 反推归属插件并取 provider。


def _plugin_name_from_source(config: dict) -> str:
    """从 config["_source"]（.mcp.json 路径）反推插件名，取不到返回空串。"""
    source = config.get("_source", "")
    if source and source.endswith(".json"):
        return Path(source).parent.name
    return ""


def _get_oauth_provider(config: dict):
    """按 config 里的 auth 声明取得 OAuthClientProvider；非 oauth 型返回 None。"""
    auth = config.get("auth")
    if not isinstance(auth, dict) or auth.get("type") != "oauth":
        return None

    from app.core.mcp_oauth import McpOAuthManager

    plugin_name = _plugin_name_from_source(config)
    if not plugin_name:
        raise ValueError("OAuth 型 MCP 服务器必须来自插件（.mcp.json），无法确定 token 存储位置")

    return McpOAuthManager.get_instance().get_provider(
        plugin_name,
        config.get("name", ""),
        config.get("url", ""),
        auth,
    )


class MCPServerConnection:
    """单个 MCP Server 的连接管理"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.session: Optional[ClientSession] = None
        self.tools: List[mcp_types.Tool] = []
        # 状态机（供 UI 指示灯直接消费）
        self.state: str = MCPState.CONNECTING
        self.last_error: str = ""
        self.updated_at: float = time.time()
        # 持久 Task 管理（__aenter__/__aexit__ 在同一 Task 中）
        self._task: Optional[asyncio.Task] = None
        self._disconnect_event: Optional[asyncio.Event] = None
        self._ready_event: Optional[asyncio.Event] = None
        self._connect_error: Optional[Exception] = None
        self._stderr_path: Optional[str] = None
        # 调用队列：session 及底层 anyio stream 由 lifespan Task 创建，
        # 有 Task 亲和性。跨 Task 直接 await session.call_tool 会永久挂死
        # （实测同 Task 9.8s 成功、跨 Task 30s 超时无响应）。
        # 所有 session 操作必须经此队列丢回 lifespan Task 执行。
        self._call_queue: Optional[asyncio.Queue] = None

    def submit_call(self, coro_factory) -> "asyncio.Future":
        """把一个 session 调用提交给 lifespan Task 执行，返回 awaitable future。

        Args:
            coro_factory: 无参可调用，返回协程。延迟到 lifespan Task 内
                才调用它，避免在提交方 Task 里提前创建协程对象。

        Returns:
            concurrent.futures.Future（run_coroutine_threadsafe 风格），
            调用方用 .result(timeout) 同步拿结果。

        Raises:
            RuntimeError: 连接未就绪 / 队列不存在（已断开）
        """
        if self._call_queue is None or self._loop is None:
            raise RuntimeError(f"服务器 '{self.name}' 未就绪，无法提交调用")
        future: asyncio.Future = self._loop.create_future()
        self._call_queue.put_nowait((coro_factory, future))
        return future

    def set_state(self, state: str, error: str = "") -> None:
        """更新连接状态（mcp 事件循环线程调用）

        状态迁移时触发 PluginChanged hook（mcp_connected/mcp_failed/mcp_disconnected）：
        - CONNECTING 不触发（频繁且无语义价值）
        - 同态重复设置过滤（避免抖动重报）
        - CONNECTED 附带发现的 MCP 工具名列表（mcp_tools）
        - FAILED 附带错误信息（last_error）
        """
        prev = self.state
        self.state = state
        self.last_error = error
        self.updated_at = time.time()

        if prev == state:
            return  # 同态重复，不重复上报
        try:
            from app.core.hooks.hook_manager import trigger_plugin_changed_hook

            if state == MCPState.CONNECTED:
                context = {
                    "action": "mcp_connected",
                    "server_name": self.name,
                    "server_type": self.server_type,
                    "mcp_tools": [t.name for t in self.tools],
                }
                trigger_plugin_changed_hook(context)
            elif state == MCPState.FAILED:
                trigger_plugin_changed_hook(
                    {"action": "mcp_failed", "server_name": self.name, "error": error}
                )
            elif state == MCPState.DISABLED:
                trigger_plugin_changed_hook({"action": "mcp_disconnected", "server_name": self.name})
            # CONNECTING → 不触发
        except Exception as e:
            logger.debug(f"[MCP] trigger mcp state hook failed: {e}")

    @property
    def server_type(self) -> str:
        return self.config.get("type", "stdio")

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True)


class MCPClientManager:
    """
    MCP 客户端管理器（全局单例）

    所有异步 MCP 操作在专用后台线程的持久事件循环中执行。
    每个服务器连接由一个持久 asyncio.Task 管理：
    - Task 内部用 async with 持有 transport + session
    - 断开时设置 Event 信号，Task 自然退出 async with 块
    - 确保 __aenter__ / __aexit__ 在同一 Task 中执行
    """

    TOOL_PREFIX = "mcp__"

    _instance = None
    _ref_count = 0

    @classmethod
    def get_instance(cls) -> "MCPClientManager":
        if cls._instance is None:
            cls._instance = MCPClientManager()
        return cls._instance

    def __init__(self):
        if MCPClientManager._instance is not None and MCPClientManager._instance is not self:
            raise RuntimeError("请使用 MCPClientManager.get_instance() 获取单例")

        # 注册表：保存所有已尝试过的 server（含失败/断开态），
        # 这样 UI 才能区分"启动中/失败/关闭"，而不是统统显示"未连接"。
        self._connections: Dict[str, MCPServerConnection] = {}
        self._connected = False

        # 按 name 加锁：同一 server 只允许一个进行中的连接/断开操作
        self._busy_names: set = set()
        self._busy_lock = threading.Lock()

        # 临时开启的服务器名（模型按需 mcp_manage(action="enable") 建立，
        # 用完 action="disable" 回收）。纯内存态：进程重启即清空，
        # 所以「重启后不会自动重连」是结构性保证，不依赖额外开关。
        # 语义：不进 get_tool_schemas_by_server() 的注入列表（config 仍为
        # enabled=false），但连接本身可用 tool_execute 中转调用；
        # _connect_all / disconnect_missing 的孤儿清理必须豁免它。
        self._ephemeral: set = set()
        # 全局 connect_all 去重：多窗口同时启动时只允许一次全量连接
        self._connect_all_running = False
        # 全量连接正在占用的服务器名（登记进 _busy_names，
        # 防止 connect_server_background 在启动路径连接中时踩踏杀进程）
        self._connect_all_names: set = set()

        # 保护 _connections / _connected 多线程访问（UI 线程 + 事件循环线程）
        self._lock = threading.Lock()

        # 专用后台线程 + 持久事件循环
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_loop()

    def _start_loop(self):
        """启动后台线程的持久事件循环"""
        self._loop_ready = threading.Event()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop_ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, name="mcp-eventloop", daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout=5)
        logger.info("[MCP] 后台事件循环已启动")

    def _run_async(self, coro, timeout: Optional[float] = 60):
        """在后台事件循环中执行协程，同步等待结果

        Args:
            coro: 协程对象
            timeout: 超时时间（秒），默认 60 秒；传 None 表示不设外层超时
                     （用于 connect_all —— 内层每个 server 各自有超时，
                     外层再叠一个 60s 会在服务器较多时提前放弃并误报失败）

        Returns:
            协程执行结果

        Raises:
            TimeoutError: 执行超时
            RuntimeError: 事件循环未运行
        """
        if not self._loop or self._loop.is_closed():
            raise RuntimeError("MCP 事件循环未运行")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except asyncio.TimeoutError:
            # 必须真正 cancel，不能只放弃等待。否则协程仍挂在后台循环里
            # 继续跑（典型：MCP session.call_tool 等读流），变成孤儿协程，
            # 它持有的 stream 引用会把 lifespan TaskGroup 拖崩。
            future.cancel()
            raise TimeoutError(f"协程执行超时（{timeout}秒）")
        except asyncio.CancelledError:
            # 🛡️ 透传取消信号，不拦截
            # 强制停止对话时 asyncio 会抛出 CancelledError（Python 3.11+ 是 BaseException），
            # 必须透传而非捕获，否则取消语义丢失并导致 Qt 事件循环收到未预期异常。
            raise
        except ExceptionGroup as e:
            # 处理 ExceptionGroup（Python 3.11+）
            raise _extract_real_error(e)
        except Exception as e:
            raise _extract_real_error(e)

    # ── 连接生命周期（持久 Task 模式）──────────────

    async def _server_lifespan(self, conn: MCPServerConnection) -> None:
        """
        连接生命周期 — 在专属 Task 中运行。

        用 async with 管理 transport 和 session，
        保证 __aenter__/__aexit__ 在同一 Task 中执行。
        断开时通过 _disconnect_event 信号自然退出。
        """
        server_type = conn.server_type
        # 调用循环：单独 Task，但在本 lifespan Task 的 scope 内创建，
        # 因此与 session 同属一个 anyio TaskGroup，满足 stream 的 Task 亲和性。
        call_loop_task = asyncio.ensure_future(self._call_loop(conn), loop=self._loop)
        try:
            if server_type == "stdio":
                await self._lifespan_stdio(conn)
            elif server_type == "sse":
                await self._lifespan_sse(conn)
            elif server_type == "http":
                # 如果 http 类型但含有 command 字段(无预设 URL)，需要先启动进程获取 URL
                if conn.config.get("command"):
                    await self._lifespan_http_from_stdio(conn)
                else:
                    await self._lifespan_http(conn)
            else:
                conn._connect_error = ValueError(f"不支持的 MCP 服务器类型: {server_type}")
                conn._ready_event.set()
        except asyncio.CancelledError:
            logger.debug(f"[MCP] 服务器 '{conn.name}' 的生命周期 Task 被取消")
        except Exception as e:
            if not conn._ready_event.is_set():
                # 尝试从 ExceptionGroup 中提取真实错误信息
                actual = _extract_real_error(e)
                conn._connect_error = actual
                conn._ready_event.set()
            else:
                # 已就绪后才异常 → 运行中掉线，标记失败让指示灯转红
                # anyio TaskGroup 会把真实异常包成 ExceptionGroup，str(e) 只有
                # "unhandled errors in a TaskGroup (N sub-exception)"，
                # 真实原因（stream closed / read timeout / auth 失败）全在子异常里。
                # 必须先提取再记日志，否则掉线原因不可诊断。
                actual = _extract_real_error(e)
                logger.warning(
                    f"[MCP] 服务器 '{conn.name}' 运行中断开: {type(actual).__name__}: {actual}"
                )
                conn.set_state(MCPState.FAILED, f"运行中断开: {actual}")
        finally:
            # 调用循环先停，避免 session 已置空后它还去碰
            if not call_loop_task.done():
                call_loop_task.cancel()
            was_connected = conn.state == MCPState.CONNECTED
            conn.session = None
            conn.tools = []
            # 主动断开（_disconnect_event 已置位）属正常退出，保持调用方设定的状态；
            # 否则说明子进程自己退出了 → 标记为失败，而不是静默变灰。
            if was_connected and conn._disconnect_event and not conn._disconnect_event.is_set():
                detail = _read_stderr_tail(conn._stderr_path)
                conn.set_state(MCPState.FAILED, f"服务器进程意外退出{(': ' + detail) if detail else ''}")
            logger.info(f"[MCP] 已断开服务器 '{conn.name}'")

    async def _call_loop(self, conn: MCPServerConnection) -> None:
        """在 lifespan Task 的 scope 内消费调用队列。

        为什么需要它：ClientSession 及其底层 anyio memory stream 由
        lifespan Task 创建，有 Task 亲和性。call_tool_sync 来自 ToolExecutor
        所在线程，经 run_coroutine_threadsafe 会把协程丢进一个新 Task；
        在新 Task 里 await session.call_tool 会永久挂死（实测同 Task 9.8s
        成功、跨 Task 30s 无响应超时）。因此所有 session 操作都必须在这里执行。

        队列项为 (coro_factory, future)：coro_factory 延迟到本 Task 内调用，
        避免在提交方 Task 提前创建协程对象导致 "coroutine was never awaited"。

        每次调用单独起 Task 执行，调用方取消 future 时能把取消传播进去，
        避免 session.call_tool 变成挂在读流上的孤儿协程拖崩 lifespan。
        """
        queue = conn._call_queue
        if queue is None:
            return
        while True:
            coro_factory, future = await queue.get()
            if future.done():
                # 调用方已放弃，别再白跑
                continue

            async def _run(factory=coro_factory):
                return await factory()

            task = asyncio.ensure_future(_run(), loop=self._loop)

            def _propagate_cancel(f, t=task):
                # 调用方主动取消（外层超时/强制停止）时，把取消传给执行 Task，
                # 让 session.call_tool 内部立刻收 CancelledError。
                if f.cancelled():
                    t.cancel()

            future.add_done_callback(_propagate_cancel)

            try:
                result = await task
            except asyncio.CancelledError:
                # 本 Task 被取消（调用方放弃或 lifespan 收尾）→ 同步取消 future
                if not future.done():
                    future.cancel()
            except Exception as e:
                if not future.done():
                    future.set_exception(_extract_real_error(e))
            else:
                if not future.done():
                    future.set_result(result)

    async def _lifespan_stdio(self, conn: MCPServerConnection) -> None:
        command = conn.config.get("command", "")
        args = conn.config.get("args", [])
        env = conn.config.get("env")

        # P1-3：启动门禁（审计 + shell 元字符拒启 + 非内置源确认流）
        from app.core.tools.mcp_lsp_safety import gate_server_launch

        source = conn.config.get("_source")
        plugin_name = Path(source).parent.name if source and Path(source).suffix == ".json" else ""
        verdict = gate_server_launch(
            "mcp",
            plugin_name,
            conn.name,
            [command] + list(args or []),
            source=source,
            command=command,
        )
        if verdict != "proceed":
            conn._connect_error = RuntimeError(
                f"MCP 服务器启动被安全门禁拦截（{verdict}）：需用户确认或 args 含非法字符"
            )
            conn._ready_event.set()
            return

        # 显式继承完整父进程环境（代理/证书/镜像源），否则 npx、uvx 常拉包失败
        params = StdioServerParameters(command=command, args=args, env=_build_stdio_env(env))

        # 子进程 stderr 落盘，失败时可回读真实报错（打包后无控制台，否则信息全丢）
        conn._stderr_path = _stderr_log_path(conn.name)
        errlog = None
        try:
            if conn._stderr_path:
                # 每次连接截断，避免日志无限增长且只保留本次启动的错误
                errlog = open(conn._stderr_path, "w", encoding="utf-8", errors="replace")
        except OSError as e:
            logger.debug(f"[MCP] '{conn.name}' 无法打开 stderr 日志: {e}")
            errlog = None

        try:
            client = stdio_client(params, errlog=errlog) if errlog else stdio_client(params)
            async with client as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    conn.session = session
                    conn.tools = result.tools
                    conn._ready_event.set()
                    await conn._disconnect_event.wait()
        finally:
            if errlog:
                try:
                    errlog.close()
                except OSError:
                    pass

    async def _lifespan_sse(self, conn: MCPServerConnection) -> None:
        url = conn.config.get("url", "")
        headers = conn.config.get("headers")

        async with sse_client(url=url, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                conn.session = session
                conn.tools = result.tools
                conn._ready_event.set()
                await conn._disconnect_event.wait()

    async def _lifespan_http(self, conn: MCPServerConnection) -> None:
        url = conn.config.get("url", "")
        headers = conn.config.get("headers")
        # OAuth 型服务器：provider 内部持 token 并按需刷新/重授权，
        # 比静态 headers 优先级高（headers 留作非 OAuth 场景）。
        auth_provider = _get_oauth_provider(conn.config)

        if auth_provider is not None:
            async with streamablehttp_client(url=url, auth=auth_provider) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    conn.session = session
                    conn.tools = result.tools
                    conn._ready_event.set()
                    await conn._disconnect_event.wait()
            return

        async with streamablehttp_client(url=url, headers=headers) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                conn.session = session
                conn.tools = result.tools
                conn._ready_event.set()
                await conn._disconnect_event.wait()

    async def _lifespan_http_from_stdio(self, conn: MCPServerConnection) -> None:
        """
        两阶段连接：先通过 stdio 启动服务器进程 -> 捕获 stdout 中的 URL -> 切换 HTTP 连接。

        适用场景：服务器使用 --transport http 参数，启动后输出 URL 到 stdout
        （如 @brave/brave-search-mcp-server --transport http）
        """
        command = conn.config.get("command", "")
        args = conn.config.get("args", [])
        env = conn.config.get("env")
        merged_env = _build_stdio_env(env)

        # P1-3：启动门禁（两阶段 stdio→http 同样过门禁）
        from app.core.tools.mcp_lsp_safety import gate_server_launch

        source = conn.config.get("_source")
        plugin_name = Path(source).parent.name if source and Path(source).suffix == ".json" else ""
        verdict = gate_server_launch(
            "mcp",
            plugin_name,
            conn.name,
            [command] + list(args or []),
            source=source,
            command=command,
        )
        if verdict != "proceed":
            conn._connect_error = RuntimeError(
                f"MCP 服务器启动被安全门禁拦截（{verdict}）：需用户确认或 args 含非法字符"
            )
            conn._ready_event.set()
            return

        logger.info(f"[MCP] '{conn.name}' 启动进程中获取 URL...")

        # 阶段一：启动进程并捕获 stdout（寻找 URL）
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )

        found_url = None
        try:
            # 读取 stdout 寻找 URL（最大等待 15 秒）
            async def _find_url():
                nonlocal found_url
                assert process.stdout is not None
                url_pattern = re.compile(r'http[s]?://[^\s"\']+')
                while True:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace").strip()
                    logger.debug(f"[MCP] '{conn.name}' stdout: {decoded}")
                    # 尝试匹配 URL
                    match = url_pattern.search(decoded)
                    if match:
                        found_url = match.group(0)
                        return
                    # 也检查是否为纯 URL 行
                    if decoded.startswith("http://") or decoded.startswith("https://"):
                        found_url = decoded
                        return

            await _find_url()
        except asyncio.TimeoutError:
            # 超时未找到 URL，尝试杀掉进程后回退到 stdio
            try:
                process.kill()
            except Exception:
                pass
            raise TimeoutError(
                f"启动服务器 '{conn.name}' 后未能在 stdout 中找到 URL（15秒超时），"
                f"请确认服务器使用了正确的 --transport 参数"
            )

        if not found_url:
            try:
                process.kill()
            except Exception:
                pass
            raise RuntimeError(f"启动服务器 '{conn.name}' 后未能从输出中解析到 URL")

        # 清理 URL 中的尾部分隔符
        found_url = found_url.rstrip("/,.;")
        # 如果 URL 是 0.0.0.0，替换为 localhost
        found_url = found_url.replace("0.0.0.0", "127.0.0.1")
        logger.info(f"[MCP] '{conn.name}' 获取到 URL: {found_url}")

        # 阶段二：用获取到的 URL 建立 HTTP 连接
        # 保持子进程运行（HTTP 服务器进程），连接关闭时杀掉进程
        try:
            async with streamablehttp_client(url=found_url) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    conn.session = session
                    conn.tools = result.tools
                    conn._ready_event.set()
                    logger.info(f"[MCP] '{conn.name}' HTTP 连接成功，发现 {len(conn.tools)} 个工具")
                    await conn._disconnect_event.wait()
        finally:
            # 断开连接时杀掉子进程
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
            except Exception:
                pass

    # ── 连接操作 ──────────────────────────────────────


    def connect_all_background(self, servers_config: List[dict], on_done=None) -> None:
        """后台连接所有 MCP 服务器（不阻塞 UI 线程）

        多窗口场景下每个窗口都会调用一次，这里做全局去重：
        已有一轮全量连接在进行中时直接跳过，避免后启动的窗口把
        前一个窗口刚连好的连接全部断掉（连接踩踏）。

        防热重载踩踏（2026-08）：把本轮全量连接的服务器名登记进
        _busy_names，使热重载路径的 connect_server_background 对同名
        服务器防重跳过——否则 _connect_all 与 _connect_single 并行连接
        同一服务器时，后者会 _disconnect_single 杀掉前者的子进程并取消
        其生命周期 Task，导致后台线程空等 90s、连接反复被杀重启。
        """
        with self._busy_lock:
            if self._connect_all_running:
                logger.debug("[MCP] 已有全量连接进行中，跳过重复请求")
                if on_done:
                    try:
                        on_done(0, len(servers_config), [])
                    except Exception:
                        pass
                return
            self._connect_all_running = True
            # 登记本轮占用的服务器名（enabled 且有名），期间单服务器连接被防重
            self._connect_all_names = {
                s.get("name") for s in servers_config if s.get("name") and s.get("enabled", True)
            }
            self._busy_names |= self._connect_all_names

        def _worker():
            try:
                # 外层不设超时：每个 server 内部各有超时，服务器多时叠加会误杀
                self._run_async(self._connect_all(servers_config), timeout=None)
            except Exception as e:
                logger.error(f"[MCP] 后台连接失败: {e}")
            finally:
                with self._busy_lock:
                    self._connect_all_running = False
                    # 释放本轮占用的服务器名，允许后续单服务器连接
                    self._busy_names -= self._connect_all_names
                    self._connect_all_names = set()
                if on_done:
                    with self._lock:
                        connected = sum(1 for c in self._connections.values() if c.state == MCPState.CONNECTED)
                        failed = [n for n, c in self._connections.items() if c.state == MCPState.FAILED]
                    enabled_total = sum(1 for s in servers_config if s.get("enabled", True))
                    try:
                        on_done(connected, enabled_total, failed)
                    except Exception as e:
                        logger.warning(f"[MCP] on_done 回调异常: {e}")

        threading.Thread(target=_worker, name="mcp-connect", daemon=True).start()
        logger.info("[MCP] 后台连接已启动")

    async def _connect_all(self, servers_config: List[dict]) -> None:
        """全量同步到目标配置（增量，不做"先全断再全连"）

        原实现开头执行 `_disconnect_all()`，多窗口各调一次时会把已连好的
        连接反复拆掉重建，是"服务经常起不来"的主要来源之一。
        """
        # 收集需要连接的服务器列表
        enabled_servers = []
        enabled_names = set()
        for server_cfg in servers_config:
            name = server_cfg.get("name", "")
            if not name:
                continue
            if not server_cfg.get("enabled", True):
                logger.debug(f"[MCP] 跳过已禁用的服务器: {name}")
                continue
            enabled_servers.append(server_cfg)
            enabled_names.add(name)

        # 已被配置层打开（enabled=true）的服务器不再算临时：用户已把它转为常驻
        with self._lock:
            self._ephemeral -= enabled_names

        # 1) 断开已不在目标列表中的连接（配置被删除或被禁用）
        #    豁免临时开启的服务器：它们的 config.enabled 恒为 false，
        #    按配置名单判定会被当孤儿杀掉（全量连接/多窗口刷新時踩掉）。
        with self._lock:
            stale = [
                n
                for n, c in self._connections.items()
                if n not in enabled_names and c.session and n not in self._ephemeral
            ]
        for name in stale:
            try:
                await self._disconnect_single(name)
            except Exception as e:
                logger.warning(f"[MCP] 断开陈旧连接 '{name}' 失败: {e}")

        # 2) 跳过已连接且配置未变的 server（幂等，避免踩踏）
        pending = []
        for server_cfg in enabled_servers:
            name = server_cfg["name"]
            with self._lock:
                conn = self._connections.get(name)
            if conn and conn.state == MCPState.CONNECTED and conn.config == server_cfg:
                logger.debug(f"[MCP] '{name}' 已连接且配置未变，跳过")
                continue
            pending.append(server_cfg)

        if not pending:
            with self._lock:
                self._connected = any(c.state == MCPState.CONNECTED for c in self._connections.values())
            return

        # 3) 并行启动所有待连接的服务器
        tasks = {
            cfg["name"]: asyncio.create_task(
                self._connect_single(cfg["name"], cfg),
                name=f"mcp-connect-{cfg['name']}",
            )
            for cfg in pending
        }

        # 等待所有连接完成（每个任务内部各自超时，互不阻塞）
        for name, task in tasks.items():
            try:
                await task
            except Exception as e:
                logger.error(f"[MCP] 连接服务器 '{name}' 失败: {e}")

        # 从实际连接状态计算，避免全失败也显示已连接
        with self._lock:
            self._connected = any(c.state == MCPState.CONNECTED for c in self._connections.values())


    def connect_server_background(self, name: str, config: dict, on_done=None) -> None:
        """后台连接单个 MCP 服务器（不阻塞 UI）

        同一 name 只允许一个进行中的连接操作，重复请求被丢弃。
        """
        # 防重：同名服务器正在连接/断开中，跳过
        # on_done 必须在锁外调用：真实 UI 的 on_done 会 emit 信号触发主线程
        # InfoBar/刷新，若在持锁期间同步执行会拉长锁持有时间、干扰其他线程。
        with self._busy_lock:
            if name in self._busy_names:
                dup = True
            else:
                self._busy_names.add(name)
                dup = False
        if dup:
            logger.debug(f"[MCP] '{name}' 正在连接/断开中，跳过重复请求")
            if on_done:
                try:
                    on_done(name, False, "服务器正在操作中，请稍后重试")
                except Exception:
                    pass
            return

        # 同步预登记 CONNECTING，保证指示灯从点击那一刻就变黄并持续到出结果，
        # 而不是等后台线程真正进入 _connect_single 才有状态可读。
        with self._lock:
            conn = self._connections.get(name)
            if conn is None:
                conn = MCPServerConnection(name, config)
                self._connections[name] = conn
            conn.config = config
            conn.set_state(MCPState.CONNECTING)

        def _worker():
            success = False
            error_msg = ""
            try:
                success, error_msg = self._run_async(self._connect_single(name, config), timeout=None)
            except Exception as e:
                error_msg = str(e)
                logger.error(f"[MCP] 热添加服务器 '{name}' 失败: {e}")
            finally:
                with self._busy_lock:
                    self._busy_names.discard(name)
                if on_done:
                    try:
                        on_done(name, success, error_msg)
                    except Exception as e:
                        logger.warning(f"[MCP] on_done 回调异常: {e}")

        threading.Thread(target=_worker, name="mcp-hot-add", daemon=True).start()

    # 连接超时（秒）。npx / uvx 首次冷启动需要联网拉包，30s 经常不够。
    DEFAULT_CONNECT_TIMEOUT = 90

    async def _connect_single(self, name: str, config: dict) -> tuple:
        """
        连接单个服务器：启动生命周期 Task 并等待就绪
        返回: (success: bool, error_msg: str)

        注意：无论成败，conn 都会登记进 self._connections，
        以便 UI 能读到 CONNECTING / FAILED 状态（而不是一律显示"未连接"）。
        """
        # 兜底解析插件路径占位符（防御旧缓存 / 热重载竞态 / UI 直增未展开的情况）
        config = _resolve_plugin_paths(config)

        # 注入用户在设置界面填的 Key（credential 声明），优先级高于 ${env.VAR}
        config = _resolve_mcp_credentials(config)

        # ── 已声明 credential 但用户还没填 Key 的预检 ────────────────────
        # 这类 server 的端点几乎都要求凭据。不拦的话，远端要么回 401，
        # 要么像高德那样回 200 + 业务错误 JSON —— 后者会让 mcp SDK 把
        # 响应当成"读流里的异常消息"而非抛错，最终挂到 90 秒超时才失败。
        # 直接说清楚去哪儿填，比等超时好。
        _cred = config.get("credential")
        if isinstance(_cred, dict) and _cred and not get_credential(
            _plugin_name_from_source(config), config.get("name", "")
        ):
            label = _cred.get("label") or "Key"
            reject = f"未配置凭据：请在设置 → MCP 连接器中为本服务器填写「{label}」"
            logger.warning(f"[MCP] 拒绝启动服务器 '{name}': {reject}")
            conn = MCPServerConnection(name, config)
            conn.set_state(MCPState.FAILED, reject)
            with self._lock:
                self._connections[name] = conn
            return False, reject

        # ── 未展开的 ${env.VAR} 预检 ────────────────────────────────────
        # 展开后 url / headers 里若还留着 ${env.X}，说明该环境变量没设。
        # 此时继续连只会拿到 401/400，或像高德那样返回 200 + 业务错误 JSON，
        # 而 mcp SDK 会把这类响应当成"读流里的一条异常消息"而非抛错，
        # 最终表现是挂住直到 connect timeout，用户看到一堆 pydantic 噪音。
        # 提前拦住，直接说缺哪个变量。
        _unresolved = _find_unresolved_env(config)
        if _unresolved:
            reject = f"环境变量未设置: {', '.join(_unresolved)}（配置中的 ${{env.*}} 占位符未解析）"
            logger.warning(f"[MCP] 拒绝启动服务器 '{name}': {reject}")
            conn = MCPServerConnection(name, config)
            conn.set_state(MCPState.FAILED, reject)
            with self._lock:
                self._connections[name] = conn
            return False, reject

        # 如果已存在，先断开（清理旧进程，避免端口/文件锁被占导致新进程起不来）
        with self._lock:
            existing = self._connections.get(name)
        if existing is not None:
            await self._disconnect_single(name)

        # ── stdio 安全校验：在 spawn 子进程之前拦截危险配置 ──
        # 只拦截含 command 的 stdio 型配置；url 型（sse/http）直接放行。
        # 放在断开旧连接之后：若新配置非法，旧连接已释放，登记 FAILED 供 UI 显示。
        if config.get("command"):
            reject = _validate_stdio_config(config)
            if reject:
                logger.warning(f"[MCP] 拒绝启动服务器 '{name}': {reject}")
                conn = MCPServerConnection(name, config)
                conn.set_state(MCPState.FAILED, reject)
                with self._lock:
                    self._connections[name] = conn
                return False, reject

        conn = MCPServerConnection(name, config)
        conn._disconnect_event = asyncio.Event()
        conn._ready_event = asyncio.Event()
        conn._connect_error = None
        conn.set_state(MCPState.CONNECTING)

        # 立即登记，让 UI 指示灯在整个启动过程中保持黄色
        with self._lock:
            self._connections[name] = conn

        timeout = config.get("timeout") or self.DEFAULT_CONNECT_TIMEOUT

        # 调用队列与 loop 引用必须在 lifespan Task 启动前就位，
        # 否则 submit_call 可能在 Task 尚未开跑时就认为未就绪。
        conn._loop = self._loop
        conn._call_queue = asyncio.Queue()

        # 在后台事件循环中启动生命周期 Task
        conn._task = asyncio.ensure_future(self._server_lifespan(conn), loop=self._loop)

        # 等待就绪或出错
        try:
            await asyncio.wait_for(conn._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # 超时必须彻底回收子进程，否则残留进程会占住资源使下次启动失败
            conn._task.cancel()
            try:
                await asyncio.wait({conn._task}, timeout=5)
            except Exception:
                pass
            detail = _read_stderr_tail(conn._stderr_path)
            msg = f"连接服务器 '{name}' 超时（{timeout}秒）"
            if detail:
                msg += f"\n子进程输出: {detail}"
            conn.set_state(MCPState.FAILED, msg)
            logger.error(f"[MCP] {msg}")
            return False, msg

        if conn._connect_error:
            err = conn._connect_error
            # 尝试提取更友好的错误信息
            err_str = str(err) or err.__class__.__name__
            if "JSONRPC" in err_str and "Invalid JSON" in err_str:
                # 服务器 stdout 输出了非 JSON 内容 → 可能是 http/sse 服务器却用了 stdio 类型
                err_str += "（服务器输出了非 JSON 内容到 stdout，请检查配置类型是否正确）"
            detail = _read_stderr_tail(conn._stderr_path)
            if detail and detail not in err_str:
                err_str += f"\n子进程输出: {detail}"
            msg = f"连接服务器 '{name}' 失败: {err_str}"
            conn.set_state(MCPState.FAILED, msg)
            logger.error(f"[MCP] {msg}")
            return False, msg

        conn.set_state(MCPState.CONNECTED)
        with self._lock:
            self._connected = True
            # 配置层本就开启（用户从 UI 打开了开关）→ 归常驻，不再算临时。
            # 漏这步会让 set_status 把它渲染成「临时开启」，误导模型去 disable。
            if config.get("enabled", True):
                self._ephemeral.discard(name)
        logger.info(f"[MCP] 已连接服务器 '{name}'，发现 {len(conn.tools)} 个工具")
        return True, ""

    # ── 断开连接 ──────────────────────────────────────


    def disconnect_server_background(self, name: str, on_done=None) -> None:
        """后台断开单个 MCP 服务器（不阻塞 UI）

        与 connect_server_background 共享一个锁，同名请求丢弃。
        """
        with self._busy_lock:
            if name in self._busy_names:
                logger.debug(f"[MCP] '{name}' 正在连接/断开中，跳过重复请求")
                if on_done:
                    try:
                        on_done(name)
                    except Exception:
                        pass
                return
            self._busy_names.add(name)

        def _worker():
            try:
                # keep_record：保留注册表条目并置 DISABLED，UI 显示黑色"已关闭"
                self._run_async(self._disconnect_single(name, keep_record=True))
            except Exception as e:
                logger.error(f"[MCP] 热断开服务器 '{name}' 失败: {e}")
            finally:
                with self._busy_lock:
                    self._busy_names.discard(name)
                if on_done:
                    try:
                        on_done(name)
                    except Exception as e:
                        logger.warning(f"[MCP] on_done 回调异常: {e}")

        # 手动关闭的服务器不再算临时（防残留幽灵状态：连接已断但 _ephemeral 里还留着，
        # 会让 list 把它渲染成「临时开启」并让孤儿清理跳过它）
        with self._lock:
            self._ephemeral.discard(name)

        threading.Thread(target=_worker, name="mcp-hot-disconnect", daemon=True).start()

    async def _disconnect_single(self, name: str, *, keep_record: bool = False) -> bool:
        """断开单个服务器

        ⚠️ 关键：绝不能在持有 self._lock（threading.Lock）时 await。
        事件循环线程持锁 await 期间会去调度其它协程，若那些协程 / UI 线程
        再去 `with self._lock` 就会阻塞住整个事件循环线程，导致锁永远不释放
        —— 事件循环彻底死锁，表现为 MCP 全部起不来且设置页卡死。
        这里改为：锁内只做摘取和标记，await 全部放到锁外执行。

        Args:
            keep_record: True 时保留注册表条目（仅置为 DISABLED），
                         供 UI 显示"已关闭"的黑色指示灯。
        """
        with self._lock:
            conn = self._connections.get(name)
            if not conn:
                return False
            if keep_record:
                conn.set_state(MCPState.DISABLED)
            else:
                self._connections.pop(name, None)
            task = conn._task
            disconnect_event = conn._disconnect_event
            conn.session = None
            conn.tools = []
            self._connected = any(c.state == MCPState.CONNECTED for c in self._connections.values())

        # ── 以下均在锁外执行 ──
        # 通知生命周期 Task 退出（走 async with 正常清理路径）
        if disconnect_event:
            disconnect_event.set()

        # 取消 Task 作为备份（CancelledError 在 async with 内触发 __aexit__）
        if task and not task.done():
            task.cancel()
            # 等待 Task 完全退出，确保子进程释放资源（文件锁、端口等），
            # 避免热重载时旧进程未完全退出 → 新进程初始化失败。
            # 用 asyncio.wait 而非 wait_for：前者不会把 CancelledError 抛给调用方。
            try:
                await asyncio.wait({task}, timeout=5)
            except Exception as e:
                logger.debug(f"[MCP] 等待 '{name}' 生命周期退出异常: {e}")
        return True

    def disconnect_all_sync(self, *, keep_record: bool = True) -> None:
        """同步断开所有连接"""
        try:
            self._run_async(self._disconnect_all(keep_record=keep_record), timeout=None)
        except Exception as e:
            logger.warning(f"[MCP] 断开连接失败: {e}")

    def disconnect_all_background(self, on_done=None) -> None:
        """后台断开所有连接（不阻塞 UI）"""

        def _worker():
            try:
                self._run_async(self._disconnect_all())
            except Exception as e:
                logger.error(f"[MCP] 后台断开所有连接失败: {e}")
            finally:
                if on_done:
                    try:
                        on_done()
                    except Exception as e:
                        logger.warning(f"[MCP] on_done 回调异常: {e}")

        threading.Thread(target=_worker, name="mcp-disconnect-all", daemon=True).start()

    async def _disconnect_all(self, *, keep_record: bool = True) -> None:
        with self._lock:
            names = list(self._connections.keys())
        for name in names:
            try:
                await self._disconnect_single(name, keep_record=keep_record)
            except Exception as e:
                logger.error(f"[MCP] 断开服务器 '{name}' 失败: {e}")

        with self._lock:
            if not keep_record:
                self._connections.clear()
            self._connected = False
            # 全局断开也是临时开启的终结：清空集合，避免残留名字让后续
            # disconnect_missing 跳过不存在的连接 / list 渲染出错状态
            self._ephemeral.clear()

    def disconnect_missing(self, valid_names: set) -> None:
        """断开所有不在 valid_names 中的已注册连接

        用于热重载后清理：插件被删除 / .mcp.json 中服务器被移除 / 被禁用时，
        对应的子进程不会自动退出，需要显式断开，否则残留进程继续运行。
        """
        with self._lock:
            # 豁免临时开启项：它们不在 enabled 名单里，但用户/模型正在用
            orphans = [
                n for n in self._connections if n not in valid_names and n not in self._ephemeral
            ]
        if not orphans:
            return
        logger.info(f"[MCP] 热重载检测到 {len(orphans)} 个已失效连接，准备断开: {orphans}")
        for name in orphans:
            self.disconnect_server_background(name)

    # ── 工具 Schema ──────────────────────────────────

    def get_tool_schemas(self, include_ephemeral: bool = False) -> List[Dict]:
        """获取所有 MCP 工具的 OpenAI function calling schema

        include_ephemeral=False（默认）：只含常驻开启的服务器——常驻注入与
        token 估算必须用它，临时开启是纯内存态，进常驻列表就丢掉「零 token
        占用」的结构性保证。
        include_ephemeral=True：额外含临时开启（_ephemeral）的服务器。仅供
        tool_search 搜索面使用：临时开启后若上下文被压缩，模型必须能重新
        找回工具定义，否则那台服务器等于白开。
        """
        schemas = []
        for server_schemas in self.get_tool_schemas_by_server(
            include_ephemeral=include_ephemeral
        ).values():
            schemas.extend(server_schemas)
        return schemas

    def _schemas_of(self, conn: MCPServerConnection) -> List[Dict]:
        """单个连接的工具 schema 列表（不做 enabled 过滤，由调用方决定）"""
        schemas = []
        for tool in conn.tools:
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{self.TOOL_PREFIX}{conn.name}__{tool.name}",
                        "description": tool.description or f"MCP tool: {tool.name}",
                        "parameters": tool.inputSchema
                        or {
                            "type": "object",
                            "properties": {},
                        },
                    },
                }
            )
        return schemas

    def get_tool_schemas_by_server(self, include_ephemeral: bool = False) -> Dict[str, List[Dict]]:
        """按服务器分组获取 MCP 工具的 OpenAI function calling schema（设置卡逐行 token 估算用）

        默认只含 config.enabled 为真的服务器：临时开启（_ephemeral）的 config
        仍是关闭态，天然不进常驻工具列表，这是「省 token」的结构性保证。
        include_ephemeral=True 时把临时开启的也纳入（tool_search 搜索面用）。
        """
        by_server: Dict[str, List[Dict]] = {}
        with self._lock:
            items = list(self._connections.items())
            ephemeral = set(self._ephemeral) if include_ephemeral else set()
        for server_name, conn in items:
            if not conn.session:
                continue
            if not conn.enabled and server_name not in ephemeral:
                continue
            by_server.setdefault(server_name, []).extend(self._schemas_of(conn))
        return by_server

    # ── 工具调用 ──────────────────────────────────────

    def call_tool_sync(self, prefixed_name: str, arguments: dict, timeout: float = 60) -> ToolResult:
        """同步调用 MCP 工具（供 ToolExecutor 调用）

        Args:
            prefixed_name: 带前缀的工具名（如 mcp__server__tool）
            arguments: 工具参数
            timeout: 超时时间（秒），默认 60 秒（原 120s，见 2026-07-22 perf）
        """
        try:
            return self._run_async(self._call_tool(prefixed_name, arguments), timeout=timeout)
        except TimeoutError as e:
            logger.error(f"[MCP] 调用工具 '{prefixed_name}' 超时（{timeout}s）: {e}")
            return ToolResult(False, error=f"MCP 工具调用超时（{timeout}秒），请稍后重试")
        except Exception as e:
            logger.error(f"[MCP] 调用工具 '{prefixed_name}' 失败: {e}")
            return ToolResult(False, error=f"MCP 工具调用失败: {e}")

    async def _call_tool(self, prefixed_name: str, arguments: dict) -> ToolResult:
        parsed = self._parse_tool_name(prefixed_name)
        if not parsed:
            return ToolResult(False, error=f"无效的 MCP 工具名: {prefixed_name}")

        server_name, tool_name = parsed
        with self._lock:
            conn = self._connections.get(server_name)
        if not conn or not conn.session:
            return ToolResult(False, error=f"MCP 服务器 '{server_name}' 未连接")

        # session 有 Task 亲和性，必须在 lifespan Task 内调用。
        # call_tool_sync 已在后台循环的独立 Task 里跑本协程，此处再经队列
        # 中转一次，让真正的 session.call_tool 落到 lifespan Task 上执行。
        async def _do_call():
            result = await conn.session.call_tool(tool_name, arguments)

            text_parts = []
            for content in result.content or []:
                if isinstance(content, mcp_types.TextContent):
                    text_parts.append(content.text)
                elif hasattr(content, "text"):
                    text_parts.append(str(content.text))

            output = "\n".join(text_parts) if text_parts else str(result)

            if result.isError:
                return ToolResult(False, error=output)
            return ToolResult(True, content=output)

        try:
            # 已在后台事件循环内（call_tool_sync 用 run_coroutine_threadsafe 送进来），
            # 直接 await 队列 future 即可，无需再套一层线程桥。
            return await asyncio.wrap_future(conn.submit_call(_do_call))
        except asyncio.CancelledError:
            # 🛡️ 透传取消：调用方超时会 cancel 本协程，此时必须让
            # _call_loop 里那个仍在跑的 session.call_tool 也感受到取消，
            # 否则它会变成孤儿协程挂在读流上，最终把 lifespan TaskGroup 拖崩
            # （表现为「调用一次工具后连接就断」）。
            raise
        except Exception as e:
            logger.error(f"[MCP] 调用工具 '{prefixed_name}' 失败: {e}")
            return ToolResult(False, error=f"MCP 工具调用失败: {e}")

    # ── 辅助方法 ──────────────────────────────────────

    def _parse_tool_name(self, prefixed_name: str) -> Optional[tuple]:
        if not prefixed_name.startswith(self.TOOL_PREFIX):
            return None
        remainder = prefixed_name[len(self.TOOL_PREFIX) :]
        if "__" not in remainder:
            return None
        server_name, tool_name = remainder.split("__", 1)
        return server_name, tool_name

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    # ── 临时开启（模型按需启用，用完显式关闭）────────────

    def ephemeral_names(self) -> set:
        """当前处于临时开启状态的服务器名集合"""
        with self._lock:
            return set(self._ephemeral)

    def enable_ephemeral(self, name: str) -> Tuple[bool, object]:
        """临时开启一个已配置但关闭的服务器（阻塞到连上或失败）

        与 UI 开关的区别：**不写 config.enabled**，只在内存连接表里建立连接，
        并登记进 _ephemeral。因此：
        - get_tool_schemas_by_server() 仍跳过它，常驻工具列表零占用；
        - _connect_all / disconnect_missing 的孤儿清理显式豁免它。

        Returns:
            (True, schemas)：schemas 为该服务器全部工具定义（供模型经
                tool_execute 中转调用）；
            (False, error)：失败原因（字符串）。
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            servers = pm.get_mcp_servers() if pm.is_initialized() else []
        except Exception as e:
            return False, f"读取 MCP 配置失败: {e}"
        config = next((s for s in servers if s.get("name") == name), None)
        if config is None:
            return False, f"未找到名为 '{name}' 的 MCP 服务器配置（用 action=\"list\" 查看可用服务器名）"

        already_enabled = bool(config.get("enabled", True))
        with self._lock:
            conn = self._connections.get(name)
            connected = conn is not None and conn.session is not None and conn.state == MCPState.CONNECTED
            tools = list(conn.tools) if conn is not None else []
        if connected:
            if already_enabled:
                return False, (
                    f"'{name}' 已是常驻开启状态，它的工具已在当前工具列表中，直接调用即可（无需临时开启）。"
                )
            with self._lock:
                self._ephemeral.add(name)
            with self._lock:
                conn = self._connections.get(name)
                return True, self._schemas_of(conn) if conn is not None else []

        with self._busy_lock:
            if name in self._busy_names:
                return False, f"'{name}' 正在连接/断开中，请稍后重试"
            self._busy_names.add(name)
        # 先登记临时态：连接期间若其它入口触发 _connect_all / disconnect_missing，
        # 孤儿清理需能识别它是「正在被临时开启」而不是待清理的残留。
        with self._lock:
            self._ephemeral.add(name)
        try:
            try:
                success, error = self._run_async(self._connect_single(name, config), timeout=None)
            except Exception as e:
                success, error = False, str(e)
            if not success:
                with self._lock:
                    self._ephemeral.discard(name)
                msg = error or "连接失败"
                if "need_confirm" in msg:
                    return False, (
                        f"MCP 服务器 '{name}' 来自用户级插件，首次启动需你在"
                        f"「设置 → MCP 连接器」中确认放行一次，之后模型即可临时开启。"
                    )
                return False, msg
            # 配置层本就开启的服务器：救活后归常驻，不留在临时集合里
            if already_enabled:
                with self._lock:
                    self._ephemeral.discard(name)
            with self._lock:
                conn = self._connections.get(name)
                schemas = self._schemas_of(conn) if conn is not None else []
            logger.info(f"[MCP] '{name}' 临时开启成功，{len(schemas)} 个工具待中转调用")
            return True, schemas
        finally:
            with self._busy_lock:
                self._busy_names.discard(name)

    def disable_ephemeral(self, name: str) -> Tuple[bool, str]:
        """关闭模型临时开启的服务器（常驻开启的不受影响，防止误关用户配置）"""
        with self._lock:
            if name not in self._ephemeral:
                return False, (
                    f"'{name}' 不是临时开启的服务器，本工具不能关闭它。"
                    f"如需关闭常驻服务器，请用户在「设置 → MCP 连接器」中操作。"
                )
        with self._busy_lock:
            if name in self._busy_names:
                return False, f"'{name}' 正在连接/断开中，请稍后重试"
            self._busy_names.add(name)
        try:
            self._run_async(self._disconnect_single(name, keep_record=True), timeout=None)
        except Exception as e:
            logger.error(f"[MCP] 临时关闭 '{name}' 失败: {e}")
            return False, f"断开失败: {e}"
        finally:
            with self._busy_lock:
                self._busy_names.discard(name)
            with self._lock:
                self._ephemeral.discard(name)
        logger.info(f"[MCP] '{name}' 临时开启已关闭")
        return True, ""

    def _configured_servers(self) -> List[dict]:
        """配置侧的 MCP 服务器全集（含从未建连的）。

        get_status 只反映内存连接表，而 enabled=false 的服务器在 _connect_all
        里被直接跳过、永不建连，于是它们在整个状态面（mcp_manage list、设置卡）
        上完全不可见——用户配了 12 个 HTTP MCP，工具却回答"没有任何 MCP 服务器
        配置"。配置来源必须由调用方合并进来，才能表达"配置全集 + 运行时状态"。
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            return list(pm.get_mcp_servers()) if pm.is_initialized() else []
        except Exception as e:
            logger.debug(f"[MCP] 读取配置侧服务器列表失败: {e}")
            return []

    def get_status(self) -> List[Dict]:
        # 注意：返回的 tools 必须带 mcp__{server}__ 前缀，与 get_tool_schemas() 保持一致，
        # 避免 LLM 从 mcp_manage 看到裸名后误用导致调用失败。
        #
        # 返回分两段：连接表里的条目（含 CONNECTING / FAILED / DISABLED，旧实现
        # 只返回成功的，UI 读不到"启动中"和"失败"）+ 配置里有但从未建连的占位
        # 条目（DISABLED）。只有这样才能表达"这个服务器存在，只是没开"。
        status = []
        seen: set = set()
        with self._busy_lock:
            busy_names = set(self._busy_names)
        with self._lock:
            items = list(self._connections.items())
            ephemeral = set(self._ephemeral)
        for name, conn in items:
            seen.add(name)
            busy = name in busy_names or conn.state == MCPState.CONNECTING
            status.append(
                {
                    "name": name,
                    "type": conn.server_type,
                    "enabled": conn.enabled,
                    "ephemeral": name in ephemeral,
                    "connected": conn.session is not None and conn.state == MCPState.CONNECTED,
                    "state": MCPState.CONNECTING if busy and conn.state != MCPState.CONNECTED else conn.state,
                    "error": conn.last_error,
                    "busy": busy,
                    "tool_count": len(conn.tools),
                    "tools": [f"{self.TOOL_PREFIX}{name}__{t.name}" for t in conn.tools],
                }
            )
        # 配置有、连接表无 → 从未建连（多为 enabled=false 被 _connect_all 跳过）
        for cfg in self._configured_servers():
            name = cfg.get("name", "")
            if not name or name in seen:
                continue
            status.append(
                {
                    "name": name,
                    "type": cfg.get("type", "stdio"),
                    "enabled": cfg.get("enabled", True),
                    "ephemeral": False,
                    "connected": False,
                    "state": MCPState.DISABLED,
                    "error": "",
                    "busy": False,
                    "tool_count": 0,
                    "tools": [],
                }
            )
        return status

    # ── 引用计数（多窗口生命周期）──────────────────────

    def acquire(self):
        MCPClientManager._ref_count += 1
        logger.debug(f"[MCP] acquire, ref_count={MCPClientManager._ref_count}")

    def release(self):
        MCPClientManager._ref_count = max(0, MCPClientManager._ref_count - 1)
        logger.debug(f"[MCP] release, ref_count={MCPClientManager._ref_count}")
        if MCPClientManager._ref_count == 0 and self._connected:
            self.disconnect_all_sync()

    def shutdown(self):
        """彻底关闭后台事件循环（进程退出时调用）"""
        if self._connected:
            self.disconnect_all_sync(keep_record=False)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)


# ═══════════════════════════════════════════════════════════
# MCP Server 自动发现
# ═══════════════════════════════════════════════════════════
# 配置路径来源参考：
# - Claude Desktop: https://modelcontextprotocol.io/docs/getting-started/installation
# - Cursor: https://www.rapidevelopers.com/mcp-tutorial/how-to-configure-mcp-in-cursor-settings
# - Windsurf: https://deepwiki.com/hidao80/mcp-tutorial-1/3.3-windsurf-setup
# - Claude Code: https://docs.code.claude.com/mcp/setup/
# - VS Code (Cline/Continue): .vscode/mcp.json（项目级）
# ═══════════════════════════════════════════════════════════


def _discover_claude_desktop_servers() -> List[dict]:
    """
    扫描 Claude Desktop 配置，发现 MCP 服务器

    配置路径：
    - Windows: %APPDATA%/Claude/claude_desktop_config.json
    - macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json
    - Linux:   ~/.config/Claude/claude_desktop_config.json

    配置格式：
    {
      "mcpServers": {
        "server-name": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]
        }
      }
    }
    """
    servers = []
    config_paths = []

    if os.name == "nt" or os.environ.get("OS") == "Windows_NT":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            config_paths.append(os.path.join(appdata, "Claude", "claude_desktop_config.json"))
    elif os.uname().sysname == "Darwin":
        config_paths.append(os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json"))
    else:
        config_paths.append(os.path.expanduser("~/.config/Claude/claude_desktop_config.json"))

    for config_path in config_paths:
        if not os.path.exists(config_path):
            continue

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            config = json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.debug(f"[MCP] 解析 Claude Desktop 配置失败 {config_path}: {e}")
            continue

        mcp_servers = config.get("mcpServers", {})
        for name, server_cfg in mcp_servers.items():
            if not isinstance(server_cfg, dict):
                continue

            command = server_cfg.get("command", "")
            args = server_cfg.get("args", [])
            if not command:
                continue

            servers.append(
                {
                    "name": name,
                    "type": "stdio",
                    "command": command,
                    "args": args,
                    "env": server_cfg.get("env"),
                    "enabled": False,
                    "_source": "claude_desktop",
                    "_source_path": config_path,
                }
            )
            logger.info(f"[MCP] 发现 Claude Desktop 服务器: {name}")

    return servers


def _discover_cursor_servers() -> List[dict]:
    """
    扫描 Cursor IDE 配置，发现 MCP 服务器

    配置路径（全局）：
    - Windows: %APPDATA%/Cursor/User/globalStorage/mcp-settings.json
    - macOS:   ~/Library/Application Support/Cursor/User/globalStorage/mcp-settings.json
    - Linux:   ~/.config/Cursor/User/globalStorage/mcp-settings.json

    配置格式：
    {
      "mcpServers": {
        "server-name": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem"]
        }
      }
    }

    注意：Cursor 还支持项目级 ~/.cursor/mcp.json（全局），
    但由于是用户 home 目录，与全局配置重复，这里只扫描 globalStorage。
    """
    servers = []
    config_paths = []

    if os.name == "nt" or os.environ.get("OS") == "Windows_NT":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            config_paths.append(os.path.join(appdata, "Cursor", "User", "globalStorage", "mcp-settings.json"))
    elif os.uname().sysname == "Darwin":
        config_paths.append(
            os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/mcp-settings.json")
        )
    else:
        config_paths.append(os.path.expanduser("~/.config/Cursor/User/globalStorage/mcp-settings.json"))

    for config_path in config_paths:
        if not os.path.exists(config_path):
            continue

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            config = json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.debug(f"[MCP] 解析 Cursor 配置失败 {config_path}: {e}")
            continue

        mcp_servers = config.get("mcpServers", {})
        for name, server_cfg in mcp_servers.items():
            if not isinstance(server_cfg, dict):
                continue

            command = server_cfg.get("command", "")
            args = server_cfg.get("args", [])
            if not command:
                continue

            servers.append(
                {
                    "name": name,
                    "type": "stdio",
                    "command": command,
                    "args": args,
                    "env": server_cfg.get("env"),
                    "enabled": False,
                    "_source": "cursor",
                    "_source_path": config_path,
                }
            )
            logger.info(f"[MCP] 发现 Cursor MCP 服务器: {name}")

    return servers


def _merge_and_deduplicate(existing: List[dict], discovered: List[dict]) -> Tuple[List[dict], List[dict]]:
    """
    合并已有配置和自动发现的配置，去重

    去重规则（按 command + args 组合判断）：
    - 已有配置保留
    - 新发现的如果 command+args 与已有完全相同则跳过
    - 新发现的如果 name 相同但 command+args 不同，name 后面加后缀区分

    Returns:
        (merged_list, new_ones) — 合并后的完整列表 + 仅新发现的列表
    """
    # 已有的 command+args 组合
    existing_signatures = set()
    for s in existing:
        cmd = s.get("command", "")
        args = tuple(s.get("args", []) or [])
        if cmd:
            existing_signatures.add((cmd, args))

    new_ones = []
    for srv in discovered:
        cmd = srv.get("command", "")
        args = tuple(srv.get("args", []) or [])
        if cmd and (cmd, args) not in existing_signatures:
            new_ones.append(srv)
            existing_signatures.add((cmd, args))  # 防止多个新发现重复

    # name 冲突处理
    existing_names = {s.get("name", "") for s in existing}

    def unique_name(name: str, suffix: str) -> str:
        if name not in existing_names:
            return name
        base = name
        idx = 1
        while f"{base}{suffix}{idx}" in existing_names:
            idx += 1
        return f"{base}{suffix}{idx}"

    for srv in new_ones:
        srv["name"] = unique_name(srv["name"], "_copy")

    merged = list(existing) + new_ones
    return merged, new_ones


def discover_and_merge() -> Tuple[List[dict], List[dict]]:
    """
    自动发现所有已知来源的 MCP 服务器

    发现结果由 backend._discover_mcp_servers() 写入 user-custom 插件，
    不再直接修改 Settings.mcp_servers。

    Returns:
        (all_servers, newly_discovered) — 所有服务器（含已有的）+ 新发现的列表
    """
    from app.plugins.managers.plugin_manager import PluginManager

    pm = PluginManager.get_instance()
    existing = pm.get_mcp_servers() if pm.is_initialized() else []

    all_discovered = []
    all_discovered.extend(_discover_claude_desktop_servers())
    all_discovered.extend(_discover_cursor_servers())

    merged, new_ones = _merge_and_deduplicate(existing, all_discovered)

    if new_ones:
        logger.info(f"[MCP] 自动发现 {len(new_ones)} 个新服务器")

    return merged, new_ones
