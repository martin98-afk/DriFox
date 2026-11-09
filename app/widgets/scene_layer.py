# -*- coding: utf-8 -*-
"""场景背景层（PR3 — 多区域主题插件基础设施）

挂载点：作为 TabManagerWindow._chat_frame 的子 widget（绝对定位，不参与布局）。
       撑满 _chat_frame.rect()——含 replace_tab_bar / 对话区 / 输入框 /
       LEFT/RIGHT/BOTTOM 停靠区 / UI 插件槽位等所有 _chat_frame 内的内容。
       是 aurora 等主题的「右侧整个区域」场景背景图。
渲染：QLabel + QPixmap，支持 image/opacity/blur/dim。
同步：监听 parent.resize 自动同步 geometry。

设计取舍：
- 作为 TabManagerWindow._chat_frame 子 widget（2026-08 改造）：
  scene 图片覆盖整个右侧圆角矩形（含 UI 插件槽位），而不仅是 OpenAIChatToolWindow
  对话区滚动区内层。
- SceneLayer 在 _chat_frame 最底层（.lower()），所有 _chat_frame 内的 UI 控件
  （replace_tab_bar / dock splitter / 对话区 / 输入框 / UI 插件槽位）都在它之上。
- SceneLayer 自身设 border-radius 8px 适配 _chat_frame 的圆角矩形，
  避免图片在 _chat_frame 圆角外溢出成方块。
- 整体（self）挂 QGraphicsOpacityEffect 控制 opacity。
- 内部 _image_label 单独挂 QGraphicsBlurEffect 控制 blur。
  Qt 限制一个 widget 只能挂一个 effect，所以拆开挂载。
- _dim_label 是纯 background-color，不挂 effect（叠在 _image_label 之上）。
"""

from typing import Optional

from PyQt5.QtCore import QEvent, QRect, Qt
from PyQt5.QtGui import QImage, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsOpacityEffect,
    QLabel,
    QWidget,
)

from app.widgets.anim_background import AnimationRegistry, BackgroundClock

# 适配 _chat_frame 的 8px 圆角；SceneLayer 自身和内部图片标签都按此设置
_CHAT_FRAME_RADIUS = 8

class _AnimView(QWidget):
    """动画帧绘制层

    直接画 QImage（cover 时只取源帧可见的那段），比 QLabel 少一次
    「整帧缩放到 label 尺寸再裁切」的浪费。

    [PERF] 2026-11-09：目标矩形与源可见矩形在窗口 resize 之间是**恒定的**，
    但旧实现每帧都让 QPainter 现场做一次双线性缩放（实测 1600x900 → 2800x1800
    的 cover 绘制 p50 3.64ms/帧；窗口层 15fps 合计占主线程约 5%）。改为
    「帧到达时按目标缩放一次并缓存」，paintEvent 退化为 1:1 blit（p50 0.64ms），
    单帧省约 3ms，视觉输出逐像素同源（同一 SmoothTransformation 缩放，抗锯齿
    级别不变）。缓存只在几何/适配模式变化时失效，不增加解码次数。
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._frame: Optional[QImage] = None
        self._fit = "cover"
        self._position = "center"
        # 预缩放缓存：(_scaled 与 _frame 一一对应，几何变化时置空重算)
        self._scaled: Optional[QImage] = None
        self._scaled_key: Optional[tuple] = None

    def configure(self, fit: str, position: str) -> None:
        self._fit = fit or "cover"
        self._position = position or "center"
        self._scaled = None
        self._scaled_key = None
        self.update()

    def set_frame(self, image: Optional[QImage]) -> None:
        self._frame = image
        self.update()

    # ── 预缩放缓存 ────────────────────────────────────────────────
    def _scaled_frame(self, target: QRect) -> Optional[QImage]:
        """按当前几何把源帧缩放成目标尺寸（结果缓存，帧率不受影响）。

        cover/contain 的几何在两次 resize 之间恒定，因此每个 key 只算一次；
        同一帧重复 paint（例如被上方控件遮挡后重绘）不再重复缩放。
        极端帧率下若每帧都换图，仍是每帧一次缩放（与旧实现等价，不更差）。
        """
        img = self._frame
        if img is None or img.isNull() or target.isEmpty():
            return None
        if self._fit == "tile":
            return img
        iw, ih = img.width(), img.height()
        if iw <= 0 or ih <= 0:
            return None
        key = (id(img), iw, ih, target.width(), target.height(), self._fit, self._position)
        if self._scaled is not None and self._scaled_key == key:
            return self._scaled
        if self._fit == "contain":
            scale = min(target.width() / iw, target.height() / ih)
            w = max(1, int(iw * scale))
            h = max(1, int(ih * scale))
            scaled = img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        else:
            # cover：先按可见段裁切，再缩放到目标尺寸（先裁后缩，省掉不可见
            # 区域的重采样；与 drawImage(target, img, src) 的最终像素一致）
            src = self.source_rect((iw, ih), (target.width(), target.height()))
            scaled = img.copy(src).scaled(
                target.width(), target.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation
            )
        self._scaled = scaled
        self._scaled_key = key
        return scaled

    def paintEvent(self, event) -> None:
        img = self._frame
        if img is None or img.isNull():
            return
        target = self.rect()
        if target.isEmpty():
            return
        scaled = self._scaled_frame(target)
        if scaled is None:
            return
        painter = QPainter(self)
        if self._fit == "tile":
            painter.drawTiledPixmap(target, QPixmap.fromImage(scaled))
            return
        if self._fit == "contain":
            # 缩放结果小于目标：居中摆放
            x = target.x() + (target.width() - scaled.width()) // 2
            y = target.y() + (target.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
            return
        # cover：缓存图已是目标尺寸，1:1 blit
        painter.drawImage(target.x(), target.y(), scaled)

    def source_rect(self, image_size, target_size) -> QRect:
        """cover 模式下从源帧取的矩形（只取可见那段，不做整帧缩放）"""
        iw, ih = int(image_size[0]), int(image_size[1])
        tw, th = int(target_size[0]), int(target_size[1])
        if iw <= 0 or ih <= 0 or tw <= 0 or th <= 0:
            return QRect(0, 0, max(1, iw), max(1, ih))
        scale = max(tw / iw, th / ih)
        sw, sh = tw / scale, th / scale
        sx = (iw - sw) / 2
        if self._position == "top":
            sy = 0.0
        elif self._position == "bottom":
            sy = ih - sh
        else:
            sy = (ih - sh) / 2
        return QRect(int(sx), int(sy), max(1, int(sw)), max(1, int(sh)))




class SceneLayer(QWidget):
    """对话区场景背景层

    支持的 cfg 字段（来自 yaml `backgrounds.scene`）：
      - image   : str  主题文件夹相对路径或 :qrc 路径
      - color   : str  纯色背景（CSS color，如 '#0f1626' / 'rgba(...)'），与 image 共存
      - opacity : float 0.0~1.0（<1.0 时挂 QGraphicsOpacityEffect）
      - blur    : int   0~30（>0 时挂 QGraphicsBlurEffect 到 _image_label）
      - dim     : str  半透明蒙版（CSS color），叠加在图片上压暗提升文字可读性
      - foreground: dict 前景图层（全画布图层，如壁纸拆分出的人物）
          结构与 image/opacity 相同：{image, opacity, enabled}
          与 image 共用同一矩形与同一缩放方式（拉伸铺满），保证拆分图层
          与背景逐像素对齐；Z 序在 dim 之上，不被压暗（dim 是给背景文字
          提可读性的，压在前景上会把人物淡到看不清）。
      - enabled : bool  默认 True
    """

    def __init__(self, parent: Optional[QWidget] = None, radius: int = _CHAT_FRAME_RADIUS):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        # 圆角：对话区/侧栏 8px，窗口层 0（整窗无圆角）
        self._radius = int(radius)

        self._image_label: Optional[QLabel] = None
        self._foreground_label: Optional[QLabel] = None
        self._dim_label: Optional[QLabel] = None
        self._anim_view: Optional[_AnimView] = None
        self._anim_source = None
        # 跨宿主共享画布：宿主注入求值函数，让本层只显示同一张图的某一段。
        # 多栏共享背景时各栏用同一画布尺寸 + 各自左缘偏移，保证接缝处图案
        # 严格连续。None = 退化为撑满自身 rect（原有单宿主行为）。
        self._canvas_provider = None
        # 模糊外扩边：blur>0 时内容层四周留出的额外像素（见 _content_geometry）
        self._blur_pad = 0

    def set_canvas_provider(self, provider) -> None:
        """注入跨宿主画布求值函数（幂等，可重复调用）

        Args:
            provider: callable() -> Optional[Tuple[int, int, int] | Tuple[int, int, int, int]]
                (画布宽, 画布高, 本宿主左缘在画布中的 x 偏移[, 统一模糊外扩])。
                返回 None 表示退回「撑满自身 rect」。
                ★ 共享画布的多个宿主必须拿到**同一组**宽高与���扩值，否则
                scaledContents 缩放比例不一致，接缝处图案会错位。
        """
        self._canvas_provider = provider
        self._sync_geometry()

    def refresh_canvas_geometry(self) -> None:
        """重新求值画布几何（宿主宽度变化/开合动画帧后调用）

        常态下宿主 resize 已由 eventFilter 驱动同步；本入口用于**另一宿主**
        的几何变化（共享画布宽度取决于两栏之和）与动画逐帧落位后的补齐。
        """
        self._sync_content_geometry()

    def sync_geometry(self) -> None:
        """公开几何同步入口：自身撑满 parent + 内容层对齐画布

        与 VideoOverlayLayer.sync_geometry 同名同语义。用于宿主 resize
        节流阶段二（布局传播被跳过）时由 _resize_bg_labels 手动收拢。
        """
        self._sync_geometry()

    def apply_config(self, cfg: Optional[dict], image_resolver=None) -> None:
        """应用主题配置（None 或 enabled=False 时清除所有内容）

        Args:
            cfg: get_theme_backgrounds()["scene"] 返回的 dict 或 None
            image_resolver: callable(str) -> str，解析图片路径（注入避免循环依赖）
        """
        self._clear()

        if not cfg or not cfg.get("enabled", True):
            return

        # 1. 纯色底（color）—— 作为 self 的 background-color
        # SceneLayer 自身 + image_label/dim_label 都设 border-radius 8px 适配
        # _chat_frame 圆角矩形，避免图片在圆角外溢出成方块。
        color = cfg.get("color")
        if color:
            self.setAutoFillBackground(True)
            self.setStyleSheet(
                f"background-color: {color}; border-radius: {self._radius}px;"
            )

        # 2. 图片层（独立 widget，可挂 blur effect）
        image = cfg.get("image")
        if image and image_resolver:
            resolved = image_resolver(image)
            from PyQt5.QtGui import QPixmap

            pix = QPixmap(resolved)
            if not pix.isNull():
                self._image_label = QLabel(self)
                self._image_label.setPixmap(pix)
                self._image_label.setScaledContents(True)
                self._sync_content_geometry()
                # 图片也设圆角（避免溢出到 _chat_frame 圆角外）
                self._image_label.setStyleSheet(
                    f"border-radius: {self._radius}px;"
                )
                self._image_label.show()
                # blur 效果挂到 _image_label
                blur = cfg.get("blur", 0)
                if blur and blur > 0:
                    blur_effect = QGraphicsBlurEffect(self._image_label)
                    blur_effect.setBlurRadius(min(float(blur), 30.0))
                    self._image_label.setGraphicsEffect(blur_effect)
                    self._blur_pad = int(min(float(blur), 30.0)) * 2
                    self._sync_content_geometry()

        # 2.2 动画层（动画 WebP / GIF，Qt 内置解码，不依赖系统媒体栈）
        # Z 序：静态图之上、dim 之下（dim 是给文字提可读性的压暗层）
        anim = cfg.get("animation")
        if isinstance(anim, dict) and anim.get("enabled", True) and anim.get("src"):
            self._apply_animation(anim, image_resolver)

        # 2.5 前景图层（壁纸拆出的全画布图层，如人物）
        # 与 image 同矩形、同 setScaledContents 拉伸，才能与背景构图逐像素对齐。
        # Z 序在 dim 之上（见 dim 块末尾的 raise_）：dim 负责压暗背景提可读性，
        # 盖到前景上会让人物淡到看不清。
        fg = cfg.get("foreground")
        if isinstance(fg, dict) and fg.get("enabled", True):
            fg_image = fg.get("image")
            if fg_image and image_resolver:
                resolved = image_resolver(fg_image)
                from PyQt5.QtGui import QPixmap

                fg_pix = QPixmap(resolved)
                if not fg_pix.isNull():
                    self._foreground_label = QLabel(self)
                    self._foreground_label.setPixmap(fg_pix)
                    self._foreground_label.setScaledContents(True)
                    self._foreground_label.setStyleSheet(
                        f"border-radius: {self._radius}px;"
                    )
                    fg_opacity = fg.get("opacity", 1.0)
                    if fg_opacity < 1.0:
                        fg_effect = QGraphicsOpacityEffect(self._foreground_label)
                        fg_effect.setOpacity(fg_opacity)
                        self._foreground_label.setGraphicsEffect(fg_effect)
                    self._sync_content_geometry()
                    self._foreground_label.show()

        # 3. dim 蒙版（半透明黑色，叠在 image_label 之上压暗）
        dim = cfg.get("dim")
        if dim:
            self._dim_label = QLabel(self)
            self._dim_label.setStyleSheet(
                f"background-color: {dim}; border-radius: {self._radius}px;"
            )
            self._sync_content_geometry()
            self._dim_label.raise_()
            self._dim_label.show()

        # 前景层浮到 dim 之上：dim 是给背景提文字可读性的压暗蒙版，盖到
        # 前景（人物等）上会把它淡到看不清，用户要的是「淡但清晰」。
        if self._foreground_label is not None:
            self._foreground_label.raise_()

        # 4. 整体透明度（不影响 blur effect 的内部计算）
        opacity = cfg.get("opacity", 1.0)
        if opacity < 1.0:
            opacity_effect = QGraphicsOpacityEffect(self)
            opacity_effect.setOpacity(opacity)
            self.setGraphicsEffect(opacity_effect)

        self._sync_geometry()

    def _apply_animation(self, anim_cfg: dict, image_resolver) -> None:
        """挂载动画层

        动画素材为动画 WebP（也兼容 GIF），由 Qt 内置解码器逐帧读取。
        素材缺失/解码失败时直接返回，调用方保留静态图或底色（降级链：
        动画 → 首帧静态图 → color → 透明）。
        """
        if image_resolver is None:
            return
        resolved = image_resolver(anim_cfg.get("src") or "")
        if not resolved:
            return
        source = AnimationRegistry.instance().acquire(resolved, anim_cfg.get("fps", 12))
        if source.failed or source.current is None:
            return

        view = _AnimView(self)
        view.configure(anim_cfg.get("fit", "cover"), anim_cfg.get("position", "center"))
        opacity = anim_cfg.get("opacity", 1.0)
        if opacity < 1.0:
            effect = QGraphicsOpacityEffect(view)
            effect.setOpacity(float(opacity))
            view.setGraphicsEffect(effect)
        if self._radius:
            view.setStyleSheet(f"border-radius: {self._radius}px;")

        self._anim_view = view
        self._anim_source = source
        source.subscribe(self)
        BackgroundClock.instance().track(source)
        view.set_frame(source.current)
        self._sync_content_geometry()
        view.show()

    def on_animation_frame(self, image) -> None:
        """帧源回调（主线程，由全局时钟驱动）"""
        if self._anim_view is not None and image is not None:
            self._anim_view.set_frame(image)

    def clear(self) -> None:
        """清除所有内容层（公开入口，供宿主主题切换时释放动画订阅）"""
        self._clear()

    def _clear(self) -> None:
        """清除旧 widgets 和 effects"""
        if self._anim_view is not None:
            if self._anim_source is not None:
                self._anim_source.unsubscribe(self)
                self._anim_source = None
            self._anim_view.hide()
            self._anim_view.deleteLater()
            self._anim_view = None
        if self._image_label is not None:
            self._image_label.hide()
            self._image_label.deleteLater()
            self._image_label = None
        if self._foreground_label is not None:
            self._foreground_label.hide()
            self._foreground_label.deleteLater()
            self._foreground_label = None
        if self._dim_label is not None:
            self._dim_label.hide()
            self._dim_label.deleteLater()
            self._dim_label = None
        self._blur_pad = 0
        self.setGraphicsEffect(None)
        self.setStyleSheet("")
        self.setAutoFillBackground(False)

    def _content_geometry(self) -> tuple:
        """内容层（图片/蒙版）几何

        跨宿主画布下返回「负偏移大矩形」——宽高是整块画布，x 取负的本宿主
        偏移，超出自身边界的部分由 Qt 父子裁剪自动切掉，于是本层只显示画布
        里属于自己的那一段。无画布时等于自身 rect。
        """
        canvas = None
        if self._canvas_provider is not None:
            try:
                canvas = self._canvas_provider()
            except Exception:
                canvas = None
        if canvas is None:
            r = self.rect()
            return (0, 0, r.width(), r.height())
        # 模糊外扩边：QGraphicsBlurEffect 采样越界像素按透明处理，label 边缘会
        # 向内淡化。共享画布下两栏各有一个 label 边缘落在接缝两侧 → 接缝出现
        # 一条淡化亮带。四周留 pad（超出宿主可视区，被父控件裁掉）后淡化带整段
        # 落在可视区外，两栏图案严格连续。
        if len(canvas) >= 4:
            width, height, offset_x, pad = canvas[0], canvas[1], canvas[2], canvas[3]
        else:
            width, height, offset_x = canvas[0], canvas[1], canvas[2]
            pad = self._blur_pad
        pad = int(pad)
        return (
            -(int(offset_x) + pad),
            -pad,
            int(width) + pad * 2,
            int(height) + pad * 2,
        )

    def _sync_content_geometry(self) -> None:
        """把内容层几何对齐到画布（self 自身始终撑满 parent）"""
        x, y, w, h = self._content_geometry()
        for label in (self._image_label, self._foreground_label, self._dim_label):
            if label is not None:
                label.setGeometry(x, y, w, h)
        if self._anim_view is not None:
            self._anim_view.setGeometry(x, y, w, h)

    def _sync_geometry(self) -> None:
        """同步到 parent 的几何"""
        parent = self.parent()
        if parent is None:
            return
        self.setGeometry(parent.rect())
        self._sync_content_geometry()

    def eventFilter(self, watched, event) -> bool:
        """监听 parent.resize → 同步 SceneLayer geometry"""
        if event.type() == QEvent.Resize and watched is self.parent():
            self._sync_geometry()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_content_geometry()

    def refresh_theme(self) -> None:
        """theme_manager.refresh_target 钩子：触发重新应用配置

        由调用方通过 host._apply_chat_backgrounds() 触发实际重新加载
        （SceneLayer 自身不直接访问 theme_manager，避免循环依赖）。
        """
        pass