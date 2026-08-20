# -*- coding: utf-8 -*-
"""基准 3：会话泄漏模拟（引擎层，无真实 LLM 调用）

链路：ChatBackend.create_session → 塞 M 条消息 →
HistoryManager.save_session（完整持久化链）→ backend.delete_session
（联动 history_manager.remove_session + session_store）。

每轮结束后 tracemalloc 采样，N 轮回归斜率 → KB/次。
正斜率显著 → 会话"新建→关闭"链路有残留。

运行：
  uv run python benchmarks/bench_session_leak.py            # 默认 N=40, M=20
  uv run python benchmarks/bench_session_leak.py --rounds 60 --msgs 40
"""

from __future__ import annotations

import argparse
import os
import sys

# 注意：不用 QT_QPA_PLATFORM=offscreen——实测 offscreen 下 design_tokens
# _apply_tooltip_style 会 0xC0000005 访问冲突。用真实平台 QApplication，
# 不创建任何顶层窗口则不弹窗（bench_startup 同策略）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_common as bc  # noqa: E402

import tracemalloc  # noqa: E402


def make_model_config() -> dict:
    """最小模型配置（不走真实网络，仅初始化数据结构）。"""
    return {
        "model": "bench-mock-model",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "bench-mock-key",
        "provider": "openai",
    }


def run(rounds: int, msgs_per_round: int) -> dict:
    tmp = bc.setup_isolation("sessleak")

    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)

    from app.core.backend import ChatBackend

    backend = ChatBackend(window_id="bench-sessleak")
    backend.initialize(get_model_config=make_model_config, workdir=str(tmp))

    # 等延迟组件（QTimer 0/200/400/600ms 错峰创建）完成
    bc.spin_qt_events(app, 2500)

    store = backend.session_store
    hm = backend.history_manager

    # 生成固定消息语料（每条 ~1KB，模拟真实对话段落）
    corpus = [
        ("user", ("用户提问段落，" * 40) + str(i)) for i in range(msgs_per_round // 2)
    ] + [
        ("assistant", ("助手回答段落，" * 60) + str(i)) for i in range(msgs_per_round // 2)
    ]

    # 预热一轮（首次路径触发懒加载/缓存建表）
    warm = backend.create_session(trigger_hook=False)
    warm.add_user_message("warmup")
    hm.save_session(warm.messages, title="warmup", session_id=warm.session_id)
    idx = len(backend.get_all_sessions()) - 1
    backend.delete_session(idx)
    store.delete_session(warm.session_id)
    bc.full_gc()

    # 循环期才开 tracemalloc（排除初始化分配）
    tracemalloc.start(10)
    snap_before = tracemalloc.take_snapshot()

    xs, tm_mb, rss_mb = [], [], []
    samples = []
    session_counts = []
    for i in range(rounds):
        session = backend.create_session(trigger_hook=False)
        for role, text in corpus:
            if role == "user":
                session.add_user_message(text)
            else:
                session.add_assistant_message(text, model_name="bench-mock-model")
        hm.save_session(
            session.messages,
            title=f"bench-round-{i}",
            session_id=session.session_id,
        )
        idx = len(backend.get_all_sessions()) - 1
        backend.delete_session(idx)
        store.delete_session(session.session_id)
        session_counts.append(len(backend.get_all_sessions()))

        if (i + 1) % 5 == 0 or i == 0:
            bc.full_gc()
            xs.append(i + 1)
            tm = bc.tracemalloc_current_mb()
            rss = bc.rss_mb()
            tm_mb.append(tm)
            rss_mb.append(rss)
            samples.append({"round": i + 1, "tracemalloc_mb": round(tm, 3), "rss_mb": round(rss, 1)})

    snap_after = tracemalloc.take_snapshot()

    tm_slope, tm_r2 = bc.slope(xs, tm_mb)
    rss_slope, rss_r2 = bc.slope(xs, rss_mb)

    # 残留对象计数（应归零/稳定）
    from collections import Counter

    bc.full_gc()
    counter = Counter(type(o).__name__ for o in __import__("gc").get_objects())
    residual = {k: counter.get(k, 0) for k in ("ChatSession", "dict", "list", "str")}

    result = {
        "metric": "session_leak",
        "rounds": rounds,
        "msgs_per_round": msgs_per_round,
        "bytes_per_msg": "~1KB",
        "samples": samples,
        "tracemalloc_slope_mb_per_round": round(tm_slope, 5),
        "tracemalloc_slope_kb_per_round": round(tm_slope * 1024, 2),
        "tracemalloc_r2": round(tm_r2, 3),
        "rss_slope_kb_per_round": round(rss_slope * 1024, 2),
        "rss_r2": round(rss_r2, 3),
        "verdict_tracemalloc": bc.leak_verdict(tm_slope * 1024, tm_r2),
        "verdict_rss": bc.leak_verdict(rss_slope * 1024, rss_r2),
        "residual_object_counts": residual,
        "session_count_first": session_counts[0],
        "session_count_last": session_counts[-1],
        "session_count_stable": len(set(session_counts)) == 1,
        "tracemalloc_diff_top": bc.tracemalloc_diff(snap_before, snap_after, 15),
    }
    tracemalloc.stop()

    print(f"\n===== 会话泄漏基准（{rounds} 轮 × {msgs_per_round} 条/轮） =====")
    print(f"tracemalloc 斜率: {tm_slope * 1024:+.2f} KB/次 (R²={tm_r2:.3f})")
    print(f"RSS 斜率:         {rss_slope * 1024:+.2f} KB/次 (R²={rss_r2:.3f})")
    print(f"判定(tracemalloc): {result['verdict_tracemalloc']}")
    print(f"判定(RSS):         {result['verdict_rss']}")
    print(
        f"会话数归位: {result['session_count_first']} → {result['session_count_last']} "
        f"(稳定={result['session_count_stable']})"
    )
    print("残留对象:", residual)
    print("增长 Top10:")
    for d in result["tracemalloc_diff_top"][:10]:
        print(f"  +{d['size_kb']:>8.1f} KB  {d['loc']}  ({d['count']} objs)")

    # 清理（走 backend cleanup 释放线程池等）
    try:
        backend.cleanup()
    except Exception:
        pass
    bc.save_result("session_leak", result)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--msgs", type=int, default=20)
    args = ap.parse_args()
    run(args.rounds, args.msgs)
