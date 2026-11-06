# -*- coding: utf-8 -*-
"""动态光影覆盖层（backgrounds.overlay）

循环播放一段黑白光影视频，paintEvent 内先铺亮度补偿色、再以
CompositionMode_Multiply + 低不透明度把视频帧画到对话区背景上：
视频白色区域（叶隙天光）与底色相乘后≈底色（视觉透明），暗部（树影）
形成缓慢流动的光影。复刻自 HanaAgent LeavesOverlay（Apache-2.0）。

Qt 侧无 CSS mix-blend-mode，混合在背景层内部完成：覆盖层只与下方
背景内容（SceneLayer 的图/纯色）相乘，上层 UI 卡片不受影响，可读性无损。

Z 序约定：SceneLayer < VideoOverlayLayer < UI 控件
（挂载方负责：layer.lower() + scene_layer.stackUnder(layer)）。
"""
from typing import Callable, Optional

from PyQt5.QtCore import QEvent, QRect, QSize, Qt, QUrl
from PyQt5.QtGui import QColor, QImage, QPainter
from PyQt5.QtMultimedia import (
    QAbstractVideoBuffer,
    QAbstractVideoSurface,
    QMediaContent,
    QMediaPlayer,
    QVideoFrame,
)
from PyQt5.QtWidgets import QWidget

# 仅声明 32 位像素格式：规避 24 位字节序坑；WMF 解码器输出 RGB32 足够
_PIX_FMT = {
    QVideoFrame.Format_RGB32: QImage.Format_RGB32,
    QVideoFrame.Format_ARGB32: QImage.Format_ARGB32,
    QVideoFrame.Format_ARGB32_Premultiplied: QImage.Format_ARGB32_Premultiplied,
}


class _FrameSurface(QAbstractVideoSurface):
    """把 QMediaPlayer 解码帧搬成 QImage，回调宿主请求重绘"""

    def __init__(self, on_frame_ready: Callable[[], None], parent=None):
        super().__init__(parent)
        self._on_frame_ready = on_frame_ready
        self.frame: Optional[QImage] = None  # 播放间隙保留旧帧，防黑闪

    def supportedPixelFormats(self, handle_type):
        # PyQt5 的 HandleType 枚举在 QAbstractVideoBuffer 上；VideoSurface 模式
        # 恒走 NoHandle 查询，直接返回支持列表即可
        return list(_PIX_FMT.keys())

    def present(self, frame: QVideoFrame) -> bool:
        if not frame.isValid():
            return False
        qimg_fmt = _PIX_FMT.get(frame.pixelFormat())
        if qimg_fmt is None:
            return False
        if not frame.map(QAbstractVideoBuffer.ReadOnly):
            return False
        try:
            # bits() 只在 map 期间有效，必须 copy()
            img = QImage(frame.bits(), frame.width(), frame.height(),
                         frame.bytesPerLine(), qimg_fmt).copy()
        finally:
            frame.unmap()
        if img.isNull():
            return False
        self.frame = img
        self._on_frame_ready()
        return True


class VideoOverlayLayer(QWidget):
    """对话区动态光影覆盖层

    cfg 字段（来自 yaml `backgrounds.overlay`）：
      - video             : str  视频路径（主题目录相对路径或绝对路径）
      - opacity           : float 0.0~1.0，默认 0.28
      - brightness_boost  : str  亮度补偿色（CSS color），补偿 multiply 变暗
      - position          : str  cover 裁剪的垂直对齐 top/center/bottom，默认 center。
                                 竖版素材（树影集中在顶部）在横窗内必须配 top，
                                 否则居中裁剪只显示原图中段空白区（实测 2026-11-06）
      - enabled           : bool 默认 True
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._surface = _FrameSurface(self._on_frame_ready, self)
        self._player = QMediaPlayer(self, QMediaPlayer.VideoSurface)
        self._player.setVideoOutput(self._surface)
        self._player.setMuted(True)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._opacity = 0.28
        self._boost: Optional[QColor] = None
        self._position = "center"
        # 预混合缓冲：把透明度烘焙进帧（见 _bake_opacity），复用避免每帧分配
        self._blend_buf: Optional[QImage] = None

    # ── 配置 ─────────────────────────────────────────

    def apply_config(self, cfg: Optional[dict], video_resolver=None) -> None:
        """应用配置；None / enabled=False / 无 video 时停止并清空"""
        self.stop_video()
        if not cfg or not cfg.get("enabled", True):
            return
        video = cfg.get("video")
        if not video:
            return
        path = video_resolver(video) if video_resolver else video
        self._opacity = max(0.0, min(float(cfg.get("opacity", 0.28)), 1.0))
        boost = cfg.get("brightness_boost")
        self._boost = QColor(boost) if boost else None
        pos = str(cfg.get("position") or "center").lower()
        self._position = pos if pos in ("top", "center", "bottom") else "center"
        self._player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
        self._player.play()

    def stop_video(self) -> None:
        self._player.stop()
        self._player.setMedia(QMediaContent())
        self._surface.frame = None
        self._boost = None
        self._opacity = 0.28
        self._position = "center"
        self.update()

    # ── 播放控制 ─────────────────────────────────────

    def _on_frame_ready(self):
        self.update()

    def _on_status(self, status):
        # Qt5 无 loop 属性；素材为正放+倒放无缝剪辑，回头重播无跳变
        if status == QMediaPlayer.EndOfMedia:
            self._player.play()

    # ── 绘制 ─────────────────────────────────────────

    def paintEvent(self, event):
        frame = self._surface.frame
        p = QPainter(self)
        try:
            if self._boost is not None:
                p.fillRect(self.rect(), self._boost)
            if frame is not None and not frame.isNull():
                processed = self._bake_opacity(frame)
                # 全强度 multiply（不 setOpacity）：raster 引擎对
                # Multiply+opacity 组合会忽略透明度（实测黑屏），透明度已在
                # _bake_opacity 里烘进帧，此处数学与 HanaAgent 公式精确等价
                p.save()
                p.setCompositionMode(QPainter.CompositionMode_Multiply)
                p.drawImage(self._cover_rect(processed.size(), self.rect(), self._position), processed)
                p.restore()
        finally:
            p.end()

    def _bake_opacity(self, frame: QImage) -> QImage:
        """把覆盖层透明度烘进帧：v' = 255×(1-opacity) + v×opacity

        与白色预混合后全强度 multiply：dst×v'/255
        = dst×[(1-opacity) + opacity×v/255]
        = HanaAgent 的 SourceOver(multiply(src), opacity) 期望公式。
        """
        buf = self._blend_buf
        if buf is None or buf.size() != frame.size() or buf.format() != QImage.Format_RGB32:
            buf = QImage(frame.size(), QImage.Format_RGB32)
            self._blend_buf = buf
        pt = QPainter(buf)
        try:
            pt.setCompositionMode(QPainter.CompositionMode_Source)
            pt.fillRect(buf.rect(), QColor(255, 255, 255))
            pt.setOpacity(self._opacity)  # 帧贡献 opacity，白底贡献 1-opacity
            pt.setCompositionMode(QPainter.CompositionMode_SourceOver)
            pt.drawImage(0, 0, frame)
        finally:
            pt.end()
        return buf

    @staticmethod
    def _cover_rect(src: QSize, dst: QRect, position: str = "center") -> QRect:
        """object-fit: cover + object-position——等比放大铺满，溢出侧按 position 对齐裁剪

        position 控制溢出方向的对齐（CSS object-position 的 top/center/bottom）：
        竖版素材在横窗内溢出的是高度方向，top=显示原图顶部段（树影密集区）。
        """
        if src.isEmpty() or dst.isEmpty():
            return dst
        scale = max(dst.width() / src.width(), dst.height() / src.height())
        w = int(src.width() * scale + 0.5)
        h = int(src.height() * scale + 0.5)
        x = dst.x() + (dst.width() - w) // 2  # 水平恒居中（横窗裁竖图时宽恰好铺满）
        overflow = dst.height() - h
        if position == "top":
            y = dst.y()
        elif position == "bottom":
            y = dst.y() + overflow
        else:
            y = dst.y() + overflow // 2
        return QRect(x, y, w, h)

    # ── 几何同步 ─────────────────────────────────────

    def sync_geometry(self) -> None:
        parent = self.parent()
        if parent is not None:
            self.setGeometry(parent.rect())

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize and watched is self.parent():
            self.sync_geometry()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_geometry()
