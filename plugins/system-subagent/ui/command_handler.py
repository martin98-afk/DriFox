# -*- coding: utf-8 -*-
"""/subagents 命令的执行体（插件侧实现）

命令定义在 ``commands/subagents.md``（frontmatter 提供描述/参数/快捷键与
``--create=`` 的 prompt 注入段），本模块只提供 ``type: function`` 对应的
Python handler：

- 无参数 → 把运行中的任务补进消息卡内嵌运行面板并展开
- ``--model=X`` → 解析三种格式（模型名 / 服务商 / 服务商:模型名）后写默认模型
- ``--reset`` → 清空默认模型
- ``--create=X`` → 抛 ``CommandNeedDegrade`` 交给 prompt 注入流程

handler 注册进全局 ``FunctionCommandHandlers``：命令元数据仍由 md 提供，主程序
不再持有任何 subagent 业务分支。
"""

import re
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


def _handle_reset(pool: Any) -> None:
    ok = bool(pool["config.write"]("llm_subagent_default_model", ""))
    if ok:
        _notify(pool, "已重置", "子智能体默认模型已清空，将使用主智能体模型")
    else:
        _notify(pool, "重置失败", "写入默认模型配置失败，详见日志", "warning")


def _handle_model(pool: Any, model_value: str) -> None:
    if not model_value:
        _notify(pool, "参数错误", "--model= 后需要指定模型名或服务商:模型名", "warning")
        return
    llm_config = pool["model.config_for"](model_value) if "model.config_for" in pool else None
    if not llm_config:
        # 具体原因（未知服务商/模型不存在）由宿主解析器日志给出，这里只回通用提示
        _notify(pool, "无法解析模型", f"未找到可用的模型配置: {model_value}", "warning")
        return
    provider_display = llm_config.get("display_name") or llm_config.get("provider_name") or ""
    model_display = llm_config.get("模型名称") or model_value
    display_value = f"{provider_display}:{model_display}" if provider_display else model_display
    if not bool(pool["config.write"]("llm_subagent_default_model", display_value)):
        _notify(pool, "设置失败", "写入默认模型配置失败，详见日志", "warning")
        return
    _notify(pool, "子智能体模型设置", display_value)


def _handle_show_panel(mw: Any, pool: Any) -> None:
    manager = pool["subagent.run"] if (pool is not None and "subagent.run" in pool) else None
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

    if text == "--reset":
        _handle_reset(pool)
        return

    model_match = re.match(r"^--model=(.+)$", text)
    if model_match:
        _handle_model(pool, model_match.group(1).strip().strip("\"'"))
        return

    _handle_show_panel(mw, pool)


def register_commands() -> None:
    """把 handler 落到全局表（register_all_commands 重建命令表后仍然有效）。"""
    from app.core.commands.builtin_commands import FunctionCommandHandlers

    FunctionCommandHandlers.register(COMMAND_NAME, handle_command)


def unregister_commands() -> None:
    from app.core.commands.builtin_commands import FunctionCommandHandlers

    FunctionCommandHandlers.unregister(COMMAND_NAME)
