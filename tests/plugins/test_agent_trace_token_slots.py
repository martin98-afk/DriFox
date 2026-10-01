# -*- coding: utf-8 -*-
"""`_token_slot_spans` 行为契约 + 长会话性能回归。

两条独立断言：

1. **数值契约**（防重构改坏）：water-filling 的分配结果必须满足
   「每条宽度 ≥ 下限（或退化为均分）」「按序铺满不重叠」「总数守恒」。
2. **复杂度契约**（防 O(N²) 复发）：n=2000 的调用耗时必须有硬上限。
   2026-09-18 实测旧实现（每轮对每个条目现算 ``sum(weights)``）n=2000/4000
   的 ``paintEvent`` 为 168ms / 652ms，`_hover_idx` 一变就重绘 → 鼠标划过
   条带就掉帧。阈值取 30ms（新实现实测该量级 3ms 上下，留一个数量级余量，
   避免 CI 抖动误报）。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_ROOT = Path(__file__).resolve().parents[2]
_UI_DIR = _ROOT / "plugins" / "agent_trace" / "ui"
for p in (str(_ROOT), str(_UI_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_panel():
    name = "agent_trace_slot_pkg"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, _UI_DIR / "__init__.py", submodule_search_locations=[str(_UI_DIR)]
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return importlib.import_module(f"{name}.timeline_panel")


panel_mod = _load_panel()
spans = panel_mod._token_slot_spans
MIN_PX = panel_mod._MIN_BAR_PX


def test_spans_cover_track_in_order_without_overlap():
    """按序铺满：区间左端递增、相邻不重叠、末条右端 ≈ 轨道右端。"""
    order = list(range(120))
    lane_w = 1600.0
    out = spans(order, [100 + i for i in order], lane_w)
    assert set(out) == set(order)
    prev_b = -1.0
    for idx in order:
        a, b = out[idx]
        assert a >= prev_b - 1e-9, f"idx={idx} 区间重叠（a={a} < 上条右端 {prev_b}）"
        assert a < b, f"idx={idx} 区间退化 a>=b"
        prev_b = b
    # 末条右端 = 轨道右端 − 一个条间缝隙（既有设计：每条后面都留 gap_px，
    # 包括最后一条；lane_w=1600 时即 1px）
    tail_gap = 1.0 / lane_w
    assert abs(out[order[-1]][1] - (1.0 - tail_gap)) < 1e-6, "末条未铺到轨道右端"


def test_spans_respect_min_width_when_roomy():
    """有空间时每条宽度不低于像素下限（这是 water-filling 的存在意义）。"""
    order = list(range(50))
    lane_w = 1600.0
    out = spans(order, [1] * 25 + [9000] * 25, lane_w)
    for idx in order:
        a, b = out[idx]
        assert (b - a) * lane_w >= MIN_PX - 0.5, f"idx={idx} 宽度低于下限"


def test_spans_degrade_to_equal_split_when_too_dense():
    """条目多到放不下下限 → 退化为等分（保证「每条都可见」）。"""
    n = 800
    order = list(range(n))
    out = spans(order, [50] * n, 1600.0)
    widths = [(out[i][1] - out[i][0]) * 1600.0 for i in order]
    assert max(widths) - min(widths) < 1e-6, "退化路径应等分"
    assert sum(widths) > 0


def test_spans_all_zero_weights_fall_back_to_equal():
    """全零 token → 等分（不能出现除零或全空）。"""
    n = 120
    order = list(range(n))
    out = spans(order, [0] * n, 1600.0)
    widths = [(out[i][1] - out[i][0]) * 1600.0 for i in order]
    assert max(widths) - min(widths) < 1e-6


def test_long_session_allocation_is_linear():
    """n=2000 的槽位分配必须远快于旧 O(N²) 实现（旧 ≈85ms，新 ≈3ms）。"""
    n = 2000
    order = list(range(n))
    vals = [200 + (i * 37) % 900 for i in order]
    t0 = time.perf_counter()
    out = spans(order, vals, 1600.0)
    cost_ms = (time.perf_counter() - t0) * 1000
    assert len(out) == n
    assert cost_ms < 30, f"槽位分配耗时 {cost_ms:.1f}ms，疑似 O(N²) 复发"


def test_returns_empty_for_degenerate_input():
    assert spans([], [], 1600.0) == {}
    assert spans([0, 1], [1, 1], 1.0) == {}
