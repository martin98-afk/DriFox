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


def _patch_app_state(monkeypatch, store):
    """主题记忆在 app_state 缓存里（不是 app.config），测试用内存 dict 顶替"""
    from app.utils import app_state

    monkeypatch.setattr(app_state, "get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(app_state, "set", lambda key, value, save=True: store.__setitem__(key, value))


def _patch_theme_manager(monkeypatch, light_theme_ids, themes=None):
    """把主题深浅判定与主题清单钉成固定集合，测试不依赖真实主题文件"""
    from app.utils.theme_manager import theme_manager

    monkeypatch.setattr(
        theme_manager,
        "is_light_theme",
        lambda theme_id=None: theme_id in light_theme_ids,
        raising=False,
    )
    all_themes = set(themes) if themes is not None else set(light_theme_ids) | {"midnight", "true-black"}
    monkeypatch.setattr(theme_manager, "list_themes", lambda: {t: t for t in all_themes}, raising=False)


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
    assert stub.ui_theme_style.value == theme_mode.default_theme_for(False)
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


def test_apply_light_restores_remembered_theme(monkeypatch):
    """切回某深浅时优先恢复该模式下用户上次选的主题，而不是落回默认"""
    stub = _StubConfig(light=True, mode="auto", style="lumia")
    _patch_app_state(monkeypatch, {"theme_style_by_mode": {"dark": "midnight"}})
    monkeypatch.setattr(theme_mode, "_settings", lambda: stub)
    _patch_theme_manager(monkeypatch, {"lumia"}, themes={"lumia", "midnight", "true-black"})

    assert theme_mode.apply_light(False) is True
    assert stub.ui_theme_style.value == "midnight"


def test_remembered_theme_rejects_mismatched_or_missing(monkeypatch):
    store = {"theme_style_by_mode": {"dark": "lumia"}}  # 浅色主题错记在 dark 桶
    _patch_app_state(monkeypatch, store)
    stub = _StubConfig()
    monkeypatch.setattr(theme_mode, "_settings", lambda: stub)
    _patch_theme_manager(monkeypatch, {"lumia"}, themes={"lumia"})
    assert theme_mode.remembered_theme(False) is None
    # 不在主题清单里（被删/未注册）同样作废
    store["theme_style_by_mode"] = {"light": "ghost-theme"}
    assert theme_mode.remembered_theme(True) is None


def test_remember_theme_writes_mode_bucket(monkeypatch):
    store = {}
    _patch_app_state(monkeypatch, store)
    stub = _StubConfig()
    monkeypatch.setattr(theme_mode, "_settings", lambda: stub)
    _patch_theme_manager(monkeypatch, {"lumia"}, themes={"lumia", "midnight"})

    theme_mode.remember_theme("midnight")
    assert store["theme_style_by_mode"] == {"dark": "midnight"}
    theme_mode.remember_theme("lumia")
    assert store["theme_style_by_mode"] == {"dark": "midnight", "light": "lumia"}


def test_set_mode_remembers_current_theme_before_switch(monkeypatch):
    """切模式前先把当前主题记进它所属的桶，回来时才有得恢复"""
    store = {}
    _patch_app_state(monkeypatch, store)
    stub = _StubConfig(light=False, mode="dark", style="midnight")
    monkeypatch.setattr(theme_mode, "_settings", lambda: stub)
    _patch_theme_manager(monkeypatch, {"lumia"}, themes={"lumia", "midnight", "true-black"})
    monkeypatch.setattr(theme_mode, "sync_watcher", lambda: None)

    theme_mode.set_mode(theme_mode.MODE_LIGHT, broadcast=False)
    assert store["theme_style_by_mode"].get("dark") == "midnight"
    assert stub.ui_theme_style.value == "lumia"

    theme_mode.set_mode(theme_mode.MODE_DARK, broadcast=False)
    assert stub.ui_theme_style.value == "midnight"


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
    assert stub.ui_theme_style.value == theme_mode.default_theme_for(False)


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


# ── 同步应用屏蔽（主题同步回退 bug 回归） ─────────────────

def test_sync_apply_flag_lifecycle():
    """begin/end 配对：进入标志态、退出复位、异常安全依赖显式配对"""
    assert theme_mode.is_sync_applying() is False
    theme_mode.begin_sync_apply()
    try:
        assert theme_mode.is_sync_applying() is True
    finally:
        theme_mode.end_sync_apply()
    assert theme_mode.is_sync_applying() is False


def test_sync_apply_flag_never_negative():
    """多余 end 不把深度打成负数（except 兜底可能多调一次）"""
    theme_mode.end_sync_apply()
    theme_mode.end_sync_apply()
    assert theme_mode.is_sync_applying() is False
    theme_mode.begin_sync_apply()
    theme_mode.end_sync_apply()
    assert theme_mode.is_sync_applying() is False


def test_apply_light_still_works_outside_sync(monkeypatch):
    """标志态外 apply_light 行为不变：深浅不符时切记忆主题（修复不改变正常路径）"""
    store = {"theme_style_by_mode": {"dark": "the-last-of-us", "light": "sledding-game"}}
    _patch_app_state(monkeypatch, store)
    _patch_theme_manager(monkeypatch, light_theme_ids={"lumia", "sledding-game"})
    monkeypatch.setattr(theme_mode, "_settings", lambda: _StubConfig(light=True, mode="light", style="the-last-of-us"))
    monkeypatch.setattr(
        "app.utils.config.update_theme_options", lambda: None, raising=False
    )

    # 深色主题 + 浅色模式 → 切浅色记忆主题（本次 bug 中第二次重载的翻转路径）
    changed = theme_mode.apply_light(True, save=False)
    assert changed is True

    # 深浅一致 → 不切、不落盘
    monkeypatch.setattr(theme_mode, "_settings", lambda: _StubConfig(light=True, mode="light", style="sledding-game"))
    changed = theme_mode.apply_light(True, save=False)
    assert changed is False


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


def test_selector_renders_all_segments(qtbot):
    """三段图标都要画得出来

    真机回归：未选中态曾用主题的 text_secondary（部分主题 alpha 仅 0.5），
    叠加在同样偏暗的轨道上，太阳细线几乎不可见，缩略截图里只剩两个图标。
    这里按段取样峰值亮度，保证三段都有可见图形。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QImage

    from app.widgets.cards.settings.theme_mode_card import ThemeModeSelector

    selector = ThemeModeSelector(compact=True)
    qtbot.addWidget(selector)
    selector.set_mode(theme_mode.MODE_DARK, emit=False, animate=False)

    image = QImage(selector.size(), QImage.Format_ARGB32)
    image.fill(Qt.black)
    selector.render(image)

    rects = selector._segment_rects()
    for index in range(len(selector._SEGMENTS)):
        rect = rects[index]
        x0 = int(rect.x()) + 3
        x1 = int(rect.x() + rect.width()) - 3
        peak = 0.0
        for x in range(x0, x1):
            for y in range(2, selector.height() - 2):
                color = image.pixelColor(x, y)
                peak = max(peak, 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue())
        assert peak > 120, f"第 {index} 段图标亮度不足（峰值 {peak:.0f}），可能被主题低 alpha 吞掉"


def test_selector_ignores_unknown_mode(qtbot):
    from app.widgets.cards.settings.theme_mode_card import ThemeModeSelector

    selector = ThemeModeSelector()
    qtbot.addWidget(selector)
    selector.set_mode(theme_mode.MODE_DARK, emit=False, animate=False)
    selector.set_mode("neon", emit=False)
    assert selector.mode() == theme_mode.MODE_DARK