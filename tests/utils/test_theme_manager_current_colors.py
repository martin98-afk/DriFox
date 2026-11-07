# -*- coding: utf-8 -*-
"""启动期竞态：当前主题 id 未就绪时 get_current_colors 必须回退到完整 colors

回归：ui_theme_style 指向尚未加载（或因版本门禁永久不可用）的插件主题时，
get_current_theme() 返回空 dict，旧实现 get_current_colors() 静默返回 {}，
current_theme() 的下游按「字典必然完整」硬索引 card_bg_solid 即抛 KeyError
（card_viewers._load_skeleton / chart_viewer_card._inject_theme_vars）。
"""

from app.utils.theme_manager import theme_manager

# current_theme() 下游按完整字典硬索引的键（见 card_viewers._load_skeleton）
_REQUIRED = (
    "card_bg_solid",
    "content_bg",
    "border",
    "border_accent",
    "text_primary",
    "text_secondary",
    "text_muted",
    "accent",
    "accent_warm",
    "hover_bg",
    "selected_bg",
)


def test_current_colors_falls_back_when_theme_id_not_loaded(monkeypatch):
    """未就绪的 theme_id 不得返回空 colors，否则骨架首帧崩溃"""
    theme_manager._ensure_loaded()
    assert theme_manager.list_themes(), "内置主题应已加载，测试前置条件不成立"
    monkeypatch.setattr(
        theme_manager,
        "get_current_theme_id",
        lambda: "__theme_not_loaded_yet__",
    )
    colors = theme_manager.get_current_colors()
    assert colors, "未就绪主题 id 不得返回空 colors"
    for key in _REQUIRED:
        assert key in colors, f"回退 colors 缺 {key}"


def test_current_colors_returns_own_theme_when_available(monkeypatch):
    """正常路径不受影响：能取到主题时返回该主题自己的 colors"""
    theme_manager._ensure_loaded()
    tid = next(iter(theme_manager.list_themes()))
    monkeypatch.setattr(theme_manager, "get_current_theme_id", lambda: tid)
    own = theme_manager.get_theme(tid)
    assert own and own.get("colors")
    assert theme_manager.get_current_colors() == own["colors"]
