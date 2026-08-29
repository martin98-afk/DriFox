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


if __name__ == "__main__":
    test_plugin_json()
    test_icons_exist()
    test_models_behavior()
    test_register_ui_signature()
    test_card_instantiation()
    print("\nALL SMOKE TESTS PASSED")
