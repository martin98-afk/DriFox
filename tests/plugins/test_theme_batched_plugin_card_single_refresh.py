# -*- coding: utf-8 -*-
"""T12 双刷修复回归：插件卡主题刷新单刷化（EV 承接）

背景
----
主题切换时 UI 插件浮动卡被刷两次：
1. 宿主路径：main_widget._refresh_floating_cards_font_style（5a/5b 共用）尾部
   直接遍历 UIPluginRegistry._card_widget_instances 逐卡 refresh_style；
2. 事件路径：theme_manager.on_theme_changed → EV_THEME_CHANGED →
   UIPluginRegistry._on_theme_changed_event 全量派发 refresh_style。

且 batched 全局段原先 on_theme_changed（publish）先于 setTheme 执行：
qfluentwidgets setTheme 会再做一次框架级重刷，插件卡第一刷落在旧框架态上。

修复（两步，顺序依赖）：
1. batched 全局段 on_theme_changed 移到 setTheme 之后（仍居全局段、无条件
   publish）；
2. 删除 _refresh_floating_cards_font_style 尾部的插件卡直调 try 块，
   插件卡刷新由 EV → UIPluginRegistry 全量派发单点承接。

本文件锁定：
1. publish 晚于 setTheme（先框架后派发）；
2. font scope 下插件卡仍被刷（registry 承接）且同轮恰刷 1 次。
"""

from unittest.mock import MagicMock

from app.core.infra.ui_event_bus import EV_THEME_CHANGED, UIEventBus
from app.core.infra import window_registry
from app.main_widget import OpenAIChatToolWindow
from app.utils.design_tokens import Colors
from app.utils import theme_manager as tm_module


def test_batched_publish_after_settheme(monkeypatch):
    """batched 全局段：EV_THEME_CHANGED publish 必须晚于 setTheme 完成。

    顺序即正确性：publish 若在 setTheme 之前，插件卡 refresh_style 刷完
    后又被 qfluentwidgets 框架级重刷覆盖 → 双刷。锁定「先框架后派发」。
    """
    calls = []

    monkeypatch.setattr(tm_module.theme_manager, "on_theme_changed", lambda: calls.append("publish"))
    monkeypatch.setattr(tm_module.theme_manager, "is_light_theme", lambda theme_id=None: False)
    monkeypatch.setattr(Colors, "refresh", classmethod(lambda cls: calls.append("colors")))
    monkeypatch.setattr(window_registry, "alive_window_instances", lambda: [])

    import qfluentwidgets

    monkeypatch.setattr(qfluentwidgets, "setTheme", lambda theme, **k: calls.append("settheme"))

    OpenAIChatToolWindow._theme_batch_timer = None
    OpenAIChatToolWindow._theme_batch_scope = None
    OpenAIChatToolWindow._execute_batched_theme_refresh()

    assert "settheme" in calls, f"setTheme 未被调用: {calls}"
    assert "publish" in calls, f"on_theme_changed 未被调用: {calls}"
    assert calls.index("settheme") < calls.index("publish"), (
        f"publish 必须晚于 setTheme，实际顺序: {calls}"
    )


def test_font_scope_plugin_card_single_refresh(monkeypatch):
    """font scope 下插件卡仍被刷（EV → registry 承接）且同轮恰刷 1 次。

    宿主 _refresh_floating_cards_font_style 不得再直调插件卡（5b 直调块
    已删）；唯一刷新入口是 EV_THEME_CHANGED → UIPluginRegistry 派发。
    """
    from app.plugins.registries import ui_plugin_registry as uir_module

    reg = uir_module.UIPluginRegistry.get_instance()
    original_instances = reg._card_widget_instances
    card = MagicMock()
    card.findChildren = lambda *a, **k: []  # replay_theme_qss 遍历用
    reg._card_widget_instances = {"w1": {"card_a": card}}
    try:
        # ── 宿主路径：5b 字体块不再直调插件卡 ──
        win = MagicMock()
        win._window_id = "w1"
        OpenAIChatToolWindow._refresh_floating_cards_font_style(win)
        assert card.refresh_style.call_count == 0, (
            "宿主路径不应再直调插件卡（应由 EV 承接避免双刷）"
        )

        # ── registry 路径承接：同轮恰刷 1 次 ──
        bus = UIEventBus.get_instance()
        bus.publish(EV_THEME_CHANGED, theme_id="t1", theme_name="T", is_dark=True)
        assert card.refresh_style.call_count == 1, (
            f"font scope 下插件卡应恰好被刷 1 次，实际 {card.refresh_style.call_count}"
        )
    finally:
        reg._card_widget_instances = original_instances


def test_batched_scope_merge_mixed_types_becomes_full(monkeypatch, qapp):
    """回归（2026-10-31）：Gitee 同步一次写回主题+字体+字号，同批多类型 scope
    不得折叠成 "theme"——_apply_runtime_ui_settings(scope="theme") 只刷颜色块、
    跳过字体块，折叠会吞掉字体/字号刷新。合并规则：同类型保持，异类型合并为
    None（全量）。
    """
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    OpenAIChatToolWindow._theme_batch_timer = None
    OpenAIChatToolWindow._theme_batch_scope = None

    win = MagicMock()
    win._window_id = "w1"
    win._is_destroyed = False  # MagicMock 自动属性为 truthy，会被销毁守卫跳过
    monkeypatch.setattr(window_registry, "alive_window_instances", lambda: [win])
    monkeypatch.setattr(Colors, "refresh", classmethod(lambda cls: None))
    monkeypatch.setattr(tm_module.theme_manager, "is_light_theme", lambda theme_id=None: False)
    monkeypatch.setattr(tm_module.theme_manager, "on_theme_changed", lambda: None)
    import qfluentwidgets

    monkeypatch.setattr(qfluentwidgets, "setTheme", lambda theme, **k: None)
    monkeypatch.setattr(
        "app.widgets.tab_manager_window.TabManagerWindow.get_instance", staticmethod(lambda: None)
    )

    # __new__ 桩：type(实例)=真类（合并状态挂类级），实例 dict 覆盖重载方法
    win_self = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win_self._load_model_configs = lambda: None

    # 模拟 Gitee 同步写回顺序：theme → font_family → font_size（30ms debounce 窗口内连发）
    for scope in ("theme", "font_family", "font_size"):
        LLMSettingsCard._last_change_type = scope
        OpenAIChatToolWindow._on_settings_config_changed(win_self)

    assert OpenAIChatToolWindow._theme_batch_scope == OpenAIChatToolWindow._BATCH_SCOPE_FULL, (
        f"异类型同批必须合并为全量哨兵，不得折叠成 theme，实际 {OpenAIChatToolWindow._theme_batch_scope!r}"
    )

    # 手动 fire：锁定窗口收到 scope=None（全量，字体块不丢）
    OpenAIChatToolWindow._execute_batched_theme_refresh()
    win._apply_runtime_ui_settings.assert_called_once_with(scope=None, _skip_global=True)


def test_batched_scope_merge_same_type_stays(monkeypatch, qapp):
    """同类型同批保持精确 scope：手动切字号连发两次 font_size → 不升格全量"""
    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    OpenAIChatToolWindow._theme_batch_timer = None
    OpenAIChatToolWindow._theme_batch_scope = None

    win_self = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win_self._load_model_configs = lambda: None

    for _ in range(2):
        LLMSettingsCard._last_change_type = "font_size"
        OpenAIChatToolWindow._on_settings_config_changed(win_self)

    assert OpenAIChatToolWindow._theme_batch_scope == "font_size"
