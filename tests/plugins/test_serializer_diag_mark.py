# -*- coding: utf-8 -*-
"""[E8-卡E] serialize_responses 耗时 diag 打点回归。

守卫：打点存在、数字合理、序列化结果与打点无关（行为等价）。
打点形态照 card_viewers/subagent_worker 先例：trace_enabled() 源码默认开、
PROBE=0/打包版关，零关态开销。
"""

import json
import sys
from pathlib import Path

import pytest
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.plugins.contracts.message_serializer import SerializeContext  # noqa: E402
from app.plugins.contracts.model_adapter import ProtocolFlags  # noqa: E402

@pytest.fixture()
def serializer():
    from importlib import import_module

    mod = import_module("plugins.system-serializers.serializers.openai")
    return mod.OpenAIChatSerializer()

def _sample_messages() -> list:
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
        {"role": "user", "content": "跑个命令"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]

def _make_ctx() -> SerializeContext:
    return SerializeContext(flags=ProtocolFlags(), supports_vision=False)

def test_diag_mark_emitted_with_reasonable_numbers(serializer, monkeypatch):
    """源码运行（trace 默认开）打点输出：含耗时数字与条数统计。"""
    monkeypatch.delenv("DRIFOX_STARTUP_PROBE", raising=False)
    records: list = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="DEBUG")
    try:
        items, instructions = serializer.serialize_responses(_sample_messages(), _make_ctx())
    finally:
        logger.remove(sink_id)
    assert items, "样例应产出 input_items"
    hits = [r for r in records if "[responses-serialize]" in r]
    assert hits, f"应输出 [responses-serialize] 打点日志，实捕 {len(records)} 条"
    msg = hits[-1]
    assert "ms" in msg and "msgs=6" in msg and f"items={len(items)}" in msg, f"打点应含耗时与条数: {msg}"

def test_diag_disabled_no_log(serializer, monkeypatch):
    """PROBE=0 → 打点关闭：无日志行、结果不受影响。"""
    monkeypatch.setenv("DRIFOX_STARTUP_PROBE", "0")
    records: list = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="DEBUG")
    try:
        items, _ = serializer.serialize_responses(_sample_messages(), _make_ctx())
    finally:
        logger.remove(sink_id)
    assert items
    assert not [r for r in records if "[responses-serialize]" in r]

def test_serialization_result_unchanged_by_diag(serializer):
    """行为等价：产物与硬编码期望结构对照（打点不改变序列化逻辑）。"""
    items, instructions = serializer.serialize_responses(_sample_messages(), _make_ctx())
    assert instructions == "你是助手"
    types = [i["type"] for i in items]
    assert types == ["message", "message", "message", "function_call", "function_call_output"], types
    assert json.dumps(items, ensure_ascii=False).count("call_1") == 2

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
