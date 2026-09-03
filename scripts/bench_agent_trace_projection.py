# -*- coding: utf-8 -*-
"""agent_trace 投影性能基线：定位 _sync 的热点。

运行：
    .venv/Scripts/python.exe scripts/bench_agent_trace_projection.py
"""

from __future__ import annotations

import cProfile
import io
import os
import pstats
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath("."))

from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])  # noqa: F841

sys.path.insert(0, os.path.abspath("plugins/agent_trace"))

from ui.trace_collector import TraceCollector  # noqa: E402


def build_messages(n_turns: int) -> list:
    """模拟一个 n_turns 轮的会话：每轮 user + hook + assistant + tool。"""
    import datetime as _dt

    base = time.time() - n_turns * 60
    msgs: list = []
    for t in range(n_turns):
        ts = _dt.datetime.fromtimestamp(base + t * 60).strftime("%Y-%m-%d %H:%M:%S")
        msgs.append(
            {
                "role": "user",
                "content": "请帮我重构这个函数并处理边界情况 " * 6,
                "timestamp": ts,
                "ts_ms": int((base + t * 60) * 1000),
            }
        )
        msgs.append(
            {
                "role": "user",
                "_hook_event": "PreToolUse",
                "content": "当前工作区状态：分支 main，有 12 个未跟踪文件。\n" + "上下文注入内容 " * 40,
                "timestamp": ts,
                "ts_ms": int((base + t * 60 + 1) * 1000),
            }
        )
        msgs.append(
            {
                "role": "assistant",
                "content": "我先读一下相关文件，然后重构。\n" + "思考过程与计划 " * 30,
                "timestamp": ts,
                "ts_ms": int((base + t * 60 + 3) * 1000),
                "elapsed_ms": 2400.0,
                "token_usage": {"input": 12000, "output": 320, "total": 12320},
                "tool_calls": [{"id": f"c{t}", "function": {"name": "read_file"}}],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "name": "read_file",
                "tool_call_id": f"c{t}",
                "arguments": {"path": f"src/mod_{t}.py", "limit": 200},
                "content": "def foo():\n    pass\n" * 60,
                "timestamp": ts,
                "ts_ms": int((base + t * 60 + 8) * 1000),
                "trace_phases": {"perm": 100.0, "exec": 830.0, "other": 2.0, "total": 932.0},
            }
        )
    return msgs


def bench(n_turns: int, repeats: int = 20) -> float:
    msgs = build_messages(n_turns)
    c = TraceCollector()
    # 预热（首次会建 token 缓存）
    c._project_messages(msgs)
    t0 = time.perf_counter()
    for _ in range(repeats):
        c._project_messages(msgs)
    return (time.perf_counter() - t0) / repeats * 1000


def main() -> int:
    print(f"消息数 → 单次 _project_messages 耗时（均值，20 次）")
    for turns in (10, 25, 50, 100, 200):
        n = turns * 4
        ms = bench(turns)
        print(f"  {n:5d} 条  {ms:8.2f} ms")

    msgs = build_messages(100)
    c = TraceCollector()
    pr = cProfile.Profile()
    pr.enable()
    for _ in range(10):
        c._project_messages(msgs)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(14)
    print("\n=== profile (400 条 × 10 次) ===")
    print("\n".join(s.getvalue().splitlines()[:30]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
