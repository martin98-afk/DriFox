# -*- coding: utf-8 -*-
"""样例·语音识别 插件 UI 组件入口 —— 仅充当命令 handler 的注册入口（无任何界面载体）。

DriFox 命令组件架构：``commands/*.md`` 只提供命令元数据（进 CommandManager），
``type: function`` 命令的 Python 执行体必须由插件在加载期经 ``register_ui``
中的 ``register_ui_command`` 登记（插件侧唯一入口，先例 system-subagent）。
宿主卸载/热重载会按 owner 自动注销账本，``unload_ui`` 里再做一次幂等兜底。
"""

from .command_handler import COMMAND_DESCRIPTION, COMMAND_NAME, handle_command

__all__ = ["register_ui", "unload_ui"]

_PLUGIN_NAME = "sample-asr"


def register_ui(registry) -> None:
    """登记 /sample_asr 的 function 处理器（命令元数据由 commands/sample_asr.md 提供）。"""
    registry.register_ui_command(
        COMMAND_NAME,
        COMMAND_DESCRIPTION,
        handle_command,
        owner=_PLUGIN_NAME,
    )


def unload_ui(registry) -> None:
    """摘除命令 handler（账本 + CommandManager + 处理器三处同步，幂等）。"""
    registry.unregister_ui_command(COMMAND_NAME)
