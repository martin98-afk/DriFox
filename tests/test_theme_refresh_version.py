# -*- coding: utf-8 -*-
"""
主题刷新版本号链路回归测试。

背景：消息卡正文/思考块颜色依赖注入到页面的 CSS 变量（var(--text) 等）。
CodeWebViewer.refresh_theme 通过 ThemeRefreshCoordinator 版本号做幂等短路，
但此前 should_skip / on_theme_changed 从未被调用，版本号恒为 0，
导致每张卡片只在首次切换注入一次 CSS 变量，之后所有主题切换正文/思考
内容颜色不刷新。修复：theme_manager.on_theme_changed() 接通 should_skip，
主题 ID 变化时推进版本号。
"""

from app.utils import theme_manager
from app.utils.theme_refresh import ThemeRefreshCoordinator


def test_theme_change_advances_coordinator_version(monkeypatch):
    """主题切换应推进 ThemeRefreshCoordinator 版本号（使注入短路失效）。"""
    fake = {"v": "themeA"}
    monkeypatch.setattr(theme_manager.theme_manager, "get_current_theme_id", lambda: fake["v"])

    v0 = ThemeRefreshCoordinator.get_version()

    # 首次主题变化 → 版本推进
    theme_manager.theme_manager.on_theme_changed()
    assert ThemeRefreshCoordinator.get_version() == v0 + 1

    # 同一主题重复回调 → 幂等，不推进
    theme_manager.theme_manager.on_theme_changed()
    assert ThemeRefreshCoordinator.get_version() == v0 + 1

    # 主题再次变化 → 继续推进
    fake["v"] = "themeB"
    theme_manager.theme_manager.on_theme_changed()
    assert ThemeRefreshCoordinator.get_version() == v0 + 2
