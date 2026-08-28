"""开/关 Tab 循环压测框架：参数化 tab 数量与循环轮数，输出每轮内存增量用于泄露检测。

用法
----
# 内置 QTabWidget 仿真压测（headless offscreen，无需真实 GUI）
uv run python scripts/tab_cycle_bench.py --n-tabs 10 --rounds 5

# 指定输出文件与泄露判定阈值（MB）
uv run python scripts/tab_cycle_bench.py --n-tabs 20 --rounds 3 -o bench.json --leak-threshold 8

# 复用项目真实 TabPanel（在项目内写一段驱动脚本）
from scripts.tab_cycle_bench import TabCycleBench

def open_tab(i: int) -> None:
    panel.add_tab(f"tab-{i}")

def close_tab(i: int) -> None:
    panel.remove_tab(panel.count() - 1)

bench = TabCycleBench(open_tab, close_tab, n_tabs=10, rounds=5)
report = bench.run()   # report 含每轮 delta 与 leak_check 结论
json.dump(report, open("bench.json", "w"))

输出
----
JSON: {"meta": {...}, "rounds": [{"round":1, "open_s":..., "rss_after_open_mb":...,
       "delta_close_mb":...}], "leak_check": {"verdict": "OK|LEAK", "growth_mb": ...}}
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import time
from typing import Any, Callable

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class TabCycleBench:
    """开/关 Tab 循环压测框架。

    open_tab/close_tab 为注入的回调，接收 tab 序号 i（0 基）；传 None 则跳过对应阶段
    （可用于仅测打开或仅测关闭）。每轮结束后强制 gc 再采样 RSS，
    通过"关闭后基线是否逐轮抬升"判定是否存在内存残留。
    """

    def __init__(
        self,
        open_tab: Callable[[int], Any] | None = None,
        close_tab: Callable[[int], Any] | None = None,
        n_tabs: int = 10,
        rounds: int = 3,
        leak_threshold_mb: float = 5.0,
    ) -> None:
        if psutil is None:
            raise RuntimeError("psutil 未安装，请执行 uv sync --group dev")
        self.open_tab = open_tab
        self.close_tab = close_tab
        self.n_tabs = max(1, n_tabs)
        self.rounds = max(1, rounds)
        self.leak_threshold_mb = leak_threshold_mb

    def run(self) -> dict:
        gc.collect()
        base = self._rss_mb()
        results: list[dict] = []
        prev_close = base
        for r in range(1, self.rounds + 1):
            t0 = time.perf_counter()
            for i in range(self.n_tabs):
                if self.open_tab:
                    self.open_tab(i)
            open_s = time.perf_counter() - t0
            gc.collect()
            after_open = self._rss_mb()

            t0 = time.perf_counter()
            for i in range(self.n_tabs):
                if self.close_tab:
                    self.close_tab(i)
            close_s = time.perf_counter() - t0
            gc.collect()
            after_close = self._rss_mb()

            results.append(
                {
                    "round": r,
                    "open_s": round(open_s, 4),
                    "close_s": round(close_s, 4),
                    "rss_after_open_mb": round(after_open, 3),
                    "rss_after_close_mb": round(after_close, 3),
                    "delta_open_mb": round(after_open - base, 3),
                    "delta_close_mb": round(after_close - base, 3),
                    "round_delta_mb": round(after_close - prev_close, 3),
                }
            )
            prev_close = after_close
        return {
            "meta": {
                "pid": os.getpid(),
                "n_tabs": self.n_tabs,
                "rounds": self.rounds,
                "leak_threshold_mb": self.leak_threshold_mb,
            },
            "rounds": results,
            "leak_check": self._leak_check(results),
        }

    def _leak_check(self, results: list[dict]) -> dict:
        if len(results) < 2:
            return {"verdict": "insufficient_data", "growth_mb": 0.0}
        first = results[0]["rss_after_close_mb"]
        last = results[-1]["rss_after_close_mb"]
        growth = last - first
        verdict = "LEAK" if growth > self.leak_threshold_mb else "OK"
        return {"verdict": verdict, "growth_mb": round(growth, 3)}

    @staticmethod
    def _rss_mb() -> float:
        return psutil.Process().memory_info().rss / 1024 / 1024


def _make_qt_callbacks() -> tuple[Callable[[int], None], Callable[[int], None], Callable[[], None]]:
    """构造内置 QTabWidget 仿真回调（headless offscreen），仅供框架演示。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QTabWidget, QWidget

    app = QApplication.instance() or QApplication([])
    widget = QTabWidget()
    widget.show()

    def open_tab(i: int) -> None:
        widget.addTab(QWidget(), f"tab-{i}")

    def close_tab(i: int) -> None:
        if widget.count():
            widget.removeTab(widget.count() - 1)
        app.processEvents()

    def cleanup() -> None:
        widget.deleteLater()
        app.processEvents()

    return open_tab, close_tab, cleanup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="开/关 Tab 循环压测：输出每轮内存增量，用于泄露检测")
    parser.add_argument("--n-tabs", type=int, default=10, help="每轮打开的 tab 数量（默认 10）")
    parser.add_argument("--rounds", type=int, default=3, help="循环轮数（默认 3）")
    parser.add_argument("--leak-threshold", type=float, default=5.0, help="泄露判定阈值 MB（默认 5）")
    parser.add_argument("--output", "-o", default="tab_cycle_bench.json", help="输出 JSON 路径")
    parser.add_argument("--no-qt", action="store_true", help="不带内置 QTabWidget 仿真（仅测框架开销）")
    args = parser.parse_args(argv)

    open_tab = close_tab = None
    cleanup: Callable[[], None] | None = None
    if not args.no_qt:
        try:
            open_tab, close_tab, cleanup = _make_qt_callbacks()
        except ImportError:
            print("警告: 未找到 PySide6，回退到 --no-qt 模式（仅测框架开销）")
            args.no_qt = True

    bench = TabCycleBench(
        open_tab=open_tab,
        close_tab=close_tab,
        n_tabs=args.n_tabs,
        rounds=args.rounds,
        leak_threshold_mb=args.leak_threshold,
    )
    report = bench.run()
    if cleanup:
        cleanup()
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
