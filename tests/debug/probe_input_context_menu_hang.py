# -*- coding: utf-8 -*-
"""临时探针 v2：输入框右键菜单卡死点分段定位

结构：真实 QApplication.exec() 主循环 + QTimer 触发右键 + watchdog 采样主线程栈。
分段计时：菜单构建 / setStyleSheet / addAction / exec_（内部）/ 关闭后返回。

运行：uv run python tests/debug/probe_input_context_menu_hang.py
"""

import os
import sys
import threading
import time
import traceback

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMenu

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.bottom_input_area import SendableTextEdit  # noqa: E402

box = SendableTextEdit()
box.resize(600, 120)
box._initializing = False
box.show()
app.processEvents()

main_tid = threading.get_ident()
state = {"done": False, "stacks": [], "t0": 0.0, "exec_in": 0.0, "exec_out": 0.0}


def watchdog():
    while not state["done"]:
        time.sleep(1.0)
        if state["done"]:
            return
        frame = sys._current_frames().get(main_tid)
        state["stacks"].append(
            (round(time.time() - state["t0"], 1), "".join(traceback.format_stack(frame)[-8:]))
        )


threading.Thread(target=watchdog, daemon=True).start()

_orig_exec = QMenu.exec_


def timed_exec(self, *a, **kw):
    state["exec_in"] = time.time() - state["t0"]
    r = _orig_exec(self, *a, **kw)
    state["exec_out"] = time.time() - state["t0"]
    return r


QMenu.exec_ = timed_exec

_orig_show = QMenu.popup


def timed_popup(self, *a, **kw):
    print(f"[t={time.time() - state['t0']:.2f}s] menu.popup 调用", flush=True)
    return _orig_show(self, *a, **kw)


QMenu.popup = timed_popup


def close_popup(tag: str):
    ms = [w for w in QApplication.topLevelWidgets() if isinstance(w, QMenu) and w.isVisible()]
    print(f"[t={time.time() - state['t0']:.2f}s] {tag} 可见菜单数={len(ms)}", flush=True)
    for w in ms:
        w.close()
    if state["exec_in"] and not state["exec_out"]:
        print(
            f"  注意：exec_ 已进入 {state['exec_in']:.2f}s，仍在阻塞中",
            flush=True,
        )


def do_click(text_label: str, text: str):
    box.setPlainText(text)
    app.processEvents()
    state["t0"] = time.time()
    state["exec_in"] = 0.0
    state["exec_out"] = 0.0
    print(f"\n===== case {text_label} =====", flush=True)
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(20, 20), QPoint(400, 400))
    QApplication.sendEvent(box.viewport(), ev)
    print(
        f"[t={time.time() - state['t0']:.2f}s] sendEvent 返回（exec_in={state['exec_in']:.2f} exec_out={state['exec_out']:.2f}）",
        flush=True,
    )


def run():
    QTimer.singleShot(1500, lambda: close_popup("无字-1.5s"))
    QTimer.singleShot(5000, lambda: close_popup("无字-5s"))
    QTimer.singleShot(9000, lambda: do_click("有字", "hello 世界"))
    QTimer.singleShot(10500, lambda: close_popup("有字-1.5s"))
    QTimer.singleShot(14000, lambda: close_popup("有字-5s"))
    QTimer.singleShot(20000, app.quit)
    do_click("无字", "")


QTimer.singleShot(300, run)
app.exec()
state["done"] = True
print(f"\n主循环退出。exec_in={state['exec_in']:.2f}s exec_out={state['exec_out']:.2f}s")
print("采样条数:", len(state["stacks"]))
for ts, stack in state["stacks"]:
    print(f"--- t={ts}s ---\n{stack}")
