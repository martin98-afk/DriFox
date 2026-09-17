# -*- coding: utf-8 -*-
"""agent_trace 页脚吞吐量（footer_stat）口径回归。

覆盖两条分支：

1. 流式期间 = 当前这条流的最近一次采样值，token 走 ``estimate_tokens_text``
   （tiktoken/cl100k，中文约 1.2 token/字），**不再是 chars÷4** —— 旧口径在
   中文回答上低估约 4~5 倍，是「流式值与落定值对不上」的根因。
2. 落定 / 历史加载 = collector 投影**逐条 per-call** 聚合
   （Σ每条输出 token ÷ Σ每条生成秒），不再读宿主卡片给的
   ``elapsed`` + ``token_usage.output``。

⚠️ 回归要点（2026-09-17 真实 bug）：加载历史会话时 ``_restore_meta_from_batch``
传的 ``elapsed`` 是**整轮墙钟**（含全部工具迭代）、``token_usage.output`` 只是
**末次** API 调用的输出，两者相除分子分母跨调用串口径。真实样本 603 tok ÷ 1584 s
= 0.38 tok/s（同期 per-call 口径 42 tok/s），页脚长期显示 0~2 tok/s。
"""

from __future__ import annotations

import importlib
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
    # collector hub 缓存必须清空：上一个用例注入的 fake hub 会串到下一个
    mod._FOOTER_HUB = None
    yield mod
    mod._FOOTER_HUB = None


def _ctx(**kw):
    ctx = {
        "window_id": "w1",
        "main_widget": None,
        "role": "assistant",
        "streaming": False,
        "round_index": 0,
        "message_index": 0,
        "elapsed": None,
        "token_usage": None,
    }
    ctx.update(kw)
    return ctx


def _models(ui_mod):
    """取与 ui 模块**同一份**的 trace_models（避免同名模块二次加载 → 枚举不相等）。"""
    return importlib.import_module(f"{ui_mod.__name__}.trace_models")


def _asst_rec(ui_mod, tokens: int, elapsed_ms: float, ttft_ms: float = 0.0, pending: bool = False):
    """构造一条 ASSISTANT 投影记录（无真实区间 → duration_ms 走 meta["elapsed_ms"]）。"""
    TraceRecord = _models(ui_mod).TraceRecord
    EntryKind = _models(ui_mod).EntryKind
    return TraceRecord(
        kind=EntryKind.ASSISTANT,
        label="Assistant",
        preview="",
        raw="",
        start_ts=0.0,
        end_ts=0.0,
        is_pending=pending,
        meta={"tokens": tokens, "elapsed_ms": float(elapsed_ms), "ttft_ms": float(ttft_ms)},
    )


class _FakeCollector:
    def __init__(self, sid: str, records: list, refresh_sid: str | None = None):
        self._active_session_id = sid
        self._records = records
        # 非 None 时：refresh() 会把 active_session_id 换成它（模拟重投影追上会话切换）
        self._refresh_sid = refresh_sid
        self.refresh_calls = 0

    @property
    def records(self):
        return list(self._records)

    def refresh(self):
        self.refresh_calls += 1
        if self._refresh_sid is not None:
            self._active_session_id = self._refresh_sid


class _FakeHub:
    """collector_for 返回固定投影的 stub hub（替代真 TraceCollectorHub）。"""

    def __init__(self, sid: str, records: list, refresh_sid: str | None = None):
        self._collector = _FakeCollector(sid, records, refresh_sid)

    def collector_for(self, mw):
        return self._collector


class _Host:
    """最小宿主 stub：仅带 _current_session_id（窗口真实路径切会话时先于卡片构建更新）。"""

    def __init__(self, sid: str):
        self._current_session_id = sid


# ──────────────────── 流式分支 ────────────────────


def test_streaming_early_returns_none(ui_mod):
    """起步 0.3s 内不显示（首字抖动）。"""
    assert ui_mod._footer_avg_throughput(_ctx(streaming=True, live_text="你好世界", live_gen_s=0.1)) is None


def test_streaming_uses_token_estimator_not_chars_div4(ui_mod):
    """中文 400 字 / 1s：chars÷4 只给 100 tok/s，真实口径必须显著更高。"""
    val = ui_mod._footer_avg_throughput(_ctx(streaming=True, live_text="中" * 400, live_gen_s=1.0))
    assert val is not None
    tps = float(val["text"].split()[0])
    assert tps >= 150, f"token 估算仍被低估：{tps} tok/s（chars÷4 只给 100）"


# ──────────────────── 落定 / 历史加载分支（collector per-call 口径） ────────────────────


def test_settled_aggregates_per_call(ui_mod, monkeypatch):
    """落定态 = collector 逐条 per-call 聚合：Σtokens ÷ Σ(elapsed_ms − ttft_ms)。

    1000 tok/(10s−2s) + 500 tok/(5s−1s) = 1500 / 12 = 125 tok/s
    """
    records = [
        _asst_rec(ui_mod, 1000, 10000.0, 2000.0),
        _asst_rec(ui_mod, 500, 5000.0, 1000.0),
    ]
    monkeypatch.setattr(ui_mod, "_FOOTER_HUB", _FakeHub("sessA", records))
    host = _Host("sessA")
    val = ui_mod._footer_avg_throughput(_ctx(main_widget=host))
    assert val["text"] == "125 tok/s"
    assert "2 次调用" in val["tooltip"]


def test_history_load_ignores_round_wallclock_and_last_call_output(ui_mod, monkeypatch):
    """回归：宿主卡片给「整轮墙钟 elapsed + 末次 output」时不得直接相除。

    真实样本 af585272：elapsed=1592.9s（整轮，含 91 次调用）、output=603（末次）。
    旧口径 603/1584.2 = 0.38 tok/s（显示 0）；合理值必须来自 per-call 投影。
    """
    records = [_asst_rec(ui_mod, 1500, 5000.0, 1000.0)]  # 1500 / 4s = 375 tok/s
    monkeypatch.setattr(ui_mod, "_FOOTER_HUB", _FakeHub("af585272-full", records))
    host = _Host("af585272-full")
    val = ui_mod._footer_avg_throughput(
        _ctx(
            main_widget=host,
            elapsed=1592.9,
            token_usage={"total": 120000, "output": 603, "ttft_ms": 8702.0},
        )
    )
    assert val["text"] == "375 tok/s", "不得用整轮墙钟当分母、末次 output 当分子"


def test_history_load_stale_projection_shows_nothing(ui_mod, monkeypatch):
    """投影未跟上会话切换 → 先驱动一次重投影。

    切会话路径（_load_session_from_record → set_current_session）不发任何 backend
    信号，collector 不会自己感知；不 refresh 页脚会一直空白到用户下次发消息。
    重投影后仍对不上（refresh 无效）→ 返回 None（不串旧会话数字）。
    """
    hub = _FakeHub("sessA", [_asst_rec(ui_mod, 500, 2000.0)])
    monkeypatch.setattr(ui_mod, "_FOOTER_HUB", hub)
    assert ui_mod._footer_avg_throughput(_ctx(main_widget=_Host("sessB"))) is None
    assert hub._collector.refresh_calls == 1, "应驱动一次重投影"


def test_history_load_reprojects_and_shows(ui_mod, monkeypatch):
    """重投影追上会话切换后 → 立即出正确值（不必等下一次 backend 信号）。"""
    records = [_asst_rec(ui_mod, 2000, 4000.0)]  # 2000 / 4s = 500 tok/s
    hub = _FakeHub("sessA", records, refresh_sid="sessB")
    monkeypatch.setattr(ui_mod, "_FOOTER_HUB", hub)
    val = ui_mod._footer_avg_throughput(_ctx(main_widget=_Host("sessB")))
    assert val is not None and val["text"] == "500 tok/s"


def test_pending_and_short_gen_excluded(ui_mod, monkeypatch):
    """in-flight 尾巴与生成段 ≤200ms 的条目不参与（防 tps 虚高 / 时长走动）。"""
    records = [
        _asst_rec(ui_mod, 1000, 10000.0, 0.0),  # 计
        _asst_rec(ui_mod, 999, 300.0, 200.0),  # 生成 100ms → 排除
        _asst_rec(ui_mod, 999, 10000.0, 0.0, pending=True),  # pending → 排除
    ]
    monkeypatch.setattr(ui_mod, "_FOOTER_HUB", _FakeHub("sessA", records))
    val = ui_mod._footer_avg_throughput(_ctx(main_widget=_Host("sessA")))
    assert val["text"] == "100 tok/s"  # 1000 / 10s


def test_no_records_returns_none(ui_mod):
    """无宿主 / 无 collector / 空投影 → 不显示。"""
    assert ui_mod._footer_avg_throughput(_ctx()) is None


# ──────────────────── 配色 ────────────────────


def test_color_thresholds(ui_mod, monkeypatch):
    """<30 红 / <60 黄 / 其余绿。"""

    def _one(sid: str, tokens: int, gen_s: float):
        records = [_asst_rec(ui_mod, tokens, gen_s * 1000.0)]
        monkeypatch.setattr(ui_mod, "_FOOTER_HUB", _FakeHub(sid, records))
        return ui_mod._footer_avg_throughput(_ctx(main_widget=_Host(sid)))

    assert _one("c1", 10, 2.0)["text"] == "5 tok/s"
    assert _one("c1", 10, 2.0)["color"] == "#f85149"
    assert _one("c2", 90, 2.0)["text"] == "45 tok/s"
    assert _one("c2", 90, 2.0)["color"] == "#d29922"
    assert _one("c3", 200, 2.0)["text"] == "100 tok/s"
    assert _one("c3", 200, 2.0)["color"] == "#2ea043"
