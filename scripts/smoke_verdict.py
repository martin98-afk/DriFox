#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""冒烟档机判：按六判据对一次冒烟归档给出 PASS / FAIL / INVALID。

用法::

    python scripts/smoke_verdict.py --archive-dir .perf_out/smoke_<commit8>_<ts>/

归档目录约定（SMK-PRE 归档命令产物）::

    trace_events.jsonl      # 进程内埋点（gdi_handles / watchdog_block / el_latency）
    mem_smoke.csv           # mem_baseline.py 汇总（elapsed_s / main_rss_mb / webengine_mb）
    mem_smoke_detail.csv    # 明细（本脚本不消费，仅完整性提示）
    long_run_b_*.csv        # 长跑场景 B（elapsed_sec / rss_mb）
    smoke_run_meta.json     # 可选：{"expected_rows": {...}}，用于行数预检
    crash_snapshot/         # 运行结束时的 crash 目录快照（crash_*.log）
    crash_baseline/         # 可选：运行前的 crash 快照，按文件名差分
    logs/ 或 *.log          # 归档内日志（判据⑥扫描 soft-unload / kill( ）

判据阈值全部来自 SMK-JUDGE 规格卡（见各常量），脚本独立、不 import 产品代码。
退出码：0=PASS / 1=FAIL / 2=INVALID。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ── 判据阈值（SMK-JUDGE 规格卡） ────────────────────────────
HEARTBEAT_P95_MS = 100.0       # ② 主判据：el_latency p95 全程 max
WATCHDOG_BLOCK_MS = 200.0      # ② 弱判据：watchdog_block 阈值（≥ 即算一次事件）
RSS_DRIFT_MB = 100.0           # ③ 末段−首段 < +100
SLOPE_MB_PER_H = 2.0           # ④ 最小二乘斜率 < 2 MB/h
B_DRIFT_MB = 20.0              # ④ 末 25% 均值−首 25% 均值 < +20
GDI_DRIFT_MAX = 50             # ⑤ 600s 窗内 GDI 增量 max < 50
MAIN_RSS_GATE_MB = 1200.0      # ⑥ 主进程峰值
WEBENGINE_GATE_MB = 800.0      # ⑥ renderer 峰值
GDI_WINDOW_S = 600             # ⑤ 窗口宽度
MIN_ROWS_RATIO = 0.95          # 完整性：行数 ≥ 理论值 95%
SEG_EDGE_S = 3600.0            # ③ 首段 [0,3600) / 末段 (3600,7200]
SEG_TAIL_S = 7200.0
# 闸门动作串：soft-unload( 全库唯一产生点是 T22 闸门（memory_governor.py:448）。
# 不能用 "kill("——[T31] idle-kill(n) 是 180s 空闲回收的正常动作，会误报；
# 闸门 kill 无日志落盘，其发生必然伴随 webengine_mb 超阈，CSV max 判据已兜住。
GATE_ACTION_RE = re.compile(r"soft-unload\(")
MIN_ROWS_FOR_SEG = 2


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_jsonl(path: Path) -> Tuple[List[dict], int]:
    rows: List[dict] = []
    bad = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return rows, bad


def _row_elapsed(row: Dict[str, str], key: str) -> Optional[float]:
    return _to_float(row.get(key))


def _infer_expected_rows(rows: List[Dict[str, str]], key: str) -> Optional[int]:
    """按中位采样间隔推算理论行数；行数不足推算不出返回 None。"""
    xs = sorted(x for x in (_row_elapsed(r, key) for r in rows) if x is not None)
    if len(xs) < 3:
        return None
    gaps = [b - a for a, b in zip(xs, xs[1:]) if b > a]
    if not gaps:
        return None
    interval = statistics.median(gaps)
    if interval <= 0:
        return None
    return int(round(xs[-1] / interval)) + 1


def _row_shortfall(rows: List[Dict[str, str]], key: str, expected_rows: Optional[int]) -> Optional[str]:
    """行数完整性：显式 expected_rows 优先，否则按中位间隔推算。"""
    expected = expected_rows or _infer_expected_rows(rows, key)
    if not expected:
        return None
    if len(rows) < expected * MIN_ROWS_RATIO:
        return f"行数 {len(rows)} < 理论值 {expected} × {MIN_ROWS_RATIO:.0%}"
    return None


def _least_squares_slope_per_hour(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return (cov / var) * 3600.0


def _mean(values: Iterable[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _parse_ts(value: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _criterion(
    cid: str,
    name: str,
    expected: str,
    actual: Any,
    passed: Optional[bool],
    data_source: str,
    note: str = "",
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "id": cid,
        "name": name,
        "expected": expected,
        "actual": actual,
        "pass": passed,
        "data_source": data_source,
    }
    if note:
        item["note"] = note
    return item


# ── 预检 ───────────────────────────────────────────────────


def preflight(archive: Path) -> Tuple[List[str], Dict[str, Any]]:
    """数据完整性预检。返回 (缺失/异常列表, 已加载数据)。"""
    errors: List[str] = []
    data: Dict[str, Any] = {}

    trace_path = archive / "trace_events.jsonl"
    if not trace_path.exists():
        errors.append("缺失 trace_events.jsonl")
    else:
        rows, bad = _read_jsonl(trace_path)
        if bad:
            errors.append(f"trace_events.jsonl 非法行 {bad}（要求 0）")
        if not rows:
            errors.append("trace_events.jsonl 无有效行")
        data["trace"] = rows

    mem_path = archive / "mem_smoke.csv"
    if not mem_path.exists():
        errors.append("缺失 mem_smoke.csv")
    else:
        rows = _read_csv_rows(mem_path)
        if len(rows) < MIN_ROWS_FOR_SEG:
            errors.append(f"mem_smoke.csv 仅 {len(rows)} 行（至少 {MIN_ROWS_FOR_SEG}）")
        data["mem"] = rows

    long_rows: List[Dict[str, str]] = []
    long_files = sorted(archive.glob("long_run_b_*.csv"))
    if not long_files:
        errors.append("缺失 long_run_b_*.csv")
    else:
        for path in long_files:
            long_rows.extend(_read_csv_rows(path))
        if len(long_rows) < MIN_ROWS_FOR_SEG:
            errors.append(f"long_run_b 合计仅 {len(long_rows)} 行（至少 {MIN_ROWS_FOR_SEG}）")
        data["long"] = long_rows

    crash_dir = archive / "crash_snapshot"
    if not crash_dir.is_dir():
        errors.append("缺失 crash_snapshot/ 目录")
    else:
        data["crash"] = crash_dir

    log_files = sorted(archive.rglob("*.log"))
    if not log_files:
        errors.append("缺失日志文件（*.log），判据⑥无法取证")
    data["logs"] = log_files

    meta_path = archive / "smoke_run_meta.json"
    expected_rows: Dict[str, int] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            expected_rows = dict(meta.get("expected_rows") or {})
        except (OSError, ValueError):
            errors.append("smoke_run_meta.json 无法解析")
    data["expected_rows"] = expected_rows

    # 行数完整性（显式理论值优先，否则中位间隔推算）
    if "mem" in data:
        short = _row_shortfall(data["mem"], "elapsed_s", expected_rows.get("mem_smoke"))
        if short:
            errors.append(f"mem_smoke.csv {short}")
    if "long" in data:
        short = _row_shortfall(data["long"], "elapsed_sec", expected_rows.get("long_run_b"))
        if short:
            errors.append(f"long_run_b {short}")

    # 必需列存在性（列名契约）
    for key, cols in (
        ("mem", ("elapsed_s", "main_rss_mb", "webengine_mb")),
        ("long", ("elapsed_sec", "rss_mb")),
    ):
        rows = data.get(key) or []
        if rows:
            missing = [c for c in cols if c not in rows[0]]
            if missing:
                errors.append(f"{key} 缺列: {', '.join(missing)}")

    return errors, data


# ── 六判据 ─────────────────────────────────────────────────


def check_crash(data: Dict[str, Any], archive: Path) -> Dict[str, Any]:
    snapshot = data.get("crash")
    if snapshot is None:
        return _criterion("①", "原生崩溃", "delta == 0", "n/a", False, "crash_snapshot/")
    snapshot_files = {p.name for p in snapshot.glob("crash_*.log")}
    baseline_dir = archive / "crash_baseline"
    baseline_files = {p.name for p in baseline_dir.glob("crash_*.log")} if baseline_dir.is_dir() else set()
    delta = len(snapshot_files - baseline_files)
    note = "" if baseline_dir.is_dir() else "无 crash_baseline/，以 0 为基线"
    return _criterion(
        "①", "原生崩溃", "delta == 0", delta, delta == 0,
        "crash_snapshot/ vs crash_baseline/", note,
    )


def check_heartbeat(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = data.get("trace") or []
    el_rows = [r for r in rows if r.get("metric") == "el_latency"]
    watchdog = [
        r for r in rows
        if r.get("metric") == "watchdog_block"
        and (_to_float(r.get("value")) or 0.0) >= WATCHDOG_BLOCK_MS
    ]
    weak_ok = len(watchdog) == 0
    if not el_rows:
        return _criterion(
            "②", "心跳时延", f"p95 max < {HEARTBEAT_P95_MS:g}ms",
            f"weak: watchdog_block={len(watchdog)}", None,
            "trace_events.jsonl: el_latency", "el_latency 未落地，按规格 SKIP（弱判据通过=%s）" % weak_ok,
        )
    p95 = max(
        v for v in (_to_float((r.get("tags") or {}).get("p95")) for r in el_rows) if v is not None
    ) if any((_to_float((r.get("tags") or {}).get("p95")) is not None) for r in el_rows) else None
    if p95 is None:
        return _criterion("②", "心跳时延", f"p95 max < {HEARTBEAT_P95_MS:g}ms",
                          "el_latency 行缺 tags.p95", False, "trace_events.jsonl: el_latency")
    return _criterion(
        "②", "心跳时延", f"p95 max < {HEARTBEAT_P95_MS:g}ms 且 watchdog_block==0",
        f"p95={p95:.1f}ms, watchdog_block={len(watchdog)}",
        p95 < HEARTBEAT_P95_MS and weak_ok,
        "trace_events.jsonl: el_latency / watchdog_block",
    )


def check_rss_drift(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = data.get("mem") or []
    pairs = [
        (e, r) for e, r in
        ((_row_elapsed(r, "elapsed_s"), _to_float(r.get("main_rss_mb"))) for r in rows)
        if e is not None and r is not None
    ]
    head = [r for e, r in pairs if 0 <= e < SEG_EDGE_S]
    tail = [r for e, r in pairs if SEG_EDGE_S < e <= SEG_TAIL_S]
    if not head or not tail:
        return _criterion(
            "③", "RSS 漂移", f"末段−首段 < +{RSS_DRIFT_MB:g}MB", "n/a", False,
            "mem_smoke.csv", f"分段数据不足（首段 {len(head)} 行 / 末段 {len(tail)} 行）",
        )
    drift = _mean(tail) - _mean(head)
    return _criterion(
        "③", "RSS 漂移", f"末段−首段 < +{RSS_DRIFT_MB:g}MB",
        f"{drift:+.1f}MB（首段 {len(head)} 行 / 末段 {len(tail)} 行）",
        drift < RSS_DRIFT_MB, "mem_smoke.csv",
    )


def check_scenario_b(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = data.get("long") or []
    pairs = [
        (e, r) for e, r in
        ((_row_elapsed(r, "elapsed_sec"), _to_float(r.get("rss_mb"))) for r in rows)
        if e is not None and r is not None
    ]
    pairs.sort(key=lambda x: x[0])
    if len(pairs) < 4:
        return _criterion(
            "④", "场景B稳定", f"slope<{SLOPE_MB_PER_H:g}MB/h 且 Δ<+{B_DRIFT_MB:g}MB", "n/a", False,
            "long_run_b_*.csv", f"有效行 {len(pairs)} < 4",
        )
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    slope = _least_squares_slope_per_hour(xs, ys)
    q = max(1, len(ys) // 4)
    delta = _mean(ys[-q:]) - _mean(ys[:q])
    passed = slope is not None and slope < SLOPE_MB_PER_H and delta < B_DRIFT_MB
    return _criterion(
        "④", "场景B稳定", f"slope<{SLOPE_MB_PER_H:g}MB/h 且 末25%−首25%<+{B_DRIFT_MB:g}MB",
        f"slope={slope:+.2f}MB/h, Δ={delta:+.1f}MB" if slope is not None else "slope=n/a",
        passed, "long_run_b_*.csv",
    )


def check_gdi_drift(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = data.get("trace") or []
    windows: Dict[int, List[Tuple[float, float]]] = {}
    for r in rows:
        if r.get("metric") != "gdi_handles":
            continue
        ts = _parse_ts(r.get("ts"))
        val = _to_float(r.get("value"))
        if ts is None or val is None:
            continue
        windows.setdefault(int(ts // GDI_WINDOW_S), []).append((ts, val))
    if not windows:
        return _criterion(
            "⑤", "GDI 漂移", f"600s 窗增量 max < {GDI_DRIFT_MAX}", "n/a", False,
            "trace_events.jsonl: gdi_handles", "无 gdi_handles 行（DRIFOX_HANDLE_TRACE 未开？）",
        )
    deltas = []
    for items in windows.values():
        items.sort(key=lambda x: x[0])
        deltas.append(items[-1][1] - items[0][1])
    worst = max(deltas)
    return _criterion(
        "⑤", "GDI 漂移", f"600s 窗增量 max < {GDI_DRIFT_MAX}",
        f"{worst:+.0f}（{len(windows)} 个窗）", worst < GDI_DRIFT_MAX,
        "trace_events.jsonl: gdi_handles",
    )


def check_gate(data: Dict[str, Any]) -> Dict[str, Any]:
    rows = data.get("mem") or []
    main_vals = [v for v in (_to_float(r.get("main_rss_mb")) for r in rows) if v is not None]
    web_vals = [v for v in (_to_float(r.get("webengine_mb")) for r in rows) if v is not None]
    main_max = max(main_vals) if main_vals else None
    web_max = max(web_vals) if web_vals else None
    action_hits: List[str] = []
    for path in data.get("logs") or []:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in GATE_ACTION_RE.finditer(text):
            snippet = text[max(0, match.start() - 40):match.end() + 40].strip().replace("\n", " ")
            action_hits.append(f"{path.name}: {snippet}")
            break  # 每文件只记一条样例，避免撑爆输出
    passed = (
        main_max is not None and main_max < MAIN_RSS_GATE_MB
        and web_max is not None and web_max < WEBENGINE_GATE_MB
        and not action_hits
    )
    actual = (
        f"main={main_max:.0f}MB, web={web_max:.0f}MB, actions={len(action_hits)}"
        if main_max is not None and web_max is not None else "n/a"
    )
    note = "; ".join(action_hits[:3])
    return _criterion(
        "⑥", "软闸门未触发",
        f"main<{MAIN_RSS_GATE_MB:g}MB 且 web<{WEBENGINE_GATE_MB:g}MB 且日志无 soft-unload(",
        actual, passed, "mem_smoke.csv + 归档日志", note,
    )


# ── 汇总 / 输出 ────────────────────────────────────────────


def _write_invalid(archive: Path, errors: List[str]) -> int:
    reason = "; ".join(errors)
    payload = {
        "summary": {
            "verdict": "INVALID",
            "pass_count": 0,
            "fail_items": [],
            "invalid_reason": reason,
        },
        "criteria": [],
    }
    (archive / "verdict.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (archive / "verdict.md").write_text(
        "# 冒烟档机判\n\n**INVALID**\n\n数据完整性预检未通过：\n\n"
        + "\n".join(f"- {e}" for e in errors) + "\n",
        encoding="utf-8",
    )
    print(f"INVALID: {reason}")
    return 2


def _render_md(criteria: List[Dict[str, Any]], verdict: str, pass_count: int, fail_items: List[str]) -> str:
    lines = [
        "# 冒烟档机判",
        "",
        f"**{verdict}** — PASS {pass_count} / 判分 {sum(1 for c in criteria if c['pass'] is not None)}"
        + (f"，FAIL: {', '.join(fail_items)}" if fail_items else ""),
        "",
        "| 判据 | 名称 | 期望 | 实际 | 结果 | 数据源 |",
        "|---|---|---|---|---|---|",
    ]
    for c in criteria:
        mark = "SKIP" if c["pass"] is None else ("PASS" if c["pass"] else "FAIL")
        lines.append(
            f"| {c['id']} | {c['name']} | {c['expected']} | {c['actual']} | {mark} | {c['data_source']} |"
        )
    notes = [f"- {c['id']}: {c['note']}" for c in criteria if c.get("note")]
    if notes:
        lines.extend(["", "## 说明", ""] + notes)
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="冒烟档机判（六判据 -> verdict.md/json）")
    parser.add_argument("--archive-dir", required=True, help="冒烟归档目录")
    args = parser.parse_args(argv)

    archive = Path(args.archive_dir)
    if not archive.is_dir():
        print(f"INVALID: 归档目录不存在: {archive}")
        return 2

    errors, data = preflight(archive)
    if errors:
        return _write_invalid(archive, errors)

    criteria = [
        check_crash(data, archive),
        check_heartbeat(data),
        check_rss_drift(data),
        check_scenario_b(data),
        check_gdi_drift(data),
        check_gate(data),
    ]
    judged = [c for c in criteria if c["pass"] is not None]
    fail_items = [c["id"] for c in judged if not c["pass"]]
    pass_count = len(judged) - len(fail_items)
    verdict = "PASS" if not fail_items else "FAIL"

    payload = {
        "summary": {
            "verdict": verdict,
            "pass_count": pass_count,
            "fail_items": fail_items,
            "invalid_reason": None,
        },
        "criteria": criteria,
    }
    (archive / "verdict.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (archive / "verdict.md").write_text(
        _render_md(criteria, verdict, pass_count, fail_items), encoding="utf-8"
    )
    print(f"{verdict}: pass={pass_count} fail={','.join(fail_items) or '-'}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
