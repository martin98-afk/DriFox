# -*- coding: utf-8 -*-
"""alert_manager 三级告警单测（方案 5.3）。

覆盖：L1 RSS 斜率（喂序列+限频）/ L1 QTimer / L2 句柄漂移（InfoBar 事件 +
恢复不重弹）/ L3 governor 埋点 env 开关 / 默认无 env 时 L1 RSS 数据源独立。
全部直调内部评估方法（时钟注入），不起后台线程。
"""
from __future__ import annotations

import time

import pytest
from loguru import logger

from app.core.infra import alert_manager as am
from app.core.infra.alert_manager import AlertManager, _gdi_self


@pytest.fixture()
def mgr():
    return AlertManager()


@pytest.fixture()
def warnings():
    records = []
    sink = logger.add(lambda m: records.append(str(m)), level="INFO")
    yield records
    logger.remove(sink)


def _feed_rss_slope(m: AlertManager, now: float, slope_mb_h: float, hours: float = 2.0):
    """构造一条斜率 slope_mb_h 的 2h 窗序列并评估。"""
    series = [(now - hours * 3600 + i * 300, 500.0 + slope_mb_h * (i * 300) / 3600.0)
              for i in range(int(hours * 12) + 1)]
    m._rss_series.clear()
    m._rss_series.extend(series)
    m._evaluate_rss_slope(list(m._rss_series), now)


def test_l1_rss_slope_breach_and_warn(mgr, warnings):
    _feed_rss_slope(mgr, now=100000.0, slope_mb_h=8.0)
    _feed_rss_slope(mgr, now=100060.0, slope_mb_h=8.0)  # 连续第 2 次评估越限 → 触发
    assert any("[alert][L1]" in w and "rss_slope" in w for w in warnings), warnings


def test_l1_rss_slope_flat_no_breach(mgr, warnings):
    _feed_rss_slope(mgr, now=100000.0, slope_mb_h=1.0)
    _feed_rss_slope(mgr, now=100060.0, slope_mb_h=1.0)
    assert not [w for w in warnings if "[alert][L1]" in w]


def test_l1_emit_rate_limited_10min(mgr, warnings):
    now = 200000.0
    mgr._raise_l1("rss_slope", True, "snap#1", now)
    mgr._raise_l1("rss_slope", True, "snap#2", now + 60)      # 1min 后：限频抑制
    mgr._raise_l1("rss_slope", True, "snap#3", now + 590)     # 9.8min 后：仍抑制
    mgr._raise_l1("rss_slope", True, "snap#4", now + 601)     # 10min+1s：放行
    emitted = [w for w in warnings if "rss_slope" in w]
    assert len(emitted) == 2, warnings
    assert "snap#1" in emitted[0] and "snap#4" in emitted[1]


def test_l1_qtimer_24h_delta(mgr, warnings, monkeypatch):
    now = 300000.0
    m = mgr
    monkeypatch.setattr(am, "_count_qtimers", lambda: 155)
    m._qtimer_series.clear()
    m._qtimer_series.extend([(now - 24 * 3600, 100), (now - 12 * 3600, 130), (now, 155)])
    m.tick(now + 1)
    assert any("qtimer_24h" in w and "+55" in w for w in warnings), warnings


def test_l2_handle_drift_requires_three_consecutive(mgr, monkeypatch):
    now = 400000.0
    monkeypatch.setattr(am, "_gdi_self", lambda: (360, 5))
    mgr._gdi_series.append((now - 600, 300))  # 10min 前基点
    for k in range(2):  # 仅 2 个连续越限周期：不足 _L2_CONSECUTIVE
        mgr.tick(now + k * 60)
    assert mgr._gdi_breach_streak < 3


def test_l2_handle_drift_emits_infobar(mgr, warnings, monkeypatch):
    now = 500000.0
    monkeypatch.setattr(am, "_gdi_self", lambda: (360, 5))
    m = mgr
    posted = []
    m.set_info_bar_poster(lambda title, msg: posted.append((title, msg)))
    m._gdi_series.append((now - 600, 300))  # 10min 前基点
    for k in range(3):
        m.tick(now + k * 60)
    assert any(t == "内存压力" for t, _ in posted), posted
    assert any("handle_drift" in w for w in warnings)


def test_l2_recovered_after_5min_no_repost(mgr, warnings, monkeypatch):
    now = 600000.0
    gdi_state = {"v": 360}
    monkeypatch.setattr(am, "_gdi_self", lambda: (gdi_state["v"], 5))
    m = mgr
    posted = []
    m.set_info_bar_poster(lambda title, msg: posted.append((title, msg)))
    m._gdi_series.append((now - 600, 300))
    for k in range(3):  # 3 连续越限周期 → InfoBar 触发
        m.tick(now + k * 60)
    n_first = len(posted)
    assert n_first >= 1
    # 恢复：gdi 回落正常值，持续 5min+ → 状态机清 active，不重弹 InfoBar
    gdi_state["v"] = 300
    for k in range(6):
        m.tick(now + 600 + k * 60)
    assert len(posted) == n_first, "恢复后不得重弹 InfoBar"
    assert m._states["handle_drift"]["active"] == 0.0
    assert any("[alert][已恢复]" in w for w in warnings), warnings


def test_l3_governor_trace_env_gate(monkeypatch):
    captured = []
    monkeypatch.setattr(am, "append_event", lambda *a, **k: captured.append((a, k)))
    monkeypatch.setenv("DRIFOX_GOVERNOR_TRACE", "1")
    am.note_governor_action("pre_kill", "deficit=300MB")
    assert captured, "env 开时应落盘（append_event 被调用）"
    assert captured[0][0][0] == "governor_action"

    captured.clear()
    monkeypatch.delenv("DRIFOX_GOVERNOR_TRACE", raising=False)
    am.note_governor_action("pre_kill", "probe")
    assert not captured, "env 关时应静默跳过"


def test_default_no_env_l1_rss_still_works(mgr, warnings, monkeypatch):
    """默认（无任何观测 env）：L1 RSS 斜率仍工作（数据源独立于埋点 env）。"""
    for k in ("DRIFOX_EL_METRICS", "DRIFOX_HANDLE_TRACE", "PERF_TRACE_MALLOC",
              "DRIFOX_GOVERNOR_TRACE"):
        monkeypatch.delenv(k, raising=False)
    _feed_rss_slope(mgr, now=700000.0, slope_mb_h=9.0)
    _feed_rss_slope(mgr, now=700060.0, slope_mb_h=9.0)
    assert any("[alert][L1] rss_slope" in w for w in warnings)


def test_gdi_self_smoke():
    r = _gdi_self()
    assert r is None or (isinstance(r, tuple) and len(r) == 2)
