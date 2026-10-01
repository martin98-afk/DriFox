# -*- coding: utf-8 -*-
"""独立弹窗示例 — ui/__init__.py

参照自仓库内置示例 plugins/ui-slots-demo（2026-09 实战沉淀）。

要点：
- 入口函数固定为 register_ui(registry)，由 UIPluginRegistry 加载钩子调用
- register_window 注册后自动获得：左侧插件栏条目（点击开/关）、右键菜单「弹出」、
  联动命令 /<window_id>（用户插件自动带命名空间前缀）
- 窗口壳（无边框/标题栏/拖动缩放/主题/生命周期）由主程序提供，插件只写内容页
- 热重载惯用法：先清理 sys.modules 中本插件残留的子模块缓存
"""
import sys
from pathlib import Path

from loguru import logger

_PLUGIN = "plugin-window-example"  # [改名] 与 plugin.json name 一致
_WINDOW_ID = f"{_PLUGIN}:tool"     # [改名] 窗口唯一 id（命令联动键）


def _ctx_provider() -> dict:
    """窗口上下文（每次调用返回最新主题 token；独立窗口的 provider 由插件提供）"""
    ctx: dict = {}
    try:
        from app.utils.design_tokens import Colors

        Colors.refresh()
        ctx["colors"] = {
            "text_primary": Colors.TEXT_PRIMARY,
            "text_secondary": Colors.TEXT_SECONDARY,
            "border": Colors.BORDER,
            "accent": Colors.TEXT_ACCENT,
        }
    except Exception:
        pass  # 取不到走内容页 fallback（isDarkTheme 判断）
    return ctx


def register_ui(registry):
    """注册独立弹窗"""
    # 热重载兼容：清掉旧子模块缓存（[改名] 换成你的 ui_plugin_xxx 前缀）
    prefix = "ui_plugin_plugin_window_example."
    for k in [k for k in sys.modules if k.startswith(prefix)]:
        del sys.modules[k]

    from .example_window_page import ExampleWindowPage

    registry.register_window(
        plugin_name=_PLUGIN,            # [改名]
        window_id=_WINDOW_ID,           # [改名]
        widget_class=ExampleWindowPage,
        title="示例工具窗",
        # icon_path=str(Path(__file__).resolve().parent.parent / "icon.svg"),  # 可选：标题栏图标
        width=560,
        height=430,                     # 默认几何
        min_width=380,
        min_height=300,                 # 最小尺寸
        context_provider=_ctx_provider,
        # group="custom",              # 可选：左侧栏分组覆盖 "system"（常驻）/ "custom"（自定义折叠区）
        #                               #      不传 = 跟随插件归属（仓库内置→常驻，用户插件→自定义）
    )
    logger.info(f"[{_PLUGIN}] 独立弹窗已注册")
