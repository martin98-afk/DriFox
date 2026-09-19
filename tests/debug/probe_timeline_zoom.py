# -*- coding: utf-8 -*-
"""泳道图滚轮缩放探针：三种模式下「光标下的那条记录」是否被锚住。

用户报（2026-09-18）：Duration 模式滚轮放大正常，等宽 / Token 下不对。

用户感知判据：滚轮放大时，**光标底下那条记录应当留在光标下**。本探针直接测
「缩放前后，光标像素位置命中的记录是否同一条」，以及该记录的像素漂移。

待验证假设：``wheelEvent`` 的锚点用**线性时间**换算
（``anchor = t0 + frac * span``），而等宽 / Token 的条带 x 是**槽位域**
（按条目序号铺满），两套坐标不同源 → 锚点落在错误时刻。

跑法：``python tests/debug/probe_timeline_zoom.py``
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_UI = os.path.join(_ROOT, "plugins", "agent_trace", "ui")
for p in (_UI, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from PyQt5.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QPixmap, QWheelEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


def _load_pkg():
    name = "probe_tl_pkg"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_UI, "__init__.py"), submodule_search_locations=[_UI]
        )
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
    import importlib as _il

    return _il.import_module(f"{name}.trace_models"), _il.import_module(f"{name}.timeline_panel")


_models, _panel = _load_pkg()
TraceRecord = _models.TraceRecord
EntryKind = _models.EntryKind
_KINDS = [EntryKind.USER, EntryKind.ASSISTANT, EntryKind.TOOL, EntryKind.CONTEXT]


def _mk_records():
    """刻意时间不均匀：前 20 条 30s/条（稀疏），后 40 条 0.25s/条（密集）。

    「条目序号」与「时间位置」严重不同源 —— 槽位域均匀、时间域前疏后密，
    锚点若用错坐标系立刻暴露（时间均匀时两套坐标数值接近，掩盖问题）。
    """
    base = time.time() - 700
    out = []
    t = base
    for i in range(20):
        out.append(
            TraceRecord(
                kind=_KINDS[i % 4],
                label=f"sparse-{i}",
                preview="",
                raw="x" * 40,
                source=f"messages[{i}]",
                start_ts=t,
                end_ts=t + 0.4,
                turn_no=i + 1,
                meta={"tokens": 500 + i * 10},
            )
        )
        t += 30.0
    for i in range(40):
        out.append(
            TraceRecord(
                kind=_KINDS[i % 4],
                label=f"dense-{i}",
                preview="",
                raw="y" * 40,
                source=f"messages[{20 + i}]",
                start_ts=t,
                end_ts=t + 0.05,
                turn_no=21 + i,
                meta={"tokens": 20000 - i * 100},
            )
        )
        t += 0.25
    return out


def _bar_of(panel, idx):
    for rect, i in panel._hit_areas:
        if i == idx:
            return rect
    return None


def _hit_at(panel, x, y):
    """用面板自身的命中测试（含 y，只有真正压在条带上才算命中）。"""
    return panel._hit_test(QPoint(int(x), int(y)))


def _lane_y(panel, lane_i: int) -> int:
    """第 lane_i 条泳道的条带中心 y。"""
    h = (panel.height() - 20 - 6) / len(_models.LANE_ORDER)
    return int(20 + lane_i * h + h / 2)


def _label(panel, idx):
    if idx is None or idx >= len(panel._records):
        return "-"
    return panel._records[idx].label


def _wheel(panel, x, zoom_in=True):
    gpos = panel.mapToGlobal(QPoint(int(x), 10))
    orig = _panel.QCursor

    class _Stub:
        @staticmethod
        def pos():
            return gpos

    _panel.QCursor = _Stub
    try:
        delta = QPoint(0, 120) if zoom_in else QPoint(0, -120)
        phase = getattr(Qt, "NoScrollPhase", 0)
        ev = QWheelEvent(
            QPointF(float(x), 10.0),
            QPointF(gpos),
            QPoint(0, 0),
            delta,
            Qt.NoButton,
            Qt.NoModifier,
            phase,
            False,
        )
        panel.wheelEvent(ev)
    finally:
        _panel.QCursor = orig


def main() -> int:
    app = QApplication.instance() or QApplication([])
    panel = _panel.TimelinePanel()
    panel.resize(1600, panel.height())
    panel.show()
    app.processEvents()
    pixmap = QPixmap(1600, panel.height())

    recs = _mk_records()
    print(f"记录数 {len(recs)}（前 20 条稀疏 30s/条，后 40 条密集 0.25s/条）")
    print("判据：把光标放在某条记录条带的左端，滚轮放大后这条记录应仍在光标附近")
    print()
    hdr = f"{'模式':>9} {'目标记录':>12} {'缩放前跨度s':>11} {'缩放后跨度s':>11} {'条带左端px 前→后':>22} {'漂移px':>8}"
    print(hdr)
    print("-" * 82)

    # 三条代表性记录：稀疏段一条、密集段一条、中间一条
    targets = ("sparse-5", "sparse-19", "dense-10", "dense-39")
    bad = 0
    for mode in (_panel.MODE_EQUAL, _panel.MODE_DURATION, _panel.MODE_TOKEN):
        for tname in targets:
            panel._view = None
            panel.set_mode(mode)
            panel.set_records(recs)
            panel.render(pixmap)
            app.processEvents()

            idx = next(i for i, r in enumerate(recs) if r.label == tname)
            bar_before = _bar_of(panel, idx)
            if bar_before is None:
                print(f"{mode:>9} {tname:>12}  （该模式下条带不在视口内，跳过）")
                continue
            span_before = panel._t1 - panel._t0
            # 光标放在条带中心：这也是真实使用姿势（鼠标指着想看的条带滚）
            x = int((bar_before.left() + bar_before.right()) / 2)

            _wheel(panel, x, zoom_in=True)
            panel.render(pixmap)
            app.processEvents()

            bar_after = _bar_of(panel, idx)
            span_after = (panel._view[1] - panel._view[0]) if panel._view else (panel._t1 - panel._t0)
            if bar_after is None:
                print(f"{mode:>9} {tname:>12}  缩放后条带被甩出视口 ✗")
                bad += 1
                continue
            # 条带左端相对光标的位移：理想情况锚点处的条带几乎不动
            off_before = bar_before.left() - x
            off_after = bar_after.left() - x
            drift = off_after - off_before
            moved = abs(drift) > 60  # 超过 60px = 肉眼明显甩动
            if moved:
                bad += 1
            print(
                f"{mode:>9} {tname:>12} {span_before:>11.1f} {span_after:>11.1f} "
                f"{bar_before.left():>10} →{bar_after.left():>10} {drift:>8.1f} {'✗' if moved else ''}"
            )
    print()
    print(f"{'全部锚定正确' if bad == 0 else f'{bad} 组锚点跑偏'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
