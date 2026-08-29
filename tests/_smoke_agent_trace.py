"""agent_trace smoke test — 验证插件可正常加载并满足基本不变量。

不在 CI 跑，仅手工验证：此文件用以检查 model / collector / card 框架
在没有真实主程序 / DB / backend 的情况下是否结构正确。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins" / "agent_trace"


def assert_true(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def test_plugin_json() -> None:
    pj = PLUGIN_ROOT / ".drifox-plugin" / "plugin.json"
    data = json.loads(pj.read_text(encoding="utf-8"))
    assert_true(data["name"] == "agent_trace", "plugin.json name")
    assert_true(data["components"]["ui"] is True, "components.ui")
    print("✓ plugin.json valid")


def test_icons_exist() -> None:
    assert_true((PLUGIN_ROOT / "icons" / "icon.svg").exists(), "icons/icon.svg")
    assert_true((PLUGIN_ROOT / "icons" / "icon_light.svg").exists(), "icons/icon_light.svg")
    print("✓ icons present")


def test_models_behavior() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT))
    from ui.trace_models import (
        EntryKind,
        TraceRecord,
        format_duration,
        format_duration_compact,
        infer_message_kind,
        message_label,
        truncate,
    )

    # format_duration
    assert_true(format_duration(0) == "0 ms", "fmt dur 0")
    assert_true(format_duration(999) == "999 ms", "fmt dur 999")
    assert_true(format_duration(1500).startswith("1.5"), "fmt dur 1.5s")
    assert_true(format_duration(83_000) == "1m 23s", "fmt dur 1m23s")
    # compact
    assert_true(format_duration_compact(1200) == "1.2s", "fmt compact 1.2s")
    assert_true(format_duration_compact(83_000) == "1m23s", "fmt compact 1m23s")
    # truncate
    assert_true(truncate("") == "（空）", "truncate empty")
    assert_true(truncate("a\nbb\nccc", 10) == "a", "truncate first line")
    # kind inference
    assert_true(infer_message_kind({"role": "user"}) == EntryKind.USER, "user -> USER")
    assert_true(
        infer_message_kind({"role": "user", "_hook_event": "PreToolUse"}) == EntryKind.CONTEXT,
        "hook -> CONTEXT",
    )
    assert_true(
        infer_message_kind({"role": "user", "_hook_event": "UserPromptSubmit"}) == EntryKind.USER,
        "UserPromptSubmit -> USER",
    )
    assert_true(infer_message_kind({"role": "system"}) == EntryKind.SYSTEM, "system")
    assert_true(infer_message_kind({"role": "tool"}) == EntryKind.TOOL, "tool")
    assert_true(infer_message_kind({"role": "assistant"}) == EntryKind.ASSISTANT, "assistant")
    # label
    assert_true(message_label({"role": "tool", "name": "skill"}) == "skill", "tool label")
    assert_true(message_label({"role": "assistant"}) == "Assistant", "assistant label")
    assert_true(message_label({"role": "user", "_hook_event": "SessionStart"}) == "SessionStart", "hook label")

    # TraceRecord duration
    rec = TraceRecord(
        kind=EntryKind.ASSISTANT,
        label="Assistant",
        preview="hello",
        raw="hello",
        start_ts=time.time() - 1.0,
        end_ts=time.time(),
    )
    assert_true(900 <= rec.duration_ms <= 1100, f"dur ~1000ms got {rec.duration_ms}")
    print("✓ trace_models invariants")


def test_register_ui_signature() -> None:
    """验证 register_ui(registry) 函数存在且签名正确。"""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from ui import register_ui

    assert callable(register_ui), "register_ui callable"

    class _MockRegistry:
        def __init__(self) -> None:
            self._floating_cards = {}
            self._titlebar_tabs = {}
            self._commands = []

        def register_floating_card(self, **kwargs) -> None:
            self._floating_cards[kwargs["card_id"]] = kwargs

        def register_titlebar_tab(self, **kwargs) -> None:
            self._titlebar_tabs[kwargs["tab_id"]] = kwargs

    reg = _MockRegistry()
    register_ui(reg)
    assert_true("agent_trace" in reg._floating_cards, "floating_card registered")
    assert_true("agent_trace" in reg._titlebar_tabs, "titlebar_tab registered")
    tab_info = reg._titlebar_tabs["agent_trace"]
    assert_true(tab_info["label"] == "轨迹", "label is '轨迹'")
    assert_true(callable(tab_info["on_click"]), "on_click callable")
    card_info = reg._floating_cards["agent_trace"]
    assert_true(card_info["container"] == "full", "full container")
    assert_true(card_info["default_visible"] is False, "default hidden")
    print("✓ register_ui registers both components")


def test_card_instantiation() -> None:
    """检查 TraceCardWidget 能在 headless QApplication 下实例化 + 渲染一次 paintEvent。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    sys.path.insert(0, str(PLUGIN_ROOT))
    from ui.trace_card import TraceCardWidget

    card = TraceCardWidget()
    # 注入 fake context（含 main_widget=None 让 collector 走 lazy bind）
    card.set_context(
        {
            "window_id": "smoke-test",
            "colors": {"text_primary": "#D0D0D0", "border": "#333"},
            "font_family": "Segoe UI",
            "font_size": 12,
            "is_dark": True,
        }
    )
    # 模拟 recordsAppended
    from PyQt5.QtCore import QCoreApplication

    from ui.trace_models import EntryKind, TraceRecord

    records = [
        TraceRecord(
            kind=EntryKind.SYSTEM,
            label="Initial System Prompt",
            preview="You are Drifox assistant...",
            raw="You are Drifox assistant...",
            source="session.messages[0]",
            start_ts=time.time() - 8.0,
            end_ts=time.time() - 7.5,
        ),
        TraceRecord(
            kind=EntryKind.USER,
            label="User",
            preview="帮我看看架构",
            raw="帮我看看当前架构",
            source="session.messages[1]",
            start_ts=time.time() - 7.0,
            end_ts=time.time() - 6.9,
        ),
        TraceRecord(
            kind=EntryKind.CONTEXT,
            label="SessionStart",
            preview="<system-reminder> workspace instructions...",
            raw="<system-reminder> workspace instructions...",
            source="session.messages[2] · SessionStart hook",
            start_ts=time.time() - 6.8,
            end_ts=time.time() - 6.7,
        ),
        TraceRecord(
            kind=EntryKind.ASSISTANT,
            label="Assistant",
            preview="I'll load caveman skill since the user is asking...",
            raw="I'll load caveman skill since the user is asking about current architecture...",
            source="session.messages[3]",
            start_ts=time.time() - 6.0,
            end_ts=time.time() - 4.0,
        ),
        TraceRecord(
            kind=EntryKind.TOOL,
            label="skill",
            preview='skill (name: "caveman") -> cskill_content...',
            raw='{"name":"caveman","args":{}}',
            source="session.messages[4]",
            start_ts=time.time() - 4.0,
            end_ts=time.time() - 3.0,
        ),
        TraceRecord(
            kind=EntryKind.ASSISTANT,
            label="Assistant",
            preview="Drifox 当前架构是……",
            raw="Drifox 当前架构是 Python 3.14 + PyQt5...",
            source="session.messages[5]",
            start_ts=time.time() - 2.5,
            end_ts=time.time() - 0.5,
        ),
    ]
    # 直接喂数据（不走 collector）
    card._timeline.set_records(records)
    card._turn_list.set_records(records)
    card._detail.set_records(records)
    # 选中第 4 条（tool）
    card._turn_list.select(3)
    # 触发一次 paint
    card.resize(1200, 720)
    card.show()
    QCoreApplication.processEvents()
    # 不应崩
    print(f"OK card instantiated, records={len(records)}")


# ═══════════════════════════════════════════════════════════
# v3 回归：tail 稳定化 / hub 隔离 / detail tab 切换 / 流基线 / SYSTEM 投影 / 闲置间隔
# ═══════════════════════════════════════════════════════════


def _ts(seconds_ago: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(time.time() - seconds_ago).strftime("%Y-%m-%d %H:%M:%S")


def _fake_backend():
    """构造带信号的 fake backend + fake session + fake main_widget。"""
    from PyQt5.QtCore import QObject, pyqtSignal

    class _BE(QObject):
        tool_call_started = pyqtSignal(str, str, dict)
        tool_result_received = pyqtSignal(str, str, dict, bool)
        stream_started = pyqtSignal()
        stream_finished = pyqtSignal(dict)
        _hook_messages_updated = pyqtSignal()

        def __init__(self) -> None:
            super().__init__()
            self.session = type(
                "S",
                (),
                {
                    "session_id": "sess-1",
                    "messages": [
                        {"role": "user", "content": "hi", "timestamp": _ts(10.0)},
                        {"role": "assistant", "content": "old-1", "timestamp": _ts(9.0)},
                        {"role": "assistant", "content": "old-2", "timestamp": _ts(8.0)},
                    ],
                },
            )()

        def get_current_session(self):
            return self.session

    be = _BE()
    mw = type("MW", (), {"_window_id": "win-A", "backend": be})()
    return be, mw


def test_v3_tail_stability() -> None:
    """tail 内容不变时不得重复发 tailChanged（修「历史记录一直在刷新」）。"""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from PyQt5.QtCore import QCoreApplication

    _app = QCoreApplication.instance() or QCoreApplication([])
    from ui.trace_collector import TraceCollector
    from ui.trace_models import EntryKind, TraceRecord

    c = TraceCollector()
    count = {"n": 0}
    c.tailChanged.connect(lambda: count.__setitem__("n", count["n"] + 1))
    r = TraceRecord(kind=EntryKind.ASSISTANT, label="A", preview="p", raw="x", start_ts=1.0, is_pending=True)
    c._set_tail([r])
    assert_true(count["n"] == 1, f"first set_tail emits once, got {count['n']}")
    c._set_tail([r])
    assert_true(count["n"] == 1, f"same-content set_tail must not emit, got {count['n']}")
    r2 = TraceRecord(kind=EntryKind.TOOL, label="T", preview="p", raw="y", start_ts=2.0, is_pending=True)
    c._set_tail([r, r2])
    assert_true(count["n"] == 2, f"changed tail emits, got {count['n']}")
    print("✓ v3 tail stability")


def test_v3_hub_isolation() -> None:
    """每个 window_id 一个常驻 collector；同窗口幂等。"""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from PyQt5.QtCore import QCoreApplication

    _app = QCoreApplication.instance() or QCoreApplication([])
    from ui.trace_collector import TraceCollectorHub

    be, mw = _fake_backend()
    be2, mw2 = _fake_backend()
    mw2._window_id = "win-B"
    hub = TraceCollectorHub()
    c1 = hub.collector_for(mw)
    c1b = hub.collector_for(mw)
    c2 = hub.collector_for(mw2)
    assert_true(c1 is not None and c1b is c1, "same window returns same collector")
    assert_true(c2 is not None and c2 is not c1, "different windows get different collectors")
    hub.dispose()
    print("✓ v3 hub per-window isolation")


def test_v3_detail_tabs() -> None:
    """Summary/Preview/Raw/Source 切换必须真的换 QStackedWidget 页面。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    sys.path.insert(0, str(PLUGIN_ROOT))
    from ui.detail_panel import DetailPanel
    from ui.trace_models import EntryKind, TraceRecord

    p = DetailPanel()
    rec = TraceRecord(kind=EntryKind.TOOL, label="skill", preview="p", raw="RAW-CONTENT", start_ts=time.time())
    p.set_records([rec])
    p.select(0)
    p._on_seg_changed("raw")
    assert_true(p._stack.currentIndex() == 2, f"raw tab -> stack index 2, got {p._stack.currentIndex()}")
    assert_true("RAW-CONTENT" in p._raw.toPlainText(), "raw page shows content")
    p._on_seg_changed("preview")
    assert_true(p._stack.currentIndex() == 1, "preview tab -> stack index 1")
    p._on_seg_changed("source")
    assert_true(p._stack.currentIndex() == 3, "source tab -> stack index 3")
    p._on_seg_changed("summary")
    assert_true(p._stack.currentIndex() == 0, "summary tab -> stack index 0")
    print("✓ v3 detail panel tab switching")


def test_v3_stream_baseline() -> None:
    """历史 assistant 消息不得抢占新流的 timing（LLM 时长不再全 0）。"""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from PyQt5.QtCore import QCoreApplication

    _app = QCoreApplication.instance() or QCoreApplication([])
    from ui.trace_collector import TraceCollector

    be, mw = _fake_backend()
    c = TraceCollector()
    c.attach(be, mw)
    assert_true(c._stream_base == 2, f"stream_base == historical assistant count, got {c._stream_base}")
    emit_start = time.time()
    be.stream_started.emit()
    time.sleep(0.2)
    be.session.messages.append({"role": "assistant", "content": "new reply", "timestamp": _ts(0.0)})
    be.stream_finished.emit({})
    recs = c.records
    last = recs[-1]
    assert_true(last.kind.value == "ASSISTANT", "last record is assistant")
    assert_true(abs(last.start_ts - emit_start) < 0.5, f"assistant start from stream, {last.start_ts} vs {emit_start}")
    assert_true(last.duration_ms >= 100, f"assistant duration from stream >=100ms, got {last.duration_ms}")
    c.detach()
    print("✓ v3 stream baseline (no more LLM 0ms)")


def test_v3_system_prompt() -> None:
    """session.system_prompt（messages 外）必须投影为头部 SYSTEM 条目。"""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from PyQt5.QtCore import QCoreApplication

    _app = QCoreApplication.instance() or QCoreApplication([])
    from ui.trace_collector import TraceCollector
    from ui.trace_models import EntryKind

    be, mw = _fake_backend()
    be.session.system_prompt = "You are DriFox assistant."
    c = TraceCollector()
    c.attach(be, mw)
    recs = c.records
    assert_true(
        len(recs) >= 1 and recs[0].kind == EntryKind.SYSTEM,
        f"first record is SYSTEM, got {recs[0].kind if recs else None}",
    )
    assert_true(recs[0].raw == "You are DriFox assistant.", "system prompt content projected")
    assert_true(recs[0].source == "session.system_prompt", "source marks synthesized entry")
    c.detach()
    be2, mw2 = _fake_backend()
    c2 = TraceCollector()
    c2.attach(be2, mw2)
    assert_true(all(r.kind != EntryKind.SYSTEM for r in c2.records), "no SYSTEM when prompt empty")
    c2.detach()
    print("✓ v3 system prompt projection")


def test_v3_idle_gap_not_duration() -> None:
    """两条消息间隔 1 小时 → 前一条时长不得膨胀成 1h（闲置≠时长）。"""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from PyQt5.QtCore import QCoreApplication

    _app = QCoreApplication.instance() or QCoreApplication([])
    from ui.trace_collector import TraceCollector

    be, mw = _fake_backend()
    be.session.messages = [
        {"role": "user", "content": "q1", "timestamp": _ts(3600.0)},
        {"role": "assistant", "content": "a1", "timestamp": _ts(3599.0)},
        # 用户隔了 1 小时才问下一条
        {"role": "user", "content": "q2", "timestamp": _ts(0.0)},
        {"role": "assistant", "content": "a2", "timestamp": _ts(0.001)},
    ]
    c = TraceCollector()
    c.attach(be, mw)
    recs = c.records
    a1 = next(r for r in recs if r.raw == "a1")
    assert_true(a1.duration_ms == 0, f"idle gap must not count as duration, got {a1.duration_ms}")
    assert_true(a1.end_ts == 0.0, "instant message end_ts==0")
    c.detach()
    print("✓ v3 idle gap not duration")


if __name__ == "__main__":
    test_plugin_json()
    test_icons_exist()
    test_models_behavior()
    test_register_ui_signature()
    test_card_instantiation()
    test_v3_tail_stability()
    test_v3_hub_isolation()
    test_v3_detail_tabs()
    test_v3_stream_baseline()
    test_v3_system_prompt()
    test_v3_idle_gap_not_duration()
    print("\nALL SMOKE TESTS PASSED")
