# -*- coding: utf-8 -*-
"""[DEBUG-memleak] DriFox PyQt5 内存泄漏复现 harness

目的：在不依赖 LLM/网络的前提下，构造 DriFox 长时间运行的典型操作路径
      （发消息 / 切会话 / 加载插件），稳定复现 RSS 单调增长与对象计数不收敛。

两类复现：
  (a) 真实交互复现 -- 模拟 MessageCard + ChatSession 生命周期
  (b) 纯单元化复现 -- 验证 Qt 父子对象树/信号槽/sip 包装层泄漏模式

监控埋点（5+ 关键点）：见 docs/perf/memleak_repro_report.md 末尾。

运行：
  python tests/debug/memleak_repro/repro.py --scenario all
  python tests/debug/memleak_repro/repro.py --scenario message --rounds 200
  python tests/debug/memleak_repro/repro.py --scenario session --rounds 100
  python tests/debug/memleak_repro/repro.py --scenario plugin --rounds 50
  python tests/debug/memleak_repro/repro.py --scenario signal --rounds 500
  python tests/debug/memleak_repro/repro.py --scenario parent --rounds 500
  python tests/debug/memleak_repro/repro.py --scenario timer --rounds 500

输出：stdout 表格 + tests/debug/memleak_repro/last_run.json
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import tracemalloc
from typing import Any, Callable, Dict, List, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _rss_mb() -> float:
    """当前进程 RSS（MB）。优先 psutil；fallback 0。"""
    try:
        import psutil  # type: ignore
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


def _obj_counts() -> Dict[str, int]:
    """按类型统计当前存活对象数。"""
    counts: Dict[str, int] = {}
    keys = (
        "PyQt5.QtCore.QObject",
        "PyQt5.QtCore.QTimer",
        "PyQt5.QtCore.QThread",
        "PyQt5.QtWidgets.QWidget",
        "PyQt5.QtWidgets.QLabel",
        "PyQt5.QtWidgets.QLayout",
        "PyQt5.QtWidgets.QVBoxLayout",
        "PyQt5.QtWebEngineWidgets.QWebEngineView",
        "PyQt5.QtWebEngineWidgets.QWebEnginePage",
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


def _snapshot(label: str) -> Dict[str, Any]:
    snap: Dict[str, Any] = {"label": label, "ts": time.time(), "rss_mb": round(_rss_mb(), 3)}
    snap["obj"] = _obj_counts()
    if tracemalloc.is_tracing():
        cur, peak = tracemalloc.get_traced_memory()
        snap["py_alloc_mb"] = round(cur / 1024 / 1024, 3)
        snap["py_peak_mb"] = round(peak / 1024 / 1024, 3)
    return snap


def _fmt_row(snap: Dict[str, Any]) -> str:
    objs = snap.get("obj", {})
    qobj = objs.get("QObject_total", 0)
    qwid = objs.get("PyQt5.QtWidgets.QWidget", 0)
    base = (
        f"{snap['label']:<28} RSS={snap['rss_mb']:>8.2f}MB  "
        f"QObject={qobj:>5}  QWidget={qwid:>4}"
    )
    if "py_alloc_mb" in snap:
        base += f"  pyAlloc={snap['py_alloc_mb']:>7.3f}MB"
    return base


def _diff(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"rss_delta_mb": round(b["rss_mb"] - a["rss_mb"], 3)}
    keys = set(a.get("obj", {})) | set(b.get("obj", {}))
    obj_delta = {k: b["obj"].get(k, 0) - a["obj"].get(k, 0) for k in keys}
    out["obj_delta"] = {k: v for k, v in obj_delta.items() if v != 0}
    if "py_alloc_mb" in a and "py_alloc_mb" in b:
        out["py_alloc_delta_mb"] = round(b["py_alloc_mb"] - a["py_alloc_mb"], 3)
    return out


def _pump_qt(app, ms: int = 60) -> None:
    from PyQt5.QtCore import QEventLoop, QTimer  # type: ignore
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    app.processEvents()



# ========== 场景 A：真实交互 —— 发消息 ==========

def scenario_message(app, rounds: int) -> List[Dict[str, Any]]:
    """模拟在 ChatWindow 中持续追加 N 条 user/assistant 消息卡。
    每 20 条做一次"清空"，模拟会话内追加后刷新/切回。
    复刻路径：MessageCard 创建 + 进 chat_layout + 旧卡 deleteLater。
    """
    from PyQt5.QtWidgets import QWidget, QVBoxLayout  # type: ignore

    snaps: List[Dict[str, Any]] = []
    snaps.append(_snapshot("msg:r0:init"))

    chat_widget = QWidget()
    chat_layout = QVBoxLayout(chat_widget)
    chat_layout.setSpacing(8)
    chat_widget.resize(420, 600)

    try:
        from app.widgets.message_card import MessageCard  # type: ignore
        use_real_card = True
    except Exception as exc:  # noqa: BLE001
        use_real_card = False
        print(f"[WARN] MessageCard 不可用，回退到 QLabel: {exc}")

    for i in range(1, rounds + 1):
        text = f"消息 #{i} —— 测试长字符串用于触发 Markdown 渲染/换行。" * 8
        if use_real_card:
            card = None
            for args in (("user",), ()):
                try:
                    card = MessageCard(*args)
                    break
                except Exception:
                    continue
            if card is not None:
                for m in ('set_message','setMarkdown','setPlainText','update_content','setContent'):
                    fn = getattr(card, m, None)
                    if not callable(fn):
                        continue
                    try:
                        if m == 'set_message':
                            fn(role='user', content=text)
                        else:
                            fn(text)
                        break
                    except Exception:
                        continue
        else:
            from PyQt5.QtWidgets import QLabel  # type: ignore
            card = QLabel(text)
            card.setWordWrap(True)
        chat_layout.addWidget(card)

        if i % 20 == 0:
            while chat_layout.count():
                item = chat_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            _pump_qt(app, 80)
            gc.collect()
            snaps.append(_snapshot(f"msg:r{i}:flush"))

        if i % 50 == 0:
            gc.collect()
            snaps.append(_snapshot(f"msg:r{i}:mid"))

    while chat_layout.count():
        item = chat_layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
    chat_widget.deleteLater()
    _pump_qt(app, 200)
    gc.collect()
    snaps.append(_snapshot(f"msg:r{rounds}:end"))
    return snaps


# ========== 场景 B：真实交互 —— 切会话 ==========

def scenario_session(app, rounds: int) -> List[Dict[str, Any]]:
    """N 次切换会话：销毁旧 chat_widget 树 → 新建新会话树。
    复刻路径：ChatBackend.switch_session → 旧卡全删 + 新建。
    """
    from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel  # type: ignore

    snaps: List[Dict[str, Any]] = []
    snaps.append(_snapshot("sess:r0:init"))

    current_widget: Optional[QWidget] = None

    for i in range(1, rounds + 1):
        if current_widget is not None:
            current_widget.setParent(None)
            current_widget.deleteLater()
            _pump_qt(app, 30)
            current_widget = None

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(4)
        for j in range(10):
            card = QLabel(f"[session#{i} msg#{j}] " + ("x" * 200))
            card.setWordWrap(True)
            layout.addWidget(card)
        w.resize(420, 500)
        current_widget = w

        if i % 20 == 0:
            gc.collect()
            snaps.append(_snapshot(f"sess:r{i}:mid"))

    if current_widget is not None:
        current_widget.setParent(None)
        current_widget.deleteLater()
    _pump_qt(app, 200)
    gc.collect()
    snaps.append(_snapshot(f"sess:r{rounds}:end"))
    return snaps



# ========== 场景 C：真实交互 —— 加载/卸载插件 ==========

def scenario_plugin(app, rounds: int) -> List[Dict[str, Any]]:
    """反复加载/卸载最小"插件" QObject。复刻 runtime_component_loader 反复 reload。
    """
    from PyQt5.QtCore import QObject, pyqtSignal  # type: ignore

    snaps: List[Dict[str, Any]] = []
    snaps.append(_snapshot("plug:r0:init"))

    registry: Dict[str, QObject] = {}

    class PluginObj(QObject):
        ready = pyqtSignal()

        def __init__(self, name: str):
            super().__init__()
            self.name = name

    for i in range(1, rounds + 1):
        name = f"plugin_{i}"
        obj = PluginObj(name)
        registry[name] = obj
        old_name = f"plugin_{i-1}" if i > 1 else None
        if old_name and old_name in registry:
            old = registry.pop(old_name)
            try:
                old.ready.disconnect()
            except Exception:
                pass
            old.setParent(None)
            old.deleteLater()
        if i % 20 == 0:
            gc.collect()
            snaps.append(_snapshot(f"plug:r{i}:mid"))

    for n, o in list(registry.items()):
        registry.pop(n, None)
        try:
            o.ready.disconnect()
        except Exception:
            pass
        o.setParent(None)
        o.deleteLater()
    _pump_qt(app, 200)
    gc.collect()
    snaps.append(_snapshot(f"plug:r{rounds}:end"))
    return snaps


# ========== 场景 D：单元化 —— 信号未断开 ==========

def scenario_signal(app, rounds: int) -> List[Dict[str, Any]]:
    """最小复现：source 持有 sink 引用，rounds 后只删 sink 列表但因 source 还活着
    → sink 因信号连接被 source 持有无法释放（PyQt5 强引用）。
    """
    from PyQt5.QtCore import QObject, pyqtSignal  # type: ignore

    snaps: List[Dict[str, Any]] = []
    snaps.append(_snapshot("sig:r0:init"))

    sources: List[QObject] = []

    class Source(QObject):
        tick = pyqtSignal(int)

    class Sink(QObject):
        def __init__(self):
            super().__init__()
            self.n = 0

        def on_tick(self, v: int):
            self.n += v

    for i in range(1, rounds + 1):
        s = Source()
        k = Sink()
        s.tick.connect(k.on_tick)
        s.tick.emit(1)
        sources.append(s)
        if i % 50 == 0:
            gc.collect()
            snaps.append(_snapshot(f"sig:r{i}:mid"))

    for s in sources:
        try:
            s.deleteLater()
        except Exception:
            pass
    _pump_qt(app, 200)
    gc.collect()
    snaps.append(_snapshot(f"sig:r{rounds}:end"))
    return snaps


# ========== 场景 E：单元化 —— 父子对象未挂 ==========

def scenario_parent(app, rounds: int) -> List[Dict[str, Any]]:
    """N 个 QWidget 不设 parent，只 del Python 引用 —— 验证 Qt 父子树外对象的 GC。
    """
    from PyQt5.QtWidgets import QWidget  # type: ignore

    snaps: List[Dict[str, Any]] = []
    snaps.append(_snapshot("par:r0:init"))

    bucket: List[QWidget] = []

    for i in range(1, rounds + 1):
        w = QWidget()
        w.resize(80, 30)
        bucket.append(w)
        if i % 50 == 0:
            bucket = bucket[::2]
            gc.collect()
            snaps.append(_snapshot(f"par:r{i}:mid"))

    bucket.clear()
    _pump_qt(app, 200)
    gc.collect()
    snaps.append(_snapshot(f"par:r{rounds}:end"))
    return snaps


# ========== 场景 F：单元化 —— QTimer 反复创建 ==========

def scenario_timer(app, rounds: int) -> List[Dict[str, Any]]:
    """每轮一个 QTimer.start(单次)，只 stop 不 deleteLater —— 验证 timer 堆积。
    """
    from PyQt5.QtCore import QTimer  # type: ignore

    snaps: List[Dict[str, Any]] = []
    snaps.append(_snapshot("tmr:r0:init"))

    timers: List[QTimer] = []

    def _fire():
        pass

    for i in range(1, rounds + 1):
        t = QTimer()
        t.setSingleShot(True)
        t.timeout.connect(_fire)
        t.start(60_000)
        timers.append(t)
        if i % 50 == 0:
            keep: List[QTimer] = []
            for idx, t in enumerate(timers):
                if idx % 2 == 0:
                    t.stop()
                    t.deleteLater()
                else:
                    keep.append(t)
            timers = keep
            _pump_qt(app, 40)
            gc.collect()
            snaps.append(_snapshot(f"tmr:r{i}:mid"))

    for t in timers:
        t.stop()
        t.deleteLater()
    _pump_qt(app, 200)
    gc.collect()
    snaps.append(_snapshot(f"tmr:r{rounds}:end"))
    return snaps



# ========== 主控 ==========

SCENARIOS: Dict[str, Callable[[Any, int], List[Dict[str, Any]]]] = {
    "message": scenario_message,
    "session": scenario_session,
    "plugin": scenario_plugin,
    "signal": scenario_signal,
    "parent": scenario_parent,
    "timer": scenario_timer,
}


def run(args: argparse.Namespace) -> int:
    from PyQt5.QtCore import Qt  # type: ignore
    import PyQt5.QtWebEngineWidgets  # noqa: F401
    from PyQt5.QtWidgets import QApplication  # type: ignore

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    scenarios = list(SCENARIOS.keys()) if args.scenario == "all" else [args.scenario]
    all_runs: Dict[str, Any] = {}

    for name in scenarios:
        print(f"\n=== 场景: {name}  rounds={args.rounds} ===")
        gc.collect()
        tracemalloc.start(25)
        snaps = SCENARIOS[name](app, args.rounds)
        tracemalloc.stop()

        for s in snaps:
            print(_fmt_row(s))
        if len(snaps) >= 2:
            diff = _diff(snaps[0], snaps[-1])
            print(f"[DELTA] RSS Δ={diff['rss_delta_mb']:+.2f}MB  objΔ={diff['obj_delta']}")
            if "py_alloc_delta_mb" in diff:
                print(f"[DELTA] py alloc Δ={diff['py_alloc_delta_mb']:+.3f}MB")
        all_runs[name] = {
            "snaps": snaps,
            "final_delta": _diff(snaps[0], snaps[-1]) if len(snaps) >= 2 else {},
        }

    out_path = os.path.join(os.path.dirname(__file__), "last_run.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(all_runs, f, ensure_ascii=False, indent=2)
        print(f"\n[SAVED] {out_path}")
    except Exception as exc:
        print(f"[WARN] 写 last_run.json 失败: {exc}")

    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DriFox PyQt5 内存泄漏复现 harness")
    p.add_argument(
        "--scenario",
        default="all",
        choices=["all", "message", "session", "plugin", "signal", "parent", "timer"],
    )
    p.add_argument("--rounds", type=int, default=100)
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
