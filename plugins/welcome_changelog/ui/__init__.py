# -*- coding: utf-8 -*-
"""welcome_changelog 插件 UI 入口。

注册欢迎卡片「📜 更新」tab（``mode_key="changelog"``）：

- ``label``：SegmentedWidget 上展示的标签
- ``render_func``：签名 ``(ctx: dict) -> str(HTML)``；返回完整 HTML 片段
  （含 ``<style>`` / 列表 / ``<script>``），由主程序 markdown 管线拼到 viewer

启用：通过 Settings 启用本插件即可在欢迎卡片看到该 tab；禁用后 tab
自动消失（UIPluginRegistry.unload_plugin 会清掉注册项，主程序下次
构建 welcome_mode_tabs 时不再插入 changelog 项）。

插件自包含：
- 渲染逻辑 + CSS/JS 在 ``_render.py``
- GitHub Releases 后台拉取 + 进程内缓存 + 异步刷新派发在 ``_fetcher.py``
- 跨线程通信走 ``app.core.ui_event_bus.EV_WELCOME_TAB_REFRESHED`` 事件
"""

from __future__ import annotations

from loguru import logger

from ._render import render_changelog

_MODE_KEY = "changelog"
_LABEL = "📜 更新"
_PLUGIN_NAME = "welcome_changelog"


def register_ui(registry) -> None:
    """UIPluginRegistry 加载钩子（被 PluginManager._load_plugin_ui 调用）

    Args:
        registry: UIPluginRegistry 单例
    """
    try:
        registry.register_welcome_tab(
            plugin_name=_PLUGIN_NAME,
            mode_key=_MODE_KEY,
            label=_LABEL,
            render_func=render_changelog,
            priority=100,
        )
        logger.info(f"[{_PLUGIN_NAME}] 已注册欢迎卡片「{_LABEL}」tab")
    except Exception as e:
        logger.warning(f"[{_PLUGIN_NAME}] 注册欢迎 tab 失败: {e}")