# -*- coding: utf-8 -*-
"""汇总全部基准结果 → 控制台报告 + results/summary.json

运行：uv run python benchmarks/bench_report.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_common as bc  # noqa: E402


def load(name: str):
    p = bc.RESULTS_DIR / f"{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    startup = load("startup")
    importtime = load("importtime")
    sessleak = load("session_leak")
    pipeline = load("chat_pipeline")
    longrun = load("longrun")

    lines = []
    lines.append("=" * 62)
    lines.append("DriFox 性能基线基准报告（生成于 " + bc.env_info()["timestamp"] + "）")
    lines.append("=" * 62)

    if startup:
        t = startup["t_total_show_s"]
        w = startup["t_window_ms"]
        r = startup["steady_rss_mb"]
        tm = startup["steady_tracemalloc_mb"]
        lines.append("")
        lines.append("[1] 启动性能（3 次取统计，GUI 全链路 main.py 复刻）")
        lines.append(f"    python→主窗 show 总耗时 : 中位 {t['median']}s（{t['min']}~{t['max']}s）")
        lines.append(f"    主窗口构造段           : 中位 {w['median']}ms（{w['min']}~{w['max']}ms）")
        lines.append(f"    稳态 RSS（show+6s）    : 中位 {r['median']}MB（{r['min']}~{r['max']}MB）")
        lines.append(f"    稳态 tracemalloc       : 中位 {tm['median']}MB")

    if importtime:
        lines.append("")
        lines.append(f"[2] 导入耗时（-X importtime，总 self {importtime['total_self_ms']:.0f}ms）Top5 累计:")
        for row in importtime["top_cumulative"][:5]:
            lines.append(f"    {row['cum_ms']:>8.1f} ms  {row['module']}")
        lines.append("    Top5 一级包(self):")
        for row in importtime["top_packages"][:5]:
            lines.append(f"    {row['self_ms']:>8.1f} ms  {row['package']}")

    if sessleak:
        lines.append("")
        lines.append(
            f"[3] 会话生命周期（新建→关闭 {sessleak['rounds']} 轮 × {sessleak['msgs_per_round']} 条/轮）"
        )
        lines.append(f"    tracemalloc 斜率: {sessleak['tracemalloc_slope_kb_per_round']} KB/轮 (R²={sessleak['tracemalloc_r2']})")
        lines.append(f"    RSS 斜率        : {sessleak['rss_slope_kb_per_round']} KB/轮 (R²={sessleak['rss_r2']})")
        lines.append(f"    判定: {sessleak['verdict_tracemalloc']} / {sessleak['verdict_rss']}")
        lines.append(f"    会话数归位稳定: {sessleak['session_count_stable']}")

    if pipeline:
        lines.append("")
        lines.append(
            f"[4] 对话管线（{pipeline['rounds']} 轮 × {pipeline['chunks_per_round']} 流式块，"
            f"渲染+no-render 对照）"
        )
        lines.append(f"    渲染组   : tracemalloc {pipeline['tracemalloc_slope_kb_per_round']} KB/轮 | "
                     f"RSS +{pipeline['rss_slope_kb_per_round']} KB/轮 (R²={pipeline['rss_r2']})")
        norender = load("chat_pipeline_norender")
        if norender:
            lines.append(f"    no-render: tracemalloc {norender['tracemalloc_slope_kb_per_round']} KB/轮 | "
                         f"RSS +{norender['rss_slope_kb_per_round']} KB/轮 (R²={norender['rss_r2']})")
            delta = pipeline["rss_slope_kb_per_round"] - norender["rss_slope_kb_per_round"]
            lines.append(f"    → 差值 {delta:+.0f} KB/轮 归渲染管线；主体 ~{norender['rss_slope_kb_per_round']:.0f} KB/轮 归 MessageCard 构造/销毁链（Qt C++ 侧，deleteLater 不回收）")
        lines.append(f"    判定: {pipeline['verdict_rss']}（Python 堆干净 {pipeline['verdict_tracemalloc']}）")

    if longrun:
        pa, pb = longrun["phaseA"], longrun["phaseB"]
        lines.append("")
        lines.append(f"[5] 长跑模拟 Phase A（渲染累积 {pa['cards']} 张卡，预期线性）")
        lines.append(f"    tracemalloc {pa['tracemalloc_slope_kb_per_card']} KB/卡 | RSS {pa['rss_slope_kb_per_card']} KB/卡 (R²={pa['rss_r2']})")
        lines.append(f"    全部卡片释放后 tracemalloc {pa['tracemalloc_after_release_mb']}MB（Python 层基本归零）")
        lines.append(f"[5] 长跑模拟 Phase B（{pb['sessions']} 会话 × {pb['switches']} 次切换）")
        lines.append(f"    tracemalloc {pb['tracemalloc_slope_kb_per_switch']} KB/次 | RSS {pb['rss_slope_kb_per_switch']} KB/次")
        lines.append(f"    判定: {pb['verdict_tracemalloc']}")

    lines.append("")
    lines.append("=" * 62)
    report = "\n".join(lines)
    print(report)
    bc.RESULTS_DIR.joinpath("summary.txt").write_text(report, encoding="utf-8")
    print(f"\n[bench] 文本报告: {bc.RESULTS_DIR / 'summary.txt'}")


if __name__ == "__main__":
    main()
