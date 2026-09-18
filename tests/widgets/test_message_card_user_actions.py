# -*- coding: utf-8 -*-
"""MessageCard user 按钮栏插件注册（footer_action role 分流）测试

覆盖：role=user/both 渲染在用户气泡底部操作行（内置按钮左侧）；
role=assistant 仅渲染在助手页脚按钮组；both 双端渲染。
"""

from qfluentwidgets import TransparentToolButton

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.message_card import MessageCard


def _fresh_reg() -> UIPluginRegistry:
    """复位并返回**当前单例**（reset 置 _instance=None，旧引用已失效）。

    卡片构造内部会再 get_instance()，注册必须落在当前单例上。
    """
    reg = UIPluginRegistry.get_instance()
    reg.reset()
    return UIPluginRegistry.get_instance()


def _make_card(role: str) -> MessageCard:
    return MessageCard(role=role)


def _user_btn_count(card: MessageCard) -> int:
    """用户气泡底部操作行的按钮总数（内置复制/撤销/删除 + 插件按钮）"""
    btns = getattr(card, "_user_action_btns", None)
    assert btns is not None
    return len(btns.findChildren(TransparentToolButton))


def _assistant_btn_count(card: MessageCard) -> int:
    """助手卡片页脚 hover 按钮组按钮数（内置分支/复制 + 插件按钮）"""
    btns = getattr(card, "_assistant_action_btns", None)
    assert btns is not None
    return len(btns.findChildren(TransparentToolButton))


def test_user_card_renders_role_user_button(qapp):
    """user 卡片：role=user 插件按钮渲染，且位于内置按钮之前（左侧）"""
    reg = _fresh_reg()
    reg.register_footer_action(
        "plug-a",
        "user-btn",
        tooltip="插件按钮",
        role="user",
        on_click=lambda ctx: None,
    )
    card = _make_card("user")
    assert _user_btn_count(card) == 4  # 3 内置（复制/撤销/删除）+ 1 插件
    # 插件按钮在布局首位（内置复制之前）
    bl = card._user_action_btns.layout()
    first = bl.itemAt(0).widget()
    assert first.toolTip() == "插件按钮"
    UIPluginRegistry.get_instance().reset()


def test_user_card_skips_role_assistant(qapp):
    """user 卡片：role=assistant 插件按钮不渲染（仅 3 内置）"""
    reg = _fresh_reg()
    reg.register_footer_action("plug-a", "assist-btn", tooltip="A", role="assistant")
    card = _make_card("user")
    assert _user_btn_count(card) == 3
    UIPluginRegistry.get_instance().reset()


def test_assistant_card_skips_role_user_button(qapp):
    """assistant 卡片：role=user 插件按钮不渲染（仅 2 内置：分支/复制）"""
    reg = _fresh_reg()
    reg.register_footer_action("plug-a", "user-btn", tooltip="U", role="user")
    card = _make_card("assistant")
    assert _assistant_btn_count(card) == 2
    UIPluginRegistry.get_instance().reset()


def test_both_role_renders_both_sides(qapp):
    """role=both：user 与 assistant 两端都渲染"""
    reg = _fresh_reg()
    reg.register_footer_action("plug-a", "both-btn", tooltip="B", role="both")
    user_card = _make_card("user")
    assert _user_btn_count(user_card) == 4  # 3 内置 + both
    assist_card = _make_card("assistant")
    assert _assistant_btn_count(assist_card) == 3  # 分支/复制 + both
    UIPluginRegistry.get_instance().reset()


def test_user_plugin_button_click_dispatches_context(qapp):
    """点击 user 侧插件按钮：on_click 收到卡片 context（含 role/card）"""
    reg = _fresh_reg()
    captured = {}

    def _on_click(ctx):
        captured.update(ctx)

    reg.register_footer_action(
        "plug-a", "user-btn", tooltip="U", role="user", on_click=_on_click
    )
    card = _make_card("user")
    bl = card._user_action_btns.layout()
    plugin_btn = bl.itemAt(0).widget()
    assert plugin_btn.toolTip() == "U"
    plugin_btn.click()
    assert captured.get("role") == "user"
    assert captured.get("card") is card
    UIPluginRegistry.get_instance().reset()