# -*- coding: utf-8 -*-
"""worktree-manager 插件 UI 注册入口

职责：
- 注册工作台「工作树」页（page_id="worktree-manager"，与插件同名，默认落点）
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

        # 工作树页：page_id 与插件同名，order_hint=0 → 页签首位 + 默认落点
        registry.register_workbench_tab(
            plugin_name=_PLUGIN_NAME,
            page_id="worktree-manager",
            label="工作树",
            widget_class=SystemWorktreePage,
            priority=20,
            metadata={"source": "system", "order_hint": 0, "default_landing": True},
        )
        logger.info("[worktree-manager] 已注册工作台 tab: worktree-manager")
    except Exception as e:
        logger.warning(f"[worktree-manager] 注册工作树页失败（将显示占位页）: {e}")
    try:
        # 服务：主程序 5 个门面方法的实现体
        registry.register_service(WorktreeService.SERVICE_NAME, WorktreeService(), plugin_name=_PLUGIN_NAME)
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
