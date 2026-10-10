# -*- coding: utf-8 -*-
"""回归：并发渲染页配额必须随 devicePixelRatio 收缩。

症状（2026-10-11 排查「独显机内存远高于老机器」）：
    同一份代码，RTX 5090 + 4K@225%（dpr=2.25）的机器主进程 1968MB + WebEngine
    子进程 854MB ≈ 2.8GB；1080p@100%（dpr=1）的老笔记本明显低得多。

根因（实测 tools/diag_webengine_mem_probe.py --show，两台机器同代码只差 dpr）：
    单个可见 QWebEngineView 的内存成本 ≈ **9.2MB / 百万物理像素**，与
    「宽 × 高 × dpr²」严格线性（截距≈0：549×2000 逻辑 → 50.5MB/view，
    244×2000 逻辑 → 22.1MB/view）。写死「12 页并发」在 dpr=2.25 上等于把
    内存预算放大 5.06 倍。

修复：`effective_render_quota` 按 1/dpr 收缩（dpr≤1 时行为完全不变）。
"""

from app.core.infra.memory_governor import (
    _DPR_QUOTA_MIN,
    _MAX_RENDERED_CARDS,
    effective_render_quota,
)


def test_dpr_1_keeps_historical_quota():
    """老机器 / 100% 缩放：配额与历史完全一致（本次改动对它们零影响）。"""
    assert effective_render_quota(_MAX_RENDERED_CARDS, 1.0) == _MAX_RENDERED_CARDS
    assert effective_render_quota(_MAX_RENDERED_CARDS, 0.5) == _MAX_RENDERED_CARDS


def test_hidpi_shrinks_quota():
    assert effective_render_quota(12, 1.5) == 8
    assert effective_render_quota(12, 2.25) == 6  # 12/2.25 = 5.33 → 保底 6


def test_extreme_dpr_floors_at_min():
    """极端 dpr 也不能把配额压到视口缓冲以下（会变成滚一下建一次）。"""
    assert effective_render_quota(12, 8.0) == _DPR_QUOTA_MIN
    assert _DPR_QUOTA_MIN >= 6


def test_abnormal_dpr_treated_as_1():
    """非法 dpr（0 / None / 非数）按 1.0 处理，绝不因解析失败而误缩配额。"""
    for bad in (0, 0.0, None, "", "abc", [], object()):
        assert effective_render_quota(12, bad) == 12


def test_base_override_respected():
    assert effective_render_quota(24, 2.0) == 12
