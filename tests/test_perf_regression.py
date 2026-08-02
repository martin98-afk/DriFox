"""性能回归测试脚手架：开/关 tab 循环后的内存泄露检测。

用法
----
# 默认跳过（未设置 PERF_TESTS），正常跑测试不受影响
pytest tests/test_perf_regression.py -v            # 全部 skip

# 启用性能回归测试
PERF_TESTS=1 pytest tests/test_perf_regression.py -v

# 自定义泄露阈值（MB）
PERF_TESTS=1 PERF_LEAK_THRESHOLD_MB=8 pytest tests/test_perf_regression.py

说明
----
- 集成 scripts/tab_cycle_bench.TabCycleBench 与 scripts/mem_track.MemoryTracker
- 阈值缺省 10MB，待子任务 #2 基线数据出来后校准（perf-tester）
- tab 用轻量内存对象模拟（无需 GUI），确保常规测试环境稳定
"""

from __future__ import annotations

import os

import pytest

from scripts.mem_track import MemoryTracker
from scripts.tab_cycle_bench import TabCycleBench

pytestmark = [
    pytest.mark.perf,  # 性能基准测试标记（已在 pyproject.toml 注册）
    pytest.mark.skipif(
        os.environ.get("PERF_TESTS") != "1",
        reason="性能回归测试需设置环境变量 PERF_TESTS=1 才运行",
    ),
]

# 泄露阈值（MB）：缺省 10MB，待子任务 #2 基线数据出来后校准
LEAK_THRESHOLD_MB = float(os.environ.get("PERF_LEAK_THRESHOLD_MB", "10"))


class _FakeTab:
    """模拟一个占内存的 tab（约 1MB 独占 payload），close 后释放引用。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.payload = bytearray(1024 * 1024)  # 每 tab 独占 1MB，放大 RSS 变化


def _make_tab_callbacks() -> tuple:
    """构造开/关 tab 回调：open 创建 _FakeTab 入表，close 弹出并释放引用。"""
    tabs: list[_FakeTab] = []

    def open_tab(i: int) -> None:
        tabs.append(_FakeTab(f"tab-{i}"))

    def close_tab(i: int) -> None:
        if tabs:
            tabs.pop()

    return open_tab, close_tab


@pytest.mark.parametrize("n_tabs,rounds", [(4, 2), (8, 3)])
def test_tab_cycle_no_leak(n_tabs: int, rounds: int) -> None:
    """打开 N 个 tab → 逐个关闭 → 多轮循环后，RSS 相对基线抬升应低于阈值。"""
    open_tab, close_tab = _make_tab_callbacks()
    bench = TabCycleBench(
        open_tab=open_tab,
        close_tab=close_tab,
        n_tabs=n_tabs,
        rounds=rounds,
        leak_threshold_mb=LEAK_THRESHOLD_MB,
    )

    # 集成 mem_track：循环期间同步采样 RSS/tracemalloc 曲线
    tracker = MemoryTracker(interval=0.02)
    tracker.start()
    try:
        report = bench.run()
    finally:
        tracker.stop()

    # mem_track 集成有效性：采样曲线非空
    assert tracker.samples, "mem_track 未采集到任何采样点"

    first_close = report["rounds"][0]["rss_after_close_mb"]
    last_close = report["rounds"][-1]["rss_after_close_mb"]
    growth = last_close - first_close

    assert report["leak_check"]["verdict"] == "OK", f"疑似内存泄露: {report['leak_check']}"
    assert growth < LEAK_THRESHOLD_MB, (
        f"关闭全部 tab 后 RSS 抬升 {growth:.2f}MB 超过阈值 {LEAK_THRESHOLD_MB}MB "
        f"(n_tabs={n_tabs}, rounds={rounds})"
    )


def test_mem_track_objects_snapshot() -> None:
    """pympler 对象快照：start/end 各一次，供 perf-analyzer 定位未释放对象。"""
    tracker = MemoryTracker(interval=0.02, track_objects=True, objects_top=10)
    tracker.start()
    try:
        open_tab, close_tab = _make_tab_callbacks()
        bench = TabCycleBench(open_tab, close_tab, n_tabs=4, rounds=1)
        bench.run()
    finally:
        tracker.stop()

    assert "start" in tracker.object_snapshots
    assert "end" in tracker.object_snapshots
    start_rows = tracker.object_snapshots["start"]
    end_rows = tracker.object_snapshots["end"]
    assert isinstance(start_rows, list) and start_rows
    assert isinstance(end_rows, list) and end_rows
    # 每行包含类型/数量/内存字段
    for row in end_rows:
        assert {"type", "count", "size_bytes"} <= set(row)
