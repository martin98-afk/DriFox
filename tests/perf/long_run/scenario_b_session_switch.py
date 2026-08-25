# -*- coding: utf-8 -*-
"""场景 B：会话切换压测（创建/切换/关闭会话循环）。

压测目标：
- SessionManager.create_new_session / switch_to_session / delete_session 循环
- max_cached_sessions 触发的 _evict_if_needed 路径
- _last_access dict 的 2× 容量保护路径
- 反复切换是否累积 session 对象 / QObject 树深度

循环次数：
- Demo 模式（默认）：30 秒 → ~3 千次操作
- Full 模式（LONGRUN_FULL=1）：≥5 万次操作
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, List

from .sampler import Sample, env_full_mode, start_tracemalloc, stop_tracemalloc


def run_session_switch_scenario(
    *,
    progress_cb: Callable[[int, Sample], None],
    duration_sec: float,
) -> dict:
    """执行会话切换压测；返回基线摘要 dict。"""
    from app.core.chat_session import SessionManager

    start_tracemalloc()
    t0 = time.time()

    # max_cached_sessions 设为 20 → 触发频繁淘汰路径（这是 leak 检测关键）
    sm = SessionManager(max_cached=20)

    # 注入消息负载，让每个 session 有点真实体积
    payload = "用户消息：" + ("y" * 200) + "\n助手回复：" + ("z" * 400) + "\n"

    samples: List[Sample] = []
    last_sample_at = t0
    iter_count = 0
    create_count = 0
    delete_count = 0
    switch_count = 0

    while True:
        now = time.time()
        if now - t0 >= duration_sec:
            break

        # 每轮：建一个新会话 → 灌入消息
        new_session = sm.create_new_session()
        new_session.add_user_message(payload)
        new_session.add_assistant_message(payload)
        create_count += 1
        iter_count += 1

        # 每 5 轮切换一次会话（触发 _touch_session + _evict_if_needed）
        if iter_count % 5 == 0 and len(sm.sessions) >= 2:
            target = (iter_count // 5) % len(sm.sessions)
            sm.switch_to_session(target)
            switch_count += 1

        # 当 sessions 超过 max_cached 时主动 delete 一个（覆盖两种删除入口）
        if len(sm.sessions) > sm.max_cached_sessions:
            sm.delete_session(0)
            delete_count += 1
        elif iter_count % 10 == 0 and len(sm.sessions) > 1:
            # 偶发主动 delete（即使未超 max_cached），覆盖手动删除路径
            sm.delete_session(0)
            delete_count += 1

        # 每 60 秒（demo 模式每 5 秒）采一次样
        if now - last_sample_at >= max(5.0, float(os.environ.get("PERF_SAMPLE_INTERVAL", "60"))):
            from .sampler import take_sample

            s = take_sample("B_session_switch", iter_count, t0)
            samples.append(s)
            progress_cb(iter_count, s)
            last_sample_at = now

    # 收尾：再采一次
    from .sampler import take_sample

    s = take_sample("B_session_switch", iter_count, t0)
    samples.append(s)

    elapsed = time.time() - t0
    rss_delta_mb = samples[-1].rss_mb - samples[0].rss_mb
    summary = {
        "scenario": "B_session_switch",
        "iterations": iter_count,
        "elapsed_sec": elapsed,
        "create_count": create_count,
        "delete_count": delete_count,
        "switch_count": switch_count,
        "final_session_count": len(sm.sessions),
        "final_last_access_size": len(sm._last_access),
        "rss_mb_first": samples[0].rss_mb,
        "rss_mb_last": samples[-1].rss_mb,
        "rss_delta_mb": rss_delta_mb,
        "qobject_first": samples[0].qobject_count,
        "qobject_last": samples[-1].qobject_count,
        "qobject_delta": samples[-1].qobject_count - samples[0].qobject_count,
        "samples": samples,
    }
    stop_tracemalloc()
    return summary


if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv)

    duration = 30.0
    if env_full_mode():
        duration = 1800.0
    print(f"[scenario_b] 启动，时长 {duration}s")

    def _cb(i: int, s: Sample) -> None:
        print(
            f"  iter={i}  rss={s.rss_mb:.1f}MB  qobj={s.qobject_count}  "
            f"elapsed={s.elapsed_sec:.0f}s  tm_cur={s.tracemalloc_current_mb:.2f}MB"
        )

    summary = run_session_switch_scenario(progress_cb=_cb, duration_sec=duration)
    print(
        f"[scenario_b] 完成：create={summary['create_count']} "
        f"delete={summary['delete_count']} switch={summary['switch_count']} "
        f"RSS Δ={summary['rss_delta_mb']:.1f}MB"
    )
    sys.exit(0)
