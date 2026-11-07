# -*- coding: utf-8 -*-
"""二分绘制路径：boost / SourceOver / Multiply 逐步 grab，定位黑从哪步进来"""
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QImage

SAMPLE = r"D:/work/DriFox/.drifox/screenshots/hana_first_frame.png"


def px(widget, x=50, y=50):
    c = QColor(widget.grab().toImage().pixel(x, y))
    return (c.red(), c.green(), c.blue(), c.alpha())


def main():
    from PyQt5.QtWidgets import QApplication, QWidget
    from app.widgets.video_overlay_layer import VideoOverlayLayer

    app = QApplication(sys.argv)
    layer = VideoOverlayLayer()
    layer.resize(100, 100)
    layer._boost = QColor(255, 255, 255)
    frame = QImage(SAMPLE).scaled(100, 100)
    print("样帧格式:", frame.format(), "(4=ARGB32_Premul, 5=RGB32)")
    layer._surface.frame = frame
    layer._opacity = 0.28
    layer.show()

    # A: 只补偿色（frame 置 None）
    layer._surface.frame = None
    print("A 只补偿色:      ", px(layer))

    # B: bake 后的帧
    baked = layer._bake_opacity(frame)
    c = QColor(baked.pixel(50, 50))
    print("B bake 帧像素:   ", (c.red(), c.green(), c.blue(), c.alpha()))

    # C: SourceOver 画 bake 帧（临时改模式）
    layer._surface.frame = frame
    orig_paint = layer.paintEvent

    def paint_sover(event):
        from PyQt5.QtGui import QPainter
        p = QPainter(layer)
        p.fillRect(layer.rect(), layer._boost)
        processed = layer._bake_opacity(layer._surface.frame)
        p.drawImage(0, 0, processed)
        p.end()

    layer.paintEvent = paint_sover
    layer.grab()
    print("C SourceOver:    ", px(layer))

    # D: Multiply 画 bake 帧
    def paint_mul(event):
        from PyQt5.QtGui import QPainter
        p = QPainter(layer)
        p.fillRect(layer.rect(), layer._boost)
        processed = layer._bake_opacity(layer._surface.frame)
        p.setCompositionMode(QPainter.CompositionMode_Multiply)
        p.drawImage(0, 0, processed)
        p.end()

    layer.paintEvent = paint_mul
    layer.grab()
    print("D Multiply:      ", px(layer))

    # E: 完整原版 paintEvent（含 cover_rect）
    layer.paintEvent = orig_paint.__get__(layer)
    layer.grab()
    print("E 完整 paintEvent:", px(layer))
    app.quit()


if __name__ == "__main__":
    main()
