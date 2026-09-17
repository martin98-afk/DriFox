# -*- coding: utf-8 -*-
"""热重载端到端复现：plugin_changed(result.ui=True) → 已打开窗口输入区按钮实时重建。

复现用户故障：改 UI 插件（新增输入区按钮）后，已打开标签页的输入区
不出现新按钮（新建标签页才显示）。
"""

import weakref

import pytest
from PyQt5.QtWidgets import QHBoxLayout, QToolButton, QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.core import window_registry



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
    # window_instances 存弱引用（window_registry.register_window 同口径），
    # alive_window_instances() 会对元素调用 r()，塞强引用会抛 TypeError。
    window_registry.window_instances.append(weakref.ref(w))
    window_registry.last_hot_reload_fingerprint = None
    OpenAIChatToolWindow._last_hot_reload_at = 0.0
    yield w
    window_registry.unregister_window(w)
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
    from PyQt5.QtWidgets import QApplication

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
    from PyQt5.QtWidgets import QToolButton

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
        window_registry, "window_instances", [weakref.ref(rogue), weakref.ref(widget)]  # 残骸排在最前
    )
    monkeypatch.setattr(window_registry, "last_hot_reload_fingerprint", None)
    monkeypatch.setattr(OpenAIChatToolWindow, "_last_hot_reload_at", 0.0)

    from PyQt5.QtWidgets import QToolButton

    widget._on_plugin_hot_reload(_hot_reload_result())
    buttons = [b for b in widget._toolbar_capsule.findChildren(QToolButton) if b.toolTip() == "新按钮"]
    assert len(buttons) == 1, "残骸窗口之后的健康窗口必须完成按钮重建"


def test_hot_reload_ui_does_not_refresh_welcome_cards(qtbot, widget, fresh_registry, monkeypatch):
    """仅 input_button 变更（插件不占 welcome 槽位）时不得刷新欢迎卡片

    回归：main_widget._on_plugin_hot_reload 的 ui 分支曾无条件调用
    UIPluginRegistry._refresh_welcome_cards()——任何 ui 组件热重载（哪怕只动
    输入区按钮）都会让每个窗口的欢迎卡片缓存失效，正显示欢迎卡片的窗口
    立即重建 QWebEngineView（100-500ms/个，肉眼可见闪一下），且与 registry
    自身调度双刷。精准判定在 registry：unload 按 had_welcome_tabs、
    load 按 before_tabs/after_tabs 才 _schedule_welcome_refresh。
    """
    widget.backend = object()
    calls = []
    monkeypatch.setattr(UIPluginRegistry, "_refresh_welcome_cards", lambda self: calls.append(1))

    fresh_registry.register_input_button("demo", "btn-1", tooltip="新按钮", on_click=lambda ctx: None)

    widget._on_plugin_hot_reload(_hot_reload_result())
    assert calls == [], f"插件不占 welcome 槽位时不应刷新欢迎卡片，实际刷新 {len(calls)} 次"


def test_welcome_tab_plugin_unload_still_schedules_welcome_refresh(qtbot, fresh_registry, monkeypatch):
    """占 welcome 槽位的插件卸载后仍必须刷新欢迎卡片（防 2026-08-23 故障回归）

    删除 main_widget 的无条件兜底后，刷新完全依赖 registry 自身判定：
    unload 按 had_welcome_tabs、load 按 before_tabs/after_tabs 调度
    _schedule_welcome_refresh（QTimer.singleShot + debounce）。
    """
    fresh_registry.register_welcome_tab("demo", "tab-1", "T", lambda ctx: "<p>x</p>")
    calls = []
    monkeypatch.setattr(UIPluginRegistry, "_refresh_welcome_cards", lambda self: calls.append(1))

    fresh_registry.unload_plugin("demo")
    qtbot.wait(50)
    assert calls, "占 welcome 槽位的插件卸载后必须调度欢迎卡片刷新"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
