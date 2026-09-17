# -*- coding: utf-8 -*-
"""ui-slots-demo — UI 扩展点示例插件入口

演示 2026-09-17 新增/增强的四类能力（可作为新插件的参考模板）：

1. ``register_window``         独立弹窗：左侧「自定义插件」栏条目（点击开/关切换）
                               + 右键菜单「弹出」+ 自动命令 /ui-slots-demo:demo
2. ``register_footer_action``  ``role="user"``：用户消息气泡底部按钮
3. ``register_footer_action``  ``role="both"``：助手页脚与用户气泡两端同款按钮
4. ``register_footer_stat``    消息卡片页脚信息注入（本条消息字数）

联动演示：点击用户消息下方的「发送到演示窗」按钮 → 自动打开独立弹窗并
把该条消息内容送去展示（按钮回调 ctx["card"] → 弹窗内容页 API）。
"""

import sys
from pathlib import Path

from loguru import logger

_PLUGIN = "ui-slots-demo"
_WINDOW_ID = "ui-slots-demo:demo"

_ICON_LIGHT = str(Path(__file__).resolve().parent.parent / "icon.svg")
_ICON_DARK = str(Path(__file__).resolve().parent.parent / "icon_dark.svg")


def _demo_context_provider() -> dict:
    """弹窗上下文拉模型（每次调用返回最新主题色）

    真实插件的上下文一般由主程序提供（浮动卡槽位自动注入 project_root /
    session_id / colors 等）；独立窗口场景下 provider 由插件在
    register_window 时自行提供，本函数演示「从主程序主题 token 取色」的写法。
    """
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
        pass
    try:
        from PyQt5.QtCore import QDateTime

        ctx["demo_time"] = QDateTime.currentDateTime().toString("HH:mm:ss")
    except Exception:
        pass
    return ctx


def _open_demo_window():
    """打开（或前置）演示弹窗，返回内容页实例（失败返回 None）"""
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        win = UIPluginRegistry.get_instance().open_window(_WINDOW_ID)
    except Exception as e:
        logger.warning(f"[ui-slots-demo] 打开演示窗失败: {e}")
        return None
    return getattr(win, "_content", None) if win is not None else None


def _on_user_button(ctx) -> None:
    """role="user" 按钮回调：把用户消息内容送到演示弹窗展示

    ctx 由消息卡片组装，含 card（MessageCard 实例）/role/message_index 等；
    ctx["card"].get_plain_text() 取消息纯文本。
    """
    text = ""
    try:
        card = ctx.get("card")
        if card is not None:
            text = card.get_plain_text() or ""
    except Exception:
        pass
    page = _open_demo_window()
    if page is not None and hasattr(page, "set_last_message"):
        page.set_last_message(text)


def _on_both_button(ctx) -> None:
    """role="both" 按钮回调：仅打开/前置演示弹窗（演示 API 直接调用）"""
    _open_demo_window()


def _footer_stat_provider(ctx):
    """页脚信息项：本条消息字数（演示 footer_stat 注入）

    provider 在主线程被调用，要求纯内存快速计算；返回 None 表示本条不显示。
    """
    try:
        card = ctx.get("card")
        text = (card.get_plain_text() or "") if card is not None else ""
    except Exception:
        return None
    n = len(text)
    if n <= 0:
        return None
    return {"text": f"{n} 字", "color": None, "tooltip": "本条消息字数（footer_stat 注入示例）"}


def register_ui(registry) -> None:
    """UI 组件注册入口（热重载时会被重新调用）"""
    # 热重载兼容：清理旧子模块缓存（避免 Python 用旧 sys.modules 引用）
    prefix = f"ui_plugin_{_PLUGIN.replace('-', '_')}."
    for k in [k for k in sys.modules if k.startswith(prefix)]:
        del sys.modules[k]

    from .demo_page import DemoWindowPage

    # ① 独立弹窗：左侧插件栏条目 + 命令 + 右键「弹出」
    registry.register_window(
        plugin_name=_PLUGIN,
        window_id=_WINDOW_ID,
        widget_class=DemoWindowPage,
        title="UI 扩展点演示",
        width=560,
        height=430,
        min_width=380,
        min_height=300,
        context_provider=_demo_context_provider,
        metadata={"demo": True},
    )

    # ② 用户消息气泡按钮（内置复制/撤销/删除左侧）
    registry.register_footer_action(
        plugin_name=_PLUGIN,
        action_id="ui-slots-demo:send-to-window",
        icon_path=_ICON_DARK,
        icon_light_path=_ICON_LIGHT,
        tooltip="发送到演示窗",
        role="user",
        on_click=_on_user_button,
    )

    # ③ 两端同款按钮（助手页脚 + 用户气泡都渲染）
    registry.register_footer_action(
        plugin_name=_PLUGIN,
        action_id="ui-slots-demo:open-window",
        icon_path=_ICON_DARK,
        icon_light_path=_ICON_LIGHT,
        tooltip="打开演示窗",
        role="both",
        on_click=_on_both_button,
    )

    # ④ 消息卡片页脚信息注入（本条消息字数）
    registry.register_footer_stat(
        plugin_name=_PLUGIN,
        stat_id="ui-slots-demo:char-count",
        provider=_footer_stat_provider,
        priority=0,
        metadata={"label": "消息字数"},
    )

    logger.info("[ui-slots-demo] UI 扩展点示例已注册：window + footer_action(user/both) + footer_stat")