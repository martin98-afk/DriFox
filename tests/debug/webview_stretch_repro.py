# -*- coding: utf-8 -*-
"""QWebEngineView 高度跳变 → 文字竖向拉伸伪影的最小复现 harness。

机制假设：widget 高度大跳（宽度不变）时，Qt 侧用旧帧纹理拉伸填满新几何，
Chromium 新帧到达后恢复 → 文字瞬间竖向拉长（消息卡片流式 burst 场景同款）。

用法：
    .venv/Scripts/python.exe tests/debug/webview_stretch_repro.py [--step]
      --step  规避策略对照：高度分步进到位（每帧 ≤160px）而非一次大跳
输出：
    tests/debug/_stretch_shots/  跳变前后连续抓帧
    控制台打印「拉伸判定」：各帧行投影与稳定帧的垂直缩放相似度
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# 与主程序一致：渲染 env 必须在 Qt import 之前落地
from app.utils.render_env import apply_render_env  # noqa: E402

apply_render_env(os.path.join(".drifox6", "app.config"))

from PySide6.QtCore import QPoint, QRect, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_stretch_shots")
HTML = (
    "<body style='margin:12px;font-size:15px;line-height:1.7;font-family:sans-serif'>"
    + "<p>这是一段用于复现文字竖向拉伸伪影的中文文本。</p>" * 12
    + "</body>"
)
STEP_MODE = "--step" in sys.argv
STEP_PX = 160
JUMP_TARGET = 900
BASE_H = 120
SHOT_INTERVAL_MS = 12
SHOOT_COUNT = 10


def grab(app: QApplication, view: QWebEngineView, tag: str) -> None:
    """全屏抓取后按 DPR 裁出窗口区域，坐标换算确定性可控。"""
    win = view.window()
    screen = win.screen() or app.primaryScreen()
    full = screen.grabWindow(0)  # 整屏（物理像素）
    rel = win.mapToGlobal(QPoint(0, 0)) - screen.geometry().topLeft()
    dpr = screen.devicePixelRatio()
    rect = QRect(int(rel.x() * dpr), int(rel.y() * dpr), int(win.width() * dpr), int(win.height() * dpr))
    full.copy(rect).save(os.path.join(OUT_DIR, f"{tag}.png"))


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    app = QApplication(sys.argv)
    view = QWebEngineView()
    view.setWindowFlag(Qt.WindowStaysOnTopHint)
    view.setFixedWidth(760)
    view.setFixedHeight(BASE_H)
    view.move(80, 80)
    view.setHtml(HTML)
    view.show()

    if "--cycle" in sys.argv:
        # 循环跳变 + 同步内容注入：模拟流式 burst（排版延迟拉长旧帧窗口期）
        def cycle_tick() -> None:
            cur = view.height()
            target = JUMP_TARGET if cur < JUMP_TARGET else BASE_H
            view.page().runJavaScript(
                "document.body.innerHTML += '<p>追加内容迫使 Chromium 重新排版渲染。</p>'.repeat(20);"
            )
            if STEP_MODE:
                # 仅几何路径不同：16ms 一帧一小步到位，内容节奏与直跳模式一致
                steps = max(1, abs(target - cur) // STEP_PX)
                for i in range(1, steps + 1):
                    nxt = round(cur + (target - cur) * i / steps)
                    QTimer.singleShot(i * 16, lambda v=nxt: view.setFixedHeight(v))
            else:
                view.setFixedHeight(target)

        win = view.window()
        QTimer.singleShot(
            200,
            lambda: print(
                "RECT_PHYS",
                int((win.mapToGlobal(QPoint(0, 0)).x() - win.screen().geometry().left()) * win.screen().devicePixelRatio()),
                int((win.mapToGlobal(QPoint(0, 0)).y() - win.screen().geometry().top()) * win.screen().devicePixelRatio()),
                int(win.width() * win.screen().devicePixelRatio()),
                int(win.height() * win.screen().devicePixelRatio()),
                flush=True,
            ),
        )
        QTimer.singleShot(2500, lambda: print("CYCLE_START", flush=True))
        cycle_timer = QTimer()
        cycle_timer.setInterval(800)
        cycle_timer.timeout.connect(cycle_tick)
        QTimer.singleShot(2500, cycle_timer.start)
        QTimer.singleShot(2500 + 9000, cycle_timer.stop)
        QTimer.singleShot(2500 + 9500, app.quit)
        sys.exit(app.exec())

    state = {"phase": "warmup", "shot": 0}

    def on_tick() -> None:
        if state["phase"] == "warmup":
            state["phase"] = "jump"
            state["shot"] = 0
            grab(app, view, "f0_before")
            if STEP_MODE:
                view.setFixedHeight(min(BASE_H + STEP_PX, JUMP_TARGET))
            else:
                view.setFixedHeight(JUMP_TARGET)
            grab(app, view, "f1_at_jump")
        elif state["phase"] == "jump":
            state["shot"] += 1
            grab(app, view, f"f{state['shot'] + 1}_after")
            if STEP_MODE and view.height() < JUMP_TARGET and state["shot"] % 2 == 0:
                view.setFixedHeight(min(view.height() + STEP_PX, JUMP_TARGET))
            if state["shot"] >= SHOOT_COUNT:
                state["phase"] = "settle"
                grab(app, view, "fZ_settled")

    timer = QTimer()
    timer.timeout.connect(on_tick)
    QTimer.singleShot(2500, lambda: (print("t=2.5s 触发跳变"), timer.start(SHOT_INTERVAL_MS)))
    QTimer.singleShot(2500 + SHOT_INTERVAL_MS * (SHOOT_COUNT + 4) + 1500, app.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
