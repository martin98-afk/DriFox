# -*- coding: utf-8 -*-
"""pytest 入口：长时运行时压测。

用法：
    pytest tests/perf/long_run/test_long_run_scenarios.py -v -m perf_long

行为：
- 单场景运行时长 = 环境变量 `LONGRUN_DURATION`（秒），默认 10s（保证 <30s CI 友好）
- Demo 模式（默认）：每场景 10s
- Full 模式（LONGRUN_FULL=1）：每场景 30min
- 全部断言保证 leak rate < 50 MB/h（宽松阈值，超出仅 warn 不 fail）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from .runner import (
    DEMO_DURATION_SEC,
    FULL_DURATION_SEC,
    SCENARIO_REGISTRY,
    _ensure_qapp,
    _register_scenarios,
    render_markdown_report,
    run_all_scenarios,
    _ts,
)
from .sampler import (
    Sample,
    start_tracemalloc,
    stop_tracemalloc,
    take_sample,
)


def _compute_growth_rate(samples):
    """复制 runner._compute_growth_rate 逻辑（避免私有 import）。"""
    if len(samples) < 2:
        return {}
    xs = [s.elapsed_sec for s in samples]
    rss = [s.rss_mb for s in samples]

    def _linreg(xs_, ys_):
        n = len(xs_)
        mx = sum(xs_) / n
        my = sum(ys_) / n
        num = sum((xs_[i] - mx) * (ys_[i] - my) for i in range(n))
        den = sum((xs_[i] - mx) ** 2 for i in range(n))
        return num / den if den > 0 else 0.0

    rss_rate_per_sec = _linreg(xs, rss)
    return {"rss_mb_per_hour": rss_rate_per_sec * 3600}


def _classify_leak(rate_mb_per_hour: float, duration_min: float) -> str:
    if rate_mb_per_hour < 5.0:
        return "stable"
    if rate_mb_per_hour < 50.0:
        return "watch"
    return "suspect_leak"


pytestmark = pytest.mark.perf_long

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = REPO_ROOT / "docs" / "perf"


def _pick_duration() -> float:
    """根据环境变量选时长。"""
    env_dur = os.environ.get("LONGRUN_DURATION")
    if env_dur:
        try:
            return float(env_dur)
        except ValueError:
            pass
    return FULL_DURATION_SEC if os.environ.get("LONGRUN_FULL") else DEMO_DURATION_SEC


@pytest.fixture(scope="module")
def qapp():
    """PyQt5 QApplication 单例。"""
    return _ensure_qapp()


@pytest.fixture(scope="module", autouse=True)
def _register():
    _register_scenarios()


def test_scenario_a_message_stream_leak(qapp):
    """场景 A：消息流压测的内存增长应低于阈值。"""
    duration = _pick_duration()

    samples_holder = {}

    def _cb(i: int, s: Sample) -> None:
        samples_holder[i] = s

    summary = SCENARIO_REGISTRY["a"](progress_cb=_cb, duration_sec=duration)
    samples = summary["samples"]
    rates = _compute_growth_rate(samples)
    rate = rates.get("rss_mb_per_hour", 0.0)
    classification = _classify_leak(rate, duration / 60.0)
    # demo 模式只断言不崩 + 不超阈值；不强制 fail（CI 太慢）
    assert summary["iterations"] > 0, "场景 A 必须至少跑出 1 次操作"
    assert classification in ("stable", "watch"), (
        f"场景 A 内存增长速率 {rate:.2f} MB/h 触发 suspect_leak 阈值（≥50 MB/h）"
    )


# ── 场景 B / C 合并到 test_scenario_b_and_c_run_all_smoke ──
#  单一 pytest 集中跑 B + C，避免每个用例都构造 PyQt5 app（PyQt5 单例约束）


@pytest.mark.parametrize("scenario", ["b", "c"])
def test_scenario_b_c_leak(qapp, scenario):
    """场景 B / C：会话切换 / 插件热重载的内存增长应低于阈值。"""
    duration = _pick_duration()

    def _cb(i: int, s: Sample) -> None:
        pass

    summary = SCENARIO_REGISTRY[scenario](progress_cb=_cb, duration_sec=duration)
    samples = summary["samples"]
    rates = _compute_growth_rate(samples)
    rate = rates.get("rss_mb_per_hour", 0.0)
    classification = _classify_leak(rate, duration / 60.0)
    assert summary["iterations"] > 0, f"场景 {scenario} 必须至少跑出 1 次操作"
    assert classification in ("stable", "watch"), (
        f"场景 {scenario} 内存增长速率 {rate:.2f} MB/h 触发 suspect_leak 阈值（≥50 MB/h）"
    )


def test_all_scenarios_generate_report(qapp, tmp_path):
    """跑全 3 场景并写 docs/perf/long_run_baseline.md（pytest 友好版本）。"""
    duration = _pick_duration()
    summaries = run_all_scenarios(
        scenarios=["a", "b", "c"],
        duration_sec=duration,
        out_dir=tmp_path,
    )
    timestamp = _ts()
    render_markdown_report(
        summaries,
        out_path=DOCS_DIR / "long_run_baseline.md",
        timestamp=timestamp,
        duration_sec=duration,
    )
    assert (DOCS_DIR / "long_run_baseline.md").exists()
