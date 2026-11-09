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
- 整体透明度由 apply_config 传导给各子层（预乘 alpha / QSS alpha），
  不再挂 QGraphicsOpacityEffect（离屏合成反模式，2026-11-09 摘除）。
- 内部 _image_label 单独挂 QGraphicsBlurEffect 控制 blur。
  Qt 限制一个 widget 只能挂一个 effect，所以拆开挂载。
- _dim_label 是纯 background-color，不挂 effect（叠在 _image_label 之上）。
"""

from typing import Optional

from PyQt5.QtCore import QEvent, QRect, QRectF, Qt, QTimer
from PyQt5.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QLabel,
    QWidget,
)

from app.widgets.anim_background import AnimationRegistry, BackgroundClock

# 适配 _chat_frame 的 8px 圆角；SceneLayer 自身和内部图片标签都按此设置
_CHAT_FRAME_RADIUS = 8


# ══════════════════════════════════════════════════════════════════
# [PERF] 2026-11-09 摘除常驻 QGraphicsEffect（PAINT-PROF 实测驱动）
#
# 背景：动画主题（window.animation，12fps 全窗）每帧 update 整窗，
# raster backing store 上层约 280 个可见控件全部连带重绘（实测峰值
# 3353 paint/秒，阈值 120）。本区把所有常驻 effect 换成等价的
# 「一次烘焙 / painter 属性」：
#   - QGraphicsOpacityEffect → painter.setOpacity（动画层）/ 预乘 alpha（静态图）
#   - QGraphicsBlurEffect    → 加载时 QGraphicsScene 离屏烘焙一次
# effect 常驻会让子树每次重绘走离屏合成路径，是双倍 blit；摘除后
# 视觉输出逐像素同源（同一 blur 算法、同一 alpha 数学）。
# ══════════════════════════════════════════════════════════════════

def _premultiply_opacity(image: QImage, opacity: float) -> QImage:
    """把透明度烘进图片 alpha（DestinationIn 乘系数）。

    QGraphicsOpacityEffect(QLabel) 的零成本替代：静态图加载时算一次，
    之后每次重绘不再经过 effect 的离屏合成路径。
    """
    if opacity >= 0.999 or image.isNull():
        return image
    img = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(img)
    painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    painter.fillRect(img.rect(), QColor(0, 0, 0, int(round(opacity * 255))))
    painter.end()
    return img


def _parse_css_color(color_str: str) -> Optional[QColor]:
    """解析 CSS 颜色串。

    QColor 构造只认 #RRGGBB/#AARRGGBB/色名，不认 rgba()/rgb()——主题 yaml
    里大量使用 rgba() 串（如 sledding-game 的 scene.color），这里手工补齐。
    alpha 兼容两种写法：0-255 整数（Qt/QSS 风格）与 0~1 小数（CSS3 风格）。
    """
    import re as _re

    s = color_str.strip()
    c = QColor(s)
    if c.isValid():
        return c
    m = _re.match(
        r"^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s]+([\d.%]+))?\s*\)$",
        s,
        _re.I,
    )
    if not m:
        return None
    r, g, b = (int(float(m.group(i))) for i in (1, 2, 3))
    a_txt = m.group(4)
    if a_txt is None:
        a = 255
    else:
        a_val = float(a_txt[:-1]) if a_txt.endswith("%") else float(a_txt)
        # 0~1 带小数点按 CSS3 比例；否则按 0-255（Qt rgba 风格）
        if 0 < a_val <= 1 and ("." in a_txt):
            a = int(round(a_val * 255))
        else:
            a = int(a_val)
    c = QColor(r, g, b, min(255, max(0, a)))
    return c if c.isValid() else None


def _scale_qss_color_alpha(color_str: str, factor: float) -> str:
    """QSS 颜色串的 alpha 乘以 factor（整体透明度传导给 color/dim 底）。

    解析失败（非法颜色）原样返回（QSS 侧自行报错，与旧行为一致）；
    factor>=1 原样返回。输出统一 #AARRGGBB。
    """
    if factor >= 0.999:
        return color_str
    c = _parse_css_color(color_str)
    if c is None:
        return color_str
    c.setAlpha(int(round(c.alpha() * factor)))
    return c.name(QColor.HexArgb)


def _bake_blur(pixmap: QPixmap, radius: float) -> QPixmap:
    """离屏烘焙一次 blur（QGraphicsScene 渲染路径，与 widget 内 effect
    同一算法），摘掉常驻 QGraphicsBlurEffect。

    输入 pixmap 需已含 blur 外扩边（调用方负责把源图拉伸到「画布+2×pad」
    尺寸再传入），输出与输入同尺寸：边缘淡化带落在可视区外，与旧行为一致。
    """
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(pixmap)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    out = QImage(
        pixmap.width(), pixmap.height(), QImage.Format_ARGB32_Premultiplied
    )
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    scene.render(
        painter,
        QRectF(0, 0, out.width(), out.height()),
        QRectF(0, 0, pixmap.width(), pixmap.height()),
    )
    painter.end()
    return QPixmap.fromImage(out)


class _AnimRenderMixin:
    """动画帧渲染核心（纯逻辑，不绑定 QWidget 基类）。

    raster 版（_AnimView）与 GL 版（_AnimViewGL）共享同一套几何/缓存/
    绘制逻辑，保证两条渲染路径输出逐像素一致。
    """

    def _init_render_state(self) -> None:
        self._frame: Optional[QImage] = None
        self._fit = "cover"
        self._position = "center"
        self._opacity = 1.0
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

    def set_opacity(self, opacity: float) -> None:
        """动画层透明度（QGraphicsOpacityEffect 的 painter 等价物）。

        在 paint 内 setOpacity，避免 effect 常驻把子树拽进离屏合成路径。
        """
        try:
            self._opacity = min(1.0, max(0.0, float(opacity)))
        except (TypeError, ValueError):
            self._opacity = 1.0
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

    def _render_frame(self, painter: QPainter) -> bool:
        """统一渲染体：raster paintEvent 与 GL paintGL 共用。

        返回是否真正绘制了内容（供调用方判定）。
        """
        img = self._frame
        if img is None or img.isNull():
            return False
        target = self.rect()
        if target.isEmpty():
            return False
        scaled = self._scaled_frame(target)
        if scaled is None:
            return False
        if self._opacity < 1.0:
            painter.setOpacity(self._opacity)
        if self._fit == "tile":
            painter.drawTiledPixmap(target, QPixmap.fromImage(scaled))
            return True
        if self._fit == "contain":
            # 缩放结果小于目标：居中摆放
            x = target.x() + (target.width() - scaled.width()) // 2
            y = target.y() + (target.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
            return True
        # cover：缓存图已是目标尺寸，1:1 blit
        painter.drawImage(target.x(), target.y(), scaled)
        return True

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


class _AnimView(_AnimRenderMixin, QWidget):
    """动画帧绘制层（raster 版）

    直接画 QImage（cover 时只取源帧可见的那段），比 QLabel 少一次
    「整帧缩放到 label 尺寸再裁切」的浪费。

    [PERF] 2026-11-09：目标矩形与源可见矩形在窗口 resize 之间是**恒定的**，
    但旧实现每帧都让 QPainter 现场做一次双线性缩放（实测 1600x900 → 2800x1800
    的 cover 绘制 p50 3.64ms/帧；窗口层 15fps 合计占主线程约 5%）。改为
    「帧到达时按目标缩放一次并缓存」，paintEvent 退化为 1:1 blit（p50 0.64ms），
    单帧省约 3ms，视觉输出逐像素同源（同一 SmoothTransformation 缩放，抗锯齿
    级别不变）。缓存只在几何/适配模式变化时失效，不增加解码次数。

    透明度走 painter.setOpacity（set_opacity），不再挂 QGraphicsOpacityEffect。
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._init_render_state()

    def paintEvent(self, event) -> None:
        if self._frame is None or self._frame.isNull() or self.rect().isEmpty():
            return
        painter = QPainter(self)
        try:
            self._render_frame(painter)
        finally:
            painter.end()


# [PERF] 2026-11-09：GL 承载方案（QOpenGLWidget）已验证并撤除——Qt5 窗口
# 合成模型下，QOpenGLWidget 的 framebuffer 每帧 blit 回顶层窗口 raster
# backing store，其上层全部 raster 控件像素被覆盖后必须每帧重绘；真机
# PAINT-PROF 实测上层连带依旧（QWidget/QLabel 爬升到 1500+ 次/秒），且
# 额外引入 GPU→CPU readback。半透明 UI × 全窗动画的每帧连带是 Qt5 合成
# 模型的固有成本，widget 侧无法绕过（Qt6 scene graph 才有真隔离）。

class SceneLayer(QWidget):
    """对话区场景背景层

    支持的 cfg 字段（来自 yaml `backgrounds.scene`）：
      - image   : str  主题文件夹相对路径或 :qrc 路径
      - color   : str  纯色背景（CSS color，如 '#0f1626' / 'rgba(...)'），与 image 共存
      - opacity : float 0.0~1.0（整体透明度：传导给 color/dim 的 QSS alpha
                  与各图层预乘 alpha，不挂 QGraphicsOpacityEffect）
      - blur    : int   0~30（静态图加载时离屏烘焙一次，不挂常驻 effect）
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
        # ── 摘 effect 后的传导/烘焙状态 ──
        # 整体透明度（原 QGraphicsOpacityEffect 的传导值）
        self._layer_opacity: float = 1.0
        # 静态图 blur 烘焙：radius>0 时启用；原图保存 + 防抖定时器 + 已烘几何 key
        self._image_blur_radius: float = 0.0
        self._image_source_pixmap: Optional[QPixmap] = None
        self._image_bake_key: Optional[tuple] = None
        self._image_bake_timer: Optional[QTimer] = None

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

        # 1. 整体透明度先读（传导给 color 底 / dim 蒙版 / 各图层）。
        # 原实现把 QGraphicsOpacityEffect 挂在整个 SceneLayer 上——effect
        # 常驻会让整棵子树每次重绘走离屏合成（双倍 blit）。传导实现后
        # 视觉等价、路径更轻。
        try:
            self._layer_opacity = min(1.0, max(0.0, float(cfg.get("opacity", 1.0))))
        except (TypeError, ValueError):
            self._layer_opacity = 1.0

        # 2. 纯色底（color）—— 作为 self 的 background-color
        # SceneLayer 自身 + image_label/dim_label 都设 border-radius 8px 适配
        # _chat_frame 圆角矩形，避免图片在圆角外溢出成方块。
        color = cfg.get("color")
        if color:
            self.setAutoFillBackground(True)
            self.setStyleSheet(
                f"background-color: {_scale_qss_color_alpha(color, self._layer_opacity)};"
                f" border-radius: {self._radius}px;"
            )

        # 3. 图片层（独立 widget；blur 离屏烘焙一次，不挂常驻 effect）
        image = cfg.get("image")
        if image and image_resolver:
            resolved = image_resolver(image)
            pix = QPixmap(resolved)
            if not pix.isNull():
                self._image_label = QLabel(self)
                self._image_source_pixmap = pix
                # 整体透明度预乘进图片 alpha（QGraphicsOpacityEffect 等价物）
                pix = QPixmap.fromImage(_premultiply_opacity(pix.toImage(), self._layer_opacity))
                self._image_label.setPixmap(pix)
                self._image_label.setScaledContents(True)
                self._sync_content_geometry()
                # 图片也设圆角（避免溢出到 _chat_frame 圆角外）
                self._image_label.setStyleSheet(
                    f"border-radius: {self._radius}px;"
                )
                self._image_label.show()
                # blur 效果：离屏烘焙一次（加载时），之后 label 直接画烘焙结果
                try:
                    blur = float(cfg.get("blur", 0) or 0)
                except (TypeError, ValueError):
                    blur = 0.0
                if blur > 0:
                    self._image_blur_radius = min(blur, 30.0)
                    self._blur_pad = int(self._image_blur_radius) * 2
                    self._sync_content_geometry()
                    self._schedule_image_bake()

        # 4. 动画层（动画 WebP / GIF，Qt 内置解码，不依赖系统媒体栈）
        # Z 序：静态图之上、dim 之下（dim 是给文字提可读性的压暗层）
        anim = cfg.get("animation")
        if isinstance(anim, dict) and anim.get("enabled", True) and anim.get("src"):
            self._apply_animation(anim, image_resolver, self._layer_opacity)

        # 5. 前景图层（壁纸拆出的全画布图层，如人物）
        # 与 image 同矩形、同 setScaledContents 拉伸，才能与背景构图逐像素对齐。
        # Z 序在 dim 之上（见 dim 块末尾的 raise_）：dim 负责压暗背景提可读性，
        # 盖到前景上会让人物淡到看不清。
        fg = cfg.get("foreground")
        if isinstance(fg, dict) and fg.get("enabled", True):
            fg_image = fg.get("image")
            if fg_image and image_resolver:
                resolved = image_resolver(fg_image)
                fg_pix = QPixmap(resolved)
                if not fg_pix.isNull():
                    try:
                        fg_opacity = float(fg.get("opacity", 1.0))
                    except (TypeError, ValueError):
                        fg_opacity = 1.0
                    self._foreground_label = QLabel(self)
                    fg_pix = QPixmap.fromImage(
                        _premultiply_opacity(fg_pix.toImage(), fg_opacity * self._layer_opacity)
                    )
                    self._foreground_label.setPixmap(fg_pix)
                    self._foreground_label.setScaledContents(True)
                    self._foreground_label.setStyleSheet(
                        f"border-radius: {self._radius}px;"
                    )
                    self._sync_content_geometry()
                    self._foreground_label.show()

        # 6. dim 蒙版（半透明黑色，叠在 image_label 之上压暗）
        dim = cfg.get("dim")
        if dim:
            self._dim_label = QLabel(self)
            self._dim_label.setStyleSheet(
                f"background-color: {_scale_qss_color_alpha(dim, self._layer_opacity)};"
                f" border-radius: {self._radius}px;"
            )
            self._sync_content_geometry()
            self._dim_label.raise_()
            self._dim_label.show()

        # 前景层浮到 dim 之上：dim 是给背景提文字可读性的压暗蒙版，盖到
        # 前景（人物等）上会把它淡到看不清，用户要的是「淡但清晰」。
        if self._foreground_label is not None:
            self._foreground_label.raise_()

        self._sync_geometry()
        # 几何就位后同步烘一次 blur（后续 resize 走防抖重烘）
        self._bake_image_now()

    def _apply_animation(self, anim_cfg: dict, image_resolver, layer_opacity: float = 1.0) -> None:
        """挂载动画层

        动画素材为动画 WebP（也兼容 GIF），由 Qt 内置解码器逐帧读取。
        素材缺失/解码失败时直接返回，调用方保留静态图或底色（降级链：
        动画 → 首帧静态图 → color → 透明）。

        渲染路径：GL 可用走 QOpenGLWidget（背景每帧失效不再连带上层
        raster 控件），否则回退 raster 版；GL 运行期失效由
        on_animation_frame 检测后原地重建 raster 版。
        """
        if image_resolver is None:
            return
        resolved = image_resolver(anim_cfg.get("src") or "")
        if not resolved:
            return
        source = AnimationRegistry.instance().acquire(resolved, anim_cfg.get("fps", 12))
        if source.failed or source.current is None:
            return

        try:
            opacity = min(1.0, max(0.0, float(anim_cfg.get("opacity", 1.0)))) * layer_opacity
        except (TypeError, ValueError):
            opacity = layer_opacity
        self._install_anim_view(
            anim_cfg.get("fit", "cover"),
            anim_cfg.get("position", "center"),
            opacity,
            source.current,
        )
        self._anim_source = source
        source.subscribe(self)
        BackgroundClock.instance().track(source)
        self._sync_content_geometry()

    def _install_anim_view(
        self,
        fit: str,
        position: str,
        opacity: float,
        first_frame: Optional[QImage],
    ) -> None:
        """创建动画视图并挂到本层（raster 版，GL 方案已验证无效并撤除）"""
        view = _AnimView(self)
        view.configure(fit, position)
        if opacity < 1.0:
            view.set_opacity(opacity)  # painter.setOpacity，不再挂 effect
        if self._radius:
            view.setStyleSheet(f"border-radius: {self._radius}px;")

        self._anim_view = view
        view.set_frame(first_frame)
        self._sync_content_geometry()
        view.show()

    def on_animation_frame(self, image) -> None:
        """帧源回调（主线程，由全局时钟驱动）"""
        view = self._anim_view
        if view is None or image is None:
            return
        view.set_frame(image)

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
        # blur 烘焙状态复位（timer 是 QTimer(self)，随本层析构，这里只停）
        self._image_blur_radius = 0.0
        self._image_source_pixmap = None
        self._image_bake_key = None
        if self._image_bake_timer is not None:
            self._image_bake_timer.stop()
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
        # 几何变化后 blur 烘焙结果尺寸不再匹配 → 防抖重烘
        self._schedule_image_bake()

    # ── blur 离屏烘焙（摘常驻 QGraphicsBlurEffect）──

    def _schedule_image_bake(self) -> None:
        """几何变化后的 blur 重烘（150ms 防抖；同几何已烘过则跳过）"""
        if self._image_label is None or self._image_blur_radius <= 0:
            return
        if self._image_source_pixmap is None:
            return
        geo = self._image_label.geometry()
        key = (geo.width(), geo.height(), self._image_blur_radius)
        if key == self._image_bake_key:
            return
        # 应急显示：防抖窗口期把当前 pixmap 拉伸到新尺寸，避免露底/错尺寸；
        # 烘焙完成后切回 1:1（setScaledContents(False)）
        self._image_label.setScaledContents(True)
        if self._image_bake_timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._bake_image_now)
            self._image_bake_timer = timer
        self._image_bake_timer.start(150)

    def _bake_image_now(self) -> None:
        """按当前几何离屏烘焙 blur（含整体透明度预乘），烘焙后 1:1 显示。

        几何语义与旧 effect 路径一致：原图先拉伸到「画布+2×pad」大矩形
        （blur 外扩边），烘焙后边缘淡化带落在可视区外。
        """
        if self._image_label is None or self._image_blur_radius <= 0:
            return
        if self._image_source_pixmap is None:
            return
        geo = self._image_label.geometry()
        if geo.width() <= 0 or geo.height() <= 0:
            return
        key = (geo.width(), geo.height(), self._image_blur_radius)
        if key == self._image_bake_key:
            return
        scaled = self._image_source_pixmap.scaled(
            geo.width(), geo.height(),
            Qt.IgnoreAspectRatio, Qt.SmoothTransformation,
        )
        baked = _bake_blur(scaled, self._image_blur_radius)
        baked = QPixmap.fromImage(
            _premultiply_opacity(baked.toImage(), self._layer_opacity)
        )
        self._image_label.setPixmap(baked)
        self._image_label.setScaledContents(False)
        self._image_bake_key = key

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