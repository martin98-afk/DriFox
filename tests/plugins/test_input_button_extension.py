# -*- coding: utf-8 -*-
"""D3: 输入区按钮扩展点（registry + main_widget 消费）

不变量：
- 注册 input button → get_input_buttons() 返回
- main_widget._build_plugin_input_buttons 生成对应 QToolButton（tooltip/图标）
- 点击派发 on_click（context 含 window_id/button_id）
- 卸载/清空后按钮消失（幂等重建）
"""

import pytest
from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


@pytest.fixture()
def widget(qtbot):
    """轻量窗口骨架：仅工具栏胶囊，避免构造完整 OpenAIChatToolWindow"""
    from app.main_widget import OpenAIChatToolWindow

    w = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    w._window_id = "test-window"
    w._toolbar_capsule = QWidget()
    w._toolbar_capsule.setLayout(QHBoxLayout())
    w._plugin_input_buttons = []
    qtbot.addWidget(w._toolbar_capsule)
    return w


def test_registry_input_buttons(fresh_registry):
    fresh_registry.register_input_button("demo", "btn-1", icon_path="i.svg", tooltip="提示", on_click=lambda ctx: None)
    buttons = fresh_registry.get_input_buttons()
    assert len(buttons) == 1
    assert buttons[0].button_id == "btn-1" and buttons[0].tooltip == "提示"


def test_build_buttons_renders(qtbot, widget, fresh_registry):
    """注册按钮 → 构建出 QToolButton（tooltip 正确）"""
    from PyQt5.QtWidgets import QToolButton

    clicks = []

    def _on_click(ctx):
        clicks.append(ctx)

    fresh_registry.register_input_button("demo", "btn-1", tooltip="插件按钮", on_click=_on_click)
    widget._build_plugin_input_buttons()

    capsule = widget._toolbar_capsule
    buttons = [b for b in capsule.findChildren(QToolButton) if b.toolTip() == "插件按钮"]
    assert len(buttons) == 1
    # 点击 → on_click 派发（context 含 window_id / button_id）
    buttons[0].click()
    assert len(clicks) == 1
    assert clicks[0]["button_id"] == "btn-1"
    assert clicks[0]["window_id"] == "test-window"


def test_rebuild_idempotent(qtbot, widget, fresh_registry):
    """重建幂等：重复调用不产生重复按钮；清空注册后按钮消失"""
    from PyQt5.QtWidgets import QToolButton

    fresh_registry.register_input_button("demo", "btn-1", tooltip="按钮A", on_click=lambda ctx: None)
    widget._build_plugin_input_buttons()
    widget._build_plugin_input_buttons()  # 幂等重建
    capsule = widget._toolbar_capsule
    buttons = [b for b in capsule.findChildren(QToolButton) if b.toolTip() == "按钮A"]
    assert len(buttons) == 1

    # 清空注册 → 重建后消失
    fresh_registry._input_buttons.clear()
    widget._build_plugin_input_buttons()
    assert [b for b in capsule.findChildren(QToolButton) if b.toolTip() == "按钮A"] == []


def test_no_buttons_renders_nothing(qtbot, widget, fresh_registry):
    """未注册任何按钮 → 不渲染（行为零变化）"""
    widget._build_plugin_input_buttons()
    capsule = widget._toolbar_capsule
    assert capsule.layout().count() == 0


def test_click_exception_safe(qtbot, widget, fresh_registry):
    """on_click 抛异常不炸 UI（日志兜底）"""
    def _boom(ctx):
        raise RuntimeError("boom")

    fresh_registry.register_input_button("demo", "btn-1", tooltip="坏按钮", on_click=_boom)
    widget._build_plugin_input_buttons()
    from PyQt5.QtWidgets import QToolButton

    btn = [b for b in widget._toolbar_capsule.findChildren(QToolButton) if b.toolTip() == "坏按钮"][0]
    btn.click()  # 不应抛异常


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
