# -*- coding: utf-8 -*-
"""agent_trace v4 冒烟：offscreen 构造卡片 + 塞数据 + 遍历所有条目类型的详情 tab。

只验证「不崩 + 关键控件状态」，不做视觉断言。
运行：
    QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe tests/_smoke_agent_trace_v4.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath("."))

import time  # noqa: E402

from PyQt5.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, os.path.abspath("plugins/agent_trace"))

from ui.trace_card import TraceCardWidget  # noqa: E402
from ui.trace_models import EntryKind, TraceRecord  # noqa: E402

DARK = {
    "card_bg": "rgba(22, 30, 45, 230)",
    "content_bg": "#1d2533",
    "border": "#3d4a60",
    "text_primary": "#f3f6fc",
    "text_secondary": "rgba(226, 235, 249, 0.72)",
    "text_muted": "#8b98ad",
    "accent": "#66c6ff",
    "accent_warm": "#f59e0b",
    "hover_bg": "rgba(102, 198, 255, 0.12)",
    "selected_bg": "rgba(102, 198, 255, 0.32)",
    "card_bg_dim": "rgba(255, 255, 255, 0.04)",
}
LIGHT = {
    "card_bg": "rgba(250, 250, 252, 245)",
    "border": "#d6dce6",
    "text_primary": "#1a1f2b",
    "text_secondary": "rgba(60, 70, 90, 0.75)",
    "text_muted": "#6b7688",
    "accent": "#0a6cff",
    "hover_bg": "rgba(0, 0, 0, 0.06)",
    "selected_bg": "rgba(0, 110, 200, 0.18)",
    "card_bg_dim": "rgba(0, 0, 0, 0.04)",
}


def _records() -> list:
    now = time.time() - 40
    return [
        TraceRecord(
            kind=EntryKind.SYSTEM,
            label="System Prompt",
            preview="你是一个助手…",
            raw="你是一个助手，请帮助用户完成任务。",
            source="session.system_prompt",
            start_ts=now,
            end_ts=now,
        ),
        TraceRecord(
            kind=EntryKind.USER,
            label="User",
            preview="帮我看一下这个项目",
            raw="帮我看一下这个项目",
            source="messages[0]",
            start_ts=now + 1,
            end_ts=now + 1,
            turn_no=1,
            meta={"turn_start": True},
        ),
        TraceRecord(
            kind=EntryKind.CONTEXT,
            label="PreToolUse",
            preview="注入工作区上下文",
            raw="当前 git 分支 main",
            source="messages[1]",
            start_ts=now + 1.2,
            end_ts=now + 1.2,
            turn_no=1,
        ),
        TraceRecord(
            kind=EntryKind.ASSISTANT,
            label="Assistant",
            preview="→ 调用工具: read_file",
            raw="",
            source="messages[2]",
            start_ts=now + 1.5,
            end_ts=now + 2.4,
            turn_no=1,
            meta={"has_tool_calls": True},
        ),
        TraceRecord(
            kind=EntryKind.TOOL,
            label="read_file",
            preview='{"path": "README.md"}',
            raw='{"path": "README.md"}\n\n── result ──\n# DriFox',
            source="messages[3]",
            start_ts=now + 2.5,
            end_ts=now + 3.1,
            turn_no=1,
            meta={"tool_call_id": "call_1", "arguments": '{"path": "README.md"}', "result": "# DriFox"},
        ),
        TraceRecord(
            kind=EntryKind.TOOL,
            label="bash",
            preview="执行中…",
            raw="ls -la",
            source="tool · bash",
            start_ts=now + 3.2,
            is_pending=True,
            turn_no=1,
            meta={"tool_call_id": "call_2"},
        ),
        TraceRecord(
            kind=EntryKind.TOOL,
            label="grep",
            preview="失败",
            raw="bad args",
            source="messages[5]",
            start_ts=now + 3.3,
            end_ts=now + 3.4,
            is_error=True,
            turn_no=1,
        ),
    ]


def check_span_ends() -> None:
    """``_fill_span_ends`` 的纯逻辑回归（不需要 Qt）。

    覆盖三类：真实耗时 / 同秒瞬时 / 超大间隔封顶 —— 后两类正是「时长列只剩
    80ms 和 3s 两个怪值」这个 bug 的现场。
    """
    from ui.trace_collector import TraceCollector
    from ui.trace_models import GAP_CAP_S

    base = 1_700_000_000.0
    recs = [
        TraceRecord(kind=EntryKind.SYSTEM, label="s", preview="", raw="", start_ts=base, end_ts=base),
        TraceRecord(kind=EntryKind.USER, label="u", preview="", raw="", start_ts=base, end_ts=base),  # 同秒
        TraceRecord(kind=EntryKind.TOOL, label="t", preview="", raw="", start_ts=base + 1, end_ts=base + 1.5),
        TraceRecord(kind=EntryKind.ASSISTANT, label="a", preview="", raw="", start_ts=base + 2),  # 空闲到 +12
        TraceRecord(kind=EntryKind.TOOL, label="t2", preview="", raw="", start_ts=base + 12, end_ts=base + 12.2),
    ]
    TraceCollector._fill_span_ends(recs)
    # 同秒 → 瞬时（0），不能被保底成 80ms
    assert recs[0].span_ms == 0, recs[0].span_ms
    assert recs[1].span_ms == int(1000 * (base + 1 - base)), recs[1].span_ms  # 跨到下一条 1s
    # 有真实耗时 → 用真实值
    assert recs[2].span_ms == 500, recs[2].span_ms
    # 超大空闲（10s）→ 封顶 GAP_CAP 并打标记
    assert recs[3].span_ms == int(GAP_CAP_S * 1000), recs[3].span_ms
    assert recs[3].meta.get("span_capped") is True
    assert recs[3].span_label.startswith("≥"), recs[3].span_label
    assert recs[4].span_ms == 200, recs[4].span_ms
    print("  span ends OK:", [r.span_label for r in recs])


def check_engine_event_forwarding() -> None:
    """验证 backend 的「引擎回调 → Qt 信号」转发（本次真实数据的关键链路）。

    背景：backend 上 tool_call_started / tool_result_received / stream_started /
    stream_finished / context_updated **全仓没有 emit 点**，插件订阅了永远收不到。
    修法是在 ``set_callback`` 注册时包一层转发。这里用探针对象借
    ``ChatBackend._wrap_trace_callback`` 的纯逻辑做验证（不实例化重对象）：
    - 原回调必须**照常收到全部参数**（不能被截断，否则主程序 UI 会坏）
    - 信号只收到 arity 个参数（丢弃 round_id / from_api 等引擎专有参数）
    """
    from PyQt5.QtCore import QObject, pyqtSignal

    from app.core.backend import ChatBackend
    from ui.trace_collector import _epoch_from_ts_ms

    class _Probe(QObject):
        stream_started = pyqtSignal()
        stream_finished = pyqtSignal(str)
        tool_call_started = pyqtSignal(str, str, dict)
        tool_result_received = pyqtSignal(str, str, dict, object)
        context_updated = pyqtSignal(int, int)

        _TRACE_SIGNAL_ARITY = ChatBackend._TRACE_SIGNAL_ARITY
        _wrap_trace_callback = ChatBackend._wrap_trace_callback

    probe = _Probe()
    got: dict = {}
    probe.tool_call_started.connect(lambda *a: got.__setitem__("tool", a))
    probe.tool_result_received.connect(lambda *a: got.__setitem__("result", a))
    probe.stream_started.connect(lambda: got.__setitem__("ss", ()))
    probe.stream_finished.connect(lambda r: got.__setitem__("sf", (r,)))
    probe.context_updated.connect(lambda c, lim: got.__setitem__("ctx", (c, lim)))

    seen: list = []
    # 引擎侧实际签名：tool_call_started 带第 4 个 round_id
    probe._wrap_trace_callback("tool_call_started", lambda *a: seen.append(("start", a)))(
        "call_1", "bash", {"command": "ls"}, "round-3"
    )
    assert seen[-1] == ("start", ("call_1", "bash", {"command": "ls"}, "round-3")), seen[-1]
    assert got["tool"] == ("call_1", "bash", {"command": "ls"}), got["tool"]

    probe._wrap_trace_callback(
        "tool_result_received", lambda *a: seen.append(("res", len(a)))
    )("call_1", "bash", {"command": "ls"}, {"success": True, "content": "ok"})
    assert got["result"][0] == "call_1" and got["result"][3] == {"success": True, "content": "ok"}, got["result"]

    probe._wrap_trace_callback("stream_started", lambda: seen.append(("ss",)))()
    assert got.get("ss") == (), got.get("ss")
    probe._wrap_trace_callback("stream_finished", lambda r: seen.append(("sf", r)))("hello")
    assert got["sf"] == ("hello",), got["sf"]
    # 引擎侧 context_updated 是 (count, limit, from_api)，第 3 参不透传
    probe._wrap_trace_callback("context_updated", lambda *a: seen.append(("ctx", len(a))))(1234, 8192, True)
    assert got["ctx"] == (1234, 8192), got["ctx"]
    # 未登记的名字原样返回，不包装
    plain = lambda: None  # noqa: E731
    assert probe._wrap_trace_callback("error", plain) is plain

    # ts_ms 换算：毫秒 / 秒级都吃，非法返回 None
    assert abs((_epoch_from_ts_ms(1_700_000_000_123) or 0) - 1_700_000_000.123) < 0.01
    assert _epoch_from_ts_ms(None) is None and _epoch_from_ts_ms(0) is None
    assert _epoch_from_ts_ms("abc") is None
    print("  engine event forwarding OK")


def check_ts_ms_persisted() -> None:
    """新字段必须进 ``normalize_message`` 白名单，否则会被 consolidate 剥掉。"""
    from app.core.message_content import normalize_message

    for msg in (
        {"role": "user", "content": "hi", "timestamp": "2026-08-30 18:00:00", "ts_ms": 1_700_000_000_123},
        {"role": "assistant", "content": "yo", "timestamp": "2026-08-30 18:00:00", "ts_ms": 1_700_000_001_456},
        {"role": "tool", "content": "out", "tool_call_id": "c1", "name": "bash", "ts_ms": 1_700_000_002_789},
    ):
        norm = normalize_message(msg)
        assert norm is not None and norm.get("ts_ms") == msg["ts_ms"], (msg["role"], norm)
    # 缺失/非法时不应写入字段
    assert "ts_ms" not in (normalize_message({"role": "user", "content": "x"}) or {}), "空值不应写入"
    print("  ts_ms persisted OK")


def main() -> int:
    check_span_ends()
    check_ts_ms_persisted()
    app = QApplication(sys.argv)
    check_engine_event_forwarding()
    card = TraceCardWidget()
    card.resize(1400, 820)
    card.show()

    recs = _records()

    for name, colors, dark in (("dark", DARK, True), ("light", LIGHT, False)):
        card.set_context(
            {
                "is_dark": dark,
                "colors": colors,
                "font_family": "Segoe UI",
                "font_size": 14,
                "services": {},
            }
        )
        app.processEvents()
        card._turn_list.set_records(recs)
        card._timeline.set_records(recs)
        card._sync_bounds(recs)
        card._detail.set_records(recs)
        card._detail.set_tools_schema(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "读取文件内容",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                },
                {
                    "type": "function",
                    "function": {"name": "bash", "description": "执行命令", "parameters": {"type": "object"}},
                },
            ]
        )
        app.processEvents()

        # 遍历每种类型的详情 tab 全组合
        for i, rec in enumerate(recs):
            card._detail.select(i)
            app.processEvents()
            for key in list(card._detail._tab_keys):
                card._detail._active_tab = key
                card._detail._fill_active_tab()
                card._detail._segmented.setCurrentItem(key)
                app.processEvents()
            print(f"  [{name}] #{i} {rec.kind.label:9s} tabs={card._detail._tab_keys}")

        # 列表排序 / 过滤 / 搜索
        card._turn_list._on_sort_changed("size", True)
        app.processEvents()
        card._turn_list._on_sort_changed("time", True)
        app.processEvents()
        card._turn_list._on_sort_changed("index", False)
        app.processEvents()
        card._turn_list.set_filter_kind(EntryKind.TOOL)
        app.processEvents()
        card._turn_list.set_search("read")
        app.processEvents()
        card._turn_list.set_search("")
        card._turn_list.set_filter_kind(None)
        app.processEvents()

        # 时间线三个开关的全组合（duration / turns / calls 可任意叠加）
        for dur in (True, False):
            for turns in (False, True):
                for calls in (False, True):
                    card._timeline.set_flags(dur, turns, calls)
                    app.processEvents()
        # 回到默认（三个开关全关 = 等宽块视图）
        card._timeline.set_flags(False, False, False)
        app.processEvents()

        # 时间线拖选 → 列表过滤（重叠区间才保留）
        t0 = recs[2].start_ts
        t1 = recs[4].span_end_ts
        card._turn_list.set_time_range(t0, t1)
        app.processEvents()
        assert card._turn_list.shown_count == 3, card._turn_list.shown_count
        assert card._turn_list._range_chip.isVisibleTo(card._turn_list), "区间 chip 应显示"
        card._turn_list.clear_time_range()
        app.processEvents()
        assert card._turn_list.shown_count == 7, card._turn_list.shown_count
        assert not card._turn_list._range_chip.isVisibleTo(card._turn_list), "区间 chip 应隐藏"

        # 时长语义回归：瞬时项 span=0（显示 —），有真实耗时/间隔的项才有值。
        # 早期版本给 0 间隔保底 80ms → 时长列只剩「80ms / 3s」两个怪值。
        for i, rec in enumerate(recs):
            assert rec.span_ms >= 0, f"#{i} span_ms 负数"
            assert rec.span_end_ts >= rec.start_ts, f"#{i} 占用终点早于起点"
            if rec.end_ts > rec.start_ts:
                assert rec.span_ms == int((rec.end_ts - rec.start_ts) * 1000), f"#{i} 应等于真实耗时"
            assert isinstance(rec.span_label, str) and rec.span_label, f"#{i} span_label 空"
        print("  span labels:", [r.span_label for r in recs])

        # 统计栏（真实路径由 _pull_records 驱动）
        card._refresh_stats(recs)
        app.processEvents()
        stats = " | ".join(
            x.text() for x in (card._stats_turns, card._stats_time, card._stats_ctx, card._stats_total)
        )
        assert "7 条" in card._stats_turns.text(), card._stats_turns.text()
        assert "1 轮" in card._stats_turns.text(), card._stats_turns.text()
        assert card._stats_total.text().startswith("完成"), card._stats_total.text()
        # Tools Schema 页：注入 2 个工具 → 目录有 2 行
        assert card._detail._page_tools._list.count() == 2, card._detail._page_tools._list.count()
        # System Prompt tab 内容落到会话 system_prompt
        card._detail.select(0)
        app.processEvents()
        assert "助手" in card._detail._page_text.toPlainText(), card._detail._page_text.toPlainText()[:60]

        print(
            f"[{name}] rows={card._turn_list._list.count()}"
            f" shown={card._turn_list.shown_count}/{card._turn_list.total_count}"
            f" stats={stats}"
        )

    # 关键回归：主题次级色不得为纯黑（rgba 字符串解析失败的典型症状）
    for name in ("text", "text_secondary", "text_muted", "border", "accent"):
        c = getattr(card._pal, name)
        assert c.isValid(), f"{name} 无效色"
        assert not (c.red() == c.green() == c.blue() == 0 and name != "line"), f"{name} 是纯黑（rgba 解析失败？）"
    print("palette:", {k: card._pal.q(k) for k in ("text", "text_secondary", "text_muted", "accent")})

    if "--shot" in sys.argv:
        out = sys.argv[sys.argv.index("--shot") + 1]
        os.makedirs(out, exist_ok=True)
        for name, colors, dark in (("dark", DARK, True), ("light", LIGHT, False)):
            card.set_context(
                {"is_dark": dark, "colors": colors, "font_family": "Segoe UI", "font_size": 14, "services": {}}
            )
            card._turn_list.set_records(recs)
            card._timeline.set_records(recs)
            card._sync_bounds(recs)
            card._detail.set_records(recs)
            card._detail.select(4)  # 选中一个 TOOL，展示 Request/Response/Timing
            card._refresh_stats(recs)
            app.processEvents()
            path = os.path.join(out, f"agent_trace_{name}.png")
            card.grab().save(path)
            print("shot ->", path)

    card.close()
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
