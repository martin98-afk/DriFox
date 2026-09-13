# -*- coding: utf-8 -*-
"""临时探针 9：谁污染了 roundtrip — get_instance 残留 vs 全局单例（用完即删）"""

from __future__ import annotations

import os

from app.widgets.tab_manager_window import TabManagerWindow

_LINES: list[str] = []


def _p(*a):
    _LINES.append(" ".join(str(x) for x in a))


def _mk(qtbot):
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    w = TabManagerWindow.create_instance()
    qtbot.addWidget(w)
    w.set_workbench_visible(False, animate=False)
    qtbot.wait(30)
    return w


def _run_double_toggle(w, qtbot, tag):
    p = w._tab_panel

    class Stub:
        def __init__(self, s):
            self._s = list(s)

        def count(self):
            return len(self._s)

        def sizes(self):
            return list(self._s)

        def setSizes(self, s):
            self._s = list(s)

    st = Stub([201, 900, 0])
    w._splitter = st
    p.set_collapsed(False)
    w._saved_panel_frame_width = 201
    p._toggle_sidebar()
    qtbot.wait(330)
    mid = p._collapsed
    p._toggle_sidebar()
    qtbot.wait(330)
    _p(f"[{tag}] mid={mid} final={p._collapsed} sizes={st.sizes()} pw={p.width()}")


def test_a_noop_1(qtbot):
    """只创建窗口，什么都不碰"""
    _mk(qtbot)


def test_b_toggle(qtbot):
    w = _mk(qtbot)
    _run_double_toggle(w, qtbot, "after-noop")


def test_c_noop_2(qtbot):
    _mk(qtbot)


def test_d_toggle(qtbot):
    w = _mk(qtbot)
    _run_double_toggle(w, qtbot, "after-noop2")


os.makedirs("reports", exist_ok=True)
import atexit

atexit.register(lambda: open("reports/_probe_run9.txt", "w", encoding="utf-8").write("\n".join(_LINES)))
