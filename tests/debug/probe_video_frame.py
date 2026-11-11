# -*- coding: utf-8 -*-
"""诊断：用 QMediaPlayer 解码 leaves-overlay.mp4，落最后一帧 PNG 并统计亮度

用法：python tests/debug/probe_video_frame.py
判读：平均亮度 <30 = 解码黑帧（问题在视频/解码器）；亮度正常（>100）= 问题在绘制侧。
"""
import sys

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import QApplication

VIDEO = r"D:/work/DriFox/.drifox/plugins/crema-leaves/themes/leaves-overlay.mp4"
OUT = r"D:/work/DriFox/.drifox/screenshots/hana_first_frame.png"

frames = []
meta = []


class ProbeSink(QVideoSink):
    """Qt6 移除了 QAbstractVideoSurface/present 协议：
    QVideoSink.videoFrameChanged 逐帧 toImage（Qt 内部完成像素格式转换）"""

    def __init__(self):
        super().__init__()
        self.videoFrameChanged.connect(self._present)

    def _present(self, frame: QVideoFrame) -> None:
        meta.append(f"帧: valid={frame.isValid()} fmt={frame.pixelFormat()} "
                    f"{frame.width()}x{frame.height()}")
        if not frame.isValid():
            return
        img = frame.toImage()
        if img.isNull():
            meta.append("  toImage 失败")
            return
        frames.append(img)


def main():
    app = QApplication(sys.argv)
    sink = ProbeSink()
    player = QMediaPlayer()
    player.setVideoOutput(sink)
    player.setSource(QUrl.fromLocalFile(VIDEO))
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
    app.exec()


if __name__ == "__main__":
    main()
