#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perf_baseline.py — DriFox 性能基线一键测量（启动耗时 + 稳态内存 + tracemalloc Top）。

运行说明
--------
# 1) 全套基线（冷启动 main.py N 轮，解析 [StartupMark] 日志得分段耗时 + 稳态 RSS）
uv run python scripts/perf_baseline.py
# 2) 两轮对比：第 1 轮冷（含数据目录初始化），第 2 轮热（常态启动）
uv run python scripts/perf_baseline.py --rounds 2
# 3) tracemalloc Top 分配点（会拖慢启动，仅用于分配点定位，启动数字仅供参考）
uv run python scripts/perf_baseline.py --trace
# 4) 只测已运行实例的内存
uv run python scripts/perf_baseline.py --mem-only --pid <PID>

测量语义
--------
- 启动里程碑锚点来自 main.py 自带的 [StartupMark] 日志打点（增量读
  <data_dir>/logs/all.log）。日志 sink 在壳可见前的 [StartupMark]（Qt import /
  fake_page 等）不落盘，故用外部轮询时刻补两个总锚点：
  t_spawn→壳可见（shell_visible_after_logging）、壳可见→首窗就绪（first_chat_window，
  可交互近似）。日志时间戳仅秒精度，到达时刻用 50ms 轮询时刻近似。
- 稳态内存 = 首窗就绪后再等 stable 秒（默认 15，覆盖 deferred startup 与
  WebEngine 预热释放窗），连采 5 次取中位；进程树按 main/webengine/python_child/
  other 分类（与 mem_baseline.py 同一口径）。
- 隔离（R1 收紧，默认一律隔离）：自动检测只认 cmdline 含 main.py 的源码版
  实例，打包版（Drifox.app/PyInstaller）识别不到会让测试实例与真实实例并发
  写 .drifox。故默认 DRIFOX_DATA_DIR=.perf_out/data_isolated（项目官方多实例
  机制），仅显式 --no-isolate 才用真实目录（自担并发写风险）。多轮复用同一
  目录 → 第 1 轮冷、后续轮热。
- tracemalloc：--trace 时向子进程注入 PYTHONPATH sitecustomize 探针，
  仅当 DRIFOX_PERF=1 才激活（双保险，默认路径零开销）；探针激活时打
  DRIFOX_PERF_CHILD 哨兵，被测进程派生的插件 loader/MCP stdio 等子进程
  不再激活（否则每个子进程被 25 帧追踪拖慢）。tracemalloc.start(25)
  常驻使启动偏慢，属预期。不修改任何业务代码。
- 输出：.perf_out/perf_baseline.json + 控制台摘要。测量期间会短暂弹出真实窗口。

依赖：psutil（pyproject 核心依赖，无新增）；Python 3.10+。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
OUT_DIR = PROJECT_ROOT / ".perf_out"
DEFAULT_OUT = OUT_DIR / "perf_baseline.json"

MARK_RE = re.compile(r"\[StartupMark\]\s+(.+?)\s+耗时\s+(\d+)ms")
# Y4: MARK_RE 与 app/main.py StartupMark 打点格式（「耗时 Nms」中文格式）强耦合，
# main.py 改措辞需同步改此处，否则锚点解析全空 → 整轮假 FAIL。
POLL_INTERVAL = 0.05
KEY_MILESTONES = ("shell_visible_after_logging", "first_chat_window")

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]

from scripts.mem_baseline import classify, snapshot  # noqa: E402  (psutil 复用口径)

# ────────────────────────── sitecustomize 探针（--trace 专用） ──────────────────────────

PROBE_SOURCE = '''
# DRIFOX_PERF 探针：仅在环境变量 DRIFOX_PERF=1 时激活（否则纯 no-op）。
# 由 scripts/perf_baseline.py --trace 经 PYTHONPATH 注入，不改任何业务代码。
import os, sys

if os.environ.get("DRIFOX_PERF") == "1" and os.environ.get("DRIFOX_PERF_CHILD") != "1":
    # Y2: 哨兵阻断继承激活——被测主进程派生的插件 loader/MCP stdio 子进程
    # 继承 DRIFOX_PERF=1 时不再追踪（否则每个子进程被 25 帧追踪拖慢）
    os.environ["DRIFOX_PERF_CHILD"] = "1"
    def _drifox_perf_probe():
        try:
            import threading, time
            def _run():
                try:
                    # 等业务代码导入 PyQt5（探针不主动 import Qt，避免扰动启动时序）
                    for _ in range(1200):  # 60s 轮询上限（1200 × 50ms）
                        if "PyQt5.QtWidgets" in sys.modules:
                            break
                        time.sleep(0.05)
                    from PyQt5.QtWidgets import QApplication
                    for _ in range(2400):  # 120s 轮询上限（2400 × 50ms）
                        if QApplication.instance() is not None:
                            break
                        time.sleep(0.05)
                    stable = float(os.environ.get("DRIFOX_PERF_STABLE_S", "15"))
                    time.sleep(stable)
                    import tracemalloc
                    snap = tracemalloc.take_snapshot()
                    top_n = int(os.environ.get("DRIFOX_PERF_TOP", "20"))
                    rows = []
                    for s in snap.statistics("lineno")[:top_n]:
                        fr = s.traceback[0]
                        rows.append({
                            "site": f"{fr.filename}:{fr.lineno}",
                            "size_mb": round(s.size / 1048576, 2),
                            "count": s.count,
                        })
                    payload = {"tracemalloc_top": rows,
                               "top_total_mb": round(sum(r["size_mb"] for r in rows), 2)}
                    out = os.environ.get("DRIFOX_PERF_OUT", "")
                    if out:
                        import json
                        with open(out, "w", encoding="utf-8") as f:
                            json.dump(payload, f, ensure_ascii=False, indent=2)
                    sys.stderr.write("[DRIFOX_PERF] tracemalloc top -> %s\\n" % (out or "stdout"))
                except Exception as e:
                    sys.stderr.write("[DRIFOX_PERF] probe failed: %r\\n" % (e,))
            threading.Thread(target=_run, daemon=True, name="drifox-perf-probe").start()
        except Exception:
            pass

    import tracemalloc as _tm
    if not _tm.is_tracing():
        _tm.start(25)  # 尽早开启，覆盖启动期分配（有开销，仅 trace 模式）
    _drifox_perf_probe()
'''


# ────────────────────────────────── 工具函数 ──────────────────────────────────

def detect_running_instance() -> int | None:
    """找已有的 Drifox 源码版主进程（cmdline 含 <PROJECT_ROOT>/main.py），排除自身。

    R1 后仅作运行提示（WARN/[i] 打印），不再参与隔离决策——打包版实例
    cmdline 不含 main.py 检测不到；隔离由 resolve_data_dir 默认强制。
    """
    me = os.getpid()
    root_s = str(PROJECT_ROOT)
    for p in psutil.process_iter(["pid", "cmdline"]):
        if p.info["pid"] == me:
            continue
        cl = p.info["cmdline"] or []
        if not cl:
            continue
        joined = " ".join(cl)
        if "main.py" in joined and root_s in joined and "perf_baseline" not in joined:
            return p.info["pid"]
    return None


def resolve_data_dir(isolate: bool | None) -> tuple[Path, bool]:
    """返回 (data_dir, isolated)。R1：默认一律隔离，仅显式 --no-isolate 用真实目录。

    自动检测只认源码版 cmdline，打包版识别不到会让测试实例与真实实例
    并发写 .drifox（静默失效），故不再依赖检测结果做隔离决策。
    """
    env_dir = os.environ.get("DRIFOX_DATA_DIR")
    if env_dir:  # 外部已显式指定，尊重之
        return Path(env_dir), True
    if isolate is False:  # 显式 --no-isolate：真实目录（自担并发写风险）
        return PROJECT_ROOT / ".drifox", False
    d = OUT_DIR / "data_isolated"
    d.mkdir(parents=True, exist_ok=True)
    return d, True


def tail_log(path: Path, offset: int) -> tuple[str, int]:
    """增量读日志，返回 (新增文本, 新 offset)。文件不存在视为空。

    Y1: size < offset 视为 rotation（all.log 10MB 轮转后新文件更小），
    offset 置 0 重读整份新文件，避免锚点永久丢失导致整轮假 FAIL。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return "", offset
    if size == offset:
        return "", offset
    if size < offset:
        offset = 0  # rotation：重读整份新文件
    with open(path, encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        text = f.read()
    return text, size


def tree_summary(root_pid: int) -> dict:
    """进程树 RSS 分类汇总（main/webengine/python_child/other），口径同 mem_baseline.py。"""
    try:
        rows = snapshot(psutil.Process(root_pid))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {}
    agg = {"main": 0.0, "webengine": 0.0, "python_child": 0.0, "other": 0.0}
    procs = []
    for r in rows:
        kind = classify(r["name"], r["cmdline"], r["is_root"])
        agg[kind] = agg.get(kind, 0.0) + r["rss_mb"]
        procs.append({"pid": r["pid"], "name": r["name"],
                      "rss_mb": round(r["rss_mb"], 1), "kind": kind})
    tree = sum(agg.values())
    return {"tree_rss_mb": round(tree, 1), "n_procs": len(rows),
            "main_rss_mb": round(agg["main"], 1), "webengine_rss_mb": round(agg["webengine"], 1),
            "python_child_rss_mb": round(agg["python_child"], 1), "other_rss_mb": round(agg["other"], 1),
            "procs": procs}


def kill_tree(proc: psutil.Process, grace: float = 5.0) -> None:
    """先子后父杀整棵进程树：SIGTERM 宽限后残留 SIGKILL。"""
    try:
        children = proc.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for c in children:
        try:
            c.terminate()
        except psutil.NoSuchProcess:
            pass
    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return
    _, alive = psutil.wait_procs([proc, *children], timeout=grace)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


def wait_tree_gone(proc: psutil.Process, timeout: float = 8.0) -> None:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        try:
            kids = proc.children(recursive=True)
            if not kids and not proc.is_running():
                return
            for k in kids:
                try:
                    k.kill()
                except psutil.NoSuchProcess:
                    pass
        except psutil.NoSuchProcess:
            return
        time.sleep(0.2)


def agg_times(xs: list[float]) -> dict:
    xs = sorted(xs)
    n = len(xs)
    return {"min": round(xs[0], 3), "median": round(xs[n // 2], 3),
            "mean": round(sum(xs) / n, 3), "max": round(xs[-1], 3), "n": n}


# ────────────────────────────────── 启动测量 ──────────────────────────────────

def run_startup_round(idx: int, data_dir: Path, isolated: bool, stable_s: float,
                      n_samples: int, sample_interval: float, trace: bool,
                      timeout_s: float) -> dict:
    """一轮：冷启动 → 解析 StartupMark → 稳态采样 → 杀树。"""
    log_path = data_dir / "logs" / "all.log"
    try:
        log_offset = log_path.stat().st_size if log_path.exists() else 0
    except OSError:
        log_offset = 0

    env = os.environ.copy()
    env["DRIFOX_DATA_DIR"] = str(data_dir)
    env.pop("QT_PLUGIN_PATH", None)
    probe_dir = None
    if trace:
        probe_dir = OUT_DIR / "_perf_probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        (probe_dir / "sitecustomize.py").write_text(PROBE_SOURCE, encoding="utf-8")
        env["PYTHONPATH"] = str(probe_dir) + os.pathsep + env.get("PYTHONPATH", "")
        env["DRIFOX_PERF"] = "1"
        env["DRIFOX_PERF_STABLE_S"] = str(stable_s)
        env["DRIFOX_PERF_OUT"] = str(OUT_DIR / f"perf_trace_top_round{idx}.json")

    marks: dict[str, int] = {}          # label -> 分段耗时 ms（打点值）
    arrival: dict[str, float] = {}      # label -> 相对 t_spawn 到达时刻 s（轮询近似）
    first_webengine_at: float | None = None

    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "main.py")],
        cwd=str(PROJECT_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ps_proc = psutil.Process(proc.pid)
    deadline = t0 + timeout_s
    got_first_window = False

    while time.perf_counter() < deadline:
        text, log_offset = tail_log(log_path, log_offset)
        for m in MARK_RE.finditer(text):
            label, ms = m.group(1), int(m.group(2))
            if label not in marks:
                marks[label] = ms
                arrival[label] = round(time.perf_counter() - t0, 3)
                if label == "first_chat_window":
                    got_first_window = True
        if first_webengine_at is None:
            try:
                for c in ps_proc.children(recursive=False):
                    if classify(c.name(), "", False) == "webengine":
                        first_webengine_at = round(time.perf_counter() - t0, 3)
                        break
            except psutil.NoSuchProcess:
                pass
        if got_first_window:
            break
        if proc.poll() is not None:  # 进程提前退出
            break
        time.sleep(POLL_INTERVAL)

    spawn_to_shell = arrival.get("shell_visible_after_logging")
    spawn_to_first = arrival.get("first_chat_window")

    # 稳态内存采样：首窗就绪（或超时）后再等 stable 秒
    time.sleep(stable_s)
    samples = []
    for _ in range(n_samples):
        s = tree_summary(proc.pid)
        if s:
            samples.append(s)
        time.sleep(sample_interval)

    # trace 模式：等探针写完 tracemalloc Top 再杀
    trace_top = None
    if trace:
        out_f = Path(env["DRIFOX_PERF_OUT"])
        t_wait = time.perf_counter()
        while time.perf_counter() - t_wait < 90:
            if out_f.exists():
                try:
                    trace_top = json.loads(out_f.read_text(encoding="utf-8"))
                except Exception:
                    pass
                break
            time.sleep(1.0)

    exit_code = proc.poll()
    if exit_code is None:
        kill_tree(ps_proc)
        wait_tree_gone(ps_proc)
    else:
        try:
            kill_tree(ps_proc)
        except psutil.NoSuchProcess:
            pass

    return {
        "round": idx,
        "isolated_data_dir": isolated,
        "spawn_to_shell_visible_s": spawn_to_shell,
        "spawn_to_first_window_s": spawn_to_first,
        "first_webengine_process_s": first_webengine_at,
        "stages_ms": marks,
        "stable_memory": {
            "samples": samples,
            "tree_rss_agg": agg_times([s["tree_rss_mb"] for s in samples]) if samples else None,
            "last": samples[-1] if samples else None,
        },
        "tracemalloc_top": trace_top,
        "exit_code": exit_code,
        "success": got_first_window,
    }


def print_round(r: dict) -> None:
    tag = f"round {r['round']}{'（隔离数据目录）' if r['isolated_data_dir'] else ''}"
    print(f"\n=== 启动耗时（{tag}）===")
    if not r["success"]:
        print(f"[FAIL] 未见 first_chat_window 打点（exit_code={r['exit_code']}）")
    if r["spawn_to_shell_visible_s"] is not None:
        print(f"t_spawn → 壳可见   : {r['spawn_to_shell_visible_s']:.2f}s")
    if r["spawn_to_first_window_s"] is not None:
        print(f"t_spawn → 首窗就绪 : {r['spawn_to_first_window_s']:.2f}s（可交互近似）")
    if r["first_webengine_process_s"] is not None:
        print(f"首个 WebEngine 子进程: {r['first_webengine_process_s']:.2f}s")
    if r["stages_ms"]:
        print("分段(ms): " + " | ".join(f"{k}={v}" for k, v in r["stages_ms"].items()))
    mem = r["stable_memory"]
    if mem["tree_rss_agg"]:
        a = mem["tree_rss_agg"]
        last = mem["last"]
        print(f"\n=== 稳态内存（{a['n']} 采样，间隔 2s）===")
        print(f"进程树 RSS: median {a['median']} MB (min {a['min']} / max {a['max']})")
        print(f"main {last['main_rss_mb']} | webengine {last['webengine_rss_mb']}"
              f" | python_child {last['python_child_rss_mb']} | other {last['other_rss_mb']} MB"
              f"，共 {last['n_procs']} 进程")
    top = r.get("tracemalloc_top")
    if top:
        print("\n=== tracemalloc Top（trace 模式，启动被拖慢仅供参考）===")
        for row in top["tracemalloc_top"][:10]:
            print(f"  {row['size_mb']:>8.2f} MB  {row['count']:>7}  {row['site']}")


# ────────────────────────────────── main ──────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="DriFox 性能基线一键测量")
    ap.add_argument("--rounds", type=int, default=1, help="冷启动轮数（默认 1）")
    ap.add_argument("--stable-wait", type=float, default=15.0, help="稳态等待秒（默认 15）")
    ap.add_argument("--samples", type=int, default=5, help="稳态采样次数（默认 5）")
    ap.add_argument("--sample-interval", type=float, default=2.0, help="采样间隔秒（默认 2）")
    ap.add_argument("--timeout", type=float, default=90.0, help="单轮启动超时秒（默认 90）")
    ap.add_argument("--trace", action="store_true", help="注入 tracemalloc 探针（DRIFOX_PERF=1 门控）")
    ap.add_argument("--mem-only", action="store_true", help="只测已运行实例内存（--pid 必填）")
    ap.add_argument("--pid", type=int, default=None, help="--mem-only 的目标进程")
    ap.add_argument("--no-isolate", action="store_true", help="强制用真实 .drifox（须先关闭已有实例）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="JSON 输出路径")
    args = ap.parse_args()

    if psutil is None:
        print("[FAIL] 需要 psutil（uv sync 即装，pyproject 核心依赖）")
        return 2

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result: dict = {"meta": {
        "timestamp": now, "python": sys.version.split()[0],
        "platform": sys.platform, "cpu_count": os.cpu_count(),
        "stable_wait_s": args.stable_wait, "samples": args.samples,
        "trace_mode": args.trace,
    }, "rounds": []}

    # 模式 1：只测已运行实例
    if args.mem_only:
        if not args.pid:
            print("[FAIL] --mem-only 需要 --pid")
            return 2
        snap = tree_summary(args.pid)
        print(f"=== 内存快照 pid={args.pid} ===")
        if not snap:
            print("[FAIL] 进程不存在或无权限")
            return 1
        for p in sorted(snap["procs"], key=lambda x: -x["rss_mb"]):
            print(f"  {p['pid']:>7} {p['name'][:40]:<42} {p['rss_mb']:>9.1f} MB  {p['kind']}")
        print(f"汇总: main {snap['main_rss_mb']} | webengine {snap['webengine_rss_mb']}"
              f" | python_child {snap['python_child_rss_mb']} | other {snap['other_rss_mb']}"
              f" = tree {snap['tree_rss_mb']} MB（{snap['n_procs']} 进程）")
        result["mem_only_snapshot"] = snap
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON -> {args.out}")
        return 0

    # 模式 2：冷启动全流程
    running = detect_running_instance()
    isolate = False if args.no_isolate else None
    data_dir, isolated = resolve_data_dir(isolate)
    if running is not None and not isolated:
        print(f"[WARN] 检测到已有实例 pid={running} 且未隔离，共享 .drifox 会互相污染")
    if running is not None and isolated:
        print(f"[i] 已有实例在跑（pid={running}），使用隔离数据目录: {data_dir}")
    print(f"[i] data_dir={data_dir}  rounds={args.rounds}  trace={args.trace}")

    for i in range(1, args.rounds + 1):
        print(f"\n--- 启动第 {i}/{args.rounds} 轮（第 1 轮含初始化为冷启动，后续为热启动）---")
        r = run_startup_round(i, data_dir, isolated, args.stable_wait,
                              args.samples, args.sample_interval, args.trace, args.timeout)
        result["rounds"].append(r)
        print_round(r)
        time.sleep(1.0)

    # 跨轮聚合（同目录下 round1=冷，round2+=热）
    ok = [r for r in result["rounds"] if r["success"]]
    if ok:
        summary: dict = {}
        cold = ok[0]
        if cold["spawn_to_first_window_s"] is not None:
            summary["cold_spawn_to_first_window_s"] = cold["spawn_to_first_window_s"]
        hot = ok[1:]
        if hot:
            vals = [r["spawn_to_first_window_s"] for r in hot if r["spawn_to_first_window_s"]]
            if vals:
                summary["hot_spawn_to_first_window_s"] = agg_times(vals)
        mem_vals = [r["stable_memory"]["tree_rss_agg"]["median"] for r in ok
                    if r["stable_memory"]["tree_rss_agg"]]
        if mem_vals:
            summary["stable_tree_rss_mb_per_round"] = mem_vals
        result["summary"] = summary
        print("\n=== 基线汇总 ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON -> {out_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
