# -*- coding: utf-8 -*-
"""基准测试公共工具

所有基准脚本共用：
- 数据隔离：开发环境 get_app_data_dir() = cwd/.drifox（含 sessions.db），
  基准运行前 chdir 到独立临时目录，避免污染真实用户数据。
- 采样：psutil RSS + tracemalloc
- 分析：线性回归斜率、Top-N 大对象

运行方式：uv run python benchmarks/bench_xxx.py
"""

from __future__ import annotations

import gc
import json
import os
import platform
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 确保可导入项目包（无论从哪个 cwd 启动）
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 基准结果输出目录（benchmarks/results/，不进 git 数据库）
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def setup_isolation(tag: str) -> Path:
    """chdir 到独立临时目录，隔离 .drifox 用户数据（sessions.db/插件/配置）。

    必须在任何 app.* 模块导入【之前】调用（get_app_data_dir 按需解析，
    SessionStore/PluginManager 单例首次 get_instance 时才锁定路径）。
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"drifox_bench_{tag}_"))
    os.chdir(tmp)
    (tmp / ".drifox").mkdir(exist_ok=True)
    return tmp


def env_info() -> dict:
    """采集运行环境（保证可复现）。"""
    import psutil

    return {
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpu": platform.processor() or f"{os.cpu_count()} cores",
        "cpu_count": os.cpu_count(),
        "mem_total_gb": round(psutil.virtual_memory().total / 1024**3, 1),
        "cwd": os.getcwd(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def rss_mb() -> float:
    """当前进程 RSS（MB）。"""
    import psutil

    return psutil.Process().memory_info().rss / 1024**2


def private_mb() -> float:
    """当前进程私有内存（Windows: privatebytes / 其他: USS 可得时）。"""
    import psutil

    try:
        m = psutil.Process().memory_full_info()
        v = getattr(m, "uss", None) or getattr(m, "private", None)
        if v:
            return v / 1024**2
    except Exception:
        pass
    return rss_mb()


def full_gc(rounds: int = 2):
    """彻底 GC（含分代 + finalize）。"""
    for _ in range(rounds):
        gc.collect()


def tracemalloc_top(n: int = 20) -> list:
    """tracemalloc 当前 Top-N 分配点（按累计大小）。"""
    snap = tracemalloc.take_snapshot()
    out = []
    for stat in snap.statistics("lineno")[:n]:
        frame = stat.traceback[0]
        out.append(
            {
                "loc": f"{Path(frame.filename).name}:{frame.lineno}",
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            }
        )
    return out


def tracemalloc_current_mb() -> float:
    cur, _peak = tracemalloc.get_traced_memory()
    return cur / 1024**2


def tracemalloc_diff(snap1, snap2, n: int = 15) -> list:
    """两个 snapshot 对比：新增分配 Top-N（按增长字节，过滤负增长）。"""
    diffs = snap2.compare_to(snap1, "lineno")
    out = []
    for stat in diffs[: 3 * n]:
        if stat.size <= 0:
            continue
        frame = stat.traceback[0]
        out.append(
            {
                "loc": f"{Path(frame.filename).name}:{frame.lineno}",
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            }
        )
        if len(out) >= n:
            break
    return out


def slope(xs: list, ys: list) -> tuple:
    """最小二乘线性回归：返回 (斜率, R²)。"""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0:
        return 0.0, 0.0
    b = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 1.0
    return b, r2


def save_result(name: str, data: dict):
    """结果落盘 benchmarks/results/<name>.json。"""
    data.setdefault("_env", env_info())
    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[bench] 结果已写入 {path}")
    return path


def spin_qt_events(qapp, total_ms: int, step_ms: int = 50):
    """在无 exec_() 的情况下泵 Qt 事件循环 total_ms 毫秒（驱动 QTimer 延迟任务）。"""
    end = time.perf_counter() + total_ms / 1000
    while time.perf_counter() < end:
        qapp.processEvents()
        remaining = end - time.perf_counter()
        time.sleep(min(step_ms / 1000, max(remaining, 0)))


def leak_verdict(kb_per_iter: float, r2: float, threshold_kb: float = 10.0) -> str:
    """泄漏判定：斜率显著为正 → 泄漏嫌疑。"""
    if kb_per_iter > threshold_kb and r2 > 0.7:
        return f"疑似泄漏：+{kb_per_iter:.1f} KB/次 (R²={r2:.3f})"
    if kb_per_iter > threshold_kb:
        return f"增长但线性度低：+{kb_per_iter:.1f} KB/次 (R²={r2:.3f})，可能为缓存填充"
    return f"无显著泄漏：{kb_per_iter:+.1f} KB/次 (R²={r2:.3f})"
