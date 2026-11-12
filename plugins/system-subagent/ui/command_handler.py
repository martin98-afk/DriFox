# -*- coding: utf-8 -*-
"""/subagents 命令的执行体（插件侧实现）

命令定义在 ``commands/subagents.md``（frontmatter 提供描述/参数/快捷键与
``--create=`` 的 prompt 注入段），本模块只提供 ``type: function`` 对应的
Python handler：

- 无参数 → 把运行中的任务补进消息卡内嵌运行面板并展开
- ``--create=X`` → 抛 ``CommandNeedDegrade`` 交给 prompt 注入流程

默认模型改由插件设置卡（``config_schema`` 的 ``subagent_model`` 字段）维护，
命令不再提供 ``--model=`` / ``--reset``；旧全局值在注册时一次性迁移。

handler 注册进全局 ``FunctionCommandHandlers``：命令元数据仍由 md 提供，主程序
不再持有任何 subagent 业务分支。
"""

from typing import Any, Optional, Tuple

from loguru import logger

COMMAND_NAME = "subagents"
_PANEL_ID = "subagent_run"
_PLUGIN_NAME = "system-subagent"


def _context() -> Tuple[Optional[Any], Optional[Any]]:
    """取当前活跃窗口与插件能力池（无窗口返回 (None, None)）。"""
    try:
        from app.plugins.capabilities.resolve import active_main_widget, pool_for

        mw = active_main_widget()
        if mw is None:
            return None, None
        return mw, pool_for(_PLUGIN_NAME, surface="ui", host=mw)
    except Exception as e:  # noqa: BLE001 - 命令链异常不打断输入
        logger.warning(f"[system-subagent] /{COMMAND_NAME} 上下文解析失败: {e}")
        return None, None


def _notify(pool: Any, title: str, message: str, kind: str = "success", duration: int = 3000) -> None:
    if pool is None or "ui.notify" not in pool:
        logger.info(f"[system-subagent] {title}: {message}")
        return
    try:
        pool["ui.notify"](title, message, kind, duration)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[system-subagent] 通知失败: {e}")


def _subagent_facade(mw: Any) -> Optional[Any]:
    """取该窗口的子智能体服务门面（插件未加载返回 None）。"""
    try:
        from app.plugins.registries.window_services import get_window_services

        return get_window_services().get("subagent", str(getattr(mw, "_window_id", "") or ""))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[system-subagent] 服务门面不可用: {e}")
        return None


def _handle_show_panel(mw: Any, pool: Any) -> None:
    facade = _subagent_facade(mw)
    manager = getattr(facade, "manager", None) if facade is not None else None
    running = dict(getattr(manager, "_running_tasks", None) or {})
    if not running:
        _notify(pool, "暂无运行中的子智能体", "当前没有正在执行的子智能体任务", "warning")
        return
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        panel = UIPluginRegistry.get_instance().attach_card_inline_panel(_PANEL_ID, main_widget=mw)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[system-subagent] 运行面板不可用: {e}")
        panel = None
    if panel is None:
        _notify(pool, "无法展开运行面板", "当前没有可用的消息卡片", "warning")
        return
    for task_id, executor in running.items():
        if panel.has_task(task_id):
            continue
        llm_cfg = getattr(executor, "llm_config", {}) or {}
        model_name = str(llm_cfg.get("模型名称", "") or llm_cfg.get("model", "") or "")
        panel.add_task(
            task_id,
            getattr(executor, "agent_name", ""),
            getattr(executor, "task_description", ""),
            model_name=model_name,
        )


def handle_command(args: str) -> None:
    """``/subagents`` 的 function 处理器（签名与 FunctionCommandHandlers 一致）。"""
    text = (args or "").strip()

    # --create= 走 prompt 注入：抛降级异常，由命令层按 prompt_sections 匹配
    if text.startswith("--create"):
        from app.core.commands.command_manager import CommandNeedDegrade

        raise CommandNeedDegrade(COMMAND_NAME, text)

    mw, pool = _context()
    if mw is None:
        logger.warning(f"[system-subagent] /{COMMAND_NAME} 无活跃窗口，跳过")
        return
    if pool is None:
        _notify(None, "子智能体插件不可用", "能力池构建失败，详见日志", "warning")
        return

    _handle_show_panel(mw, pool)


def register_commands() -> None:
    """把 handler 落到全局表（register_all_commands 重建命令表后仍然有效）。"""
    from app.core.commands.builtin_commands import FunctionCommandHandlers

    FunctionCommandHandlers.register(COMMAND_NAME, handle_command)
    _migrate_legacy_default_model()


def _config_pool() -> Any:
    """无窗口 ctx 时取本插件 ui 能力池（迁移旧默认模型用）。"""
    try:
        from app.plugins.capabilities.resolve import pool_for

        return pool_for(_PLUGIN_NAME, surface="ui")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[system-subagent] 配置能力池不可用: {e}")
        return None


def _migrate_legacy_default_model() -> None:
    """一次性把旧全局配置搬进插件数据目录（值格式一致，无需转换）。"""
    pool = _config_pool()
    needed = ("plugin.config.read", "plugin.config.write", "config.read", "config.write")
    if pool is None or not all(cap in pool for cap in needed):
        return
    try:
        if str(pool["plugin.config.read"]("subagent_model", "") or "").strip():
            return
        legacy = str(pool["config.read"]("llm_subagent_default_model", "") or "").strip()
        if not legacy:
            return
        if not bool(pool["plugin.config.write"]("subagent_model", legacy)):
            return
        pool["config.write"]("llm_subagent_default_model", "")
        logger.info("[system-subagent] 默认模型已迁移到插件设置")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[system-subagent] 默认模型迁移跳过: {e}")


def unregister_commands() -> None:
    from app.core.commands.builtin_commands import FunctionCommandHandlers

    FunctionCommandHandlers.unregister(COMMAND_NAME)
