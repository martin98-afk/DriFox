# -*- coding: utf-8 -*-
"""perf_compare.py — DriFox 性能基线对比器（子任务 #11，口径设计见子任务 #4）

用法:
  python tools/perf/perf_compare.py baseline_before.json baseline_after.json [--md out.md]

输入:
  - baseline.py `all` 产出的 JSON（dimensions.startup/memory/fps/longevity）
  - 兼容 splitter_tabpanel_baseline.py 产出（results.collapse/expand/splitter_drag）

输出:
  stdout markdown 对比表 + 每指标 verdict；--md 同时落盘。数字全由脚本直出，禁手工誊写。

退出码:
  0 = 通过（含 WARN）；1 = 存在 FAIL；2 = 口径不一致（拒绝对比）

判定规则（#4 设计定稿）:
  A1/A2 内存 rss_*: Δ>+5% 且 Δ>+50MB → WARN；绝对值超闸门线 → 软阈 WARN / 硬阈 FAIL
  B1 CPU cpu_mean:  Δ>+20% → WARN（CPU 方差大，只警告）
  B3 fps_mean:      Δ<-5% → FAIL；绝对值 <30 → FAIL，<55 → WARN（真窗空闲目标 55）
  B3 帧间隔 p99:    >33ms → WARN（交互下限 30fps）
  D1 启动 p95:      Δ>+10% → WARN（网络 IO 方差大，不 FAIL）
  longevity:        leak_suspected=true → FAIL；rss_delta_mb Δ>+10MB → WARN
  A7 回收动作:      测量窗口内 kill/idle/gate 动作 >0 → WARN（闸门被触发即异常信号）
  splitter schema:  jank_ratio>1% → FAIL；>0 → WARN；paint_mean 回归 >10% → WARN

meta 锁定字段: platform / python / qt_version / entry / synthetic / fixture_scenario
任一不一致 → 退出码 2，拒绝对比（口径必须可复现）。

环境纪律（历史教训固化，测量侧必须遵守）:
  - 真窗测量（offscreen 下内存/FPS 结论不可信）
  - 禁 sitecustomize 探针（污染内存曲线）
  - 对比双方须同机、同 DPR、同 fixture 规模
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# meta 锁定字段：值不一致即拒绝对比
LOCK_KEYS = ["platform", "python", "qt_version", "entry", "synthetic", "fixture_scenario"]

# 闸门线（与 app/core/infra/memory_governor.py 常量对齐，单位 MB）
MAIN_RSS_SOFT, MAIN_RSS_HARD = 1200, 1800
WEB_RSS_SOFT, WEB_RSS_HARD = 800, 1200


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _d(a, b):
    """(绝对差, 百分比差)；基线为 0/None 时百分比返回 None。"""
    da = None if (a is None or b is None) else round(b - a, 4)
    dp = None
    if a not in (None, 0) and b is not None:
        dp = round((b - a) / abs(a) * 100.0, 2)
    return da, dp


def _fmt(v, unit=""):
    return "—" if v is None else f"{v}{unit}"


class Row:
    def __init__(self, label, before, after, unit="", verdict="OK", note=""):
        self.label, self.before, self.after = label, before, after
        self.unit, self.verdict, self.note = unit, verdict, note
        self.delta, self.pct = _d(before, after)


def _cmp_mem(rows, label, b, a, soft, hard):
    """内存指标：回归 WARN → 超软阈 WARN → 超硬阈 FAIL。"""
    rows.append(Row(label, b, a, "MB"))
    r = rows[-1]
    if b is not None and a is not None:
        if r.delta is not None and r.pct is not None and r.delta > 50 and r.pct > 5:
            r.verdict, r.note = "WARN", "回归 >+5% 且 >+50MB"
    if hard is not None and a is not None and a > hard:
        r.verdict, r.note = "FAIL", f"超硬阈 {hard}MB"
    elif soft is not None and a is not None and a > soft and r.verdict == "OK":
        r.verdict, r.note = "WARN", f"超软阈 {soft}MB"


def _cmp_framerate(rows, fps_b, fps_a, p99_b, p99_a):
    rows.append(Row("fps_mean", fps_b, fps_a, "fps"))
    r = rows[-1]
    if fps_b and fps_a:
        if fps_a < 30:
            r.verdict, r.note = "FAIL", "低于 30fps 硬线"
        elif fps_a < 55:
            r.verdict, r.note = "WARN", "低于空闲目标 55fps"
        elif r.pct is not None and r.pct < -5:
            r.verdict, r.note = "FAIL", "回归 >-5%"
    rows.append(Row("frame_p99_ms", p99_b, p99_a, "ms",
                    "WARN" if (p99_a is not None and p99_a > 33) else "OK",
                    "交互下限 30fps（p99≤33ms）" if (p99_a is not None and p99_a > 33) else ""))


def _cmp_startup(rows, b_cold, a_cold):
    for key, name in (("import_s", "启动 import_s(p95)"),
                      ("to_visible_s", "启动 to_visible_s(p95)"),
                      ("app_init_s", "启动 app_init_s(p95)")):
        b = (b_cold or {}).get(key, {}).get("p95") if b_cold else None
        a = (a_cold or {}).get(key, {}).get("p95") if a_cold else None
        rows.append(Row(name, b, a, "s"))
        r = rows[-1]
        if b and a and r.pct is not None and r.pct > 10:
            r.verdict, r.note = "WARN", "回归 >+10%（网络方差源，仅警告）"


def _cmp_longevity(rows, b_l, a_l):
    if not b_l and not a_l:
        return
    rows.append(Row("longevity rss_delta_mb", (b_l or {}).get("rss_delta_mb"),
                    (a_l or {}).get("rss_delta_mb"), "MB"))
    r = rows[-1]
    if r.delta is not None and r.delta > 10:
        r.verdict, r.note = "WARN", "长时增长回归 >+10MB"
    bv, av = (b_l or {}).get("leak_suspected"), (a_l or {}).get("leak_suspected")
    rows.append(Row("longevity leak_suspected", bv, av, "",
                    "FAIL" if av else "OK", "疑似泄漏" if av else ""))
    # A7：本进程窗口内的回收动作计数（kill/idle/gate 合计 >0 即 WARN）
    ba = (b_l or {}).get("recycle_actions") or {}
    aa = (a_l or {}).get("recycle_actions") or {}
    bt = sum(v for k, v in ba.items() if k != "gate_main_pressure")
    at = sum(v for k, v in aa.items() if k != "gate_main_pressure")
    rows.append(Row("回收动作计数(kill/idle/gate)", bt, at, "次",
                    "WARN" if at > 0 else "OK",
                    "闸门在测量窗口内被触发" if at > 0 else ""))


def _cmp_cpu(rows, b_m, a_m):
    for key, name in (("cpu_mean", "cpu_mean(单核%)"), ("cpu_p95", "cpu_p95(单核%)")):
        b = (b_m or {}).get(key)
        a = (a_m or {}).get(key)
        rows.append(Row(name, b, a, "%"))
        r = rows[-1]
        if b and a and r.pct is not None and r.pct > 20:
            r.verdict, r.note = "WARN", "回归 >+20%（CPU 方差大，仅警告）"


def _compare_baseline(b_rep, a_rep):
    rows = []
    bd, ad = b_rep.get("dimensions", {}), a_rep.get("dimensions", {})
    bm, am = bd.get("memory") or {}, ad.get("memory") or {}
    bf, af = bd.get("fps") or {}, ad.get("fps") or {}
    bl, al = bd.get("longevity") or {}, ad.get("longevity") or {}

    # A1 主进程 RSS
    _cmp_mem(rows, "主进程 rss_visible_mb", bm.get("rss_visible_mb"), am.get("rss_visible_mb"),
             MAIN_RSS_SOFT, MAIN_RSS_HARD)
    _cmp_mem(rows, "主进程 rss_peak_mb", bm.get("rss_peak_mb"), am.get("rss_peak_mb"),
             MAIN_RSS_SOFT, MAIN_RSS_HARD)
    # A2/A3/A4 WebEngine 子进程 RSS（rss_sampler 口径）
    bs, as_ = bm.get("rss_sampler_snapshot") or {}, am.get("rss_sampler_snapshot") or {}
    _cmp_mem(rows, "WebEngine web_rss_mb", bs.get("web_rss_mb"), as_.get("web_rss_mb"),
             WEB_RSS_SOFT, WEB_RSS_HARD)
    rows.append(Row("WebEngine 进程数", bs.get("web_by_pid_count"),
                    as_.get("web_by_pid_count"), "个"))
    rows.append(Row("单 renderer max_mb", bs.get("web_pid_max_mb"),
                    as_.get("web_pid_max_mb"), "MB"))
    rows.append(Row("单 renderer median_mb", bs.get("web_pid_median_mb"),
                    as_.get("web_pid_median_mb"), "MB"))
    # A5/A6 governor
    bg, ag = bm.get("governor_snapshot") or {}, am.get("governor_snapshot") or {}
    rows.append(Row("global_rendered_pages", bg.get("global_rendered_pages"),
                    ag.get("global_rendered_pages"), "页"))
    # B1 CPU
    _cmp_cpu(rows, bm, am)
    # B3 帧率
    _cmp_framerate(rows, bf.get("fps_mean"), af.get("fps_mean"),
                   bf.get("frame_p99_ms"), af.get("frame_p99_ms"))
    # D1 启动
    _cmp_startup(rows, (bd.get("startup") or {}).get("cold"),
                 (ad.get("startup") or {}).get("cold"))
    # longevity + A7
    _cmp_longevity(rows, bl, al)
    return rows


def _compare_splitter(b_rep, a_rep):
    rows = []
    br, ar = b_rep.get("results", {}), a_rep.get("results", {})
    for op in ("collapse", "expand", "splitter_drag"):
        b, a = br.get(op) or {}, ar.get(op) or {}
        if not b and not a:
            continue
        rows.append(Row(f"{op} jank_ratio", b.get("jank_ratio"), a.get("jank_ratio"), "",
                        "FAIL" if (a.get("jank_ratio") or 0) > 0.01
                        else ("WARN" if (a.get("jank_ratio") or 0) > 0 else "OK")))
        rows.append(Row(f"{op} paint_mean_ms", b.get("paint_mean_ms"),
                        a.get("paint_mean_ms"), "ms"))
        r = rows[-1]
        if b.get("paint_mean_ms") and a.get("paint_mean_ms") and r.pct is not None and r.pct > 10:
            r.verdict, r.note = "WARN", "回归 >+10%"
        rows.append(Row(f"{op} paint_max_ms", b.get("paint_max_ms"),
                        a.get("paint_max_ms"), "ms"))
    rows.append(Row("sync_collapsed_ui_ms", br.get("sync_collapsed_ui_ms"),
                    ar.get("sync_collapsed_ui_ms"), "ms"))
    return rows


def _check_meta(b_rep, a_rep):
    bm, am = b_rep.get("meta", {}), a_rep.get("meta", {})
    bad = []
    for k in LOCK_KEYS:
        bv, av = bm.get(k), am.get(k)
        if k == "fixture_scenario" and bv in (None, "none") and av in (None, "none"):
            continue  # 两边缺省等价 none
        if bv != av:
            bad.append(f"{k}: {bv!r} vs {av!r}")
    return bad


def _render(rows, before_path, after_path, bad_meta):
    lines = ["# 性能基线对比报告", "",
             f"- before: `{before_path}`", f"- after: `{after_path}`", ""]
    if bad_meta:
        lines += ["## 口径不一致（拒绝对比）", ""]
        lines += [f"- {x}" for x in bad_meta]
        return "\n".join(lines)
    lines += ["| 指标 | before | after | Δ | Δ% | 判定 | 备注 |",
              "|---|---|---|---|---|---|---|"]
    for r in rows:
        icon = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌"}[r.verdict]
        lines.append(
            f"| {r.label} | {_fmt(r.before, r.unit)} | {_fmt(r.after, r.unit)} "
            f"| {_fmt(r.delta, r.unit)} | {_fmt(r.pct)}% "
            f"| {icon} {r.verdict} | {r.note} |")
    n_fail = sum(1 for r in rows if r.verdict == "FAIL")
    n_warn = sum(1 for r in rows if r.verdict == "WARN")
    lines += ["", (f"## 结论: ❌ FAIL（{n_fail} FAIL / {n_warn} WARN）" if n_fail
                   else f"## 结论: {'⚠️ PASS with WARN（' + str(n_warn) + ' WARN）' if n_warn else '✅ PASS'}")]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="DriFox 性能基线对比器")
    ap.add_argument("before", help="改前基线 JSON（baseline.py --out 产出）")
    ap.add_argument("after", help="改后 JSON")
    ap.add_argument("--md", default="", help="markdown 报告落盘路径（可选）")
    args = ap.parse_args()

    b_rep, a_rep = _load(args.before), _load(args.after)
    # 防覆盖（#22 预审建议）：--md 落盘路径不得等于任一输入 JSON，防编排误传覆盖基线
    out_abs = os.path.abspath(args.md) if args.md else ""
    if out_abs and out_abs in (os.path.abspath(args.before), os.path.abspath(args.after)):
        sys.stderr.write(f"[perf_compare] --md 路径与输入 JSON 重合，拒绝写出: {args.md}\n")
        sys.exit(2)
    bad_meta = _check_meta(b_rep, a_rep)
    is_splitter = "results" in b_rep and "dimensions" not in b_rep
    rows = _compare_splitter(b_rep, a_rep) if is_splitter else _compare_baseline(b_rep, a_rep)
    md = _render(rows, args.before, args.after, bad_meta)
    print(md)
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(md + "\n")
    if bad_meta:
        sys.exit(2)
    sys.exit(1 if any(r.verdict == "FAIL" for r in rows) else 0)


if __name__ == "__main__":
    main()
