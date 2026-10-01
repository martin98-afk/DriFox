# -*- coding: utf-8 -*-
"""MessageCard 流式吞吐采样：生成秒只累计「出字时间」，不含工具执行空档。

背景（2026-09-17 实测反馈）：工具循环里 assistant 卡片**不重建** ——
``main_widget._on_stream_started`` 只在 ``_elapsed_start_time is None`` 时调
``start_elapsed_tracking``，所以一张卡片会跨多个 worker 迭代（LLM 流式 → 工具
执行 → 再流式）。旧口径用「首字至今」当分母，把工具执行的十几秒算进生成秒，
实时吞吐量被稀释到真实值的几分之一（10s 工具 → tps 掉到约 1/10）。

修复：逐 chunk 累加间隔，单段间隔超过 ``_LIVE_GAP_CAP_S``（2s）视为工具执行 /
请求等待，不计入。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import app.widgets.message_card as mc  # noqa: E402


def _make_card(qapp, monkeypatch):
    """构造 assistant 卡片 + 可控时钟；append_text 置空，只测采样逻辑。"""
    clock = {"t": 1000.0}
    monkeypatch.setattr(mc.time, "time", lambda: clock["t"])
    card = mc.MessageCard(role="assistant")
    card.append_text = lambda *a, **k: None
    card.start_elapsed_tracking()
    return card, clock


def test_tool_gap_excluded_from_gen_seconds(qapp, monkeypatch):
    """1s（计入）+ 10s（工具，排除）+ 0.5s（计入）→ 生成秒 = 1.5。"""
    card, clk = _make_card(qapp, monkeypatch)
    card.update_content("中" * 100)  # 首 chunk，只记时刻
    clk["t"] += 1.0
    card.update_content("中" * 100)
    clk["t"] += 10.0  # 工具执行空档
    card.update_content("中" * 100)
    clk["t"] += 0.5
    card.update_content("中" * 100)

    ctx = card._footer_stat_context(streaming=True)
    assert ctx["live_text"] == "中" * 400
    assert ctx["live_gen_s"] == pytest.approx(1.5)


def test_first_chunk_has_zero_gen_seconds(qapp, monkeypatch):
    """只有一个 chunk 时无从谈速率 → 生成秒 0（provider 侧按 <0.3s 隐藏）。"""
    card, _clk = _make_card(qapp, monkeypatch)
    card.update_content("中" * 100)
    assert card._footer_stat_context(streaming=True)["live_gen_s"] == 0.0


def test_settled_clears_sampling_state(qapp, monkeypatch):
    """落定清缓冲：下一轮不会把上一轮的出字时间带进来。"""
    card, clk = _make_card(qapp, monkeypatch)
    card.update_content("中" * 100)
    clk["t"] += 1.0
    card.update_content("中" * 100)
    card.set_meta_info(elapsed=3.0, token_usage={"output": 300})
    assert card._footer_stat_context(streaming=True)["live_gen_s"] == 0.0
    assert card._footer_stat_context(streaming=True)["live_text"] == ""
