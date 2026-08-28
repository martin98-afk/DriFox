# -*- coding: utf-8 -*-
"""
splitter 拖拽 & tab_panel 折叠/展开 — baseline 性能基准测试 (perf-tester #2)

运行方式
--------
无显示器（CI/沙箱，默认）：
    QT_QPA_PLATFORM=offscreen python tools/perf/splitter_tabpanel_baseline.py
带显示器（真实 FPS）：
    python tools/perf/splitter_tabpanel_baseline.py --mode live

测量方法
--------
1. 构造真实 TabPanel + QSplitter（带与 TabManagerWindow 一致的
   setMinimumWidth(_collapsed_min_width) 约束、stretch(0,1)、handleWidth 4）。
2. 复刻 TabManagerWindow 的宽度动画逻辑（_start_sidebar_anim /
   _on_sidebar_anim_value / sync_collapsed_ui），但**手动步进**动画，
   每帧显式 repaint() 强制绘制——offscreen 不会自动 flush 延迟重绘，
   必须显式 repaint 才能测到真实 paint 主线程成本（"卡卡"主因）。
3. 用 QApplication.notify 插桩，记录每个事件的派发耗时(ms) → 主线程阻塞时长；
   统计 paint 事件数、>16.7ms(>60FPS 预算) 卡顿帧占比、最大单事件耗时。
4. splitter 拖拽：以 2px/步 在 展开↔折叠 间往返，每步 setSizes + repaint，
   捕获每步 paint/resize 事件耗时。

注意（offscreen 模式）
---------------------
offscreen 不做真实栅格化/GPU 合成，paintEvent 的 Qt 绘制逻辑（渐变/文字/项绘制）
仍执行，是 CPU 侧主线程成本的下界；真实 FPS 需 --mode live 在带显示器机器跑。
本脚本仅量化 tab_panel 侧成本；真实 app 中 content 区含 WebEngineView，
其 resize/repaint 成本需在 live 模式或真实环境复测。
"""
import os
import sys
import time
import json
import argparse
from collections import defaultdict

# ---- 早期环境：默认 offscreen，避免无显示器挂起 ----
if os.environ.get("QT_QPA_PLATFORM") is None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import (QApplication, QWidget, QSplitter, QVBoxLayout)
from PySide6.QtCore import Qt, QTimer, QElapsedTimer, QEvent

import app.utils.icons_rc  # noqa: F401
import app.utils.icons_light_rc  # noqa: F401

from app.widgets.tab_panel import TabPanel

# 与 tab_manager_window.py 对齐的常量
_DEFAULT_PANEL_WIDTH = 187
_EXPANDED_MIN_FRAME_WIDTH = 200
COLLAPSE_BUDGET_MS = 16.7  # 60FPS 单帧预算

# PySide6 下 QEvent.type() 返回 int，无 .name；手动映射关键事件类型名
_QEVENT_NAMES = {
    1: "Timer", 12: "Paint", 13: "Move", 14: "Resize", 17: "Show",
    26: "WindowStateChange", 43: "MetaCall", 67: "PolishRequest",
    68: "Polish", 69: "ChildPolished", 75: "UpdateLater", 76: "LayoutRequest",
    77: "UpdateRequest", 78: "StyleAnimationUpdate", 98: "DynamicPropertyChange",
    110: "Enter", 111: "Leave", 126: "StyleAnimationUpdate",
}


# ───────────────────────── 事件耗时记录器 ─────────────────────────
class EventRecorder:
    """记录测量窗口内每个事件的 (type, dt_ms)。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.events = []          # (type_name, dt_ms)
        self.by_type = defaultdict(list)

    def start(self):
        self.reset()
        self.active = True

    def stop(self):
        self.active = False

    def record(self, ev_type, dt_ms):
        if not self.active:
            return
        name = _QEVENT_NAMES.get(ev_type, str(ev_type))
        self.events.append((name, dt_ms))
        self.by_type[name].append(dt_ms)

    def summary(self, total_wall_ms):
        n = len(self.events)
        if n == 0:
            return self._empty(total_wall_ms)
        dts = [e[1] for e in self.events]
        dts_sorted = sorted(dts)
        p95 = dts_sorted[min(len(dts_sorted) - 1, int(len(dts_sorted) * 0.95))]
        paint = self.by_type.get("Paint", [])
        resize = self.by_type.get("Resize", [])
        over16 = sum(1 for d in dts if d > COLLAPSE_BUDGET_MS)
        over33 = sum(1 for d in dts if d > 33.3)
        paint_over16 = sum(1 for d in paint if d > COLLAPSE_BUDGET_MS)
        wall_s = total_wall_ms / 1000.0
        fps = (len(paint) / wall_s) if wall_s > 0 else 0.0
        return {
            "event_count": n,
            "wall_ms": round(total_wall_ms, 2),
            "mean_ms": round(sum(dts) / n, 4),
            "max_ms": round(max(dts), 4),
            "p95_ms": round(p95, 4),
            "frame_count": len(paint),
            "paint_mean_ms": round(sum(paint) / len(paint), 4) if paint else 0.0,
            "paint_max_ms": round(max(paint), 4) if paint else 0.0,
            "paint_over16_count": paint_over16,
            "jank_ratio": round(paint_over16 / len(paint), 4) if paint else 0.0,
            "resize_count": len(resize),
            "resize_mean_ms": round(sum(resize) / len(resize), 4) if resize else 0.0,
            "over16_count": over16,
            "over33_count": over33,
            "fps_proxy": round(fps, 2),
        }

    @staticmethod
    def _empty(wall_ms):
        return {
            "event_count": 0, "wall_ms": round(wall_ms, 2), "mean_ms": 0.0,
            "max_ms": 0.0, "p95_ms": 0.0, "frame_count": 0, "paint_mean_ms": 0.0,
            "paint_max_ms": 0.0, "paint_over16_count": 0, "jank_ratio": 0.0,
            "resize_count": 0, "resize_mean_ms": 0.0, "over16_count": 0,
            "over33_count": 0, "fps_proxy": 0.0,
        }


class PerfApp(QApplication):
    def __init__(self, argv, recorder):
        super().__init__(argv)
        self._rec = recorder

    def notify(self, receiver, event):
        if self._rec.active:
            t0 = time.perf_counter()
            try:
                return super().notify(receiver, event)
            finally:
                dt = (time.perf_counter() - t0) * 1000.0
                self._rec.record(int(event.type()), dt)
        return super().notify(receiver, event)


# ───────────────────────── 基准 harness ─────────────────────────
class BaselineHarness:
    EXPANDED_W = _DEFAULT_PANEL_WIDTH + 14      # 201
    COLLAPSED_W = 46 + 14                        # 60
    TOTAL_W = 1200
    HEIGHT = 700

    def __init__(self, app, recorder):
        self.app = app
        self.rec = recorder
        self.panel = TabPanel()
        # 与 TabManagerWindow._setup_ui 对齐的最小宽度约束
        self.panel.setMinimumWidth(self.panel._collapsed_min_width)
        self.content = QWidget()
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(4)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.panel)
        self.splitter.addWidget(self.content)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.host = QWidget()
        self.host.setWindowTitle("perf-harness")
        lay = QVBoxLayout(self.host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.splitter)
        self.host.resize(self.TOTAL_W, self.HEIGHT)
        self.host.show()
        self.app.processEvents()
        self.splitter.setSizes([self.EXPANDED_W, self.TOTAL_W - self.EXPANDED_W])
        self.app.processEvents()
        self._anim_collapsing = False
        self._anim_ui_switched = False

    # ---- 宽度控制（offscreen 下 setSizes 可能不立即生效，需兜底 resize）----
    def _ensure_width(self, w):
        self.splitter.setSizes([w, self.TOTAL_W - w])
        if self.panel.width() != w:
            self.panel.resize(w, self.panel.height())

    def _apply_frame(self, w):
        """模拟真实一帧：改变宽度 → flush resize/layout → 强制 repaint。
        offscreen 不会自动 flush 延迟重绘，显式 repaint 才能测到真实 paint 成本。"""
        self._ensure_width(w)
        self.app.processEvents()            # flush resizeEvent + layout
        self.panel.repaint()                # 强制同步绘制当前帧
        self.content.repaint()
        self.app.processEvents()            # flush 绘制产生的事件

    def _sync_ui_if_threshold(self, w):
        if self._anim_ui_switched:
            return
        if self._anim_collapsing and w <= 100:
            self._anim_ui_switched = True
            self.panel.sync_collapsed_ui()
        elif (not self._anim_collapsing) and w >= 120:
            self._anim_ui_switched = True
            self.panel.sync_collapsed_ui()

    # ---- 操作：折叠 / 展开（手动步进动画）----
    def do_toggle(self, collapse: bool, frames: int = 48):
        start = self.panel.width()
        end = self.COLLAPSED_W if collapse else self.EXPANDED_W
        self.panel._animating = True
        self._anim_collapsing = collapse
        self._anim_ui_switched = False
        for i in range(1, frames + 1):
            w = int(round(start + (end - start) * i / frames))
            self._sync_ui_if_threshold(w)
            self._apply_frame(w)
        self._ensure_width(end)
        self.panel.sync_collapsed_ui()
        self.panel._animating = False
        self.app.processEvents()

    # ---- 操作：splitter 拖拽模拟（展开↔折叠 往返）----
    def do_drag(self, step: int = 2):
        seq = list(range(self.EXPANDED_W, self.COLLAPSED_W - 1, -step))
        seq += list(range(self.COLLAPSED_W, self.EXPANDED_W + 1, step))
        for w in seq:
            self._apply_frame(w)
            # 吸收 resizeEvent 中可能排队的 singleShot(0) 信号
            QTimer.singleShot(0, lambda: None)
            self.app.processEvents()

    # ---- 填充 tab ----
    def populate(self, n):
        self.panel.begin_batch_add()
        for i in range(n):
            self.panel.add_tab(f"会话 {i:03d}", project_initials=f"P{i % 9}",
                               project_color="rgba(33,139,255,255)")
        self.panel.end_batch_add()
        if n > 0:
            self.panel.set_active_index(0)
        self.app.processEvents()

    def sync_cost(self):
        t0 = time.perf_counter()
        self.panel.sync_collapsed_ui()
        self.app.processEvents()
        return (time.perf_counter() - t0) * 1000.0


# ───────────────────────── 主流程 ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["offscreen", "live"], default="offscreen")
    ap.add_argument("--tabs", type=int, default=30, help="填充 tab 数量")
    ap.add_argument("--runs", type=int, default=5, help="每类操作重复次数")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "baseline_result.json"))
    args = ap.parse_args()

    if args.mode == "live":
        os.environ["QT_QPA_PLATFORM"] = "windows"

    rec = EventRecorder()
    app = PerfApp(sys.argv, rec)
    app.setApplicationName("DriFox-perf")

    harness = BaselineHarness(app, rec)

    # TabPanel 一次性构造成本（历史参考，非每帧）
    t0 = time.perf_counter()
    _probe_panel = TabPanel()
    del _probe_panel
    panel_construct_ms = (time.perf_counter() - t0) * 1000.0

    report = {
        "meta": {
            "mode": args.mode,
            "platform": app.platformName(),
            "python": sys.version.split()[0],
            "qt": __import__("PySide6.QtCore", fromlist=["QT_VERSION_STR"]).QT_VERSION_STR,
            "tabs": args.tabs,
            "runs": args.runs,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "constants": {
                "EXPANDED_W": BaselineHarness.EXPANDED_W,
                "COLLAPSED_W": BaselineHarness.COLLAPSED_W,
                "TOTAL_W": BaselineHarness.TOTAL_W,
                "auto_collapse_width": 100,
                "frame_budget_ms": COLLAPSE_BUDGET_MS,
            },
            "note": "offscreen: CPU 侧 paint/resize 主线程成本下界，不含真实栅格化/GPU；live 模式需显示器。",
        },
        "results": {},
    }
    report["meta"]["panel_construct_ms"] = round(panel_construct_ms, 2)

    harness.populate(args.tabs)
    app.processEvents()

    # 单次折叠/展开 baseline
    collapse_runs, expand_runs = [], []
    for _ in range(args.runs):
        harness._ensure_width(BaselineHarness.EXPANDED_W)
        harness.panel._collapsed = False
        harness.panel._animating = False
        app.processEvents()
        rec.start()
        et = QElapsedTimer(); et.start()
        harness.do_toggle(collapse=True)
        wall = et.elapsed()
        rec.stop()
        collapse_runs.append(rec.summary(wall))

        harness._ensure_width(BaselineHarness.COLLAPSED_W)
        harness.panel._collapsed = True
        app.processEvents()
        rec.start()
        et.start()
        harness.do_toggle(collapse=False)
        wall = et.elapsed()
        rec.stop()
        expand_runs.append(rec.summary(wall))

    def agg(runs):
        keys = ["wall_ms", "event_count", "mean_ms", "max_ms", "p95_ms",
                "frame_count", "paint_mean_ms", "paint_max_ms", "paint_over16_count",
                "jank_ratio", "resize_count", "resize_mean_ms", "over16_count",
                "over33_count", "fps_proxy"]
        out = {}
        for k in keys:
            vals = [r[k] for r in runs]
            out[k] = round(sum(vals) / len(vals), 4)
        return out

    report["results"]["collapse"] = agg(collapse_runs)
    report["results"]["expand"] = agg(expand_runs)

    # splitter 拖拽 baseline
    drag_runs = []
    for _ in range(args.runs):
        harness._ensure_width(BaselineHarness.EXPANDED_W)
        harness.panel._collapsed = False
        harness.panel._animating = False
        app.processEvents()
        rec.start()
        et = QElapsedTimer(); et.start()
        harness.do_drag(step=2)
        wall = et.elapsed()
        rec.stop()
        drag_runs.append(rec.summary(wall))
    report["results"]["splitter_drag"] = agg(drag_runs)

    # sync_collapsed_ui 单次成本（跨阈值时调用一次）
    harness._ensure_width(BaselineHarness.EXPANDED_W)
    harness.panel._collapsed = False
    app.processEvents()
    syncs = [harness.sync_cost() for _ in range(20)]
    report["results"]["sync_collapsed_ui_ms"] = round(sum(syncs) / len(syncs), 4)

    # 不同 tab 数量下 单次 repaint 最大耗时（展开宽度）
    paint_scaling = {}
    for n in [1, 10, 30, 50]:
        harness.populate(n)
        app.processEvents()
        harness._ensure_width(BaselineHarness.EXPANDED_W)
        app.processEvents()
        samples = []
        for _ in range(10):
            rec.start()
            et = QElapsedTimer(); et.start()
            harness.panel.repaint()
            app.processEvents()
            rec.stop()
            samples.append(rec.summary(et.elapsed())["paint_max_ms"])
        paint_scaling[str(n)] = round(sum(samples) / len(samples), 4)
    report["results"]["paint_max_ms_by_tabs"] = paint_scaling

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 64)
    print("splitter/tab_panel baseline  (mode=%s, platform=%s)" % (args.mode, app.platformName()))
    print("tabs=%d runs=%d  out=%s" % (args.tabs, args.runs, args.out))
    print("-" * 64)
    print("collapse (expanded->collapsed):")
    for k, v in report["results"]["collapse"].items():
        print("  %-16s %s" % (k, v))
    print("expand (collapsed->expanded):")
    for k, v in report["results"]["expand"].items():
        print("  %-16s %s" % (k, v))
    print("splitter_drag (expanded<->collapsed, 2px/step):")
    for k, v in report["results"]["splitter_drag"].items():
        print("  %-16s %s" % (k, v))
    print("sync_collapsed_ui 单次: %.4f ms" % report["results"]["sync_collapsed_ui_ms"])
    print("paint_max_ms_by_tabs:", paint_scaling)
    print("=" * 64)


if __name__ == "__main__":
    main()
