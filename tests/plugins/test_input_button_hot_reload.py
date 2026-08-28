# -*- coding: utf-8 -*-
"""热重载端到端复现：plugin_changed(result.ui=True) → 已打开窗口输入区按钮实时重建。

复现用户故障：改 UI 插件（新增输入区按钮）后，已打开标签页的输入区
不出现新按钮（新建标签页才显示）。
"""

import pytest
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.core import window_registry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def widget(qtbot, monkeypatch):
    """轻量窗口骨架 + 挂入 _instances（模拟已打开标签页）"""
    from app.main_widget import OpenAIChatToolWindow

    w = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    w._window_id = "test-window"
    w._is_destroyed = False
    w._toolbar_capsule = QWidget()
    w._toolbar_capsule.setLayout(QHBoxLayout())
    w._plugin_input_buttons = []
    # 命令卡片 mock：绕过 _on_plugin_hot_reload 首循环的 _command_card 访问
    from unittest.mock import MagicMock

    w._command_card = MagicMock()
    qtbot.addWidget(w._toolbar_capsule)
    # 模拟已打开窗口：注册进类级实例表 + 复位热重载指纹
    window_registry.window_instances.append(w)
    window_registry.last_hot_reload_fingerprint = None
    OpenAIChatToolWindow._last_hot_reload_at = 0.0
    yield w
    if w in window_registry.window_instances:
        window_registry.window_instances.remove(w)
    window_registry.last_hot_reload_fingerprint = None
    OpenAIChatToolWindow._last_hot_reload_at = 0.0


def _hot_reload_result(plugin_name="demo"):
    from app.plugins.kernel import KNOWN_COMPONENTS

    result = {k: (0 if k == "agents" else False) for k in KNOWN_COMPONENTS}
    result["ui"] = True
    result["_event_seq"] = 1
    result["_plugin_name"] = plugin_name
    return result


def test_hot_reload_rebuilds_open_window_buttons(qtbot, widget, fresh_registry, monkeypatch):
    """已打开窗口 + 热重载(ui=True) → 新注册按钮出现在输入区"""
    from PySide6.QtWidgets import QApplication

    # backend 存在（守卫条件）
    widget.backend = object()
    # registry 先空 → 热重载后注册新按钮
    assert fresh_registry.get_input_buttons() == []

    fresh_registry.register_input_button("demo", "btn-1", tooltip="新按钮", on_click=lambda ctx: None)

    widget._on_plugin_hot_reload(_hot_reload_result())

    buttons = [b for b in widget._toolbar_capsule.findChildren(QToolButton) if b.toolTip() == "新按钮"]
    assert len(buttons) == 1, "热重载后已打开窗口的输入区应实时出现新按钮"


def test_hot_reload_fingerprint_dedup(qtbot, widget, fresh_registry):
    """同一事件第二窗口去重跳过（不炸、不重复执行）"""
    widget.backend = object()
    fresh_registry.register_input_button("demo", "btn-1", tooltip="新按钮", on_click=lambda ctx: None)
    widget._on_plugin_hot_reload(_hot_reload_result())
    # 第二次同指纹事件 → 去重 return（不报错）
    widget._on_plugin_hot_reload(_hot_reload_result())
    from PySide6.QtWidgets import QToolButton

    buttons = [b for b in widget._toolbar_capsule.findChildren(QToolButton) if b.toolTip() == "新按钮"]
    assert len(buttons) == 1


def test_hot_reload_rogue_window_does_not_block_broadcast(qtbot, widget, fresh_registry, monkeypatch):
    """残骸窗口（C++ 已删/未 init）不得阻断其余窗口的按钮重建（回归：用户故障根因）"""
    from app.main_widget import OpenAIChatToolWindow

    widget.backend = object()
    fresh_registry.register_input_button("demo", "btn-1", tooltip="新按钮", on_click=lambda ctx: None)

    # 构造残骸：未 super().__init__ 的半成品，缺 _command_card/_is_destroyed——
    # 修复前 hasattr(win, "_command_card") 抛 RuntimeError 中断整个广播槽
    rogue = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    monkeypatch.setattr(
        window_registry, "window_instances", [rogue, widget]  # 残骸排在最前
    )
    monkeypatch.setattr(window_registry, "last_hot_reload_fingerprint", None)
    monkeypatch.setattr(OpenAIChatToolWindow, "_last_hot_reload_at", 0.0)

    from PySide6.QtWidgets import QToolButton

    widget._on_plugin_hot_reload(_hot_reload_result())
    buttons = [b for b in widget._toolbar_capsule.findChildren(QToolButton) if b.toolTip() == "新按钮"]
    assert len(buttons) == 1, "残骸窗口之后的健康窗口必须完成按钮重建"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
