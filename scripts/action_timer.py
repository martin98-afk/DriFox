"""通用耗时测量工具：装饰器 / 上下文管理器，可插桩测量任意阶段耗时（如 tab 打开各阶段）。

用法
----
# 上下文管理器（推荐：测量 tab 打开各阶段）
from scripts.action_timer import timer, dump_timings

with timer("tab.open.create_widget"):
    w = create_widget()
with timer("tab.open.rebuild_layout"):
    panel._rebuild_team_layout()
dump_timings("timings.json")

# 装饰器
from scripts.action_timer import timed

@timed("chat.send_message")
def send(...):
    ...

# CLI 自测（输出示例 JSON）
uv run python scripts/action_timer.py

输出
----
JSON: {"阶段名": {"calls": n, "total_s": ..., "avg_s": ..., "min_s": ..., "max_s": ...}}
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator

_TIMINGS: dict[str, list[float]] = {}
_LOCK = threading.Lock()


def _record(name: str, seconds: float) -> None:
    with _LOCK:
        _TIMINGS.setdefault(name, []).append(seconds)


@contextmanager
def timer(name: str) -> Iterator[None]:
    """上下文管理器：记录代码块耗时到全局统计表。"""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _record(name, time.perf_counter() - t0)


def timed(name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器：记录函数每次调用的耗时，name 缺省用 模块.函数名。"""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        label = name or f"{func.__module__}.{func.__qualname__}"

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                _record(label, time.perf_counter() - t0)

        return wrapper

    return decorator


def timings() -> dict[str, dict]:
    """导出聚合统计：次数 / 总耗时 / 平均 / 最小 / 最大（秒）。"""
    with _LOCK:
        raw = {k: list(v) for k, v in _TIMINGS.items()}
    out: dict[str, dict] = {}
    for name, values in raw.items():
        total = sum(values)
        out[name] = {
            "calls": len(values),
            "total_s": round(total, 6),
            "avg_s": round(total / len(values), 6),
            "min_s": round(min(values), 6),
            "max_s": round(max(values), 6),
        }
    return out


def dump_timings(path: str | None = None) -> dict:
    """输出统计到 JSON 文件（path 为 None 时仅返回数据）。"""
    data = timings()
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    return data


def clear() -> None:
    """清空全局统计（测试隔离用）。"""
    with _LOCK:
        _TIMINGS.clear()


def _self_test(output: str) -> int:
    @timed("demo.decorated_func")
    def work(n: int) -> int:
        time.sleep(0.01)
        return n

    for i in range(5):
        with timer("demo.context_phase"):
            work(i)
    data = dump_timings(output)
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通用耗时测量工具：装饰器/上下文管理器 + 统计导出")
    parser.add_argument("--output", "-o", default="action_timer_self_test.json", help="自测输出 JSON 路径")
    args = parser.parse_args(argv)
    return _self_test(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
