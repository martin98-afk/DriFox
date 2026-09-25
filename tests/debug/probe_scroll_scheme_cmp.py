# -*- coding: utf-8 -*-
"""临时探针：对比三种滚动参数方案的主观手感指标（用完即删）

背景：chat_scroll_area 用 qfluentwidgets SingleDirectionScrollArea，内部
FixedStepSmoothScrollEngine 默认 duration=400ms / stepRatio=1.5 /
acceleration=1 / LINEAR。实测在用户自然滚速（每格 200-400ms）下产生
「三角形速度曲线」：起步 3 帧不动 → 峰值 → 收尾 3 帧几乎不动，
每格一次「等→冲→停」，即用户体感的「划一下停一下」。

本探针用**真实 Qt 定时器**（不手动 _smoothMove，避免双倍驱动污染），
对比候选方案的三个客观指标：
  1. 每格 mishandle：起步延迟帧数（静止多久才开始动）
  2. 单次静止段最长时长（ms）—— 直接对应"停一下"的体感
  3. 每格总位移（px）—— 是否过冲（原生基准 60px/notch）

用法：python tests/debug/probe_scroll_scheme_cmp.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false")

from PyQt5.QtCore import QPoint, Qt, QTimer  # noqa: E402
from PyQt5.QtGui import QWheelEvent  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QVBoxLayout,
    QWidget,
)

app = QApplication([])

from qfluentwidgets import SingleDirectionScrollArea  # noqa: E402
from qfluentwidgets.common.smooth_scroll import SmoothMode  # noqa: E402

_FRAME_MS = 16


def build():
    area = SingleDirectionScrollArea()
    content = QWidget()
    lay = QVBoxLayout(content)
    for i in range(400):
        lab = QLabel(f"row {i}")
        lab.setFixedHeight(120)
        lay.addWidget(lab)
    area.setWidget(content)
    area.setWidgetResizable(True)
    area.resize(900, 700)
    area.show()
    app.processEvents()
    return area


def notch(area, delta=-120):
    e = QWheelEvent(
        QPoint(10, 10), QPoint(10, 10), QPoint(), QPoint(0, delta),
        delta, Qt.Vertical, Qt.NoButton, Qt.NoModifier,
    )
    app.sendEvent(area.viewport(), e)


def measure(mode, duration, step_ratio, accel, gap_ms, rounds=6):
    """真实 60fps 驱动，返回 (起步静止帧, 最长静止帧, 每格位移px)"""
    area = build()
    area.setSmoothMode(mode)
    eng = area.smoothScroll.fixedStepScrollEngine
    eng.duration = duration
    eng.stepRatio = step_ratio
    eng.acceleration = accel
    bar = area.verticalScrollBar()
    bar.setValue(0)
    eng.stepsLeftQueue.clear()
    eng.scrollStamps.clear()
    app.processEvents()

    samples = []
    elapsed = [0]
    fired = [0]
    total_ms = gap_ms * rounds + 300

    def tick():
        elapsed[0] += _FRAME_MS
        want = int(elapsed[0] // gap_ms) + 1
        if want > fired[0]:
            fired[0] = want
            notch(area)
        eng._smoothMove()
        samples.append((elapsed[0], bar.value()))

    timer = QTimer()
    timer.setInterval(_FRAME_MS)
    timer.timeout.connect(tick)
    timer.start()
    QTimer.singleShot(total_ms + 100, app.quit)
    app.exec_()
    timer.stop()

    vals = [v for _, v in samples]
    diffs = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]

    # 第一格：起步延迟 = 首个非零位移之前的帧数
    start_delay = 0
    for d in diffs:
        if d != 0:
            break
        start_delay += 1

    # 全程最长静止段
    runs, cur = [], 0
    for d in diffs:
        if d == 0:
            cur += 1
        else:
            if cur:
                runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)

    per_notch = bar.value() / max(fired[0], 1)
    return start_delay, (max(runs) if runs else 0) * _FRAME_MS, per_notch


def main():
    # 原生基准：QScrollArea 一格 = 60px，无动画，零延迟
    print("[基准] 原生 QScrollArea: 每格 60px, 起步延迟 0ms, 无静止段")
    print()

    schemes = [
        ("现状 LINEAR/400/1.5/accel1", SmoothMode.LINEAR, 400, 1.5, 1),
        ("匀速 CONSTANT/400/1.5/accel1", SmoothMode.CONSTANT, 400, 1.5, 1),
        ("短时 LINEAR/200/1.0/accel0", SmoothMode.LINEAR, 200, 1.0, 0),
        ("短时 CONSTANT/180/1.0/accel0", SmoothMode.CONSTANT, 180, 1.0, 0),
        ("短时 LINEAR/160/1.0/accel0", SmoothMode.LINEAR, 160, 1.0, 0),
        ("对齐原生 LINEAR/240/0.625/accel0", SmoothMode.LINEAR, 240, 0.625, 0),
    ]

    for gap in (250, 350, 500):
        print(f"===== 用户每格间隔 {gap}ms（滚轮 {gap}ms 一格）=====")
        print(f"{'方案':<34} {'起步延迟':>10} {'最长静止':>10} {'每格位移':>10}")
        for name, mode, dur, sr, ac in schemes:
            delay, dead, per = measure(mode, dur, sr, ac, gap)
            flag = ""
            if per > 80:
                flag = "  ← 过冲"
            elif per < 50:
                flag = "  ← 偏慢"
            print(f"{name:<34} {delay*16:>8}ms {dead:>8}ms {per:>8.0f}px{flag}")
        print()


if __name__ == "__main__":
    main()
