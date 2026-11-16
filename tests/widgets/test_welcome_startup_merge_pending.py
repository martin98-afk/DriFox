# -*- coding: utf-8 -*-
"""回归测试：新建会话时欢迎卡片刷新两次（2026-11-15）

== 现象（日志实锤 win_4567/win_4568）==
  16:57:13 _schedule_initial_welcome: 启动窗口合并去抖 600ms
  16:57:13 _show_initial_welcome OK        ← _notify_history_data_changed 立即渲染
  16:57:14 _on_welcome_render_slot fired   ← 去抖 timer 600ms 后再渲染
  16:57:14 _show_initial_welcome OK

== 根因 ==
启动窗口的去抖分支只 ``timer.start()``，排队的渲染凭证只存在于 timer 内部；
``_notify_history_data_changed`` 判「是否已有渲染在排」时只查
``_welcome_render_pending``，看不见去抖 timer → 立即补渲染一次，随后 timer
到期再渲染一次。

== 修复 ==
1. ``_show_initial_welcome`` 渲染落地后 ``_cancel_pending_welcome_render()``
   作废排队项（pending 清零 + 去抖 timer 停掉）
2. 槽位回调见到 pending 已被消费即跳过（第二道防线）
3. 去抖 timer 回调改为独立入口 ``_on_welcome_merge_timeout``（不参与消费判定）
"""

import time

import pytest

from app.main_widget import OpenAIChatToolWindow


class _FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)


class _FakeTimer:
    def __init__(self, parent=None):
        self.timeout = _FakeSignal()
        self.started = []
        self.active = False

    def setSingleShot(self, flag):
        pass

    def start(self, ms):
        self.started.append(ms)
        self.active = True

    def stop(self):
        self.active = False

    def isActive(self):
        return self.active


@pytest.fixture()
def fake_qtimer(monkeypatch):
    """patch PyQt5.QtCore.QTimer（去抖分支在函数内 import 该名字）"""
    import PyQt5.QtCore as QtCore

    created = []

    def factory(parent=None):
        t = _FakeTimer(parent)
        created.append(t)
        return t

    monkeypatch.setattr(QtCore, "QTimer", factory)
    return created


@pytest.fixture()
def capture_singleshot(monkeypatch):
    """patch app.main_widget.QTimer.singleShot（非去抖分支走它）"""
    calls = []

    def fake(msec, callback):
        calls.append((msec, callback))

    monkeypatch.setattr("app.main_widget.QTimer.singleShot", fake)
    return calls


def _make_stub(rendered_once: bool):
    """__new__ 绕过 __init__ 构造 stub；类属性写在派生类上避免污染父类"""
    stub_cls = type("_WelcomeMergeStub", (OpenAIChatToolWindow,), {})
    win = stub_cls.__new__(stub_cls)
    win._window_id = "win_merge"
    win._welcome_render_pending = False
    win._welcome_render_pending_since = None
    win._welcome_rendered_once = rendered_once
    win._welcome_hold_pending = False
    # __new__ 实例读未设置属性会抛 RuntimeError（PyQt wrapper 兜底），显式预置
    win._welcome_startup_merge_timer = None
    win._welcome_last_schedule_at = None
    win._welcome_slot = 0
    stub_cls._welcome_first_schedule_at = time.monotonic()
    return win


def test_merge_branch_starts_timer(fake_qtimer, capture_singleshot):
    """启动合并分支：走去抖 timer，不排 singleShot 槽位"""
    win = _make_stub(rendered_once=True)

    win._schedule_initial_welcome()

    assert len(fake_qtimer) == 1, "启动合并分支应创建去抖 timer"
    assert fake_qtimer[0].started == [OpenAIChatToolWindow._WELCOME_STARTUP_DEBOUNCE_MS]
    assert capture_singleshot == [], "去抖分支不应再调度 singleShot"
    assert fake_qtimer[0].timeout.slots, "timer.timeout 必须连到渲染回调"


def test_merge_branch_is_visible_to_cancel(fake_qtimer, capture_singleshot):
    """去抖 timer 必须能被 _cancel_pending_welcome_render 停掉

    这是双渲染的直接修法：``_notify_history_data_changed`` 会插队同步渲染一次，
    渲染落地即作废排队中的去抖 timer，否则 600ms 后 timer 再渲染一次。
    """
    win = _make_stub(rendered_once=True)

    win._schedule_initial_welcome()
    assert win._welcome_startup_merge_timer.isActive()

    win._cancel_pending_welcome_render()

    assert win._welcome_startup_merge_timer.isActive() is False


def test_repeated_schedule_restarts_same_timer(fake_qtimer, capture_singleshot):
    """去抖分支可重复重启（尾触语义），不新建 timer"""
    win = _make_stub(rendered_once=True)

    win._schedule_initial_welcome()
    win._schedule_initial_welcome()

    assert len(fake_qtimer) == 1, "重复调度应复用同一个去抖 timer（尾触去抖）"
    assert len(fake_qtimer[0].started) == 2, "尾触语义：每次调度都重启计时"


def test_show_cancels_queued_render(fake_qtimer, capture_singleshot):
    """渲染落地后作废排队项：pending 清零 + 去抖 timer 停止"""
    win = _make_stub(rendered_once=True)
    win._invalidate_welcome_card = lambda: None
    win._clear_chat_area = lambda **kw: None
    win._get_or_create_welcome_card = lambda: object()
    win._add_chat_widget = lambda w: None
    win._displayed_session_id = None
    win.session_manager = None  # 跳过「会话已有消息」守卫
    win._is_streaming = False
    win._welcome_build_hold = False

    win._schedule_initial_welcome()
    timer = win._welcome_startup_merge_timer
    assert timer.isActive()

    win._show_initial_welcome()

    assert timer.isActive() is False, "渲染落地后应停掉排队的去抖 timer"
    assert win._welcome_render_pending is False


def test_consumed_slot_skips_rerender(monkeypatch):
    """槽位回调见到 pending 已被消费（False）→ 跳过，不重复渲染"""
    win = _make_stub(rendered_once=True)
    rendered = []
    win._show_initial_welcome = lambda: rendered.append(1)
    win._welcome_render_pending = False  # 别的路径已完成渲染并作废

    win._on_welcome_render_slot()

    assert rendered == [], "pending 已被消费的槽位应直接作废"


def test_live_slot_renders(monkeypatch):
    """槽位回调见到 pending 有效（True）→ 正常渲染"""
    win = _make_stub(rendered_once=True)
    rendered = []
    win._show_initial_welcome = lambda: rendered.append(1)
    win._welcome_render_pending = True

    win._on_welcome_render_slot()

    assert rendered == [1]
    assert win._welcome_render_pending is False


def test_non_merge_branch_still_sets_pending(fake_qtimer, capture_singleshot):
    """回归保护：非去抖分支（新建会话常态）语义不变"""
    win = _make_stub(rendered_once=True)
    type(win)._welcome_first_schedule_at = 0.0  # 早于启动窗口 → 走普通槽位

    win._schedule_initial_welcome()

    assert win._welcome_render_pending is True
    assert len(capture_singleshot) == 1
    assert fake_qtimer == []
