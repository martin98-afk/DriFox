# -*- coding: utf-8 -*-
"""system-subagent UI 组件入口

三块载体 + 一份宿主运行时：

1. **窗口宿主**：``SubAgentHost`` 为每个窗口建管理器并注册到 ``WindowServiceRegistry``
   （key = ``subagent``），主程序与工具侧按 window_id 取用——子智能体字段不再落在主程序。
2. **运行区**：``register_card_inline_panel`` 注册消息卡内嵌运行面板，由
   ``SubAgentRunBridge`` 把该窗口管理器的事件投影到最新 assistant 卡片上（迁卡语义同任务看板）。
3. **会话卡**：``register_floating_card(container="full")`` 注册子智能体会话面板，
   数据经 ``SubAgentSessionService`` 灌入（主程序只调服务，不持有卡片实例）。

模块级 ``_host`` / ``_bridge`` 必须活到插件卸载：它们持有管理器与信号连接。
"""

from loguru import logger

from .host import SubAgentHost
from .run_bridge import PANEL_ID, SubAgentRunBridge
from .run_panel import SubAgentRunPanel
from .session_card import SubAgentSessionCard
from .session_service import CARD_ID, SERVICE_NAME, SubAgentSessionService

_PLUGIN_NAME = "system-subagent"

__all__ = ["SubAgentHost", "SubAgentRunBridge", "SubAgentRunPanel", "SubAgentSessionCard", "register_ui", "unload_ui"]

_host = None
_bridge = None


def register_ui(registry) -> None:
    """窗口宿主 + 内嵌运行面板 + 会话卡浮动卡 + 会话服务。"""
    global _host, _bridge
    try:
        registry.register_card_inline_panel(
            _PLUGIN_NAME,
            PANEL_ID,
            SubAgentRunPanel,
            order=110,
            metadata={"title": "子智能体", "role": "assistant"},
        )
    except Exception as e:  # noqa: BLE001 - 注册失败不拖垮宿主
        logger.warning(f"[system-subagent] 运行面板注册失败: {e}")
        return
    try:
        registry.register_floating_card(
            plugin_name=_PLUGIN_NAME,
            card_id=CARD_ID,
            widget_class=SubAgentSessionCard,
            container="full",
            title="子智能体会话",
            default_visible=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[system-subagent] 会话卡注册失败: {e}")
    try:
        registry.register_service(SERVICE_NAME, SubAgentSessionService(registry), plugin_name=_PLUGIN_NAME)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[system-subagent] 会话服务注册失败: {e}")
    try:
        _host = SubAgentHost(registry)
        _host.start()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[system-subagent] 窗口宿主启动失败，子智能体不可用: {e}")
        _host = None
    try:
        _bridge = SubAgentRunBridge(registry, _host)
        _bridge.start()
        logger.info("[system-subagent] 运行面板与会话卡已注册，窗口宿主已启动")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[system-subagent] 运行面板桥接启动失败: {e}")
        _bridge = None
    try:
        from .command_handler import register_commands

        register_commands()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[system-subagent] /subagents 命令注册失败: {e}")


def unload_ui(registry) -> None:
    """停心跳、停宿主（取消任务 + 摘服务）、注销命令处理器、摘除面板。"""
    global _host, _bridge
    try:
        from .command_handler import unregister_commands

        unregister_commands()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[system-subagent] 命令注销失败: {e}")
    if _bridge is not None:
        try:
            _bridge.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 桥接停止失败: {e}")
        _bridge.deleteLater()
        _bridge = None
    if _host is not None:
        try:
            _host.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-subagent] 窗口宿主停止失败: {e}")
        _host.deleteLater()
        _host = None
    logger.info("[system-subagent] 运行面板已卸载")
