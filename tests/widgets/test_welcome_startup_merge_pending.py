# -*- coding: utf-8 -*-
"""回归测试：「启动窗口合并去抖」分支必须同样占用 pending 守卫。

== 现象（2026-11-15 日志实锤）==
新建会话时欢迎卡片连续渲染两次：
  16:57:13 _schedule_initial_welcome: 启动窗口合并去抖 600ms
  16:57:13 _show_initial_welcome OK        ← _notify_history_data_changed 立即渲染
  16:57:14 _on_welcome_render_slot fired   ← 去抖 timer 600ms 后再渲染
  16:57:14 _show_initial_welcome OK

== 根因 ==
_schedule_initial_welcome 的「启动窗口合并去抖」分支只 timer.start()，没有
置 _welcome_render_pending=True。紧随其后的 _notify_history_data_changed
用 `not _welcome_render_pending` 判「是否已有待渲染」，看到 False 就同步
_show_initial_welcome() 渲染一次；600ms 后去抖 timer 再渲染一次。
（非去抖分支会置 pending，所以只有启动 20s 窗口内新建会话触发。）
"""

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
        self.single_shot = False

    def setSingleShot(self, flag):
        self.single_shot = flag

    def start(self, ms):
        self.started.append(ms)


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
    """__new__ 绕过 __init__ 构造 stub，类属性写在派生类上避免污染父类"""
    stub_cls = type("_WelcomeMergeStub", (OpenAIChatToolWindow,), {})
    win = stub_cls.__new__(stub_cls)
    win._window_id = "win_merge"
    win._welcome_render_pending = False
    win._welcome_render_pending_since = None
    win._welcome_rendered_once = rendered_once
    win._welcome_hold_pending = False
    # __new__ 实例读未设置属性会抛 RuntimeError（PyQt 属性兜底），去抖分支的
    # getattr(..., None) 会因此降级到普通分支 —— 显式预置 None 走真实路径
    win._welcome_startup_merge_timer = None
    win._welcome_last_schedule_at = None
    win._welcome_slot = 0
    # 启动窗口基准置为「刚刚」，确保走启动合并去抖分支
    stub_cls._welcome_first_schedule_at = __import__("time").monotonic()
    return win


def test_merge_branch_sets_pending(fake_qtimer, capture_singleshot):
    """去抖分支必须置 pending，否则 _notify_history_data_changed 会插队立即渲染"""
    win = _make_stub(rendered_once=True)

    win._schedule_initial_welcome()

    assert len(fake_qtimer) == 1, "启动合并分支应创建去抖 timer"
    assert fake_qtimer[0].started == [OpenAIChatToolWindow._WELCOME_STARTUP_DEBOUNCE_MS]
    assert win._welcome_render_pending is True, (
        "去抖分支未置 pending → _notify_history_data_changed 会立即渲染一次，"
        "600ms 后 timer 再渲染一次（欢迎卡刷新两次）"
    )
    assert win._welcome_render_pending_since is not None
    assert capture_singleshot == [], "去抖分支不应再调度 singleShot"


def test_merge_branch_pending_blocks_second_schedule(fake_qtimer, capture_singleshot):
    """去抖调度后紧接着的第二次调度应被 pending 守卫合并（不新建 timer）"""
    win = _make_stub(rendered_once=True)

    win._schedule_initial_welcome()
    win._schedule_initial_welcome()

    assert len(fake_qtimer) == 1, "第二次调度应被 pending 守卫拦下"


def test_non_merge_branch_still_sets_pending(fake_qtimer, capture_singleshot):
    """回归保护：非去抖分支（新建会话常态）语义不变"""
    win = _make_stub(rendered_once=True)
    type(win)._welcome_first_schedule_at = 0.0  # 早于启动窗口 → 走普通槽位

    win._schedule_initial_welcome()

    assert win._welcome_render_pending is True
    assert len(capture_singleshot) == 1
    assert fake_qtimer == []
