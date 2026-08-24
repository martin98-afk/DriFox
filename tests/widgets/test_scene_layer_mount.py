# -*- coding: utf-8 -*-
"""SceneLayer 挂载点回归测试

回归点：scene_layer 必须作为 TabManagerWindow._chat_frame 子 widget 创建
（撑满整个右侧圆角矩形，含 replace_tab_bar / 对话区 / LEFT/RIGHT/BOTTOM
停靠区 / UI 插件槽位），而不是挂在 OpenAIChatToolWindow/chat_container 上。

验证：
1. TabManagerWindow._setup_ui 后存在 _scene_layer
2. _scene_layer.parent() == _chat_frame
3. scene_layer apply scene 配置后背景色生效
4. ChatAreaModule 不再创建 _scene_layer（由 TabManagerWindow 接管）
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def tab_manager(qapp, monkeypatch):
    """创建最小可用的 TabManagerWindow（_setup_ui 走完整路径）"""
    from PyQt5.QtWidgets import QApplication

    # 关闭 TabManagerWindow._setup_ui 中可能需要外部依赖的初始化副作用
    fake_tray = type("FakeTray", (), {"_tab_manager_window": None})()
    monkeypatch.setattr("app.tray_manager.TrayManager.get_instance", lambda: fake_tray)

    from app.widgets.tab_manager_window import TabManagerWindow

    # 拿单例
    if TabManagerWindow._instance is not None:
        TabManagerWindow._instance.deleteLater()
        TabManagerWindow._instance = None
    try:
        win = TabManagerWindow()
        win.resize(1200, 800)
        win.show()
        QApplication.processEvents()
        yield win
    finally:
        try:
            win.close()
        except Exception:
            pass
        win.deleteLater()
        TabManagerWindow._instance = None


def test_scene_layer_mounted_on_chat_frame(tab_manager):
    """核心断言：scene_layer 必须挂在 _chat_frame 上"""
    assert hasattr(tab_manager, "_scene_layer"), "TabManagerWindow 未创建 _scene_layer"
    assert tab_manager._scene_layer.parent() is tab_manager._chat_frame, (
        f"scene_layer 父 widget 错误：期望 _chat_frame, 实际 {tab_manager._scene_layer.parent()}"
    )


def test_scene_layer_fills_chat_frame_rect(tab_manager):
    """scene_layer 必须撑满 _chat_frame.rect()（覆盖整个右侧圆角矩形）"""
    layer = tab_manager._scene_layer
    frame_rect = tab_manager._chat_frame.rect()
    assert layer.geometry() == frame_rect, (
        f"scene_layer 尺寸未撑满 _chat_frame：layer={layer.geometry()} frame={frame_rect}"
    )


def test_chat_area_module_no_longer_creates_scene_layer(qapp, monkeypatch):
    """ChatAreaModule 应不再创建 _scene_layer（由 TabManagerWindow 接管）"""
    from PyQt5.QtWidgets import QVBoxLayout, QWidget
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.widgets.modules.chat_area_module import ChatAreaModule
    from app.widgets.ui_composition import compose

    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    reg.register_ui_module("chat_area", ChatAreaModule, plugin_name="system")

    class _Host(QWidget):
        pass

    host = _Host()
    host._top_card_container = QWidget(host)
    host._bottom_card_container = QWidget(host)

    def _ensure_layout(h):
        if h.layout() is None:
            QVBoxLayout(h)

    compose(host, ["chat_area"], root_layout_factory=_ensure_layout)
    # ChatAreaModule 不再持有 _scene_layer（场景图由 TabManagerWindow._chat_frame 接管）
    assert not hasattr(host, "_scene_layer"), (
        "ChatAreaModule 不应再创建 _scene_layer（已迁移到 TabManagerWindow._chat_frame）"
    )
    # 但 _decoration_layer 仍然由 ChatAreaModule 创建
    assert hasattr(host, "_decoration_layer"), "ChatAreaModule 应仍创建 _decoration_layer"
