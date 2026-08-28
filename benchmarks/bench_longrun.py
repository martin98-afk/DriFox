# -*- coding: utf-8 -*-
"""基准 5：长跑模拟（消息渲染累积 + 会话切换循环）

Phase A 渲染累积：单会话追加 M 张 MessageCard（不删除，模拟长对话滚动），
测 RSS/tracemalloc 线性度。预期斜率 ≈ 单卡平均内存；显著超出 → 每卡额外泄漏。

Phase B 切换泄漏：K 个会话循环切换 N 次（引擎层 backend.switch_session +
HistoryManager 懒加载读链路 hm.get_session_by_session_id）。切换不应留存，
正斜率 → 切换链路泄漏。

运行：
  uv run python benchmarks/bench_longrun.py                 # 默认 M=60, K=8, N=40
  uv run python benchmarks/bench_longrun.py --cards 100 --switches 80
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_common as bc  # noqa: E402

import tracemalloc  # noqa: E402


def run(cards: int, switch_sessions: int, switches: int) -> dict:
    tmp = bc.setup_isolation("longrun")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    import PySide6.QtWebEngineWidgets  # noqa: F401

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)

    from app.core.webengine_profile import init_shared_web_profile

    init_shared_web_profile(parent=app)

    from app.core.backend import ChatBackend
    from app.widgets.message_card import MessageCard

    backend = ChatBackend(window_id="bench-longrun")
    backend.initialize(get_model_config=lambda: {
        "model": "bench-mock-model",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "bench-mock-key",
        "provider": "openai",
    }, workdir=str(tmp))
    bc.spin_qt_events(app, 2000)

    hm = backend.history_manager
    msg_text = "长跑模拟消息，包含**markdown**、`inline code` 与列表：\n\n- 条目一\n- 条目二\n\n" * 6

    result = {"metric": "longrun", "phaseA": {}, "phaseB": {}}

    # ================= Phase A：消息渲染累积 =================
    keep_cards = []
    xs, tm_mb, rss_mb = [], [], []
    # 预热 2 张（懒加载：viewer/JS 桥/样式缓存）
    for _ in range(2):
        c = MessageCard(role="assistant")
        c.update_content(msg_text)
        c.set_content(msg_text * 3)
        keep_cards.append(c)
    bc.spin_qt_events(app, 400)
    bc.full_gc()
    tracemalloc.start(10)

    for i in range(cards):
        c = MessageCard(role="assistant")
        c.update_content(msg_text)
        c.set_content(msg_text * 3)
        keep_cards.append(c)
        if (i + 1) % 10 == 0 or i == 0:
            bc.spin_qt_events(app, 150)
            bc.full_gc()
            xs.append(i + 1)
            tm_mb.append(bc.tracemalloc_current_mb())
            rss_mb.append(bc.rss_mb())

    tm_slope_a, tm_r2_a = bc.slope(xs, tm_mb)
    rss_slope_a, rss_r2_a = bc.slope(xs, rss_mb)
    result["phaseA"] = {
        "cards": cards,
        "tracemalloc_slope_kb_per_card": round(tm_slope_a * 1024, 1),
        "tracemalloc_r2": round(tm_r2_a, 3),
        "rss_slope_kb_per_card": round(rss_slope_a * 1024, 1),
        "rss_r2": round(rss_r2_a, 3),
        "note": "累积为预期行为；斜率≈单卡内存。异常判定看斜率是否随轮次加速",
        "samples": [
            {"cards": x, "tracemalloc_mb": round(t, 2), "rss_mb": round(r, 1)}
            for x, t, r in zip(xs, tm_mb, rss_mb)
        ],
    }
    print(f"\n===== Phase A 渲染累积（{cards} 张卡） =====")
    print(f"tracemalloc: {tm_slope_a * 1024:+.1f} KB/卡 (R²={tm_r2_a:.3f})")
    print(f"RSS:         {rss_slope_a * 1024:+.1f} KB/卡 (R²={rss_r2_a:.3f})")

    # 释放 Phase A
    for c in keep_cards:
        try:
            c.deleteLater()
        except Exception:
            pass
    keep_cards = None
    bc.spin_qt_events(app, 300)
    bc.full_gc()
    tm_after_release = bc.tracemalloc_current_mb()
    result["phaseA"]["tracemalloc_after_release_mb"] = round(tm_after_release, 2)
    result["phaseA"]["release_delta_mb"] = round(tm_mb[-1] - tm_after_release, 2)
    print(f"全部卡片释放后 tracemalloc: {tm_mb[-1]:.1f} → {tm_after_release:.1f} MB")

    # ================= Phase B：会话切换循环 =================
    # 建 K 个会话并落库（模拟历史会话），随后循环切换
    session_ids = []
    for k in range(switch_sessions):
        s = backend.create_session(trigger_hook=False)
        s.add_user_message(f"会话 {k} 用户消息 " + msg_text)
        s.add_assistant_message(f"会话 {k} 助手回复 " + msg_text * 2, model_name="bench-mock-model")
        hm.save_session(s.messages, title=f"longrun-sess-{k}", session_id=s.session_id)
        session_ids.append(s.session_id)

    # 切换预热
    for idx in range(min(3, len(session_ids))):
        backend.switch_session(idx)
        hm.get_session_by_session_id(session_ids[idx])
    bc.spin_qt_events(app, 200)
    bc.full_gc()

    snap_b0 = tracemalloc.take_snapshot()
    xs2, tm2, rss2 = [], [], []
    for i in range(switches):
        idx = i % len(session_ids)
        backend.switch_session(idx)
        # 模拟 UI 切换时的历史懒加载读
        rec = hm.get_session_by_session_id(session_ids[idx])
        rec = None
        if (i + 1) % 10 == 0 or i == 0:
            bc.spin_qt_events(app, 100)
            bc.full_gc()
            xs2.append(i + 1)
            tm2.append(bc.tracemalloc_current_mb())
            rss2.append(bc.rss_mb())

    snap_b1 = tracemalloc.take_snapshot()
    tm_slope_b, tm_r2_b = bc.slope(xs2, tm2)
    rss_slope_b, rss_r2_b = bc.slope(xs2, rss2)
    result["phaseB"] = {
        "sessions": switch_sessions,
        "switches": switches,
        "tracemalloc_slope_kb_per_switch": round(tm_slope_b * 1024, 2),
        "tracemalloc_r2": round(tm_r2_b, 3),
        "rss_slope_kb_per_switch": round(rss_slope_b * 1024, 2),
        "rss_r2": round(rss_r2_b, 3),
        "verdict_tracemalloc": bc.leak_verdict(tm_slope_b * 1024, tm_r2_b),
        "verdict_rss": bc.leak_verdict(rss_slope_b * 1024, rss_r2_b),
        "samples": [
            {"switch": x, "tracemalloc_mb": round(t, 3), "rss_mb": round(r, 1)}
            for x, t, r in zip(xs2, tm2, rss2)
        ],
        "tracemalloc_diff_top": bc.tracemalloc_diff(snap_b0, snap_b1, 10),
    }
    tracemalloc.stop()

    print(f"\n===== Phase B 会话切换（{switch_sessions} 会话 × {switches} 次） =====")
    print(f"tracemalloc: {tm_slope_b * 1024:+.2f} KB/次 (R²={tm_r2_b:.3f})")
    print(f"RSS:         {rss_slope_b * 1024:+.2f} KB/次 (R²={rss_r2_b:.3f})")
    print(f"判定(tracemalloc): {result['phaseB']['verdict_tracemalloc']}")
    print(f"判定(RSS):         {result['phaseB']['verdict_rss']}")
    print("增长 Top10:")
    for d in result["phaseB"]["tracemalloc_diff_top"][:10]:
        print(f"  +{d['size_kb']:>8.1f} KB  {d['loc']}  ({d['count']} objs)")

    try:
        backend.cleanup()
    except Exception:
        pass
    bc.save_result("longrun", result)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", type=int, default=60)
    ap.add_argument("--sessions", type=int, default=8)
    ap.add_argument("--switches", type=int, default=40)
    args = ap.parse_args()
    run(args.cards, args.sessions, args.switches)
