# -*- coding: utf-8 -*-
"""冷/热启动归因对照实验（一次性诊断用，不改任何产品代码）。

目的：perf_baseline.py 曾出现「热启动比冷启动慢近一倍」的反常数字。本脚本
隔离三个可能的污染源，逐个给出数据：

  A. 解释器层：uv shim（Scripts/python.exe，会再 spawn 真 CPython）
     vs 真解释器（sys.base_prefix/python.exe + sitecustomize 垫片）
  B. 并发争抢：每轮前轮询确认上一轮进程树完全消失（不留 pyc/WebEngine 残留）
  C. pyc 红利 vs 冷写代价：--pyc-off 用 -B 禁止写 pyc，--pyc-cold 先清 __pycache__

输出：每轮 spawn→首窗就绪、壳可见、以及分段大户，落 .perf_out/attribution.json

运行：
  .venv\\Scripts\\python.exe benchmarks/bench_coldhot_attribution.py --runs 3
  .venv\\Scripts\\python.exe benchmarks/bench_coldhot_attribution.py --runs 2 --pyc-off
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(BENCH_DIR))

import bench_common as bc  # noqa: E402

OUT_DIR = PROJECT_ROOT / ".perf_out"
MARK_RE = re.compile(r"\[StartupMark\]\s+(.+?)\s+耗时\s+(\d+)ms")

BIG_SEGMENTS = (
    "app_services_start",
    "tab_manager_complete",
    "reload_agents",
    "import_main_widget",
    "import_services",
    "plugin_scan",
    "first_chat_window",
    "early_forensics",
    "preheat_process",
    "shell_visible_after_logging",
)


def _wait_clean(timeout_s: float = 20.0) -> bool:
    """等所有 main.py 实例（除用户长期在跑的那两个）退干净。"""
    import psutil

    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout_s:
        busy = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            cl = " ".join(p.info["cmdline"] or [])
            if "main.py" in cl and str(PROJECT_ROOT) in cl:
                busy.append(p.info["pid"])
        if not busy:
            return True
        time.sleep(0.3)
    return False


def _run_once(label: str, use_shim: bool, pyc_off: bool, timeout_s: int = 180) -> dict:
    """起一次 main.py，解析 [StartupMark] 与到达时刻。"""
    env = dict(os.environ)
    env["DRIFOX_DATA_DIR"] = str(OUT_DIR / "data_attribution")
    if use_shim:
        exe = sys.executable
        env.pop("PYTHONPATH", None)
    else:
        exe = bc.real_python()
        env = bc.spawn_env({"DRIFOX_DATA_DIR": str(OUT_DIR / "data_attribution")})
    if pyc_off:
        env["PYTHONDONTWRITEBYTECODE"] = "1"

    log_path = Path(env["DRIFOX_DATA_DIR"]) / "logs" / "all.log"
    start_off = log_path.stat().st_size if log_path.exists() else 0

    _wait_clean()
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [exe, str(PROJECT_ROOT / "main.py")],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    marks: dict[str, int] = {}
    arrival: dict[str, float] = {}
    deadline = t0 + timeout_s
    got = False
    while time.perf_counter() < deadline:
        try:
            if log_path.exists():
                with open(log_path, "rb") as fh:
                    fh.seek(start_off)
                    text = fh.read().decode("utf-8", "replace")
                for m in MARK_RE.finditer(text):
                    lbl, ms = m.group(1), int(m.group(2))
                    if lbl not in marks:
                        marks[lbl] = ms
                        arrival[lbl] = round(time.perf_counter() - t0, 3)
                        if lbl == "first_chat_window":
                            got = True
        except Exception:
            pass
        if got or proc.poll() is not None:
            break
        time.sleep(0.05)

    # 杀净（等进程树消失，避免下一轮争抢）
    import psutil

    kill_start = time.perf_counter()
    try:
        root = psutil.Process(proc.pid)
        for c in root.children(recursive=True):
            try:
                c.kill()
            except Exception:
                pass
        root.kill()
    except Exception:
        pass
    _wait_clean()
    kill_s = round(time.perf_counter() - kill_start, 2)

    return {
        "label": label,
        "exe_kind": "uv-shim" if use_shim else "real-python",
        "exe": exe,
        "pyc_off": pyc_off,
        "spawn_to_shell_s": arrival.get("shell_visible_after_logging"),
        "spawn_to_first_window_s": arrival.get("first_chat_window"),
        "got_first_window": got,
        "segments_ms": {k: marks.get(k) for k in BIG_SEGMENTS if k in marks},
        "cleanup_s": kill_s,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="每种配置跑几次")
    ap.add_argument("--pyc-off", action="store_true", help="禁止写 pyc（-B / PYTHONDONTWRITEBYTECODE）")
    ap.add_argument("--out", default=str(OUT_DIR / "attribution.json"))
    args = ap.parse_args()

    bc.warn_if_drifox_running("attribution")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rounds = []
    for i in range(args.runs):
        rounds.append(_run_once(f"shim-r{i + 1}", use_shim=True, pyc_off=args.pyc_off))
        rounds.append(_run_once(f"real-r{i + 1}", use_shim=False, pyc_off=args.pyc_off))

    def _agg(kind: str) -> dict:
        vals = [r["spawn_to_first_window_s"] for r in rounds if r["exe_kind"] == kind and r["spawn_to_first_window_s"]]
        if not vals:
            return {}
        s = sorted(vals)
        return {
            "n": len(s),
            "min_s": s[0],
            "median_s": s[len(s) // 2],
            "max_s": s[-1],
            "raw": vals,
        }

    summary = {
        "uv_shim": _agg("uv-shim"),
        "real_python": _agg("real-python"),
        "rounds": rounds,
        "env": bc.env_info(),
    }
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 冷/热归因对照 =====")
    print(f"{'配置':<14}{'n':>4}{'min':>10}{'median':>10}{'max':>10}")
    for kind, st in (("uv shim", summary["uv_shim"]), ("real python", summary["real_python"])):
        if st:
            print(f"{kind:<14}{st['n']:>4}{st['min_s']:>10.2f}{st['median_s']:>10.2f}{st['max_s']:>10.2f}")
    print("\n分段（ms，各轮）:")
    for r in rounds:
        segs = " ".join(f"{k}={v}" for k, v in (r["segments_ms"] or {}).items())
        print(f"  {r['label']:<12} first={r['spawn_to_first_window_s']} shell={r['spawn_to_shell_s']} {segs}")
    print(f"\nJSON -> {args.out}")


if __name__ == "__main__":
    main()
