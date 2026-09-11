# -*- coding: utf-8 -*-
"""artifacts-manager 插件 UI 注册入口

职责：
- 注册工作台「产物」页（``page_id="artifacts"`` 为**保留** page_id —— 注册它即
  填充产物页槽位，index 1；面板本身不提供产物实现，插件卸载后显示占位页）。

宿主契约（插件版产物页必须实现，否则面板推送数据会失败）：
- ``set_operations(ops)``：接收文件操作记录并渲染
- ``set_diff_all_callback(cb)``：接收差异回调
- ``refresh_style()``：主题刷新

页面实现见 ``artifacts_page.SystemArtifactsPage``：通过 context 从宿主拉取数据
（``context["backend"]`` / ``context["session_id"]``），并通过
``context["diff_requested_callback"]`` 触发差异对比。

注册后自动获得 ``/artifacts`` 命令（UIPluginRegistry 命令账本联动注册）。
"""

from loguru import logger

_PLUGIN_NAME = "artifacts-manager"


def register_ui(registry) -> None:
    """产物页 ui 组件注册入口（被 UIPluginRegistry.load_plugin 调用）

    Args:
        registry: UIPluginRegistry 单例
    """
    try:
        from .artifacts_page import SystemArtifactsPage

        registry.register_workbench_tab(
            plugin_name=_PLUGIN_NAME,
            page_id="artifacts",
            label="产物",
            widget_class=SystemArtifactsPage,
            priority=10,
            metadata={"source": "system", "order_hint": 10},
        )
        logger.info("[artifacts-manager] 已注册工作台 tab: artifacts（产物页）")
    except Exception as e:
        logger.warning(f"[artifacts-manager] 注册 artifacts tab 失败（产物页将显示占位）: {e}")
