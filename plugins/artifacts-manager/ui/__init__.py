# -*- coding: utf-8 -*-
"""artifacts-manager 插件注册入口

职责（代码不分散，成果栏与它的工具同属本插件）：

- 卡片成果栏面板：``register_card_inline_panel(panel_id="artifacts")``，
  面板类见 ``artifact_panel.InlineArtifactPanel``。
- 成果展示工具：``tools/show_artifacts.py``，返回 ``ToolResult.artifacts``，
  由宿主通用链路投递到本面板。

宿主契约：

- ``update_items(items)``：运行态增量推送
- ``restore_items(items)``：会话重载历史回填（``history_loader`` 提供数据）
- ``heightChanged`` / ``previewImageRequested``：宿主自动接线
"""

from loguru import logger

_PLUGIN_NAME = "artifacts-manager"


def _load_history(batch, host):
    """会话重载：从落库的 tool 消息收集成果条目（宿主不猜，按批次传账）"""
    items = []
    for msg in batch or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        artifacts = msg.get("artifacts")
        if artifacts:
            items.extend(artifacts)
    return items


def register_ui(registry) -> None:
    """成果栏面板注册入口（被 UIPluginRegistry.load_plugin 调用）"""
    try:
        from .artifact_panel import InlineArtifactPanel

        registry.register_card_inline_panel(
            plugin_name=_PLUGIN_NAME,
            panel_id="artifacts",
            widget_class=InlineArtifactPanel,
            order=20,
            metadata={"source": "system"},
            history_loader=_load_history,
        )
        logger.info("[artifacts-manager] 已注册卡片成果栏: artifacts")
    except Exception as e:
        logger.warning(f"[artifacts-manager] 注册成果栏失败（成果将无处展示）: {e}")
