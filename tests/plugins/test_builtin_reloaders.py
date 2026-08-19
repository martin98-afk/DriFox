# -*- coding: utf-8 -*-
"""内置 reloader 注册测试 — monkeypatch 各子系统，断言分派正确、不真实加载"""

from app.plugins import builtin_reloaders
from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext


def _make_reg() -> ComponentReloaderRegistry:
    reg = ComponentReloaderRegistry()
    builtin_reloaders.register_builtin_reloaders(reg)
    return reg


def test_all_components_registered():
    reg = _make_reg()
    assert reg.known_components() == builtin_reloaders.RELOADED_COMPONENTS


def test_themes_reloader_calls_theme_manager(monkeypatch):
    called = {}

    class _FakeThemeManager:
        def reload(self):
            called["reload"] = True

    import app.utils.theme_manager as tm_mod
    monkeypatch.setattr(tm_mod, "theme_manager", _FakeThemeManager())
    monkeypatch.setattr("app.utils.config.update_theme_options", lambda: called.update(options=True))

    reg = _make_reg()
    ok = reg.reload(ReloadContext("p", plugin=None, component="themes", is_new_plugin=False))
    assert ok is True and called == {"reload": True, "options": True}


def test_skills_reloader_invalidates_cache(monkeypatch):
    called = []
    import app.plugins.builtin_reloaders as br
    monkeypatch.setattr(br, "invalidate_skills_cache", lambda: called.append(1))

    reg = _make_reg()
    ok = reg.reload(ReloadContext("p", plugin=None, component="skills", is_new_plugin=False))
    assert ok is True and called == [1]


def test_idempotent_register():
    reg = ComponentReloaderRegistry()
    builtin_reloaders.register_builtin_reloaders(reg)
    builtin_reloaders.register_builtin_reloaders(reg)  # 二次注册不抛
    assert reg.known_components() == builtin_reloaders.RELOADED_COMPONENTS
