# -*- coding: utf-8 -*-
"""基准 6：标签内存增长压测（长时间多标签 + 删除标签，验证删除后内存回落）

驱动真实 GUI 的标签开/关链路（TabManagerWindow.add_window / _close_window_at），
在【单进程内】反复执行「开 N 标签 → 采样 RSS/堆 → 删除全部/部分 → GC → 再采样」，
用跨轮 RSS 斜率判定「删除后内存是否回落」，定位反复开关 tab 的残留泄漏。

为何用真实平台而非 offscreen
---------------------------
bench_session_leak.py 实测：offscreen 下 design_tokens._apply_tooltip_style 会 0xC0000005
访问冲突；真实平台 QApplication + 手动 pump 事件（不 app.exec_ 阻塞）即可 headless 跑通，
且不触发该崩溃。故本脚本【不设置】 QT_QPA_PLATFORM=offscreen。

运行
----
  uv run python benchmarks/bench_tab_mem_leak.py                 # 默认 rounds=4, tabs=6, keep=0
  uv run python benchmarks/bench_tab_mem_leak.py --rounds 8 --tabs 10 --keep 2
  uv run python benchmarks/bench_tab_mem_leak.py --objects      # 额外 pympler 对象快照(start/end)

输出
----
JSON(benchmarks/results/tab_mem_leak.json)：每轮 base/open/close RSS、delta、
跨轮 RSS 斜率(KB/轮)与 LEAK 判定；--objects 时含 start/end 对象类型统计。
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import time
import tracemalloc
import warnings

warnings.filterwarnings("ignore")
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ.pop("MEM_DIAG", None)
os.environ.pop("MEM_TRACE", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_common as bc  # noqa: E402

from PyQt5.QtCore import QCoreApplication, QEvent, Qt, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineSettings, QWebEngineView  # noqa: F401

import app.plugins.registries.coding_plan_fetcher as cpf  # noqa: E402

# 用量请求计数探针：替换为无网络回调，仅计数（避免真实 fetch）
_FETCH_CALLS: list[dict] = []


def _counting_fetch(provider_name, config, callback):
    _FETCH_CALLS.append({"provider": provider_name, "t": time.monotonic()})
    callback(None)


cpf.fetch_async = _counting_fetch  # 运行时替换


def _flush_deferred_deletes(app):
    """强制消费 DeferredDelete 队列（口径修正）。

    deleteLater 只能被「与调用时同层」的事件循环消费；本脚本手动泵
    （processEvents / spin_qt_events）不处理 DeferredDelete，若不冲刷，
    关闭的窗口对象树（含其上 ~95 个 tooltip filter 与 WebEngine page）
    会一直存活到进程结束 → RSS 殘留虚高（实测 4 tab 多算 ~6MB，且
    tracemalloc 的 tooltip 增长全部是此假象）。
    真实运行时 app.exec_() 每帧自动处理，此函数使 bench 口径对齐生产语义。"""
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.tab_manager_window import TabManagerWindow  # noqa: E402

# 禁用 models.dev 后台拉取：避免后台线程在窗口销毁后回调 _models_dev_ready 到已删除
# C++ 对象，触发原生崩溃（STATUS_STACK_BUFFER_OVERRUN）。perf_regression 同思路禁用网络副作用。
OpenAIChatToolWindow._start_models_dev_sync = lambda self: None
# 禁用所有延迟初始化（_safe_timer_call 调度 _sync_working_directory / _load_model_configs /
# _async_refresh_opencode_models 等后台回调）：避免窗口快速销毁后这些回调/线程进已删 widget
# 触发原生崩溃。内存压测只关心"标签开/关生命周期"，与这些延迟特性无关。
OpenAIChatToolWindow._safe_timer_call = lambda self, *a, **k: None


class FakePage(QWidget):
    """与 main.py / TabManagerWindow._create_fake_page 等价的假页面。"""

    def __init__(self):
        super().__init__()
        self.cfg = None

    def isActiveWindow(self):
        return True

    @property
    def workflow_name(self):
        return "tab_manager"

    @property
    def global_variables_changed(self):
        class FakeSignal:
            def connect(self, *a, **k):
                pass

        return FakeSignal()

    def setUpdatesEnabled(self, enabled):
        pass

    def update(self):
        pass

    def show_splitter(self):
        pass

    def hide_splitter(self):
        pass


def _rss_mb() -> float:
    return bc.rss_mb()


def run(rounds: int, tabs_per_round: int, keep: int, track_objects: bool, objects_top: int,
        leak_threshold_mb: float = 5.0) -> dict:
    bc.setup_isolation("tabmemleak")
    tmp = os.getcwd()

    app = QApplication(sys.argv)
    from app.utils import icons_light_rc, icons_rc  # noqa: F401

    app.setStyle("Fusion")
    app.setApplicationName("Drifox")
    from app.core.webengine_profile import init_shared_web_profile

    init_shared_web_profile(parent=app)
    # 规避 HeapCompact access violation（T1 实测在测试环境偶发崩溃）
    import app.main_widget as mw

    mw._compact_process_heap_after_cleanup = lambda: None

    fake_page = FakePage()
    first_window = OpenAIChatToolWindow(fake_page)
    tm = TabManagerWindow.create_instance()
    tm.add_window(first_window)
    tm.show()
    # 等延迟组件（QTimer 0/200/400/600ms 错峰创建）完成
    bc.spin_qt_events(app, 2500)

    obj_start = None
    if track_objects:
        from pympler import muppy, summary

        rows = summary.summarize(muppy.get_objects())
        obj_start = [
            {"type": n, "count": c, "size_bytes": s}
            for n, c, s in sorted(rows, key=lambda r: r[2], reverse=True)[:objects_top]
        ]

    # ── 预热一轮：开 N + 全删 + GC，吸收一次性懒加载（decoder/mmseg/pinyin_dict 等），
    #    排除冷启动噪声，使测量轮基线干净 ──
    for _ in range(tabs_per_round):
        tm._on_new_tab_requested()
        app.processEvents()
    bc.spin_qt_events(app, 600)
    while tm.window_count > 0:
        tm._close_window_at(tm.window_count - 1)
        app.processEvents()
    bc.spin_qt_events(app, 400)
    _flush_deferred_deletes(app)
    for _ in range(3):
        bc.full_gc()
        app.processEvents()
        time.sleep(0.1)

    tracemalloc.start(10)
    snap0 = tracemalloc.take_snapshot()

    xs, base_mb, open_mb, close_mb = [], [], [], []
    samples = []
    first_close_base = None  # 首轮（预热后）关后 RSS，作"删除后回落"基线
    threshold = leak_threshold_mb
    for r in range(1, rounds + 1):
        bc.full_gc()
        base = _rss_mb()

        # ── 开 N 标签 ──
        t0 = time.perf_counter()
        for _ in range(tabs_per_round):
            tm._on_new_tab_requested()
            app.processEvents()
        bc.spin_qt_events(app, 600)  # 让新窗口 showEvent/布局/懒加载完成
        open_s = time.perf_counter() - t0
        bc.full_gc()
        after_open = _rss_mb()

        # ── 删除（保留 keep 个，默认全删）──
        t0 = time.perf_counter()
        target = max(keep, 0)
        while tm.window_count > target:
            tm._close_window_at(tm.window_count - 1)
            app.processEvents()
        bc.spin_qt_events(app, 400)
        for _ in range(3):
            bc.full_gc()
            app.processEvents()
            time.sleep(0.1)
        # 口径修正：先冲刷 DeferredDelete 再采样（否则窗口树滞留虚高）
        _flush_deferred_deletes(app)
        for _ in range(3):
            bc.full_gc()
            app.processEvents()
            time.sleep(0.1)
        close_s = time.perf_counter() - t0
        after_close = _rss_mb()

        xs.append(r)
        base_mb.append(base)
        open_mb.append(after_open)
        close_mb.append(after_close)
        samples.append(
            {
                "round": r,
                "open_s": round(open_s, 4),
                "close_s": round(close_s, 4),
                "rss_base_mb": round(base, 2),
                "rss_after_open_mb": round(after_open, 2),
                "rss_after_close_mb": round(after_close, 2),
                "delta_open_mb": round(after_open - base, 2),
                "delta_close_mb": round(after_close - base, 2),
            }
        )
        if first_close_base is None:
            first_close_base = after_close  # 首轮关后 RSS 作"删除后回落"基线
        print(
            f"轮{r}/{rounds}: 基线 {base:.1f} → 开{tabs_per_round}tab {after_open:.1f}MB"
            f" → 删后 {after_close:.1f}MB (delta_close {after_close - base:+.1f}MB)",
            flush=True,
        )

    snap1 = tracemalloc.take_snapshot()
    # 斜率以"删除后 RSS"对轮次回归；首轮含冷启动懒加载，从第 2 轮起判定更准
    meas_xs = xs[1:] if len(xs) > 1 else xs
    meas_close = close_mb[1:] if len(close_mb) > 1 else close_mb
    rss_slope, rss_r2 = bc.slope(meas_xs, meas_close)
    if first_close_base is None:
        first_close_base = close_mb[0] if close_mb else 0.0
    drift = close_mb[-1] - first_close_base
    verdict = "LEAK" if drift > threshold else "OK"

    obj_end = None
    if track_objects:
        from pympler import muppy, summary

        rows = summary.summarize(muppy.get_objects())
        obj_end = [
            {"type": n, "count": c, "size_bytes": s}
            for n, c, s in sorted(rows, key=lambda r: r[2], reverse=True)[:objects_top]
        ]

    result = {
        "metric": "tab_mem_leak",
        "rounds": rounds,
        "tabs_per_round": tabs_per_round,
        "keep_after_close": keep,
        "samples": samples,
        "rss_slope_kb_per_round": round(rss_slope * 1024, 2),
        "rss_r2": round(rss_r2, 3),
        "tracemalloc_diff_top": bc.tracemalloc_diff(snap0, snap1, 10),
        "verdict": verdict,
        "close_drift_mb": round(drift, 2),
        "leak_threshold_mb": threshold,
        "note": "drift=末轮关后RSS-首轮(预热后)关后RSS；超过阈值视为删除后未回落(LEAK)",
        "caveat": "RSS 含分配器滞留±10~15MB 噪声；精确泄露以 tracemalloc_diff_top 为准",
    }
    if track_objects:
        result["object_snapshots"] = {"start": obj_start, "end": obj_end}
    tracemalloc.stop()

    print(f"\n===== 标签内存增长压测（{rounds}轮×{tabs_per_round}标签，保留{keep}）=====")
    print(f"删除后 RSS 跨轮漂移: {drift:+.2f} MB (斜率 {rss_slope * 1024:+.2f} KB/轮, R²={rss_r2:.3f})")
    print(f"判定: {verdict}")
    print("增长 Top10:")
    for d in result["tracemalloc_diff_top"][:10]:
        print(f"  +{d['size_kb']:>8.1f} KB  {d['loc']}  ({d['count']} objs)")

    # 先落盘结果：末尾 teardown（关闭多 WebEngine 视图 + app.quit）在原生的 0xC0000409 止血后
    # 仍可能崩溃（不同退出码），测量已完成，先保存 JSON 避免丢失。
    bc.save_result("tab_mem_leak", result)
    try:
        first_window.close()
        tm.close()
    except Exception:
        pass
    app.processEvents()
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="标签内存增长压测：开N标签→删→GC→采样，验证删除后回落")
    ap.add_argument("--rounds", type=int, default=4, help="循环轮数（默认4）")
    ap.add_argument("--tabs", type=int, default=6, help="每轮打开标签数（默认6）")
    ap.add_argument("--keep", type=int, default=0, help="删除后保留标签数（默认0=全删）")
    ap.add_argument("--objects", action="store_true", help="额外 pympler 对象快照(start/end)")
    ap.add_argument("--objects-top", type=int, default=20, help="对象快照 top N（默认20）")
    ap.add_argument("--leak-threshold", type=float, default=5.0,
                    help="删除后 RSS 跨轮漂移阈值 MB（默认5，待基线校准后调整）")
    args = ap.parse_args()
    run(args.rounds, args.tabs, args.keep, args.objects, args.objects_top, args.leak_threshold)
