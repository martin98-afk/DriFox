# -*- coding: utf-8 -*-
"""冒烟档 2h 起跑 / 归档一键脚本（SMK-PRE 序列的自动化实现）。

用法::

    # 演练：只做检查并打印将执行的命令，不起任何进程
    python scripts/smoke_run.py --duration 7200 --dry-run
    # 正式跑（必须无其他 DriFox 实例存活；否则拒绝起跑）
    python scripts/smoke_run.py --duration 7200
    # 允许与已存活实例并行（数据需标注降级，判据②③会受宿主竞争污染）
    python scripts/smoke_run.py --duration 7200 --allow-shared

序列：独占检查 → 清档（保留 baseline_index/最新基线/trace）→ env 装配 →
起应用 + run-meta → mem_baseline 采集 → 场景 B 交织 → 到点停采 → 归档
smoke_<commit8>_<ts>/ → 跑 smoke_verdict 判读。

注意：本脚本自身起 QApplication 之外的进程，必须在 DriFox 全关的桌面会话执行，
否则 WebEngine profile 与 CPU 竞争会让判据②③失真。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
OUT_DIR = PROJECT_ROOT / ".perf_out"
PRE_ARCHIVE = OUT_DIR / "_archive_pre_smoke"
SMOKE_ISO = OUT_DIR / "smoke_iso"
LONG_BASELINES = PROJECT_ROOT / "tests" / "perf" / "long_run" / "baselines"
SMOKE_ISO_B = OUT_DIR / "smoke_iso_b"

# SMK-PRE env 一行式（口径以 O2 实装为准：bare_ratio 并入 DRIFOX_EL_METRICS）
APP_ENV = {
    "DRIFOX_WATCHDOG": "1",
    "DRIFOX_WATCHDOG_JSONL": "1",
    "DRIFOX_WATCHDOG_THRESHOLD_MS": "500",
    "DRIFOX_EL_METRICS": "1",
    "DRIFOX_HANDLE_TRACE": "1",
    "DRIFOX_CPU_TRACE": "1",
    "DRIFOX_RENDER_PROBE": "1",
    "DRIFOX_GC_TRACE": "1",
    "DRIFOX_GC_METRICS": "1",  # O7 未合并时无人读取，设置无害
    "DRIFOX_WEBVIEW_POOL_IDLE_GC": "1",
    "DRIFOX_DATA_DIR": str(SMOKE_ISO),
}
# 场景 B runner 会 import app：必须剔除进程内埋点门控，
# 否则第二个进程也往 .perf_out/trace_events.jsonl 写 gdi/el 行，污染判据②⑤
_RUNNER_DROP = (
    "DRIFOX_WATCHDOG_JSONL",
    "DRIFOX_EL_METRICS",
    "DRIFOX_HANDLE_TRACE",
    "DRIFOX_GC_TRACE",
    "DRIFOX_CPU_TRACE",
    "DRIFOX_RENDER_PROBE",
)


def _log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def _git_commit() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _my_chain() -> set:
    """自身父进程链（避免把本脚本的宿主误判为「其他实例」）。"""
    try:
        import psutil

        chain, p = set(), psutil.Process(os.getpid())
        while p is not None and len(chain) < 12:
            chain.add(p.pid)
            p = p.parent()
        return chain
    except Exception:
        return {os.getpid()}


def _drifox_instances() -> list:
    """扫描存活的 DriFox 主进程（只排除本脚本自身）。

    注意不排除父链：若本脚本在 DriFox 内部运行，宿主实例同样是竞争源，
    必须如实报出（否则独占检查形同虚设）。
    """
    try:
        import psutil
    except ImportError:
        return []
    hits = []
    for q in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = q.info["pid"]
            if pid == os.getpid():
                continue
            cmd = " ".join(q.info.get("cmdline") or [])
            if "main.py" in cmd and "drifox" in cmd.lower().replace("\\", "/"):
                hits.append((pid, q.info.get("name"), cmd[:120]))
        except Exception:
            continue
    return hits


def _archive_pre(tag: str) -> Path:
    """清档：历史 trace/mem/long_run 移入 _archive_pre_smoke/<tag>/。

    baseline_index.jsonl 与最新 perf_baseline_*.json 保留（O3 台账要用）。
    """
    dst = PRE_ARCHIVE / tag
    dst.mkdir(parents=True, exist_ok=True)
    moved = []
    for pat in ("trace_events.jsonl", "mem_smoke*.csv", "smoke_run_meta.json"):
        for f in OUT_DIR.glob(pat):
            shutil.move(str(f), dst / f.name)
            moved.append(f.name)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"清档：{len(moved)} 个历史产物 -> {dst.name}")
    return dst


def _wait_first_window(timeout_s: float = 180.0) -> bool:
    """轮询隔离目录日志等待首窗就绪（失败不阻断，只记录）。"""
    log_path = SMOKE_ISO / "logs" / "all.log"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if log_path.exists():
                text = log_path.read_text(encoding="utf-8", errors="replace")
                if "first_chat_window" in text:
                    return True
        except OSError:
            pass
        time.sleep(2.0)
    return False


def _kill_tree(proc: "subprocess.Popen") -> None:
    try:
        import psutil

        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except Exception:
                pass
        parent.terminate()
        parent.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _record_host_load(path: Path, pids: list) -> None:
    """追加宿主进程负载（cpu/rss）：把冒烟延迟毛刺归因到宿主竞争用。"""
    if not pids:
        return
    try:
        import psutil
    except ImportError:
        return
    new_file = not path.exists()
    try:
        with path.open("a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(["ts_iso", "pid", "cpu_pct", "rss_mb"])
            for pid in pids:
                try:
                    p = psutil.Process(pid)
                    w.writerow([
                        datetime.now().isoformat(timespec="seconds"), pid,
                        p.cpu_percent(None), round(p.memory_info().rss / 1048576, 1),
                    ])
                except Exception:
                    continue
    except OSError:
        pass


def _archive_run(tag: str, run_meta: dict, started_at: float) -> Path:
    """归档 smoke_<commit8>_<ts>/：trace / mem / long_run_b / meta / crash / logs。"""
    dst = OUT_DIR / f"smoke_{tag}"
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    (dst / "logs").mkdir(parents=True, exist_ok=True)
    (dst / "crash_snapshot").mkdir(exist_ok=True)

    trace = OUT_DIR / "trace_events.jsonl"
    if trace.exists():
        shutil.copy2(trace, dst / "trace_events.jsonl")
    for f in OUT_DIR.glob("mem_smoke*.csv"):
        shutil.copy2(f, dst / f.name)
    host_csv = OUT_DIR / "host_load.csv"
    if host_csv.exists():
        shutil.copy2(host_csv, dst / host_csv.name)
    for f in LONG_BASELINES.glob("long_run_b_*.csv"):
        if f.stat().st_mtime >= started_at - 5:
            shutil.copy2(f, dst / f.name)
    (dst / "smoke_run_meta.json").write_text(
        json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for f in (SMOKE_ISO / "logs").glob("*"):
        if f.is_file() and f.suffix in (".log", ".txt"):
            shutil.copy2(f, dst / "logs" / f.name)
    crash_src = SMOKE_ISO / "logs" / "crash"
    if crash_src.is_dir():
        for f in crash_src.glob("*"):
            if f.is_file() and f.suffix == ".log":
                shutil.copy2(f, dst / "crash_snapshot" / f.name)
    return dst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="冒烟档 2h 起跑/归档一键脚本")
    ap.add_argument("--duration", type=float, default=7200.0, help="冒烟时长秒（默认 7200）")
    ap.add_argument("--allow-shared", action="store_true", help="允许与已存活 DriFox 实例并行（数据降级）")
    ap.add_argument("--dry-run", action="store_true", help="只做检查并打印命令，不起进程")
    ap.add_argument("--collect-interval", type=float, default=60.0, help="mem_baseline 采样间隔秒")
    args = ap.parse_args(argv)

    tag_ts = datetime.now().strftime("%Y%m%d_%H%M")
    commit = _git_commit()
    duration = args.duration

    live = _drifox_instances()
    chain = _my_chain()
    hosted_by = [p for p, _n, _c in live if p in chain]
    _log(f"HEAD={commit} duration={duration:.0f}s 存活 DriFox 实例={live or '无'}"
         + (f"（宿主={hosted_by}）" if hosted_by else ""))
    if live and not args.allow_shared and not args.dry_run:
        _log("拒绝起跑：存在其他 DriFox 实例（WebEngine profile 与 CPU 竞争会污染判据②③）。"
             "先关闭它们，或显式 --allow-shared 接受降级数据。")
        return 3
    if live and args.dry_run:
        _log("[WARN] 存在存活实例；正式跑需加 --allow-shared 并接受判据②③降级")

    pre_dir = PRE_ARCHIVE / f"pre_{tag_ts}"
    smoke_py = str(PROJECT_ROOT / "main.py")
    mem_py = str(PROJECT_ROOT / "scripts" / "mem_baseline.py")
    app_cmd = [sys.executable, smoke_py]
    mem_cmd = [sys.executable, mem_py, "--pid", "<app_pid>", "--out", str(OUT_DIR / "mem_smoke.csv"),
               "--interval", str(int(args.collect_interval)), "--duration", str(int(duration) + 60)]
    b_cmd = [sys.executable, "-m", "tests.perf.long_run.runner", "--scenario", "b",
             "--duration", str(int(duration))]

    if args.dry_run:
        _log(f"清档目标：{pre_dir}")
        _log("APP env: " + " ".join(f"{k}={v}" for k, v in APP_ENV.items()))
        _log("APP cmd: " + " ".join(app_cmd))
        _log("MEM cmd: " + " ".join(mem_cmd))
        _log("B   cmd: " + " ".join(b_cmd))
        _log(f"expected_rows: mem_smoke={int(duration / args.collect_interval) + 1}, "
             f"long_run_b={int(duration / 60) + 1}")
        _log("dry-run 结束（未起任何进程）")
        return 0

    pre_dir = _archive_pre(tag_ts)

    # [SERDE-FIX] 清 __pycache__：防 pyc 与源码行号错位（曾实测 +1 错位误导崩溃归因）
    import shutil as _sh

    for pkg_dir in (ROOT / "app", ROOT / "tools"):
        for cache in pkg_dir.rglob("__pycache__"):
            _sh.rmtree(cache, ignore_errors=True)
    _log("已清理 app/ tools/ __pycache__")

    app_env = dict(os.environ, **APP_ENV)
    runner_env = dict(os.environ, **APP_ENV)
    for k in _RUNNER_DROP:
        runner_env.pop(k, None)
    # 场景 B 与主应用分库：同一 SQLite 数据目录会锁争用（会话切换要读写会话库）
    runner_env["DRIFOX_DATA_DIR"] = str(SMOKE_ISO_B)

    started_at = time.time()
    run_meta = {
        "commit": commit,
        "started_at": datetime.fromtimestamp(started_at).isoformat(timespec="seconds"),
        "duration_s": duration,
        "collect_interval_s": args.collect_interval,
        "app_env": APP_ENV,
        "host_instances": [{"pid": p, "name": n, "cmd": c} for p, n, c in live],
        "hosted_by": hosted_by,
        "shared_mode": bool(live),
        "shared_mode_note": (
            "非独占环境：宿主实例常驻（CPU/RSS 曲线见 host_load.csv），判据②③含宿主竞争噪声，"
            "本结果不用于发布门禁，门禁级数据以独占复测为准"
        ) if live else "独占环境",
        "pre_archive": str(pre_dir),
        "expected_rows": {
            "mem_smoke": int(duration / args.collect_interval) + 1,
            "long_run_b": int(duration / 60) + 1,
        },
    }

    _log("起主应用（SC1 挂机主体）...")
    app = subprocess.Popen(app_cmd, cwd=str(PROJECT_ROOT), env=app_env)
    run_meta["app_pid"] = app.pid
    ready = _wait_first_window()
    _log(f"首窗就绪={ready} pid={app.pid}")

    _log("起 mem_baseline 采集器 ...")
    mem_cmd[mem_cmd.index("<app_pid>")] = str(app.pid)
    mem = subprocess.Popen(mem_cmd, cwd=str(PROJECT_ROOT), env=os.environ.copy())

    _log("起场景 B runner ...")
    runner = subprocess.Popen(b_cmd, cwd=str(PROJECT_ROOT), env=runner_env)

    host_csv = OUT_DIR / "host_load.csv"
    deadline = time.time() + duration + 900
    timed_out = False
    try:
        while runner.poll() is None and time.time() < deadline:
            time.sleep(30)
            _record_host_load(host_csv, [p for p, _n, _c in live])
        if runner.poll() is None:
            timed_out = True
            _log("场景 B 超时未结束，强制终止")
            _kill_tree(runner)
        else:
            _log(f"场景 B 结束 rc={runner.returncode}，等最后一拍采样")
            time.sleep(30)
    finally:
        _kill_tree(mem)
        _kill_tree(app)
    run_meta["runner_timeout"] = timed_out

    run_meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
    run_meta["app_exit_code"] = app.returncode
    run_meta["runner_exit_code"] = runner.returncode
    dst = _archive_run(f"{commit}_{tag_ts}", run_meta, started_at)
    _log(f"归档完成：{dst}")

    _log("跑 smoke_verdict ...")
    verdict = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "smoke_verdict.py"), "--archive-dir", str(dst)],
        cwd=str(PROJECT_ROOT),
    )
    return verdict.returncode


if __name__ == "__main__":
    raise SystemExit(main())
