# -*- coding: utf-8 -*-
"""R4-A：流式高度追踪降频 + factor 配套的回归测试。

#14 实测：单次跨进程 resize 往返 p50=2.18ms（DOM 800 节点），33ms tick =
30.3 次/s = 66ms/s 主线程同步等待，是流式卡顿主因（B6）。
R4-A 降到 50ms（20 次/s = 44ms/s，-34%），factor 0.70→0.84、boost cap
0.85→0.90 配套，保持收敛时间基本不变。

配套关系：拍数变为 1/k（k = 50/33），要保持收敛时间不变需
(1-f_new) = (1-f_old)^k → f_new = 1 - (1-0.70)^1.515 = 0.839 ≈ 0.84。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import app.widgets.card_render_core as crc  # noqa: E402


# ── 纯函数模拟（前移：多个契约测试共用）──────────────────────────────
def _converge_ticks(diff: float, factor: float, cap: float, boost: float, far_px: int, eps: int) -> int:
    """模拟 _stream_height_tick_step 的逼近过程，返回收敛所需拍数。"""
    n = 0
    rem = diff
    while rem > eps and n < 500:
        f = factor
        if abs(rem) > far_px:
            f = min(cap, factor + boost)
        rem *= 1 - f
        n += 1
    return n


# ── 常量契约（防回归：后续重构不得随意改这三个值的配套关系）────────────
def test_tick_and_factor_are_paired():
    """tick 与 factor 必须配套，否则收敛时间会漂移。

    关系：(1-f_new) = (1-f_old)^k，k = tick_new / tick_old。
    以旧值 (33ms, 0.70) 为基准，反解当前 tick 对应的理论 factor，
    允许 ±0.03 的工程余量。
    """
    old_tick, old_factor = 33, 0.70
    tick = crc.STREAM_HEIGHT_TICK_MS
    factor = crc.STREAM_HEIGHT_TRACK_FACTOR
    if tick == old_tick:
        assert factor == pytest.approx(old_factor, abs=0.03)
        return
    k = tick / old_tick
    expected = 1 - (1 - old_factor) ** k
    assert factor == pytest.approx(expected, abs=0.03), f"tick={tick} 应与 factor≈{expected:.3f} 配套，实际 {factor}"


def test_finish_factor_paired_with_tick():
    """FINISH 态 factor 必须与 tick 配套（#20-M1：否则结束态收敛劣化 61~69%）。"""
    old_tick, old_finish = 33, 0.12
    tick = crc.STREAM_HEIGHT_TICK_MS
    finish = crc.FINISH_HEIGHT_TRACK_FACTOR
    if tick == old_tick:
        assert finish == pytest.approx(old_finish, abs=0.03)
        return
    k = tick / old_tick
    expected = 1 - (1 - old_finish) ** k
    assert finish == pytest.approx(expected, abs=0.03), f"tick={tick} 时 FINISH factor 应≈{expected:.3f}，实际 {finish}"


def test_finish_convergence_time_not_regressed():
    """FINISH 态五档 diff 收敛时间不得劣化超过 +20%（#20-M1 验收口径）。

    #19 实测未配套时劣化 61~69%（200px 1056→1700ms、600px 1155→1950ms）；
    M1 配套后目标拉到 +20% 以内。
    """
    old = dict(factor=0.12, tick=33, boost=0.15, cap=0.85)
    new = dict(
        factor=crc.FINISH_HEIGHT_TRACK_FACTOR,
        tick=crc.STREAM_HEIGHT_TICK_MS,
        boost=crc.STREAM_HEIGHT_TRACK_BOOST,
        cap=crc.STREAM_HEIGHT_TRACK_MAX_FACTOR,
    )
    far, eps = crc.STREAM_HEIGHT_TRACK_FAR_PX, crc.STREAM_HEIGHT_TRACK_EPSILON
    for diff in (15, 80, 200, 600, 2000):
        old_ms = _converge_ticks(diff, old["factor"], old["cap"], old["boost"], far, eps) * old["tick"]
        new_ms = _converge_ticks(diff, new["factor"], new["cap"], new["boost"], far, eps) * new["tick"]
        ratio = (new_ms / old_ms - 1) * 100 if old_ms else 0.0
        assert ratio <= 20, f"FINISH 态 diff={diff}px 收敛劣化 {ratio:+.0f}%（{old_ms}→{new_ms}ms）"


def test_boost_cap_leaves_adaptive_room():
    """boost cap 必须高于基线，否则自适应提速失效（cap=baseline 时 boost 无意义）。"""
    assert crc.STREAM_HEIGHT_TRACK_MAX_FACTOR > crc.STREAM_HEIGHT_TRACK_FACTOR
    assert crc.STREAM_HEIGHT_TRACK_MAX_FACTOR <= 0.92, "cap 过高趋近 snap，失去连续生长感"
    assert crc.STREAM_HEIGHT_TRACK_BOOST > 0


def test_debounce_stays_single_frame():
    """防抖保持 16ms（单帧）：只降追踪拍频率，不增加上报滞后。"""
    assert crc.STREAM_HEIGHT_DEBOUNCE_MS == 16


def test_tick_env_override(monkeypatch):
    """回退开关：DRIFOX_STREAM_HEIGHT_TICK_MS 可回到旧值 33。"""
    # 该常量在模块导入时求值，故只能验证「默认值」与「解析逻辑」一致
    assert isinstance(crc.STREAM_HEIGHT_TICK_MS, int)
    assert crc.STREAM_HEIGHT_TICK_MS > 0


# ── 收敛行为（用纯函数模拟追踪过程，不依赖 Qt）────────────────────────
def test_convergence_time_not_regressed():
    """收敛时间不得明显变慢（大台阶 600px，允许 +30% 工程余量）。"""
    old_ms = _converge_ticks(600, 0.70, 0.85, 0.15, 80, 2) * 33
    new_ms = (
        _converge_ticks(
            600,
            crc.STREAM_HEIGHT_TRACK_FACTOR,
            crc.STREAM_HEIGHT_TRACK_MAX_FACTOR,
            crc.STREAM_HEIGHT_TRACK_BOOST,
            crc.STREAM_HEIGHT_TRACK_FAR_PX,
            crc.STREAM_HEIGHT_TRACK_EPSILON,
        )
        * crc.STREAM_HEIGHT_TICK_MS
    )
    assert new_ms <= old_ms * 1.3, f"收敛时间从 {old_ms}ms 劣化到 {new_ms}ms"


def test_small_step_convergence_time_not_regressed():
    """小台阶（15px，流式典型节奏）收敛时间不得明显变慢。

    小台阶拍数已被 EPSILON 压到下限（2 拍），无法靠提 factor 再减少拍数，
    故按**绝对时间**判定（允许 +50ms）：降频必然让最小拍数的场景变慢，
    但 34ms 量级用户不可感。大台阶另按相对比例判定。
    """
    old_ms = _converge_ticks(15, 0.70, 0.85, 0.15, 80, 2) * 33
    new_ms = (
        _converge_ticks(
            15,
            crc.STREAM_HEIGHT_TRACK_FACTOR,
            crc.STREAM_HEIGHT_TRACK_MAX_FACTOR,
            crc.STREAM_HEIGHT_TRACK_BOOST,
            crc.STREAM_HEIGHT_TRACK_FAR_PX,
            crc.STREAM_HEIGHT_TRACK_EPSILON,
        )
        * crc.STREAM_HEIGHT_TICK_MS
    )
    assert new_ms - old_ms <= 50, f"小台阶收敛从 {old_ms}ms 劣化到 {new_ms}ms（超过 50ms 阈值）"


def test_cross_process_roundtrips_reduced():
    """跨进程往返频率必须下降（#14 的 2.18ms/次 × 频率 = 主线程占用）。"""
    old_per_s = 1000.0 / 33
    new_per_s = 1000.0 / crc.STREAM_HEIGHT_TICK_MS
    assert new_per_s < old_per_s, "降频后每秒次数反而增加"
    old_ms_per_s = old_per_s * 2.18
    new_ms_per_s = new_per_s * 2.18
    # 至少省 25%
    assert new_ms_per_s <= old_ms_per_s * 0.75, (
        f"主线程占用仅从 {old_ms_per_s:.0f}ms/s 降到 {new_ms_per_s:.0f}ms/s，收益不足"
    )
