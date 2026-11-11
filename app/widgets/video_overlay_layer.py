# -*- coding: utf-8 -*-
"""动态光影覆盖层（backgrounds.overlay）

循环播放一段黑白光影视频，paintEvent 内先铺亮度补偿色、再按 v 的明暗把
视频帧转成「黑色 + alpha」遮罩叠在对话区背景上：视频白色区域（叶隙天光）
alpha≈0 不压暗，暗部（树影）alpha 高形成缓慢流动的光影。
复刻自 HanaAgent LeavesOverlay（Apache-2.0）的视觉效果。

为什么不用 CompositionMode_Multiply（2026-11-06 实测）：真实播放时
raster 引擎对「解码器帧 + Multiply」的组合会输出远暗于理论值的结果
（1080x1920 帧实测 35~48，理论 236），离屏复算与 SourceOver 绘制同一帧
均正常，灰帧也正常，属引擎侧行为且不稳定。改用 SourceOver + alpha 遮罩，
数学等价（见 _build_mask）但不读目标像素，不受该行为影响。

Qt 侧无 CSS mix-blend-mode，混合在背景层内部完成：覆盖层只压暗下方
背景内容（SceneLayer 的图/纯色），上层 UI 卡片不受影响，可读性无损。

Z 序约定：SceneLayer < VideoOverlayLayer < UI 控件
（挂载方负责：layer.lower() + scene_layer.stackUnder(layer)）。
"""
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import QWidget

from app.utils.design_tokens import qcolor_from_token

# 遮罩变换降采样倍率：帧先缩到 1/n 再烘明暗遮罩（见 present）。
# 仅适用于低频光影素材；换成高频细节素材时应调低或改用高分辨率素材
_MASK_DOWNSCALE = 3


class _FrameSurface(QVideoSink):
    """把 QMediaPlayer 解码帧搬成 QImage，回调宿主请求重绘

    Qt6 移除了 QAbstractVideoSurface/present 协议与像素格式协商表：改为继承
    QVideoSink，videoFrameChanged 逐帧走 toImage() 由 Qt 内部完成像素格式
    转换（ffmpeg 后端 yuv420p 实测输出 RGBA8888_Premultiplied）。
    """

    def __init__(self, on_frame_ready: Callable[[], None], parent=None):
        super().__init__(parent)
        self._on_frame_ready = on_frame_ready
        self.frame: Optional[QImage] = None  # 播放间隙保留旧帧，防黑闪
        self.videoFrameChanged.connect(self._present)

    def _present(self, frame: QVideoFrame) -> None:
        if not frame.isValid():
            return
        img = frame.toImage()
        if img.isNull():
            return
        # 降采样后再烘遮罩：光影是低频信号，1/3 分辨率经 cover 放大视觉几乎
        # 无损，而灰度/反相/alpha 变换的像素量降到 1/9（1080x1920 实测单帧
        # paintEvent 9.8ms → 2.6ms，2026-11-07）；且竖版素材配 position=top
        # 时横窗只显示顶部约 1/3 画面，全分辨率处理本就大量浪费
        img = img.scaled(max(1, img.width() // _MASK_DOWNSCALE),
                         max(1, img.height() // _MASK_DOWNSCALE),
                         Qt.IgnoreAspectRatio, Qt.FastTransformation)
        self.frame = img
        self._on_frame_ready()
        return True


class VideoOverlayLayer(QWidget):
    """对话区动态光影覆盖层

    cfg 字段（来自 yaml `backgrounds.overlay`）：
      - video             : str  视频路径（主题目录相对路径或绝对路径）
      - opacity           : float 0.0~1.0，默认 0.28
      - brightness_boost  : str  亮度补偿色（CSS color，支持 rgba() 写法），
                                 补偿遮罩压暗（QColor 构造器不认 rgba()，
                                 必须走 qcolor_from_token，实测 2026-11-06）
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
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._surface)
        self._audio.setMuted(True)  # Qt6 静音在 QAudioOutput 上（QMediaPlayer.setMuted 实测绑定不可用）
        self._player.mediaStatusChanged.connect(self._on_status)
        self._opacity = 0.28
        self._boost: Optional[QColor] = None
        self._position = "center"
        # 遮罩缓冲：把帧明暗烘成黑色 alpha（见 _build_mask），复用避免每帧分配
        self._mask_buf: Optional[QImage] = None
        # 遮罩结果缓存：resize/父级重绘等触发的重复 paintEvent 对同一帧
        # 不再重跑全图变换，直接复用上次结果（实测重复 update 单次重绘
        # 9.8ms → 1.1ms，2026-11-07）
        self._cached_mask: Optional[QImage] = None
        self._cached_frame: Optional[QImage] = None
        self._cached_opacity: Optional[float] = None

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
        self._boost = qcolor_from_token(boost) if boost else None
        pos = str(cfg.get("position") or "center").lower()
        self._position = pos if pos in ("top", "center", "bottom") else "center"
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def stop_video(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())  # Qt6 清空用空 QUrl（对应 Qt5 setMedia(QMediaContent())）
        self._surface.frame = None
        self._cached_mask = None
        self._cached_frame = None
        self._cached_opacity = None
        self._boost = None
        self._opacity = 0.28
        self._position = "center"
        self.update()

    # ── 播放控制 ─────────────────────────────────────

    def _on_frame_ready(self):
        self.update()

    def _on_status(self, status):
        # 素材为正放+倒放无缝剪辑，回头重播无跳变（EndOfMedia 后 play() 从头重播）
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
                mask = self._build_mask(frame, self._opacity)
                p.save()
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                # 遮罩已降采样，放大绘制必须平滑插值，否则低频光影出块状
                p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                p.drawImage(self._cover_rect(mask.size(), self.rect(), self._position), mask)
                p.restore()
        finally:
            p.end()

    def _build_mask(self, frame: QImage, opacity: float) -> QImage:
        """把帧明暗转成黑色 alpha 遮罩：a = opacity × (255 - v)

        SourceOver 下 out = dst×(1-a/255) + 0×(a/255)
        = dst×[1 - opacity×(255-v)/255]
        = dst×[(1-opacity) + opacity×v/255]，即把帧与白色预混合后做 multiply
        的结果。等价于原 HanaAgent 公式，但不依赖目标像素。

        同一帧 + 同一 opacity 重复调用直接返回缓存结果。
        """
        if (self._cached_mask is not None and frame is self._cached_frame
                and opacity == self._cached_opacity):
            return self._cached_mask
        mask = self._mask_buf
        if mask is None or mask.size() != frame.size() or mask.format() != QImage.Format_ARGB32_Premultiplied:
            mask = QImage(frame.size(), QImage.Format_ARGB32_Premultiplied)
            self._mask_buf = mask
        mask.fill(QColor(0, 0, 0, 0))
        pt = QPainter(mask)
        try:
            pt.setCompositionMode(QPainter.CompositionMode_Source)
            pt.setOpacity(opacity)          # 整体压暗强度
            pt.drawImage(0, 0, self._alpha_of(frame))
        finally:
            pt.end()
        self._cached_mask = mask
        self._cached_frame = frame
        self._cached_opacity = opacity
        return mask

    def _alpha_of(self, frame: QImage) -> QImage:
        """黑底 + 「反相亮度」alpha 的黑图（白→完全透明，黑→不透明）

        先 setAlphaChannel 再整体 setOpacity 绘制，两层都在带 alpha 的图上做；
        直接把 Grayscale8 当 DestinationIn 的 src 不行——Qt 把它按不透明色
        处理，alpha 恒 255（实测）。
        """
        gray = frame.convertToFormat(QImage.Format_Grayscale8)
        gray.invertPixels()
        canvas = QImage(frame.size(), QImage.Format_ARGB32)
        canvas.fill(QColor(0, 0, 0, 255))
        canvas.setAlphaChannel(gray)
        return canvas

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
