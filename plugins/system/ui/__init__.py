# -*- coding: utf-8 -*-
"""系统插件内置 UI 组件入口

当前职责：
- 把「产物」页注册到右侧工作台（WorkbenchPanel）。

page_id 约定：
- ``"artifacts"`` 是**保留** page_id —— 注册它即**填充产物页槽位**（index 0）。
  面板本身**不提供产物实现**，产物功能完全插件化；插件卸载后槽位显示占位页。
- 其它 page_id 一律作为新增 tab 追加在「产物 / 记忆」之后。

产物页实现见 ``_artifacts_page.SystemArtifactsPage``：通过 context 从宿主
拉取数据（``context["backend"]`` / ``context["session_id"]``），并通过
``context["diff_requested_callback"]`` 触发差异对比。

宿主契约（插件版产物页必须实现，否则面板推送数据会失败）：
- ``set_operations(ops)``：接收文件操作记录并渲染
- ``set_diff_all_callback(cb)``：接收差异回调
- ``refresh_style()``：主题刷新
"""

from loguru import logger


def register_ui(registry) -> None:
    """系统插件 ui 组件注册入口（被 UIPluginRegistry.load_plugin 调用）

    Args:
        registry: UIPluginRegistry 单例
    """
    try:
        from ._worktree_page import SystemWorktreePage

        # 工作树页：page_id="worktree" 填工作树槽位（index 0，默认落点）
        registry.register_workbench_tab(
            plugin_name="system",
            page_id="worktree",
            label="工作树",
            widget_class=SystemWorktreePage,
            priority=20,
            metadata={"source": "system"},
        )
        logger.info("[system.ui] 已注册工作台 tab: worktree（工作树·系统插件版）")
    except Exception as e:
        logger.warning(f"[system.ui] 注册 worktree tab 失败（工作树页将显示占位）: {e}")
    try:
        from ._artifacts_page import SystemArtifactsPage

        # 产物页：page_id="artifacts" 填产物页槽位（面板无内置实现）
        registry.register_workbench_tab(
            plugin_name="system",
            page_id="artifacts",
            label="产物",
            widget_class=SystemArtifactsPage,
            priority=10,
            metadata={"source": "system"},
        )
        logger.info("[system.ui] 已注册工作台 tab: artifacts（产物·系统插件版）")
    except Exception as e:
        logger.warning(f"[system.ui] 注册 artifacts tab 失败（产物页将显示占位）: {e}")
