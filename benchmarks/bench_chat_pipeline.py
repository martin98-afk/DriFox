# -*- coding: utf-8 -*-
"""基准 4：对话残留模拟（消息管线一轮，无真实 LLM）

组件级模拟一次完整对话轮（引擎 + 渲染组件，不开主窗口）：
  session.add_user_message → MessageCard(role=assistant) 创建 →
  update_content × K 流式块（走 CodeWebViewer 渲染管线）→
  set_content 定稿 → session.add_assistant_message →
  message_content.messages_to_api 序列化链 →
  HistoryManager.save_session（历史列表）→
  卡片 deleteLater + sip.delete 释放。

每轮结束后 tracemalloc 采样，N 轮回归斜率 + MessageCard 残留计数。

运行：
  uv run python benchmarks/bench_chat_pipeline.py          # 默认 N=30, K=40 块
  uv run python benchmarks/bench_chat_pipeline.py --rounds 50 --chunks 80
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_common as bc  # noqa: E402

import tracemalloc  # noqa: E402


def run(rounds: int, chunks: int, render: bool = True) -> dict:
    tmp = bc.setup_isolation("chatpipe")

    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    # WebEngine 约束：必须在 QApplication 创建前导入（bench_startup 同款）
    import PyQt5.QtWebEngineWidgets  # noqa: F401

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)

    # CodeWebViewer 依赖共享 WebEngine Profile（同 main.py DeferredStartup）
    from app.core.webengine_profile import init_shared_web_profile

    init_shared_web_profile(parent=app)

    from app.core.backend import ChatBackend
    from app.core.message_content import messages_to_api
    from app.widgets.message_card import MessageCard

    backend = ChatBackend(window_id="bench-chatpipe")
    backend.initialize(get_model_config=lambda: {
        "model": "bench-mock-model",
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "bench-mock-key",
        "provider": "openai",
    }, workdir=str(tmp))
    bc.spin_qt_events(app, 2000)

    hm = backend.history_manager

    # 流式块语料：每块 ~200B，模拟 markdown 密集输出
    chunk_text = "流式输出内容块，包含**加粗**、`代码`与换行。\n\n" * 4

    # 预热一轮（触发懒加载：MessageCard viewer / JS 桥 / history 缓存）
    warm_session = backend.create_session(trigger_hook=False)
    warm_session.add_user_message("warmup")
    warm_card = MessageCard(role="assistant")
    if render:
        warm_card.update_content(chunk_text)
        warm_card.set_content(chunk_text * 3)
    warm_session.add_assistant_message(chunk_text * 3, model_name="bench-mock-model")
    messages_to_api(warm_session.messages)
    hm.save_session(warm_session.messages, title="warmup", session_id=warm_session.session_id)
    warm_card.deleteLater()
    bc.spin_qt_events(app, 300)
    bc.full_gc()

    tracemalloc.start(10)
    snap_before = tracemalloc.take_snapshot()

    xs, tm_mb, rss_mb, card_counts = [], [], [], []
    samples = []
    for i in range(rounds):
        session = backend.create_session(trigger_hook=False)

        # 用户消息
        session.add_user_message("用户问题：" + chunk_text * 2)

        # 助手卡片：流式 K 块（render=False 对照组：只建卡不走渲染管线）
        card = MessageCard(role="assistant")
        if render:
            for _ in range(chunks):
                card.update_content(chunk_text)
        full = chunk_text * chunks
        card.set_content(full)
        session.add_assistant_message(full, model_name="bench-mock-model")

        # 序列化链（消息对象转换）
        api_msgs = messages_to_api(session.messages)

        # 历史持久化（历史列表缓存链路）
        hm.save_session(
            session.messages,
            title=f"pipe-round-{i}",
            session_id=session.session_id,
        )

        # 释放：卡片 deleteLater（Qt 侧）+ 引用置空
        card.deleteLater()
        card = None
        api_msgs = None
        sid = session.session_id
        session = None
        backend.delete_session(len(backend.get_all_sessions()) - 1)
        backend.session_store.delete_session(sid)

        if (i + 1) % 5 == 0 or i == 0:
            bc.spin_qt_events(app, 200)
            bc.full_gc()
            xs.append(i + 1)
            tm = bc.tracemalloc_current_mb()
            rss = bc.rss_mb()
            tm_mb.append(tm)
            rss_mb.append(rss)

            import gc as _gc

            card_counts.append(sum(1 for o in _gc.get_objects() if type(o).__name__ == "MessageCard"))
            samples.append(
                {
                    "round": i + 1,
                    "tracemalloc_mb": round(tm, 3),
                    "rss_mb": round(rss, 1),
                    "messagecard_count": card_counts[-1],
                }
            )

    snap_after = tracemalloc.take_snapshot()
    tm_slope, tm_r2 = bc.slope(xs, tm_mb)
    rss_slope, rss_r2 = bc.slope(xs, rss_mb)

    result = {
        "metric": "chat_pipeline_leak",
        "rounds": rounds,
        "chunks_per_round": chunks,
        "chunk_bytes": len(chunk_text.encode("utf-8")),
        "samples": samples,
        "tracemalloc_slope_kb_per_round": round(tm_slope * 1024, 2),
        "tracemalloc_r2": round(tm_r2, 3),
        "rss_slope_kb_per_round": round(rss_slope * 1024, 2),
        "rss_r2": round(rss_r2, 3),
        "verdict_tracemalloc": bc.leak_verdict(tm_slope * 1024, tm_r2),
        "verdict_rss": bc.leak_verdict(rss_slope * 1024, rss_r2),
        "messagecard_first": card_counts[0],
        "messagecard_last": card_counts[-1],
        "tracemalloc_diff_top": bc.tracemalloc_diff(snap_before, snap_after, 15),
    }
    tracemalloc.stop()

    print(f"\n===== 对话管线残留基准（{rounds} 轮 × {chunks} 块/轮） =====")
    print(f"tracemalloc 斜率: {tm_slope * 1024:+.2f} KB/轮 (R²={tm_r2:.3f})")
    print(f"RSS 斜率:         {rss_slope * 1024:+.2f} KB/轮 (R²={rss_r2:.3f})")
    print(f"判定(tracemalloc): {result['verdict_tracemalloc']}")
    print(f"判定(RSS):         {result['verdict_rss']}")
    print(f"MessageCard 存活数: {result['messagecard_first']} → {result['messagecard_last']}")
    print("增长 Top10:")
    for d in result["tracemalloc_diff_top"][:10]:
        print(f"  +{d['size_kb']:>8.1f} KB  {d['loc']}  ({d['count']} objs)")

    try:
        backend.cleanup()
    except Exception:
        pass
    bc.save_result("chat_pipeline", result)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--chunks", type=int, default=40)
    ap.add_argument("--no-render", action="store_true", help="对照组：只建 MessageCard 不走 CodeWebViewer 渲染管线")
    args = ap.parse_args()
    run(args.rounds, args.chunks, render=not args.no_render)
