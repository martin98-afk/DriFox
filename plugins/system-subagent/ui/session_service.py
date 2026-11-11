# -*- coding: utf-8 -*-
"""子智能体会话卡的数据门面

主程序需要一个「带参数显示某任务会话」的入口，而浮动卡的创建/显隐归注册表管，
卡片本身不感知调用方。本服务把两者接起来：灌数据 + 确保卡可见。主程序只调
``show(task_id, ...)``，不再自己持有卡片实例或碰 CardManager。
"""

from typing import Any, Callable, Dict, List, Optional

from loguru import logger

CARD_ID = "subagent_session"
SERVICE_NAME = "subagent_session"


class SubAgentSessionService:
    """子智能体会话卡服务（经 UIPluginRegistry.register_service 暴露）。"""

    SERVICE_NAME = SERVICE_NAME

    def __init__(self, registry: Any):
        self._registry = registry

    def show(
        self,
        task_id: str,
        agent_name: str,
        logs: List[Dict[str, Any]],
        summary: Optional[Dict[str, Any]] = None,
        logs_provider: Optional[Callable[[], Dict]] = None,
    ) -> bool:
        """显示该任务的会话卡并灌入数据。

        Returns:
            True 已显示；False 卡片不可用（浮动卡未注册 / 宿主缺失）。
        """
        widget = self._registry.get_floating_card_widget(CARD_ID)
        # 未创建或已被隐藏才触发显示（toggle 语义会反转已可见的卡）
        if widget is None or not self._is_visible(widget):
            self._registry.toggle_floating_card(CARD_ID)
            widget = self._registry.get_floating_card_widget(CARD_ID)
        if widget is None:
            logger.warning("[system-subagent] 会话卡不可用（浮动卡未创建）")
            return False
        try:
            widget.load(task_id, agent_name, logs, summary or {}, logs_provider)
        except Exception as e:  # noqa: BLE001 - 卡片渲染失败不拖垮调用方
            logger.warning(f"[system-subagent] 会话卡加载失败: {e}")
            return False
        return True

    def hide(self) -> None:
        """隐藏会话卡（主程序不再直接操作 CardManager）。"""
        self._registry.hide_floating_card_globally(CARD_ID)

    @staticmethod
    def _is_visible(widget: Any) -> bool:
        try:
            return bool(widget.isVisible())
        except RuntimeError:
            return False
