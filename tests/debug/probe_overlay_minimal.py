# -*- coding: utf-8 -*-
"""最小重现：干净 QWidget 窗口 + VideoOverlayLayer，2 秒后截图落盘

判读：截图若为黑 → 组件自身在独立环境下就黑（组件 bug）；
     截图若为暖色树影 → DriFox 宿主环境的合成路径问题（查主窗口渲染配置）。
"""
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor

OUT = r"D:/work/DriFox/.drifox/screenshots/overlay_minimal_repro.png"


def main():
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication(sys.argv)
    host = QWidget()
    host.resize(1200, 700)
    host.setStyleSheet("background-color: #F5EFE4;")  # 模拟暖纸底

    from app.widgets.video_overlay_layer import VideoOverlayLayer

    layer = VideoOverlayLayer(host)
    layer.apply_config({
        "video": r"D:/work/DriFox/plugins/system-themes/themes/crema/leaves-overlay.mp4",
        "opacity": 0.28,
        "brightness_boost": "rgba(255, 253, 247, 0.12)",
        "enabled": True,
    })
    layer.show()
    layer.sync_geometry()
    layer.lower()
    host.show()

    def dump():
        pix = host.grab()
        pix.save(OUT)
        c = QColor(pix.toImage().pixel(600, 80))  # 顶部空白区（无卡片遮挡）
        print("顶部空白区像素:", c.red(), c.green(), c.blue(), "alpha:", c.alpha())
        print("截图已存:", OUT)
        app.quit()

    QTimer.singleShot(4000, dump)
    app.exec()


if __name__ == "__main__":
    main()
