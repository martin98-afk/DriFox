# -*- coding: utf-8 -*-
"""临时探针：打开 left 容器 UI 卡片，抓「对话区 tab 动画」的触发者。

patch 三个可疑入口，打印调用栈：
1. TabIndicatorController.move_to(animate=True)  —— 标题栏胶囊滑动
2. _ContentStack.setCurrentIndex(direction!=0)   —— 对话区整页 push 滑动
3. _ContentStack.slide_over / slide_swap         —— 覆盖层内滑动
4. CardContainer.overlayStateChanged             —— 覆盖层显隐
"""

import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from PyQt5.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PyQt5.QtWidgets import QApplication, QWidget

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication(["exp"])

from app.widgets.custom_title_bar import TabIndicatorController  # noqa: E402
from app.widgets.tab_manager_window import TabManagerWindow, _ContentStack  # noqa: E402
from app.widgets.cards.card_container import CardContainer  # noqa: E402
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry  # noqa: E402

TRACE = False


def _dump(tag, detail=""):
    if not TRACE:
        return
    print(f"\n===== [ANIM] {tag} {detail}", file=sys.stderr)
    stack = traceback.extract_stack()[:-2]
    for fr in stack[-12:]:
        print(f"    {os.path.basename(fr.filename)}:{fr.lineno} {fr.name}", file=sys.stderr)


_orig_move = TabIndicatorController.move_to


def _patched_move(self, target, *, animate):
    if animate:
        _dump("TabIndicator.move_to(animate=True)", f"target={target.x()},{target.y()} {target.width()}x{target.height()}")
    return _orig_move(self, target, animate=animate)


TabIndicatorController.move_to = _patched_move

_orig_setidx = _ContentStack.setCurrentIndex


def _patched_setidx(self, i, direction=0):
    if direction != 0:
        _dump("ContentStack.setCurrentIndex", f"i={i} dir={direction}")
    return _orig_setidx(self, i, direction)


_ContentStack.setCurrentIndex = _patched_setidx

_orig_over = _ContentStack.slide_over


def _patched_over(self, direction=1):
    _dump("ContentStack.slide_over", f"dir={direction}")
    return _orig_over(self, direction)


_ContentStack.slide_over = _patched_over

_orig_swap = _ContentStack.slide_swap


def _patched_swap(self, direction=1, swap=None):
    _dump("ContentStack.slide_swap", f"dir={direction}")
    return _orig_swap(self, direction, swap)


_ContentStack.slide_swap = _patched_swap

_orig_ovl = CardContainer.overlayStateChanged


def _pump(ms=30):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def main():
    global TRACE
    tm = TabManagerWindow.create_instance()
    tm.complete_construction()
    tm.resize(1400, 900)
    tm.show()
    _pump(400)
    _pump(600)

    panel = tm._tab_panel
    if panel._collapsed:
        panel.set_collapsed(False)
        tm._on_sidebar_toggled(False, animate=False)
        _pump(300)

    print("[probe] titleBar tabs =", list(tm.titleBar._tabs.keys()))
    print("[probe] left_card_active =", tm._left_card_area_active())
    print("[probe] content_stack index =", tm._content_stack.currentIndex())

    reg = UIPluginRegistry.get_instance()
    reg.register_floating_card("probe", "probe-left", QWidget, "left", title="探针卡")

    print("\n---- 打开 left 卡片（采集动画触发）----")
    TRACE = True
    reg.toggle_floating_card("probe-left")
    for _ in range(8):
        QApplication.processEvents()
    _pump(600)
    TRACE = False
    print("---- 打开结束 ----")
    print("[probe] left_card_active =", tm._left_card_area_active())
    sp = tm._sidebar_splitter
    print("[probe] sidebarSplitter sizes =", sp.sizes())
    print("[probe] tab_panel collapsed =", tm._tab_panel._collapsed)
    print("[probe] content_stack index =", tm._content_stack.currentIndex())
    print("[probe] titleBar tabs =", list(tm.titleBar._tabs.keys()))
    print("[probe] indicator running =", tm.titleBar._indicator_ctl.running)

    print("\n---- 关闭 left 卡片 ----")
    TRACE = True
    reg.hide_floating_card_globally("probe-left")
    for _ in range(8):
        QApplication.processEvents()
    _pump(600)
    TRACE = False
    print("---- 关闭结束 ----")


if __name__ == "__main__":
    main()
