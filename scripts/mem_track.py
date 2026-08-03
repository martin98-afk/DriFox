"""内存跟踪脚本：tracemalloc + 进程 RSS 周期采样，输出随时间的内存曲线 JSON。

用法
----
# 自测/演示：对自身进程跑一段模拟负载并采样（无需 GUI）
uv run python scripts/mem_track.py --self-test --interval 0.05 --duration 3

# 监控外部进程（pid 1234），每 0.5s 采样一次，持续 30s
uv run python scripts/mem_track.py --pid 1234 --interval 0.5 --duration 30 -o mem.json

# 对象级泄漏定位：--objects 输出 start/end 两次对象类型统计快照（需 pympler）
uv run python scripts/mem_track.py --self-test --objects --objects-top 20 -o mem.json

# 作为库在代码中插桩（测量 tab 打开/关闭的内存曲线）
from scripts.mem_track import MemoryTracker
tracker = MemoryTracker(interval=0.1)
tracker.start()
panel.add_tab("x")          # 被测代码
panel.remove_tab(0)
tracker.stop()
tracker.save("mem.json")

输出
----
JSON: {"meta": {...}, "samples": [{"t": 0.0, "rss_mb": ..., "traced_mb": ..., "peak_mb": ...}],
       "object_snapshots": {"start": [...], "end": [...]}}
- rss_mb:    进程 RSS（psutil，MB）
- traced_mb: tracemalloc 当前已跟踪堆（MB，仅监控自身进程时有意义）
- peak_mb:   tracemalloc 峰值堆（MB，仅监控自身进程时有意义）
- object_snapshots: pympler muppy 对象类型统计（--objects 时存在），每项
  {"type": 类名, "count": 数量, "size_bytes": 总内存字节}，按内存降序
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import tracemalloc

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class MemoryTracker:
    """周期采样 RSS 与 tracemalloc 堆内存的跟踪器。

    pid 为 None 时监控自身进程并启用 tracemalloc；指定 pid 时仅采样 RSS
    （tracemalloc 只能跟踪启用它的进程）。
    """

    def __init__(
        self,
        pid: int | None = None,
        interval: float = 0.1,
        track_objects: bool = False,
        objects_top: int = 20,
    ) -> None:
        if psutil is None:
            raise RuntimeError("psutil 未安装，请执行 uv sync --group dev")
        self.pid = pid
        self.interval = max(interval, 0.01)
        self.track_objects = track_objects
        self.objects_top = max(1, objects_top)
        self.object_snapshots: dict[str, list[dict]] = {}
        self._samples: list[dict] = []
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_ts = 0.0
        self._traced = False
        self._traced_at_start = False

    def start(self) -> None:
        """启动采样线程；pid 为 None 时同时启动 tracemalloc。"""
        if self._thread is not None:
            return
        if self.pid is None:
            tracemalloc.start()
            self._traced = True
            self._traced_at_start = True
            if self.track_objects:
                self.object_snapshots["start"] = self.snapshot_objects(self.objects_top)
        self._start_ts = time.monotonic()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, name="mem-track", daemon=True)
        self._thread.start()

    def stop(self) -> list[dict]:
        """停止采样并返回全部样本；同时停止 tracemalloc。"""
        if self._thread is not None:
            self._stop_evt.set()
            self._thread.join(timeout=max(self.interval * 2, 1.0))
            self._thread = None
        if self._traced:
            tracemalloc.stop()
            self._traced = False
            if self.track_objects:
                self.object_snapshots["end"] = self.snapshot_objects(self.objects_top)
        return self.samples

    def snapshot_objects(self, top: int = 20) -> list[dict]:
        """用 pympler muppy 拍摄对象类型统计快照（按总内存降序，单位字节）。

        供 perf-analyzer 定位"关闭 tab 后哪些对象未释放"：对比 start/end 快照中
        数量明显增长的类型即疑似残留对象。
        """
        try:
            from pympler import muppy, summary
        except ImportError as exc:
            raise RuntimeError("对象快照需要 pympler，请执行 uv add --group dev pympler") from exc
        rows = summary.summarize(muppy.get_objects())
        rows.sort(key=lambda row: row[2], reverse=True)
        return [
            {"type": name, "count": count, "size_bytes": size}
            for name, count, size in rows[:top]
        ]

    @property
    def samples(self) -> list[dict]:
        with self._lock:
            return list(self._samples)

    def save(self, path: str) -> None:
        """将采样结果写入 JSON 文件。"""
        data = {
            "meta": {
                "pid": self.pid if self.pid is not None else os.getpid(),
                "interval_s": self.interval,
                "tracemalloc": self._traced_at_start,
                "objects_tracked": bool(self.object_snapshots),
                "sample_count": len(self._samples),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "samples": self.samples,
            "object_snapshots": self.object_snapshots if self.object_snapshots else None,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            self._sample_once()
            self._stop_evt.wait(self.interval)

    def _sample_once(self) -> None:
        proc = psutil.Process(self.pid)
        rss_mb = proc.memory_info().rss / 1024 / 1024
        traced_mb = peak_mb = 0.0
        if self._traced:
            cur, peak = tracemalloc.get_traced_memory()
            traced_mb = cur / 1024 / 1024
            peak_mb = peak / 1024 / 1024
        with self._lock:
            self._samples.append(
                {
                    "t": round(time.monotonic() - self._start_ts, 4),
                    "rss_mb": round(rss_mb, 3),
                    "traced_mb": round(traced_mb, 3),
                    "peak_mb": round(peak_mb, 3),
                }
            )


def _self_test_load(duration: float) -> None:
    """模拟负载：不断分配/释放大列表，制造可见的锯齿内存曲线。"""
    chunks: list[list[int]] = []
    end = time.monotonic() + duration
    while time.monotonic() < end:
        chunks.append(list(range(100_000)))
        if len(chunks) > 8:
            chunks.pop(0)
        time.sleep(0.02)


def _print_summary(samples: list[dict], path: str, object_snapshots: dict | None = None) -> None:
    if not samples:
        print("无采样数据")
        return
    rss = [s["rss_mb"] for s in samples]
    print(f"采样 {len(samples)} 点 -> {path}")
    print(f"RSS   min={min(rss):.2f} MB  max={max(rss):.2f} MB  delta={rss[-1] - rss[0]:+.2f} MB")
    traced = [s["traced_mb"] for s in samples]
    if any(t > 0 for t in traced):
        print(f"TRACED min={min(traced):.2f} MB  max={max(traced):.2f} MB  delta={traced[-1] - traced[0]:+.2f} MB")
    if object_snapshots:
        for label, rows in object_snapshots.items():
            brief = ", ".join(f"{r['type']}x{r['count']}({r['size_bytes']}B)" for r in rows[:3])
            print(f"OBJECTS[{label}] top3: {brief}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="内存跟踪：tracemalloc + 进程 RSS 周期采样")
    parser.add_argument("--pid", type=int, default=None, help="监控的进程 PID（默认监控自身并启用 tracemalloc）")
    parser.add_argument("--interval", type=float, default=0.1, help="采样间隔秒（默认 0.1）")
    parser.add_argument("--duration", type=float, default=10.0, help="采样时长秒（默认 10）")
    parser.add_argument("--output", "-o", default="mem_curve.json", help="输出 JSON 路径（默认 mem_curve.json）")
    parser.add_argument("--self-test", action="store_true", help="对自身跑模拟负载后采样（演示/自测）")
    parser.add_argument("--objects", action="store_true", help="启用 pympler 对象类型统计快照（start/end 各一次，仅自身进程）")
    parser.add_argument("--objects-top", type=int, default=20, help="对象快照保留 top N 类型（默认 20）")
    args = parser.parse_args(argv)

    if args.objects and args.pid is not None:
        print("警告: --objects 仅对自身进程有效，--pid 已指定，忽略对象快照")
        args.objects = False

    tracker = MemoryTracker(
        pid=args.pid,
        interval=args.interval,
        track_objects=args.objects,
        objects_top=args.objects_top,
    )
    tracker.start()
    try:
        if args.self_test:
            _self_test_load(args.duration)
        else:
            time.sleep(args.duration)
    finally:
        samples = tracker.stop()
    tracker.save(args.output)
    _print_summary(samples, args.output, tracker.object_snapshots)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
