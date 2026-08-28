# -*- coding: utf-8 -*-
"""压测 runner：统一调度 3 个场景，落 CSV/JSON，生成内存曲线图 + Markdown 摘要。

CLI 用法：
    python -m tests.perf.long_run.runner                  # demo 模式（每场景 ≤30s）
    LONGRUN_FULL=1 python -m tests.perf.long_run.runner    # 全量模式（每场景 ≥30min）
    python -m tests.perf.long_run.runner --scenario a     # 只跑场景 A

pytest 用法（自动 demo 模式）：
    pytest tests/perf/long_run/test_long_run_scenarios.py -v -m perf_long
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List

# 让 PySide6 / app.* 可被 import
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtWidgets import QApplication

from .sampler import Sample, env_full_mode, get_sample_interval, start_tracemalloc, stop_tracemalloc


BASELINES_DIR = Path(__file__).resolve().parent / "baselines"
BASELINES_DIR.mkdir(parents=True, exist_ok=True)

# 默认时长：demo 30s/场景，full 30min/场景
DEMO_DURATION_SEC = 30.0
FULL_DURATION_SEC = 1800.0

SCENARIO_REGISTRY: Dict[str, Callable] = {}


def register_scenario(name: str):
    """装饰器：注册场景函数到 SCENARIO_REGISTRY。"""

    def _wrap(fn: Callable) -> Callable:
        SCENARIO_REGISTRY[name] = fn
        return fn

    return _wrap


def _ensure_qapp() -> QApplication:
    """确保 QApplication 单例存在（PySide6 必要）。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app  # type: ignore[return-value]


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_csv(samples: List[Sample], path: Path) -> None:
    """落 CSV：列：scenario,elapsed_sec,loop_iter,rss_mb,qobject_count,biz_total,tm_current_mb,tm_peak_mb,timestamp"""
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "scenario",
                "elapsed_sec",
                "loop_iter",
                "rss_mb",
                "rss_ok",
                "qobject_count",
                "qobject_ok",
                "biz_total",
                "biz_top_label",
                "biz_top_count",
                "tm_current_mb",
                "tm_peak_mb",
                "tm_ok",
                "timestamp",
            ]
        )
        for s in samples:
            biz_total = sum(s.biz_object_counts.values())
            biz_top_label = ""
            biz_top_count = 0
            if s.biz_object_counts:
                top_name, top_count = max(s.biz_object_counts.items(), key=lambda kv: kv[1])
                biz_top_label = top_name
                biz_top_count = top_count
            w.writerow(
                [
                    s.scenario,
                    f"{s.elapsed_sec:.2f}",
                    s.loop_iter,
                    f"{s.rss_mb:.2f}",
                    s.rss_ok,
                    s.qobject_count,
                    s.qobject_ok,
                    biz_total,
                    biz_top_label,
                    biz_top_count,
                    f"{s.tracemalloc_current_mb:.3f}",
                    f"{s.tracemalloc_peak_mb:.3f}",
                    s.tracemalloc_ok,
                    f"{s.timestamp:.3f}",
                ]
            )


def _write_json(summary: dict, path: Path) -> None:
    """落 JSON：完整 summary（含每个 Sample 详情）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = dict(summary)
    serializable["samples"] = [s.to_dict() for s in summary["samples"]]
    with path.open("w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def _compute_growth_rate(samples: List[Sample]) -> Dict[str, float]:
    """计算 RSS / QObject 增长率（MB/h 或 obj/h）。

    线性拟合 y = a*x + b 中 a 即速率。样本 < 2 时返回空 dict。

    抗噪声：只用末 60% 样本做拟合，避免启动期 tracemalloc 栈帧分配 /
    import 缓存造成的初始波动污染斜率（demo 模式样本少时尤其重要）。
    """
    if len(samples) < 2:
        return {}

    # 取末 60%（至少 2 个样本）
    start_idx = max(0, len(samples) - max(2, int(len(samples) * 0.6)))
    tail = samples[start_idx:]

    xs = [s.elapsed_sec for s in tail]
    rss = [s.rss_mb for s in tail]
    qobj = [s.qobject_count for s in tail]

    def _linreg(xs: List[float], ys: List[float]) -> float:
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        den = sum((xs[i] - mx) ** 2 for i in range(n))
        return num / den if den > 0 else 0.0

    rss_rate_per_sec = _linreg(xs, rss)  # MB/sec
    qobj_rate_per_sec = _linreg(xs, qobj)  # obj/sec
    return {
        "rss_mb_per_hour": rss_rate_per_sec * 3600,
        "qobject_per_hour": qobj_rate_per_sec * 3600,
    }


def _classify_leak(rate_mb_per_hour: float, duration_min: float) -> str:
    """判定泄漏等级：
    - rate < 5 MB/h：稳定（demo 模式常见抖动）
    - 5-50 MB/h：观察（短期 OK，长时累计可能溢出）
    - ≥50 MB/h：可疑泄漏
    """
    if rate_mb_per_hour < 5.0:
        return "stable"
    if rate_mb_per_hour < 50.0:
        return "watch"
    return "suspect_leak"


def _render_html_chart(scenarios: Dict[str, List[Sample]], out_path: Path) -> None:
    """生成 ECharts HTML 内存曲线（无需 matplotlib 依赖，浏览器/VSCode 内可看）。"""
    series = []
    for name, samples in scenarios.items():
        series.append(
            {
                "name": name,
                "type": "line",
                "showSymbol": False,
                "smooth": False,
                "data": [[round(s.elapsed_sec, 1), round(s.rss_mb, 2)] for s in samples],
            }
        )
        series.append(
            {
                "name": f"{name} (QObject)",
                "type": "line",
                "yAxisIndex": 1,
                "showSymbol": False,
                "smooth": False,
                "data": [[round(s.elapsed_sec, 1), s.qobject_count] for s in samples],
            }
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>DriFox 长时压测内存曲线</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
body{{font-family:system-ui;margin:24px;background:#0f1115;color:#e6e6e6}}
h1{{margin:0 0 8px}}
.sub{{color:#888;margin-bottom:16px}}
.chart{{width:100%;height:520px}}
</style></head>
<body>
<h1>DriFox 长时间运行内存 / QObject 曲线</h1>
<div class="sub">采样间隔 {get_sample_interval():.0f}s · 模式 {"FULL" if env_full_mode() else "DEMO"} · 生成时间 {_ts()}</div>
<div id="c1" class="chart"></div>
<script>
const chart = echarts.init(document.getElementById('c1'));
chart.setOption({{
  tooltip: {{trigger:'axis'}},
  legend: {{textStyle:{{color:'#ccc'}}}},
  xAxis: {{type:'value',name:'elapsed_sec'}},
  yAxis: [
    {{type:'value',name:'RSS (MB)',position:'left'}},
    {{type:'value',name:'QObject count',position:'right'}}
  ],
  series: {json.dumps(series, ensure_ascii=False)}
}});
</script></body></html>"""
    out_path.write_text(html, encoding="utf-8")


def _render_ascii_chart(samples: List[Sample], width: int = 60, height: int = 18) -> str:
    """生成 ASCII 内存曲线（终端可看，无外部依赖）。

    横轴：采样序号；纵轴：RSS（MB）。
    """
    if len(samples) < 2:
        return "(<2 samples, no chart)"

    rss = [s.rss_mb for s in samples]
    lo, hi = min(rss), max(rss)
    if hi - lo < 0.1:
        return f"(RSS 几乎不变: {lo:.1f} MB, {len(samples)} samples)"

    # 归一化到 height
    grid = [[" "] * width for _ in range(height)]
    n = len(samples)
    for i, s in enumerate(samples):
        x = int(i / max(n - 1, 1) * (width - 1))
        y_norm = (s.rss_mb - lo) / (hi - lo)
        y = height - 1 - int(y_norm * (height - 1))
        grid[y][x] = "●"

    # 边框
    rows = ["┌" + "─" * width + "┐"]
    for row in grid:
        rows.append("│" + "".join(row) + "│")
    rows.append("└" + "─" * width + "┘")
    rows.append(f"  RSS 范围: {lo:.1f} - {hi:.1f} MB  ·  样本数: {n}")
    return "\n".join(rows)


def run_all_scenarios(
    *,
    scenarios: List[str],
    duration_sec: float,
    out_dir: Path = BASELINES_DIR,
) -> Dict[str, dict]:
    """跑指定场景列表，返回每个场景的 summary dict。

    每个场景单独落 CSV + JSON 到 out_dir/<scenario>_long_run_<ts>.{csv,json}。
    """
    _ensure_qapp()
    start_tracemalloc()

    timestamp = _ts()
    summaries: Dict[str, dict] = {}

    for name in scenarios:
        if name not in SCENARIO_REGISTRY:
            print(f"[runner] 未知场景: {name}，跳过", file=sys.stderr)
            continue

        fn = SCENARIO_REGISTRY[name]
        print(f"\n[r Runner] ===== 场景 {name} 启动 (duration={duration_sec:.0f}s) =====")

        def _cb(i: int, s: Sample, _n=name) -> None:
            print(
                f"  [{_n}] iter={i:6d}  rss={s.rss_mb:7.1f}MB  "
                f"qobj={s.qobject_count:5d}  elapsed={s.elapsed_sec:6.1f}s  "
                f"tm_cur={s.tracemalloc_current_mb:6.2f}MB"
            )

        summary = fn(progress_cb=_cb, duration_sec=duration_sec)
        summaries[name] = summary

        # 落 CSV / JSON
        csv_path = out_dir / f"long_run_{name}_{timestamp}.csv"
        json_path = out_dir / f"long_run_{name}_{timestamp}.json"
        _write_csv(summary["samples"], csv_path)
        # summary 里 samples 已经在 JSON 中
        _write_json(summary, json_path)
        print(f"  [runner] 落盘: {csv_path.name} + {json_path.name}")

    # 收尾：渲染 HTML 曲线
    chart_path = out_dir / f"long_run_chart_{timestamp}.html"
    _render_html_chart(
        {name: summaries[name]["samples"] for name in scenarios if name in summaries},
        chart_path,
    )
    print(f"  [runner] 图表: {chart_path}")

    stop_tracemalloc()
    return summaries


def render_markdown_report(
    summaries: Dict[str, dict],
    *,
    out_path: Path,
    timestamp: str,
    duration_sec: float,
) -> None:
    """汇总 3 场景 → Markdown 报告（基线数字 + 增长率 + 泄漏判定）。"""
    lines: List[str] = []
    lines.append("# DriFox 长时压测基线报告\n")
    lines.append(f"- 生成时间: `{timestamp}`")
    lines.append(f"- 模式: **{'FULL' if env_full_mode() else 'DEMO'}**")
    lines.append(f"- 单场景时长: **{duration_sec:.0f}s**")
    lines.append(f"- 采样间隔: {get_sample_interval():.0f}s")
    # 运行环境（保证可复现）
    import platform

    try:
        from PySide6.QtCore import QT_VERSION_STR
        from PySide6.Qt import PYQT_VERSION_STR
    except Exception:
        QT_VERSION_STR = PYQT_VERSION_STR = "n/a"
    lines.append(
        f"- 运行环境: Python {platform.python_version()} · "
        f"PySide6 {PYQT_VERSION_STR} · Qt {QT_VERSION_STR} · {platform.system()} {platform.machine()}"
    )
    lines.append("")

    # 判定方法说明
    lines.append("## 判定方法\n")
    lines.append("- **采样**: 每 `采样间隔` 秒记录一次 RSS（psutil）/ QObject 总数（gc 扫描）/ tracemalloc top10")
    lines.append("- **增长率**: 对末 60% 样本的 RSS 做线性回归，斜率 ×3600 = MB/h（抗启动期噪声）")
    lines.append("- **泄漏判定**:")
    lines.append("  - `< 5 MB/h` → `stable`（通常 GC 抖动）")
    lines.append("  - `5–50 MB/h` → `watch`（弱增长，建议长时二次采样）")
    lines.append("  - `≥ 50 MB/h` → `suspect_leak`（⚠️ 可疑泄漏，需结合 perf-analyzer 静态分析）")
    lines.append("- **注**: DEMO 模式（≤45s）样本点少，速率判定可能受 tracemalloc 栈帧分配尖峰干扰，")
    lines.append("  **权威基线请以 FULL 模式（每场景 ≥30min）为准**。")
    lines.append("")

    # 总览表
    lines.append("## 场景总览\n")
    lines.append("| 场景 | 操作次数 | RSS Δ(MB) | QObject Δ | RSS 速率(MB/h) | 等级 |")
    lines.append("|---|---:|---:|---:|---:|---|")

    worst_scenario = None
    worst_rate = float("-inf")
    for name, summary in summaries.items():
        samples: List[Sample] = summary["samples"]
        rates = _compute_growth_rate(samples)
        rate = rates.get("rss_mb_per_hour", 0.0)
        classification = _classify_leak(rate, duration_sec / 60.0)
        if rate > worst_rate:
            worst_rate = rate
            worst_scenario = name
        lines.append(
            f"| `{name}` | {summary['iterations']:,} | "
            f"{summary['rss_delta_mb']:+.2f} | "
            f"{summary['qobject_delta']:+d} | "
            f"{rate:+.2f} | **{classification}** |"
        )

    lines.append("")
    lines.append(f"**判定**: 最严重场景为 `{worst_scenario}`，RSS 速率 {worst_rate:+.2f} MB/h。")

    if worst_rate < 5.0:
        lines.append("**结论**: 三场景在采样窗口内均表现稳定，未观察到线性内存增长（速率 <5 MB/h 通常为 GC 抖动）。")
    elif worst_rate < 50.0:
        lines.append("**结论**: 存在弱增长（5-50 MB/h），建议长时运行（≥8h）后二次采样以确认。")
    else:
        lines.append(
            "**结论**: ⚠️ **存在可疑泄漏**，需结合 tracemalloc top10 与 perf-analyzer 的代码层静态分析确认根因。"
        )

    # 每个场景详情
    lines.append("\n## 场景详情\n")
    for name, summary in summaries.items():
        lines.append(f"\n### {name}\n")
        samples = summary["samples"]
        rates = _compute_growth_rate(samples)
        rate = rates.get("rss_mb_per_hour", 0.0)
        classification = _classify_leak(rate, duration_sec / 60.0)

        lines.append(f"- 操作次数: **{summary['iterations']:,}**")
        lines.append(f"- 实际时长: {summary['elapsed_sec']:.1f}s")
        lines.append(
            f"- RSS: {summary['rss_mb_first']:.1f} MB → {summary['rss_mb_last']:.1f} MB (Δ {summary['rss_delta_mb']:+.2f} MB)"
        )
        lines.append(
            f"- QObject: {summary['qobject_first']} → {summary['qobject_last']} (Δ {summary['qobject_delta']:+d})"
        )
        lines.append(f"- RSS 增长速率: **{rate:+.2f} MB/h** ({classification})")

        # 场景特定字段
        for k in summary.keys():
            if k in (
                "scenario",
                "iterations",
                "elapsed_sec",
                "samples",
                "rss_mb_first",
                "rss_mb_last",
                "rss_delta_mb",
                "qobject_first",
                "qobject_last",
                "qobject_delta",
            ):
                continue
            if isinstance(summary[k], (int, float, str)):
                lines.append(f"- {k}: {summary[k]}")

        # ASCII 内存曲线
        lines.append("\n**内存曲线 (ASCII):**\n")
        lines.append("```")
        lines.append(_render_ascii_chart(samples))
        lines.append("```")

        # tracemalloc top10（取最后一个采样点）
        if samples and samples[-1].tracemalloc_top:
            lines.append("\n**tracemalloc top 5 分配点（末次采样）:**\n")
            lines.append("| # | 大小(KB) | 计数 | 来源 |")
            lines.append("|---|---:|---:|---|")
            for i, e in enumerate(samples[-1].tracemalloc_top[:5], 1):
                f = e.file.replace("\\", "/").split("/")[-1]
                lines.append(f"| {i} | {e.size_kb:.1f} | {e.count} | `{f}:{e.line}` |")

    # 重跑指引
    lines.append("\n## 重跑指引\n")
    lines.append("```bash")
    lines.append("# Demo（每场景 30s，适合本地/CI）")
    lines.append("cd D:/work/DriFox && python -m tests.perf.long_run.runner")
    lines.append("")
    lines.append("# Full（每场景 30min，总 90min）")
    lines.append("LONGRUN_FULL=1 python -m tests.perf.long_run.runner")
    lines.append("")
    lines.append("# pytest 入口")
    lines.append("pytest tests/perf/long_run/test_long_run_scenarios.py -v -m perf_long")
    lines.append("```\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[runner] 报告: {out_path}")


def _register_scenarios() -> None:
    """延迟注册：场景函数依赖 PySide6 / app.*，必须在 import 时才导入。"""
    from .scenario_a_message_stream import run_message_stream_scenario
    from .scenario_b_session_switch import run_session_switch_scenario
    from .scenario_c_plugin_hot_reload import run_plugin_hot_reload_scenario

    SCENARIO_REGISTRY["a"] = run_message_stream_scenario
    SCENARIO_REGISTRY["b"] = run_session_switch_scenario
    SCENARIO_REGISTRY["c"] = run_plugin_hot_reload_scenario


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="DriFox 长时压测 runner")
    parser.add_argument(
        "--scenario",
        choices=["a", "b", "c", "all"],
        default="all",
        help="跑哪个场景（默认 all）",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="单场景时长（秒），默认根据 LONGRUN_FULL / LONGRUN_DURATION 自动选",
    )
    args = parser.parse_args(argv)

    _register_scenarios()

    # 优先级：--duration > LONGRUN_DURATION > LONGRUN_FULL → 30/1800
    if args.duration is not None:
        duration = args.duration
    else:
        env_dur = os.environ.get("LONGRUN_DURATION")
        if env_dur:
            try:
                duration = float(env_dur)
            except ValueError:
                duration = FULL_DURATION_SEC if env_full_mode() else DEMO_DURATION_SEC
        else:
            duration = FULL_DURATION_SEC if env_full_mode() else DEMO_DURATION_SEC

    if args.scenario == "all":
        scenarios = ["a", "b", "c"]
    else:
        scenarios = [args.scenario]

    timestamp = _ts()
    summaries = run_all_scenarios(scenarios=scenarios, duration_sec=duration)

    # 报告路径：仓库 docs/perf/long_run_baseline.md
    docs_dir = REPO_ROOT / "docs" / "perf"
    docs_dir.mkdir(parents=True, exist_ok=True)
    report_path = docs_dir / "long_run_baseline.md"
    render_markdown_report(
        summaries,
        out_path=report_path,
        timestamp=timestamp,
        duration_sec=duration,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
