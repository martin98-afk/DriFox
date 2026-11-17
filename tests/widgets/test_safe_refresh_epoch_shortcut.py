# -*- coding: utf-8 -*-
"""[I5] _safe_refresh 外观纪元短路回归：主题切换双刷消除

背景：batched 主题刷新里，EV_THEME_CHANGED 全局段派发
（UIPluginRegistry._on_theme_changed_event → ensure_theme_fresh）先于
per-window 5a/5b 块执行，且只打 `_drifox_theme_epoch`（外观纪元）；而
`_safe_refresh` 原有 `_refreshed_in_cycle` set 对 EV 链不可见 → 同一张
可见卡被 refresh_style 双刷（约 ×2 单批 141ms）。修复：_safe_refresh 开头
比对纪元，相等即短路。

四边界（不得破坏）：
①字体/字号变更无条件 bump 纪元 → EV 链刷过的卡短路，5b 不漏刷；
②隐藏卡 mark_theme_dirty 纪元落后 → 不短路，照常置脏；
③非 batched 单窗口路径不经 EV、纪元未 bump → 不短路照刷；
④showEvent 补刷走 ensure_theme_fresh 自身比对，闭环不变。
"""

import pytest

from app.utils.theme_refresh import ThemeRefreshCoordinator


class _FakeCard:
    """最小浮动卡桩：计数 refresh_style 调用；is_visible 可控"""

    _theme_needs_refresh = True  # 类级协议：有补刷链路（激活隐藏门控分支）

    def __init__(self, visible=True, epoch=None):
        self.visible = visible
        self.calls = 0
        if epoch is not None:
            self._drifox_theme_epoch = epoch

    def isVisible(self):
        return self.visible

    def refresh_style(self):
        self.calls += 1


class _FakeHost:
    """_safe_refresh 依赖的最小宿主状态（不构建真窗口）"""

    def __init__(self):
        self._refreshed_in_cycle = set()
        self._slow_refresh_cards = []


@pytest.fixture(autouse=True)
def _reset_coordinator():
    """纪元快照隔离：测后恢复原值（协调器是类级单例）"""
    snapshot = ThemeRefreshCoordinator.get_appearance_epoch()
    yield
    ThemeRefreshCoordinator._appearance_epoch = snapshot


def _refresh(host, card):
    from app.main_widget import OpenAIChatToolWindow

    OpenAIChatToolWindow._safe_refresh(host, card)


def test_epoch_match_short_circuits():
    """纪元相等（EV 链已刷过）→ refresh_style 不再被调（双刷消除）"""
    host = _FakeHost()
    ThemeRefreshCoordinator.bump_appearance_epoch()  # EV 链已把卡刷到当前纪元
    epoch = ThemeRefreshCoordinator.get_appearance_epoch()
    card = _FakeCard(visible=True, epoch=epoch)

    _refresh(host, card)
    _refresh(host, card)

    assert card.calls == 0, "纪元相等的卡必须短路，refresh_style 不得被调"
    assert (id(card), "refresh_style") not in host._refreshed_in_cycle, "短路发生在周期登记之前"


def test_epoch_stale_refreshes_normally():
    """纪元落后（隐藏期 mark_theme_dirty 的卡）→ 不短路，正常刷一次"""
    host = _FakeHost()
    ThemeRefreshCoordinator.bump_appearance_epoch()  # 当前纪元推进
    card = _FakeCard(visible=True, epoch=ThemeRefreshCoordinator.get_appearance_epoch() - 1)

    _refresh(host, card)

    assert card.calls == 1, "纪元落后的卡必须照刷"
    # 刷完打周期标（跨链去重语义不变）
    assert card._last_theme_refresh_cycle == ThemeRefreshCoordinator.get_refresh_cycle()


def test_epoch_absent_refreshes_normally():
    """无纪元标（非 batched 单窗口路径 / 旧卡）→ 不短路照刷（边界③）"""
    host = _FakeHost()
    card = _FakeCard(visible=True, epoch=None)

    _refresh(host, card)

    assert card.calls == 1


def test_hidden_card_with_epoch_still_marked_dirty():
    """隐藏卡且纪元落后：走置脏门控，不真刷（原隐藏门控行为不变，边界②）"""
    host = _FakeHost()
    ThemeRefreshCoordinator.bump_appearance_epoch()
    card = _FakeCard(visible=False, epoch=ThemeRefreshCoordinator.get_appearance_epoch() - 1)

    _refresh(host, card)

    assert card.calls == 0
    assert card._theme_needs_refresh is True


def test_font_size_epoch_bump_unblocks_refresh():
    """字体/字号变更 bump 纪元 → 上一纪元刷过的卡重新可刷（边界①）"""
    host = _FakeHost()
    ThemeRefreshCoordinator.bump_appearance_epoch()
    card = _FakeCard(visible=True, epoch=ThemeRefreshCoordinator.get_appearance_epoch())
    _refresh(host, card)
    assert card.calls == 0  # 短路

    ThemeRefreshCoordinator.bump_appearance_epoch()  # 字体/字号变更同样推进纪元
    _refresh(host, card)
    assert card.calls == 1, "新纪元下同一卡必须重新可刷（5b 不漏刷）"
