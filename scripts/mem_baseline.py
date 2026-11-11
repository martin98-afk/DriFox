# -*- coding: utf-8 -*-
"""外部内存基线采样器：对目标进程整棵树周期采样 RSS，输出时间序列 CSV（T1 内存基线）。

用法
----
# 监控 pid 1234 的进程树，1s 采样，写 out.csv（+ out_detail.csv 明细）
python scripts/mem_baseline.py --pid 1234 --out .perf_out/mem_a.csv --interval 1.0

# 阶段标记：采样期间每轮读取 <out目录>/stage.txt 内容作为 stage 列
# 外部驱动改 stage.txt 即可在时间线上标记场景分段

# 单次快照（不循环）：--once
python scripts/mem_baseline.py --pid 1234 --once

CSV 汇总列：ts_iso, elapsed_s, stage, tree_rss_mb, tree_count,
            main_rss_mb, webengine_mb, python_mb, other_mb
CSV 明细列：ts_iso, elapsed_s, stage, pid, ppid, name, rss_mb, kind
"""

from __future__ import annotations

import argparse
import csv
import datetime
import os
import time

import psutil

WEBENGINE_HINTS = ("qtwebengineprocess", "chrome", "msedge")
PYTHON_HINTS = ("python", "pythonw")


def classify(name: str, cmdline: str, is_root: bool) -> str:
    low_n = name.lower()
    low_c = cmdline.lower()
    if is_root:
        return "main"
    if any(h in low_n for h in WEBENGINE_HINTS):
        return "webengine"
    if low_n.startswith(PYTHON_HINTS):
        # conhost 包装的临时脚本也算 python 子进程
        return "python_child"
    return "other"


def snapshot(root: psutil.Process) -> list[dict]:
    """返回整棵树的进程行（含 root），进程消失则跳过。"""
    rows = []
    stack = [(root, 0)]
    seen = set()
    while stack:
        proc, depth = stack.pop()
        pid = proc.pid
        if pid in seen:
            continue
        seen.add(pid)
        try:
            mi = proc.memory_info()
            name = proc.name()
            try:
                cmdline = " ".join(proc.cmdline())
            except Exception:
                cmdline = ""
            rows.append(
                {
                    "pid": pid,
                    "ppid": proc.ppid(),
                    "name": name,
                    "cmdline": cmdline,
                    "rss_mb": mi.rss / 1048576.0,
                    "is_root": depth == 0,
                }
            )
            for c in proc.children(recursive=False):
                stack.append((c, depth + 1))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return rows


def write_csv(
    out_path: str,
    root_pid: int,
    interval: float,
    stage_file: str,
    duration: float | None,
    detail_path: str | None,
) -> None:
    stage_file = stage_file or os.path.splitext(out_path)[0] + "_stage.txt"
    detail_path = detail_path or os.path.splitext(out_path)[0] + "_detail.csv"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    root = psutil.Process(root_pid)
    t0 = time.perf_counter()
    last_stage = None

    with (
        open(out_path, "w", newline="", encoding="utf-8") as fsum,
        open(detail_path, "w", newline="", encoding="utf-8") as fdet,
    ):
        wsum = csv.writer(fsum)
        wdet = csv.writer(fdet)
        wsum.writerow(
            [
                "ts_iso",
                "elapsed_s",
                "stage",
                "tree_rss_mb",
                "tree_count",
                "main_rss_mb",
                "webengine_mb",
                "python_mb",
                "other_mb",
            ]
        )
        wdet.writerow(["ts_iso", "elapsed_s", "stage", "pid", "ppid", "name", "rss_mb", "kind"])

        while True:
            now = time.perf_counter()
            elapsed = now - t0
            if duration is not None and elapsed >= duration:
                break
            try:
                stage = open(stage_file, encoding="utf-8").read().strip() or "unknown"
            except OSError:
                stage = "unknown"
            if stage != last_stage:
                last_stage = stage

            rows = snapshot(root)
            ts_iso = datetime.datetime.now().isoformat(timespec="seconds")
            agg = {"main": 0.0, "webengine": 0.0, "python_child": 0.0, "other": 0.0}
            for r in rows:
                kind = classify(r["name"], r["cmdline"], r["is_root"])
                agg[kind] = agg.get(kind, 0.0) + r["rss_mb"]
                wdet.writerow(
                    [
                        ts_iso,
                        f"{elapsed:.1f}",
                        stage,
                        r["pid"],
                        r["ppid"],
                        r["name"],
                        f"{r['rss_mb']:.1f}",
                        kind,
                    ]
                )
            tree = sum(agg.values())
            wsum.writerow(
                [
                    ts_iso,
                    f"{elapsed:.1f}",
                    stage,
                    f"{tree:.1f}",
                    len(rows),
                    f"{agg['main']:.1f}",
                    f"{agg['webengine']:.1f}",
                    f"{agg['python_child']:.1f}",
                    f"{agg['other']:.1f}",
                ]
            )
            fsum.flush()
            fdet.flush()

            sleep_for = interval - (time.perf_counter() - now)
            if sleep_for > 0:
                time.sleep(sleep_for)


def main() -> None:
    ap = argparse.ArgumentParser(description="DriFox 外部内存基线采样器（进程树 RSS → CSV）")
    ap.add_argument("--pid", type=int, required=True, help="目标根进程 pid")
    ap.add_argument("--out", default=".perf_out/mem_baseline.csv", help="汇总 CSV 路径")
    ap.add_argument("--interval", type=float, default=1.0, help="采样间隔秒，默认 1.0")
    ap.add_argument("--duration", type=float, default=None, help="总时长秒；缺省一直采到 Ctrl+C")
    ap.add_argument("--stage-file", default=None, help="阶段标记文件，缺省 <out>_stage.txt")
    ap.add_argument("--detail", default=None, help="明细 CSV 路径，缺省 <out>_detail.csv")
    ap.add_argument("--once", action="store_true", help="只快照一次打印到 stdout")
    args = ap.parse_args()

    if args.once:
        rows = snapshot(psutil.Process(args.pid))
        agg: dict[str, float] = {}
        for r in rows:
            k = classify(r["name"], r["cmdline"], r["is_root"])
            agg[k] = agg.get(k, 0.0) + r["rss_mb"]
        for r in sorted(rows, key=lambda x: -x["rss_mb"]):
            print(f"{r['pid']:>7} {r['name'][:40]:<42} {r['rss_mb']:>9.1f} MB")
        print("汇总:", {k: round(v, 1) for k, v in agg.items()},
              "tree=", round(sum(agg.values()), 1), "MB", "n=", len(rows))
        return

    try:
        write_csv(args.out, args.pid, args.interval, args.stage_file, args.duration, args.detail)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
