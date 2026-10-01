# -*- coding: utf-8 -*-
"""
欢迎卡片插件 tab 记忆回归测试

背景 bug：自定义插件注入的 welcome tab 切换后无法被记住（重启回 sessions）。
根因（两层拦截）：
1. main_widget._on_welcome_mode_changed 对非内置 mode 直接 return，不写配置；
2. 即使写入，welcome_mode 的 OptionsValidator(["sessions"]) 会在
   value setter 把插件 mode_key correct 回 sessions。

修复演进：插件 tab 记忆先存独立无验证器字段 welcome_plugin_tab，
后随「上次状态与系统配置分离」迁至 app_state（.drifox/cache/app_state.json）。
初始 mode 由 resolve_initial_welcome_mode 解析（保存的插件 tab 仍注册时优先）。
"""

import pytest

from app.utils.config import Settings

PLUGIN_KEY = "plugin_custom_tab"


@pytest.fixture(autouse=True)
def _isolated_app_state(tmp_path, monkeypatch):
    """app_state 隔离到 tmp_path（不读写真实缓存文件）；结束后还原 welcome_mode 内存态"""
    from app.utils import app_state

    monkeypatch.setattr(app_state, "_state_file", lambda: tmp_path / "app_state.json")
    monkeypatch.setattr(app_state, "_cache", None)
    cfg = Settings.get_instance()
    saved_mode = cfg.welcome_mode.value
    yield
    cfg.welcome_mode.value = saved_mode


def test_welcome_mode_rejects_plugin_key():
    """复现旧行为：welcome_mode 的 OptionsValidator 会把插件 key 纠正回 sessions"""
    cfg = Settings.get_instance()
    cfg.welcome_mode.value = PLUGIN_KEY
    assert cfg.welcome_mode.value == "sessions"


def test_plugin_tab_state_roundtrip():
    """承载字段语义：app_state 原样保留插件 key（无验证器纠正）"""
    from app.utils import app_state

    app_state.set("welcome_plugin_tab", PLUGIN_KEY)
    assert app_state.get("welcome_plugin_tab") == PLUGIN_KEY
    app_state.set("welcome_plugin_tab", "")
    assert app_state.get("welcome_plugin_tab") == ""


def _on_welcome_mode_changed(new_mode: str):
    """直接调用主窗口类._on_welcome_mode_changed（不实例化窗口）"""
    from app.main_widget import OpenAIChatToolWindow

    mw = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    mw._on_welcome_mode_changed(new_mode)


def test_plugin_tab_write_goes_to_dedicated_field():
    """插件 tab 切换 → 写 app_state 的 welcome_plugin_tab，不动 welcome_mode"""
    from app.utils import app_state

    _on_welcome_mode_changed(PLUGIN_KEY)
    assert app_state.get("welcome_plugin_tab") == PLUGIN_KEY
    assert Settings.get_instance().welcome_mode.value == "sessions"


def test_builtin_tab_clears_plugin_memory():
    """切回内置 tab → 清空插件记忆并更新 welcome_mode（sessions 是当前唯一内置 mode）"""
    from app.utils import app_state

    _on_welcome_mode_changed(PLUGIN_KEY)
    _on_welcome_mode_changed("sessions")
    assert app_state.get("welcome_plugin_tab") == ""
    assert Settings.get_instance().welcome_mode.value == "sessions"


def test_resolve_prefers_registered_plugin_tab():
    """保存的插件 tab 仍注册 → 初始 mode 用插件 tab"""
    from app.widgets.message_card import resolve_initial_welcome_mode

    registered = {PLUGIN_KEY: object(), "sessions": object()}
    mode = resolve_initial_welcome_mode("sessions", PLUGIN_KEY, registered)
    assert mode == PLUGIN_KEY


def test_resolve_falls_back_when_plugin_gone():
    """保存的插件 tab 未注册（插件卸载/停用）→ 回退内置 mode，不崩溃"""
    from app.widgets.message_card import resolve_initial_welcome_mode

    mode = resolve_initial_welcome_mode("sessions", PLUGIN_KEY, {})
    assert mode == "sessions"


def test_resolve_falls_back_when_empty():
    """无插件记忆 → 用内置 mode"""
    from app.widgets.message_card import resolve_initial_welcome_mode

    mode = resolve_initial_welcome_mode("sessions", "", {PLUGIN_KEY: object()})
    assert mode == "sessions"
