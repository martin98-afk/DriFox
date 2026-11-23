# -*- coding: utf-8 -*-
"""#I2 聊天区内容层实底化回归测试（纯色主题条件化）

被测：app/widgets/modules/chat_area_module.apply_chat_surface_opaque

纯色主题（scene/chat_area 无图、无 overlay）→ chat_container 刷 palette
实底（autoFillBackground=True），启用滚动 blit；
壁纸/动态层主题（scene.image / chat_area.image / overlay.enabled）→
保持透明（autoFillBackground=False）；
主题配置翻转后重入（模拟 EV_THEME_CHANGED → host.refresh_theme 联动）
状态正确翻转；无 chat_container 的桩安全返回。
"""
import pytest

from app.widgets.modules.chat_area_module import apply_chat_surface_opaque


class _Host:
    """最小 host 桩：只带 apply_chat_surface_opaque 消费的属性"""

    def __init__(self, with_container=True):
        if with_container:
            from PyQt5.QtWidgets import QWidget

            self.chat_container = QWidget()


def _mock_theme(monkeypatch, *, scene=None, chat_area=None, overlay=None):
    """替换 theme_manager 单例的主题背景查询（不加载真实主题库）"""
    from app.utils.theme_manager import theme_manager

    bgs = {
        "window": None,
        "sidebar": None,
        "chat_area": chat_area,
        "scene": scene,
        "decorations": [],
        "overlay": overlay,
        "music": None,
    }
    monkeypatch.setattr(theme_manager, "get_current_theme_id", lambda: "flat-test")
    monkeypatch.setattr(theme_manager, "get_theme_backgrounds", lambda tid: bgs)


def test_flat_theme_paints_opaque(qapp, monkeypatch):
    _mock_theme(monkeypatch)
    host = _Host()
    apply_chat_surface_opaque(host)
    c = host.chat_container
    assert c.autoFillBackground() is True, "纯色主题必须 autofill 实底（blit 前置条件）"
    color = c.palette().color(c.backgroundRole())
    assert color.alpha() == 255, "实底色必须是预合成不透明色"


def test_scene_wallpaper_stays_transparent(qapp, monkeypatch):
    _mock_theme(monkeypatch, scene={"image": "bg.jpg", "enabled": True})
    host = _Host()
    apply_chat_surface_opaque(host)
    assert host.chat_container.autoFillBackground() is False, "壁纸主题必须保持透明"


def test_chat_area_image_stays_transparent(qapp, monkeypatch):
    _mock_theme(monkeypatch, chat_area={"color": None, "image": "paper.png"})
    host = _Host()
    apply_chat_surface_opaque(host)
    assert host.chat_container.autoFillBackground() is False, "chat_area 配图也算壁纸"


def test_overlay_enabled_stays_transparent(qapp, monkeypatch):
    _mock_theme(monkeypatch, overlay={"enabled": True, "video": "x.mp4"})
    host = _Host()
    apply_chat_surface_opaque(host)
    assert host.chat_container.autoFillBackground() is False, "overlay 动态层必须保持透明"


def test_overlay_disabled_is_flat(qapp, monkeypatch):
    _mock_theme(monkeypatch, overlay={"enabled": False})
    host = _Host()
    apply_chat_surface_opaque(host)
    assert host.chat_container.autoFillBackground() is True, "overlay 禁用视为纯色"


def test_theme_flip_toggles_state(qapp, monkeypatch):
    """模拟主题切换联动：壁纸→纯色 翻转后实底恢复"""
    _mock_theme(monkeypatch, scene={"image": "bg.jpg", "enabled": True})
    host = _Host()
    apply_chat_surface_opaque(host)
    assert host.chat_container.autoFillBackground() is False

    _mock_theme(monkeypatch)  # 切到纯色主题
    apply_chat_surface_opaque(host)
    assert host.chat_container.autoFillBackground() is True, "切纯色后必须重新实底（EV 联动翻转）"


def test_idempotent_when_already_opaque(qapp, monkeypatch):
    _mock_theme(monkeypatch)
    host = _Host()
    apply_chat_surface_opaque(host)
    once = host.chat_container.palette().color(host.chat_container.backgroundRole()).name()
    apply_chat_surface_opaque(host)
    twice = host.chat_container.palette().color(host.chat_container.backgroundRole()).name()
    assert once == twice, "重入必须幂等（palette 色不得漂移）"


def test_host_without_container_is_safe(qapp, monkeypatch):
    _mock_theme(monkeypatch)
    apply_chat_surface_opaque(_Host(with_container=False))  # 不抛异常即可
