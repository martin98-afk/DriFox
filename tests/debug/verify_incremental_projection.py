# -*- coding: utf-8 -*-
"""一次性验证：_try_incremental 修复后命中且与全量投影一致。用完可删。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.agent_trace.ui.trace_collector import TraceCollector  # noqa: E402


def make_msg(role: str, text: str, ts_ms: int) -> dict:
    return {"role": role, "content": text, "ts_ms": ts_ms}


def make_session(n: int) -> object:
    """鸭子类型 session：messages + system_prompt。"""
    msgs = []
    for i in range(n):
        if i % 4 == 0:
            msgs.append(make_msg("user", f"问题 {i}", 1_700_000_000_000 + i * 1000))
        elif i % 4 == 2:
            msgs.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": f"tc{i}", "function": {"name": "read", "arguments": "{}"}}],
                    "ts_ms": 1_700_000_000_000 + i * 1000,
                }
            )
        else:
            msgs.append(
                {
                    "role": "tool",
                    "content": f"结果 {i}",
                    "tool_call_id": f"tc{i - 1}",
                    "ts_ms": 1_700_000_000_000 + i * 1000,
                }
            )

    class S:
        session_id = "s1"
        messages = msgs
        system_prompt = "你是测试助手"

    return S()


def main() -> None:
    c = TraceCollector()
    n = 300
    session = make_session(n)
    msgs = session.messages

    # 第一次：全量投影
    r1 = c._project(list(msgs), session.system_prompt)
    assert len(r1) == n + 1, f"全量形状错: {len(r1)} != {n + 1}"
    assert r1[0].source == "session.system_prompt"
    assert r1[1].source.startswith("messages[0]"), r1[1].source

    # 第二次：尾部追加 3 条（前缀对象身份不变）→ 必须命中增量
    msgs.extend(
        [
            make_msg("user", "新问题", 1_700_000_100_000),
            make_msg("assistant", "新回答", 1_700_000_101_000),
            make_msg("user", "再来", 1_700_000_102_000),
        ]
    )

    calls = {"n": 0}
    orig = TraceCollector._project_messages

    def counting(self, *a, **kw):
        calls["n"] += 1
        return orig(self, *a, **kw)

    TraceCollector._project_messages = counting
    try:
        r2 = c._project(list(msgs), session.system_prompt)
    finally:
        TraceCollector._project_messages = orig

    assert calls["n"] == 1, f"增量未命中：_project_messages 被调 {calls['n']} 次（应恰好 1 次=尾部片）"
    assert len(r2) == len(msgs) + 1, f"增量形状错: {len(r2)} != {len(msgs) + 1}"

    # 正确性：增量结果 == 重新全量投影（全量路径自身会合成 SYSTEM 行）
    r_full = c._project_messages(list(msgs), system_prompt=session.system_prompt)
    assert len(r_full) == len(r2)
    for i, (a, b) in enumerate(zip(r2, r_full)):
        assert a.kind == b.kind and a.label == b.label and a.turn_no == b.turn_no, (
            f"第 {i} 条不一致: {a.kind}/{a.turn_no} vs {b.kind}/{b.turn_no}"
        )
        assert a.source == b.source, f"第 {i} 条 source 不一致: {a.source} vs {b.source}"
        assert round(a.start_ts, 3) == round(b.start_ts, 3), f"第 {i} 条 ts 不一致"
        assert a.preview == b.preview, f"第 {i} 条 preview 不一致"

    # 轮次号续接：最后一条的 turn_no 不低于中位（未归零）
    turns = [r.turn_no for r in r2]
    assert turns[-1] >= turns[n // 2] > 0, f"轮次号未续接: tail={turns[-3:]}"

    # 截断场景：长度变小 → 回退全量，不崩
    del msgs[-5:]
    r3 = c._project(list(msgs), session.system_prompt)
    assert len(r3) == len(msgs) + 1

    # system_prompt 变化 → 回退全量
    session.system_prompt = "换了提示词"
    r4 = c._project(list(msgs), session.system_prompt)
    assert r4[0].preview.startswith("换了"), r4[0].preview

    print("OK：增量命中 1 次、结果与全量一致、轮次续接、截断/换提示词正确回退")


if __name__ == "__main__":
    main()
