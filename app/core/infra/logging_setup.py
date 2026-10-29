# -*- coding: utf-8 -*-
"""集中式日志配置：按子系统拆分日志文件。

设计要点：
- 业务代码零改动：全局继续 ``from loguru import logger``，仅在配置层做多 sink 路由。
- 路由依据调用点模块名（``record["name"]``，即 ``__name__``）做路径段前缀匹配，
  先具体后模糊；一条日志可同时写入 ``all.log``（全量）与命中的分文件。
- 未命中任何分文件的日志（含第三方库）仅写 ``all.log``，保证无遗漏。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Callable

from loguru import logger

# 日志格式与历史版本保持一致
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"

# 分文件路由表：(文件名, 包含的模块名前缀, 排除的模块名前缀)。
# 排除优先于包含（例：app.tools 命中 tools.log，但 app.tools.mcp_tools 归 mcp.log）。
# 排查场景见各条目注释；后续增删子系统只需调整本表。
LOG_ROUTES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    # 平台消息收发（QQ/微信/钉钉等网关）
    ("gateway.log", ("app.gateway", "app.core.services.gateway_service"), ()),
    # MCP 连接与工具调用
    ("mcp.log", ("app.tools.mcp_tools",), ()),
    # 语言服务（LSP）
    ("lsp.log", ("app.core.lsp",), ()),
    # 团队协作与子智能体
    ("team.log", ("app.core.team", "app.core.team.team_manager", "app.core.conversation.agent"), ()),
    # 会话/记忆/用量存储
    ("store.log", ("app.core.store", "app.core.infra.usage_service"), ()),
    # 工具框架与执行（mcp_tools 除外）
    (
        "tools.log",
        (
            "app.tools",
            "app.core.tools.tool_executor",
            "app.core.tools.tool_permission_controller",
            "app.core.tools.tool_result_persister",
            "app.core.tools.tool_call_parser",
        ),
        ("app.tools.mcp_tools",),
    ),
    # 插件加载/热更/插件宿主/系统插件代码
    ("plugins.log", ("app.plugins", "app.core.services.plugin_host_service", "plugins"), ()),
    # UI 组件与窗口
    (
        "ui.log",
        (
            "app.widgets",
            "app.main_widget",
            "app.tray_manager",
            "app.update_checker",
            "app.core.infra.ui_event_bus",
            "app.core.infra.webengine_profile",
            "app.core.infra.window_registry",
        ),
        (),
    ),
    # 对话/模型请求（core 中未单独拆分的对话管线其余部分）
    (
        "llm.log",
        (
            "app.core.conversation.backend",
            "app.core.conversation.chat_session",
            "app.core.workers",
            "app.core.conversation",
            "app.core.engines",
            "app.core.context.builder",
            "app.core.context.usage",
            "app.core.hooks.hook_manager",
            "app.core.conversation.memory_manager",
            "app.core.infra.token_estimator",
            "app.core.modelmeta.models_dev_sync",
            "app.core.conversation.message_content",
            "app.core.modelmeta.model_capabilities",
            "app.core.modelmeta.provider_profile",
            "app.core.commands.builtin_commands",
            "app.core.commands.command_manager",
        ),
        (),
    ),
]


def _match_prefix(name: str, prefixes: tuple[str, ...]) -> bool:
    """按路径段匹配模块名前缀：精确相等，或以 ``prefix + "."`` 开头。

    避免子串误匹配（如 ``app.core.services.gateway_service`` 不应命中 ``app.core.gateway``）。
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


def _install_stdlib_logging_bridge() -> None:
    """把标准 ``logging`` 的记录接管进 loguru（幂等）。

    背景：本模块给 loguru 配 sink，但标准 logging 是独立体系 —— 第三方库
    （lark_oapi / openai / httpx / aiohttp / watchfiles 等）与部分插件用
    ``logging.getLogger(__name__)`` 输出，这些记录**一条都不落盘**。实证代价：
    飞书 WS 建连失败时 lark SDK 的 ``connect failed, err: [SSL:
    CERTIFICATE_VERIFY_FAILED] ...`` 完全不可见，只能靠盲猜定位（当时误判两轮）。

    做法：把 root logger 的 handler 换成转发器，逐条转投 loguru。
    - 保留 ``record.name`` 作 loguru 的 ``name`` → 命中 LOG_ROUTES 分文件路由
      （如 ``app.tools.mcp_tools`` 仍进 mcp.log）；不命中任何前缀的第三方库
      日志落 all.log，符合本模块「无遗漏」设计目标。
    - 保留原始 levelname 与调用点（filename/lineno），便于溯源。
    - ``logging`` 侧不设级别上限（level=0 / setLevel(NOTSET)），由 loguru sink
      的 level="DEBUG" 统一过滤，避免两套阈值不一致。

    幂等：重复调用只安装一次（标记挂在 root logger 上）。
    """
    import logging

    root = logging.getLogger()
    if getattr(root, "_drifox_loguru_bridge", False):
        return

    class _LoguruForwarder(logging.Handler):
        """标准 logging → loguru 转发器（保留模块名/级别/调用点）。"""

        def emit(self, record: logging.LogRecord) -> None:
            try:
                # 标准级别名 → loguru 级别名（数值语义一致，用名字更直观）
                level = record.levelname
                if level not in ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                    level = "INFO"
                # 用 patch 显式把原 logger 名写进 loguru 记录的 name 字段：
                # 这样 LOG_ROUTES 的前缀匹配（app.tools / plugins 等）对插件与
                # 第三方库同样生效；不命中任何前缀的记录落 all.log（本模块
                # 「无遗漏」设计目标）。若不 patch，name 会是本模块名。
                logger.patch(lambda r: r.update(name=record.name)).opt(exception=record.exc_info).log(
                    level, record.getMessage()
                )
            except Exception:
                pass  # 日志链路自身异常绝不能影响业务

    handler = _LoguruForwarder()
    handler.setLevel(0)
    # 替换既有 handler（basicConfig(force=True) 语义）：默认 StreamHandler 会
    # 往 stderr 重复输出一份，且我们已全量接管，无需保留
    root.handlers = [handler]
    # 关键：root 保持 WARNING，**不设 0**。
    # 原因：未显式设级别的库（urllib3 / httpcore / httpx / asyncio / aiohttp /
    # watchfiles …）其 getEffectiveLevel() 继承 root。若 root 设 0，这些库的
    # 有效级别变成 NOTSET → DEBUG 全放行，all.log 会被连接池噪音淹没（实测
    # 100 条噪音全量落盘）。保持 WARNING 则维持库的原生默认（只出 WARNING+）。
    # 需要 DEBUG 的库/插件自己 setLevel，照常生效——正是我们要收的目标
    # （lark SDK 内部设 DEBUG、插件 setLevel 后同样透传）。
    root.setLevel(logging.WARNING)
    root._drifox_loguru_bridge = True  # type: ignore[attr-defined]


def setup_logging(log_dir: Path, mem_diag_enabled: bool = False) -> None:
    """配置全量日志 + 分系统日志 + 内存诊断日志。

    - ``all.log``：全量兜底，10 MB 轮转并保留历史，供跨系统时间线排查。
    - 分文件：每日 0 点轮转，仅保留近七天，方便跨周回溯崩溃前的日志链。
    - ``mem_diag.log``：内存诊断（消息含 ``[MEM]``），受 ``mem_diag_enabled`` 控制。
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    # 标准 logging 接管（第三方库/插件日志 → loguru，链路见函数注释）。
    # 必须在加 sink 前安装：转发器只做转投，落盘由下面的 sink 决定。
    _install_stdlib_logging_bridge()

    # 逐个 sink 独立安装：单个 sink 失败不能连带其余全丢。
    # 2026-10-28 实测：POSIX semaphore 耗尽（Errno 28）时首个 enqueue=True 的
    # logger.add 直接抛错，此前顺序安装会让 11 个 sink 全部没装 → 应用能跑但
    # 零日志、事后无任何证据。失败原因写 stderr（此时日志链路本身不可信）。
    #
    # 降级策略：enqueue=True 依赖 multiprocessing 的 SimpleQueue/Event/Lock，
    # 每个都要一个 POSIX semaphore；enqueue=False 只用线程锁，不受该配额影响
    # （同一耗尽环境下实测：前者 Errno 28，后者正常写入）。所以失败时退回同步
    # 写盘 —— 牺牲吞吐换「日志永远有」，这类环境本就是排查期，日志比性能重要。
    _failed: list[str] = []
    _degraded: list[str] = []

    def _add_sink(name: str, **kwargs: Any) -> None:
        try:
            logger.add(**kwargs)
            return
        except Exception as exc:
            if not kwargs.get("enqueue"):
                _failed.append(name)
                sys.stderr.write(f"[logging_setup] sink 安装失败 {name}: {exc}\n")
                return
        # 走到这里说明 enqueue=True 不可用，退回同步 sink
        kwargs["enqueue"] = False
        try:
            logger.add(**kwargs)
            _degraded.append(name)
            sys.stderr.write(f"[logging_setup] sink 降级为同步写入 {name}（enqueue 不可用）\n")
        except Exception as exc:  # noqa: BLE001
            _failed.append(name)
            sys.stderr.write(f"[logging_setup] sink 安装失败 {name}: {exc}\n")

    # 全量日志（兜底）
    # enqueue=True：写盘转 loguru 内部后台线程，调用线程只格式化入队。
    # 背景：主线程槽内高频 logger.info（工具结果/流式节拍）同步写 all.log，
    # DEBUG 全量 + 10MB 轮转下每条都摊文件 IO，累积成可感卡顿。
    _add_sink(
        "all.log",
        sink=log_dir / "all.log",
        rotation="10 MB",
        level="DEBUG",
        format=LOG_FORMAT,
        enqueue=True,
    )

    # 分系统日志（enqueue=True 同 all.log：主线程高频写不再同步落盘）
    for file_name, include, exclude in LOG_ROUTES:
        _add_sink(
            file_name,
            sink=log_dir / file_name,
            rotation="00:00",
            retention="7 days",
            level="DEBUG",
            format=LOG_FORMAT,
            filter=make_module_filter(include, exclude),
            enqueue=True,
        )

    # 单独的内存诊断日志文件
    if mem_diag_enabled:
        _add_sink(
            "mem_diag.log",
            sink=log_dir / "mem_diag.log",
            rotation="10 MB",
            level="DEBUG",
            format=LOG_FORMAT,
            filter=lambda r: "[MEM]" in r["message"],
            enqueue=True,
        )

    if _degraded:
        sys.stderr.write(f"[logging_setup] {len(_degraded)} 个 sink 降级为同步: {', '.join(_degraded)}\n")
    if _failed:
        sys.stderr.write(f"[logging_setup] {len(_failed)} 个 sink 未安装: {', '.join(_failed)}\n")
