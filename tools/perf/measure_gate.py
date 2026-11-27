#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""measure_gate.py — 测量环境门禁（DriFox 性能测量的前置检查）。

为什么需要它：CPU 敏感指标（启动 ms、渲染帧 ms）在 load 高 / 多实例并发时
噪声可以超过被测目标本身（实测 load 8.48 时 41.9ms 的优化目标噪声 >100ms）。
本工具只做判定与报告，**不 kill 任何进程**。

用法：
  .venv/bin/python tools/perf/measure_gate.py check
  .venv/bin/python tools/perf/measure_gate.py check --report
  .venv/bin/python tools/perf/measure_gate.py check --wait 120
  # 自测三种判定（CI/演示用，不读真实环境）
  .venv/bin/python tools/perf/measure_gate.py check --override "load=30,cores=12,instances=1,idle=5"

判定（退出码即结论）：
  0 GO       适合测量
  1 CAUTION  可测但噪声偏大：重复次数 ≥5，报告中标注环境
  2 STOP     不适合测量：等待或清理后重跑

阈值依据：
  - load > 核数 × 1.5 → STOP。CPU 密集测量的排队延迟在 load 超过核数后
    非线性上升，1.5 倍时单次 wall-clock 抖动已可超过 20%。
  - main.py 实例 > 2 → STOP。多实例争抢 WindowServer / WebEngine profile /
    SQLite 锁，启动与渲染数据互相污染（实测 6 实例时 load 涨到 8.48）。
  - load > 核数 × 0.8、实例 == 2、CPU idle < 40% → CAUTION。
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

GO, CAUTION, STOP = 0, 1, 2
# override 未与 --allow-override 成对出现时的拒绝码：独立于 STOP(2)，
# 让调用方能区分「环境脏」与「用法违规」。
OVERRIDE_REFUSED = 3

_LEVEL_NAME = {GO: "GO", CAUTION: "CAUTION", STOP: "STOP"}

# 命令行里出现这些前缀的是测量/检查工具自身，不参与实例计数
_SELF_HINTS = ("measure_gate", "pgrep", "grep ", "ps -", "pytest")


def _snapshot() -> list[dict] | None:
    """一次 ps 全表快照：pid / cpu% / etime / command。

    失败返回 None（不是 []）——空列表会被判定成「0 个实例」从而误判 GO，
    一个拿不到进程表的门禁必须说「不知道」，不能给绿灯。
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,pcpu=,etime=,command="],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, cpu = int(parts[0]), float(parts[1])
        except ValueError:
            continue
        rows.append({"pid": pid, "cpu": cpu, "etime": parts[2], "command": parts[3]})
    # ps 成功但输出为空（被 wrapper 吞掉 / 极简环境）：同样不能当成「0 个实例」，
    # docstring 已经写明这条纪律，这里补上实际判据。
    return rows or None


def _cpu_idle_pct() -> float | None:
    """CPU 空闲率（top 两次采样取第二次，避免首帧统计偏差）。"""
    try:
        out = subprocess.run(
            ["top", "-l", "2", "-n", "0", "-s", "1"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        hits = re.findall(r"CPU usage: ([\d.]+)% user, ([\d.]+)% sys, ([\d.]+)% idle", out)
        if hits:
            return float(hits[-1][2])
    except Exception:
        pass
    return None


def _etime_seconds(text: str) -> int:
    """ps 的 etime（[[dd-]hh:]mm:ss）转秒；解析失败返回 0。"""
    try:
        days = 0
        rest = text
        if "-" in text:
            d, rest = text.split("-", 1)
            days = int(d)
        parts = [int(x) for x in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, sec = parts[-3], parts[-2], parts[-1]
        return days * 86400 + h * 3600 + m * 60 + sec
    except Exception:
        return 0


# 长时运行（>1h）的源码实例按「用户自己的开发实例」处理：开发者开着跑一天
# 很常见，把它算成污染源会让门禁在自己机器上永远 STOP。
_DEV_INSTANCE_USER_AFTER_S = 3600


def classify(rows: list[dict], self_pid: int, assume_user_pids: set[int] | None = None) -> dict:
    """把进程快照分成：main.py 测试实例 / 用户自己的 Drifox / renderer / 其他吃 CPU 的。"""
    instances: list[dict] = []
    user_apps: list[dict] = []
    renderers = 0
    assume_user = assume_user_pids or set()
    for row in rows:
        cmd = row["command"]
        if row["pid"] == self_pid or any(h in cmd for h in _SELF_HINTS):
            continue
        if row["pid"] in assume_user:
            user_apps.append({**row, "assumed_user": True})
            continue
        # shell 包装行（/bin/sh -c "..."）与它的 python 子进程会重复计数，只认子进程
        if cmd.startswith("/bin/sh -c") or cmd.startswith("sh -c"):
            continue
        if "QtWebEngineProcess" in cmd:
            renderers += 1
            continue
        if "/Applications/Drifox.app/" in cmd:
            user_apps.append(row)
            continue
        if _etime_seconds(row["etime"]) >= _DEV_INSTANCE_USER_AFTER_S:
            # 源码运行但活了 1 小时以上：按用户自己的开发实例处理（不阻断测量）
            user_apps.append({**row, "long_running": True})
            continue
        # 测量用实例：main.py 直跑，或队友的 /tmp/run_app_*.py 宿主
        if re.search(r"(^|[\s/])main\.py(\s|$)", cmd) or "/tmp/run_app_" in cmd:
            instances.append(row)
    return {
        "test_instances": instances,
        "user_apps": user_apps,
        "renderers": renderers,
    }


def evaluate(load: float, cores: int, instances: int, idle: float | None) -> tuple[int, list[str]]:
    """纯函数判定：返回 (level, reasons)。STOP 优先于 CAUTION。"""
    reasons: list[str] = []
    level = GO
    if cores and load > cores * 1.5:
        level = STOP
        reasons.append(f"load {load:.2f} > 核数×1.5 ({cores * 1.5:.1f})")
    if instances > 2:
        level = STOP
        reasons.append(f"main.py 测试实例 {instances} > 2")
    if level != STOP:
        if cores and load > cores * 0.5:
            # 0.8 太宽：实测 load 8.48（12 核的 0.71 倍）时启动抖动已 >100ms
            level = max(level, CAUTION)
            reasons.append(f"load {load:.2f} > 核数×0.5 ({cores * 0.5:.1f})")
        if instances == 2:
            level = max(level, CAUTION)
            reasons.append("main.py 测试实例 = 2")
        if idle is not None and idle < 40:
            level = max(level, CAUTION)
            reasons.append(f"CPU idle {idle:.1f}% < 40%")
    return level, reasons


def collect(override: dict | None = None, assume_user_pids: set[int] | None = None) -> dict:
    """采一次环境快照并判定。override 仅用于自测/CI。"""
    self_pid = os.getpid()
    if override:
        data = {
            "load_avg": float(override.get("load", 0.0)),
            "cpu_cores": int(override.get("cores", os.cpu_count() or 1)),
            "main_py_instances": int(override.get("instances", 0)),
            "cpu_idle_pct": float(override["idle"]) if "idle" in override else None,
            "test_instances": [],
            "user_apps": [],
            "renderers": 0,
            "override": True,
        }
    else:
        rows = _snapshot()
        if rows is None:
            data = {
                "load_avg": round(os.getloadavg()[0], 2),
                "cpu_cores": os.cpu_count() or 1,
                "main_py_instances": None,
                "cpu_idle_pct": None,
                "test_instances": [],
                "user_apps": [],
                "renderers": 0,
                "override": False,
                "ps_failed": True,
            }
            data.update({
                "level": "STOP",
                "exit_code": STOP,
                "reasons": ["ps 进程表读取失败：无法确认实例数，按 STOP 处理"],
                "verdict": "环境不可判定（拿不到进程表）",
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            })
            return data
        parts = classify(rows, self_pid, assume_user_pids)
        data = {
            "load_avg": round(os.getloadavg()[0], 2),
            "cpu_cores": os.cpu_count() or 1,
            "main_py_instances": len(parts["test_instances"]),
            "cpu_idle_pct": _cpu_idle_pct(),
            "test_instances": [
                {"pid": r["pid"], "cpu_pct": r["cpu"], "etime": r["etime"],
                 "command": r["command"][:160]}
                for r in sorted(parts["test_instances"], key=lambda x: -x["cpu"])
            ],
            "user_apps": [
                {"pid": r["pid"], "cpu_pct": r["cpu"], "etime": r["etime"],
                 "command": r["command"][:160]}
                for r in parts["user_apps"]
            ],
            "renderers": parts["renderers"],
            "override": False,
        }
    level, reasons = evaluate(
        data["load_avg"], data["cpu_cores"], data["main_py_instances"], data["cpu_idle_pct"]
    )
    if override:
        # 伪造 GO 是不可接受的：override 只用于自测渲染判定分支，
        # 结果永远至少 CAUTION，退出码不会是 0，调用方必须读 JSON。
        level = max(level, CAUTION)
    data.update(
        {
            "level": _LEVEL_NAME[level],
            "exit_code": level,
            "reasons": ([f"override 生效（非真实环境：{override}），结果不可作为判定依据"] + reasons)
        if override
        else reasons,
            "verdict": {
                GO: "适合测量",
                CAUTION: "可测但噪声偏大：重复 ≥5 次并在报告标注环境",
                STOP: "不适合测量（等待或清理后重跑）",
            }[level],
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return data


def _print_human(d: dict) -> None:
    print(f"checked_at: {d['checked_at']}")
    print(f"load_avg: {d['load_avg']}  (核数 {d['cpu_cores']}) → 判定 {d['level']}")
    print(f"main_py_instances: {d['main_py_instances']} → 判定 {d['level']}")
    print(f"cpu_idle_pct: {d['cpu_idle_pct'] if d['cpu_idle_pct'] is not None else 'n/a'}")
    print(f"QtWebEngineProcess: {d['renderers']} 个")
    if d["test_instances"]:
        print("疑似可清理的测量实例（仅列出，不 kill）：")
        for r in d["test_instances"]:
            print(f"  pid={r['pid']:<7} cpu={r['cpu_pct']:>5.1f}%  age={r['etime']:<8} {r['command']}")
    if d["user_apps"]:
        print("用户自己的 Drifox 实例（勿动）：")
        for r in d["user_apps"]:
            print(f"  pid={r['pid']:<7} cpu={r['cpu_pct']:>5.1f}%  age={r['etime']:<8}")
    if d["reasons"]:
        print("触发项: " + "；".join(d["reasons"]))
    print(f"结论：{d['verdict']}")
    print(f"退出码：{d['exit_code']}")


def _parse_override(text: str) -> dict:
    out = {}
    for part in text.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="DriFox 测量环境门禁（不 kill 任何进程）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="检查当前环境是否适合测量")
    p.add_argument("--report", nargs="?", const="-", default=None,
                   help="输出 JSON（不带路径→stdout；带路径→写文件）")
    p.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                   help="轮询直到 GO，最多等待 N 秒（超时返回 2）")
    p.add_argument("--interval", type=int, default=5, help="--wait 的轮询间隔秒数")
    p.add_argument("--override", default=None, metavar="K=V,...",
                   help="自测用：直接指定 load/cores/instances/idle，不读真实环境"
                        "（非 tty 环境需同时给 --allow-override）")
    p.add_argument("--allow-override", action="store_true",
                   help="允许在非 tty 环境使用 --override（结果会标记不可作为判定依据）")
    p.add_argument("--assume-user-pid", default="", metavar="PID[,PID...]",
                   help="把这些 pid 视为用户实例（不阻断测量）")
    args = ap.parse_args(argv)

    if args.cmd != "check":
        return 2

    user_pids = {
        int(x) for x in str(args.assume_user_pid or "").replace(" ", "").split(",") if x.isdigit()
    }
    override = _parse_override(args.override) if args.override else None
    if override is not None and not args.allow_override:
        # 判据不能依赖 isatty：`script -q /dev/null ...` 可以伪造 tty，实测能绕过。
        # 只有「显式成对」才放行，且放行后也不允许返回 GO（见 collect 的强制 CAUTION）。
        print(
            "拒绝：--override 必须与 --allow-override 成对出现（override 结果不可作为判定依据）。",
            file=sys.stderr,
        )
        return OVERRIDE_REFUSED
    deadline = time.time() + max(0, args.wait)
    while True:
        data = collect(override, assume_user_pids=user_pids)
        if data["level"] == "GO" or not args.wait or time.time() >= deadline:
            break
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {data['level']}："
              f"{'；'.join(data['reasons'])}，{args.interval}s 后重试…")
        time.sleep(args.interval)

    _print_human(data)
    if args.report is not None:
        payload = json.dumps(data, ensure_ascii=False, indent=1)
        if args.report == "-":
            print("\n--- JSON ---")
            print(payload)
        else:
            with open(args.report, "w", encoding="utf-8") as f:
                f.write(payload)
            print(f"\nJSON 已写入 {args.report}")
    return data["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
