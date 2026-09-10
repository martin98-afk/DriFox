# -*- coding: utf-8 -*-
"""欢迎页 tab 示例 — ui/__init__.py

参照自系统插件 welcome_changelog（D:/work/DriFox/plugins/welcome_changelog/ui/__init__.py）。

要点：
- register_welcome_tab 在欢迎卡片的 SegmentedWidget 上新增一个 tab
- render_func 签名固定为 (ctx: dict) -> str，返回**完整 HTML 片段**
  （可含 <style> / 列表 / <script>），由主程序 markdown 管线拼进 viewer
- 禁用插件后 tab 自动消失（unload_plugin 清注册项），无需额外处理
- 渲染逻辑放独立模块（_render.py）是 welcome_changelog 的分层惯例
"""
from loguru import logger

from ._render import render_example

_MODE_KEY = "example"        # [改名] tab 的模式标识，唯一
_LABEL = "⭐ 示例"           # [改名] tab 上展示的标签
_PLUGIN_NAME = "welcome-tab-example"  # [改名] 与 plugin.json name 一致


def register_ui(registry) -> None:
    """UIPluginRegistry 加载钩子（被 PluginManager._load_plugin_ui 调用）"""
    try:
        registry.register_welcome_tab(
            plugin_name=_PLUGIN_NAME,
            mode_key=_MODE_KEY,
            label=_LABEL,
            render_func=render_example,
            priority=100,
        )
        logger.info(f"[{_PLUGIN_NAME}] 已注册欢迎卡片「{_LABEL}」tab")
    except Exception as e:
        logger.warning(f"[{_PLUGIN_NAME}] 注册欢迎 tab 失败: {e}")
