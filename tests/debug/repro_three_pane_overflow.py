# -*- coding: utf-8 -*-
"""复现：三窗格 900/850 窗口下 360 默认宽下限的溢出行为

复刻 TestDockMinLockUpgrade::test_medium_window_three_pane_dock_fits，
打印每步 splitter 分配，定位断言失败的具体数值。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget, QSplitter

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication(["repro"])

from app.widgets.cards.card_container import CardContainer, ContainerType  # noqa: E402


def _pump(ms=300):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def _card(min_w, min_h):
    w = QWidget()
    w.setMinimumSize(min_w, min_h)
    return w


def run(win_w):
    left = CardContainer(ContainerType.LEFT)
    right = CardContainer(ContainerType.RIGHT)
    content = QWidget()
    content.setMinimumWidth(320)
    splitter = QSplitter(Qt.Horizontal)
    splitter.addWidget(left)
    splitter.addWidget(content)
    splitter.addWidget(right)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    splitter.setStretchFactor(2, 0)
    splitter.setChildrenCollapsible(False)
    splitter.setHandleWidth(6)
    left.enable_dock_mode(splitter)
    right.enable_dock_mode(splitter)
    lc = _card(300, 60)
    left.add_card("file", lc)
    rc = _card(300, 60)
    right.add_card("context", rc)
    splitter.resize(win_w, 600)
    splitter.show()
    lc.show()
    left.show()
    rc.show()
    right.show()
    print(f"[win={win_w}] pre-expand sizes={splitter.sizes()}")
    left._do_expand()
    print(f"[win={win_w}] after L expand sizes={splitter.sizes()} L.min={left.minimumWidth()} L.w={left.width()}")
    right._do_expand()
    _pump(300)
    sizes = splitter.sizes()
    handle_total = splitter.handleWidth() * (splitter.count() - 1)
    print(f"[win={win_w}] final sizes={sizes} sum+handles={sum(sizes) + handle_total} splitter.w={splitter.width()}")
    print(f"[win={win_w}] L: w={left.width()} min={left.minimumWidth()} | "
          f"R: w={right.width()} min={right.minimumWidth()}")
    print(f"[win={win_w}] L.natural(sizeHint)= {left.sizeHint().width()} target 默认下限应生效=360")


if __name__ == "__main__":
    for w in (900, 850):
        run(w)
