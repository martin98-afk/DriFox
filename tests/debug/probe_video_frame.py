# -*- coding: utf-8 -*-
"""诊断：用 QMediaPlayer 解码 leaves-overlay.mp4，落最后一帧 PNG 并统计亮度

用法：python tests/debug/probe_video_frame.py
判读：平均亮度 <30 = 解码黑帧（问题在视频/解码器）；亮度正常（>100）= 问题在绘制侧。
"""
import sys

from PyQt5.QtCore import QTimer, QUrl
from PyQt5.QtGui import QImage
from PyQt5.QtMultimedia import (
    QAbstractVideoBuffer,
    QAbstractVideoSurface,
    QMediaContent,
    QMediaPlayer,
    QVideoFrame,
)
from PyQt5.QtWidgets import QApplication

VIDEO = r"D:/work/DriFox/plugins/system-themes/themes/crema/leaves-overlay.mp4"
OUT = r"D:/work/DriFox/.drifox/screenshots/hana_first_frame.png"

_FMT = {
    QVideoFrame.Format_RGB32: QImage.Format_RGB32,
    QVideoFrame.Format_ARGB32: QImage.Format_ARGB32,
    QVideoFrame.Format_ARGB32_Premultiplied: QImage.Format_ARGB32_Premultiplied,
}

frames = []
meta = []


class ProbeSurface(QAbstractVideoSurface):
    def supportedPixelFormats(self, handle_type):
        return list(_FMT.keys())

    def present(self, frame: QVideoFrame) -> bool:
        meta.append(f"帧: valid={frame.isValid()} fmt={frame.pixelFormat()} "
                    f"{frame.width()}x{frame.height()} bpl={frame.bytesPerLine()}")
        fmt = _FMT.get(frame.pixelFormat())
        if not frame.isValid() or fmt is None:
            return False
        if not frame.map(QAbstractVideoBuffer.ReadOnly):
            meta.append("  map 失败")
            return False
        try:
            img = QImage(frame.bits(), frame.width(), frame.height(),
                         frame.bytesPerLine(), fmt).copy()
        finally:
            frame.unmap()
        frames.append(img)
        return True


def main():
    app = QApplication(sys.argv)
    surface = ProbeSurface()
    player = QMediaPlayer(None, QMediaPlayer.VideoSurface)
    player.setVideoOutput(surface)
    player.setMedia(QMediaContent(QUrl.fromLocalFile(VIDEO)))
    player.play()

    def dump():
        player.pause()
        print("mediaStatus:", player.mediaStatus(), "error:", player.errorString())
        for m in meta[:6]:
            print(" ", m)
        if not frames:
            print("结论：没有收到任何帧 → 解码器/格式协商失败")
            app.quit()
            return
        img = frames[len(frames) // 2]  # 取中段帧
        total, n = 0, 0
        for x in range(0, img.width(), 16):
            for y in range(0, img.height(), 16):
                total += img.pixelColor(x, y).red()
                n += 1
        avg = total / max(n, 1)
        img.save(OUT)
        print(f"收到帧数: {len(frames)}  中段帧 {img.width()}x{img.height()}  平均亮度: {avg:.0f}")
        print("结论：", "解码黑帧（问题在视频/解码器）" if avg < 30 else "帧内容正常（问题在绘制侧）")
        print("样帧已存:", OUT)
        app.quit()

    QTimer.singleShot(5000, dump)
    app.exec_()


if __name__ == "__main__":
    main()
