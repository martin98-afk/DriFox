# -*- coding: utf-8 -*-
"""历史会话首屏一次揭示：状态机 + 宿主采集口径 单测。

覆盖四件容易回归的事：
1. 揭示条件必须是「屏内正文落地 + 首屏几何稳定 + 整屏高度静止 + 贴底」，懒渲染
   标志（``_lazy_rendered``）不得被当成正文就绪；
2. 屏外卡片不参与「屏内落地」判定（否则揭示会被整段历史拖住），但整屏高度
   静止是必须的（否则屏外落高会把可见内容顶走），且两者都有超时出口；
3. 慢卡超时、切会话取消、主题重刷、用户自己滚开 都有明确出口，不会永久
   停在提示页，也不会把上一轮等待的结论套到新会话上；
4. 宿主采集口径：几何用**屏幕坐标**算（贴底跟随时上方落高不改变用户视野）。
"""

import types
from unittest import mock

import pytest

import app.main_widget as mw
import app.widgets.history_first_paint as hfp
from app.widgets.history_first_paint import HistoryFirstPaintGate


# ── 假时钟 ────────────────────────────────────────────────
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def advance_ms(self, ms):
        self.t += ms / 1000.0


@pytest.fixture()
def clock():
    c = _Clock()
    with mock.patch.object(hfp, "time", types.SimpleNamespace(monotonic=c.monotonic)):
        yield c


class _Host:
    """状态机宿主替身：只提供三个回调。"""

    def __init__(self):
        self.parent_widget = None
        self.state = {}
        self.reveals = []

    def cover_parent(self):
        return self.parent_widget

    def state_provider(self):
        return dict(self.state) if self.state is not None else None

    def on_reveal(self, stick_bottom):
        self.reveals.append(bool(stick_bottom))


def _make_gate(qtbot, host):
    """建一个挂在真实 QWidget 上的状态机（遮罩是真 QLabel，需要 QApplication）。"""
    from PyQt5.QtWidgets import QWidget

    holder = QWidget()
    qtbot.addWidget(holder)
    holder.resize(400, 300)
    host.parent_widget = holder
    return HistoryFirstPaintGate(
        cover_parent=host.cover_parent,
        state_provider=host.state_provider,
        on_reveal=host.on_reveal,
    )


def _state(
    session_id="s1",
    visible=2,
    pending=0,
    unrendered=0,
    at_bottom=True,
    away=False,
    geometry=("g", 1),
    layout_height=1000,
):
    return {
        "session_id": session_id,
        "visible_cards": visible,
        "pending_cards": pending,
        "unrendered_cards": unrendered,
        "at_bottom": at_bottom,
        "away": away,
        "visible_geometry": geometry,
        "layout_height": layout_height,
    }


def _tick(gate, clock, host, **kw):
    """按轮询节拍推进一拍（假时钟同步前进）。"""
    if kw:
        host.state = _state(**kw)
    clock.advance_ms(hfp.POLL_INTERVAL_MS)
    gate._tick()


def _quiet_ticks(needed_ms=None):
    """覆盖整屏静默保持所需的拍数（多给一拍容差）。"""
    need = hfp.LAYOUT_QUIET_HOLD_MS if needed_ms is None else needed_ms
    return int(need // hfp.POLL_INTERVAL_MS) + 2


# ── 状态机 ────────────────────────────────────────────────
def test_reveal_requires_settled_cards_and_stable_geometry(qtbot, clock):
    host = _Host()
    gate = _make_gate(qtbot, host)

    gate.begin("s1")
    assert gate.is_waiting and gate.cover_visible(), "历史重建必须立刻上罩（赶在首绘前）"

    host.state = _state(pending=2)
    for _ in range(_quiet_ticks()):
        _tick(gate, clock, host)
    assert gate.is_waiting, "屏内还有未落地卡片时不得揭示"
    assert host.reveals == []

    # 屏内落地，但几何刚变：先等两拍确认不是布局中间态
    _tick(gate, clock, host, pending=0, geometry=("g", 2))
    assert gate.is_waiting

    for _ in range(_quiet_ticks()):
        _tick(gate, clock, host)
    assert host.reveals == [True], "屏内落地 + 几何稳定 + 整屏静止 + 贴底 → 一次揭示"
    assert not gate.is_waiting
    assert not gate.cover_visible(), "揭示必须撤罩"


def test_geometry_change_resets_stability(qtbot, clock):
    """首屏几何还在变（卡片落高 / 位置移动）：不得揭示。"""
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state()
    gate.begin("s1")

    for i in range(_quiet_ticks() * 2):
        _tick(gate, clock, host, geometry=("g", i))
        _tick(gate, clock, host, layout_height=1000 + i)
    assert gate.is_waiting and host.reveals == [], "几何每拍都变时不得揭示"

    for _ in range(_quiet_ticks()):
        _tick(gate, clock, host, geometry=("g", 9), layout_height=1300)
    assert host.reveals == [True]


def test_layout_growth_defers_reveal_until_quiet(qtbot, clock):
    """屏内已就绪但屏外还在落高：整屏高度每拍都变，禁止揭示（会顶走可见内容）。"""
    host = _Host()
    gate = _make_gate(qtbot, host)
    gate.begin("s1")
    for i in range(_quiet_ticks() * 2):  # 持续落高
        _tick(gate, clock, host, layout_height=1000 + 100 * i)
    assert host.reveals == [], "整屏高度还在变时不得揭示"

    for _ in range(_quiet_ticks()):
        _tick(gate, clock, host, layout_height=1000 + 100 * 30)
    assert host.reveals == [True], "落高收尾（整屏静止）后立即揭示"


def test_layout_growth_reveals_at_hard_timeout(qtbot, clock):
    """整屏高度长时间不停（渲染持续抖动）：硬超时按期揭示，不无限遮挡聊天区。"""
    host = _Host()
    gate = _make_gate(qtbot, host)
    gate.begin("s1")
    for i in range(400):
        _tick(gate, clock, host, layout_height=1000 + i)
        if host.reveals:
            break
    assert host.reveals == [True]
    elapsed_ms = (clock.monotonic() - 1000.0) * 1000.0
    assert elapsed_ms >= hfp.MAX_WAIT_MS - hfp.POLL_INTERVAL_MS
    assert not gate.cover_visible()


def test_not_at_bottom_blocks_reveal(qtbot, clock):
    """贴底是条件之一：视口没贴底就不揭示（否则揭示瞬间用户看到的是中段）。"""
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state(at_bottom=False)
    gate.begin("s1")
    for _ in range(_quiet_ticks() * 2):
        _tick(gate, clock, host)
    assert gate.is_waiting and host.reveals == []


def test_timeout_reveals_when_cards_never_settle(qtbot, clock):
    """慢卡 / 从未派发 viewer 的卡片：硬超时兜底揭示，不得永久遮挡聊天区。"""
    host = _Host()
    gate = _make_gate(qtbot, host)
    gate.begin("s1")
    for i in range(400):
        # viewer 一直没派发（unrendered>0）→ 禁止「静止」兜底，只能等硬超时
        _tick(gate, clock, host, pending=3, unrendered=3, geometry=("g", i))
        if host.reveals:
            break
    assert host.reveals == [True], "超时后必须揭示"
    assert not gate.cover_visible()


def test_quiet_fallback_for_dispatched_but_unsettled_card(qtbot, clock):
    """viewer 已建但高度始终落不下（如正文极短停在起步高度）：静止若干拍即揭示。"""
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state(pending=1, unrendered=0, geometry="same")
    gate.begin("s1")

    for _ in range(hfp._QUIET_TICKS):  # 攒够静止拍数
        _tick(gate, clock, host)
    assert host.reveals == [], "静止拍数未攒够前不得揭示"
    assert (clock.monotonic() - 1000.0) * 1000.0 < hfp.MAX_WAIT_MS, "本用例应在硬超时之前触发"

    _tick(gate, clock, host)
    assert host.reveals == [True], "屏内高度长时间不动 → 视为落地失败，兜底揭示"


def test_session_switch_cancels_without_reveal(qtbot, clock):
    """旧一轮等待绝不能把新会话揭出来。"""
    host = _Host()
    gate = _make_gate(qtbot, host)
    gate.begin("old")
    for _ in range(3):
        _tick(gate, clock, host, session_id="old", pending=1)
    assert gate.is_waiting

    _tick(gate, clock, host, session_id="new", geometry=("g", 9))
    assert not gate.is_waiting
    assert host.reveals == [], "会话已切走：静默作废，不触发揭示"
    assert not gate.cover_visible()


def test_user_scroll_reveals_without_forcing_bottom(qtbot, clock):
    """用户自己滚开看历史：立刻揭示且不再强行贴底。"""
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state(pending=5, away=True)
    gate.begin("s1")
    _tick(gate, clock, host)
    assert host.reveals == [False]
    assert not gate.cover_visible()


def test_hint_text_only_appears_when_slow(qtbot, clock):
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state(pending=1)
    gate.begin("s1")
    assert gate.hint_text() == "", "上罩瞬间只有底色，不闪提示文字"

    _tick(gate, clock, host)
    _tick(gate, clock, host)
    assert gate.hint_text() == "", "提示文字要等 HINT_TEXT_DELAY_MS 之后才出现"

    for _ in range(_quiet_ticks(hfp.HINT_TEXT_DELAY_MS)):
        _tick(gate, clock, host)
    assert gate.hint_text() == hfp.HINT_TEXT


def test_fast_load_never_shows_hint(qtbot, clock):
    """快加载（一个静默保持周期内就绪）不该闪一行提示文字。"""
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state(pending=0)
    gate.begin("s1")
    for _ in range(_quiet_ticks()):
        _tick(gate, clock, host)
        if host.reveals:
            break
    assert host.reveals == [True]
    assert (clock.monotonic() - 1000.0) * 1000.0 < hfp.HINT_TEXT_DELAY_MS, "本用例应早于提示阈值"
    assert gate.hint_text() == ""


def test_theme_change_restyles_pending_cover(qtbot, clock):
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state(pending=1)
    gate.begin("s1")
    _tick(gate, clock, host)

    with mock.patch.object(hfp, "_hint_color", lambda: "rgb(1, 2, 3)"):
        gate.on_theme_changed()
    assert "rgb(1, 2, 3)" in gate.cover_style(), "等待中的遮罩必须按新主题重刷配色"


def test_cancel_removes_cover_and_is_idempotent(qtbot, clock):
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state(pending=1)
    gate.begin("s1")
    assert gate.cancel() is True
    assert not gate.cover_visible()
    assert gate.cancel() is False
    gate._tick()
    assert host.reveals == []


def test_begin_twice_keeps_single_wait(qtbot, clock):
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state(session_id="a", pending=1)
    gate.begin("a")
    gate.begin("b")
    assert gate.session_id == "b"
    assert gate.is_waiting
    assert gate.cover_visible()


def test_disabled_switch_keeps_legacy_behaviour(qtbot, clock):
    host = _Host()
    gate = _make_gate(qtbot, host)
    host.state = _state(pending=0)
    with mock.patch.object(hfp, "ENABLED", False):
        gate.begin("s1")
        assert not gate.is_waiting
        assert not gate.cover_visible()
        gate._tick()
    assert host.reveals == []


# ── 宿主采集口径（main_widget._history_first_paint_state）────────
class _FakeCard:
    def __init__(self, y, height, settled, role="assistant", lazy=True):
        self._y = y
        self._h = height
        self._settled = settled
        self.role = role
        self._lazy_rendered = lazy

    def y(self):
        return self._y

    def height(self):
        return self._h

    def is_layout_height_settled(self):
        return self._settled


class _Item:
    def __init__(self, widget):
        self._w = widget

    def widget(self):
        return self._w


class _Layout:
    def __init__(self, widgets):
        self._widgets = list(widgets)

    def count(self):
        return len(self._widgets)

    def itemAt(self, index):
        w = self._widgets[index]
        return _Item(w) if w is not None else None


class _Container:
    def __init__(self, height):
        self._h = height

    def height(self):
        return self._h


class _Area:
    def __init__(self, value, viewport_height, container_height=2000):
        self._value = value
        self._vh = viewport_height
        self._bar = mock.MagicMock()
        self._bar.value.return_value = value
        self._vp = mock.MagicMock()
        self._vp.height.return_value = viewport_height
        self._container = _Container(container_height)

    def verticalScrollBar(self):
        return self._bar

    def viewport(self):
        return self._vp

    def widget(self):
        return self._container


def _collector_host(widgets, value=100, viewport_height=400, at_bottom=True, away=False, container_height=2000):
    win = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    win.chat_scroll_area = _Area(value, viewport_height, container_height)
    win.chat_layout = _Layout(widgets)
    win.session_manager = mock.MagicMock()
    win.session_manager.get_current_session.return_value = types.SimpleNamespace(session_id="s1")
    win._user_intentionally_away_from_bottom = away
    win._is_view_at_bottom = lambda tolerance=10: at_bottom
    return win


def test_state_ignores_offscreen_cards():
    """屏外未落地卡片不得计入 pending（否则揭示被整段历史拖住）。"""
    cards = [
        _FakeCard(y=0, height=50, settled=False, lazy=False),  # 完全在视口上方
        _FakeCard(y=150, height=120, settled=True),
        _FakeCard(y=300, height=120, settled=True),
    ]
    win = _collector_host(cards, value=100, viewport_height=400)
    with mock.patch.object(mw, "MessageCard", _FakeCard):
        state = win._history_first_paint_state()
    assert state["visible_cards"] == 2
    assert state["pending_cards"] == 0
    assert state["at_bottom"] is True


def test_state_pending_uses_settled_not_lazy_flag():
    """`_lazy_rendered` 已置位 ≠ 正文落地：pending 必须按 is_layout_height_settled 判。"""
    cards = [
        _FakeCard(y=100, height=200, settled=False, lazy=True, role="assistant"),
        _FakeCard(y=350, height=80, settled=False, lazy=False, role="assistant"),
        _FakeCard(y=380, height=60, settled=False, lazy=False, role="user"),
    ]
    win = _collector_host(cards, value=0, viewport_height=500)
    with mock.patch.object(mw, "MessageCard", _FakeCard):
        state = win._history_first_paint_state()
    assert state["visible_cards"] == 3
    assert state["pending_cards"] == 3
    # 只有「viewer 还没派发」的 assistant 卡计入 unrendered；
    # user 卡同步渲染，永远不进该计数（否则静止兜底会永久失效）
    assert state["unrendered_cards"] == 1


def test_state_geometry_uses_screen_coordinates():
    """贴底跟随时上方内容增长只改变容器 y，不改变屏幕位置 → 几何签名不变。"""
    card = _FakeCard(y=600, height=200, settled=True)
    win = _collector_host([card], value=600, viewport_height=400, container_height=1200)
    with mock.patch.object(mw, "MessageCard", _FakeCard):
        state_a = win._history_first_paint_state()
        # 上方旧卡片落了 300px：容器内 y 与滚动值同步 +300，屏幕位置不变
        card._y = 900
        win.chat_scroll_area = _Area(900, 400, container_height=1500)
        state_b = win._history_first_paint_state()
    assert state_a["visible_geometry"] == state_b["visible_geometry"]
    assert state_a["visible_cards"] == 1
    assert state_a["layout_height"] == 1200
    assert state_b["layout_height"] == 1500


def test_state_returns_none_when_geometry_unavailable():
    """窗口未布局（viewport 高度 0）→ 采集失败，状态机只等硬超时。"""
    win = _collector_host([_FakeCard(y=0, height=100, settled=True)], value=0, viewport_height=0)
    with mock.patch.object(mw, "MessageCard", _FakeCard):
        assert win._history_first_paint_state() is None


def test_state_reports_away_and_session():
    win = _collector_host([_FakeCard(y=0, height=100, settled=True)], away=True)
    with mock.patch.object(mw, "MessageCard", _FakeCard):
        state = win._history_first_paint_state()
    assert state["away"] is True
    assert state["session_id"] == "s1"


# ── 接线守卫（不构造真实主窗口）────────────────────────────
def test_cancel_before_any_begin_is_noop():
    """`_display_current_session` 入口无条件调 cancel：未装配时不得抛错。"""
    win = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    win._cancel_history_first_paint()
    win._history_first_paint_theme_changed()


def test_begin_without_chat_area_does_not_wait():
    """遮罩父控件不可用（早期装配 / 无滚动区）时应静默跳过等待。"""
    host = _Host()
    host.parent_widget = None
    gate = HistoryFirstPaintGate(
        cover_parent=host.cover_parent,
        state_provider=host.state_provider,
        on_reveal=host.on_reveal,
    )
    host.state = _state(pending=0)
    gate.begin("s1")
    assert gate.is_waiting is False
    assert gate.cover_visible() is False


def test_wiring_on_uninitialized_window_does_not_raise():
    """未跑 __init__ 的 PyQt 测试桩上接线必须静默退化（不得中断会话显示主链）。"""
    win = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    win._begin_history_first_paint("s1")
    win._cancel_history_first_paint()
    win._history_first_paint_theme_changed()
    win._reveal_history_first_paint(True)
