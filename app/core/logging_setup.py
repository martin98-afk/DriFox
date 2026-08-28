# -*- coding: utf-8 -*-
"""集中式日志配置：按子系统拆分日志文件。

设计要点：
- 业务代码零改动：全局继续 ``from loguru import logger``，仅在配置层做多 sink 路由。
- 路由依据调用点模块名（``record["name"]``，即 ``__name__``）做路径段前缀匹配，
  先具体后模糊；一条日志可同时写入 ``all.log``（全量）与命中的分文件。
- 未命中任何分文件的日志（含第三方库）仅写 ``all.log``，保证无遗漏。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

# 日志格式与历史版本保持一致
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"

# 分文件路由表：(文件名, 包含的模块名前缀, 排除的模块名前缀)。
# 排除优先于包含（例：app.tools 命中 tools.log，但 app.tools.mcp_tools 归 mcp.log）。
# 排查场景见各条目注释；后续增删子系统只需调整本表。
LOG_ROUTES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    # 平台消息收发（QQ/微信/钉钉等网关）
    ("gateway.log", ("app.gateway", "app.core.gateway_service"), ()),
    # MCP 连接与工具调用
    ("mcp.log", ("app.tools.mcp_tools",), ()),
    # 语言服务（LSP）
    ("lsp.log", ("app.core.lsp",), ()),
    # 团队协作与子智能体
    ("team.log", ("app.core.team", "app.core.team_manager", "app.core.agent"), ()),
    # 会话/记忆/用量存储
    ("store.log", ("app.core.store", "app.core.usage_service"), ()),
    # 工具框架与执行（mcp_tools 除外）
    (
        "tools.log",
        (
            "app.tools",
            "app.core.tool_executor",
            "app.core.tool_permission_controller",
            "app.core.tool_result_persister",
            "app.core.tool_call_parser",
        ),
        ("app.tools.mcp_tools",),
    ),
    # 插件加载/热更/插件宿主/系统插件代码
    ("plugins.log", ("app.plugins", "app.core.plugin_host_service", "plugins"), ()),
    # UI 组件与窗口
    (
        "ui.log",
        (
            "app.widgets",
            "app.main_widget",
            "app.tray_manager",
            "app.tool_popup",
            "app.update_checker",
            "app.core.ui_event_bus",
            "app.core.webengine_profile",
            "app.core.window_registry",
        ),
        (),
    ),
    # 对话/模型请求（core 中未单独拆分的对话管线其余部分）
    (
        "llm.log",
        (
            "app.core.backend",
            "app.core.chat_session",
            "app.core.workers",
            "app.core.conversation",
            "app.core.engines",
            "app.core.context_builder",
            "app.core.context_usage",
            "app.core.hook_manager",
            "app.core.history_compactor",
            "app.core.memory_manager",
            "app.core.token_estimator",
            "app.core.models_dev_sync",
            "app.core.message_content",
            "app.core.model_capabilities",
            "app.core.provider_profile",
            "app.core.builtin_commands",
            "app.core.command_manager",
        ),
        (),
    ),
]


def _match_prefix(name: str, prefixes: tuple[str, ...]) -> bool:
    """按路径段匹配模块名前缀：精确相等，或以 ``prefix + "."`` 开头。

    避免子串误匹配（如 ``app.core.gateway_service`` 不应命中 ``app.core.gateway``）。
    """
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def make_module_filter(include: tuple[str, ...], exclude: tuple[str, ...] = ()) -> Callable[[Any], bool]:
    """构造按模块名路由的 loguru filter（排除优先于包含）。"""

    def _filter(record: Any) -> bool:
        name = record["name"]
        if _match_prefix(name, exclude):
            return False
        return _match_prefix(name, include)

    return _filter


def setup_logging(log_dir: Path, mem_diag_enabled: bool = False) -> None:
    """配置全量日志 + 分系统日志 + 内存诊断日志。

    - ``all.log``：全量兜底，10 MB 轮转并保留历史，供跨系统时间线排查。
    - 分文件：每日 0 点轮转，仅保留近一天，磁盘上基本只留当天。
    - ``mem_diag.log``：内存诊断（消息含 ``[MEM]``），受 ``mem_diag_enabled`` 控制。
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    # 全量日志（兜底）
    logger.add(
        log_dir / "all.log",
        rotation="10 MB",
        level="DEBUG",
        format=LOG_FORMAT,
    )

    # 分系统日志
    for file_name, include, exclude in LOG_ROUTES:
        logger.add(
            log_dir / file_name,
            rotation="00:00",
            retention="1 day",
            level="DEBUG",
            format=LOG_FORMAT,
            filter=make_module_filter(include, exclude),
        )

    # 单独的内存诊断日志文件
    if mem_diag_enabled:
        logger.add(
            log_dir / "mem_diag.log",
            rotation="10 MB",
            level="DEBUG",
            format=LOG_FORMAT,
            filter=lambda r: "[MEM]" in r["message"],
        )
