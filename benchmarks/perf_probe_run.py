#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perf_probe_run.py — 带性能探针启动 DriFox 主程序的运行入口。

用途
----
一条命令拉起真实 DriFox，同时打开 ``app.core.infra.telemetry.perf_probe``：
启动分段、主窗构造、后台内存曲线全部落 JSONL，退出时打一份聚合摘要。

硬约束（与 perf_probe 模块一致）
--------------------------------
- **默认完全关闭**：不传 ``--enable`` 时本脚本只做一件事——告诉你怎么开，
  然后原样 exec 主程序（探针 env 未设置，业务侧零副作用）。
- **打包版强制不可用**：``sys.frozen`` / ``_MEIPASS`` 为真时直接拒绝运行
  （探针在打包产物里 enabled() 恒 False，本脚本也不接受打包版调用）。
- **不接管业务**：只设环境变量 + 在主程序退出后读 JSONL 汇总，
  不 import 主程序、不 patch 任何函数。

用法::

    # 只看开关帮助（默认行为，不启动应用）
    uv run python benchmarks/perf_probe_run.py

    # 带探针启动真实应用（内存采样 2s 一次）
    uv run python benchmarks/perf_probe_run.py --enable --mem

    # 指定落点与采样间隔，跑完自动汇总
    uv run python benchmarks/perf_probe_run.py --enable --mem --mem-ms 1000 \
        --out .perf_out/probe_baseline.jsonl

    # 只汇总一份已有 JSONL（不启动应用）
    uv run python benchmarks/perf_probe_run.py --summarize .perf_out/probe_baseline.jsonl

产物格式（JSONL，每行一条）::

    {"kind":"span","name":"startup.tab_manager","ms":1234.5,"tags":{},"ts":...}
    {"kind":"count","name":"card.paint","n":1,"tags":{},"ts":...}
    {"kind":"gauge","name":"pool.size","value":3,"tags":{},"ts":...}
    {"kind":"mem","name":"auto","rss_mb":412.3,"ts":...}
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ".perf_out/perf_probe.jsonl"


def _packaged() -> bool:
    return bool(getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"))


def summarize(path: Path) -> dict:
    """读 JSONL 汇总：span 统计 + count 累加 + mem 曲线斜率。"""
    spans: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    gauges: dict[str, list[float]] = {}
    mem: list[tuple[float, float]] = []
    bad = 0

    if not path.exists():
        print(f"[probe] 未找到 {path}", file=sys.stderr)
        return {}

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                bad += 1
                continue
            kind = rec.get("kind")
            if kind == "span":
                spans.setdefault(str(rec.get("name")), []).append(float(rec.get("ms") or 0))
            elif kind == "count":
                n = str(rec.get("name"))
                counts[n] = counts.get(n, 0) + int(rec.get("n") or 0)
            elif kind == "gauge":
                gauges.setdefault(str(rec.get("name")), []).append(float(rec.get("value") or 0))
            elif kind == "mem":
                if rec.get("rss_mb") is not None:
                    mem.append((float(rec.get("ts") or 0), float(rec["rss_mb"])))

    out: dict = {"file": str(path), "lines_bad": bad}

    if spans:
        table = {}
        for name, vals in spans.items():
            s = sorted(vals)
            table[name] = {
                "n": len(s),
                "min_ms": round(s[0], 2),
                "median_ms": round(statistics.median(s), 2),
                "max_ms": round(s[-1], 2),
                "sum_ms": round(sum(s), 2),
            }
        out["spans"] = table
        print(f"\n===== SPANS ({len(table)}) =====")
        print(f"{'name':<44}{'n':>6}{'median':>10}{'min':>10}{'max':>10}")
        for name, st in sorted(table.items(), key=lambda kv: -kv[1]["sum_ms"]):
            print(
                f"{name:<44}{st['n']:>6}{st['median_ms']:>10.2f}"
                f"{st['min_ms']:>10.2f}{st['max_ms']:>10.2f}"
            )

    if counts:
        out["counts"] = counts
        print("\n===== COUNTS =====")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{name:<44}{n:>10}")

    if gauges:
        out["gauges"] = {
            k: {"n": len(v), "min": round(min(v), 2), "max": round(max(v), 2), "last": round(v[-1], 2)}
            for k, v in gauges.items()
        }

    if len(mem) >= 2:
        t0 = mem[0][0]
        xs = [t - t0 for t, _ in mem]
        ys = [mb for _, mb in mem]
        out["mem"] = {
            "samples": len(mem),
            "rss_first_mb": round(ys[0], 1),
            "rss_last_mb": round(ys[-1], 1),
            "rss_min_mb": round(min(ys), 1),
            "rss_max_mb": round(max(ys), 1),
            "rss_peak_delta_mb": round(max(ys) - min(ys), 1),
            "span_s": round(xs[-1], 1),
        }
        print("\n===== MEM =====")
        print(json.dumps(out["mem"], ensure_ascii=False, indent=2))

    if bad:
        print(f"\n[probe] 跳过 {bad} 行不可解析记录", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="带性能探针启动 DriFox（默认关闭，只打印用法）")
    ap.add_argument("--enable", action="store_true", help="真正启用探针并启动应用")
    ap.add_argument("--mem", action="store_true", help="启用后台 RSS 采样线程")
    ap.add_argument("--mem-ms", type=int, default=2000, help="RSS 采样间隔毫秒（默认 2000）")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"JSONL 落点（默认 {DEFAULT_OUT}）")
    ap.add_argument("--summarize", default="", help="只汇总指定 JSONL，不启动应用")
    ap.add_argument("--sum-out", default="", help="把汇总 JSON 写到该路径")
    args = ap.parse_args()

    if args.summarize:
        data = summarize(Path(args.summarize))
        if args.sum_out and data:
            Path(args.sum_out).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[probe] 汇总已写入 {args.sum_out}")
        return 0

    if not args.enable:
        print(
            "性能探针默认关闭（零副作用）。启用方式：\n"
            f"  uv run python benchmarks/perf_probe_run.py --enable --mem --out {DEFAULT_OUT}\n"
            "或直接设环境变量后照常启动 main.py：\n"
            "  DRIFOX_PERF_PROBE=1 DRIFOX_PERF_PROBE_MEM=1 uv run python main.py\n"
            "汇总一份已有产物：\n"
            "  uv run python benchmarks/perf_probe_run.py --summarize <jsonl>\n"
            "打包版不可用：探针在 sys.frozen 下 enabled() 恒 False。"
        )
        return 0

    if _packaged():
        print("[probe] 拒绝运行：这是打包产物，诊断设施不允许随发布版流出。", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env["DRIFOX_PERF_PROBE"] = "1"
    env["DRIFOX_PERF_PROBE_OUT"] = str(Path(args.out).absolute())
    if args.mem:
        env["DRIFOX_PERF_PROBE_MEM"] = "1"
        env["DRIFOX_PERF_PROBE_MEM_MS"] = str(max(200, args.mem_ms))

    print(f"[probe] 启用：DRIFOX_PERF_PROBE=1 out={env['DRIFOX_PERF_PROBE_OUT']} mem={bool(args.mem)}")
    rc = subprocess.call([sys.executable, str(PROJECT_ROOT / "main.py")], cwd=str(PROJECT_ROOT), env=env)
    print(f"[probe] 应用退出 rc={rc}，汇总产物…")
    data = summarize(Path(env["DRIFOX_PERF_PROBE_OUT"]))
    if args.sum_out and data:
        Path(args.sum_out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[probe] 汇总已写入 {args.sum_out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
