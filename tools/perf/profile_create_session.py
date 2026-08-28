# -*- coding: utf-8 -*-
"""
子任务 #3-base：创建团队 `backend.create_session()` 主线程热点 profiling（改动前基准）

复现路径：backend.create_session() → trigger_event("SessionStart", trigger_async=True)
（UI 线程真实路径，4 窗创建团队时每个窗背靠背触发）

- 用真实 plugins/system/hooks/hooks.json 注册 SessionStart hook（#team_member 匹配组）。
- 用 cProfile 包裹 trigger_event，输出热点表（主线程火焰）。
- 逐 hook 包裹计时，区分「UI 主线程同步执行」vs「worker 线程池执行 + 主线程 QEventLoop 等待」。
- 三种模式：
    baseline      : 仅 shipped 内置 hook，量化框架基准耗时（sync vs pool-wait 占比）。
    slow_python   : 追加 python add_output=True 注入型慢 hook(默认 8.7s)，证明耗时计入 backend_create。
    slow_command  : 追加 command 异步慢 hook(默认 8.7s)，证明 command 永远后台、不计入 backend_create。

仅做测量，不修改任何生产文件（shipped hooks.json 全含 id，register 不会回写；临时 hook 注册到临时文件）。

用法:
    python tools/perf/profile_create_session.py --mode baseline
    python tools/perf/profile_create_session.py --mode slow_python
    python tools/perf/profile_create_session.py --mode slow_command
"""
import argparse
import cProfile
import io
import json
import pstats
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---------- 0. 环境准备 ----------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent.parent  # D:/work/DriFox
PERF_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PERF_DIR))  # 让 slow_hooks 可被标准 import

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

from app.core.hook_manager import HookManager  # noqa: E402

# 屏蔽重依赖：内置 python hook inject_team_context 需要 app.core.team_manager。
# 必须在 app/app.core 作为真实包导入之后，再仅替换子模块（惰性导入，hook_manager 顶层不触发）。
import types
_fake_tm = types.ModuleType("app.core.team_manager")
class _TM:  # noqa: N801
    @classmethod
    def get_instance(cls):
        return cls()
    def get_template(self):
        return None
    def get_members(self):
        return []
    def get_team_run_ids(self):
        return []
    def get_team_label_by_run(self, rid):
        return rid
_fake_tm.TeamManager = _TM
sys.modules["app.core.team_manager"] = _fake_tm

SYS_HOOKS = ROOT / "plugins/system/hooks/hooks.json"
MAIN_TID = threading.current_thread().ident
RECORDS = []


def _wrap_execute():
    """包裹 _execute_hook，记录每 hook 类型/耗时/是否主线程。"""
    orig = HookManager._execute_hook

    def wrapped(self, hook, context, trigger_async=True):
        is_main = (threading.current_thread().ident == MAIN_TID)
        t0 = time.perf_counter()
        res = orig(self, hook, context, trigger_async)
        dt = (time.perf_counter() - t0) * 1000.0
        RECORDS.append({
            "id": hook.id,
            "type": hook.type,
            "ms": round(dt, 3),
            "main_thread": is_main,
            "add_output": hook.add_output_to_context,
        })
        return res

    HookManager._execute_hook = wrapped


def build_context():
    return {
        "project_root": str(ROOT),
        "state": "startup",
        "project_name": ROOT.name,
        "is_team_member": True,   # 命中 #team_member matcher
        "window_id": "win_perf",
        "message": "",
        "event_name": "SessionStart",
        "timestamp": time.time(),
    }


def register_slow(mode: str):
    """把慢 hook 注册到临时文件（回写仅落在临时文件，不影响生产）。"""
    secs = float(os.environ.get("PERF_SLOW_SEC", "8.7"))
    # 让子进程 / worker 线程都能拿到睡眠时长（os.environ 在同进程跨线程共享；子进程继承）
    os.environ["PERF_SLOW_SEC"] = str(secs)
    import slow_hooks  # noqa: WPS433
    slow_hooks.set_sec(secs)

    if mode == "slow_python":
        cfg = {"hooks": {"SessionStart": [{
            "matcher": "#team_member",
            "hooks": [{
                "id": "perf_blocking_py", "type": "python",
                "function": ".slow_hooks:blocking_sleep",
                "add_output_to_context": True,
                "statusMessage": "慢python注入:", "timeout": 300,
            }],
        }]}}
    elif mode == "slow_command":
        # 写临时 .py 避免 `python -c "..."` 在 Windows cmd + shell=True 下的引号嵌套问题
        tmppy = PERF_DIR / f"_perf_slow_cmd_{int(time.time()*1000)}.py"
        tmppy.write_text(
            "import os, sys, time\n"
            f"time.sleep(float(os.environ.get('PERF_SLOW_SEC', '{secs}')))\n"
            "sys.stdout.write('slow-cmd-done\\n')\n",
            encoding="utf-8",
        )
        cmd = f'"{sys.executable}" "{tmppy}"'
        cfg = {"hooks": {"SessionStart": [{
            "matcher": "#team_member",
            "hooks": [{
                "id": "perf_cmd", "type": "command", "command": cmd,
                "add_output_to_context": True,
                "statusMessage": "慢command:", "timeout": 300,
            }],
        }]}}
    else:
        return None
    tf = tempfile.NamedTemporaryFile(
        "w", suffix=".json", dir=str(PERF_DIR), delete=False, encoding="utf-8"
    )
    json.dump(cfg, tf, ensure_ascii=False)
    tf.close()
    hm = HookManager()
    hm.register_hooks_from_json("perf-demo", str(PERF_DIR), cfg, tf.name)
    return hm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "slow_python", "slow_command", "all"],
                    default="baseline")
    ap.add_argument("--iters", type=int, default=5)
    args = ap.parse_args()

    _wrap_execute()
    hm = HookManager()
    # 注册真实 shipped SessionStart hook（#team_member 组）
    hm.register_hooks_from_json(
        "system", str(SYS_HOOKS.parent),
        json.loads(SYS_HOOKS.read_text(encoding="utf-8")), str(SYS_HOOKS),
    )
    if args.mode in ("slow_python", "slow_command"):
        register_slow(args.mode)
    elif args.mode == "all":
        # 真实注册 system + 全部用户插件 hooks.json 里的 SessionStart，实测各自真实耗时
        import glob as _glob
        for hf in [str(SYS_HOOKS)] + _glob.glob(str(ROOT / ".drifox" / "plugins" / "*" / "hooks" / "hooks.json")):
            try:
                cfg = json.loads(Path(hf).read_text(encoding="utf-8"))
                hm.register_hooks_from_json(Path(hf).parent.name, str(Path(hf).parent), cfg, hf)
                print(f"[reg] {hf}", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"[skip] {hf}: {e}", file=sys.stderr, flush=True)

    ctx = build_context()
    if args.mode == "all":
        print("=== SessionStart registered rules (after all registration) ===", file=sys.stderr, flush=True)
        for ri, rule in enumerate(hm._hooks.get("SessionStart", [])):
            for h in rule.hooks:
                print(f"  rule#{ri} matcher={rule.matcher!r} id={h.id} type={h.type} "
                      f"enabled={h.enabled} add_output={h.add_output_to_context}", file=sys.stderr, flush=True)
    iters = 1 if args.mode != "baseline" else args.iters

    walls = []
    for i in range(iters):
        RECORDS.clear()
        t0 = time.perf_counter()
        pr = cProfile.Profile()
        pr.enable()
        results = hm.trigger_event("SessionStart", ctx, trigger_async=True)
        pr.disable()
        wall = (time.perf_counter() - t0) * 1000.0
        walls.append(wall)
        # cProfile 仅取最后一次（慢模式只跑 1 次）
        last_pr = pr
        if args.mode == "baseline" and i < iters - 1:
            continue

    # ---- 汇总 ----
    wall_avg = sum(walls) / len(walls)
    # 主线程同步执行耗时（PROMPT 等直接在主线程跑的 hook）
    sync_ui = sum(r["ms"] for r in RECORDS if r["main_thread"])
    # 主线程被 QEventLoop 阻塞等待 worker hook 的耗时 = 总墙钟 - 主线程同步
    pool_wait = max(0.0, wall_avg - sync_ui)

    # 逐 hook 聚合（同名多跑取末次；baseline 多次取均值）
    per_hook = {}
    for r in RECORDS:
        k = (r["id"], r["type"])
        per_hook.setdefault(k, []).append(r["ms"])
    per_hook_list = [
        {"id": k[0], "type": k[1],
         "ms": round(sum(v) / len(v), 3), "main_thread": (k[0] in {r["id"] for r in RECORDS if r["main_thread"]}),
         "add_output": True}
        for k, v in per_hook.items()
    ]

    # cProfile 热点（主线程火焰）
    sio = io.StringIO()
    ps = pstats.Stats(last_pr, stream=sio)
    ps.sort_stats("cumulative")
    ps.print_stats(15)
    profile_text = sio.getvalue()

    summary = {
        "mode": args.mode,
        "wall_ms_avg": round(wall_avg, 3),
        "sync_ui_ms": round(sync_ui, 3),
        "pool_wait_ms": round(pool_wait, 3),
        "sync_ratio_pct": round(100.0 * sync_ui / wall_avg, 1) if wall_avg else 0.0,
        "pool_ratio_pct": round(100.0 * pool_wait / wall_avg, 1) if wall_avg else 0.0,
        "per_hook": per_hook_list,
        "n_results": len(results),
        "results_detail": [
            {"success": getattr(r, "success", None),
             "output": (getattr(r, "output", "") or "")[:200],
             "status_message": getattr(r, "status_message", "")}
            for r in results
        ],
    }
    print("=== PERF SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("=== cProfile (main thread, cumulative top 15) ===")
    print(profile_text)


if __name__ == "__main__":
    main()
