# -*- coding: utf-8 -*-
"""泳道图（TimelinePanel）渲染开销基准：paintEvent 在长会话下的线性成本。

只测一件事：``paintEvent`` 的耗时随记录数增长的斜率，以及三种宽度模式
（equal / duration / token）的差异。修复前先要数字，避免凭感觉优化。

跑法：``python tests/debug/bench_timeline_paint.py``
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_UI = os.path.join(_ROOT, "plugins", "agent_trace", "ui")
for p in (_UI, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from PyQt5.QtGui import QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


def _load_pkg():
    """以包形式加载 plugins/agent_trace/ui（模块内有相对导入）。"""
    name = "bench_tl_pkg"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, os.path.join(_UI, "__init__.py"), submodule_search_locations=[_UI])
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
    import importlib as _il

    return _il.import_module(f"{name}.trace_models"), _il.import_module(f"{name}.timeline_panel")


_models, _panel = _load_pkg()


def _mk_records(n: int):
    """造 n 条记录：四种泳道轮转，时间戳线性铺开。"""
    TraceRecord = _models.TraceRecord
    EntryKind = _models.EntryKind
    kinds = [EntryKind.USER, EntryKind.ASSISTANT, EntryKind.TOOL, EntryKind.CONTEXT]
    base = time.time() - 600
    out = []
    for i in range(n):
        kind = kinds[i % len(kinds)]
        rec = TraceRecord(
            kind=kind,
            label=f"item-{i}",
            preview=f"preview {i}",
            raw=f"raw content {i} " * 12,
            source=f"messages[{i}]",
            start_ts=base + i * 0.25,
            end_ts=base + i * 0.25 + 0.2,
            turn_no=i // 4 + 1,
            meta={"tokens": 200 + (i * 37) % 900},
        )
        out.append(rec)
    return out


def _measure(panel, pixmap, rounds: int = 7) -> float:
    """单次 paintEvent 的中位耗时（ms）。"""
    from PyQt5.QtGui import QPainter

    ts = []
    for _ in range(rounds):
        p = QPainter(pixmap)
        t0 = time.perf_counter()
        try:
            panel.render(p)
        finally:
            p.end()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    panel = _panel.TimelinePanel()
    panel.resize(1600, panel.height())
    panel.show()
    app.processEvents()

    pixmap = QPixmap(1600, panel.height())
    print(f"面板尺寸 1600x{panel.height()}  泳道数 {len(_models.LANE_ORDER)}")
    print(f"{'记录数':>8} {'equal':>10} {'duration':>10} {'token':>10}")
    for n in (200, 500, 1000, 2000, 4000):
        recs = _mk_records(n)
        row = []
        for mode in (_panel.MODE_EQUAL, _panel.MODE_DURATION, _panel.MODE_TOKEN):
            panel.set_mode(mode)
            panel.set_records(recs)
            app.processEvents()
            row.append(_measure(panel, pixmap))
        print(f"{n:>8} {row[0]:>10.2f} {row[1]:>10.2f} {row[2]:>10.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
