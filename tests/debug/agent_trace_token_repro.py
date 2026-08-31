# -*- coding: utf-8 -*-
"""[agent_trace-token-repro] 复现 assistant Tokens 列显示总量而非单回复占用。

跑法：uv run python -X utf8 tests/debug/agent_trace_token_repro.py
"""
import sys

sys.path.insert(0, ".")

from PyQt5.QtCore import QCoreApplication

app = QCoreApplication.instance() or QCoreApplication([])

from plugins.agent_trace.ui.trace_collector import TraceCollector


def main() -> int:
    c = TraceCollector()
    msgs = [
        {"role": "user", "content": "你好", "timestamp": 1000.0},
        # 第1轮回复：API 返回 input=5000(全上下文), output=120(本回复), total=5120
        {
            "role": "assistant",
            "content": "第一轮回复" * 5,
            "timestamp": 1001.0,
            "token_usage": {"input": 5000, "output": 120, "total": 5120},
        },
        {"role": "user", "content": "继续", "timestamp": 1002.0},
        # 第2轮回复：input=5300(上下文更大), output=200, total=5500
        {
            "role": "assistant",
            "content": "第二轮回复" * 8,
            "timestamp": 1003.0,
            "token_usage": {"input": 5300, "output": 200, "total": 5500},
        },
    ]
    recs = c._project_messages(msgs)
    print("投影结果:")
    for r in recs:
        print(f"  {r.label:16} tokens={r.meta.get('tokens')}  exact={r.meta.get('tokens_exact')}")

    exact = [r for r in recs if r.meta.get("tokens_exact")]
    shown = [r.meta.get("tokens") for r in exact]
    expected = [120, 200]  # 每个回复实际输出 (completion_tokens)
    print(f"\n预期 Tokens 列（单回复实际占用）: {expected}")
    print(f"当前实际 Tokens 列: {shown}")
    ok = shown == expected
    print("PASS" if ok else "FAIL — 显示的是 input+output 总量而非单回复占用")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
