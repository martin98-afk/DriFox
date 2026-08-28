# -*- coding: utf-8 -*-
"""[DEBUG-memleak-msg] message 场景最小化实验

目的：在 T1 报告「message 场景 20 轮 +105.99 MB」基础上，**逐步剥离非关键代码**，
      找出真正贡献 5.30 MB/卡的最小代码路径。

剥离阶梯（每步独立可跑）：
  M0 = QWidget 基线（无 MessageCard）                    -- 期望 ~0.5 MB/卡
  M1 = MessageCard(role="user") 不调用 set_message       -- 期望 <1 MB/卡
  M2 = MessageCard(role="user") + setPlainText            -- 期望 1-3 MB/卡
  M3 = MessageCard(role="user") + setMarkdown(长)         -- 期望 3-6 MB/卡 (= 当前)
  M4 = M3 + 手动 card.cleanup() 后再 deleteLater         -- 验证 cleanup 影响
  M5 = M3 + 主动 disconnect(contentHeightChanged)        -- 验证信号影响

运行：python tests/debug/memleak_repro/minimize_message.py [--rounds N]
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import tracemalloc
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


def _obj_counts(extra_keys: Tuple[str, ...] = ()) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    keys = (
        "PySide6.QtCore.QObject",
        "PySide6.QtCore.QTimer",
        "PySide6.QtCore.QPropertyAnimation",
        "PySide6.QtCore.QVariantAnimation",
        "PySide6.QtWidgets.QWidget",
        "PySide6.QtWidgets.QLabel",
        "PySide6.QtWidgets.QPushButton",
        "PySide6.QtWidgets.QTextEdit",
        "PySide6.QtWidgets.QTextDocument",
        "PySide6.QtWidgets.QVBoxLayout",
        "PySide6.QtGui.QColor",
        "PySide6.QtGui.QLinearGradient",
        "PySide6.QtGui.QPainterPath",
    ) + extra_keys
    for obj in gc.get_objects():
        try:
            cn = type(obj).__module__ + "." + type(obj).__name__
        except Exception:
            continue
        if cn in keys:
            counts[cn] = counts.get(cn, 0) + 1
    try:
        from PySide6.QtCore import QObject  # type: ignore
        counts["QObject_total"] = sum(1 for o in gc.get_objects() if isinstance(o, QObject))
    except Exception:
        pass
    return counts


def _snap(label: str) -> Dict[str, Any]:
    s: Dict[str, Any] = {"label": label, "ts": time.time(), "rss_mb": round(_rss_mb(), 3), "obj": _obj_counts()}
    if tracemalloc.is_tracing():
        cur, peak = tracemalloc.get_traced_memory()
        s["py_alloc_mb"] = round(cur / 1024 / 1024, 3)
        s["py_peak_mb"] = round(peak / 1024 / 1024, 3)
    return s


def _pump(app, ms: int = 80) -> None:
    from PySide6.QtCore import QEventLoop, QTimer  # type: ignore
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def _make_chat_widget():
    from PySide6.QtWidgets import QWidget, QVBoxLayout  # type: ignore
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.setSpacing(8)
    w.resize(420, 600)
    return w, layout


def _flush_layout(layout, app, pump_ms: int = 80):
    """完全清空：takeAt + setParent(None) + deleteLater + 泵事件循环。"""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is None:
            continue
        w.setParent(None)
        w.deleteLater()
    _pump(app, pump_ms)
    gc.collect()



# ========== 各阶梯实验 ==========

def stage_M0_baseline(app, rounds: int) -> List[Dict[str, Any]]:
    """M0：纯 QWidget，不引入 MessageCard。"""
    from PySide6.QtWidgets import QWidget  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap(f"M0:r0:init"))
    chat_widget, layout = _make_chat_widget()
    for i in range(1, rounds + 1):
        w = QWidget(chat_widget)
        w.resize(80, 30)
        layout.addWidget(w)
        if i % 20 == 0:
            _flush_layout(layout, app)
            snaps.append(_snap(f"M0:r{i}:flush"))
    _flush_layout(layout, app)
    chat_widget.deleteLater()
    _pump(app, 200)
    snaps.append(_snap(f"M0:r{rounds}:end"))
    return snaps


def stage_M1_no_setmsg(app, rounds: int) -> List[Dict[str, Any]]:
    """M1：MessageCard(role="user") 但不调用 set_message。"""
    from app.widgets.message_card import MessageCard  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap(f"M1:r0:init"))
    chat_widget, layout = _make_chat_widget()
    for i in range(1, rounds + 1):
        card = MessageCard(role="user")
        layout.addWidget(card)
        if i % 20 == 0:
            _flush_layout(layout, app)
            snaps.append(_snap(f"M1:r{i}:flush"))
    _flush_layout(layout, app)
    chat_widget.deleteLater()
    _pump(app, 200)
    snaps.append(_snap(f"M1:r{rounds}:end"))
    return snaps


def stage_M2_setplain(app, rounds: int) -> List[Dict[str, Any]]:
    """M2：MessageCard + setPlainText（短文本）。"""
    from app.widgets.message_card import MessageCard  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap(f"M2:r0:init"))
    chat_widget, layout = _make_chat_widget()
    for i in range(1, rounds + 1):
        card = MessageCard(role="user")
        try:
            if hasattr(card, "viewer") and card.viewer is not None and hasattr(card.viewer, "setPlainText"):
                card.viewer.setPlainText(f"消息 #{i} 测试")
            elif hasattr(card, "set_message"):
                card.set_message(role="user", content=f"消息 #{i} 测试")
        except Exception as exc:  # noqa: BLE001
            print(f"[M2] setPlainText 失败: {exc}")
        layout.addWidget(card)
        if i % 20 == 0:
            _flush_layout(layout, app)
            snaps.append(_snap(f"M2:r{i}:flush"))
    _flush_layout(layout, app)
    chat_widget.deleteLater()
    _pump(app, 200)
    snaps.append(_snap(f"M2:r{rounds}:end"))
    return snaps


def stage_M6_weakref(app, rounds: int) -> List[Dict[str, Any]]:
    """M6：M3 + weakref 追踪 + gc.get_referrers 定位外部 Python 引用持有者。"""
    import weakref
    from app.widgets.message_card import MessageCard  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap(f"M6:r0:init"))

    alive: "weakref.WeakSet[MessageCard]" = weakref.WeakSet()
    orig_init = MessageCard.__init__
    def _patched(self, *a, **kw):
        orig_init(self, *a, **kw)
        alive.add(self)
    MessageCard.__init__ = _patched
    try:
        chat_widget, layout = _make_chat_widget()
        text = "消息长内容。" * 50
        keep_cards: list = []
        for i in range(1, rounds + 1):
            card = MessageCard(role="user")
            keep_cards.append(card)
            try:
                if hasattr(card, "viewer") and card.viewer is not None and hasattr(card.viewer, "setPlainText"):
                    card.viewer.setPlainText(f"#{i} " + text)
                elif hasattr(card, "set_message"):
                    card.set_message(role="user", content=f"#{i} " + text)
            except Exception:
                pass
            layout.addWidget(card)
            if i % 20 == 0:
                _flush_layout(layout, app)
                gc.collect()
                snaps.append(_snap(f"M6:r{i}:flush"))
                # 找出仍持有 card 的对象（排除栈/局部）
                still_alive = [c for c in alive if c is not None]
                if still_alive:
                    sample = still_alive[0]
                    refs = gc.get_referrers(sample)
                    interesting = []
                    for r in refs:
                        if isinstance(r, dict):
                            for k, v in list(r.items()):
                                if v is sample:
                                    interesting.append(("dict-key", k, type(r).__name__))
                                    break
                        elif isinstance(r, list):
                            for idx, v in enumerate(r):
                                if v is sample:
                                    interesting.append(("list-idx", idx, type(r).__name__))
                                    break
                        elif isinstance(r, (set, frozenset)):
                            interesting.append(("set-member", None, type(r).__name__))
                    print(f"[M6 r={i}] alive={len(still_alive)} sample-refs={interesting[:6]}")
        # 收尾
        _flush_layout(layout, app)
        chat_widget.deleteLater()
        _pump(app, 200)
        gc.collect()
        still_alive = [c for c in alive if c is not None]
        snaps.append(_snap(f"M6:r{rounds}:end"))
        print(f"[M6 end] alive_cards={len(still_alive)}")
    finally:
        MessageCard.__init__ = orig_init
    return snaps

def stage_M3_setmarkdown(app, rounds: int) -> List[Dict[str, Any]]:
    """M3：MessageCard + 长文本（对齐 T1 message 场景）。"""
    from app.widgets.message_card import MessageCard  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap(f"M3:r0:init"))
    chat_widget, layout = _make_chat_widget()
    text = "消息长内容。" * 50  # ~500 字符
    for i in range(1, rounds + 1):
        card = MessageCard(role="user")
        try:
            if hasattr(card, "viewer") and card.viewer is not None and hasattr(card.viewer, "setPlainText"):
                card.viewer.setPlainText(f"#{i} " + text)
            elif hasattr(card, "set_message"):
                card.set_message(role="user", content=f"#{i} " + text)
        except Exception:
            pass
        layout.addWidget(card)
        if i % 20 == 0:
            _flush_layout(layout, app)
            snaps.append(_snap(f"M3:r{i}:flush"))
    _flush_layout(layout, app)
    chat_widget.deleteLater()
    _pump(app, 200)
    snaps.append(_snap(f"M3:r{rounds}:end"))
    return snaps


def stage_M4_manual_cleanup(app, rounds: int) -> List[Dict[str, Any]]:
    """M4：M3 + 手动 card.cleanup() 再 deleteLater（验证 cleanup 影响）。"""
    from app.widgets.message_card import MessageCard  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap(f"M4:r0:init"))
    chat_widget, layout = _make_chat_widget()
    text = "消息长内容。" * 50
    for i in range(1, rounds + 1):
        card = MessageCard(role="user")
        try:
            if hasattr(card, "viewer") and card.viewer is not None and hasattr(card.viewer, "setPlainText"):
                card.viewer.setPlainText(f"#{i} " + text)
            elif hasattr(card, "set_message"):
                card.set_message(role="user", content=f"#{i} " + text)
        except Exception:
            pass
        layout.addWidget(card)
        if i % 20 == 0:
            # 手动 cleanup 再 flush
            for j in range(layout.count()):
                item = layout.itemAt(j)
                w = item.widget() if item else None
                if w and hasattr(w, "cleanup"):
                    try:
                        w.cleanup()
                    except Exception:
                        pass
            _flush_layout(layout, app)
            snaps.append(_snap(f"M4:r{i}:flush+cleanup"))
    # 收尾时也手动 cleanup
    for j in range(layout.count()):
        item = layout.itemAt(j)
        w = item.widget() if item else None
        if w and hasattr(w, "cleanup"):
            try:
                w.cleanup()
            except Exception:
                pass
    _flush_layout(layout, app)
    chat_widget.deleteLater()
    _pump(app, 200)
    snaps.append(_snap(f"M4:r{rounds}:end"))
    return snaps


def stage_M5_disconnect_signal(app, rounds: int) -> List[Dict[str, Any]]:
    """M5：M3 + 主动 disconnect contentHeightChanged 信号（验证信号影响）。"""
    from app.widgets.message_card import MessageCard  # type: ignore
    snaps: List[Dict[str, Any]] = []
    snaps.append(_snap(f"M5:r0:init"))
    chat_widget, layout = _make_chat_widget()
    text = "消息长内容。" * 50
    for i in range(1, rounds + 1):
        card = MessageCard(role="user")
        try:
            if hasattr(card, "viewer") and card.viewer is not None and hasattr(card.viewer, "setPlainText"):
                card.viewer.setPlainText(f"#{i} " + text)
            elif hasattr(card, "set_message"):
                card.set_message(role="user", content=f"#{i} " + text)
        except Exception:
            pass
        # 主动 disconnect 关键信号
        try:
            if hasattr(card, "viewer") and card.viewer is not None:
                card.viewer.contentHeightChanged.disconnect(card._update_height)
        except Exception:
            pass
        layout.addWidget(card)
        if i % 20 == 0:
            _flush_layout(layout, app)
            snaps.append(_snap(f"M5:r{i}:flush"))
    _flush_layout(layout, app)
    chat_widget.deleteLater()
    _pump(app, 200)
    snaps.append(_snap(f"M5:r{rounds}:end"))
    return snaps



STAGES: List[Tuple[str, Callable[[Any, int], List[Dict[str, Any]]]]] = [
    ("M0_baseline_QWidget", stage_M0_baseline),
    ("M1_MsgCard_no_setmsg", stage_M1_no_setmsg),
    ("M2_MsgCard_setPlainText", stage_M2_setplain),
    ("M3_MsgCard_longText", stage_M3_setmarkdown),
    ("M4_M3_plus_manual_cleanup", stage_M4_manual_cleanup),
    ("M5_M3_plus_disconnect_signal", stage_M5_disconnect_signal),
    ("M6_weakref_locate_holders", stage_M6_weakref),
]


def _diff_obj(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    keys = set(a) | set(b)
    return {k: b.get(k, 0) - a.get(k, 0) for k in keys if b.get(k, 0) - a.get(k, 0) != 0}


def _fmt(label: str, snap: Dict[str, Any]) -> str:
    objs = snap.get("obj", {})
    qobj = objs.get("QObject_total", 0)
    qwid = objs.get("PySide6.QtWidgets.QWidget", 0)
    qtext = objs.get("PySide6.QtWidgets.QTextEdit", 0)
    qcolor = objs.get("PySide6.QtGui.QColor", 0)
    qgrad = objs.get("PySide6.QtGui.QLinearGradient", 0)
    qpp = objs.get("PySide6.QtGui.QPainterPath", 0)
    qtimer = objs.get("PySide6.QtCore.QTimer", 0)
    qanim = objs.get("PySide6.QtCore.QVariantAnimation", 0)
    base = f"{label:<32} RSS={snap['rss_mb']:>8.2f}MB  QObj={qobj:>4}  QWid={qwid:>4}"
    base += f"  QTextEdit={qtext:>3}  QColor={qcolor:>3}  Grad={qgrad:>2}  PainterPath={qpp:>2}  QTimer={qtimer:>3}  QVariantAnim={qanim:>2}"
    if "py_alloc_mb" in snap:
        base += f"  py={snap['py_alloc_mb']:>6.3f}MB"
    return base


def run(args: argparse.Namespace) -> int:
    import PySide6.QtWebEngineWidgets  # noqa: F401
    from PySide6.QtWidgets import QApplication  # type: ignore

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    results: Dict[str, Any] = {}
    summary_rows: List[Tuple[str, float, Dict[str, int], float]] = []

    for stage_name, fn in STAGES:
        print(f"\n========== {stage_name} (rounds={args.rounds}) ==========")
        gc.collect()
        tracemalloc.start(25)
        try:
            snaps = fn(app, args.rounds)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {stage_name} 抛出: {exc}")
            tracemalloc.stop()
            results[stage_name] = {"error": str(exc)}
            continue
        tracemalloc.stop()

        for s in snaps:
            print(_fmt(s["label"], s))
        if len(snaps) >= 2:
            rss_delta = round(snaps[-1]["rss_mb"] - snaps[0]["rss_mb"], 3)
            obj_delta = _diff_obj(snaps[0].get("obj", {}), snaps[-1].get("obj", {}))
            py_alloc_delta = 0.0
            if "py_alloc_mb" in snaps[0] and "py_alloc_mb" in snaps[-1]:
                py_alloc_delta = round(snaps[-1]["py_alloc_mb"] - snaps[0]["py_alloc_mb"], 3)
            print(f"[DELTA] RSS={rss_delta:+.2f}MB  py={py_alloc_delta:+.3f}MB  objΔ={obj_delta}")
            results[stage_name] = {
                "snaps": snaps,
                "rss_delta_mb": rss_delta,
                "py_alloc_delta_mb": py_alloc_delta,
                "obj_delta": obj_delta,
            }
            summary_rows.append((stage_name, rss_delta, obj_delta, py_alloc_delta))

    print("\n========== 汇总：每阶梯 RSS Δ 排序 ==========")
    print(f"{'Stage':<35} {'RSSΔ/MB':>10} {'pyAllocΔ/MB':>12} {'objΔ top':<40}")
    summary_rows.sort(key=lambda r: r[1], reverse=True)
    for name, rss_d, obj_d, py_d in summary_rows:
        top3 = sorted(obj_d.items(), key=lambda x: -x[1])[:3]
        top3_str = ", ".join(f"{k}:{v:+d}" for k, v in top3)
        print(f"{name:<35} {rss_d:>+10.2f} {py_d:>+12.3f} {top3_str:<40}")

    out = os.path.join(os.path.dirname(__file__), "minimize_message_last.json")
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[SAVED] {out}")
    except Exception as exc:
        print(f"[WARN] 写 {out} 失败: {exc}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DriFox message 场景最小化实验")
    p.add_argument("--rounds", type=int, default=20)
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
