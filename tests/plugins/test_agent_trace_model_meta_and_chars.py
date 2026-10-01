# -*- coding: utf-8 -*-
"""agent_trace：模型名落投影 meta + 字符统计含思考内容（回归）。

背景（2026-09-18 用户反馈）：
1. 详情面板统计页没有记录该条回复使用的模型 —— worker 落盘在
   ``msg["model_name"]``，collector 投影时未带出，历史会话里 provider
   配置已换后就无从知道当时真正用的模型。
2. 标题栏 / Info 页「字符」只算正文 ``rec.raw``，思维链（reasoning）
   字数缺失，带思考的回复显示的字符数远小于实际输出量。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt5.QtCore")

from plugins.agent_trace.ui.trace_collector import TraceCollector  # noqa: E402
from plugins.agent_trace.ui.trace_models import EntryKind  # noqa: E402


def _bare_collector() -> TraceCollector:
    """最小实例：QObject 走完 ``__init__`` 即可（不依赖 qapp，见
    test_agent_trace_reasoning_loader.py 同款做法）。"""
    return TraceCollector(None)


def _assistant(**over):
    msg = {
        "role": "assistant",
        "content": "正文六个字",
        "reasoning_content": "思考一二三",
        "model_name": "deepseek-v4",
        "elapsed_ms": 2030.0,
        "ttft_ms": 1280.0,
        "timestamp": "2026-09-18 13:23:57",
        "ts_ms": 1758175437000,
    }
    msg.update(over)
    return msg


def _project_assistant(collector: TraceCollector, msgs) -> object:
    recs = collector._project_messages(msgs, system_prompt="", session_id="s1")
    return next(r for r in recs if r.kind == EntryKind.ASSISTANT)


def _detail_panel():
    """绕过 ``__init__`` 的裸 DetailPanel（Qt widget 构建太重；
    被测方法只依赖 staticmethod 与入参）。"""
    from plugins.agent_trace.ui.detail_panel import DetailPanel

    return DetailPanel.__new__(DetailPanel)


def test_model_name_lands_in_meta():
    rec = _project_assistant(_bare_collector(), [{"role": "user", "content": "hi"}, _assistant()])
    assert rec.meta.get("model") == "deepseek-v4"


def test_legacy_message_without_model_has_no_key():
    rec = _project_assistant(_bare_collector(), [_assistant(model_name=None)])
    assert "model" not in rec.meta


def test_llm_stat_rows_contains_model_row():
    rec = _project_assistant(_bare_collector(), [_assistant()])
    rows = dict(_detail_panel()._llm_stat_rows(rec))
    assert rows.get("模型") == "deepseek-v4"
    assert "开始时间" in rows


def test_llm_stat_rows_without_model_has_no_row():
    rec = _project_assistant(_bare_collector(), [_assistant(model_name=None)])
    assert "模型" not in dict(_detail_panel()._llm_stat_rows(rec))


def test_content_size_counts_reasoning():
    """字符统计 = 正文 + 思考内容合计（本例 5 + 5 = 10）。"""
    p = _detail_panel()
    rec = _project_assistant(_bare_collector(), [_assistant()])
    assert p._content_size_text(rec) == "10 字符"


def test_content_size_empty_is_dash():
    p = _detail_panel()
    rec = _project_assistant(_bare_collector(), [_assistant(content="", reasoning_content="")])
    assert p._content_size_text(rec) == "—"


def test_reasoning_text_prefers_meta_then_loader(monkeypatch):
    p = _detail_panel()
    rec = _project_assistant(_bare_collector(), [_assistant()])
    assert p._reasoning_text(rec) == "思考一二三"
    # meta 没有 → 走 loader 懒读
    rec.meta.pop("reasoning")
    rec.reasoning_loader = lambda: "懒读内容"
    assert p._reasoning_text(rec) == "懒读内容"
