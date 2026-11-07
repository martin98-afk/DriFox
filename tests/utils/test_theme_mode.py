# -*- coding: utf-8 -*-
"""外观模式：三态解析、生效值回写、旧配置迁移与分段控件交互"""

from PyQt5.QtCore import Qt

from app.utils import theme_mode


class _Item:
    def __init__(self, value):
        self.value = value


class _StubConfig:
    def __init__(self, light=True, mode="auto", style="lumia"):
        self.ui_light_mode = _Item(light)
        self.ui_theme_mode = _Item(mode)
        self.ui_theme_style = _Item(style)
        self.saved = 0

    def set(self, item, value, save=False):
        item.value = value
        if save:
            self.saved += 1


def _patch_theme_manager(monkeypatch, light_theme_ids):
    """把主题深浅判定钉成固定集合，测试不依赖真实主题文件"""
    from app.utils.theme_manager import theme_manager

    monkeypatch.setattr(
        theme_manager,
        "is_light_theme",
        lambda theme_id=None: theme_id in light_theme_ids,
        raising=False,
    )


# ── 解析 ──────────────────────────────────────────────


def test_resolve_light_matrix():
    assert theme_mode.resolve_light(theme_mode.MODE_AUTO, True, False) is True
    assert theme_mode.resolve_light(theme_mode.MODE_AUTO, False, True) is False
    # 探测失败回退当前生效值，不改动用户观感
    assert theme_mode.resolve_light(theme_mode.MODE_AUTO, None, False) is False
    assert theme_mode.resolve_light(theme_mode.MODE_AUTO, None, True) is True
    # 手动模式忽略系统探测
    assert theme_mode.resolve_light(theme_mode.MODE_LIGHT, False, False) is True
    assert theme_mode.resolve_light(theme_mode.MODE_DARK, True, True) is False


# ── 生效值回写 ────────────────────────────────────────


def test_apply_light_switches_default_theme(monkeypatch):
    stub = _StubConfig(light=True, mode="auto", style="lumia")
    monkeypatch.setattr(theme_mode, "_settings", lambda: stub)
    _patch_theme_manager(monkeypatch, {"lumia"})

    assert theme_mode.apply_light(False) is True
    assert stub.ui_light_mode.value is False
    assert stub.ui_theme_style.value == theme_mode.DEFAULT_THEME_DARK
    assert stub.saved == 2


def test_apply_light_keeps_matching_custom_theme(monkeypatch):
    """当前主题已属于目标深浅（用户手选的 midnight）时不该打回默认"""
    stub = _StubConfig(light=True, mode="auto", style="midnight")
    monkeypatch.setattr(theme_mode, "_settings", lambda: stub)
    _patch_theme_manager(monkeypatch, {"lumia"})

    assert theme_mode.apply_light(False) is True
    assert stub.ui_theme_style.value == "midnight"


def test_apply_light_noop_when_already_aligned(monkeypatch):
    stub = _StubConfig(light=True, mode="light", style="lumia")
    monkeypatch.setattr(theme_mode, "_settings", lambda: stub)
    _patch_theme_manager(monkeypatch, {"lumia"})

    assert theme_mode.apply_light(True) is False
    assert stub.saved == 0


def test_set_mode_rejects_unknown_value(monkeypatch):
    stub = _StubConfig(light=True, mode="auto", style="lumia")
    monkeypatch.setattr(theme_mode, "_settings", lambda: stub)
    _patch_theme_manager(monkeypatch, {"lumia"})

    theme_mode.set_mode("neon", broadcast=False)
    assert stub.ui_theme_mode.value == "auto"


def test_set_mode_writes_mode_and_light(monkeypatch):
    stub = _StubConfig(light=True, mode="auto", style="lumia")
    monkeypatch.setattr(theme_mode, "_settings", lambda: stub)
    _patch_theme_manager(monkeypatch, {"lumia"})
    monkeypatch.setattr(theme_mode, "sync_watcher", lambda: None)

    theme_mode.set_mode(theme_mode.MODE_DARK, broadcast=False)
    assert stub.ui_theme_mode.value == "dark"
    assert stub.ui_light_mode.value is False
    assert stub.ui_theme_style.value == theme_mode.DEFAULT_THEME_DARK


# ── 旧配置迁移 ────────────────────────────────────────


class _StubSettings:
    def __init__(self, path, mode="auto"):
        self.file = path
        self.ui_theme_mode = _Item(mode)
        self.saved = 0

    def save(self):
        self.saved += 1


def test_migrate_theme_mode_from_legacy_bool(tmp_path):
    from app.utils import config as config_mod

    path = tmp_path / "app.config"
    path.write_text('{"UI": {"LightMode": false}}', encoding="utf-8")

    stub = _StubSettings(path)
    assert config_mod._migrate_theme_mode(stub) is True
    assert stub.ui_theme_mode.value == "dark"
    assert stub.saved == 1


def test_migrate_theme_mode_skips_new_install(tmp_path):
    """全新安装没有 LightMode 键：保留默认 auto，开箱跟随系统"""
    from app.utils import config as config_mod

    path = tmp_path / "app.config"
    path.write_text('{"UI": {"FontSize": "2"}}', encoding="utf-8")

    stub = _StubSettings(path)
    assert config_mod._migrate_theme_mode(stub) is False
    assert stub.ui_theme_mode.value == "auto"
    assert stub.saved == 0


def test_migrate_theme_mode_skips_when_already_migrated(tmp_path):
    from app.utils import config as config_mod

    path = tmp_path / "app.config"
    path.write_text('{"UI": {"LightMode": false, "ThemeMode": "auto"}}', encoding="utf-8")

    stub = _StubSettings(path)
    assert config_mod._migrate_theme_mode(stub) is False
    assert stub.ui_theme_mode.value == "auto"


# ── 分段控件 ──────────────────────────────────────────


def test_selector_keyboard_switching(qtbot):
    from app.widgets.cards.settings.theme_mode_card import ThemeModeSelector

    selector = ThemeModeSelector()
    qtbot.addWidget(selector)
    selector.set_mode(theme_mode.MODE_AUTO, emit=False, animate=False)
    selector.setFocus()
    seen = []
    selector.modeChanged.connect(seen.append)

    qtbot.keyClick(selector, Qt.Key_Right)
    assert selector.mode() == theme_mode.MODE_DARK
    assert seen == [theme_mode.MODE_DARK]

    qtbot.keyClick(selector, Qt.Key_End)
    assert selector.mode() == theme_mode.MODE_LIGHT
    assert seen[-1] == theme_mode.MODE_LIGHT

    # 同段重复触发不重复发信号
    qtbot.keyClick(selector, Qt.Key_End)
    assert seen.count(theme_mode.MODE_LIGHT) == 1


def test_selector_ignores_unknown_mode(qtbot):
    from app.widgets.cards.settings.theme_mode_card import ThemeModeSelector

    selector = ThemeModeSelector()
    qtbot.addWidget(selector)
    selector.set_mode(theme_mode.MODE_DARK, emit=False, animate=False)
    selector.set_mode("neon", emit=False)
    assert selector.mode() == theme_mode.MODE_DARK