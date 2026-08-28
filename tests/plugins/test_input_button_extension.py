# -*- coding: utf-8 -*-
"""D3: 输入区按钮扩展点（registry + main_widget 消费）

不变量：
- 注册 input button → get_input_buttons() 返回
- main_widget._build_plugin_input_buttons 生成对应 QToolButton（tooltip/图标）
- 点击派发 on_click（context 含 window_id/button_id）
- 卸载/清空后按钮消失（幂等重建）
"""

import pytest
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

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


def test_registry_input_button_light_icon(fresh_registry):
    """注册时可带 icon_light_path（浅色主题图标），缺省为空字符串"""
    fresh_registry.register_input_button(
        "demo", "btn-1", icon_path="dark.svg", icon_light_path="light.svg", on_click=lambda ctx: None
    )
    buttons = fresh_registry.get_input_buttons()
    assert buttons[0].icon_path == "dark.svg"
    assert buttons[0].icon_light_path == "light.svg"


def test_resolve_icon_theme_aware(qtbot, widget, fresh_registry, monkeypatch):
    """_resolve_input_button_icon 随主题选择图标：浅色用 icon_light_path，深色用 icon_path"""
    import os
    import tempfile

    from app.main_widget import OpenAIChatToolWindow
    from app.plugins.registries.ui_plugin_registry import InputButtonInfo
    from PySide6.QtGui import QIcon

    tmp = tempfile.mkdtemp()
    dark = os.path.join(tmp, "dark.svg")
    light = os.path.join(tmp, "light.svg")
    with open(dark, "w", encoding="utf-8") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10" fill="#fff"/></svg>')
    with open(light, "w", encoding="utf-8") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10" fill="#000"/></svg>')

    info = InputButtonInfo(plugin_name="demo", button_id="b", icon_path=dark, icon_light_path=light)

    import app.utils.theme_manager as tm_mod

    # 深色主题 → icon_path
    monkeypatch.setattr(tm_mod.theme_manager, "is_light_theme", lambda: False)
    icon = OpenAIChatToolWindow._resolve_input_button_icon(info)
    assert isinstance(icon, QIcon) and not icon.isNull()

    # 浅色主题 → icon_light_path
    monkeypatch.setattr(tm_mod.theme_manager, "is_light_theme", lambda: True)
    icon2 = OpenAIChatToolWindow._resolve_input_button_icon(info)
    assert isinstance(icon2, QIcon) and not icon2.isNull()

    # 浅色主题缺 icon_light_path → 回退 icon_path
    info_fallback = InputButtonInfo(plugin_name="demo", button_id="b", icon_path=dark)
    icon3 = OpenAIChatToolWindow._resolve_input_button_icon(info_fallback)
    assert isinstance(icon3, QIcon) and not icon3.isNull()


def test_refresh_icons_updates_button(qtbot, widget, fresh_registry, monkeypatch):
    """_refresh_plugin_input_button_icons 遍历按钮按新主题重设图标"""
    import os
    import tempfile

    from PySide6.QtWidgets import QToolButton

    tmp = tempfile.mkdtemp()
    dark = os.path.join(tmp, "dark.svg")
    light = os.path.join(tmp, "light.svg")
    with open(dark, "w", encoding="utf-8") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10" fill="#fff"/></svg>')
    with open(light, "w", encoding="utf-8") as f:
        f.write('<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10" fill="#000"/></svg>')

    fresh_registry.register_input_button(
        "demo", "btn-1", icon_path=dark, icon_light_path=light, tooltip="主题按钮", on_click=lambda ctx: None
    )
    widget._build_plugin_input_buttons()
    btn = [b for b in widget._toolbar_capsule.findChildren(QToolButton) if b.toolTip() == "主题按钮"][0]

    # 主题切换后刷新：不重建控件（引用一致），图标重设
    old_btn = btn
    widget._refresh_plugin_input_button_icons()
    assert btn is old_btn  # 控件未重建
    assert not btn.icon().isNull()


def test_build_buttons_renders(qtbot, widget, fresh_registry):
    """注册按钮 → 构建出 QToolButton（tooltip 正确）"""
    from PySide6.QtWidgets import QToolButton

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
    from PySide6.QtWidgets import QToolButton

    fresh_registry.register_input_button("demo", "btn-1", tooltip="按钮A", on_click=lambda ctx: None)
    widget._build_plugin_input_buttons()
    widget._build_plugin_input_buttons()  # 幂等重建
    capsule = widget._toolbar_capsule
    buttons = [b for b in capsule.findChildren(QToolButton) if b.toolTip() == "按钮A"]
    assert len(buttons) == 1

    # 清空注册 → 重建后消失（Phase E：注册表走 region 存储，用 unload_plugin 清理）
    fresh_registry.unload_plugin("demo")
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
    from PySide6.QtWidgets import QToolButton

    btn = [b for b in widget._toolbar_capsule.findChildren(QToolButton) if b.toolTip() == "坏按钮"][0]
    btn.click()  # 不应抛异常


# ---------- Phase E：position 位置插入 ----------


class TestInputButtonPosition:
    def test_default_position_end(self, fresh_registry):
        fresh_registry.register_input_button("demo", "b1")
        assert fresh_registry.get_input_buttons()[0].position == "end"

    def test_position_passthrough(self, fresh_registry):
        fresh_registry.register_input_button("demo", "b1", position="before:memory")
        assert fresh_registry.get_input_buttons()[0].position == "before:memory"

    def test_invalid_position_rejected(self, fresh_registry):
        with pytest.raises(ValueError, match="position"):
            fresh_registry.register_input_button("demo", "b1", position="middle")


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
