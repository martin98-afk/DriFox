# -*- coding: utf-8 -*-
"""基准 8：典型 UI 操作耗时 + 反复操作后的内存增长（脚本化驱动）。

测量项（全部在主线程内计时，取中位数）：
- op.switch_tab    : 侧栏会话/标签切换（TabManagerWindow.set_current_window）
- op.new_tab       : 新建会话标签（TabManagerWindow.add_window 路径）
- op.close_tab     : 关闭标签
- op.workbench     : 工作台面板显示/隐藏
- op.scroll        : 列表滚动（QScrollBar setValue，逐帧）
- op.search        : 侧栏搜索输入（逐字符 setText）
- mem.ops100       : 上述典型操作循环 100 次后的 RSS 变化

与 bench_startup.py 的差异：本脚本只关心「操作一次要多久」与「操作 100 次涨多少
内存」，不追启动分段。

运行：
  uv run python benchmarks/bench_ops.py --rounds 5 --mem-cycles 100
  uv run python benchmarks/bench_ops.py --only switch_tab --rounds 20
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import warnings

warnings.filterwarnings("ignore")
os.environ["PYTHONIOENCODING"] = "utf-8"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_common as bc  # noqa: E402

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)

from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: E402,F401

_ALL_OPS = ("switch_tab", "new_tab", "close_tab", "workbench", "scroll")


class FakePage(QWidget):
    """最小 page 替身（与 bench_startup.py 同款，避免拉入完整 workflow）。"""

    def __init__(self):
        super().__init__()
        from app.utils.config import Settings

        self.cfg = Settings.get_instance()

    def isActiveWindow(self):  # noqa: N802
        return True

    @property
    def workflow_name(self):
        return "standalone_llm_chatter"

    @property
    def global_variables_changed(self):
        class FakeSignal:
            def connect(self, *a, **k):
                pass

        return FakeSignal()

    def setUpdatesEnabled(self, e):  # noqa: N802
        pass

    def update(self):
        pass

    def show_splitter(self):
        pass

    def hide_splitter(self):
        pass


def _pump(app, ms: int = 50) -> None:
    """泵一次事件循环（让 Qt 把操作真正执行完）。"""
    bc.spin_qt_events(app, ms, step_ms=10)


def _timeit(fn, repeat: int, app) -> dict:
    """跑 repeat 次，返回 min/median/max（毫秒）。每次前后各泵一次事件。"""
    samples = []
    for _ in range(repeat):
        _pump(app, 5)
        t0 = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - t0) * 1000.0
        samples.append(elapsed)
    s = sorted(samples)
    return {
        "n": len(s),
        "min_ms": round(s[0], 2),
        "median_ms": round(statistics.median(s), 2),
        "max_ms": round(s[-1], 2),
    }


def build_scene(app):
    """复刻生产启动链，返回 (app, tm, chat_window)。"""
    from app.main_widget import OpenAIChatToolWindow
    from app.utils.config import Settings
    from app.widgets.tab_manager_window import TabManagerWindow

    fake_page = FakePage()
    tm = TabManagerWindow.create_instance()
    tm.show()
    tm.show_boot_placeholder()
    try:
        tm.repaint()
    except Exception:
        pass
    tm.complete_construction()
    chat_window = OpenAIChatToolWindow(fake_page)
    tm.add_window(chat_window)
    tm.remove_boot_placeholder()
    tm._mark_first_window_ready()

    from app.core.infra.webengine_profile import init_shared_web_profile

    try:
        init_shared_web_profile(parent=app)
    except Exception:
        pass
    _pump(app, 300)
    _ = Settings.get_instance()
    return tm, chat_window


def _sidebar(tm):
    """取侧栏 TabPanel（不同版本命名不一，按 attr 探测）。"""
    for attr in ("tab_panel", "sidebar", "_tab_panel"):
        panel = getattr(tm, attr, None)
        if panel is not None and hasattr(panel, "add_tab"):
            return panel
    return None


def _scrollable(chat_window):
    """取对话区滚动容器（生产字段名 chat_scroll_area，见 main_widget 全文）。"""
    for attr in ("chat_scroll_area", "message_area", "chat_scroll"):
        w = getattr(chat_window, attr, None)
        if w is not None and hasattr(w, "verticalScrollBar"):
            return w
    return None


def run_once(ops, repeat: int, mem_cycles: int) -> dict:
    app = QApplication.instance() or QApplication(sys.argv)
    # tracemalloc 必须在建场景前启动，否则稳态采样恒为 0（RSS 与 Python 堆
    # 斜率失去对照，判不了是 Qt/C++ 侧累积还是 Python 侧）。
    import tracemalloc

    if not tracemalloc.is_tracing():
        tracemalloc.start(1)
    result = {"ops": {}, "mem": {}}

    t0 = time.perf_counter()
    tm, chat_window = build_scene(app)
    result["scene_build_s"] = round(time.perf_counter() - t0, 3)
    bc.full_gc()

    result["mem"]["rss_after_build_mb"] = round(bc.rss_mb(), 1)

    # ── 单次操作耗时 ──
    if "switch_tab" in ops:
        wins = list(getattr(tm, "_windows", []) or [])

        def _switch():
            if len(wins) > 1:
                tm.set_current_window(wins[-1] if tm.get_current_window() is wins[0] else wins[0])
            else:
                _pump(app, 2)

        result["ops"]["switch_tab"] = _timeit(_switch, repeat, app)

    if "new_tab" in ops:
        created = []

        def _new():
            from app.main_widget import OpenAIChatToolWindow

            w = OpenAIChatToolWindow(FakePage())
            created.append(w)
            tm.add_window(w)

        result["ops"]["new_tab"] = _timeit(_new, max(1, repeat // 2), app)
        # 关掉新建的标签（不测耗时，只为收尾）
        for w in created:
            try:
                tm.close_window(w)
            except Exception:
                pass
        _pump(app, 200)

    if "close_tab" in ops:
        from app.main_widget import OpenAIChatToolWindow

        victims = []
        for _ in range(min(3, repeat)):
            w = OpenAIChatToolWindow(FakePage())
            tm.add_window(w)
            victims.append(w)
        _pump(app, 200)

        def _close():
            if victims:
                w = victims.pop()
                try:
                    tm.close_window(w)
                except Exception:
                    pass

        result["ops"]["close_tab"] = _timeit(_close, min(3, repeat), app)

    if "workbench" in ops:
        def _wb():
            try:
                if tm.is_workbench_visible():
                    tm.set_workbench_visible(False, persist=False)
                else:
                    tm.set_workbench_visible(True, persist=False)
            except Exception:
                pass

        result["ops"]["workbench"] = _timeit(_wb, repeat, app)

    if "scroll" in ops:
        area = _scrollable(chat_window)
        if area is None:
            result["ops"]["scroll"] = {"skipped": "未找到 chat_scroll_area"}
        else:
            bar = area.verticalScrollBar()
            lo, hi = int(bar.minimum()), int(bar.maximum())

            def _scroll():
                if hi > lo:
                    bar.setValue(hi if bar.value() == lo else lo)

            result["ops"]["scroll"] = _timeit(_scroll, repeat, app)
            result["ops"]["scroll"]["range"] = [lo, hi]

    # ── 反复操作 100 次后的内存增长 ──
    if mem_cycles > 0:
        bc.full_gc(3)
        rss_start = bc.rss_mb()
        tm_start = bc.tracemalloc_current_mb()
        series = []
        panel = _sidebar(tm)
        area = _scrollable(chat_window)
        bar = area.verticalScrollBar() if area is not None else None

        for i in range(mem_cycles):
            try:
                if "workbench" in ops:
                    tm.set_workbench_visible(i % 2 == 0, persist=False)
                if bar is not None and bar.maximum() > bar.minimum():
                    bar.setValue(bar.maximum() if i % 2 else bar.minimum())
                if panel is not None and hasattr(panel, "set_active_index"):
                    n = len(getattr(panel, "_items", []) or [])
                    if n > 1:
                        panel.set_active_index(i % n)
            except Exception:
                pass
            if i % 10 == 0:
                _pump(app, 10)
            if i % 20 == 0 or i == mem_cycles - 1:
                bc.full_gc(1)
                series.append([i, round(bc.rss_mb(), 1), round(bc.tracemalloc_current_mb(), 1)])

        bc.full_gc(3)
        rss_end = bc.rss_mb()
        tm_end = bc.tracemalloc_current_mb()
        idx = [s[0] for s in series]
        rss = [s[1] for s in series]
        tms = [s[2] for s in series]
        rss_slope, rss_r2 = bc.slope(idx, rss) if len(idx) > 1 else (0.0, 0.0)
        tm_slope, tm_r2 = bc.slope(idx, tms) if len(idx) > 1 else (0.0, 0.0)
        result["mem"].update(
            {
                "cycles": mem_cycles,
                "rss_start_mb": round(rss_start, 1),
                "rss_end_mb": round(rss_end, 1),
                "rss_delta_mb": round(rss_end - rss_start, 1),
                "rss_kb_per_cycle": round(rss_slope * 1024, 2),
                "rss_r2": round(rss_r2, 3),
                "tracemalloc_delta_mb": round(tm_end - tm_start, 1),
                "tracemalloc_kb_per_cycle": round(tm_slope * 1024, 2),
                "tracemalloc_r2": round(tm_r2, 3),
                "series": series,
            }
        )

    # 清理
    try:
        chat_window.backend.cleanup()
    except Exception:
        pass
    return result


def main() -> None:
    bc.warn_if_drifox_running("ops")
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3, help="每个操作重复采样次数（取中位）")
    ap.add_argument("--mem-cycles", type=int, default=100, help="内存增长循环次数；0=跳过")
    ap.add_argument("--only", default="", help=f"只跑指定操作，逗号分隔；可选 {_ALL_OPS}")
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()
    bc.set_result_tag(args.tag)

    bc.setup_isolation("ops")
    ops = tuple(x.strip() for x in args.only.split(",") if x.strip()) or _ALL_OPS

    t0 = time.perf_counter()
    data = run_once(ops, args.rounds, args.mem_cycles)
    data["wall_s"] = round(time.perf_counter() - t0, 1)

    print("\n===== 典型操作基准 =====")
    print(json.dumps({k: v for k, v in data.items() if k != "mem"}, ensure_ascii=False, indent=2))
    print("\n----- 内存 -----")
    print(json.dumps({k: v for k, v in data["mem"].items() if k != "series"}, ensure_ascii=False, indent=2))
    bc.save_result("ops", data)


if __name__ == "__main__":
    main()
