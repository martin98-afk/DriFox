# -*- coding: utf-8 -*-
"""用真实 QMediaPlayer 播放流喂 VideoOverlayLayer，检查实时帧 alpha 与显示结果"""
import sys

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

OUT = r"D:/work/DriFox/.drifox/screenshots/overlay_live_repro.png"
VIDEO = r"D:/work/DriFox/.drifox/plugins/crema-leaves/themes/leaves-overlay.mp4"


def main():
    app = QApplication(sys.argv)
    host = QWidget()
    host.resize(1200, 700)
    host.setStyleSheet("background-color: #F5EFE4;")

    from app.widgets.video_overlay_layer import VideoOverlayLayer

    layer = VideoOverlayLayer(host)
    layer.apply_config({
        "video": VIDEO,
        "opacity": 0.28,
        "brightness_boost": "rgba(255, 253, 247, 0.12)",
        "enabled": True,
    })
    layer.show()
    layer.sync_geometry()
    layer.lower()
    host.show()

    info = {}

    def dump():
        f = layer._surface.frame
        if f is None:
            print("实时帧: None（surface 没收到帧）")
        else:
            c = QColor(f.pixel(f.width() // 2, f.height() // 2))
            info["fmt"] = f.format()
            info["center"] = (c.red(), c.green(), c.blue(), c.alpha())
            # 四角+中心采样
            samples = []
            for fx, fy in ((0.25, 0.25), (0.5, 0.5), (0.75, 0.75)):
                cc = QColor(f.pixel(int(f.width() * fx), int(f.height() * fy)))
                samples.append((cc.red(), cc.green(), cc.blue(), cc.alpha()))
            info["samples"] = samples
            print("实时帧 format:", f.format(), "（Qt6 ffmpeg 后端 toImage 输出，常见为 RGBA8888_Premultiplied）")
            print("采样(25%,25%)(50%,50%)(75%,75%) RGBA:", samples)
        pix = host.grab()
        pix.save(OUT)
        c2 = QColor(pix.toImage().pixel(600, 80))
        print("宿主顶部空白区像素:", c2.red(), c2.green(), c2.blue())
        print("截图:", OUT)
        app.quit()

    QTimer.singleShot(4000, dump)
    app.exec()


if __name__ == "__main__":
    main()
