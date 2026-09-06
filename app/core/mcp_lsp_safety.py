# -*- coding: utf-8 -*-
"""P1-3：MCP/LSP 子进程启动安全门禁（审计 + shell 元字符拒启 + 非内置源确认流）。

三态状态机：
- proceed：放行（系统插件源 / 非内置但已在确认白名单）
- need_confirm：非内置源首次启动，等待用户确认（确认前不启动）
- denied：shell 元字符命中拒启（warning + 原因），或用户本次会话已拒绝

确认记录存 Settings.confirmed_plugin_servers（["mcp:<plugin>:<server>", ...]）；
用户点拒绝（confirm_plugin_server(allow=False)）→ 会话禁用集合，不写 Settings。

来源判定：source 路径解析后位于系统插件根（项目 plugins/）下即内置；
未知来源 / 特殊标识（claude_desktop / cursor 等外部导入）按非内置处理。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from loguru import logger

# args/command 任一元素命中即拒启（正经 server args 不需要 shell 元字符）
_SHELL_METACHAR_RE = re.compile(r"[;&|`]|\$\(")

# 会话级拒绝（用户点拒绝后本会话内不再提示/启动，不落盘）
_SESSION_DENIED: set = set()

_SYSTEM_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins"


def check_args_safety(args) -> Tuple[bool, str]:
    """args 任一元素含 shell 元字符（``; & | ` $(``）→ 拒。

    Returns:
        (是否安全, 首个违规元素)
    """
    for item in (args or []):
        text = str(item)
        if text and _SHELL_METACHAR_RE.search(text):
            return False, text
    return True, ""


def is_builtin_source(source) -> bool:
    """来源是否为系统插件根（项目 plugins/）。未知来源按非内置处理。"""
    if not source:
        return False
    try:
        return Path(source).resolve().is_relative_to(_SYSTEM_PLUGIN_ROOT)
    except (OSError, ValueError):
        return False


def gate_server_launch(
    kind: str,
    plugin: str,
    server: str,
    args: Optional[List[str]],
    source=None,
) -> str:
    """启动门禁三态判定 + 审计日志。

    Args:
        kind: "mcp" | "lsp"
        plugin: 插件名（未知传空串，会尝试从 source 路径推导）
        server: server 名
        args: 启动参数列表
        source: 配置来源路径（.mcp.json / .lsp.json），用于内置判定与插件名推导

    Returns:
        "proceed" | "need_confirm" | "denied"
    """
    tag = "MCPAudit" if kind == "mcp" else "LSPAudit"
    args = [str(a) for a in (args or [])]

    # 1) shell 元字符拒启
    safe, bad = check_args_safety(args)
    if not safe:
        logger.warning(
            f"[{tag}] plugin={plugin or '?'} server={server} 拒绝启动："
            f"args 含 shell 元字符（{bad!r}）——正经 server args 不需要这些字符"
        )
        return "denied"

    # 2) 审计日志（启动放行路径统一记录：插件名+server+args 摘要）
    summary = " ".join(args)[:120]
    builtin = is_builtin_source(source)
    key = f"{kind}:{plugin or '-'}:{server}"

    # 3) 非内置源确认流
    if not builtin:
        confirmed: list = []
        try:
            from app.utils.config import Settings

            confirmed = Settings.get_instance().confirmed_plugin_servers.value or []
        except Exception:
            pass
        if key in confirmed:
            logger.warning(
                f"[{tag}] plugin={plugin or '?'} server={server} 已确认放行 args=[{summary}]"
            )
            return "proceed"
        if key in _SESSION_DENIED:
            logger.warning(
                f"[{tag}] plugin={plugin or '?'} server={server} 本次会话已被用户拒绝，跳过启动"
            )
            return "denied"
        logger.warning(
            f"[{tag}] plugin={plugin or '?'} server={server} 非内置源首次启动，需用户确认 "
            f"(source={source}; args=[{summary}]；确认后调 confirm_plugin_server 白名单化)"
        )
        return "need_confirm"

    logger.warning(f"[{tag}] plugin={plugin or '?'} server={server} args=[{summary}]")
    return "proceed"


def confirm_plugin_server(kind: str, plugin: str, server: str, allow: bool = True) -> None:
    """用户确认结果：allow=True 写入永久白名单；False 加入本次会话禁用集（不落盘）。"""
    key = f"{kind}:{plugin or '-'}:{server}"
    if allow:
        try:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            confirmed = list(cfg.confirmed_plugin_servers.value or [])
            if key not in confirmed:
                confirmed.append(key)
                cfg.set(cfg.confirmed_plugin_servers, confirmed, save=True)
        except Exception as e:
            logger.warning(f"[mcp_lsp_safety] 写入确认记录失败: {e}")
    else:
        _SESSION_DENIED.add(key)
