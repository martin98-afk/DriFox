# -*- coding: utf-8 -*-
"""性能回归对比脚本：一键测量开 tab 耗时 / 内存曲线 / 用量请求计数，输出结构化对比表。

T9-pre 固化：把 T1 基线的真实 GUI 测量方法固化为可复现回归脚本，
供优化落地后一键复测，输出「优化前 / 优化后」对比。

测量语义（与 T1 一致）
--------------------
- 每轮 = 独立子进程 + 干净 .drifox：进程态 / 内存 / 数据目录均为全新，
  避免上一轮泄漏对象污染下一轮基线（T1 的 3 轮独立进程语义）。
- 每轮流程：启动 → 记录基线 RSS → 开 N tab（计时）→ 记录峰值 → 逐个关闭
  → 强制 GC → 记录关闭后 RSS → UI 响应 → 用量请求计数。
- LEAK 判定：关闭后 RSS - 打开峰值 > 阈值（默认 5MB）视为泄漏不回落。

用法
----
# 完整测量（默认：2 轮 × 8 tab，输出 JSON + 控制台）
uv run python scripts/perf_regression.py

# 指定轮数 / tab 数 / 输出路径
uv run python scripts/perf_regression.py --tabs 8 --rounds 3 -o perf_result.json

# 单轮模式（内部使用：只跑一轮，输出该轮 JSON）
uv run python scripts/perf_regression.py --single-shot -o perf_single.json

# 与优化前基线对比（--baseline 指向基线 JSON）
uv run python scripts/perf_regression.py --baseline perf_baseline.json

# 关闭数据目录隔离（不备份/恢复 .drifox，自检演示用，会污染用户数据！）
uv run python scripts/perf_regression.py --no-isolate

输出
----
JSON: {"meta": {...}, "rounds": [每轮数据...], "summary": 聚合指标}
- summary.startup_s:        启动耗时（首轮）
- summary.open_tab:         开 tab 聚合 avg/median/min/max（全部轮次样本）
- summary.memory:           内存聚合（基线/开后/关后/差值）+ LEAK 判定
- summary.usage_fetch:      套餐用量查询 fetch_async 总次数
- summary.ui:               tab 切换 / 事件循环帧率

环境
----
- Python 3.14+ / PyQt5 / psutil（必需）/ pympler（可选，未用）
- 不修改任何被测代码；数据目录 .drifox 自动备份并在结束后恢复

注意
----
- 驱动需先 init_shared_web_profile()（否则懒加载 CodeWebViewer 抛 RuntimeError → qFatal）
- 运行时 patch _compact_process_heap_after_cleanup 规避 HeapCompact access violation
  （--keep-heapcompact 可关闭 patch，用于验证真实 HeapCompact 行为）
- 真实 GUI 测量：需可交互桌面会话（QT_QPA_PLATFORM=windows）；RDP/无 GPU
  环境多窗口 WebEngine 可能受限
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import subprocess
import sys
import time
import warnings

warnings.filterwarnings("ignore")
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ.pop("MEM_DIAG", None)
os.environ.pop("MEM_TRACE", None)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ── 与 main.py 一致：QApplication 创建前设置 Qt 属性 + 导入 WebEngine ──
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QWidget

QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineSettings, QWebEngineView  # noqa: F401

# ── 用量请求计数探针（运行时 patch，仅本次进程生效）──
_FETCH_CALLS: list[dict] = []


def _counting_fetch(provider_name, config, callback):
    """替换 fetch_async：计数并立即回调 None，避免真实网络请求。"""
    _FETCH_CALLS.append({"provider": provider_name, "t": time.monotonic()})
    callback(None)


import app.core.coding_plan_fetcher as cpf  # noqa: E402

cpf.fetch_async = _counting_fetch  # 运行时替换

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.tab_manager_window import TabManagerWindow  # noqa: E402
from scripts.mem_track import MemoryTracker  # noqa: E402

RESULT: dict = {}


class FakePage(QWidget):
    """与 main.py / TabManagerWindow._create_fake_page 等价的假页面"""

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
    """当前进程 RSS（MB）"""
    import psutil

    return psutil.Process().memory_info().rss / 1024 / 1024


def _count_by_provider(calls):
    out = {}
    for c in calls:
        out[c["provider"]] = out.get(c["provider"], 0) + 1
    return out


def _aggregate_times(times: list[float]) -> dict:
    """聚合耗时样本 → avg/median/min/max"""
    if not times:
        return {"n": 0, "avg_s": 0.0, "median_s": 0.0, "min_s": 0.0, "max_s": 0.0}
    return {
        "n": len(times),
        "avg_s": round(sum(times) / len(times), 4),
        "median_s": round(sorted(times)[len(times) // 2], 4),
        "min_s": round(min(times), 4),
        "max_s": round(max(times), 4),
    }


# ── 测量阶段状态（单轮进程内全局）──
app: QApplication = None
tm: TabManagerWindow = None
mem_tracker: MemoryTracker = None
OPEN_TIMES: list[float] = []
CLOSE_TIMES: list[float] = []
SWITCH_TIMES: list[float] = []
HEARTBEATS: list[float] = []
BASE_RSS: float = 0.0
PEAK_AFTER_OPEN: float = 0.0
MEMORY_ROUNDS: list[dict] = []
open_round = 0
N_TABS = 8
LEAK_THRESHOLD_MB = 5.0


def _next_timer(sec: float, fn):
    QTimer.singleShot(int(sec * 1000), fn)


# ── 阶段 1: 启动 ──
def _phase_startup():
    global tm
    t0 = time.perf_counter()
    fake_page = FakePage()
    t1 = time.perf_counter()
    first_window = OpenAIChatToolWindow(fake_page)
    t2 = time.perf_counter()
    tm = TabManagerWindow.create_instance()
    tm.add_window(first_window)
    tm.show()
    t3 = time.perf_counter()
    RESULT["startup"] = {
        "fake_page_s": round(t1 - t0, 4),
        "first_window_ctor_s": round(t2 - t1, 4),
        "tab_mgr_add_show_s": round(t3 - t2, 4),
        "total_to_ready_s": round(t3 - t0, 4),
    }
    print("阶段1 启动完成:", json.dumps(RESULT["startup"]), flush=True)
    _next_timer(0.8, _phase_mem_round_start)


# ── 阶段 2: 内存曲线（开 N tab → 关 → GC）──
def _phase_mem_round_start():
    """记录轮前基线 → 开 N tab → 记录峰值 → 关全部 → GC → 记录关闭后"""
    global mem_tracker, BASE_RSS, open_round
    mem_tracker = MemoryTracker(interval=0.05)
    mem_tracker.start()
    gc.collect()
    BASE_RSS = _rss_mb()
    open_round = 0
    print(f"--- 内存基线 base={BASE_RSS:.1f}MB ---", flush=True)
    _next_timer(0.3, _phase_open_one)


def _phase_open_one():
    """逐个打开 tab 并计时"""
    global open_round, PEAK_AFTER_OPEN
    t_start = time.perf_counter()
    tm._on_new_tab_requested()
    OPEN_TIMES.append(time.perf_counter() - t_start)
    open_round += 1
    if open_round < N_TABS:
        _next_timer(0.15, _phase_open_one)
    else:
        PEAK_AFTER_OPEN = _rss_mb()
        print(f"  开 {N_TABS} tab 后峰值={PEAK_AFTER_OPEN:.1f}MB", flush=True)
        _next_timer(0.5, _phase_close_one)


def _phase_close_one():
    """逐个关闭 tab 并计时"""
    if tm.window_count > 0:
        t_start = time.perf_counter()
        tm._close_window_at(tm.window_count - 1)
        CLOSE_TIMES.append(time.perf_counter() - t_start)
        _next_timer(0.12, _phase_close_one)
    else:
        _next_timer(0.5, _phase_close_gc)


def _phase_close_gc():
    """GC 后记录关闭后 RSS，完成内存记录"""
    global mem_tracker
    for _ in range(3):
        gc.collect()
        time.sleep(0.15)
    rss_after_close = _rss_mb()
    mem_tracker.stop()
    delta_vs_base = rss_after_close - BASE_RSS
    delta_vs_peak = rss_after_close - PEAK_AFTER_OPEN
    RESULT["memory"] = {
        "base_rss_mb": round(BASE_RSS, 2),
        "rss_after_open_mb": round(PEAK_AFTER_OPEN, 2),
        "rss_after_close_mb": round(rss_after_close, 2),
        "delta_close_vs_base_mb": round(delta_vs_base, 2),
        "delta_close_vs_peak_mb": round(delta_vs_peak, 2),
        "leak_verdict": "LEAK" if delta_vs_peak > LEAK_THRESHOLD_MB else "OK",
    }
    print("阶段3 内存:", json.dumps(RESULT["memory"]), flush=True)
    _phase_ui_prep()


# ── 阶段 4: UI 响应 ──
def _phase_ui_prep():
    """再开 2 个 tab 供切换测量（轻载：避免欢迎卡片 WebEngine 构建拖慢/卡死）"""
    for _ in range(2):
        tm._on_new_tab_requested()
    _next_timer(1.5, _phase_ui_switch)


def _phase_ui_switch(i=0):
    if tm.window_count < 2:
        # 窗口不足（极慢环境），跳过切换测量直接心跳
        _phase_ui_heartbeat()
        return
    idx = tm.window_count - 1 - (i % tm.window_count)
    t_start = time.perf_counter()
    tm._on_tab_selected(idx)
    SWITCH_TIMES.append(time.perf_counter() - t_start)
    if i + 1 < 12:
        _next_timer(0.05, lambda: _phase_ui_switch(i + 1))
    else:
        _phase_ui_heartbeat()


def _phase_ui_heartbeat():
    """事件循环心跳：16ms 定时器实际间隔 → 近似帧率（看门狗 15s 兜底）"""
    t_end = time.monotonic() + 1.0
    timer = QTimer()
    timer.setInterval(16)
    timer.timeout.connect(lambda: HEARTBEATS.append(time.monotonic()))
    timer.start()

    def stop():
        if not timer.isActive():
            return
        timer.stop()
        intervals = [b - a for a, b in zip(HEARTBEATS, HEARTBEATS[1:])] if len(HEARTBEATS) > 1 else []
        avg_itv = sum(intervals) / len(intervals) if intervals else 0.0
        RESULT["ui"] = {
            "tab_switch_avg_s": round(sum(SWITCH_TIMES) / len(SWITCH_TIMES), 4) if SWITCH_TIMES else 0.0,
            "tab_switch_max_s": round(max(SWITCH_TIMES), 4) if SWITCH_TIMES else 0.0,
            "heartbeat_interval_avg_s": round(avg_itv, 4) if intervals else None,
            "approx_fps": round(1.0 / avg_itv, 1) if avg_itv > 0 else None,
        }
        RESULT["usage_fetch"] = {
            "total": len(_FETCH_CALLS),
            "by_provider": _count_by_provider(_FETCH_CALLS),
        }
        print("阶段4 UI:", json.dumps(RESULT["ui"]), flush=True)
        print("阶段5 用量请求计数:", RESULT["usage_fetch"]["total"], flush=True)
        _finish()

    QTimer.singleShot(int((t_end - time.monotonic()) * 1000) + 100, stop)
    # 看门狗：15s 内未完成（如 WebEngine 卡死）则强制收尾
    QTimer.singleShot(15000, lambda: stop() if not RESULT.get("ui") else None)


def _finish():
    """汇总开 tab 统计并退出"""
    RESULT["open_tab"] = _aggregate_times(OPEN_TIMES)
    RESULT["open_tab"]["times_s"] = [round(t, 4) for t in OPEN_TIMES]  # 保留原始样本供主控聚合
    RESULT["close_tab"] = _aggregate_times(CLOSE_TIMES)
    RESULT["meta"] = {
        "tool": "perf_regression",
        "tabs_per_round": N_TABS,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
    }
    print("=== DONE ===", flush=True)
    app.quit()


def _run_single_shot(args: argparse.Namespace) -> int:
    """启动 GUI 事件循环完成一轮测量（独立子进程内调用）"""
    global app, N_TABS
    N_TABS = args.tabs
    app = QApplication(sys.argv)
    from app.utils import icons_light_rc, icons_rc  # noqa: F401

    app.setStyle("Fusion")
    app.setApplicationName("Drifox")

    # 与 main.py _deferred_startup 一致：初始化共享 WebEngine Profile，
    # 否则懒加载 CodeWebViewer 时 get_shared_web_profile() 抛 RuntimeError → qFatal abort
    from app.core.webengine_profile import init_shared_web_profile

    init_shared_web_profile(parent=app)

    if not args.keep_heapcompact:
        # 规避 HeapCompact access violation（T1 实测在测试环境偶发崩溃）
        import app.main_widget as mw

        mw._compact_process_heap_after_cleanup = lambda: None

    QTimer.singleShot(0, _phase_startup)
    app.exec_()  # 事件循环结束后返回（_finish 已 app.quit）
    return 0


# ── 数据目录隔离：备份/恢复 .drifox，避免污染用户数据 ──
_DATA_DIR = os.path.join(PROJECT_ROOT, ".drifox")
_BACKUP_DIR = os.path.join(PROJECT_ROOT, ".drifox.perfregression.bak")


def _backup_data_dir():
    """把当前 .drifox 备份到临时目录（先清残留备份，确保备份=测量前状态）"""
    if not os.path.isdir(_DATA_DIR):
        return False
    if os.path.isdir(_BACKUP_DIR):
        shutil.rmtree(_BACKUP_DIR)
    shutil.copytree(_DATA_DIR, _BACKUP_DIR)
    print(f"[isolate] 已备份 .drifox -> {_BACKUP_DIR}", flush=True)
    return True


def _reset_data_dir():
    """每轮前把 .drifox 恢复为备份状态（模拟干净启动）"""
    if not os.path.isdir(_BACKUP_DIR):
        return
    if os.path.isdir(_DATA_DIR):
        shutil.rmtree(_DATA_DIR)
    shutil.copytree(_BACKUP_DIR, _DATA_DIR)


def _restore_data_dir():
    """测量结束后恢复用户原始 .drifox"""
    _reset_data_dir()
    print("[isolate] 已恢复 .drifox（测量数据已隔离）", flush=True)


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── 对比表输出 ──
def _fmt(v, unit=""):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}{unit}"
    return f"{v}{unit}"


def _build_compare(baseline: dict, current: dict) -> list[dict]:
    """构造对比行：[指标, 优化前, 优化后, 变化]"""
    b_mem = baseline["summary"]["memory"]
    c_mem = current["summary"]["memory"]
    rows = [
        {
            "metric": "启动耗时 total (s)",
            "before": baseline["summary"]["startup_s"],
            "after": current["summary"]["startup_s"],
        },
        {
            "metric": "开 tab avg (s)",
            "before": baseline["summary"]["open_tab"]["avg_s"],
            "after": current["summary"]["open_tab"]["avg_s"],
        },
        {
            "metric": "开 tab median (s)",
            "before": baseline["summary"]["open_tab"]["median_s"],
            "after": current["summary"]["open_tab"]["median_s"],
        },
        {
            "metric": "开 tab max (s)",
            "before": baseline["summary"]["open_tab"]["max_s"],
            "after": current["summary"]["open_tab"]["max_s"],
        },
        {"metric": "内存 基线 (MB)", "before": b_mem["base_rss_mb"], "after": c_mem["base_rss_mb"]},
        {"metric": "内存 开 N tab 后 (MB)", "before": b_mem["rss_after_open_mb"], "after": c_mem["rss_after_open_mb"]},
        {"metric": "内存 关闭+GC 后 (MB)", "before": b_mem["rss_after_close_mb"], "after": c_mem["rss_after_close_mb"]},
        {
            "metric": "关闭 vs 打开峰值 (MB)",
            "before": b_mem["delta_close_vs_peak_mb"],
            "after": c_mem["delta_close_vs_peak_mb"],
        },
        {
            "metric": "关闭 vs 基线 (MB)",
            "before": b_mem["delta_close_vs_base_mb"],
            "after": c_mem["delta_close_vs_base_mb"],
        },
        {"metric": "LEAK 判定", "before": b_mem["leak_verdict"], "after": c_mem["leak_verdict"]},
        {
            "metric": "用量请求次数",
            "before": baseline["summary"]["usage_fetch_total"],
            "after": current["summary"]["usage_fetch_total"],
        },
        {
            "metric": "tab 切换 avg (ms)",
            "before": round(baseline["summary"]["ui"]["tab_switch_avg_s"] * 1000, 2),
            "after": round(current["summary"]["ui"]["tab_switch_avg_s"] * 1000, 2),
        },
    ]
    for r in rows:
        b, a = r["before"], r["after"]
        if isinstance(b, (int, float)) and isinstance(a, (int, float)):
            r["change"] = round(a - b, 4)
        else:
            r["change"] = "→"
    return rows


def _print_table(rows: list[dict]):
    print("")
    print("=" * 78)
    print(f"{'指标':<30} {'优化前':>14} {'优化后':>14} {'变化':>14}")
    print("-" * 78)
    for r in rows:
        print(f"{r['metric']:<30} {_fmt(r['before']):>14} {_fmt(r['after']):>14} {_fmt(r['change']):>14}")
    print("=" * 78)


# ── 聚合多轮 → summary ──
def _aggregate_summary(rounds: list[dict]) -> dict:
    """把多轮（每轮独立进程）结果聚合为回归 summary"""
    # open_tab.times_s 为单轮原始样本，跨轮合并后重新聚合
    open_times_all: list[float] = []
    for r in rounds:
        open_times_all.extend(_flatten_open_times(r))
    mems = [r["memory"] for r in rounds]
    uis = [r["ui"] for r in rounds]
    fetch_totals = [r["usage_fetch"]["total"] for r in rounds]

    def avg(vals, key):
        vals = [v[key] for v in vals]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    return {
        "startup_s": rounds[0]["startup"]["total_to_ready_s"],
        "open_tab": _aggregate_times(open_times_all),
        "memory": {
            "base_rss_mb": avg(mems, "base_rss_mb"),
            "rss_after_open_mb": avg(mems, "rss_after_open_mb"),
            "rss_after_close_mb": avg(mems, "rss_after_close_mb"),
            "delta_close_vs_base_mb": avg(mems, "delta_close_vs_base_mb"),
            "delta_close_vs_peak_mb": avg(mems, "delta_close_vs_peak_mb"),
            "leak_verdict": "LEAK" if any(m["leak_verdict"] == "LEAK" for m in mems) else "OK",
        },
        "ui": {
            "tab_switch_avg_s": round(sum(u["tab_switch_avg_s"] for u in uis) / len(uis), 4) if uis else 0.0,
            "approx_fps": round(
                sum(u["approx_fps"] for u in uis if u["approx_fps"]) / len([u for u in uis if u["approx_fps"]]), 1
            )
            if any(u["approx_fps"] for u in uis)
            else None,
        },
        "usage_fetch_total": sum(fetch_totals),
    }


def _flatten_open_times(round_data: dict) -> list[float]:
    """从单轮结果提取所有开 tab 耗时样本"""
    if "times_s" in round_data.get("open_tab", {}):
        return round_data["open_tab"]["times_s"]
    return []


def _run_round(args: argparse.Namespace, output_path: str) -> dict:
    """以独立子进程执行一轮测量，返回该轮 JSON"""
    # 每轮前重置 .drifox 为备份（干净数据目录）
    _reset_data_dir()
    cmd = [sys.executable, "-u", os.path.abspath(__file__), "--single-shot", "--tabs", str(args.tabs)]
    if args.keep_heapcompact:
        cmd.append("--keep-heapcompact")
    cmd += ["-o", output_path]
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, timeout=args.timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"单轮测量失败 rc={proc.returncode}")
    return _load_json(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DriFox 性能回归对比：开 tab / 内存 / 用量请求一键测量")
    parser.add_argument("--tabs", type=int, default=8, help="每轮打开 tab 数（默认 8）")
    parser.add_argument("--rounds", type=int, default=2, help="测量轮数（默认 2，每轮独立子进程+干净数据目录）")
    parser.add_argument("--output", "-o", default="perf_regression_result.json", help="聚合结果 JSON 路径")
    parser.add_argument("--baseline", type=str, default=None, help="优化前基线 JSON 路径（存在则输出对比表）")
    parser.add_argument("--no-isolate", action="store_true", help="不备份/恢复 .drifox（会污染用户数据，仅自检用）")
    parser.add_argument("--keep-heapcompact", action="store_true", help="不 patch HeapCompact（默认 patch 规避崩溃）")
    parser.add_argument("--timeout", type=int, default=180, help="单轮测量超时秒数（默认 180）")
    parser.add_argument("--single-shot", action="store_true", help="内部模式：仅跑一轮测量（供主控子进程调用）")
    args = parser.parse_args(argv)

    # ── 单轮模式（子进程）──
    if args.single_shot:
        _run_single_shot(args)
        _save_json(args.output, RESULT)
        return 0

    # ── 主控模式 ──
    if not args.no_isolate:
        _backup_data_dir()
    try:
        rounds = []
        for i in range(1, args.rounds + 1):
            print(f"\n===== 轮次 {i}/{args.rounds} =====", flush=True)
            out_path = os.path.join(PROJECT_ROOT, f"_perf_round_{i}.json")
            r = _run_round(args, out_path)
            rounds.append(r)
            mem = r["memory"]
            print(
                f"  轮{i}: 基线 {mem['base_rss_mb']}MB -> 开{args.tabs}tab {mem['rss_after_open_mb']}MB "
                f"-> 关后 {mem['rss_after_close_mb']}MB, delta_peak={mem['delta_close_vs_peak_mb']}MB "
                f"[{mem['leak_verdict']}]",
                flush=True,
            )
        summary = _aggregate_summary(rounds)
        result = {
            "meta": {
                "tool": "perf_regression",
                "tabs_per_round": args.tabs,
                "rounds": args.rounds,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "rounds": rounds,
            "summary": summary,
        }
        _save_json(args.output, result)
        print(f"\n[result] 已保存 -> {args.output}", flush=True)
        print("\n===== 聚合摘要 =====", flush=True)
        print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
        if args.baseline and os.path.exists(args.baseline):
            baseline = _load_json(args.baseline)
            rows = _build_compare(baseline, result)
            _print_table(rows)
        else:
            print("\n[提示] 未指定 --baseline，仅输出本次测量。", flush=True)
            print("       对比用法: uv run python scripts/perf_regression.py --baseline <优化前>.json", flush=True)
        return 0
    finally:
        if not args.no_isolate:
            _restore_data_dir()


if __name__ == "__main__":
    raise SystemExit(main())
