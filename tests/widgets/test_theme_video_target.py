# -*- coding: utf-8 -*-
"""overlay.target：缺省回落、非法回落、切换 target 时旧层停止、多层不并存

不启动真实窗口：直接把 TabManagerWindow 的 _apply_video_overlay 绑到一个
构造好的假宿主上，宿主提供各区域挂载点与 _resolve_theme_image。
这样能精确验证「层挂在哪个父 widget 上」「旧层是否被停掉」。
"""
import pytest
from PySide6.QtWidgets import QWidget

from app.widgets.tab_manager_window import TabManagerWindow


class _Host(QWidget):
    """最小真宿主：只提供 _apply_video_overlay 依赖的属性"""

    _resolve_theme_image = staticmethod(TabManagerWindow._resolve_theme_image)
    _apply_video_overlay = TabManagerWindow._apply_video_overlay
    _overlay_target_widget = TabManagerWindow._overlay_target_widget
    _OVERLAY_TARGETS = TabManagerWindow._OVERLAY_TARGETS

    def __init__(self):
        super().__init__()
        self._chat_frame = QWidget(self)
        self._chat_frame.resize(200, 200)
        self._tab_frame = QWidget(self)
        self._tab_frame.resize(120, 200)
        self._window_base_layer = QWidget(self)
        self._scene_layer = None
        self._video_overlay_layers = {}


@pytest.fixture()
def host(qapp, monkeypatch):
    # 不碰真实媒体后端：本测试只验证挂载位置与显隐，真实播放由人工验收
    monkeypatch.setattr("PySide6.QtMultimedia.QMediaPlayer.play", lambda self: None)
    monkeypatch.setattr("PySide6.QtMultimedia.QMediaPlayer.setSource", lambda self, source: None)
    h = _Host()
    h.resize(400, 400)
    yield h
    for layer in getattr(h, "_video_overlay_layers", {}).values():
        try:
            layer.deleteLater()
        except RuntimeError:
            pass
    h.deleteLater()


def _cfg(target=None, enabled=True):
    cfg = {"video": "x.mp4", "enabled": enabled}
    if target is not None:
        cfg["target"] = target
    return cfg


def test_default_target_is_chat_area(host):
    host._apply_video_overlay(_cfg())
    layers = host._video_overlay_layers
    assert set(layers) == {"chat_area"}
    assert layers["chat_area"].parent() is host._chat_frame


def test_invalid_target_falls_back_to_chat_area(host):
    host._apply_video_overlay(_cfg(target="nonsense"))
    assert set(host._video_overlay_layers) == {"chat_area"}


def test_target_sidebar_uses_tab_frame(host):
    host._apply_video_overlay(_cfg(target="sidebar"))
    assert host._video_overlay_layers["sidebar"].parent() is host._tab_frame


def test_target_window_uses_self(host):
    host._apply_video_overlay(_cfg(target="window"))
    assert host._video_overlay_layers["window"].parent() is host


def test_target_scene_uses_chat_frame(host):
    host._apply_video_overlay(_cfg(target="scene"))
    assert host._video_overlay_layers["scene"].parent() is host._chat_frame


def test_switching_target_stops_and_hides_previous(host):
    host._apply_video_overlay(_cfg(target="chat_area"))
    old = host._video_overlay_layers["chat_area"]
    host._apply_video_overlay(_cfg(target="sidebar"))
    # isHidden 与父窗口是否可见无关，比 isVisible 更适合无 show 的测试
    assert old.isHidden()
    assert set(host._video_overlay_layers) == {"chat_area", "sidebar"}


def test_disabling_stops_all_layers(host):
    host._apply_video_overlay(_cfg(target="sidebar"))
    layer = host._video_overlay_layers["sidebar"]
    host._apply_video_overlay(_cfg(target="sidebar", enabled=False))
    assert layer.isHidden()


def test_missing_video_stops_all_layers(host):
    host._apply_video_overlay(_cfg(target="chat_area"))
    layer = host._video_overlay_layers["chat_area"]
    host._apply_video_overlay({"target": "chat_area", "enabled": True})
    assert layer.isHidden()
