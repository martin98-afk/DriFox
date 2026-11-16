# -*- coding: utf-8 -*-
"""[selfcheck] scroll_tuning 接管器行为校验（临时，跑完删）。"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QWheelEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication, QLabel, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

from app.utils.scroll_tuning import install_fast_wheel  # noqa: E402


def wheel(area, notches=1):
    vp = area.viewport()
    pos = QPointF(vp.width() / 2, vp.height() / 2)
    ev = QWheelEvent(
        pos,
        QPointF(pos),
        QPoint(0, 0),
        QPoint(0, -120 * notches),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(vp, ev)
    return ev.isAccepted()


def main():
    app = QApplication(sys.argv)
    area = QScrollArea()
    area.resize(600, 400)
    inner = QWidget()
    lay = QVBoxLayout(inner)
    for i in range(200):
        lay.addWidget(QLabel(f"row {i}"))
    area.setWidgetResizable(True)
    area.setWidget(inner)
    area.show()
    app.processEvents()

    print("wheelScrollLines =", QApplication.wheelScrollLines())
    print("singleStep =", area.verticalScrollBar().singleStep())

    area.verticalScrollBar().setValue(1000)
    print("install =", install_fast_wheel(area), "重装 =", install_fast_wheel(area))

    v0 = area.verticalScrollBar().value()
    wheel(area, 1)
    v1 = area.verticalScrollBar().value()
    print(f"首格: {v0} -> {v1} 距离={v0 - v1}")

    for _ in range(4):
        wheel(area, 1)
    v2 = area.verticalScrollBar().value()
    print(f"连续 5 格后: {v2}  单格均距={(v0 - v2) / 5:.1f}")

    area.verticalScrollBar().setValue(0)
    ok = wheel(area, -1)  # 向上滚 = 已到顶
    print(f"到顶仍吞事件={ok}（应为 False，交给外层）")
    area.verticalScrollBar().setValue(area.verticalScrollBar().maximum())
    ok = wheel(area, 1)  # 向下滚 = 已到底
    print(f"到底仍吞事件={ok}（应为 False）")

    # 饱和加速：连续 20 格后单格应接近 step*1.5*2 = 180
    area.verticalScrollBar().setValue(0)
    for _ in range(20):
        wheel(area, 1)
    v_start = area.verticalScrollBar().value()
    wheel(area, 1)
    print(f"饱和后单格距离={area.verticalScrollBar().value() - v_start}（预期 ≈ {20 * 3 * 1.5 * 2:.0f}）")


if __name__ == "__main__":
    main()
