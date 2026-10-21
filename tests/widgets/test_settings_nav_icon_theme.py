# -*- coding: utf-8 -*-
"""回归测试：设置卡左侧导航 FluentIcon 图标随深浅主题切换变色

背景（2026-09-09）：用户报告设置导航栏最后几个图标（通用/更新/插件，
均来自 NAV_GROUPS 的 FluentIcon 枚举源）在深浅切换后颜色不变。

根因：FluentIconBase.icon()（默认 Theme.AUTO）在调用瞬间把当前主题烧进
静态 QIcon（svg 文件名含颜色），主题切换后不更新；qicon() 返回
FluentIconEngine 动态包装，绘制时按当前主题取色。
_resolve_nav_icon 曾用 icon() → 改为 qicon()。
"""

import pytest
from qfluentwidgets import FluentIcon, Theme, isDarkTheme, setTheme

from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard


def _icon_avg_color(icon, size=18) -> str:
    """取图标不透明像素平均色（transparent → "transparent"）"""
    pm = icon.pixmap(size, size)
    img = pm.toImage()
    r = g = b = n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 100:
                r += c.red()
                g += c.green()
                b += c.blue()
                n += 1
    if n == 0:
        return "transparent"
    return f"#{r // n:02x}{g // n:02x}{b // n:02x}"


@pytest.fixture()
def _restore_theme():
    """setTheme 是全局副作用，测完恢复原主题"""
    original = Theme.LIGHT if not isDarkTheme() else Theme.DARK
    yield
    setTheme(original)


@pytest.mark.parametrize("fluent_icon", [FluentIcon.SETTING, FluentIcon.UPDATE, FluentIcon.APPLICATION])
def test_fluent_nav_icon_follows_theme(qapp, fluent_icon, _restore_theme):
    """NAV_GROUPS 三个 FluentIcon 枚举源图标深浅切换后颜色跟随"""
    icon = LLMSettingsCard._resolve_nav_icon(fluent_icon)

    setTheme(Theme.DARK)
    dark_color = _icon_avg_color(icon)
    setTheme(Theme.LIGHT)
    light_color = _icon_avg_color(icon)

    # 深色主题白图标、浅色主题黑图标，且两态颜色不同
    assert dark_color != "transparent" and light_color != "transparent"
    assert dark_color != light_color
    assert light_color == "#000000"
