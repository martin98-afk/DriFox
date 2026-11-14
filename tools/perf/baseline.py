# -*- coding: utf-8 -*-
"""
DriFox 性能基线测试脚本（PyQt5 GUI）

无侵入式性能基线采集：启动耗时 / 内存基线 / 帧率 / 长时运行内存增长。
所有插桩通过运行时 monkey-patch 完成，不修改任何业务代码。

仅依赖：标准库 + psutil（项目运行时依赖，无需新增三方依赖）。
平台：Windows（主）/ Linux；默认 offscreen 平台可无显示器复现。

入口（子命令）：
  all         运行全部维度（启动×repeats + 内存 + 帧率 + 长时），汇总为一份报告
  startup     仅冷启动耗时（多次独立进程采样方差）
  mem         仅内存基线（RSS + tracemalloc + 对象数）
  fps         仅帧率采样（QTimer(16ms) 实际间隔分布）
  longevity    仅长时运行内存增长（模拟定时操作 N 次）
  _once       内部单次采样（供 subprocess 拉起），输出 JSON 到 stdout

常用：
  python tools/perf/baseline.py all --out tools/perf/results/baseline_<ts>.json
  python tools/perf/baseline.py startup --repeats 5
  python tools/perf/baseline.py longevity --ops 1000 --op-interval-ms 20
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import threading
import time
import tracemalloc
from datetime import datetime, timezone

# ---- 必须在任何 PyQt5 导入前设置 Qt 平台 ----
# 默认 offscreen 以便无显示器环境可复现；可用环境变量或父进程传入覆盖。
if "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

# 把项目根加入路径，使 `import main` / `import app` 可用
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ============================================================
# 单次 harness：导入入口、patch 首屏可见、采集指标、运行入口 main()
# ============================================================
def run_once(mode, entry="main", duration_ms=3000, frame_ms=16,
             mem_interval_ms=100, ops=0, op_interval_ms=20,
             sample_every=50, synthetic=False, enable_tracemalloc=True,
             fixture="none"):
    """
    在独立进程内运行一次采集，返回指标 dict。
    mode: timing | mem | fps | longevity
    fixture: none | synthetic-tabs[:N]（仅 synthetic 模式，锁定负载规模口径）
    """
    import PyQt5.QtCore as QtCore
    from PyQt5.QtCore import QTimer, QMetaObject, Qt
    from PyQt5.QtWidgets import QApplication
    import PyQt5.QtWidgets as qw
    import psutil

    proc = psutil.Process(os.getpid())
    t0 = time.monotonic()
    # 真实 app + longevity：app teardown 完成后再跑 tracemalloc compare_to 会
    # 触发原生断点崩溃（rc=0x80000003，实测连续复现；watchdog 栈顶=snap.compare_to）。
    # 该维度已有 RSS/对象数序列，trace_top 收益低，直接禁用换稳定退出。
    if mode == "longevity" and not synthetic:
        enable_tracemalloc = False

    # ---- patch 首屏可见（首个 top-level 窗口 showEvent）----
    orig_show = qw.QWidget.showEvent
    state = {
        "t_import_start": t0,
        "t_import_done": None,
        "t_visible": None,
        "visible_cls": None,
        "first_paint": None,
        "frame_intervals": [],
        "mem_series": [],
        "longevity_series": [],
        "ops_done": 0,
        "trace_top": [],
        "rss_visible": None,
        "obj_visible": None,
        "snap_visible": None,
        "cpu_series": [],        # B1/B2：cpu_percent 序列（相对单核，100=占满一核）
        "web_rss_series": [],    # A2：[t, main_rss_mb, web_rss_mb]（rss_sampler 口径）
        "wall_t0": datetime.now(),  # A7：日志行过滤起点（wall clock）
    }
    _last_frame = [None]
    _last_mem = [t0]
    _op_counter = [0]
    _timers = []

    def patched_show(self, event):
        w = self
        if state["t_visible"] is None and getattr(w, "isWindow", lambda: False)():
            state["t_visible"] = time.monotonic()
            state["visible_cls"] = type(w).__name__
            on_visible()
        return orig_show(self, event)

    qw.QWidget.showEvent = patched_show

    # 说明：不 patch paintEvent——真实复杂控件（含 WebEngine）上 patch 虚函数会触发
    # 原生层崩溃（0xC0000005）。首帧时刻改由帧率采样器的首次触发推导。

    # ---- 首屏可见后的采样器安装 ----
    def on_visible():
        app = QApplication.instance()
        tv = state["t_visible"]
        # B1/B2 CPU 口径初始化：首次 cpu_percent(interval=None) 建立测量基线（非阻塞）
        try:
            proc.cpu_percent(interval=None)
        except Exception:
            pass
        # 内存快照：首屏可见瞬间
        if enable_tracemalloc:
            state["snap_visible"] = tracemalloc.take_snapshot()
        try:
            state["rss_visible"] = proc.memory_info().rss
            state["obj_visible"] = len(gc.get_objects())
        except Exception:
            pass

        if mode in ("mem", "fps", "longevity", "timing"):
            # 周期内存采样
            def mem_tick():
                try:
                    rss = proc.memory_info().rss
                    obj = len(gc.get_objects())
                    cpu = proc.cpu_percent(interval=None)
                    state["cpu_series"].append(cpu)
                except Exception:
                    return
                state["mem_series"].append(
                    [round(time.monotonic() - tv, 3), rss, obj]
                )
                # A2/A4：读 rss_sampler 最近快照（真实 app 模式下可用；synthetic 无 WebEngine）
                try:
                    from app.core.infra.rss_sampler import rss_sampler
                    state["web_rss_series"].append([
                        round(time.monotonic() - tv, 3),
                        rss_sampler.rss_mb(),
                        rss_sampler.web_rss_mb(),
                    ])
                except Exception:
                    pass
            mt = QTimer()
            mt.setInterval(mem_interval_ms)
            mt.timeout.connect(mem_tick)
            mt.start()
            _timers.append(mt)

        if mode in ("fps", "timing"):
            # 帧率采样：QTimer(16ms) 记录两次触发实际间隔
            def frame_tick():
                now = time.monotonic()
                if _last_frame[0] is not None:
                    if state["first_paint"] is None:
                        # 首帧时刻 ≈ 首次实际帧间隔的起点
                        state["first_paint"] = _last_frame[0]
                    state["frame_intervals"].append(now - _last_frame[0])
                _last_frame[0] = now
            ft = QTimer()
            ft.setInterval(frame_ms)
            ft.timeout.connect(frame_tick)
            ft.start()
            _timers.append(ft)

        if mode == "longevity" and ops > 0:
            if not synthetic:
                # 真实 app 模式：密集创建/销毁临时控件会与 app 全局事件钩子/样式
                # 系统在退出阶段竞态 → 原生崩溃（实测 0x80000003 连续 3 次复现）。
                # 改为无副作用周期任务：语义=「app 挂机跑 N 拍，观察内存漂移与
                # 回收动作（A7 口径）」，不再模拟 UI 操作。
                def op_tick():
                    _op_counter[0] += 1
                    try:
                        from app.core.infra.rss_sampler import rss_sampler
                        rss_sampler.rss_mb()  # 无副作用触碰：顺带驱动采样器新鲜度
                    except Exception:
                        pass
                    if _op_counter[0] % 200 == 0:
                        gc.collect()
                    if _op_counter[0] % sample_every == 0 or _op_counter[0] == ops:
                        try:
                            rss = proc.memory_info().rss
                            obj = len(gc.get_objects())
                        except Exception:
                            rss = obj = 0
                        state["longevity_series"].append(
                            [_op_counter[0], rss, obj]
                        )
                    if _op_counter[0] >= ops:
                        state["ops_done"] = _op_counter[0]
                        QMetaObject.invokeMethod(app, "quit", Qt.QueuedConnection)
            else:
                from PyQt5.QtWidgets import QWidget, QLabel
                # 长时运行：模拟定时操作 ops 次，记录 RSS/对象数增长
                def op_tick():
                    # 模拟一次定时操作：创建并销毁临时控件 + 周期 gc
                    _op_counter[0] += 1
                    w = QLabel("op")
                    w.setText("x" * 64)
                    w.deleteLater()
                    if _op_counter[0] % 200 == 0:
                        gc.collect()
                    if _op_counter[0] % sample_every == 0 or _op_counter[0] == ops:
                        try:
                            rss = proc.memory_info().rss
                            obj = len(gc.get_objects())
                        except Exception:
                            rss = obj = 0
                        state["longevity_series"].append(
                            [_op_counter[0], rss, obj]
                        )
                    if _op_counter[0] >= ops:
                        state["ops_done"] = _op_counter[0]
                        QMetaObject.invokeMethod(app, "quit", Qt.QueuedConnection)
            lt = QTimer()
            lt.setInterval(op_interval_ms)
            lt.timeout.connect(op_tick)
            lt.start()
            _timers.append(lt)
        else:
            # 非长时模式：在 duration_ms 后自动退出
            QTimer.singleShot(duration_ms, lambda: QMetaObject.invokeMethod(
                app, "quit", Qt.QueuedConnection))

    # ---- 看门狗：超时强制退出，防 GUI 卡死 ----
    cap = (duration_ms / 1000.0) + (ops * op_interval_ms / 1000.0) + 30.0

    def watchdog():
        time.sleep(cap)
        try:
            app = QApplication.instance()
            if app is not None:
                QMetaObject.invokeMethod(app, "quit", Qt.QueuedConnection)
        except Exception:
            pass

    threading.Thread(target=watchdog, daemon=True).start()

    # ---- 导入入口模块 ----
    if synthetic:
        # 合成轻量窗口（不含 WebEngine），用于无 WebEngine 环境 / 快速验证
        if enable_tracemalloc and not tracemalloc.is_tracing():
            tracemalloc.start()
        state["t_import_done"] = time.monotonic()
        app = QApplication(sys.argv)
        from PyQt5.QtWidgets import QMainWindow
        win = QMainWindow()
        win.setWindowTitle("DriFox-baseline-synthetic")
        win.resize(800, 600)
        _apply_fixture(win, fixture)
        win.show()
        app.exec_()
        # 立即退出，跳过 Qt 原生拆卸（offscreen 下拆卸会触发原生层崩溃）
        _emit_and_exit(state, mode, entry, synthetic, enable_tracemalloc, proc)

    mod = __import__(entry, fromlist=["main"])
    if fixture not in (None, "", "none"):
        # 真实 app 模式暂无会话注入 API；synthetic-tabs 仅 synthetic 模式有意义。
        # 明确失败好过静默忽略（口径必须可复现）。
        raise SystemExit(
            f"--fixture-scenario {fixture} 仅支持 --synthetic 模式（真实 app 会话注入待接入）")
    state["t_import_done"] = time.monotonic()

    if enable_tracemalloc and not tracemalloc.is_tracing():
        tracemalloc.start()

    try:
        mod.main()
    except SystemExit:
        pass

    # 不手动拆卸 Qt 对象（会触发原生层崩溃）；构建结果后直接 os._exit 跳过拆卸
    res = _build_result(state, mode, entry, synthetic, enable_tracemalloc, proc)
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(0)


def _apply_fixture(win, fixture):
    """按 --fixture-scenario 给合成窗口加载固定负载（锁定规模口径）。

    - none: 空主窗（默认）
    - synthetic-tabs[:N]: QTabWidget × N 页，每页 20 行 QLabel（默认 N=30）
    """
    if fixture in (None, "", "none"):
        return
    if fixture.startswith("synthetic-tabs"):
        n = int(fixture.split(":", 1)[1]) if ":" in fixture else 30
        from PyQt5.QtWidgets import QTabWidget, QLabel, QVBoxLayout, QWidget
        tabs = QTabWidget()
        for i in range(n):
            page = QWidget()
            lay = QVBoxLayout(page)
            for r in range(20):
                lay.addWidget(QLabel(f"tab{i}-row{r}-" + "x" * 64))
            tabs.addTab(page, f"tab{i}")
        win.setCentralWidget(tabs)
        return
    raise SystemExit(f"未知 fixture-scenario: {fixture}（支持 none / synthetic-tabs[:N]）")


def _count_recycle_actions(wall_t0):
    """A7 口径：扫描 DriFox 日志（all*.log），统计本进程启动后的回收动作。

    匹配标记：[B4-2] strong-recycle / [T31] idle-kill / 闸门动作
    （main-soft/main-hard/kill(n)/soft-unload(n)）。纯读文件，日志缺失返回 {}。
    """
    import re
    out = {}
    try:
        try:
            from app.utils.utils import get_app_data_dir
        except ImportError:
            from app.utils import get_app_data_dir
        log_dir = get_app_data_dir() / "logs"
    except Exception:
        return out
    if not log_dir.is_dir():
        return out
    t0_str = wall_t0.strftime("%Y-%m-%d %H:%M:%S")
    pats = {
        "strong_recycle": re.compile(r"\[B4-2\] strong-recycle kill (\d+)"),
        "idle_kill": re.compile(r"\[T31\] idle-kill\((\d+)\)"),
        "gate_kill": re.compile(r"kill\((\d+)\)"),
        "gate_soft_unload": re.compile(r"soft-unload\((\d+)\)"),
        "gate_main_pressure": re.compile(r"\bmain-(?:soft|hard)\("),
    }
    try:
        for fp in sorted(log_dir.glob("all*.log")):
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        # loguru 行首时间戳 YYYY-MM-DD HH:MM:SS：字典序=时间序，
                        # 只统计本进程启动之后的行（多实例共存时不污染他进程计数）
                        if line[:19] < t0_str:
                            continue
                        for name, pat in pats.items():
                            m = pat.search(line)
                            if not m:
                                continue
                            inc = 1 if name == "gate_main_pressure" else int(m.group(1))
                            out[name] = out.get(name, 0) + inc
            except OSError:
                continue
    except Exception:
        return out
    return out


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _build_result(state, mode, entry, synthetic, enable_tracemalloc, proc):
    res = {
        "entry": entry,
        "synthetic": synthetic,
        "import_s": (state["t_import_done"] - state["t_import_start"]) if state["t_import_done"] else None,
        "to_visible_s": (state["t_visible"] - state["t_import_start"]) if state["t_visible"] else None,
        "app_init_s": (state["t_visible"] - state["t_import_done"]) if (state["t_visible"] and state["t_import_done"]) else None,
        "visible_cls": state["visible_cls"],
        "first_paint_delta_s": (state["first_paint"] - state["t_visible"]) if (state["first_paint"] and state["t_visible"]) else None,
    }
    if state["rss_visible"] is not None:
        res["rss_visible_mb"] = round(state["rss_visible"] / 1024 / 1024, 2)
        res["obj_visible"] = state["obj_visible"]

    if mode in ("mem", "fps", "timing") and state["mem_series"]:
        rss_vals = [r for _, r, _ in state["mem_series"]]
        res["mem_series_len"] = len(state["mem_series"])
        if rss_vals:
            res["rss_peak_mb"] = round(max(rss_vals) / 1024 / 1024, 2)
            res["rss_min_mb"] = round(min(rss_vals) / 1024 / 1024, 2)
        res["mem_series"] = state["mem_series"]

    if mode in ("fps", "timing") and state["frame_intervals"]:
        iv = sorted(state["frame_intervals"])
        ms = [x * 1000 for x in iv]
        res["frame_samples"] = len(ms)
        res["frame_mean_ms"] = round(sum(ms) / len(ms), 3)
        res["frame_p50_ms"] = round(_pct(ms, 50), 3)
        res["frame_p95_ms"] = round(_pct(ms, 95), 3)
        res["frame_p99_ms"] = round(_pct(ms, 99), 3)
        res["frame_min_ms"] = round(min(ms), 3)
        res["frame_max_ms"] = round(max(ms), 3)
        res["fps_mean"] = round(1000.0 / res["frame_mean_ms"], 2) if res["frame_mean_ms"] else None

    if mode == "longevity" and state["longevity_series"]:
        ser = state["longevity_series"]
        rss_start = ser[0][1]
        rss_end = ser[-1][1]
        obj_start = ser[0][2]
        obj_end = ser[-1][2]
        res["ops_total"] = state["ops_done"]
        res["rss_start_mb"] = round(rss_start / 1024 / 1024, 2)
        res["rss_end_mb"] = round(rss_end / 1024 / 1024, 2)
        res["rss_delta_mb"] = round((rss_end - rss_start) / 1024 / 1024, 2)
        res["obj_start"] = obj_start
        res["obj_end"] = obj_end
        res["obj_delta"] = obj_end - obj_start
        # 泄漏初判：对象数单调上升且无回落后仍高
        res["leak_suspected"] = bool(obj_end > obj_start * 1.1 and state["ops_done"] >= 200)
        res["longevity_series"] = ser

    if enable_tracemalloc and tracemalloc.is_tracing() and state["snap_visible"] is not None:
        try:
            snap = tracemalloc.take_snapshot()
            top = snap.compare_to(state["snap_visible"], "lineno")[:10]
            res["trace_top"] = [
                {
                    "size_kb": round(s.size / 1024, 2),
                    "size_diff_kb": round(s.size_diff / 1024, 2),
                    "count": s.count,
                    "file": s.traceback.format()[-1] if s.traceback else "",
                }
                for s in top
            ]
        except Exception:
            pass

    # ---- B1/B2 CPU 统计 ----
    if state["cpu_series"]:
        cs = sorted(state["cpu_series"])
        res["cpu_mean"] = round(sum(cs) / len(cs), 2)
        res["cpu_p95"] = round(_pct(cs, 95), 2)
        res["cpu_max"] = round(cs[-1], 2)

    # ---- A2/A4 WebEngine RSS 时序统计 + 进程级快照（A3/A4）----
    if state["web_rss_series"]:
        webs = [w for _, _, w in state["web_rss_series"] if w]
        mains = [m for _, m, _ in state["web_rss_series"] if m]
        if webs:
            res["web_rss_peak_mb"] = round(max(webs), 2)
            res["web_rss_end_mb"] = round(webs[-1], 2)
        if mains:
            res["main_rss_sampler_end_mb"] = round(mains[-1], 2)
    try:
        from app.core.infra.rss_sampler import rss_sampler
        by_pid = rss_sampler.web_rss_by_pid()
        snap = {
            "main_rss_mb": round(rss_sampler.rss_mb(), 2),
            "web_rss_mb": round(rss_sampler.web_rss_mb(), 2),
            "web_by_pid_count": len(by_pid),
            "web_pid_max_mb": round(max(by_pid.values()), 2) if by_pid else None,
            "web_pid_median_mb": (round(sorted(by_pid.values())[len(by_pid) // 2], 2)
                                  if by_pid else None),
        }
        res["rss_sampler_snapshot"] = snap
    except Exception:
        pass

    # ---- A5/A6 governor 计数器快照 ----
    try:
        from app.core.infra import memory_governor as _mg
        res["governor_snapshot"] = {
            "global_rendered_pages": int(getattr(_mg, "_global_rendered_pages", 0) or 0),
            "max_global_rendered_pages": int(getattr(_mg, "_MAX_GLOBAL_RENDERED_PAGES", 32)),
        }
    except Exception:
        pass

    # ---- A7 回收动作计数（本进程启动后的日志行）----
    res["recycle_actions"] = _count_recycle_actions(state["wall_t0"])

    # 仅当未被前面维度（如 longevity 的采样末值）设置时才用脚本退出时进程 RSS 兜底
    res.setdefault("rss_end_mb", round(proc.memory_info().rss / 1024 / 1024, 2))
    return res


def _emit_and_exit(state, mode, entry, synthetic, enable_tracemalloc, proc):
    """构建结果、打印、flush，随后 os._exit 跳过 Qt 原生拆卸（避免退出崩溃）。"""
    res = _build_result(state, mode, entry, synthetic, enable_tracemalloc, proc)
    sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    os._exit(0)


# ============================================================
# 编排：subprocess 拉起 _once，聚合多维度
# ============================================================
SELF = os.path.abspath(__file__)


def spawn_once(mode, entry, platform, retries=3, **kw):
    """拉起 _once 子进程；真实 app 在 offscreen 下可能偶发原生崩溃（WebEngine），
    故对非零退出码做有限重试。"""
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = platform
    cmd = [sys.executable, SELF, "_once", "--mode", mode, "--entry", entry]
    for k, v in kw.items():
        if isinstance(v, bool):
            if v:
                cmd.append(f"--{k.replace('_', '-')}")  # store_true 标志，不跟值
            continue
        if v is None:
            continue
        cmd += [f"--{k.replace('_', '-')}", str(v)]
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            p = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300)
        except subprocess.TimeoutExpired as e:
            last_err = f"timeout: {e}"
            continue
        if p.returncode != 0:
            # 降级容错：真实 app 退出阶段存在概率性原生崩溃（WebEngine teardown
            # 竞态，实测 0x80000003），但测量 JSON 可能在崩溃前已完整输出。
            # stdout 有合法 JSON 时接受结果并标注异常退出码，避免整维度报废。
            out = p.stdout.strip() if p.stdout else ""
            parsed = None
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        break
                    except Exception:
                        continue
            if parsed is not None:
                parsed["_crashed_exit"] = p.returncode
                return parsed
            last_err = f"rc={p.returncode}\nSTDERR: {p.stderr[-1500:]}"
            continue
        out = p.stdout.strip()
        try:
            return json.loads(out)
        except Exception:
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except Exception:
                        continue
            last_err = f"json 解析失败\nSTDOUT: {out[-1500:]}\nSTDERR: {p.stderr[-1500:]}"
            continue
    raise RuntimeError(f"_once({mode}) 重试 {retries} 次仍失败: {last_err}")


def collect_startup(repeats, entry, platform, duration_ms, synthetic=False, retries=3,
                    fixture="none"):
    samples = []
    for _ in range(repeats):
        r = spawn_once("timing", entry, platform, retries=retries,
                       synthetic=synthetic, duration_ms=duration_ms,
                       fixture_scenario=fixture)
        samples.append({
            "import_s": r.get("import_s"),
            "to_visible_s": r.get("to_visible_s"),
            "app_init_s": r.get("app_init_s"),
            "visible_cls": r.get("visible_cls"),
        })
    imp = [s["import_s"] for s in samples if s["import_s"] is not None]
    vis = [s["to_visible_s"] for s in samples if s["to_visible_s"] is not None]
    init = [s["app_init_s"] for s in samples if s["app_init_s"] is not None]

    def stat(xs):
        if not xs:
            return None
        xs2 = sorted(xs)
        return {
            "min": round(min(xs2), 4),
            "max": round(max(xs2), 4),
            "mean": round(sum(xs2) / len(xs2), 4),
            "p95": round(_pct(xs2, 95), 4),
            "stdev": round((sum((x - sum(xs2) / len(xs2)) ** 2 for x in xs2) / len(xs2)) ** 0.5, 4),
        }

    return {
        "repeats": repeats,
        "samples": samples,
        "cold": {"import_s": stat(imp), "to_visible_s": stat(vis), "app_init_s": stat(init)},
        # 热启动近似：取多次最小（模块缓存已热）
        "hot_approx_min": {
            "import_s": round(min(imp), 4) if imp else None,
            "to_visible_s": round(min(vis), 4) if vis else None,
        },
    }


def build_meta(entry, platform):
    import PyQt5.QtCore as QtCore
    return {
        "tool": "DriFox baseline.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "qt_version": QtCore.QT_VERSION_STR,
        "platform": platform,
        "entry": entry,
        "machine": sys.platform,
    }


def cmd_all(args):
    meta = build_meta(args.entry, args.platform)
    report = {"meta": meta, "dimensions": {}, "errors": {}}
    dims = {
        "startup": lambda: collect_startup(
            args.repeats, args.entry, args.platform, args.duration_ms,
            synthetic=args.synthetic, retries=args.retries,
            fixture=args.fixture_scenario),
        "memory": lambda: spawn_once(
            "mem", args.entry, args.platform, retries=args.retries,
            synthetic=args.synthetic, duration_ms=args.duration_ms,
            mem_interval_ms=args.mem_interval_ms,
            fixture_scenario=args.fixture_scenario),
        "fps": lambda: spawn_once(
            "fps", args.entry, args.platform, retries=args.retries,
            synthetic=args.synthetic, duration_ms=args.duration_ms,
            frame_ms=args.frame_ms,
            fixture_scenario=args.fixture_scenario),
        "longevity": lambda: spawn_once(
            "longevity", args.entry, args.platform, retries=args.retries,
            synthetic=args.synthetic, ops=args.ops,
            op_interval_ms=args.op_interval_ms, sample_every=args.sample_every,
            duration_ms=args.duration_ms,
            fixture_scenario=args.fixture_scenario),
    }
    for name, fn in dims.items():
        try:
            report["dimensions"][name] = fn()
        except Exception as e:
            report["errors"][name] = str(e)[:500]
    return report


def main():
    ap = argparse.ArgumentParser(description="DriFox 性能基线测试")
    sub = ap.add_subparsers(dest="cmd")

    def add_common(p):
        p.add_argument("--entry", default="main", help="入口模块（默认 main，即仓库根 main.py）")
        p.add_argument("--platform", default="offscreen", help="Qt 平台（offscreen/windows/xcb/...）")
        p.add_argument("--duration-ms", type=int, default=3000)
        p.add_argument("--no-tracemalloc", action="store_true")
        p.add_argument("--synthetic", action="store_true",
                       help="合成轻量窗口（不含 WebEngine），用于无显示/无 WebEngine 环境")
        p.add_argument("--retries", type=int, default=3, help="子进程崩溃重试次数")
        p.add_argument("--fixture-scenario", default="none",
                       help="负载口径：none / synthetic-tabs[:N]（仅 --synthetic 模式）")

    p_startup = sub.add_parser("startup"); add_common(p_startup)
    p_mem = sub.add_parser("mem"); add_common(p_mem)
    p_fps = sub.add_parser("fps"); add_common(p_fps)
    p_long = sub.add_parser("longevity"); add_common(p_long)
    p_all = sub.add_parser("all"); add_common(p_all)

    p_startup.add_argument("--repeats", type=int, default=3)
    p_fps.add_argument("--frame-ms", type=int, default=16)
    p_mem.add_argument("--mem-interval-ms", type=int, default=100)
    p_long.add_argument("--ops", type=int, default=1000)
    p_long.add_argument("--op-interval-ms", type=int, default=20)
    p_long.add_argument("--sample-every", type=int, default=50)
    p_all.add_argument("--repeats", type=int, default=3)
    p_all.add_argument("--frame-ms", type=int, default=16)
    p_all.add_argument("--mem-interval-ms", type=int, default=100)
    p_all.add_argument("--ops", type=int, default=1000)
    p_all.add_argument("--op-interval-ms", type=int, default=20)
    p_all.add_argument("--sample-every", type=int, default=50)
    p_all.add_argument("--out", default="")

    # _once 内部子命令
    once = sub.add_parser("_once")
    once.add_argument("--mode", required=True)
    once.add_argument("--entry", default="main")
    once.add_argument("--duration-ms", type=int, default=3000)
    once.add_argument("--frame-ms", type=int, default=16)
    once.add_argument("--mem-interval-ms", type=int, default=100)
    once.add_argument("--ops", type=int, default=0)
    once.add_argument("--op-interval-ms", type=int, default=20)
    once.add_argument("--sample-every", type=int, default=50)
    once.add_argument("--synthetic", action="store_true")
    once.add_argument("--no-tracemalloc", action="store_true")
    once.add_argument("--fixture-scenario", default="none")

    args = ap.parse_args()
    if args.cmd is None:
        args.cmd = "all"

    if args.cmd == "_once":
        # run_once 内部会构建结果、打印并 os._exit（跳过 Qt 拆卸）
        run_once(
            mode=args.mode, entry=args.entry, duration_ms=args.duration_ms,
            frame_ms=args.frame_ms, mem_interval_ms=args.mem_interval_ms,
            ops=args.ops, op_interval_ms=args.op_interval_ms,
            sample_every=args.sample_every, synthetic=args.synthetic,
            enable_tracemalloc=not args.no_tracemalloc,
            fixture=args.fixture_scenario)

    if args.cmd == "startup":
        rep = {"meta": build_meta(args.entry, args.platform),
               "dimensions": {"startup": collect_startup(
                   args.repeats, args.entry, args.platform, args.duration_ms,
                   synthetic=args.synthetic, retries=args.retries,
                   fixture=args.fixture_scenario)}}
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return

    if args.cmd == "mem":
        rep = {"meta": build_meta(args.entry, args.platform),
               "dimensions": {"memory": spawn_once(
                   "mem", args.entry, args.platform, retries=args.retries,
                   synthetic=args.synthetic, duration_ms=args.duration_ms,
                   mem_interval_ms=args.mem_interval_ms,
                   fixture_scenario=args.fixture_scenario)}}
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return

    if args.cmd == "fps":
        rep = {"meta": build_meta(args.entry, args.platform),
               "dimensions": {"fps": spawn_once(
                   "fps", args.entry, args.platform, retries=args.retries,
                   synthetic=args.synthetic, duration_ms=args.duration_ms,
                   frame_ms=args.frame_ms,
                   fixture_scenario=args.fixture_scenario)}}
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return

    if args.cmd == "longevity":
        rep = {"meta": build_meta(args.entry, args.platform),
               "dimensions": {"longevity": spawn_once(
                   "longevity", args.entry, args.platform, retries=args.retries,
                   synthetic=args.synthetic, ops=args.ops,
                   op_interval_ms=args.op_interval_ms, sample_every=args.sample_every,
                   duration_ms=args.duration_ms,
                   fixture_scenario=args.fixture_scenario)}}
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return

    if args.cmd == "all":
        rep = cmd_all(args)
        txt = json.dumps(rep, ensure_ascii=False, indent=2)
        print(txt)
        if args.out:
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(txt)
            print(f"[baseline] 报告已写入: {args.out}", file=sys.stderr)
        return


if __name__ == "__main__":
    main()
