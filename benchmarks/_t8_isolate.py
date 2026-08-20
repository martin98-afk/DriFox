# -*- coding: utf-8 -*-
"""T8：MessageCard 构造链 C++ 侧泄漏二分定位

单变体子进程运行（变体间互不污染），父进程汇总 RSS 斜率对比表。

变体（monkeypatch 注入点）：
  base       完整 MessageCard（对照，≈ +556KB/轮）
  flush      base + deleteLater 后 sendPostedEvents/processEvents 强冲刷（验证延迟释放）
  sipdel     base + sip.delete 强删（验证 deleteLater 语义）
  keepalive  base + 不销毁（验证"构造链固定增长" vs "销毁不归还"）
  no_tip     patch install_hover_tooltip → no-op
  no_style   patch MessageCard._apply_card_style → no-op（qss 不设）
  no_sep     patch CardSeparator → 普通 QWidget（分离分隔条组件）
  no_btn     patch TransparentToolButton → 普通 QToolButton（去 qfluentwidgets 按钮）
  qss_widget 纯 QWidget + 同 qss（最小复现：非 MessageCard）

运行：
  uv run python benchmarks/_t8_isolate.py                 # 全变体顺序跑
  uv run python benchmarks/_t8_isolate.py --variant base  # 单变体
  uv run python benchmarks/_t8_isolate.py --rounds 20
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_common as bc  # noqa: E402


def run_variant(variant: str, rounds: int, chunks: int) -> dict:
    tmp = bc.setup_isolation("t8")

    from PyQt5.QtCore import Qt, QEvent
    from PyQt5.QtWidgets import QApplication, QWidget

    import PyQt5.QtWebEngineWidgets  # noqa: F401

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)

    from app.core.webengine_profile import init_shared_web_profile

    init_shared_web_profile(parent=app)

    # ============ monkeypatch 注入点（按变体） ============
    patches = []
    import app.widgets.message_card as mc

    if variant == "no_tip":
        def _no_tip(widget, delay_ms=400, **kw):
            return None

        from app.widgets import simple_hover_tooltip as sht

        sht.install_hover_tooltip = _no_tip
        # message_card 可能 from-import 绑定
        if hasattr(mc, "install_hover_tooltip"):
            mc.install_hover_tooltip = _no_tip
        patches.append("install_hover_tooltip→noop")

    if variant == "no_style":
        mc.MessageCard._apply_card_style = lambda self, border=None, bg=None: None
        patches.append("_apply_card_style→noop")

    if variant == "no_sep":
        mc.CardSeparator = QWidget
        patches.append("CardSeparator→QWidget")

    if variant == "no_btn":
        from PyQt5.QtWidgets import QToolButton

        def _fake_btn(ic, parent=None):
            b = QToolButton(parent)
            b.setFixedSize(32, 32)
            return b

        mc.TransparentToolButton = _fake_btn
        patches.append("TransparentToolButton→QToolButton(适配签名)")

    # ============ 通用：qfluentwidgets 样式桩（除 no_style 外全保留原样） ============

    msg_text = "T8 二分实验消息，包含**markdown**与`code`。\n\n- a\n- b\n\n"

    # 预热 2 张（懒加载缓存）
    from app.widgets.message_card import MessageCard

    warm = []
    for _ in range(2):
        c = MessageCard(role="assistant")
        c.update_content(msg_text)
        c.set_content(msg_text * 2)
        warm.append(c)
    bc.spin_qt_events(app, 400)
    for c in warm:
        c.deleteLater()
    warm = None
    bc.spin_qt_events(app, 300)
    bc.full_gc()

    import tracemalloc

    tracemalloc.start(1)

    # ====== 真 exec_ 事件循环验证变体：QTimer 驱动，DeferredDelete 走真实路径 ======
    if variant == "exec_loop":
        state = {"i": 0, "xs": [], "tm": [], "rss": []}

        def _step():
            i = state["i"]
            card = MessageCard(role="assistant")
            card.update_content(msg_text)
            card.set_content(msg_text * 2)
            card.deleteLater()
            card = None
            state["i"] = i + 1
            if (i + 1) % 5 == 0 or i == 0:
                bc.full_gc()
                state["xs"].append(i + 1)
                state["tm"].append(bc.tracemalloc_current_mb())
                state["rss"].append(bc.rss_mb())
            if state["i"] < rounds:
                from PyQt5.QtCore import QTimer

                QTimer.singleShot(10, _step)
            else:
                app.quit()

        from PyQt5.QtCore import QTimer

        QTimer.singleShot(10, _step)
        app.exec_()
        xs, tm_mb, rss_mb = state["xs"], state["tm"], state["rss"]
        tm_slope, tm_r2 = bc.slope(xs, tm_mb)
        rss_slope, rss_r2 = bc.slope(xs, rss_mb)
        tracemalloc.stop()
        result = {
            "variant": variant,
            "patches": ["真 exec_ 事件循环（QTimer 驱动），DeferredDelete 走生产路径"],
            "rounds": rounds,
            "tracemalloc_slope_kb": round(tm_slope * 1024, 2),
            "tracemalloc_r2": round(tm_r2, 3),
            "rss_slope_kb": round(rss_slope * 1024, 2),
            "rss_r2": round(rss_r2, 3),
            "samples": [
                {"round": x, "tm": round(t, 3), "rss": round(r, 1)}
                for x, t, r in zip(xs, tm_mb, rss_mb)
            ],
        }
        print("[T8_RESULT]" + json.dumps(result, ensure_ascii=False))
        return result

    keep = []
    xs, tm_mb, rss_mb = [], [], []
    for i in range(rounds):
        if variant == "qss_widget":
            card = QWidget()
            card.setStyleSheet(
                "QWidget { background: rgba(30,32,38,0.92); border-radius: 8px; font-size: 13px; }"
            )
        else:
            card = MessageCard(role="assistant")
            card.update_content(msg_text)
            card.set_content(msg_text * 2)

        # 释放策略
        if variant == "keepalive":
            keep.append(card)
            card = None
        elif variant == "sipdel":
            import sip

            sip.delete(card)
        else:
            card.deleteLater()
            if variant == "flush":
                from PyQt5.QtCore import QCoreApplication

                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                app.processEvents()
            card = None

        if (i + 1) % 5 == 0 or i == 0:
            bc.spin_qt_events(app, 120)
            bc.full_gc()
            xs.append(i + 1)
            tm_mb.append(bc.tracemalloc_current_mb())
            rss_mb.append(bc.rss_mb())

    tm_slope, tm_r2 = bc.slope(xs, tm_mb)
    rss_slope, rss_r2 = bc.slope(xs, rss_mb)
    tracemalloc.stop()

    result = {
        "variant": variant,
        "patches": patches,
        "rounds": rounds,
        "tracemalloc_slope_kb": round(tm_slope * 1024, 2),
        "tracemalloc_r2": round(tm_r2, 3),
        "rss_slope_kb": round(rss_slope * 1024, 2),
        "rss_r2": round(rss_r2, 3),
        "samples": [
            {"round": x, "tm": round(t, 3), "rss": round(r, 1)}
            for x, t, r in zip(xs, tm_mb, rss_mb)
        ],
    }
    print("[T8_RESULT]" + json.dumps(result, ensure_ascii=False))
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default=None)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--chunks", type=int, default=1)
    ap.add_argument("--outer", action="store_true", help="父进程模式：顺序调子进程")
    args = ap.parse_args()

    if args.variant:
        run_variant(args.variant, args.rounds, args.chunks)
        return

    variants = [
        "base", "flush", "sipdel", "keepalive", "exec_loop",
        "no_tip", "no_style", "no_sep", "no_btn", "qss_widget",
    ]
    results = []
    for v in variants:
        print(f"\n===== T8 variant {v} =====", flush=True)
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--variant", v,
             "--rounds", str(args.rounds), "--chunks", str(args.chunks)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(bc.PROJECT_ROOT),
        )
        got = None
        for line in proc.stdout.splitlines():
            if line.startswith("[T8_RESULT]"):
                got = json.loads(line[len("[T8_RESULT]"):])
                break
        if not got:
            print(f"!! {v} 失败 exit={proc.returncode}")
            print("\n".join(proc.stderr.splitlines()[-15:]))
            continue
        results.append(got)
        print(f"  RSS {got['rss_slope_kb']:+.1f} KB/轮 (R²={got['rss_r2']}) | "
              f"tm {got['tracemalloc_slope_kb']:+.2f} KB/轮")

    print("\n===== T8 二分汇总 =====")
    print(f"{'variant':<12}{'RSS KB/轮':>12}{'R²':>8}{'tm KB/轮':>10}{'patches':>40}")
    for r in results:
        print(f"{r['variant']:<12}{r['rss_slope_kb']:>+12.1f}{r['rss_r2']:>8.3f}"
              f"{r['tracemalloc_slope_kb']:>+10.2f}   {','.join(r['patches'])}")
    bc.RESULTS_DIR.mkdir(exist_ok=True)
    (bc.RESULTS_DIR / "t8_isolate.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[bench] 写入 {bc.RESULTS_DIR / 't8_isolate.json'}")


if __name__ == "__main__":
    main()
