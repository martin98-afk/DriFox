# -*- coding: utf-8 -*-
"""worktree-manager 插件 UI 注册入口

职责：
- 注册左侧边栏「工作树」卡片
- 注册 WorktreeService（主程序门面查询：会话联动 + 分支标签）
- 注册标题栏分支标签 widget 工厂（slot="branch"）
"""
from loguru import logger

from .service import WorktreeService

_PLUGIN_NAME = "worktree-manager"

__all__ = ["WorktreeService", "register_ui"]


def register_ui(registry) -> None:
    try:
        from .worktree_page import SystemWorktreePage

        registry.register_floating_card(
            plugin_name=_PLUGIN_NAME,
            card_id=_PLUGIN_NAME,
            title="工作树",
            widget_class=SystemWorktreePage,
            container="left",
        )
        logger.info("[worktree-manager] 已注册左侧工作树卡片")
    except Exception as e:
        logger.warning(f"[worktree-manager] 注册工作树卡片失败: {e}")
    try:
        # 服务：主程序 5 个门面方法的实现体
        service = WorktreeService()
        registry.register_service(WorktreeService.SERVICE_NAME, service, plugin_name=_PLUGIN_NAME)
        # 工具侧切换请求通道：manage_worktree(switch) 经 bridge 投递到主线程执行
        from .service import _install_switch_receiver

        _install_switch_receiver(service)
        # 标题栏分支标签：窗口 build 时经 get_titlebar_widget("branch") 装配
        from .branch_chip import BranchChip

        registry.register_titlebar_widget(
            plugin_name=_PLUGIN_NAME,
            slot="branch",
            widget_factory=lambda host: BranchChip(text="main", parent=host),
            priority=20,
        )
        logger.info("[worktree-manager] 已注册 WorktreeService + 标题栏分支标签")
    except Exception as e:
        logger.warning(f"[worktree-manager] 注册服务/分支标签失败（降级为无分支标签）: {e}")
