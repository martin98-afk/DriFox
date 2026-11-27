#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DriFox 核心性能基准（macOS / 源码态）——一把采集四类指标。

指标与口径：
1. 启动耗时：父进程拉起真实入口 main.py，测 Popen → stderr 上
   ``[StartupMark] first_chat_window`` 直写行的墙钟时间（端到端用户感知）。
   预热一次丢弃，再跑 --repeats 次（默认 3），输出中位数/最大/最小。
2. 内存：子进程内独立采样线程按固定间隔读 ``proc_pid_rusage.phys_footprint``
   （macOS 真实物理占用，不用 RSS），给出全程峰值与末段稳态。
3. import 开销：独立子进程 ``-X importtime`` 跑同一入口，按模块名聚合
   self 耗时，输出 Top 20。
4. 首帧：独立一次运行，运行时 patch ``QWidget.show`` + QApplication 级
   事件过滤器，测「首个顶层窗口 show() → 该窗口首个 Paint 事件」。
   拿不到 Paint 时如实记录 reason=timeout_no_paint，不伪造数值。

纪律：
- 只读复用 main.py 及现有诊断钩子（DRIFOX_STARTUP_PROBE / _EXIT_MS），
  不修改任何业务代码；全部插桩在独立子进程 wrapper 内完成。
- 每轮 cwd 指向独立临时数据目录（首次预热建库，正式轮复用），避免污染
  真实 .drifox 数据，也保证多次运行可比。
- 所有输出带时间戳，结果落 tools/perf/results/benchmark_core_<ts>/。

用法：
  .venv/bin/python tools/perf/benchmark_core.py all --repeats 3
  .venv/bin/python tools/perf/benchmark_core.py all --skip-importtime
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

# 契约：本脚本固定位于 <repo>/tools/perf/ 下，PROJECT_ROOT 由自身位置推导。
# 需要测别的仓库时用 --repo-root（父进程会把它写进 DRIFOX_BENCH_REPO_ROOT，
# 子进程 wrapper 重新导入本模块时按同一变量解析，保证父子同源）。
_ENV_REPO_ROOT = os.environ.get("DRIFOX_BENCH_REPO_ROOT")
PROJECT_ROOT = (
    Path(_ENV_REPO_ROOT).resolve() if _ENV_REPO_ROOT else Path(__file__).resolve().parents[2]
)
ENTRY = PROJECT_ROOT / "main.py"
SELF = Path(__file__).resolve()
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# proc_pid_rusage 接收缓冲：系统按 flavor 完整结构体写入，缓冲不足会越界写。
# 本仓既有实现（app/utils/_startup_probe.py）实测 240 字节会写坏堆，故留 1KB。
FP_BUF_SIZE = 1024
FP_OFFSET = 16 + 7 * 8  # ri_uuid[16] + 前 7 个 uint64 = ri_phys_footprint
FP_FLAVOR_V4 = 4

MARK_RE = re.compile(r"^\[StartupMark\] (\S+) 耗时 (\d+)ms")
IMPORTTIME_RE = re.compile(r"^import time:\s+(\d+)\s+\|\s+(\d+)\s+\|\s+(\S+)")

_LIBC = None
_FP_INITED = False


def footprint_mb(pid: int | None = None) -> float:
    """指定进程（缺省当前进程）phys_footprint（MB）；非 macOS 或读取失败返回 0。"""
    global _LIBC, _FP_INITED
    if sys.platform != "darwin":
        return 0.0
    if not _FP_INITED:
        _FP_INITED = True
        try:
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            lib.proc_pid_rusage.restype = ctypes.c_int
            _LIBC = lib
        except Exception:
            _LIBC = None
    if _LIBC is None:
        return 0.0
    buf = ctypes.create_string_buffer(FP_BUF_SIZE)
    target = os.getpid() if pid is None else int(pid)
    if _LIBC.proc_pid_rusage(target, FP_FLAVOR_V4, buf) != 0:
        return 0.0
    return ctypes.c_uint64.from_buffer(buf, FP_OFFSET).value / 1048576.0


# ============================== 子进程侧 ==============================

_EMIT_LOCK = threading.Lock()


def _emit(path: str, payload: dict) -> None:
    """追加一行 JSON 到采集文件（每次 open/close，os._exit 也不丢已写数据）。"""
    line = json.dumps(payload, ensure_ascii=False)
    with _EMIT_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _start_mem_sampler(samples_path: str, interval_ms: int) -> threading.Event:
    stop = threading.Event()

    def _run() -> None:
        t0 = time.perf_counter()
        while not stop.is_set():
            _emit(
                samples_path,
                {
                    "kind": "sample",
                    "t": round(time.perf_counter() - t0, 3),
                    "fp_mb": round(footprint_mb(), 2),
                },
            )
            stop.wait(interval_ms / 1000.0)

    threading.Thread(target=_run, daemon=True, name="bench-mem-sampler").start()
    return stop


def _install_first_frame_probe(samples_path: str) -> None:
    """patch QWidget.show + app 级事件过滤器，测 show→首个 Paint。"""
    from PyQt5.QtCore import QEvent, QObject
    from PyQt5.QtWidgets import QApplication, QWidget

    state: dict = {"show_t": None, "widget": None, "watcher": None, "done": False}

    def _finish(reason: str, ms: float | None) -> None:
        # 不在此处 removeEventFilter：可能发生在过滤器自身调用栈内（Qt 不安全），
        # 且 timeout 线程不在 GUI 线程。done 标志直接让后续事件早退，开销可忽略。
        if state["done"]:
            return
        state["done"] = True
        _emit(
            samples_path,
            {
                "kind": "first_frame",
                "ok": ms is not None,
                "reason": reason,
                "show_to_paint_ms": round(ms, 2) if ms is not None else None,
            },
        )

    class _PaintWatcher(QObject):
        def eventFilter(self, obj, ev):  # noqa: N802 - Qt 命名
            try:
                if (
                    not state["done"]
                    and ev.type() == QEvent.Paint
                    and obj is state["widget"]
                ):
                    _finish("paint", (time.perf_counter() - state["show_t"]) * 1000.0)
            except Exception:
                pass
            return False

    orig_show = QWidget.show

    def patched_show(self, *args, **kwargs):
        first = state["show_t"] is None and self.isWindow()
        if first:
            state["show_t"] = time.perf_counter()
            state["widget"] = self
            app = QApplication.instance()
            if app is not None:
                watcher = _PaintWatcher()
                state["watcher"] = watcher
                app.installEventFilter(watcher)
        return orig_show(self, *args, **kwargs)

    QWidget.show = patched_show

    def _timeout() -> None:
        time.sleep(8.0)
        _finish("timeout_no_paint", None)

    threading.Thread(target=_timeout, daemon=True, name="bench-first-frame-timeout").start()


def _child_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", required=True)
    ap.add_argument("--first-frame", action="store_true")
    ap.add_argument("--sample-ms", type=int, default=50)
    args = ap.parse_args(argv)

    _emit(args.samples, {"kind": "child_start", "t": 0.0, "fp_mb": round(footprint_mb(), 2)})
    _start_mem_sampler(args.samples, args.sample_ms)
    if args.first_frame:
        try:
            _install_first_frame_probe(args.samples)
        except Exception as exc:  # 插桩失败不得阻断被测程序
            _emit(args.samples, {"kind": "first_frame", "ok": False,
                                  "reason": f"probe_error: {exc!r}", "show_to_paint_ms": None})

    import runpy

    # 入口语义对齐真实启动：__name__ == "__main__"、argv[0] 指向 main.py。
    sys.argv = [str(ENTRY)]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        runpy.run_path(str(ENTRY), run_name="__main__")
    except SystemExit:
        pass
    _emit(args.samples, {"kind": "child_return", "t": round(time.perf_counter(), 3)})
    return 0


# ============================== 父进程侧 ==============================


def _descendant_pids(root: int) -> list[int]:
    """ps 全表一次扫描，返回 root 的全部后代 pid（含 renderer 孙进程）。"""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            cpid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(cpid)
    result: list[int] = []
    stack = [root]
    while stack:
        for kid in children.get(stack.pop(), ()):
            result.append(kid)
            stack.append(kid)
    return result


def _start_tree_sampler(root_pid: int, interval_s: float = 0.5):
    """父进程视角采全树：主进程 footprint + 全部后代合计 + 子进程数。"""
    samples: list[dict] = []
    stop = threading.Event()

    def _run() -> None:
        t0 = time.perf_counter()
        while not stop.is_set():
            kids = _descendant_pids(root_pid)
            main_mb = footprint_mb(root_pid)
            child_mb = [(k, footprint_mb(k)) for k in kids]
            child_total = sum(v for _, v in child_mb)
            samples.append(
                {
                    "t": round(time.perf_counter() - t0, 2),
                    "main_mb": round(main_mb, 1),
                    "children": len(kids),
                    "renderer_total_mb": round(child_total, 1),
                    "tree_total_mb": round(main_mb + child_total, 1),
                }
            )
            stop.wait(interval_s)

    threading.Thread(target=_run, daemon=True, name="bench-tree-sampler").start()
    return stop, samples


def _summarize_tree(samples: list[dict]) -> dict:
    if not samples:
        return {}
    peak = max(samples, key=lambda x: x["tree_total_mb"])
    tail_from = samples[-1]["t"] - 1.5
    tail = [x for x in samples if x["t"] >= tail_from] or [samples[-1]]
    return {
        "main_phys_footprint_mb": round(statistics.median([x["main_mb"] for x in tail]), 1),
        "renderer_total_mb": round(statistics.median([x["renderer_total_mb"] for x in tail]), 1),
        "tree_total_mb": round(statistics.median([x["tree_total_mb"] for x in tail]), 1),
        "child_processes": int(statistics.median([x["children"] for x in tail])),
        "child_processes_peak": max(x["children"] for x in samples),
        "peak_tree_total_mb": peak["tree_total_mb"],
        "peak_tree_t_s": peak["t"],
        "tree_samples": len(samples),
    }




# 删除项目自身 pyc 时要跳过的目录（第三方/构建产物/其他 worktree）
_PYCACHE_SKIP_PARTS = {
    ".venv", ".git", "node_modules", ".worktrees", "dist", "build", "results",
    # tests/ 不参与启动 import，删它的 pyc 只会拖慢其他人跑测试
    "tests",
    # 数据目录与工具缓存目录：里面没有业务源码 pyc，误删只会破坏他人环境
    ".drifox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}


def _purge_project_pycache() -> int:
    """删除项目自身的 __pycache__（app/、tools/、根、plugins/），返回删除目录数。

    **不碰** `.venv/lib/python*/site-packages/`：用户真实场景里第三方 pyc 一直都在，
    把它们一起清掉会造出 2590ms 的合成冷启动（实测第三方 pyc 占其中 ~1.8s，
    项目自身 pyc 只值 25ms）。打包版（onedir + PYZ）根本不写 pyc，不受影响。
    """
    removed = 0
    for pycache in PROJECT_ROOT.rglob("__pycache__"):
        if any(part in _PYCACHE_SKIP_PARTS for part in pycache.parts):
            continue
        shutil.rmtree(pycache, ignore_errors=True)
        removed += 1
    return removed


def _pump(stream, log_path: Path, on_line=None) -> None:
    """逐行消费子进程输出并落盘；on_line 用于抓启动打点时刻。"""
    with open(log_path, "w", encoding="utf-8", errors="replace") as f:
        for line in stream:
            f.write(line)
            if on_line is not None:
                on_line(line)


def _parse_samples(path: Path) -> dict:
    samples: list[tuple[float, float]] = []
    first_frame = None
    child_return = None
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = rec.get("kind")
            if kind == "sample":
                fp = rec.get("fp_mb") or 0.0
                if fp > 0:
                    samples.append((float(rec.get("t", 0.0)), float(fp)))
            elif kind == "first_frame":
                first_frame = rec
            elif kind == "child_return":
                child_return = rec
    out = {"first_frame": first_frame, "samples": samples, "child_return": child_return}
    if samples:
        peak = max(v for _, v in samples)
        # 稳态窗口取末 1.5s：4s 采样窗内仍有 GC/延迟任务抖动，窗口太短会抽到
        # 瞬时低点（实测连续三轮 439/377/364 MB），拉长后中位数稳定。
        tail_from = samples[-1][0] - 1.5
        tail = [v for t, v in samples if t >= tail_from] or [samples[-1][1]]
        out["peak_mb"] = round(peak, 1)
        out["peak_t_s"] = round(max(samples, key=lambda x: x[1])[0], 2)
        out["steady_mb"] = round(statistics.median(tail), 1)
        out["steady_min_mb"] = round(min(tail), 1)
        out["steady_max_mb"] = round(max(tail), 1)
        out["first_mb"] = round(samples[0][1], 1)
        out["sample_count"] = len(samples)
        out["duration_s"] = round(samples[-1][0] - samples[0][0], 2)
    return out


def run_entry(
    *,
    tag: str,
    data_dir: Path,
    log_dir: Path,
    exit_ms: int,
    first_frame: bool = False,
    importtime: bool = False,
    pycache_prefix: Path | None = None,
    timeout_s: int = 300,
) -> dict:
    """拉起一次真实启动，返回本次测量结果。

    pycache_prefix：指向一个空目录时强制重新编译 pyc（冷启动）；None 用默认
    __pycache__（热启动）。
    """
    samples_path = log_dir / f"{tag}.jsonl"
    if samples_path.exists():
        samples_path.unlink()
    env = os.environ.copy()
    # ⚠️ 必须显式覆盖：DRIFOX_DATA_DIR 在 get_app_data_dir() 里优先级最高，
    # 调用者 shell 一旦 export 过它，子进程就会绕过 cwd 隔离把数据写到别处
    # （实测：export DRIFOX_DATA_DIR=/tmp/leak_test 后，cwd 隔离目录为空，
    # 数据全落 leak_test）。隔离不能依赖「调用者没设这个变量」。
    app_data_dir = (data_dir / ".drifox").resolve()
    app_data_dir.mkdir(parents=True, exist_ok=True)
    env.pop("DRIFOX_DATA_DIR", None)
    env["DRIFOX_DATA_DIR"] = str(app_data_dir)
    env.update(
        {
            "DRIFOX_STARTUP_PROBE": "1",
            "DRIFOX_STARTUP_PROBE_EXIT_MS": str(exit_ms),
            "DRIFOX_BENCH_SAMPLE_FILE": str(samples_path),
            "PYTHONPATH": str(PROJECT_ROOT)
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            "PYTHONDONTWRITEBYTECODE": "0",
        }
    )
    if pycache_prefix is not None:
        env["PYTHONPYCACHEPREFIX"] = str(pycache_prefix)
    else:
        env.pop("PYTHONPYCACHEPREFIX", None)
    env.pop("QT_QPA_PLATFORM", None)  # 必须真实渲染，offscreen 会改变首帧路径
    cmd = [sys.executable]
    if importtime:
        cmd += ["-X", "importtime"]
    cmd += [str(SELF), "_child", "--samples", str(samples_path)]
    if first_frame:
        cmd.append("--first-frame")
    t0 = time.perf_counter()
    marks: dict[str, float] = {}
    err_log = log_dir / f"{tag}.stderr.log"
    out_log = log_dir / f"{tag}.stdout.log"
    proc = subprocess.Popen(
        cmd,
        cwd=str(data_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tree_stop, tree_samples = _start_tree_sampler(proc.pid)

    def _on_err(line: str) -> None:
        # 只认 os.write(2) 直写的裸行（loguru 行带 " | __main__:_smark" 前缀不匹配）
        m = MARK_RE.match(line)
        if m:
            label = m.group(1)
            if label not in marks:
                marks[label] = time.perf_counter() - t0

    # 两条管道都必须由独立线程消费：主线程要留出 wait(timeout) 的位置，
    # 否则子进程挂死时读循环永不返回，超时保护形同虚设。
    out_thread = threading.Thread(target=_pump, args=(proc.stdout, out_log), daemon=True)
    err_thread = threading.Thread(target=_pump, args=(proc.stderr, err_log, _on_err), daemon=True)
    out_thread.start()
    err_thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        # 第二次 wait 仍可能超时（进程进入不可中断态）：必须吞掉，否则异常
        # 穿出函数，`returncode` 停在 None 之外的路径永远走不到，orphan 判定成死代码。
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    err_thread.join(timeout=10)
    out_thread.join(timeout=10)
    tree_stop.set()
    lifetime = time.perf_counter() - t0
    parsed = _parse_samples(samples_path)
    first_window = marks.get("first_chat_window")
    tree = _summarize_tree(tree_samples)
    result = {
        "tag": tag,
        "app_data_dir": str(app_data_dir),
        "orphan": timed_out and proc.returncode is None,
        "ok": (proc.returncode == 0) and (first_window is not None) and not timed_out,
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "cold": pycache_prefix is not None,
        "startup_ms": round(first_window * 1000, 1) if first_window is not None else None,
        "shell_visible_ms": round(marks["shell_visible_after_logging"] * 1000, 1)
        if "shell_visible_after_logging" in marks
        else None,
        "process_lifetime_s": round(lifetime, 2),
        "marks_ms": {k: round(v * 1000, 1) for k, v in marks.items()},
        "mem": {
            # 主进程（自采，50ms）——原名 steady_mb，按口径改名，保留旧键兼容
            "main_phys_footprint_mb": tree.get("main_phys_footprint_mb", parsed.get("steady_mb")),
            "steady_mb": parsed.get("steady_mb"),
            "steady_min_mb": parsed.get("steady_min_mb"),
            "steady_max_mb": parsed.get("steady_max_mb"),
            "peak_mb": parsed.get("peak_mb"),
            "peak_t_s": parsed.get("peak_t_s"),
            "first_mb": parsed.get("first_mb"),
            "sample_count": parsed.get("sample_count"),
            "window_s": parsed.get("duration_s"),
            # 父进程全树（500ms）：主进程 + 全部后代
            "renderer_total_mb": tree.get("renderer_total_mb"),
            "tree_total_mb": tree.get("tree_total_mb"),
            "child_processes": tree.get("child_processes"),
            "child_processes_peak": tree.get("child_processes_peak"),
            "peak_tree_total_mb": tree.get("peak_tree_total_mb"),
            "peak_tree_t_s": tree.get("peak_tree_t_s"),
            "tree_samples": tree.get("tree_samples"),
        },
        "first_frame": parsed.get("first_frame"),
        "log": {"stderr": str(err_log), "stdout": str(out_log), "samples": str(samples_path)},
    }
    if importtime:
        result["importtime"] = _aggregate_importtime(err_log)
        result["importtime"]["reached_first_window"] = first_window is not None
    return result


def _aggregate_importtime(err_log: Path, top_n: int = 20) -> dict:
    self_us: dict[str, int] = {}
    calls: dict[str, int] = {}
    cum_ms: dict[str, float] = {}
    total_lines = 0
    for line in err_log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = IMPORTTIME_RE.match(line)
        if not m:
            continue
        total_lines += 1
        self_v, cum_v, name = int(m.group(1)), int(m.group(2)), m.group(3)
        self_us[name] = self_us.get(name, 0) + self_v
        calls[name] = calls.get(name, 0) + 1
        cum_ms[name] = max(cum_ms.get(name, 0.0), cum_v / 1000.0)
    rows = [
        {
            "module": name,
            "self_ms": round(v / 1000.0, 1),
            "max_cum_ms": round(cum_ms[name], 1),
            "imports": calls[name],
        }
        for name, v in self_us.items()
    ]
    return {
        "total_import_ms": round(sum(self_us.values()) / 1000.0, 1),
        "distinct_modules": len(self_us),
        "importtime_lines": total_lines,
        # self：模块自身代码的导入开销（不含子模块）
        "top_self": sorted(rows, key=lambda r: -r["self_ms"])[:top_n],
        # cumulative：单条 import 记录的累计开销（含子模块）——「最重的模块」直觉口径
        "top_cumulative": sorted(rows, key=lambda r: -r["max_cum_ms"])[:top_n],
    }


# ============================== 渲染与滚动（render） ==============================

# 每帧推送的 chunk：~60B，含行内格式与段落边界，贴近真实流式输出
_STREAM_CHUNK = "流式输出内容块，包含**加粗**、`代码`与列表项。\n\n"
# 等渲染进程执行完前面排队的 DOM patch，并强制一次 layout（返回值即失焦）
_LAYOUT_FLUSH_JS = (
    "(function(){var b=document.getElementById('content-placeholder');"
    "return b?(b.scrollHeight|0):0})()"
)


def _spin_for(seconds: float) -> None:
    from PyQt5.QtCore import QEventLoop
    from PyQt5.QtWidgets import QApplication

    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        QApplication.processEvents(QEventLoop.AllEvents, 10)
        time.sleep(0.001)


def _spin_until(pred, timeout: float) -> bool:
    from PyQt5.QtCore import QEventLoop
    from PyQt5.QtWidgets import QApplication

    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if pred():
            return True
        QApplication.processEvents(QEventLoop.AllEvents, 10)
        time.sleep(0.001)
    return bool(pred())


def _js_sync(viewer, script: str, timeout: float = 15.0):
    """runJavaScript 往返：返回时该页已按 FIFO 执行完此前排队的全部脚本。"""
    box: list = []
    page = viewer.page() if viewer is not None else None
    if page is None:
        return None
    page.runJavaScript(script, lambda r: box.append(r))
    _spin_until(lambda: bool(box), timeout)
    return box[0] if box else None


def _pctl(vals: list, p: float):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[idx], 3)


def _dist_stats(vals: list) -> dict:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean_ms": round(statistics.fmean(vals), 3),
        "p50_ms": _pctl(vals, 50),
        "p95_ms": _pctl(vals, 95),
        "p99_ms": _pctl(vals, 99),
        "max_ms": round(max(vals), 3),
    }


def _make_webengine_app():
    """真实 QApplication + 共享 WebEngine Profile；无显示环境在此抛异常。"""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    import PyQt5.QtWebEngineWidgets  # noqa: F401 - 必须先于 QApplication

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from app.core.infra.webengine_profile import init_shared_web_profile

    init_shared_web_profile(parent=app)
    return app


def _bench_stream(app, *, v2: bool, frames: int) -> dict:
    """流式渲染帧耗时。

    两套口径（同一张卡、同一条流式累积路径，先后跑两轮）：
    - ``sync``：只计 Python 侧渲染调用（update_content + 强制 immediate
      _perform_update），不含 JS 执行等待。对应历史基线口径（v2 0.24ms /
      v1 1.54ms 每 payload），用于判断渲染算法本身的成本。
    - ``e2e``：每帧额外等一次 runJavaScript 往返（渲染进程执行完 DOM patch），
      是「用户可见的一帧」成本上界，含 WebEngine IPC 固定开销。
    """
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    from app.widgets.message_card import MessageCard
    from app.widgets.webview_pool import WebViewPool

    os.environ["DRIFOX_RENDER_V2"] = "1" if v2 else "0"
    pool = WebViewPool.get_instance()
    pool.set_enabled(False)  # 强制新建，避免跨模式复用带旧 _render_v2 的 viewer

    holder = QWidget()
    holder.resize(1000, 800)
    lay = QVBoxLayout(holder)
    holder.show()
    _spin_for(0.2)

    card = MessageCard(role="assistant")
    lay.addWidget(card)
    card.start_streaming_anim()  # streaming=True → 必须走 WebEngine 通道
    card.start_elapsed_tracking()  # 初始化流式原文/计时字段（生产由 UI 装载路径调用）
    card._stream_text_clear()
    card.ensure_rendered()
    ready = _spin_until(
        lambda: card.viewer is not None and bool(getattr(card.viewer, "_is_js_ready", False)),
        30.0,
    )
    info = {"ok": ready, "v2": v2, "frames": frames}
    if not ready:
        info["reason"] = "viewer/js not ready"
        holder.close()
        return info
    _js_sync(card.viewer, "1")
    info["render_v2_flag"] = bool(getattr(card.viewer, "_render_v2", None))

    def _push(wait_js: bool) -> float:
        t0 = time.perf_counter()
        card.update_content(_STREAM_CHUNK)
        card.viewer._schedule_render(immediate=True)
        if wait_js:
            _js_sync(card.viewer, _LAYOUT_FLUSH_JS)
        return (time.perf_counter() - t0) * 1000.0

    for _ in range(10):  # 预热：首帧建块/骨架分配不进分布
        _push(True)

    # ── 口径 A：同步渲染成本（每 20 帧同步一次防队列积压，同步帧不计入分布）──
    sync_times = []
    for i in range(frames):
        dt = _push(False)
        if i % 20 == 19:
            _js_sync(card.viewer, _LAYOUT_FLUSH_JS)
        else:
            sync_times.append(dt)

    # ── 口径 B：端到端（每帧等 DOM patch 执行完）──
    e2e_times = [_push(True) for _ in range(frames)]

    info["sync"] = _dist_stats(sync_times)
    info["e2e"] = _dist_stats(e2e_times)
    info["dom_text_len"] = _js_sync(
        card.viewer,
        "(function(){var b=document.getElementById('content-placeholder');"
        "return b?((b.innerText||'').length):-1})()",
    )
    holder.close()
    card.deleteLater()
    _spin_for(0.2)
    return info


def _bench_full_render(app, rounds: int = 7) -> dict:
    """全量 markdown→HTML 转换耗时（历史基线 v1=1.54ms/payload 对应的旧全量路径）。

    当前 v1 也走差量路径（同步成本与 v2 同级），此组用不同文本长度的全量转换
    给出「整页重写」的真实量级，作为历史口径的对照锚点。
    """
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    from app.widgets.message_card import MessageCard

    holder = QWidget()
    holder.resize(1000, 800)
    QVBoxLayout(holder)
    holder.show()
    _spin_for(0.15)

    card = MessageCard(role="assistant")
    holder.layout().addWidget(card)
    card.ensure_rendered()
    ready = _spin_until(
        lambda: card.viewer is not None and bool(getattr(card.viewer, "_is_js_ready", False)),
        30.0,
    )
    if not ready:
        holder.close()
        return {"ok": False, "reason": "viewer/js not ready"}

    viewer = card.viewer
    rows = {}
    for size in (4096, 16384, 65536):
        text = (_STREAM_CHUNK * (size // len(_STREAM_CHUNK) + 1))[:size]
        viewer._render_markdown_to_html(text)  # 预热（字体/主题链幂等刷新）
        times = []
        for i in range(rounds):
            # 每轮改内容并清 HTML 缓存：判等（len+采样）命中会直接返回旧结果，
            # 那样测到的不是转换成本。
            viewer._cached_streaming_html = None
            t0 = time.perf_counter()
            viewer._render_markdown_to_html(text + f"\n\n轮次 {i} 标记")
            times.append((time.perf_counter() - t0) * 1000.0)
        rows[f"{size // 1024}KB"] = _dist_stats(times)
    holder.close()
    card.deleteLater()
    _spin_for(0.2)
    return {
        "ok": True,
        "by_size": rows,
        "metric": "viewer._render_markdown_to_html(text) 单次全量转换耗时",
    }


def _new_webengine_card(holder, *, streaming: bool):
    from app.widgets.message_card import MessageCard

    card = MessageCard(role="assistant")
    holder.layout().addWidget(card)
    if streaming:
        card.start_streaming_anim()
    return card


def _bench_card_build(app, rounds: int = 5) -> dict:
    """卡片构建：首建（新建 viewer）与池化复用（WebViewPool.acquire）两条路径。"""
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    from app.widgets.webview_pool import WebViewPool

    pool = WebViewPool.get_instance()
    md = "# 标题\n\n正文 **加粗** 与 `代码`。\n\n- 项目一\n- 项目二\n\n" * 6

    def _build_once(*, use_pool: bool) -> float:
        pool.set_enabled(use_pool)
        holder = QWidget()
        holder.resize(1000, 800)
        QVBoxLayout(holder)
        holder.show()
        _spin_for(0.05)
        t0 = time.perf_counter()
        card = _new_webengine_card(holder, streaming=False)
        card.ensure_rendered()
        _spin_until(
            lambda: card.viewer is not None and bool(getattr(card.viewer, "_is_js_ready", False)),
            30.0,
        )
        card.set_content(md)
        card.viewer._schedule_render(immediate=True)
        _js_sync(card.viewer, _LAYOUT_FLUSH_JS)
        dt = (time.perf_counter() - t0) * 1000.0
        if use_pool:  # 归还池，供下一次复用
            card.detach_viewer()
        holder.close()
        card.deleteLater()
        _spin_for(0.1)
        return dt

    first = []
    reuse = []
    for _ in range(rounds):
        first.append(_build_once(use_pool=False))
    _build_once(use_pool=True)  # 先把一个 viewer 放进池
    for _ in range(rounds):
        reuse.append(_build_once(use_pool=True))
    pool.set_enabled(False)
    return {
        "ok": True,
        "first_build": _dist_stats(first),
        "pooled_reuse": _dist_stats(reuse),
        "pool_size_after": pool.size(),
        "metric": "构造 → ensure_rendered → JS 就绪 → set_content 落 DOM（含 Chromium 初始化/复用）",
    }


def _bench_scroll(app, cards: int = 200, step_px: int = 40) -> dict:
    """滚动帧耗时：200 张固定高度消息卡（虚拟滚动的占位态）逐段推进滚动条。"""
    from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget

    from app.widgets.message_card import MessageCard

    t_card0 = time.perf_counter()
    scroll = QScrollArea()
    scroll.resize(1000, 800)
    content = QWidget()
    lay = QVBoxLayout(content)
    lay.setSpacing(8)
    scroll.setWidget(content)
    scroll.setWidgetResizable(True)
    scroll.show()
    _spin_for(0.1)

    cards_list = []
    for _ in range(cards):
        card = MessageCard(role="assistant")
        card.setFixedHeight(120)
        lay.addWidget(card)
        cards_list.append(card)
    build_ms = (time.perf_counter() - t_card0) * 1000.0
    _spin_for(0.5)

    instantiated = sum(1 for c in cards_list if getattr(c, "viewer", None) is not None)
    bar = scroll.verticalScrollBar()
    times = []
    positions = list(range(0, max(1, bar.maximum()) + 1, step_px))
    for v in positions:
        t0 = time.perf_counter()
        bar.setValue(v)
        app.processEvents()
        times.append((time.perf_counter() - t0) * 1000.0)
    max_range = bar.maximum()
    scroll.close()
    for c in cards_list:
        c.deleteLater()
    _spin_for(0.2)
    out = {
        "ok": True,
        "cards": cards,
        "viewer_instances": instantiated,
        "card_build_total_ms": round(build_ms, 1),
        "steps": len(positions),
        "range_px": max_range,
        "metric": "QScrollBar.setValue 逐段推进 + processEvents（卡片为固定高度占位态，不含 WebEngine 内容绘制）",
    }
    out.update(_dist_stats(times))
    return out


def _render_child_main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", required=True)
    ap.add_argument("--frames", type=int, default=200)
    ap.add_argument("--cards", type=int, default=200)
    ap.add_argument("--rounds", type=int, default=5)
    args = ap.parse_args(argv)

    out = args.samples
    _emit(out, {"kind": "render_child_start", "t": 0.0})

    def _run(name: str, fn) -> None:
        t0 = time.perf_counter()
        try:
            res = fn()
        except Exception as exc:  # 单组失败不影响其他组
            import traceback

            traceback.print_exc()
            res = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        res["elapsed_s"] = round(time.perf_counter() - t0, 2)
        _emit(out, {"kind": "render_metric", "name": name, **res})

    try:
        app = _make_webengine_app()
    except Exception as exc:
        _emit(out, {"kind": "render_metric", "name": "env",
                    "ok": False, "reason": f"QApplication/WebEngine 初始化失败: {exc!r}"})
        return 3

    _run("stream_v2", lambda: _bench_stream(app, v2=True, frames=args.frames))
    _run("stream_v1", lambda: _bench_stream(app, v2=False, frames=args.frames))
    _run("card_build", lambda: _bench_card_build(app, rounds=args.rounds))
    _run("full_md_render", lambda: _bench_full_render(app))
    _run("scroll", lambda: _bench_scroll(app, cards=args.cards))
    _emit(out, {"kind": "render_child_done", "t": round(time.perf_counter(), 2)})
    return 0


def _env_gate() -> dict:
    """测量环境门禁快照（measure_gate.collect）。失败不影响基准本身。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from measure_gate import collect as _collect

        return _collect()
    except Exception as exc:  # noqa: BLE001 — 门禁只是标注，绝不阻断测量
        return {"level": "UNKNOWN", "error": repr(exc)}


def _print_env_gate(gate: dict) -> None:
    if not gate:
        return
    print(
        f"[环境] load {gate.get('load_avg')}（{gate.get('cpu_cores')} 核）｜ "
        f"main.py 实例 {gate.get('main_py_instances')} ｜ idle {gate.get('cpu_idle_pct')}% ｜ "
        f"判定 {gate.get('level')}"
    )
    if gate.get("level") in ("STOP", "CAUTION"):
        print(f"       {gate.get('verdict')}；{'；'.join(gate.get('reasons') or [])}")


def _git_info() -> dict:
    def _run(*cmd: str) -> str:
        return subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *cmd],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()

    try:
        return {"commit": _run("rev-parse", "--short", "HEAD"),
                "dirty": bool(_run("status", "--porcelain"))}
    except Exception as exc:
        return {"commit": None, "dirty": None, "error": repr(exc)}


def run_render(*, data_dir, log_dir, frames: int, cards: int, rounds: int, timeout_s: int = 300) -> dict:
    samples_path = log_dir / "render.jsonl"
    if samples_path.exists():
        samples_path.unlink()
    env = os.environ.copy()
    app_data_dir = (data_dir / ".drifox").resolve()
    app_data_dir.mkdir(parents=True, exist_ok=True)
    env.pop("DRIFOX_DATA_DIR", None)
    env["DRIFOX_DATA_DIR"] = str(app_data_dir)
    env.update({
        "PYTHONPATH": str(PROJECT_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
    })
    env.pop("QT_QPA_PLATFORM", None)  # 必须真实渲染
    cmd = [sys.executable, str(SELF), "_render", "--samples", str(samples_path),
           "--frames", str(frames), "--cards", str(cards), "--rounds", str(rounds)]
    err_log = log_dir / "render.stderr.log"
    out_log = log_dir / "render.stdout.log"
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, cwd=str(data_dir), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
    out_thread = threading.Thread(target=_pump, args=(proc.stdout, out_log), daemon=True)
    err_thread = threading.Thread(target=_pump, args=(proc.stderr, err_log), daemon=True)
    out_thread.start()
    err_thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        # 第二次 wait 仍可能超时（进程进入不可中断态）：必须吞掉，否则异常
        # 穿出函数，`returncode` 停在 None 之外的路径永远走不到，orphan 判定成死代码。
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    err_thread.join(timeout=10)
    out_thread.join(timeout=10)

    metrics = {}
    if samples_path.exists():
        for raw in samples_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "render_metric":
                metrics[rec["name"]] = rec
    return {
        "ok": (proc.returncode == 0) and not timed_out and "env" not in metrics,
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "metrics": metrics,
        "log": {"stderr": str(err_log), "stdout": str(out_log), "samples": str(samples_path)},
    }


def cmd_render(args) -> int:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = RESULTS_DIR / f"benchmark_render_{ts}"
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="drifox_render_"))
    print(f"[{datetime.now().isoformat(timespec='seconds')}] 工作区：{data_dir}")
    res = run_render(data_dir=data_dir, log_dir=log_dir, frames=args.frames,
                     cards=args.cards, rounds=args.rounds)
    report = {
        "tool": "benchmark_core.render",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git": _git_info(),
        "env_gate": _env_gate(),
        "env": {"platform": sys.platform, "python": sys.version.split()[0],
                "executable": sys.executable,
                "repo_root": str(PROJECT_ROOT),
                "data_dir": str((data_dir / ".drifox").resolve())},
        "render": res,
        "out_path": str(out_dir / "report.json"),
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_env_gate(report.get("env_gate") or {})
    _print_render(report)
    return 0 if res["ok"] else 1


def _print_render(report: dict) -> None:
    g = report["git"]
    print(f"\n===== DriFox render @ {report['generated_at']}  git={g.get('commit')} dirty={g.get('dirty')} =====")
    _print_render_metrics((report.get("render") or {}).get("metrics") or {})
    print(f"[输出] {report['out_path']}\n")


def _print_render_metrics(m: dict) -> None:
    for key, label in (("stream_v2", "流式 v2"), ("stream_v1", "流式 v1")):
        d = m.get(key) or {}
        if not d.get("ok"):
            print(f"[{label}] 失败：{d.get('reason') or d}")
            continue
        sy, e2 = d.get("sync") or {}, d.get("e2e") or {}
        print(f"[{label}] 同步渲染 n={sy.get('n')}  p50 {sy.get('p50_ms')} ms  p95 {sy.get('p95_ms')} ms  "
              f"max {sy.get('max_ms')} ms  (mean {sy.get('mean_ms')})")
        print(f"           端到端   n={e2.get('n')}  p50 {e2.get('p50_ms')} ms  p95 {e2.get('p95_ms')} ms  "
              f"max {e2.get('max_ms')} ms  (mean {e2.get('mean_ms')})  dom={d.get('dom_text_len')}")
    fm = m.get("full_md_render") or {}
    if fm.get("ok"):
        parts = "  ".join(
            f"{k}: p50 {v.get('p50_ms')} ms" for k, v in (fm.get("by_size") or {}).items()
        )
        print(f"[全量转换] {parts}")
    cb = m.get("card_build") or {}
    if cb.get("ok"):
        f, r = cb["first_build"], cb["pooled_reuse"]
        print(f"[卡片] 首建 n={f.get('n')} 均值 {f.get('mean_ms')} ms p95 {f.get('p95_ms')} ms ｜ "
              f"池化复用 n={r.get('n')} 均值 {r.get('mean_ms')} ms p95 {r.get('p95_ms')} ms")
    else:
        print(f"[卡片] 失败：{cb.get('reason')}")
    sc = m.get("scroll") or {}
    if sc.get("ok"):
        print(f"[滚动] {sc['cards']} 张卡（viewer 实例 {sc['viewer_instances']}）/ {sc['steps']} 段："
              f"均值 {sc.get('mean_ms')} ms  p95 {sc.get('p95_ms')} ms  最大 {sc.get('max_ms')} ms")
    else:
        print(f"[滚动] 失败：{sc.get('reason')}")


def _print_report(report: dict) -> None:
    ts = report["generated_at"]
    print(f"\n===== DriFox benchmark_core @ {ts} =====")
    st = report["startup"]
    if st.get("cold_startup_ms") is not None:
        print(
            f"[启动-冷/{st.get('cold_mode', '?')}] {len(st['cold_samples'])} 次 中位 {st['cold_startup_ms']} ms  "
            f"最大 {st['cold_max_ms']} ms  最小 {st['cold_min_ms']} ms  各次 {st['cold_samples']} ms"
        )
    if st.get("warm_startup_ms") is not None:
        print(
            f"[启动-热] {len(st['warm_samples'])} 次 中位 {st['warm_startup_ms']} ms  "
            f"最大 {st['max_ms']} ms  最小 {st['min_ms']} ms  各次 {st['warm_samples']} ms"
        )
    if st.get("cold_startup_ms") is None and st.get("warm_startup_ms") is None:
        print("[启动] 无有效样本")
    mem = report["memory"]
    print(
        f"[内存] main {mem.get('main_phys_footprint_mb')} MB ｜ renderer 合计 "
        f"{mem.get('renderer_total_mb')} MB ｜ 全树 {mem.get('tree_total_mb')} MB ｜ "
        f"子进程 {mem.get('child_processes')} 个（峰值 {mem.get('child_processes_peak')}）"
    )
    print(
        f"       主进程峰值 {mem.get('peak_mb')} MB @ t={mem.get('peak_mb_t_s')}s ｜ "
        f"全树峰值 {mem.get('peak_tree_total_mb')} MB @ t={mem.get('peak_tree_t_s')}s"
    )
    print(f"       采样 {mem.get('sample_count')} 次 / {mem.get('window_s')}s，phys_footprint（非 RSS）")
    ff = report["first_frame"]
    if ff.get("ok"):
        print(f"[首帧] 主窗口 show → 首个 Paint: {ff['show_to_paint_ms']} ms")
    else:
        print(f"[首帧] 未取到（reason={ff.get('reason')}）")
    it = report.get("importtime") or {}
    if it.get("top_cumulative"):
        print(
            f"[import] 累计 {it['total_import_ms']} ms（{it['distinct_modules']} 个模块，"
            f"首窗打点 {it['reached_first_window']}），Top5 累计最重："
        )
        for row in it["top_cumulative"][:5]:
            print(f"        {row['max_cum_ms']:>8.1f} ms  {row['module']}")
    print(f"[输出] {report['out_path']}\n")


def cmd_all(args: argparse.Namespace) -> int:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = RESULTS_DIR / f"benchmark_core_{ts}"
    log_dir = out_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="drifox_bench_"))
    cold_pycache = Path(tempfile.mkdtemp(prefix="drifox_bench_pyc_"))
    print(f"[{datetime.now().isoformat(timespec='seconds')}] 工作区：{data_dir}（数据与项目隔离）")

    # ── 冷启动组 ──
    # project（默认）：只删项目 __pycache__，第三方 pyc 保留 = 源码运行者的真实首次启动
    # all（旧口径）：PYTHONPYCACHEPREFIX 指向空目录，连第三方 pyc 一起清 = 合成值
    cold_runs = []
    for i in range(args.cold_repeats):
        if args.cold_mode == "project":
            _purge_project_pycache()
            cold_prefix = None
        else:
            shutil.rmtree(cold_pycache, ignore_errors=True)
            cold_pycache.mkdir(parents=True, exist_ok=True)
            cold_prefix = cold_pycache
        cold_data = Path(tempfile.mkdtemp(prefix=f"drifox_bench_cold_data_{i}_"))
        r = run_entry(tag=f"cold_{i + 1}", data_dir=cold_data, log_dir=log_dir,
                      exit_ms=args.cold_exit_ms, pycache_prefix=cold_prefix)
        cold_runs.append(r)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 冷启动[{args.cold_mode}] "
              f"{i + 1}/{args.cold_repeats}: ok={r['ok']} startup={r['startup_ms']} ms")

    # 预热：生成会话库/配置并写 pyc（热启动组的前置，不计入统计）
    warm = run_entry(tag="warmup", data_dir=data_dir, log_dir=log_dir,
                     exit_ms=min(args.exit_ms, 1500))
    print(f"[预热] ok={warm['ok']} startup={warm['startup_ms']} ms（丢弃）")
    runs = []
    for i in range(args.repeats):
        r = run_entry(tag=f"startup_{i + 1}", data_dir=data_dir, log_dir=log_dir,
                      exit_ms=args.exit_ms)
        runs.append(r)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 热启动 {i + 1}/{args.repeats}: "
              f"ok={r['ok']} startup={r['startup_ms']} ms  "
              f"main={r['mem'].get('main_phys_footprint_mb')} MB "
              f"全树={r['mem'].get('tree_total_mb')} MB "
              f"子进程={r['mem'].get('child_processes')}")
    ok_runs = [r for r in runs if r["ok"] and r["startup_ms"] is not None]
    cold_ok = [r for r in cold_runs if r["ok"] and r["startup_ms"] is not None]
    startup_ms = [r["startup_ms"] for r in ok_runs]
    cold_ms = [r["startup_ms"] for r in cold_ok]
    steady = [r["mem"]["steady_mb"] for r in ok_runs if r["mem"].get("steady_mb")]
    peak = [r["mem"]["peak_mb"] for r in ok_runs if r["mem"].get("peak_mb")]
    steady_ranges = [
        (r["mem"]["steady_min_mb"], r["mem"]["steady_max_mb"])
        for r in ok_runs
        if r["mem"].get("steady_min_mb") is not None
    ]

    def _med(key: str):
        vals = [r["mem"][key] for r in ok_runs if r["mem"].get(key) is not None]
        return round(statistics.median(vals), 1) if vals else None

    def _maxv(key: str):
        vals = [r["mem"][key] for r in ok_runs if r["mem"].get(key) is not None]
        return round(max(vals), 1) if vals else None

    # 首帧：单独一次运行（需提前 import PyQt5 装钩子，不计入启动耗时）
    ff_run = run_entry(tag="first_frame", data_dir=data_dir, log_dir=log_dir,
                       exit_ms=1200, first_frame=True)
    ff = ff_run.get("first_frame") or {"ok": False, "reason": "no_data", "show_to_paint_ms": None}

    importtime_res = None
    it_run = None
    if not args.skip_importtime:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 采 import 开销（-X importtime，较慢）…")
        it_run = run_entry(tag="importtime", data_dir=data_dir, log_dir=log_dir,
                           exit_ms=400, importtime=True, timeout_s=300)
        importtime_res = it_run.get("importtime")
        if importtime_res is not None:
            importtime_res["run_ok"] = it_run["ok"]
            importtime_res["startup_ms"] = it_run["startup_ms"]

    real_runs = cold_runs + runs + ([it_run] if it_run is not None else [])

    render_res = None
    if not getattr(args, "skip_render", False):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 采渲染与滚动指标（真实 WebEngine）…")
        render_res = run_render(data_dir=data_dir, log_dir=log_dir, frames=200, cards=200, rounds=5)

    report = {
        "tool": "benchmark_core",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "cpu_count": os.cpu_count(),
            "entry": str(ENTRY),
            "repo_root": str(PROJECT_ROOT),
            "data_dir": str((data_dir / ".drifox").resolve()),
            "data_dir_note": "子进程实际使用的数据目录（显式钉死 DRIFOX_DATA_DIR，不继承调用者）",
            "cwd_isolated": True,
            "qt_platform": os.environ.get("QT_QPA_PLATFORM") or "default(native)",
        },
        "startup": {
            # 冷/热两组分开报：冷=空 PYTHONPYCACHEPREFIX + 全新数据目录；热=已有 pyc
            "cold_mode": args.cold_mode,
            "cold_startup_ms": round(statistics.median(cold_ms), 1) if cold_ms else None,
            "cold_samples": cold_ms,
            "cold_max_ms": max(cold_ms) if cold_ms else None,
            "cold_min_ms": min(cold_ms) if cold_ms else None,
            "warm_startup_ms": round(statistics.median(startup_ms), 1) if startup_ms else None,
            "warm_samples": startup_ms,
            # 兼容旧键：取热启动组
            "repeats": len(startup_ms),
            "samples": startup_ms,
            "median_ms": round(statistics.median(startup_ms), 1) if startup_ms else None,
            "max_ms": max(startup_ms) if startup_ms else None,
            "min_ms": min(startup_ms) if startup_ms else None,
            "metric": (
                "Popen → [StartupMark] first_chat_window（stderr 直写行）。"
                "cold_mode=project（默认）只清项目 __pycache__、保留第三方 pyc —— "
                "这是源码运行者首次启动的真实成本；cold_mode=all 是旧口径"
                "（PYTHONPYCACHEPREFIX 清空缓存目录，第三方 pyc 一并重编，合成值偏高）。"
                "打包版（onedir+PYZ）不写 pyc、完全不受影响。"
            ),
        },
        "memory": {
            # 三列口径：main=主进程，renderer=全部后代合计，tree=两者之和
            "main_phys_footprint_mb": _med("main_phys_footprint_mb"),
            "renderer_total_mb": _med("renderer_total_mb"),
            "tree_total_mb": _med("tree_total_mb"),
            "child_processes": _med("child_processes"),
            "child_processes_peak": _maxv("child_processes_peak"),
            "peak_tree_total_mb": _maxv("peak_tree_total_mb"),
            "peak_mb_t_s": _med("peak_t_s"),
            "peak_tree_t_s": _med("peak_tree_t_s"),
            # 兼容旧键：steady_mb = 主进程稳态（自采 50ms）
            "steady_mb": round(statistics.median(steady), 1) if steady else None,
            "steady_min_mb": min((a for a, _ in steady_ranges), default=None),
            "steady_max_mb": max((b for _, b in steady_ranges), default=None),
            "peak_mb": max(peak) if peak else None,
            "sample_count": int(
                statistics.median(
                    [r["mem"]["sample_count"] for r in ok_runs if r["mem"].get("sample_count")]
                )
            )
            if ok_runs
            else None,
            "window_s": statistics.median(
                [r["mem"]["window_s"] for r in ok_runs if r["mem"].get("window_s")]
            )
            if ok_runs
            else None,
            "metric": (
                "phys_footprint（非 RSS）。main=主进程 50ms 自采；renderer=父进程每 "
                "500ms 用 ps 全表递归 + proc_pid_rusage 采全部后代；tree=两者之和。"
                "稳态=末 1.5s 中位数，峰值=全程最大并附时刻"
            ),
            "per_run": [r["mem"] for r in ok_runs],
        },
        "first_frame": {
            **ff,
            "metric": "QWidget.show(首个顶层窗口) → 该窗口首个 QEvent.Paint（app 级事件过滤器）",
            "probe_overhead_note": "该次运行提前 import PyQt5 装钩子，只用于区间测量，不计入启动样本",
        },
        "importtime": importtime_res,
        "render": render_res,
        "git": _git_info(),
        "env_gate": _env_gate(),
        "git_commit": _git_info().get("commit"),
        "git_dirty": _git_info().get("dirty"),
        "runs": real_runs,
        "warmup": warm,
        "notes": [
            "启动时长含解释器启动与全部顶层 import，不含用户首次交互后的 deferred 任务。",
            "内存三列：main=主进程，renderer=全部后代合计，tree=两者之和；"
            "旧字段 steady_mb 保留为兼容别名（等于主进程稳态）。",
            "启动分冷/热两组。冷启动默认 --cold-mode project（只清项目 __pycache__，"
            "保留第三方 pyc）；--cold-mode all 是旧口径（连第三方 pyc 一起清）。"
            "两种口径的数字不可混比。",
            "首次预热轮用于生成会话库与 pyc，已从热启动统计中剔除。",
            "启动探针（DRIFOX_STARTUP_PROBE）自身开销在毫秒级；连续运行同版本，"
            "启动中位数仍有 ±60ms 抖动，优化前后对比应各跑 ≥3 次取中位数。",
            "首帧为单独一轮测量（需提前 import PyQt5 装钩子），只采 show→Paint 区间，"
            "该轮不计入启动样本。",
        ],
        "out_path": str(out_dir / "report.json"),
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_env_gate(report.get("env_gate") or {})
    _print_report(report)
    if render_res is not None:
        _print_render_metrics((render_res or {}).get("metrics") or {})
    return 0 if startup_ms else 1


def main(argv: list[str]) -> int:
    if argv and argv[0] == "_child":
        return _child_main(argv[1:])
    if argv and argv[0] == "_render":
        return _render_child_main(argv[1:])
    ap = argparse.ArgumentParser(description="DriFox 核心性能基准")
    sub = ap.add_subparsers(dest="mode", required=True)
    p_all = sub.add_parser("all", help="跑全部五类指标（启动/内存/import/首帧/渲染）")
    p_all.add_argument("--repeats", type=int, default=3, help="热启动采样次数（≥3）")
    p_all.add_argument("--exit-ms", type=int, default=30000,
                       help="热启动轮首窗就绪后再运行多久（默认 30s，覆盖 6.6s 峰值段）")
    p_all.add_argument("--cold-exit-ms", type=int, default=8000,
                       help="冷启动轮首窗就绪后再运行多久")
    p_all.add_argument("--cold-repeats", type=int, default=3,
                       help="冷启动采样次数")
    p_all.add_argument("--cold-mode", choices=("project", "all"), default="project",
                       help="冷启动口径：project（默认，只清项目 __pycache__，保留第三方 pyc，"
                            "= 源码运行者真实场景）/ all（旧口径，连第三方 pyc 一起清，合成值偏高）")
    p_all.add_argument("--skip-importtime", action="store_true")
    p_all.add_argument("--skip-render", action="store_true", help="跳过渲染/滚动指标")
    p_all.add_argument("--repo-root", default=None,
                       help="被测仓库根目录（默认按本脚本位置推导 <repo>/tools/perf/）")
    p_render = sub.add_parser("render", help="只跑渲染与滚动指标（流式/卡片构建/滚动）")
    p_render.add_argument("--frames", type=int, default=200, help="流式帧数（≥200）")
    p_render.add_argument("--cards", type=int, default=200, help="滚动场景卡片数（≥200）")
    p_render.add_argument("--rounds", type=int, default=5, help="卡片构建/复用采样轮数")
    p_render.add_argument("--repo-root", default=None,
                          help="被测仓库根目录（默认按本脚本位置推导）")
    args = ap.parse_args(argv)
    if getattr(args, "repo_root", None):
        global PROJECT_ROOT, ENTRY
        PROJECT_ROOT = Path(args.repo_root).resolve()
        ENTRY = PROJECT_ROOT / "main.py"
        if not ENTRY.exists():
            print(
                f"--repo-root 无效：{PROJECT_ROOT} 下没有 main.py（ENTRY={ENTRY}）。"
                f"子进程会静默 ok=False，故在此直接失败。",
                file=sys.stderr,
            )
            return 2
        os.environ["DRIFOX_BENCH_REPO_ROOT"] = str(PROJECT_ROOT)  # 子进程 wrapper 同源
    if args.mode == "all":
        if args.repeats < 3:
            print("[warn] --repeats 小于 3，样本量不足以取中位数", file=sys.stderr)
        return cmd_all(args)
    if args.mode == "render":
        return cmd_render(args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
