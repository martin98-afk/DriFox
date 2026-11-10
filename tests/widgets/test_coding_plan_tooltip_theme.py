# -*- coding: utf-8 -*-
"""回归测试：套餐用量浮层（CodingPlanTooltip）跟随主题切换刷新文字色

2026-11-08 bug：浮层已打开时切换主题，卡片背景（paintEvent 实时读属性）与
进度条轨道跟随变浅，但 QLabel 文字（套餐用量 / 5小时用量 / 重置倒计时）
仍是旧主题的白色 —— set_data() 只调了 _load_theme_colors() 更新属性，
没有 _apply_sheet() 重建样式表，浅色卡片上文字几乎不可见。
"""

from PyQt5.QtWidgets import QApplication  # noqa: F401  (确保 Qt 已导入)

_DARK = {
    "card_bg_solid": "#212126",
    "border": "#3d3d3d",
    "text_primary": "#ffffff",
    "text_secondary": "rgba(240, 240, 240, 0.5)",
}

_LIGHT = {
    "card_bg_solid": "#fafafa",
    "border": "#e0e0e0",
    "text_primary": "#1a1a1a",
    "text_secondary": "rgba(26, 26, 26, 0.55)",
}


def _use_theme(monkeypatch, state):
    import app.utils.design_tokens as dt

    monkeypatch.setattr(dt, "current_theme", lambda: dict(state["theme"]))


def test_set_data_applies_new_theme_colors(qapp, monkeypatch):
    """主题版本推进后，set_data 必须把新文字色写进样式表"""
    from app.utils.theme_refresh import ThemeRefreshCoordinator
    from app.widgets.coding_plan_ring import CodingPlanTooltip

    state = {"theme": _DARK}
    _use_theme(monkeypatch, state)

    ThemeRefreshCoordinator.on_theme_changed("test-coding-plan-dark")
    tip = CodingPlanTooltip()
    layers = {"rolling": {"percent": 10, "reset_sec": 60}}
    tip.set_data(layers)
    assert "#ffffff" in tip.styleSheet()

    state["theme"] = _LIGHT
    ThemeRefreshCoordinator.on_theme_changed("test-coding-plan-light")
    tip.set_data(layers)
    # 修复前：样式表停留在旧主题的 #ffffff
    assert "#1a1a1a" in tip.styleSheet()


def test_opened_tooltip_refreshes_via_ring(qapp, monkeypatch):
    """已打开的浮层：主窗口调 ring.refresh_theme() 后文字色同步"""

    from app.utils.theme_refresh import ThemeRefreshCoordinator
    from app.widgets.coding_plan_ring import CodingPlanRing

    state = {"theme": _DARK}
    _use_theme(monkeypatch, state)

    ThemeRefreshCoordinator.on_theme_changed("test-coding-plan-ring-dark")
    ring = CodingPlanRing()
    ring.set_usage(rolling={"percent": 42, "reset_sec": 3600})
    tip = ring._tooltip
    assert "#ffffff" in tip.styleSheet()

    state["theme"] = _LIGHT
    ThemeRefreshCoordinator.on_theme_changed("test-coding-plan-ring-light")
    ring.refresh_theme()
    assert "#1a1a1a" in tip.styleSheet()


def test_same_theme_version_skips_repolish(qapp, monkeypatch):
    """同主题版本内重复 set_data 不应重建样式表（60s 轮询驱动）"""
    from app.utils.theme_refresh import ThemeRefreshCoordinator
    from app.widgets.coding_plan_ring import CodingPlanTooltip

    state = {"theme": _DARK}
    _use_theme(monkeypatch, state)

    ThemeRefreshCoordinator.on_theme_changed("test-coding-plan-same")
    tip = CodingPlanTooltip()
    calls = {"n": 0}
    orig = tip._apply_theme

    def _counting():
        calls["n"] += 1
        orig()

    tip._apply_theme = _counting
    layers = {"rolling": {"percent": 10, "reset_sec": 60}}
    tip.set_data(layers)
    tip.set_data(layers)
    assert calls["n"] == 0
