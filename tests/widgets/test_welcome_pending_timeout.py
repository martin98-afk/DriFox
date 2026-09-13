# -*- coding: utf-8 -*-
"""回归测试：_schedule_initial_welcome 的 pending 守卫超时兜底。

根因（win_676 案例，2026-09-02）：pending 守卫依赖 slot 回调必然执行来复位
``_welcome_render_pending``；插件热重载风暴中 QTimer 回调可能丢失（日志实锤：
21:58:01 调度 slot=3 后无 fired 记录），pending 永久 True，此后该窗口所有
欢迎卡片重建请求被永久拦截 → 插件启停/更新后欢迎卡片无法刷新出来。

修复：pending 记录时间戳，超过 _WELCOME_PENDING_TIMEOUT_S 未回调即视为
泄漏，强制放行重建。
"""

import time

import pytest

from app.main_widget import OpenAIChatToolWindow

_TEST_WID = "win_test"


def _make_stub_win(pending: bool, since: float):
    """__new__ 绕过 __init__ 构造 stub 窗口（对齐 _schedule_initial_welcome 的 stub 兼容路径）"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._window_id = _TEST_WID
    win._welcome_render_pending = pending
    win._welcome_render_pending_since = since
    return win


@pytest.fixture()
def capture_singleshot(monkeypatch):
    """捕获 QTimer.singleShot 调用（不触发真实定时器）"""
    calls = []

    def _fake(msec, callback):
        calls.append((msec, callback))

    monkeypatch.setattr("app.main_widget.QTimer.singleShot", _fake)
    return calls


def test_stale_pending_forces_rebuild(capture_singleshot):
    """pending 超时（>3s 未回调）→ 强制放行重建（回调丢失熔断修复）。"""
    win = _make_stub_win(pending=True, since=time.monotonic() - 10.0)
    win._schedule_initial_welcome()
    assert len(capture_singleshot) == 1, "超时 pending 应放行，调度 singleShot 重建"
    # 放行后 pending 重置时间戳
    assert win._welcome_render_pending is True
    assert time.monotonic() - win._welcome_render_pending_since < 1.0


def test_fresh_pending_skips(capture_singleshot):
    """新鲜 pending（<3s）→ 维持去重语义，跳过本次调度。"""
    win = _make_stub_win(pending=True, since=time.monotonic())
    win._schedule_initial_welcome()
    assert capture_singleshot == [], "新鲜 pending 应跳过，不重复调度"


def test_no_pending_schedules(capture_singleshot):
    """无 pending → 正常调度（回归保护：不破坏原有去重语义）。"""
    win = _make_stub_win(pending=False, since=0.0)
    win._schedule_initial_welcome()
    assert len(capture_singleshot) == 1
