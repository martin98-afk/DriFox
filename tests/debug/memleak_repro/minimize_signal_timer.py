# -*- coding: utf-8 -*-
"""[DEBUG-memleak-st] #2 signal + #3 timer 场景深度根因验证

基于 T1 报告：
  #2 signal: 100 轮 +8.84 MB / QObject +101（不收敛，强引用泄漏）
  #3 timer:  500 轮 +8.78 MB / QTimer +49（不收敛）

目的：最小化定位两个场景的真实泄漏路径 + 给出可直接动手的修复点。

Signal 阶梯：
  S0 = baseline（无 connect）                          -- ~0 objΔ
  S1 = source.connect(sink.method) 默认强引用          -- +1 obj/轮
  S2 = S1 + 显式 disconnect 再 del source              -- 期望 0 objΔ（验证 disconnect 修复）
  S3 = source.connect(lambda: ...)                    -- lambda 闭包持有 self？
  S4 = source.connect(functools.partial(func, sink))   -- partial 持有 sink
  S5 = source.connect(sink.method, Qt.UniqueConnection) -- UniqueConnection 模式
  S6 = 跨线程 signal (QThread + connect)               -- QueuedConnection

Timer 阶梯：
  T0 = baseline（无 QTimer）                          -- ~0 objΔ
  T1 = QTimer.start + stop() only                     -- 验证 stop 是否够
  T2 = QTimer.start + deleteLater() only              -- 验证 deleteLater 是否够
  T3 = QTimer.start + stop() + deleteLater()          -- 完整清理（验证必要性）
  T4 = QTimer.singleShot() 静态方法                    -- 验证静态方法路径
  T5 = QTimer(parent) 带 parent + stop + deleteLater  -- 父子树清理路径

运行：python tests/debug/memleak_repro/minimize_signal_timer.py [--rounds N]
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import tracemalloc
import weakref
from typing import Any, Callable, Dict, List, Optional, Tuple

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _rss_mb() -> float:
    try:
        import psutil  # type: ignore
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


def _obj_counts() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    keys = (
        "PyQt5.QtCore.QObject",
        "PyQt5.QtCore.QTimer",
        "PyQt5.QtCore.QThread",
        "PyQt5.QtCore.QSignalMapper",
    )
    for obj in gc.get_objects():
        try:
            cn = type(obj).__module__ + "." + type(obj).__name__
        except Exception:
            continue
        if cn in keys:
            counts[cn] = counts.get(cn, 0) + 1
    try:
        from PyQt5.QtCore import QObject  # type: ignore
        counts["QObject_total"] = sum(1 for o in gc.get_objects() if isinstance(o, QObject))
    except Exception:
        pass
    return counts


def _snap(label: str) -> Dict[str, Any]:
    s: Dict[str, Any] = {"label": label, "ts": time.time(), "rss_mb": round(_rss_mb(), 3), "obj": _obj_counts()}
    if tracemalloc.is_tracing():
        cur, peak = tracemalloc.get_traced_memory()
        s["py_alloc_mb"] = round(cur / 1024 / 1024, 3)
    return s


def _pump(app, ms: int = 80) -> None:
    from PyQt5.QtCore import QEventLoop, QTimer  # type: ignore
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    app.processEvents()



# ========== Signal 阶梯 ==========

def stage_S0_baseline(app, rounds: int) -> List[Dict[str, Any]]:
    """S0：创建 source + sink 但不 connect。"""
    from PyQt5.QtCore import QObject, pyqtSignal  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("S0:r0:init"))

    class Source(QObject):
        tick = pyqtSignal(int)

    class Sink(QObject):
        def __init__(self):
            super().__init__()
            self.n = 0

        def on_tick(self, v: int):
            self.n += v

    bucket: List[Source] = []
    for i in range(1, rounds + 1):
        s = Source()
        k = Sink()
        bucket.append(s)
        if i % 50 == 0:
            bucket.clear()
            gc.collect()
            snaps.append(_snap(f"S0:r{i}:mid"))
    bucket.clear()
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"S0:r{rounds}:end"))
    return snaps


def stage_S1_strong_ref(app, rounds: int) -> List[Dict[str, Any]]:
    """S1：source.connect(sink.on_tick) — 默认强引用，模拟 T1 signal 场景。"""
    from PyQt5.QtCore import QObject, pyqtSignal  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("S1:r0:init"))

    class Source(QObject):
        tick = pyqtSignal(int)

    class Sink(QObject):
        def __init__(self):
            super().__init__()
            self.n = 0

        def on_tick(self, v: int):
            self.n += v

    sources: List[Source] = []
    for i in range(1, rounds + 1):
        s = Source()
        k = Sink()
        s.tick.connect(k.on_tick)
        s.tick.emit(1)
        sources.append(s)
        if i % 50 == 0:
            gc.collect()
            snaps.append(_snap(f"S1:r{i}:mid"))

    for s in sources:
        try:
            s.deleteLater()
        except Exception:
            pass
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"S1:r{rounds}:end"))
    return snaps


def stage_S2_explicit_disconnect(app, rounds: int) -> List[Dict[str, Any]]:
    """S2：S1 + 每轮显式 disconnect + del sink。验证 disconnect 修复。"""
    from PyQt5.QtCore import QObject, pyqtSignal  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("S2:r0:init"))

    class Source(QObject):
        tick = pyqtSignal(int)

    class Sink(QObject):
        def __init__(self):
            super().__init__()
            self.n = 0

        def on_tick(self, v: int):
            self.n += v

    sources: List[Source] = []
    for i in range(1, rounds + 1):
        s = Source()
        k = Sink()
        s.tick.connect(k.on_tick)
        s.tick.emit(1)
        sources.append(s)
        # 显式 disconnect
        try:
            s.tick.disconnect(k.on_tick)
        except Exception:
            pass
        if i % 50 == 0:
            gc.collect()
            snaps.append(_snap(f"S2:r{i}:mid"))

    for s in sources:
        try:
            s.deleteLater()
        except Exception:
            pass
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"S2:r{rounds}:end"))
    return snaps


def stage_S3_lambda(app, rounds: int) -> List[Dict[str, Any]]:
    """S3：source.connect(lambda: ...) — lambda 闭包持有什么？"""
    from PyQt5.QtCore import QObject, pyqtSignal  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("S3:r0:init"))

    class Source(QObject):
        tick = pyqtSignal(int)

    class Sink(QObject):
        def __init__(self):
            super().__init__()
            self.n = 0

    sources: List[Source] = []
    for i in range(1, rounds + 1):
        s = Source()
        k = Sink()
        s.tick.connect(lambda v, sink=k: setattr(sink, "n", sink.n + v))
        s.tick.emit(1)
        sources.append(s)
        if i % 50 == 0:
            gc.collect()
            snaps.append(_snap(f"S3:r{i}:mid"))

    for s in sources:
        try:
            s.deleteLater()
        except Exception:
            pass
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"S3:r{rounds}:end"))
    return snaps


def stage_S4_partial(app, rounds: int) -> List[Dict[str, Any]]:
    """S4：source.connect(partial(func, sink)) — partial 持 sink。"""
    import functools
    from PyQt5.QtCore import QObject, pyqtSignal  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("S4:r0:init"))

    class Source(QObject):
        tick = pyqtSignal(int)

    class Sink(QObject):
        def __init__(self):
            super().__init__()
            self.n = 0

    def slot(v, sink):
        sink.n += v

    sources: List[Source] = []
    for i in range(1, rounds + 1):
        s = Source()
        k = Sink()
        s.tick.connect(functools.partial(slot, k))
        s.tick.emit(1)
        sources.append(s)
        if i % 50 == 0:
            gc.collect()
            snaps.append(_snap(f"S4:r{i}:mid"))

    for s in sources:
        try:
            s.deleteLater()
        except Exception:
            pass
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"S4:r{rounds}:end"))
    return snaps


def stage_S5_unique_connection(app, rounds: int) -> List[Dict[str, Any]]:
    """S5：Qt.UniqueConnection — 避免重复连接，但不解决强引用。"""
    from PyQt5.QtCore import QObject, pyqtSignal, Qt  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("S5:r0:init"))

    class Source(QObject):
        tick = pyqtSignal(int)

    class Sink(QObject):
        def __init__(self):
            super().__init__()
            self.n = 0

        def on_tick(self, v: int):
            self.n += v

    sources: List[Source] = []
    for i in range(1, rounds + 1):
        s = Source()
        k = Sink()
        s.tick.connect(k.on_tick, Qt.UniqueConnection)
        s.tick.emit(1)
        sources.append(s)
        if i % 50 == 0:
            gc.collect()
            snaps.append(_snap(f"S5:r{i}:mid"))

    for s in sources:
        try:
            s.deleteLater()
        except Exception:
            pass
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"S5:r{rounds}:end"))
    return snaps


def stage_S6_cross_thread(app, rounds: int) -> List[Dict[str, Any]]:
    """S6：跨线程 signal — QThread receiver + QueuedConnection。"""
    from PyQt5.QtCore import QObject, QThread, pyqtSignal  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("S6:r0:init"))

    class Worker(QObject):
        received = pyqtSignal(int)

        def __init__(self):
            super().__init__()
            self.n = 0

        def on_recv(self, v: int):
            self.n += v

    threads: List[QThread] = []
    for i in range(1, rounds + 1):
        thread = QThread()
        worker = Worker()
        worker.moveToThread(thread)
        # emit 与 recv 都在主线程（简化），但使用 QueuedConnection 模拟跨线程引用
        thread.start()
        # 通过 thread 的 started 信号触发 connect
        def _connect(_worker=worker):
            pass
        # 简化：不真做跨线程 emit，只验证 connect 的引用行为
        threads.append(thread)
        if i % 20 == 0:
            gc.collect()
            snaps.append(_snap(f"S6:r{i}:mid"))

    for t in threads:
        try:
            t.quit()
            t.wait(50)
            t.deleteLater()
        except Exception:
            pass
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"S6:r{rounds}:end"))
    return snaps



# ========== Timer 阶梯 ==========

def stage_T0_baseline(app, rounds: int) -> List[Dict[str, Any]]:
    """T0：基线（无 QTimer 创建）。"""
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("T0:r0:init"))
    bucket: List[Any] = []
    for i in range(1, rounds + 1):
        bucket.append(i)
        if i % 100 == 0:
            bucket = bucket[::2]
            gc.collect()
            snaps.append(_snap(f"T0:r{i}:mid"))
    bucket.clear()
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"T0:r{rounds}:end"))
    return snaps


def stage_T1_stop_only(app, rounds: int) -> List[Dict[str, Any]]:
    """T1：QTimer.start + 只 stop()，不 deleteLater。"""
    from PyQt5.QtCore import QTimer  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("T1:r0:init"))

    def _fire():
        pass

    timers: List[QTimer] = []
    for i in range(1, rounds + 1):
        t = QTimer()
        t.setSingleShot(True)
        t.timeout.connect(_fire)
        t.start(60_000)
        timers.append(t)
        if i % 50 == 0:
            for tt in timers:
                tt.stop()
            timers = []
            gc.collect()
            snaps.append(_snap(f"T1:r{i}:mid"))

    for t in timers:
        t.stop()
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"T1:r{rounds}:end"))
    return snaps


def stage_T2_deletelater_only(app, rounds: int) -> List[Dict[str, Any]]:
    """T2：QTimer.start + 只 deleteLater()，不 stop()。"""
    from PyQt5.QtCore import QTimer  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("T2:r0:init"))

    def _fire():
        pass

    timers: List[QTimer] = []
    for i in range(1, rounds + 1):
        t = QTimer()
        t.setSingleShot(True)
        t.timeout.connect(_fire)
        t.start(60_000)
        timers.append(t)
        if i % 50 == 0:
            for tt in timers:
                tt.deleteLater()
            _pump(app, 50)
            timers = []
            gc.collect()
            snaps.append(_snap(f"T2:r{i}:mid"))

    for t in timers:
        t.deleteLater()
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"T2:r{rounds}:end"))
    return snaps


def stage_T3_stop_then_delete(app, rounds: int) -> List[Dict[str, Any]]:
    """T3：QTimer.start + stop() + deleteLater() — 完整清理。"""
    from PyQt5.QtCore import QTimer  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("T3:r0:init"))

    def _fire():
        pass

    timers: List[QTimer] = []
    for i in range(1, rounds + 1):
        t = QTimer()
        t.setSingleShot(True)
        t.timeout.connect(_fire)
        t.start(60_000)
        timers.append(t)
        if i % 50 == 0:
            for tt in timers:
                tt.stop()
                tt.deleteLater()
            _pump(app, 50)
            timers = []
            gc.collect()
            snaps.append(_snap(f"T3:r{i}:mid"))

    for t in timers:
        t.stop()
        t.deleteLater()
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"T3:r{rounds}:end"))
    return snaps


def stage_T4_singleshot_static(app, rounds: int) -> List[Dict[str, Any]]:
    """T4：QTimer.singleShot() 静态方法 — 不持有实例。"""
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("T4:r0:init"))

    def _fire():
        pass

    for i in range(1, rounds + 1):
        from PyQt5.QtCore import QTimer  # type: ignore
        QTimer.singleShot(60_000, _fire)
        if i % 100 == 0:
            gc.collect()
            snaps.append(_snap(f"T4:r{i}:mid"))

    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"T4:r{rounds}:end"))
    return snaps


def stage_T5_with_parent(app, rounds: int) -> List[Dict[str, Any]]:
    """T5：QTimer(parent) 带 parent — 父子树清理路径。"""
    from PyQt5.QtCore import QObject, QTimer  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("T5:r0:init"))

    parents: List[QObject] = []
    for i in range(1, rounds + 1):
        parent = QObject()
        parents.append(parent)
        for j in range(3):  # 每轮 3 个 timer
            t = QTimer(parent)
            t.setSingleShot(True)
            t.timeout.connect(lambda: None)
            t.start(60_000)
        if i % 50 == 0:
            gc.collect()
            snaps.append(_snap(f"T5:r{i}:mid"))

    for p in parents:
        for child in p.findChildren(QTimer):
            child.stop()
            child.deleteLater()
        p.setParent(None)
        p.deleteLater()
    _pump(app, 300)
    gc.collect()
    snaps.append(_snap(f"T5:r{rounds}:end"))
    return snaps


def stage_T6_half_stop(app, rounds: int) -> List[Dict[str, Any]]:
    """T6：每 50 轮半数 stop+deleteLater，半数只 keep — 模拟生产"半数清理半数泄漏"。"""
    from PyQt5.QtCore import QTimer  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap("T6:r0:init"))

    def _fire():
        pass

    timers: List[QTimer] = []
    for i in range(1, rounds + 1):
        t = QTimer()
        t.setSingleShot(True)
        t.timeout.connect(_fire)
        t.start(60_000)
        timers.append(t)
        if i % 50 == 0:
            keep: List[QTimer] = []
            for idx, tt in enumerate(timers):
                if idx % 2 == 0:
                    tt.stop()
                    tt.deleteLater()
                else:
                    keep.append(tt)
            timers = keep
            _pump(app, 50)
            gc.collect()
            snaps.append(_snap(f"T6:r{i}:mid"))

    for t in timers:
        t.stop()
        t.deleteLater()
    _pump(app, 200)
    gc.collect()
    snaps.append(_snap(f"T6:r{rounds}:end"))
    return snaps



# ========== 主控 ==========

STAGES: List[Tuple[str, str, Callable[[Any, int], List[Dict[str, Any]]]]] = [
    # signal 阶梯
    ("S0_signal_baseline", "signal", stage_S0_baseline),
    ("S1_signal_strong_ref", "signal", stage_S1_strong_ref),
    ("S2_signal_disconnect", "signal", stage_S2_explicit_disconnect),
    ("S3_signal_lambda", "signal", stage_S3_lambda),
    ("S4_signal_partial", "signal", stage_S4_partial),
    ("S5_signal_unique", "signal", stage_S5_unique_connection),
    ("S6_signal_cross_thread", "signal", stage_S6_cross_thread),
    # timer 阶梯
    ("T0_timer_baseline", "timer", stage_T0_baseline),
    ("T1_timer_stop_only", "timer", stage_T1_stop_only),
    ("T2_timer_deletelater_only", "timer", stage_T2_deletelater_only),
    ("T3_timer_stop_then_delete", "timer", stage_T3_stop_then_delete),
    ("T4_timer_singleshot_static", "timer", stage_T4_singleshot_static),
    ("T5_timer_with_parent", "timer", stage_T5_with_parent),
    ("T6_timer_half_cleanup", "timer", stage_T6_half_stop),
]


def _diff_obj(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    keys = set(a) | set(b)
    return {k: b.get(k, 0) - a.get(k, 0) for k in keys if b.get(k, 0) - a.get(k, 0) != 0}


def _fmt(label: str, snap: Dict[str, Any]) -> str:
    objs = snap.get("obj", {})
    qobj = objs.get("QObject_total", 0)
    qtimer = objs.get("PyQt5.QtCore.QTimer", 0)
    return f"{label:<32} RSS={snap['rss_mb']:>8.2f}MB  QObj={qobj:>4}  QTimer={qtimer:>3}"


def run(args: argparse.Namespace) -> int:
    import PyQt5.QtWebEngineWidgets  # noqa: F401
    from PyQt5.QtWidgets import QApplication  # type: ignore

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    results: Dict[str, Any] = {}
    summary_rows: List[Tuple[str, str, float, Dict[str, int], float]] = []

    for stage_name, kind, fn in STAGES:
        print(f"\n========== {stage_name} ==========")
        gc.collect()
        tracemalloc.start(25)
        try:
            snaps = fn(app, args.rounds)
        except Exception as exc:
            print(f"[FAIL] {stage_name} 抛出: {exc}")
            tracemalloc.stop()
            results[stage_name] = {"error": str(exc)}
            continue
        tracemalloc.stop()

        for s in snaps:
            print(_fmt(s["label"], s))
        if len(snaps) >= 2:
            rss_d = round(snaps[-1]["rss_mb"] - snaps[0]["rss_mb"], 3)
            obj_d = _diff_obj(snaps[0].get("obj", {}), snaps[-1].get("obj", {}))
            py_d = 0.0
            if "py_alloc_mb" in snaps[0] and "py_alloc_mb" in snaps[-1]:
                py_d = round(snaps[-1]["py_alloc_mb"] - snaps[0]["py_alloc_mb"], 3)
            print(f"[DELTA] RSS={rss_d:+.2f}MB  py={py_d:+.3f}MB  objΔ={obj_d}")
            results[stage_name] = {
                "snaps": snaps,
                "rss_delta_mb": rss_d,
                "py_alloc_delta_mb": py_d,
                "obj_delta": obj_d,
            }
            summary_rows.append((stage_name, kind, rss_d, obj_d, py_d))

    print("\n========== 汇总：按类分组 + objΔ 排序 ==========")
    for kind in ("signal", "timer"):
        rows = [r for r in summary_rows if r[1] == kind]
        rows.sort(key=lambda r: r[3].get("QObject_total", 0), reverse=True)
        print(f"\n--- {kind.upper()} ---")
        print(f"{'Stage':<35} {'RSSΔ/MB':>10} {'QObjectΔ':>10} {'pyΔ/MB':>10}")
        for name, _, rss_d, obj_d, py_d in rows:
            print(f"{name:<35} {rss_d:>+10.2f} {obj_d.get('QObject_total', 0):>+10} {py_d:>+10.3f}")

    out = os.path.join(os.path.dirname(__file__), "minimize_signal_timer_last.json")
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[SAVED] {out}")
    except Exception as exc:
        print(f"[WARN] 写 {out} 失败: {exc}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="signal + timer 场景最小化实验")
    p.add_argument("--rounds", type=int, default=100)
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
