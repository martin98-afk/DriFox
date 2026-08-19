# -*- coding: utf-8 -*-
"""D4: 右键菜单扩展点（registry + 注入方法层）

不变量：
- 按 target 注册 action → get_context_actions(target) 返回有序列表（separator_before 标记）
- action_func 返回 False 的菜单关闭语义由调用方处理（注入方法返回 bool）
- message_card / tab_panel 注入方法：插件项按注册序追加，enabled_func 为 False 置灰
"""

import pytest
from PyQt5.QtWidgets import QMenu, QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


# ---------- registry 层 ----------


def test_get_context_actions_by_target(fresh_registry):
    fresh_registry.register_context_menu_action("demo", "a1", target="message_card", label="A", action_func=lambda ctx: True)
    fresh_registry.register_context_menu_action("demo", "a2", target="tab", label="B", action_func=lambda ctx: True)
    assert [a.action_id for a in fresh_registry.get_context_actions("message_card")] == ["a1"]
    assert [a.action_id for a in fresh_registry.get_context_actions("tab")] == ["a2"]
    assert fresh_registry.get_context_actions("other") == []


def test_priority_override(fresh_registry):
    fresh_registry.register_context_menu_action("demo", "a1", target="message_card", label="低", action_func=lambda ctx: True, priority=1)
    fresh_registry.register_context_menu_action("demo", "a1", target="message_card", label="高", action_func=lambda ctx: True, priority=5)
    assert fresh_registry.get_context_actions("message_card")[0].label == "高"


# ---------- message_card 注入 ----------


def test_message_card_injection(qtbot, fresh_registry):
    """消息卡片菜单注入：插件项追加到菜单末尾；enabled False 置灰；点击触发 action_func"""
    from app.widgets.message_card import CodeWebViewer

    calls = []

    def _act(ctx):
        calls.append(ctx)
        return True

    fresh_registry.register_context_menu_action("demo", "act-1", target="message_card", label="插件动作", action_func=_act)
    fresh_registry.register_context_menu_action(
        "demo", "act-2", target="message_card", label="置灰动作", action_func=lambda ctx: True, enabled_func=lambda ctx: False
    )

    viewer = CodeWebViewer.__new__(CodeWebViewer)
    menu = QMenu()
    qtbot.addWidget(menu)
    context = {"round_index": 0, "message_index": 1, "window_id": "w1"}
    viewer._inject_plugin_context_actions(menu, context)

    actions = menu.actions()
    labels = [a.text() for a in actions]
    assert "插件动作" in labels and "置灰动作" in labels
    disabled = next(a for a in actions if a.text() == "置灰动作")
    assert not disabled.isEnabled(), "enabled_func=False 应置灰"

    # 触发插件动作 → action_func 收到 context
    plugin_action = next(a for a in actions if a.text() == "插件动作")
    plugin_action.trigger()
    assert calls and calls[0]["window_id"] == "w1"


def test_message_card_injection_empty(fresh_registry):
    """无插件注册 → 注入零项（菜单结构不变）"""
    from app.widgets.message_card import CodeWebViewer

    viewer = CodeWebViewer.__new__(CodeWebViewer)
    menu = QMenu()
    viewer._inject_plugin_context_actions(menu, {"round_index": 0})
    assert menu.actions() == []


# ---------- tab_panel 注入 ----------


def test_tab_panel_injection(qtbot, fresh_registry):
    """tab 菜单注入：插件项追加；点击触发 action_func（context 含 tab_index）"""
    from app.widgets.tab_panel import TabPanel

    calls = []

    def _act(ctx):
        calls.append(ctx)
        return False  # 关闭菜单语义

    fresh_registry.register_context_menu_action("demo", "tab-act", target="tab", label="Tab插件项", action_func=_act)

    panel = TabPanel.__new__(TabPanel)
    menu = QMenu()
    qtbot.addWidget(menu)
    context = {"tab_index": 2, "window_id": "w1"}
    panel._inject_plugin_tab_actions(menu, context)

    actions = menu.actions()
    assert len(actions) == 1 and actions[0].text() == "Tab插件项"
    actions[0].trigger()
    assert calls and calls[0]["tab_index"] == 2


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
