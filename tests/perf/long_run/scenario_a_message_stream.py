# -*- coding: utf-8 -*-
"""场景 A：消息流压测（高频 send_message + 接收流式 token）。

压测目标：
- ChatSession.add_user_message + 模拟 stream_chunk 信号 emit
- SessionManager 会话持续累积消息时的内存增长
- 信号回调闭包是否随循环泄漏

循环次数：
- Demo 模式（默认）：30 秒 → ~3 千次 send_message
- Full 模式（LONGRUN_FULL=1）：≥5 万次 send_message

输出：每个采样点一个 Sample，落 CSV/JSON 由 runner.py 统一处理。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, List

from .sampler import Sample, env_full_mode, start_tracemalloc, stop_tracemalloc


def _build_session_manager():
    """构造 SessionManager（PySide6 QApplication 必须已建）。

    延迟 import：避免在 conftest 建 QApplication 之前触发 PySide6 import。
    """
    from app.core.chat_session import SessionManager

    return SessionManager()


def _build_dummy_session_payload() -> str:
    """模拟一段用户消息（与真实 send_message 调用 size 接近）。"""
    return "请帮我分析这段代码：\n" + ("x = 1\n" * 30)


def _build_dummy_stream_chunks(n: int = 20) -> List[str]:
    """模拟 LLM 流式响应切片。"""
    chunks: List[str] = []
    for i in range(n):
        chunks.append(f"[chunk#{i:03d}] " + ("abcdefgh" * 5) + "\n")
    return chunks


def _wire_stream_sink(stream_chunk_signal, on_chunk: Callable[[str], None]) -> None:
    """挂载 stream_chunk 信号槽（直接 connect，避免 Qt 事件循环依赖）。

    PySide6 在同一线程内 emit → connect 的 Lambda 是同步执行的，可立即拿到 chunk。
    """
    stream_chunk_signal.connect(on_chunk)


def run_message_stream_scenario(
    *,
    progress_cb: Callable[[int, Sample], None],
    duration_sec: float,
) -> dict:
    """执行消息流压测；返回基线摘要 dict。

    循环结构（每轮）：
    1. create_new_session（带消息负载）
    2. 模拟 stream_chunk 信号（高频 emit）
    3. 触发 _evict_if_needed（max_cached=10）→ 删除最旧的会话
    4. gc.collect() 主动回收（让 ChatSession 释放看得见）

    设计要点：必须 create+delete 配对，否则 ChatSession 无限累积 → RSS 必然线性增长
    （此时无法判定"是否泄漏"）。本场景通过 max_cached 触发删除路径，观测：
    - delete_session 后 ChatSession 是否被真释放（biz_object_counts['ChatSession']）
    - stream_chunk 信号回调是否随 QObject 释放
    - messages 累积是否触发 context_usage 单调上涨
    """
    from app.core.chat_session import SessionManager
    from PySide6.QtCore import QObject, Signal
    import gc as _gc

    start_tracemalloc()
    t0 = time.time()

    # --- 构造一个本地 chat_backend stub：只为压测流式信号链 ---
    class _StreamStub(QObject):
        stream_started = Signal()
        stream_chunk = Signal(str)
        stream_finished = Signal(dict)

        def __init__(self):
            super().__init__()
            self.chunk_received_count = 0
            self.message_count = 0
            self._chunks_per_msg = 8

        def on_chunk(self, _chunk: str) -> None:
            self.chunk_received_count += 1

        def on_finished(self, _msg: dict) -> None:
            self.message_count += 1

        def fake_send_message(self, text: str, sid: str) -> None:
            """模拟 send_message：emit stream_started + 多 chunk + finished。"""
            self.stream_started.emit()
            for c in _build_dummy_stream_chunks(self._chunks_per_msg):
                self.stream_chunk.emit(c)
            self.stream_finished.emit(
                {
                    "role": "assistant",
                    "content": text + "\n[assistant reply]",
                    "session_id": sid,
                }
            )

    # max_cached=10：高频触发淘汰 → delete_session 路径被反复执行
    sm = SessionManager(max_cached=10)
    stub = _StreamStub()
    _wire_stream_sink(stub.stream_chunk, stub.on_chunk)
    _wire_stream_sink(stub.stream_finished, stub.on_finished)

    # --- 主循环 ---
    samples: List[Sample] = []
    last_sample_at = t0
    iter_count = 0

    while True:
        now = time.time()
        if now - t0 >= duration_sec:
            break

        # 每轮：建一个新会话、发一条消息
        session = sm.create_new_session()
        text = _build_dummy_session_payload()
        session.add_user_message(text)
        session.add_assistant_message("[assistant reply] " + "z" * 200)
        stub.fake_send_message(text, session.session_id)

        iter_count += 1

        # 主动 delete：覆盖 create+delete 闭环，量化泄漏
        # 设计：每 100 轮 + 当 sessions 超过 8（max_cached 的 80%）时
        if len(sm.sessions) > 8:
            sm.delete_session(0)
        elif iter_count % 100 == 0:
            _gc.collect()
            if len(sm.sessions) > 1:
                sm.delete_session(0)

        # 每 60 秒（demo 模式每 5 秒）采一次样
        if now - last_sample_at >= max(5.0, float(os.environ.get("PERF_SAMPLE_INTERVAL", "60"))):
            from .sampler import take_sample

            s = take_sample("A_message_stream", iter_count, t0)
            samples.append(s)
            progress_cb(iter_count, s)
            last_sample_at = now

    # 收尾：gc + 采样
    _gc.collect()
    from .sampler import take_sample

    s = take_sample("A_message_stream", iter_count, t0)
    samples.append(s)

    elapsed = time.time() - t0
    rss_delta_mb = samples[-1].rss_mb - samples[0].rss_mb
    summary = {
        "scenario": "A_message_stream",
        "iterations": iter_count,
        "elapsed_sec": elapsed,
        "messages": stub.message_count,
        "chunks": stub.chunk_received_count,
        "final_session_count": len(sm.sessions),
        "final_last_access_size": len(sm._last_access),
        "rss_mb_first": samples[0].rss_mb,
        "rss_mb_last": samples[-1].rss_mb,
        "rss_delta_mb": rss_delta_mb,
        "qobject_first": samples[0].qobject_count,
        "qobject_last": samples[-1].qobject_count,
        "qobject_delta": samples[-1].qobject_count - samples[0].qobject_count,
        "biz_first": samples[0].biz_object_counts.copy(),
        "biz_last": samples[-1].biz_object_counts.copy(),
        "samples": samples,
    }
    stop_tracemalloc()
    return summary


if __name__ == "__main__":
    # 独立运行（需先有 QApplication）
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv)

    duration = 30.0
    if env_full_mode():
        duration = 1800.0  # 30 分钟
    print(f"[scenario_a] 启动，时长 {duration}s")

    def _cb(i: int, s: Sample) -> None:
        print(
            f"  iter={i}  rss={s.rss_mb:.1f}MB  qobj={s.qobject_count}  "
            f"elapsed={s.elapsed_sec:.0f}s  tm_cur={s.tracemalloc_current_mb:.2f}MB"
        )

    summary = run_message_stream_scenario(progress_cb=_cb, duration_sec=duration)
    print(f"[scenario_a] 完成：{summary['iterations']} 次，RSS Δ={summary['rss_delta_mb']:.1f}MB")
    sys.exit(0)
