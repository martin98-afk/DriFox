# -*- coding: utf-8 -*-
"""基准 1b：importtime 导入耗时 Top-N

两种模式：
1. 引用既有 _importtime_before.txt（主程序启动 -X importtime 抓取）
2. 重新采集：uv run python -X importtime main.py --bench-importtime-exit

运行：
  uv run python benchmarks/bench_importtime.py                 # 解析已有文件
  uv run python benchmarks/bench_importtime.py --top 20
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_common as bc  # noqa: E402

DEFAULT_SRC = Path(bc.PROJECT_ROOT) / "_importtime_before.txt"
LINE_RE = re.compile(r"import time:\s+(\d+) \|\s+(\d+) \| (\s*)(\S+)")


def parse(path: Path) -> list:
    """返回 [{module, self_us, cum_us, depth}]，按累计耗时降序。"""
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        self_us, cum_us, indent, name = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        rows.append(
            {
                "module": name,
                "self_ms": round(self_us / 1000, 1),
                "cum_ms": round(cum_us / 1000, 1),
                "depth": len(indent) // 2,
            }
        )
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"[bench] 找不到 {src}，请先采集：uv run python -X importtime main.py > _importtime_before.txt 2>&1")
        sys.exit(1)

    rows = parse(src)
    total_us = sum(r["self_ms"] for r in rows)
    top_cum = sorted(rows, key=lambda r: -r["cum_ms"])[: args.top]
    top_self = sorted(rows, key=lambda r: -r["self_ms"])[: args.top]

    # 聚合顶层包（depth=0 直接导入项太少，按一级包名聚合）
    agg = {}
    for r in rows:
        top_pkg = r["module"].split(".")[0]
        agg[top_pkg] = round(agg.get(top_pkg, 0.0) + r["self_ms"], 1)
    top_pkgs = sorted(agg.items(), key=lambda kv: -kv[1])[: args.top]

    result = {
        "metric": "importtime",
        "src": str(src),
        "total_self_ms": round(total_us, 1),
        "top_cumulative": top_cum,
        "top_self": top_self,
        "top_packages": [{"package": k, "self_ms": v} for k, v in top_pkgs],
    }
    print(f"导入总耗时(self 合计): {result['total_self_ms']:.0f} ms")
    print(f"\n===== Top{args.top} 按累计耗时(cumulative) =====")
    for r in top_cum:
        print(f"  {r['cum_ms']:>8.1f} ms  {'  ' * r['depth']}{r['module']}")
    print(f"\n===== Top{args.top} 按自身耗时(self) =====")
    for r in top_self:
        print(f"  {r['self_ms']:>8.1f} ms  {r['module']}")
    print(f"\n===== Top{args.top} 一级包聚合(self) =====")
    for k, v in top_pkgs:
        print(f"  {v:>8.1f} ms  {k}")
    bc.save_result("importtime", result)


if __name__ == "__main__":
    main()
