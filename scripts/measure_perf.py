#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""measure_perf.py — 轻量性能回归测量骨架（子任务 #4.3）。

测量 startup / animation / upload / memory 四类场景，输出 JSON 结果，
可与基线 reports/perf-baseline-before.json 对比，超阈值红色标记。

设计原则（铁律：不修改任何产品代码、不引入新三方依赖）：
- 仅做测量，不改产品代码；可 import 产品模块做测量，但不改动。
- 仅用标准库 + 已在依赖中的 psutil / PySide6 / requests。
- 可在当前 dev 分支无产品代码改动下跑通（headless 安全）：
  * memory   : tracemalloc + psutil 测代表性负载的净分配（无需 GUI）
  * startup  : 子进程冷导入 openai（benchmarks 显示其占导入耗时 34%，启动导入瓶颈代理）
  * animation: PySide6.QColor 渐变插值（值类型，无需 QApplication）
  * upload   : 本地 loopback http.server + requests 往返（无外网依赖）

说明：当前 scenario 测量器为「代表性代理负载」，用于打通框架与基线对比链路；
真实产品负载（驱动真实 GUI / 真实上传 / 真实渲染）待 #1/#2/#3 诊断后由 build 在此骨架内替换。
每条结果带 method / note 字段标明测量语义，diff 时不会误读为真实产品指标。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT = os.path.join(PROJECT_ROOT, "reports", "perf-measure-{scenario}.json")
DEFAULT_BASELINE = os.path.join(PROJECT_ROOT, "reports", "perf-baseline-before.json")

SCENARIOS = ("startup", "animation", "upload", "memory")


# ─────────────────────────────── 测量器 ───────────────────────────────

def _env_block() -> dict:
    try:
        import psutil
        mem_total_gb = round(psutil.virtual_memory().total / 1e9, 1)
    except Exception:
        mem_total_gb = None
    return {
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpu": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "mem_total_gb": mem_total_gb,
        "cwd": PROJECT_ROOT,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def measure_memory(iterations: int) -> tuple[list[dict], str, str]:
    """tracemalloc + psutil 测量代表性负载（构建 2000 条卡片级 dict 结构）的净分配。"""
    import gc
    import psutil
    import tracemalloc

    proc = psutil.Process(os.getpid())
    rows: list[dict] = []
    for _ in range(iterations):
        gc.collect()
        base_rss = proc.memory_info().rss / 1e6
        if not tracemalloc.is_tracing():
            tracemalloc.start(25)
        data = []
        for i in range(2000):
            data.append({
                "id": i,
                "role": "assistant",
                "content": "x" * 512,
                "meta": {"elapsed": 1.2, "tokens": 100},
            })
        cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        gc.collect()
        after_rss = proc.memory_info().rss / 1e6
        rows.append({
            "tracemalloc_current_mb": cur / 1e6,
            "tracemalloc_peak_mb": peak / 1e6,
            "rss_base_mb": base_rss,
            "rss_after_mb": after_rss,
            "rss_delta_mb": after_rss - base_rss,
        })
        del data
    method = "tracemalloc + psutil：构建 2000 条卡片级 dict 结构，追踪 Python 堆分配与 RSS 增量"
    note = "代理负载：代表单会话多卡片的内存构造成本；真实场景待 #1/#2/#3 接入真实渲染管线"
    return rows, method, note


def measure_startup(iterations: int) -> tuple[list[dict], str, str]:
    """子进程冷导入 openai（benchmarks 显示 openai 占导入总耗时 34%，为启动导入瓶颈代理）。"""
    rows: list[dict] = []
    for _ in range(iterations):
        code = "import time; t=time.perf_counter(); import openai; print('%.4f' % (time.perf_counter()-t))"
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        try:
            secs = float(r.stdout.strip().splitlines()[-1])
        except Exception:
            secs = -1.0
        rows.append({"cold_import_openai_s": secs})
    method = "子进程冷导入 openai（perf_counter 计时），每个 iteration 独立进程"
    note = "代理负载：openai 为导入耗时 Top1 包（34%）；真实启动需驱动 main.py + QApplication，待 #4 后接入"
    return rows, method, note


def measure_animation(iterations: int) -> tuple[list[dict], str, str]:
    """PySide6.QColor 渐变插值（值类型，无需 QApplication）；失败则退化为纯 Python 插值。"""
    frames = 600
    try:
        from PySide6.QtGui import QColor
        use_qt = True
    except Exception:
        use_qt = False

    rows: list[dict] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        if use_qt:
            c1 = QColor(20, 20, 30)
            c2 = QColor(240, 240, 255)
            for _f in range(frames):
                _ = [QColor(
                    int(c1.red() + (c2.red() - c1.red()) * i / 10),
                    int(c1.green() + (c2.green() - c1.green()) * i / 10),
                    int(c1.blue() + (c2.blue() - c1.blue()) * i / 10),
                ) for i in range(11)]
        else:
            c1 = (20, 20, 30)
            c2 = (240, 240, 255)
            for _f in range(frames):
                _ = [(
                    int(c1[0] + (c2[0] - c1[0]) * i / 10),
                    int(c1[1] + (c2[1] - c1[1]) * i / 10),
                    int(c1[2] + (c2[2] - c1[2]) * i / 10),
                ) for i in range(11)]
        dt_ms = (time.perf_counter() - t0) * 1000
        rows.append({"gradient_build_ms_per_600frames": dt_ms})
    method = ("PySide6.QColor 渐变插值（无需 QApplication）" if use_qt
              else "纯 Python 颜色插值（PySide6 不可用时的退化路径）")
    note = "代理负载：代表消息卡片每帧渐变构建成本（Top① 动画高频绘制）；真实 paintEvent 待 #1 接入"
    return rows, method, note


def measure_upload(iterations: int) -> tuple[list[dict], str, str]:
    """本地 loopback http.server + requests 64KB 往返，无外网依赖。"""
    import http.server
    import socketserver
    import threading

    import requests

    handler = http.server.SimpleHTTPRequestHandler
    rows: list[dict] = []
    payload = "x" * (1024 * 64)
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        for _ in range(iterations):
            t0 = time.perf_counter()
            try:
                requests.post(f"http://127.0.0.1:{port}/", data=payload, timeout=10)
            except Exception:
                pass
            rows.append({"upload_roundtrip_ms": (time.perf_counter() - t0) * 1000})
        httpd.shutdown()
    method = "本地 loopback HTTP POST 64KB 往返（requests，无外网）"
    note = "代理负载：代表上传网络栈延迟；真实分享上传路径（后台线程 + requests.post timeout=30）待 #3 接入"
    return rows, method, note


MEASURERS = {
    "memory": measure_memory,
    "startup": measure_startup,
    "animation": measure_animation,
    "upload": measure_upload,
}


# ─────────────────────────────── 聚合 / 对比 ───────────────────────────────

def aggregate(rows: list[dict]) -> dict:
    metrics: dict[str, dict] = {}
    for key in rows[0].keys():
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        if not vals:
            continue
        metrics[key] = {
            "min": round(min(vals), 4),
            "median": round(statistics.median(vals), 4),
            "mean": round(statistics.fmean(vals), 4),
            "max": round(max(vals), 4),
        }
    return metrics


def load_baseline(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _baseline_metrics(baseline: dict, scenario: str) -> dict | None:
    # 兼容两种基线格式：单场景结果（含 "metrics"）或多场景聚合（{scenario: {metrics/...}}）
    if isinstance(baseline, dict):
        if "metrics" in baseline and baseline.get("scenario") == scenario:
            return baseline["metrics"]
        if scenario in baseline and isinstance(baseline[scenario], dict):
            return baseline[scenario].get("metrics", baseline[scenario])
    return None


def compare(current: dict, baseline: dict | None, threshold: float) -> list[dict]:
    if not baseline:
        return []
    base_metrics = _baseline_metrics(baseline, current["scenario"])
    if not base_metrics:
        return []
    regressions: list[dict] = []
    for key, agg in current["metrics"].items():
        base = base_metrics.get(key)
        if not isinstance(base, dict):
            continue
        base_val = base.get("median", base.get("mean"))
        cur_val = agg.get("median", agg.get("mean"))
        if base_val in (None, 0) or cur_val is None:
            continue
        if cur_val > base_val * (1 + threshold):
            regressions.append({
                "metric": key,
                "baseline": round(base_val, 4),
                "current": round(cur_val, 4),
                "ratio": round(cur_val / base_val, 3) if base_val else None,
            })
    return regressions


# ─────────────────────────────── 入口 ───────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DriFox 性能回归测量骨架")
    ap.add_argument("--scenario", required=True, choices=SCENARIOS,
                    help="测量场景：startup / animation / upload / memory")
    ap.add_argument("--iterations", type=int, default=3, help="迭代次数（默认 3）")
    ap.add_argument("--output", default=DEFAULT_OUTPUT, help="结果 JSON 输出路径（{scenario} 占位）")
    ap.add_argument("--baseline", default=DEFAULT_BASELINE, help="基线 JSON 路径（用于对比）")
    ap.add_argument("--threshold", type=float, default=0.10,
                    help="回归阈值：相对基线的百分比，默认 0.10（即超过 10 个百分点标红）")
    args = ap.parse_args(argv)

    if args.iterations < 1:
        ap.error("--iterations 必须 >= 1")

    rows, method, note = MEASURERS[args.scenario](args.iterations)
    metrics = aggregate(rows)
    baseline = load_baseline(args.baseline)
    regressions = compare({"scenario": args.scenario, "metrics": metrics}, baseline, args.threshold)

    result = {
        "scenario": args.scenario,
        "iterations": args.iterations,
        "runs": args.iterations,
        "metrics": metrics,
        "method": method,
        "note": note,
        "baseline_file": args.baseline if baseline else None,
        "threshold": args.threshold,
        "regressions": regressions,
        "pass": len(regressions) == 0,
        "_env": _env_block(),
    }

    out_path = args.output.format(scenario=args.scenario)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    red = "\033[31m"
    reset = "\033[0m"
    print(f"[measure_perf] scenario={args.scenario} iterations={args.iterations}")
    print(f"[measure_perf] output={out_path}")
    for key, agg in metrics.items():
        print(f"  {key}: median={agg['median']} mean={agg['mean']} "
              f"min={agg['min']} max={agg['max']}")
    if regressions:
        print(f"{red}[REGRESSION] {len(regressions)} 项超阈值（>{args.threshold*100:.0f}%）：{reset}")
        for r in regressions:
            print(f"{red}  - {r['metric']}: baseline={r['baseline']} current={r['current']} "
                  f"ratio={r['ratio']}{reset}")
    else:
        print(f"[OK] 无回归（阈值 {args.threshold*100:.0f}%）" + ("" if baseline else "（无基线可比）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
