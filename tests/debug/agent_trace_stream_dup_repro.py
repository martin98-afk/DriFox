# -*- coding: utf-8 -*-
"""[agent_trace-stream-dup] 复现 stream_started 双发导致的轨迹异常。

真实链路（非 api_mode）：
  executor.execute() 内部 cb() → adapter → engine._emit("stream_started")   # 第 1 次
  engine._start_worker() 尾部 self._emit("stream_started")                  # 第 2 次（engine.py:745）
→ collector._on_stream_started 被连调 2 次。

跑法：uv run python -X utf8 tests/debug/agent_trace_stream_dup_repro.py
"""
import sys
import time

sys.path.insert(0, ".")

from PyQt5.QtCore import QCoreApplication

app = QCoreApplication.instance() or QCoreApplication([])

from plugins.agent_trace.ui.trace_collector import TraceCollector
from plugins.agent_trace.ui.trace_models import EntryKind


def check_tail_dup() -> bool:
    """后果①：一次对话产生 2 条 pending assistant tail（列表底部残留项）。"""
    c = TraceCollector()
    c._on_stream_started()  # executor 内部 cb() 发的第 1 次
    c._on_stream_started()  # engine._start_worker 尾部发的第 2 次
    tail = c.tail
    pending = [r for r in tail if r.kind == EntryKind.ASSISTANT and r.is_pending]
    print(f"[tail] 双发后 pending assistant tail 数量 = {len(pending)}")
    if len(pending) > 1:
        d = abs(pending[0].start_ts - pending[1].start_ts)
        print(f"[tail] 两条 start_ts 差 = {d:.6f}s（用户看到：2 项、时长一模一样）")
    return len(pending) == 1


def check_timing_mismatch() -> bool:
    """后果②：_streams 每轮 2 条 vs 落盘 assistant 每轮 1 条 → 流配对错位。

    第 1 轮：双发 → _streams[0..1]，时长=整轮 worker 生命周期（非常长）。
    第 2 轮：又双发 → _streams[2..3]。但第 2 轮的 assistant 投影时
    assistant_seq-_stream_base=1 → 取到 _streams[1]（**第 1 轮的流**）
    → 该条记录的起止根本不属于本轮 → 「不在实际对话里」。
    """
    c = TraceCollector()
    # 第 1 轮
    c._on_stream_started()
    c._on_stream_started()
    time.sleep(0.05)
    c._on_stream_finished({})  # 两条流都被闭合为同一 end
    msgs1 = [
        {"role": "user", "content": "q1", "timestamp": time.time()},
        {"role": "assistant", "content": "a1", "timestamp": time.time() + 0.01},
    ]
    c._sync = lambda *a, **k: None  # 屏蔽 backend 同步，直接测投影
    recs1 = c._project_messages(msgs1)
    a1 = [r for r in recs1 if r.kind == EntryKind.ASSISTANT][0]
    dur1 = (a1.end_ts - a1.start_ts) * 1000
    # [DEBUG-dup] 探针：流表状态与 a1 配对输入
    print(f"[debug] _streams={c._streams}  _stream_base={c._stream_base}")
    print(f"[debug] a1 start={a1.start_ts:.3f} end={a1.end_ts:.3f}")
    # 第 2 轮（间隔 1s，模拟用户再次提问）
    time.sleep(0.05)
    c._on_stream_started()
    c._on_stream_started()
    time.sleep(0.05)
    c._on_stream_finished({})
    msgs2 = msgs1 + [
        {"role": "user", "content": "q2", "timestamp": time.time()},
        {"role": "assistant", "content": "a2", "timestamp": time.time() + 0.01},
    ]
    recs2 = c._project_messages(msgs2)
    assistants = [r for r in recs2 if r.kind == EntryKind.ASSISTANT]
    a1, a2 = assistants[0], assistants[1]  # msgs2 含 a1/a2 两条 assistant
    dur1b = (a1.end_ts - a1.start_ts) * 1000
    dur2 = (a2.end_ts - a2.start_ts) * 1000
    # [DEBUG-dup] 探针：第二轮流表与配对输入
    print(f"[debug] 第二轮 _streams={c._streams}  _stream_base={c._stream_base}")
    print(f"[debug] a1 start={a1.start_ts:.3f} end={a1.end_ts:.3f} | a2 start={a2.start_ts:.3f} end={a2.end_ts:.3f}")
    overlap = a2.start_ts < a1.end_ts  # 第 2 轮 assistant 的起止落在第 1 轮时间窗内
    print(f"[timing] a1 duration={dur1b:.1f}ms  a2 duration={dur2:.1f}ms")
    print(f"[timing] a2.start({a2.start_ts:.3f}) < a1.end({a1.end_ts:.3f}) → {overlap}")
    ok = not overlap
    print("[timing] PASS" if ok else "[timing] FAIL — 第2轮 assistant 配到了第1轮的流（时长错位/回溯）")
    return ok


def check_landed_cleans_tail() -> bool:
    """后果③：stream_finished 丢失（手动停止/异常中断）时，assistant 一旦
    落盘，pending 尾巴必须被清掉（README「落盘后自动被正式记录取代」）。"""
    c = TraceCollector()
    c._on_stream_started()  # 1 条 pending tail，finished 将不再到来
    # 模拟 backend 会话已有落盘 assistant 消息（finished 丢失的 stop 路径）
    fake_msgs = [
        {"role": "user", "content": "q", "timestamp": time.time()},
        {"role": "assistant", "content": "a", "timestamp": time.time() + 0.01},
    ]

    class _FakeSession:  # noqa: D401
        session_id = "fake"
        messages = fake_msgs
        system_prompt = ""

    c._current_session = lambda: _FakeSession()  # type: ignore[method-assign]
    c._sync()
    leftover = [r for r in c.tail if r.kind == EntryKind.ASSISTANT and r.is_pending]
    print(f"[landed] 落盘后残留 pending assistant tail = {len(leftover)}")
    return not leftover


def main() -> int:
    print("=== 复现 stream_started 双发的可见后果 ===\n")
    t1 = check_tail_dup()
    print()
    t2 = check_timing_mismatch()
    print()
    t3 = check_landed_cleans_tail()
    print()
    print("=== 结论 ===")
    print(f"tail 双条残留:  {'复现' if not t1 else '未复现'}")
    print(f"流配对错位:    {'复现' if not t2 else '未复现'}")
    print(f"落盘后尾巴清场: {'通过' if t3 else '失败'}")
    return 0 if (t1 and t2 and t3) else 1


if __name__ == "__main__":
    sys.exit(main())
