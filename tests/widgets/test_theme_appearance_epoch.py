# -*- coding: utf-8 -*-
"""插件页外观纪元：隐藏期只置脏、显示时补刷（主题/字体切换自愈）

背景
----
插件页样式是构造期 f-string 固化的（``f"color: {Colors.TEXT_PRIMARY}"``），
写进控件后与 ``Colors`` 脱钩。历史做法是主题切换时**全量无条件刷**所有插件页
（含隐藏页、其他窗口的卡），代价实测单次 141ms（UIPluginRegistry 派发），
换来的是「隐藏页也不会停旧主题」。

代价之外还有漏洞：只要某条派发没命中（方法名不匹配 / 插件自己的 show_card
忘了刷 / 新增容器没接派发），该页就永久停在旧主题，**没有自愈路径**。

本文件锁定新机制：
1. 外观纪元随每次外观变更推进，可见页立即刷并记纪元，隐藏页只置脏；
2. 页面再次可见时 ``ensure_theme_fresh`` 按纪元比对补刷一次（幂等）；
3. 主题切换成本与隐藏页数量无关（隐藏页零 repolish）。

探针实测（tests 外，离屏）：SystemCleanerCard 的 ``_apply_latest_theme`` 本身
可用（light→dark 会真的改行文字色），但「仅 setVisible(True) 不重刷」不会自愈
——本机制补的正是这一段。
"""

from unittest.mock import MagicMock

import pytest

from app.utils import theme_manager as tm_module
from app.utils.theme_refresh import ThemeRefreshCoordinator
from app.utils.theme_style import ensure_theme_fresh, mark_theme_dirty, stamp_theme_epoch


class _FakePage:
    """最小插件页桩：只记录 refresh_style 调用次数"""

    def __init__(self):
        self.calls = 0

    def refresh_style(self):
        self.calls += 1

    def isVisible(self):
        return True


@pytest.fixture()
def bump_epoch():
    """推进外观纪元并返回新值（模拟一次主题/字体变更）"""
    return ThemeRefreshCoordinator.bump_appearance_epoch()


def test_epoch_advances_on_theme_change():
    """on_theme_changed 必须推进纪元（batched 每次外观变更都调它一次）"""
    before = ThemeRefreshCoordinator.get_appearance_epoch()
    tm_module.theme_manager.on_theme_changed()
    after = ThemeRefreshCoordinator.get_appearance_epoch()
    assert after != before, "外观纪元未随主题变更推进 → 隐藏页永远等不到补刷"


def test_hidden_page_dirty_then_refresh_on_show():
    """隐藏期置脏 → 显示时补刷一次 → 再显示不重复刷（纪元比对）"""
    page = _FakePage()
    stamp_theme_epoch(page)
    assert page.calls == 0

    ThemeRefreshCoordinator.bump_appearance_epoch()
    mark_theme_dirty(page)
    assert page.calls == 0, "置脏阶段不应执行任何刷新（隐藏页零成本）"

    assert ensure_theme_fresh(page) is True
    assert page.calls == 1, "显示补刷必须执行且只执行一次"

    assert ensure_theme_fresh(page) is False
    assert page.calls == 1, "同一纪元内重复补刷必须短路"


def test_visible_page_refreshes_immediately():
    """可见页：ensure_theme_fresh 直接刷，无需先置脏"""
    page = _FakePage()
    ThemeRefreshCoordinator.bump_appearance_epoch()
    assert ensure_theme_fresh(page) is True
    assert page.calls == 1


def test_font_change_also_advances_epoch():
    """字体/字号变更同样走 on_theme_changed → 纪元推进

    若复用 ThemeRefreshCoordinator.get_version()（只在主题 id 变化时推进），
    字体切换产生的脏页会永远等不到补刷——这是选独立计数器的原因。
    """
    before = ThemeRefreshCoordinator.get_appearance_epoch()
    tm_module.theme_manager.on_theme_changed()
    assert ThemeRefreshCoordinator.get_appearance_epoch() != before


def test_registry_skips_hidden_card_but_marks_dirty(monkeypatch):
    """UIPluginRegistry 派发：可见卡刷、隐藏卡只置脏不刷"""
    from app.plugins.registries import ui_plugin_registry as uir_module

    reg = uir_module.UIPluginRegistry.get_instance()
    original = reg._card_widget_instances

    visible = MagicMock()
    visible.isVisible.return_value = True
    visible.refresh_style = MagicMock()
    hidden = MagicMock()
    hidden.isVisible.return_value = False
    hidden.refresh_style = MagicMock()
    reg._card_widget_instances = {"w1": {"card_vis": visible, "card_hid": hidden}}
    try:
        ThemeRefreshCoordinator.bump_appearance_epoch()
        reg._on_theme_changed_event({"theme_id": "t2"})
    finally:
        reg._card_widget_instances = original

    assert visible.refresh_style.call_count >= 1, "可见卡必须立即刷新"
    assert hidden.refresh_style.call_count == 0, (
        f"隐藏卡不应在主题切换时刷新（白付整树 repolish），实际调用 "
        f"{hidden.refresh_style.call_count} 次"
    )
    assert getattr(hidden, "_drifox_theme_epoch", "missing") is None, "隐藏卡必须被置脏"
