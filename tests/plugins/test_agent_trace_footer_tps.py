# -*- coding: utf-8 -*-
"""agent_trace 页脚吞吐量（footer_stat）口径回归。

覆盖三条分支：

1. 流式期间 = 当前这条流的最近一次采样值，token 走 ``estimate_tokens_text``
   （tiktoken/cl100k，中文约 1.2 token/字），**不再是 chars÷4** —— 旧口径在
   中文回答上低估约 4~5 倍，是「流式值与落定值对不上」的根因。
2. 回合落定 = 本会话 Σ输出 token ÷ Σ生成秒，本轮真实 usage 即时并入累加表，
   **不等** collector 投影（旧版靠 1s/2.5s 补刷拉回，观感即"延迟高"）。
3. 同轮重复刷新幂等、不同会话互相隔离。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtCore")

_ROOT = Path(__file__).resolve().parents[2]
_UI_DIR = _ROOT / "plugins" / "agent_trace" / "ui"
for p in (str(_ROOT), str(_UI_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_ui_module():
    """以包方式加载 ``plugins/agent_trace/ui/__init__.py``（模块内有相对导入）。"""
    name = "agent_trace_ui_pkg"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, _UI_DIR / "__init__.py", submodule_search_locations=[str(_UI_DIR)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ui_mod(qapp):
    """用 pytest-qt 的 session 级 ``qapp``（不手动建 QApplication）。

    ⚠️ 手动 QApplication(sys.argv) 会让同批次的 widgets 测试（如
    test_message_card_meta_deleted）拿到被 pytest-qt 视为"外来"的 app 实例，
    表现为 ``wrapped C/C++ object of type QConfig has been deleted``。
    """
    mod = _load_ui_module()
    mod._ROUND_STATS.clear()
    yield mod
    mod._ROUND_STATS.clear()


def _ctx(**kw):
    ctx = {
        "window_id": "w1",
        "main_widget": None,  # 无 backend → session_id 退化为空，按 window_id 分组
        "role": "assistant",
        "streaming": False,
        "round_index": 0,
        "message_index": 0,
        "elapsed": None,
        "token_usage": None,
    }
    ctx.update(kw)
    return ctx


def test_streaming_early_returns_none(ui_mod):
    """起步 0.3s 内不显示（首字抖动）。"""
    assert ui_mod._footer_avg_throughput(_ctx(streaming=True, live_text="你好世界", live_gen_s=0.1)) is None


def test_streaming_uses_token_estimator_not_chars_div4(ui_mod):
    """中文 400 字 / 1s：chars÷4 只给 100 tok/s，真实口径必须显著更高。"""
    val = ui_mod._footer_avg_throughput(_ctx(streaming=True, live_text="中" * 400, live_gen_s=1.0))
    assert val is not None
    tps = float(val["text"].split()[0])
    assert tps >= 150, f"token 估算仍被低估：{tps} tok/s（chars÷4 只给 100）"


def test_settled_first_round(ui_mod):
    val = ui_mod._footer_avg_throughput(_ctx(elapsed=2.0, token_usage={"output": 500, "ttft_ms": 200}))
    assert val["text"] == "278 tok/s"  # 500 / (2.0 − 0.2)


def test_settled_accumulates_rounds(ui_mod):
    ui_mod._footer_avg_throughput(_ctx(elapsed=2.0, token_usage={"output": 500, "ttft_ms": 200}))
    val = ui_mod._footer_avg_throughput(
        _ctx(round_index=1, message_index=2, elapsed=3.0, token_usage={"output": 1000, "ttft_ms": 500})
    )
    assert val["text"] == "349 tok/s"  # (500 + 1000) / (1.8 + 2.5)
    assert "2 轮" in val["tooltip"]


def test_repeat_refresh_is_idempotent(ui_mod):
    ctx = _ctx(elapsed=2.0, token_usage={"output": 500, "ttft_ms": 200})
    for _ in range(3):
        val = ui_mod._footer_avg_throughput(ctx)
    assert val["text"] == "278 tok/s"


def test_sessions_are_isolated(ui_mod):
    ui_mod._footer_avg_throughput(_ctx(elapsed=2.0, token_usage={"output": 500, "ttft_ms": 200}))
    other = ui_mod._footer_avg_throughput(
        _ctx(window_id="w2", elapsed=1.0, token_usage={"output": 100, "ttft_ms": 0})
    )
    assert other["text"] == "100 tok/s"


def test_no_usage_returns_none(ui_mod):
    assert ui_mod._footer_avg_throughput(_ctx(elapsed=2.0, token_usage={})) is None
    assert ui_mod._footer_avg_throughput(_ctx()) is None


class _FakeHub:
    """collector_for 返回固定 active session 的 stub hub（替代真 TraceCollectorHub）。"""

    def __init__(self, sid: str):
        self._sid = sid

    def collector_for(self, mw):
        from types import SimpleNamespace

        return SimpleNamespace(_active_session_id=self._sid)


class _Host:
    """最小宿主 stub：仅带 _current_session_id（窗口真实路径切会话时先于卡片构建更新）。"""

    def __init__(self, sid: str):
        self._current_session_id = sid


def test_history_load_keys_by_host_session(ui_mod, monkeypatch):
    """加载历史会话：宿主已切 sessB、collector 投影还停 sessA → 轮次必须归 sessB。

    修复前 key 取 collector._active_session_id（异步投影滞后于卡片构建），历史轮
    会写进旧会话的累加桶（按 (round_index, message_index) 覆盖旧数据），旧桶非空
    时还会把旧会话均值直接顶出来 —— 页脚吞吐量与当前对话对不上。
    """
    monkeypatch.setattr(ui_mod, "_FOOTER_HUB", _FakeHub("sessA"))
    host = _Host("sessB")
    val = ui_mod._footer_avg_throughput(
        _ctx(main_widget=host, elapsed=2.0, token_usage={"output": 500, "ttft_ms": 200})
    )
    assert val["text"] == "278 tok/s"  # sessB 单轮真实值，非 sessA 旧桶均值
    assert any(k.endswith("sessB") for k in ui_mod._ROUND_STATS), "轮次应归入宿主当前会话的桶"


def test_history_load_stale_projection_shows_nothing(ui_mod, monkeypatch):
    """累加桶空 + collector 投影未跟上会话切换 → 宁可不显示（等 1s 补刷），不串旧会话数字。"""
    monkeypatch.setattr(ui_mod, "_FOOTER_HUB", _FakeHub("sessA"))
    host = _Host("sessB")
    # 无 token_usage → 不写桶 → 走 _aggregate_records → 投影 stale → None
    assert ui_mod._footer_avg_throughput(_ctx(main_widget=host)) is None


def test_color_thresholds(ui_mod):
    """<30 红 / <60 黄 / 其余绿；每组用独立会话，避免累加表串扰。"""

    def _one(wid: str, out: int):
        return ui_mod._footer_avg_throughput(
            _ctx(window_id=wid, elapsed=2.0, token_usage={"output": out, "ttft_ms": 0})
        )

    assert _one("c1", 10)["text"] == "5 tok/s"
    assert _one("c1", 10)["color"] == "#f85149"
    assert _one("c2", 90)["text"] == "45 tok/s"
    assert _one("c2", 90)["color"] == "#d29922"
    assert _one("c3", 200)["text"] == "100 tok/s"
    assert _one("c3", 200)["color"] == "#2ea043"
