# -*- coding: utf-8 -*-
"""卡片内嵌成果栏（InlineArtifactPanel）

展示工具/插件推送的成果条目（生成图片、视频、HTML、文本、任意文件），挂在
assistant 气泡布局末尾（任务看板之后、页脚之前）。与 ``InlineTodoPanel`` 同构：

1. **默认折叠**：卡片空间宝贵，常态只占「分隔条 + 统计 + 图片缩略图带」。
2. **展开全量**：列表不限高、不内滚（QScrollArea sizeHint 缓存会让宿主高度链
   拿到滞后值，见 InlineTodoPanel 同款注释）；超过 ``MAX_VISIBLE_ITEMS`` 时
   只渲染前 N 条，溢出提示可点击展开全部。
3. **无成果整区隐藏**：零高度，不占布局。

与任务看板的关键语义差异——**不迁卡**：任务列表是"当前状态"，每轮工具都会
迁到最新卡；成果属于产生它的那一轮，新卡不继承旧卡的成果。宿主侧
宿主 ``_sync_artifact_panel`` 只投递到产生它的那一轮卡片，不做旧卡卸载。

五类条目分控件渲染（spec: 2026-10-24-message-artifacts-panel-design）：

=====  ========  ====================================
kind   预览      交互
=====  ========  ====================================
image  真缩略图  单击内置预览；左键拖拽导出；右键菜单
video  占位封面  双击系统播放器；左键拖拽导出；右键菜单
html   徽标+名   双击浏览器；左键拖拽导出；右键菜单
text   徽标+名   双击系统程序；左键拖拽导出；右键菜单
file   徽标+名   双击系统程序；左键拖拽导出；右键菜单
=====  ========  ====================================

缩略图懒解码：折叠态画廊与展开列表分两队列分帧解码（每拍一张），
4K PNG 全量解码约百毫秒级，一帧一张不会卡死 UI 线程。折叠画廊所有
缩略图统一 64×64 方格（方向 C，用户拍板），块数按宽度计算，溢出收进
「+N」；cover 裁切由自绘圆角完成，点击缩略图/展开列表仍可看完整图。

数据入口按 ``path`` 合并（同路径原位更新、新路径追加），同轮多次推送不会
互相覆盖；历史会话加载时由 ``render_batch_to_assistant_card`` 从 tool 消息的
``artifacts`` 字段重建。
"""

import os
from typing import Any, Dict, List, Optional
from app.widgets._bounded_caches import _BoundedLRUCache

from PyQt5.QtCore import QAbstractAnimation, QEvent, QEasingCurve, QMargins, QPropertyAnimation, QSize, Qt, QTimer, pyqtProperty, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QImageReader, QMouseEvent, QPainter, QPainterPath, QPixmap
from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget
from qfluentwidgets import TransparentToolButton

from app.utils.design_tokens import Animations, BorderRadius, Colors, font_size_css, scale_font_size
from app.utils.color_parse import parse_color
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.elided_label import _ElidedLabel

# 单卡最多渲染条数：插件一次性推几百个文件时把卡片撑爆（上限外折叠进「还有 N 个」）
MAX_VISIBLE_ITEMS = 20
# 折叠态画廊（方向 C，用户拍板 2026-11-20）：所有缩略图统一方格、cover 裁切，
# 不再按宽高比自适应块宽。块数 = floor((W+6)/70)，溢出收进「+N」。
_COLLAPSED_THUMB = 64
_COLLAPSED_GAP = 6
# 折叠态可见块上限（病态超宽兜底；slots 先算再用它截断）
_COLLAPSED_MAX_THUMBS = 12
# 摘要行（「另有 N 个文件」）高度
_COLLAPSED_SUMMARY_H = 18
# 展开列表：行高 / 缩略图与徽标 / 行间距
_LIST_ROW_H = 52
_THUMB_W, _THUMB_H = 40, 40
_LIST_GAP = 2
# 图片缩略图不叠类型角标（18×14 装不下 "IMG"，review 视觉复核裁定删除）；
# 类型信息由摘要行与非图片徽标表达。
_HEADER_H = 28
# 展开列表限高放行值（Qt 伪无限，同 markdown 折叠卡惯例）
_QWIDGETSIZE_MAX = 16777215

# kind → (徽标短标签, 语义色 token 名)；颜色在渲染时从 Colors 动态取，
# 主题切换后 refresh_style 重新解析（禁止模块导入期固化 hex）。
_KIND_META = {
    "image": ("IMG", "INFO"),
    "video": ("VID", "TAG_PURPLE"),
    "html": ("HTML", "WARNING"),
    "text": ("TXT", "SUCCESS"),
    "file": ("FILE", "TEXT_MUTED"),
}
# 人类可读类型名（展开列表 meta 行 / tooltip）
_KIND_LABEL = {
    "image": "图像",
    "video": "视频",
    "html": "HTML",
    "text": "文本",
    "file": "文件",
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
_HTML_EXTS = {".html", ".htm"}
_TEXT_EXTS = {".md", ".txt", ".json", ".csv", ".log", ".py", ".js", ".ts", ".yaml", ".yml"}


def _normalize_item(raw: Any) -> Optional[Dict[str, Any]]:
    """条目归一化：只认契约字段，未知字段忽略；非法条目返回 None

    容忍脏数据是硬要求——插件先升级/主程序后升级、插件手拼 dict 都可能喂进
    各种形状，面板绝不能因一条坏数据崩掉整卡。
    """
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    if not path:
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in _KIND_META:
        kind = _kind_from_ext(path)
    return {
        "path": path,
        "kind": kind,
        "name": str(raw.get("name") or os.path.basename(path)),
        "width": raw.get("width"),
        "height": raw.get("height"),
        "size": raw.get("size"),
        "caption": str(raw.get("caption") or "").strip(),
        "meta": raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
    }


def _kind_from_ext(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _HTML_EXTS:
        return "html"
    if ext in _TEXT_EXTS:
        return "text"
    return "file"


def _thumb_key(path: str, w: int, h: int, dpr: float = 1.0) -> str:
    """缩略图缓存 key：路径 + 逻辑尺寸 + DPR（跨屏拖动不会拿错密度图）"""
    return f"{path}|{w}x{h}@{dpr:.2f}"


def _human_size(size: Any) -> str:
    try:
        n = int(size)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return ""


def _kind_color(kind: str) -> str:
    """kind 的语义色（运行时取 Colors，主题刷新后自动跟随）"""
    Colors.refresh()
    return getattr(Colors, _KIND_META.get(kind, _KIND_META["file"])[1])


def _with_alpha(color: str, alpha: int) -> str:
    """统一用 rgba() 表达底/边透明度，避免 hex8 在旧 QSS 解析差异"""
    c = QColor(color)
    if not c.isValid():
        return color
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def _relative_luminance(color: QColor) -> float:
    def channel(value: int) -> float:
        s = value / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.red())
        + 0.7152 * channel(color.green())
        + 0.0722 * channel(color.blue())
    )


def _contrast_ratio(fg: QColor, bg: QColor) -> float:
    """WCAG 对比度（1~21），用于边框与卡片底的层级校验"""
    hi, lo = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _panel_card_surface() -> QColor:
    """成果栏卡片的等价不透明底色：CARD_BG(250) over 气泡 over 聊天区

    层级与 message_card 一致：CONTENT_BG ← chatFrame(CARD_BG a150) ←
    ASSISTANT_CARD_BG(a150) ← 本面板(CARD_BG a250)。
    """
    from app.utils.design_tokens import opaque_surface_color

    Colors.refresh()
    card_fg = Colors.CARD_BG
    if "{alpha}" in card_fg:
        card_fg = card_fg.format(alpha=255)
    chat = opaque_surface_color(card_fg, 150, Colors.CONTENT_BG)
    bubble = opaque_surface_color(Colors.ASSISTANT_CARD_BG, 150, chat.name())
    return opaque_surface_color(card_fg, 250, bubble.name())


def _derived_card_border() -> str:
    """卡片边框：Colors.BORDER 对卡片合成底 <1.5 时按主题明暗逐档校正。

    为什么不用阴影：分层窗口 bounding rect 失真、每控件一个 effect 性能差
    （command_card.py / main_widget.py 均有同款结论）。为什么不靠 alpha 拉
    对比度：review 穷举 4 主题 alpha110~250，卡/气泡对比最高仅 1.13——
    CARD_BG 与 ASSISTANT_CARD_BG 原始色差太小（lumia 两者几乎同白），
    alpha 不是杠杆；层级改用 1px 边框 + 圆角维持 1.5 可辨识度。
    """
    Colors.refresh()
    card = _panel_card_surface()
    border = QColor(Colors.BORDER)
    if not border.isValid() or _contrast_ratio(border, card) >= 1.5:
        return Colors.BORDER
    from app.utils.theme_manager import theme_manager

    light = theme_manager.is_light_theme()
    color = QColor(border)
    for _ in range(40):
        color = color.darker(108) if light else color.lighter(108)
        if _contrast_ratio(color, card) >= 1.5:
            break
    return color.name()


def _rounded_cover(pix: QPixmap, w: int, h: int, radius: int, dpr: float = 1.0) -> QPixmap:
    """cover 裁切 + 圆角：QSS border-radius 不裁 pixmap，必须自绘。

    统一方格（方向 C）要视觉满格：先按 KeepAspectRatioByExpanding 缩放到覆盖
    目标框，再居中裁切，最后用 QPainterPath 裁圆角。
    """
    tw, th = max(1, int(round(w * dpr))), max(1, int(round(h * dpr)))
    scaled = pix.scaled(tw, th, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x = max(0, (scaled.width() - tw) // 2)
    y = max(0, (scaled.height() - th) // 2)
    cropped = scaled.copy(x, y, tw, th)
    out = QPixmap(tw, th)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    rr = max(0.0, radius * dpr)
    path.addRoundedRect(0, 0, tw, th, rr, rr)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, cropped)
    painter.end()
    out.setDevicePixelRatio(dpr)
    return out


class _ThumbLabel(QLabel):
    """视频占位封面：主题派生底色 + 播放三角 + 可选时长徽标（不做真实解码）"""

    def __init__(self, duration_text: str = "", parent=None):
        super().__init__(parent)
        self._duration = duration_text
        self.setFixedSize(_THUMB_W, _THUMB_H)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        # 不新增硬编码色：用 TEXT_MUTED 半透明叠在父级卡片底上（review D1）
        p.setBrush(QColor(_with_alpha(Colors.TEXT_MUTED, 46)))
        radius = int(BorderRadius.SM.rstrip("px"))
        p.drawRoundedRect(self.rect(), radius, radius)
        # 播放三角
        p.setBrush(QColor(Colors.TEXT_PRIMARY))
        cx, cy, r = self.width() / 2, self.height() / 2, 7
        from PyQt5.QtCore import QPointF

        p.drawPolygon(QPointF(cx - r * 0.6, cy - r), QPointF(cx - r * 0.6, cy + r), QPointF(cx + r, cy))
        if self._duration:
            p.setPen(QColor(Colors.TEXT_SECONDARY))
            f = self.font()
            f.setPixelSize(scale_font_size(9))
            p.setFont(f)
            p.drawText(self.rect().adjusted(0, 0, -4, -3), Qt.AlignBottom | Qt.AlignRight, self._duration)


class _KindBadge(QLabel):
    """非图片条目的类型徽标：语义色圆角色块 + 短标签"""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._label = _KIND_META.get(kind, _KIND_META["file"])[0]
        self.setFixedSize(_THUMB_W, _THUMB_H)
        self.setAlignment(Qt.AlignCenter)
        # 徽标字与样式必须构造期落地：此前只在主题刷新里补，而刷新路径遍历的是
        # 列表根节点（徽标是行的子控件，永远命中不到）→ 徽标一直是空白色块。
        self.setText(self._label)
        self.refresh_style()

    def refresh_style(self) -> None:
        color = _kind_color(self._kind)
        self.setStyleSheet(
            f"QLabel {{ background: {_with_alpha(color, 22)}; color: {color};"
            f" border: 1px solid {_with_alpha(color, 55)}; border-radius: {BorderRadius.SM};"
            f" {get_font_family_css()} {font_size_css(11)}; font-weight: 600; }}"
        )


class _ArtifactHeader(QFrame):
    """标题行：图标 + 「成果」 + 右侧计数 + chevron（整行可点，hover 120ms）"""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("artifactHeader")
        self.setFixedHeight(_HEADER_H)
        self._hover = 0.0
        self._hover_anim = QPropertyAnimation(self, b"hoverProgress", self)
        self._hover_anim.setDuration(Animations.HOVER_MS)
        self._hover_anim.setEasingCurve(QEasingCurve(Animations.EASE_HOVER))
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        # 左内距 8 = viewer 4 + QTextDocument.documentMargin 4（气泡 12 与卡片主布局 4
        # 已体现在面板左缘，不能再重复计入）；对齐正文首行文字左缘 X=24。
        layout.setContentsMargins(8, 3, 6, 3)
        layout.setSpacing(0)  # 间距逐段显式给：图标 6 / 计数与 chevron 4
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(16, 16)
        self._icon_label.setScaledContents(True)
        self._title_label = QLabel("成果", self)
        self._extra_label = QLabel("", self)
        layout.addWidget(self._icon_label)
        layout.addSpacing(6)
        layout.addWidget(self._title_label)
        layout.addStretch(1)
        layout.addWidget(self._extra_label)
        layout.addSpacing(4)
        self._collapse_btn = TransparentToolButton(get_icon("展开"), self)
        self._collapse_btn.setFixedSize(22, 22)
        self._collapse_btn.setIconSize(QSize(14, 14))
        self._collapse_btn.clicked.connect(self.clicked)
        layout.addWidget(self._collapse_btn)
        self.refresh_style()

    # ── hover 动画（paintEvent 自绘圆角底，QSS :hover 无法补间）──
    def _get_hover(self) -> float:
        return self._hover

    def _set_hover(self, value: float) -> None:
        self._hover = float(value)
        self.update()

    hoverProgress = pyqtProperty(float, fget=_get_hover, fset=_set_hover)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().enterEvent(event)
        self._animate_hover(1.0)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().leaveEvent(event)
        self._animate_hover(0.0)

    def _animate_hover(self, target: float) -> None:
        self._hover_anim.stop()
        if not Animations.motion_enabled():
            self._set_hover(target)
            return
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._hover > 0.001:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            base = parse_color(Colors.HOVER_BG)
            alpha = int(round(base.alpha() * self._hover))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(base.red(), base.green(), base.blue(), alpha))
            radius = int(BorderRadius.SM.rstrip("px"))
            p.drawRoundedRect(self.rect(), radius, radius)
        super().paintEvent(event)

    def set_extra(self, text: str) -> None:
        self._extra_label.setText(text)

    def set_expanded(self, expanded: bool) -> None:
        icon = "折叠" if expanded else "展开"
        self._collapse_btn.setIcon(get_icon(icon))
        tip = "折叠成果列表" if expanded else "展开成果列表"
        self._collapse_btn.setToolTip(tip)
        self.setToolTip(tip)

    def refresh_style(self) -> None:
        self._icon_label.setPixmap(get_icon("upload-file").pixmap(16, 16))
        self.setStyleSheet(
            f"QFrame#artifactHeader {{ background: transparent; border: none; }}"
            f" QLabel {{ color: {Colors.TEXT_PRIMARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(12)}; font-weight: 600; }}"
            f" QLabel#artifactCount {{ color: {Colors.TEXT_MUTED};"
            f" {font_size_css(11)}; font-weight: 400; }}"
        )
        self._extra_label.setObjectName("artifactCount")
        # 对象名变更后重新 polish，保证 #artifactCount 选择器生效
        self._extra_label.style().unpolish(self._extra_label)
        self._extra_label.style().polish(self._extra_label)


class InlineArtifactPanel(QWidget):
    """成果栏：分隔条 + 行式清单（默认折叠）

    宿主通用内嵌面板（``UIPluginRegistry.register_card_inline_panel``，
    panel_id="artifacts"）：

    - ``update_items(items)``：运行态增量推送（同路径原位更新、新路径追加）
    - ``restore_items(items)``：会话重载时按落库的 tool 消息重建
    - ``heightChanged``：宿主自动接线做卡片高度锚定补偿
    - ``previewImageRequested``：宿主自动接线到内置图片预览
    """

    heightChanged = pyqtSignal()
    # 图片单击 → 宿主内置预览（复用 message_card._on_preview_image 通道）
    previewImageRequested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._collapsed = True
        self._show_all = False  # 点过「还有 N 项」后不再截断
        self._items: List[Dict[str, Any]] = []
        self._last_sig: Optional[str] = None
        self._pending_thumbs: List[tuple] = []  # [(QLabel, path, w, h)] 展开态列表行的分帧解码队列
        self._pending_strip_thumbs: List[tuple] = []  # [(QLabel, path, w, h)] 折叠态画廊的解码队列
        # [A3-2] 有界 LRU（32 ≈ 384KB 上界 @64x48 RGBA 12KB/条）：面板随卡片
        # 存活、无确定性收尾点，上限是唯一可靠边界；命中触碰热序。
        # key 带 (path, 尺寸)：折叠画廊与展开列表请求不同尺寸，不互相污染
        self._thumb_pixmaps = _BoundedLRUCache(32)
        # 左键拖拽导出（拖到资源管理器/聊天软件即引用该文件）
        self._drag_press_pos = None
        self._drag_started = False
        # 折叠画廊当前可见槽位数：宽度变化只在槽位数变化时重建（防逐像素重排/重解码）
        self._strip_slot_count = -1
        # 展开动画状态：动画中宿主按钳后的 sizeHint 补偿（见 sizeHint 重写）
        self._list_anim: Optional[QPropertyAnimation] = None
        self._list_natural_h = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)

        self._header = _ArtifactHeader(self)
        self._header.clicked.connect(self._on_collapse_clicked)
        self._collapse_btn = self._header._collapse_btn
        layout.addWidget(self._header)

        # 折叠态画廊：浅底圆角容器 = 缩略图行 +（可选）非图片摘要行。
        # 只有折叠 + 有图片成果时可见，展开后让位给完整清单。
        self._thumb_strip = QFrame(self)
        self._thumb_strip.setObjectName("artifactStripCard")
        strip_box = QVBoxLayout(self._thumb_strip)
        # 左右内边距归零（口径裁定 2026-11-20）：槽位公式以 _thumb_strip 外宽为 W，
        # isotropic padding 不再参与计算；上下保留 6，摘要行 gap4 不变。
        strip_box.setContentsMargins(0, 6, 0, 6)
        strip_box.setSpacing(4)
        strip_row = QWidget(self._thumb_strip)
        strip_row.setStyleSheet("background: transparent;")
        self._strip_layout = QHBoxLayout(strip_row)
        self._strip_layout.setContentsMargins(0, 0, 0, 0)
        self._strip_layout.setSpacing(_COLLAPSED_GAP)
        self._strip_layout.addStretch(1)
        strip_box.addWidget(strip_row)
        self._summary_label = _ElidedLabel("", self._thumb_strip)
        self._summary_label.setObjectName("artifactSummary")
        self._summary_label.setFixedHeight(_COLLAPSED_SUMMARY_H)
        self._summary_label.setVisible(False)
        strip_box.addWidget(self._summary_label)
        self._thumb_strip.setVisible(False)
        layout.addWidget(self._thumb_strip)

        # 列表容器：同款浅底圆角卡片；不用 QScrollArea（sizeHint 缓存失真，见模块注释），全量展示
        self._list_wrap = QFrame(self)
        self._list_wrap.setObjectName("artifactListCard")
        self._list_layout = QVBoxLayout(self._list_wrap)
        self._list_layout.setContentsMargins(8, 6, 8, 6)
        self._list_layout.setSpacing(_LIST_GAP)
        self._list_layout.addStretch(1)
        self._list_wrap.setVisible(False)
        layout.addWidget(self._list_wrap)
        # 底部弹性空白：面板被给到高于 sizeHint 的空间时，多余高度落在这里，
        # 而不是被 header 吸收（否则折叠带的缩略图会被顶到面板底部）
        layout.addStretch(1)

        self.hide()  # 无成果整区隐藏
        self.refresh_style()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        # 宽度变化只影响折叠画廊的可见槽位数；槽位数不变不重建（防逐像素重排重解码）
        if not self._collapsed or not self._items or self._strip_slot_count <= 0:
            return
        images = sum(1 for it in self._items if it["kind"] == "image")
        if images and self._collapsed_slot_count(images) != self._strip_slot_count:
            self._rebuild_thumb_strip()
            lay = self.layout()
            if lay is not None:
                lay.invalidate()
            self.updateGeometry()
            self.heightChanged.emit()

    def _collapsed_slot_count(self, image_count: int) -> int:
        """按当前宽度算可见槽位（含可能的 +N 块）：slots = (W+6)//70，上限 12。

        W = ``_thumb_strip`` 外宽（画廊容器左右内边距已归零，等价内容宽）。
        槽位与块宽共用 ``_COLLAPSED_THUMB`` / ``_COLLAPSED_GAP`` 单一来源，
        不再维护第二套宽高比估算（旧实现的 112/72/6 与块宽计算是双套数）。
        单图特例：W<70 时仍显示 1 格（缩至可用宽），不消失。
        """
        if image_count <= 0:
            return 0
        if image_count == 1:
            return 1
        strip_w = self._thumb_strip.width()
        if strip_w <= 32:
            # 尚未完成布局（构造期 / 未 show）：按常见会话宽度兜底，show 后 resizeEvent 重算
            strip_w = 420
        slots = (strip_w + _COLLAPSED_GAP) // (_COLLAPSED_THUMB + _COLLAPSED_GAP)
        return max(0, min(slots, image_count, _COLLAPSED_MAX_THUMBS))

    # ── 折叠 ──

    def _on_collapse_clicked(self) -> None:
        self._collapsed = not self._collapsed
        self._header.set_expanded(not self._collapsed)
        if self._collapsed:
            # 折叠：先重建画廊拿目标占高（保持隐藏），列表收到同高后无缝换回画廊
            self._rebuild_thumb_strip()
            self._thumb_strip.setVisible(False)
            self._animate_list(False)
        else:
            # 展开：先取画廊占高作等高起点，再隐藏画廊、钳列表同高显示（标题不动）
            start = self._strip_visual_height()
            self._thumb_strip.setVisible(False)
            self._list_wrap.setVisible(True)
            self._list_wrap.setMaximumHeight(start)
            self._animate_list(True, start)
            self._schedule_thumb_decode()

    def _strip_visual_height(self) -> int:
        """折叠态画廊的占高（含摘要行）；画廊不可见则 0"""
        if self._thumb_strip.isHidden():
            return 0
        return self._thumb_strip.height()

    def _animate_list(self, expanding: bool, start: Optional[int] = None) -> None:
        """展开列表高度动画（轴向 maximumHeight，惯例同 markdown 折叠卡）：

        与画廊等高交接：展开从画廊占高长出、收起收到画廊占高再换回，
        全程面板高度单调变化，标题不抖。展开 ENTER_MS+OutCubic / 收起
        EXIT_MS+OutQuad（OutCubic 收起尾段拖尾）；逐帧 emit heightChanged
        让宿主卡片高度跟随。
        """
        wrap = self._list_wrap
        if not Animations.motion_enabled() or self.parentWidget() is None:
            # reduced-motion / 未挂载：跳过补间直落终值（收尾回调幂等）
            self._list_anim = None
            self._list_natural_h = 0
            if expanding:
                wrap.setMaximumHeight(_QWIDGETSIZE_MAX)
            else:
                wrap.hide()
                self._show_strip_after_collapse()
                wrap.setMaximumHeight(_QWIDGETSIZE_MAX)
            self._emit_height_chain()
            return
        old = self._list_anim
        if old is not None:
            old.stop()
        if expanding:
            start = start if start is not None else self._strip_visual_height()
            target = max(wrap.sizeHint().height(), 1)
        else:
            start = max(wrap.height(), 1)
            wrap.setMaximumHeight(start)  # 先钳到实际高度再动画
            target = self._strip_visual_height()
        self._list_natural_h = max(wrap.sizeHint().height(), 1)
        anim = QPropertyAnimation(wrap, b"maximumHeight", self)
        self._list_anim = anim
        anim.setDuration(Animations.ENTER_MS if expanding else Animations.EXIT_MS)
        anim.setEasingCurve(QEasingCurve(Animations.EASE_ENTER if expanding else Animations.EASE_EXIT))
        anim.setStartValue(start)
        anim.setEndValue(target)
        anim.valueChanged.connect(lambda _v: self._emit_height_chain())
        anim.finished.connect(lambda: self._on_list_anim_done(expanding))
        anim.start()

    def _on_list_anim_done(self, expanding: bool) -> None:
        self._list_anim = None
        self._list_natural_h = 0
        wrap = self._list_wrap
        if expanding:
            wrap.setMaximumHeight(_QWIDGETSIZE_MAX)  # 放开限高不二次跳变
        else:
            wrap.hide()
            wrap.setMaximumHeight(_QWIDGETSIZE_MAX)
            self._show_strip_after_collapse()
        self._emit_height_chain()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        """动画期间手算钳后占高（宿主补偿取 max(sizeHint, height)）

        maximumHeight 只钳 height 不钳布局 sizeHint：不重写的话展开第一帧
        宿主就把卡片拉到全高，标题先跳后动画（实测抖动根因）。布局自然高
        在子项受钳时不可信，故按可见子项手算：header + spacing + 列表(钳)。
        """
        anim = self._list_anim
        if anim is None or anim.state() != QAbstractAnimation.Running:
            return super().sizeHint()
        lay = self.layout()
        margins = lay.contentsMargins() if lay is not None else QMargins(0, 0, 0, 0)
        spacing = lay.spacing() if lay is not None else 0
        h = self._header.sizeHint().height()
        if self._list_wrap.isVisible():
            h += spacing + max(0, int(self._list_wrap.maximumHeight()))
        if self._thumb_strip.isVisible():
            h += spacing + self._thumb_strip.height()
        return QSize(super().sizeHint().width(), max(1, h + margins.top() + margins.bottom()))

    def _show_strip_after_collapse(self) -> None:
        """折叠动画收尾：画廊回归 + 解码调度（高度已在动画前算好，切换等高无缝）"""
        # 折叠带与清单互斥：折叠看缩略图，展开看完整行
        self._thumb_strip.setVisible(self._collapsed and self._strip_layout.count() > 1)
        if self._collapsed:
            self._schedule_strip_decode()

    def _emit_height_chain(self) -> None:
        """高度链对账：布局失效 + 宿主补偿（动画逐帧调用，值单调不抖）"""
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
        self.updateGeometry()
        self.heightChanged.emit()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """可见瞬间对账高度（欠账补 emit，契约同 InlineTodoPanel.showEvent）"""
        super().showEvent(event)
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
        self.updateGeometry()
        if self._collapsed:
            self._schedule_strip_decode()
        else:
            self._schedule_thumb_decode()
        self.heightChanged.emit()

    # ── 数据 ──

    def update_items(self, items: List[Dict[str, Any]]) -> None:
        """宿主通用契约：增量合并推送（同路径原位更新、新路径追加、保序）

        ★ 不替换：同轮多次工具产出与异步回填每次只带增量，整体替换会把先到
        的条目顶掉（2026-11 修）。空列表不清空，显式清空走 ``clear_artifacts``。
        """
        if not items:
            return
        merged = self._merge_items(self._items, items)
        self.update_artifacts(merged)

    def restore_items(self, items: List[Dict[str, Any]]) -> None:
        """宿主通用契约：会话重载历史回填（从 tool 消息的 artifacts 字段重建）"""
        self.update_items(items)

    @staticmethod
    def _merge_items(current, incoming) -> List[Dict[str, Any]]:
        """按 path 合并：同路径原位替换，新路径追加（保持首次出现顺序）"""
        merged = list(current or [])
        index = {}
        for i, item in enumerate(merged):
            if isinstance(item, dict) and item.get("path"):
                index[str(item["path"])] = i
        for raw in incoming or []:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("path") or "").strip()
            if not path:
                continue
            item = dict(raw)
            item["path"] = path
            pos = index.get(path)
            if pos is None:
                index[path] = len(merged)
                merged.append(item)
            else:
                merged[pos] = item
        return merged

    def update_artifacts(self, items: List[Dict[str, Any]]) -> None:
        """刷新成果列表（幂等：同签名跳过；同路径去重保序；坏条目静默丢弃）"""
        normalized: List[Dict[str, Any]] = []
        seen_paths: set = set()
        for raw in items or []:
            item = _normalize_item(raw)
            if item is None:
                continue
            key = item["path"]
            if key in seen_paths:
                continue
            seen_paths.add(key)
            normalized.append(item)
        if not normalized:
            self._items = []
            self._last_sig = None
            self._show_all = False
            self._clear_items()
            self._clear_strip()
            self._thumb_strip.setVisible(False)
            self._header.set_extra("")
            self.hide()
            self.heightChanged.emit()
            return

        sig = repr([(it["path"], it["size"], it["kind"], it["name"], it["caption"]) for it in normalized])
        if sig == self._last_sig and self._items:
            return
        self._last_sig = sig
        self._items = normalized
        self._rebuild()
        self.show()
        self._header.set_extra(f"{len(normalized)} 项" if len(normalized) > 1 else "")
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
        self.updateGeometry()
        if self._collapsed:
            self._schedule_strip_decode()
        else:
            self._schedule_thumb_decode()
        self.heightChanged.emit()

    def clear_artifacts(self) -> None:
        """清空（实例保留，仅隐藏——面板随卡片存活，重建时直接复用）"""
        self._items = []
        self._last_sig = None
        self._show_all = False
        self._pending_thumbs = []
        self._clear_items()
        self._clear_strip()
        self._thumb_strip.setVisible(False)
        self._header.set_extra("")
        self.hide()
        self.heightChanged.emit()

    def _clear_items(self) -> None:
        """彻底摘除旧条目（setParent(None) 防残影，同 InlineTodoPanel._clear_items）"""
        # 解码队列一并清空：旧 label 即将销毁，残留队列会让分帧解码摸到死壳
        self._pending_thumbs = []
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            w.setParent(None)
            w.deleteLater()

    def _rebuild(self) -> None:
        self._clear_items()
        self._rebuild_thumb_strip()
        visible = self._items if self._show_all else self._items[:MAX_VISIBLE_ITEMS]
        for item in visible:
            self._list_layout.addWidget(self._build_row(item))
        overflow = len(self._items) - len(visible)
        if overflow > 0:
            more = QLabel(f"… 还有 {overflow} 项，点击展开全部", self._list_wrap)
            more.setCursor(Qt.PointingHandCursor)
            more.setToolTip("展开全部成果条目")
            more.setStyleSheet(
                f"QLabel {{ color: {Colors.BORDER_ACCENT}; background: transparent;"
                f" border: none; {get_font_family_css()} {font_size_css(12)};"
                f" font-weight: 600; padding: 4px 8px; }}"
            )
            more.mousePressEvent = lambda _ev: self._expand_all()
            self._list_layout.addWidget(more)

    def _expand_all(self) -> None:
        """「还有 N 项」点击：取消截断重排（单向，数据更新后保持全量）"""
        if self._show_all:
            return
        self._show_all = True
        self._rebuild()
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
        self.updateGeometry()
        self._schedule_thumb_decode()
        self.heightChanged.emit()

    # ── 折叠态缩略图带 ──

    def _clear_strip(self) -> None:
        self._pending_strip_thumbs = []
        while self._strip_layout.count():
            item = self._strip_layout.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            w.setParent(None)
            w.deleteLater()

    def _strip_block_side(self) -> int:
        """单格边长：常规 64；W<70 的极窄栏缩到可用宽（单图不消失）"""
        strip_w = self._thumb_strip.width()
        if strip_w <= 32:
            strip_w = 420
        return max(24, min(_COLLAPSED_THUMB, strip_w))

    def _dpr(self) -> float:
        """当前 DPR（qtbot/无窗口环境回退 1.0）"""
        try:
            return max(1.0, float(self.devicePixelRatioF()))
        except (AttributeError, RuntimeError):
            return 1.0

    def _build_strip_thumb(self, item: Dict[str, Any]) -> QLabel:
        """单个画廊块：方格（常规 64×64）、cover 圆角裁切、右下 IMG 角标

        极窄栏（W<64）按可用宽等比缩格，仍保持正方形（不取 64×64 裁右缘，
        避免竖条变形）；缩略图同样按 side×side 做 cover。
        """
        path = item["path"]
        side = self._strip_block_side()
        thumb = QLabel(self._thumb_strip)
        thumb.setObjectName("artifactThumbnail")
        thumb.setFixedSize(side, side)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setCursor(Qt.PointingHandCursor)
        thumb.setToolTip(
            f"{item['name']}\n{_KIND_LABEL.get('image', '图像')} · 单击预览，左键拖拽可引用到其他应用"
        )
        pix = self._thumb_pixmaps.get(_thumb_key(path, side, side, self._dpr()))
        if pix is not None and not pix.isNull():
            thumb.setPixmap(pix)
        else:
            thumb.setText("…")
            self._pending_strip_thumbs.append((thumb, path, side, side))
        thumb.mousePressEvent = lambda ev, p=path: self._on_strip_press(ev, p)
        thumb.mouseReleaseEvent = lambda ev, p=path: self._on_strip_release(ev, p)
        thumb.mouseMoveEvent = lambda ev, p=path, w=thumb: self._on_row_move(ev, p, w)
        return thumb

    def _rebuild_thumb_strip(self) -> None:
        """折叠态单行画廊（方向 C）：统一 64×64，溢出收进 +N，非图片只入摘要

        槽位与块宽同一套常量；放得下时全部展示、绝不出 +N；放不下时最后
        一个槽位让给 +N（+N 与图片同尺寸）。cover 裁切保证每格视觉满格。
        """
        self._clear_strip()
        images = [it for it in self._items if it["kind"] == "image"]
        if not images:
            self._thumb_strip.setVisible(False)
            self._summary_label.setVisible(False)
            self._strip_slot_count = 0
            return
        slots = self._collapsed_slot_count(len(images))
        self._strip_slot_count = slots
        overflowing = len(images) > slots
        # +N 占一格：溢出时缩略图数 = slots-1（slots=1 时 0 图 + 仅计数入口）
        visible = (slots - 1) if overflowing else len(images)
        visible = max(0, min(visible, len(images)))
        for item in images[:visible]:
            self._strip_layout.addWidget(self._build_strip_thumb(item))
        rest = len(images) - visible
        if rest > 0:
            side = self._strip_block_side()
            more = QLabel(f"+{rest}", self._thumb_strip)
            more.setObjectName("artifactOverflow")
            more.setFixedSize(side, side)
            more.setAlignment(Qt.AlignCenter)
            more.setCursor(Qt.PointingHandCursor)
            more.setToolTip(f"还有 {rest} 张图片，点击展开全部")
            # 点 +N 直接进完整列表（折叠看图，展开找文件）
            more.mousePressEvent = lambda _ev: self._on_collapse_clicked()
            self._strip_layout.addWidget(more)
        self._strip_layout.addStretch(1)
        files_n = len(self._items) - len(images)
        summary_text = self._summary_text(images, files_n)
        if summary_text:
            self._summary_label.setText(summary_text)
            self._summary_label.setToolTip(summary_text)
            self._summary_label.setVisible(True)
        else:
            self._summary_label.setVisible(False)
        strip_layout = self._thumb_strip.layout()
        sm = strip_layout.contentsMargins() if strip_layout is not None else QMargins(0, 0, 0, 0)
        spacing = strip_layout.spacing() if strip_layout is not None else 4
        strip_h = sm.top() + sm.bottom() + _COLLAPSED_THUMB
        if summary_text:
            strip_h += spacing + _COLLAPSED_SUMMARY_H
        total_h = strip_h
        self._thumb_strip.setFixedHeight(total_h)
        self._style_strip()
        self._thumb_strip.setVisible(self._collapsed)

    @staticmethod
    def _summary_text(images: List[Dict[str, Any]], files_n: int) -> str:
        """摘要行：首项文件名 + 类型分布（单图直接给文件名），避免无信息计数"""
        parts: List[str] = []
        if images:
            parts.append(str(images[0]["name"]))
            if len(images) > 1:
                parts.append(f"{len(images)} 张图片")
        if files_n > 0:
            parts.append(f"{files_n} 个文件")
        return " · ".join(parts)

    def _on_strip_press(self, ev: QMouseEvent, path: str) -> None:
        """折叠带缩略图：左键按下记拖拽起点（单击预览在抬起时判定）"""
        if ev.button() == Qt.LeftButton:
            self._drag_press_pos = ev.pos()
            self._drag_started = False

    def _on_strip_release(self, ev: QMouseEvent, path: str) -> None:
        started = self._drag_started
        self._drag_press_pos = None
        self._drag_started = False
        if ev.button() == Qt.LeftButton and not started:
            self.previewImageRequested.emit(path)

    # ── 条目行 ──

    def _build_row(self, item: Dict[str, Any]) -> QFrame:
        row = QFrame(self._list_wrap)
        row.setObjectName("artifactItem")
        row.setFixedHeight(_LIST_ROW_H)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        kind = item["kind"]
        path = item["path"]

        # 预览块：40×40（图片 cover 圆角 + 右下 IMG 角标）
        if kind == "image":
            thumb = QLabel(row)
            thumb.setObjectName("artifactRowThumb")
            thumb.setFixedSize(_THUMB_W, _THUMB_H)
            thumb.setAlignment(Qt.AlignCenter)
            pix = self._thumb_pixmaps.get(_thumb_key(path, _THUMB_W, _THUMB_H, self._dpr()))
            if pix is not None and not pix.isNull():
                thumb.setPixmap(pix)
            else:
                thumb.setText("…")
                # 折叠态也入队但不调度（_decode_next_thumb 检查折叠态即停）；
                # 展开切换时 _on_collapse_clicked 统一调度。此前版本折叠态
                # 直接剔除队列，导致「先推数据后展开」的场景缩略图永不解码。
                self._pending_thumbs.append((thumb, path, _THUMB_W, _THUMB_H))
        elif kind == "video":
            duration = ""
            meta = item.get("meta") or {}
            if meta.get("duration"):
                duration = f"{meta['duration']}s"
            thumb = _ThumbLabel(duration, row)
        else:
            thumb = _KindBadge(kind, row)
        lay.addWidget(thumb)

        # 文本区：名称 + meta
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        name = _ElidedLabel(item["name"], row)
        name.setStyleSheet(
            f"QLabel {{ color: {Colors.TEXT_PRIMARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(12)}; font-weight: 600; }}"
        )
        text_col.addWidget(name)

        meta_bits = [_KIND_LABEL.get(kind, "文件")]
        if item.get("width") and item.get("height"):
            meta_bits.append(f"{item['width']}×{item['height']}")
        size_text = _human_size(item.get("size"))
        if size_text:
            meta_bits.append(size_text)
        if item.get("caption") and kind in ("text", "html"):
            meta_bits.append(item["caption"][:40])
        meta = _ElidedLabel(" · ".join(b for b in meta_bits if b), row)
        meta.setStyleSheet(
            f"QLabel {{ color: {Colors.TEXT_MUTED}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(11)}; }}"
        )
        text_col.addWidget(meta)
        lay.addLayout(text_col, stretch=1)

        # 打开所在文件夹
        folder_btn = TransparentToolButton(get_icon("folder"), row)
        folder_btn.setFixedSize(22, 22)
        folder_btn.setToolTip("打开所在文件夹")
        folder_btn.clicked.connect(lambda _=False, p=path: self._reveal_in_folder(p))
        lay.addWidget(folder_btn)

        row.setToolTip(f"{path}\n（双击打开，左键拖拽可引用到其他应用，右键更多操作）")
        row.setCursor(Qt.PointingHandCursor)

        row.mouseDoubleClickEvent = lambda ev, p=path, k=kind: self._on_row_activate(p, k)
        row.mousePressEvent = lambda ev, p=path, k=kind, r=row: self._on_row_press(ev, p, k, r)
        row.mouseReleaseEvent = lambda ev, p=path, k=kind, r=row: self._on_row_release(ev, p, k, r)
        row.mouseMoveEvent = lambda ev, p=path, r=row: self._on_row_move(ev, p, r)
        row.setContextMenuPolicy(Qt.CustomContextMenu)
        self._style_row(row)
        return row

    def _on_row_press(self, ev: QMouseEvent, path: str, kind: str, row: QFrame) -> None:
        """左键按下只记拖拽起点；单击预览改到抬起（否则一按就预览，拖不走）"""
        if ev.button() == Qt.LeftButton:
            self._drag_press_pos = ev.pos()
            self._drag_started = False
        elif ev.button() == Qt.RightButton:
            self._show_context_menu(ev.globalPos(), path, kind)

    def _on_row_release(self, ev: QMouseEvent, path: str, kind: str, row: QFrame) -> None:
        started = self._drag_started
        self._drag_press_pos = None
        self._drag_started = False
        if ev.button() == Qt.LeftButton and not started and kind == "image":
            self.previewImageRequested.emit(path)

    def _on_row_move(self, ev: QMouseEvent, path: str, row: QWidget) -> None:
        """左键按住移动超阈值 → 起拖（mime urls，可拖到资源管理器/聊天窗口）"""
        if self._drag_press_pos is None or not (ev.buttons() & Qt.LeftButton):
            return
        from PyQt5.QtWidgets import QApplication

        if (ev.pos() - self._drag_press_pos).manhattanLength() < QApplication.startDragDistance():
            return
        # 只起拖一次：清空起点后，同一手势内的后续 move 全部短路
        self._drag_press_pos = None
        drag = self._make_drag(path, row)
        if drag is None:
            return
        self._drag_started = True
        try:
            drag.exec_(Qt.CopyAction)
        except RuntimeError:
            pass

    def _make_drag(self, path: str, row: QWidget):
        """构造文件拖拽对象（独立方法，便于单测直接校验 mimeData）"""
        if not path or not os.path.exists(path):
            return None
        from PyQt5.QtCore import QMimeData
        from PyQt5.QtGui import QDrag

        drag = QDrag(self)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path)])
        drag.setMimeData(mime)
        try:
            # 优先用已裁圆角的行内缩略图（40×40），避免 grab 直角/透明角黑边；
            # 无缩略图（视频/文本行）时对 grab 结果做同款圆角裁切。
            thumb = row.findChild(QLabel, "artifactRowThumb")
            pix = thumb.pixmap() if thumb is not None else None
            if pix is None or pix.isNull():
                raw = row.grab()
                if raw.isNull():
                    return drag
                width = min(240, max(64, raw.width()))
                pix = raw.scaledToWidth(width, Qt.SmoothTransformation)
            radius = int(BorderRadius.SM.rstrip("px"))
            pix = _rounded_cover(pix, pix.width(), pix.height(), radius)
            if not pix.isNull():
                drag.setPixmap(pix)
        except (RuntimeError, AttributeError):
            pass
        return drag

    def _on_row_activate(self, path: str, kind: str) -> None:
        """双击：图片走内置预览，其余走能力池 file.open（接管优先，降级系统程序）

        与消息卡片里点工具参数文件走同一条链路：装了 file-tree 这类接管方时，
        代码 / md / 图片直接开在应用内预览页，不跳出应用。
        """
        if kind == "image":
            self.previewImageRequested.emit(path)
            return
        self._open_with_opener_chain(path)

    @staticmethod
    def _open_with_opener_chain(path: str) -> None:
        """经能力池 file.open 打开：插件接管优先，无人接管自动降级系统程序

        ⚠️ 不直接 import 宿主注册表：插件与主程序之间的唯一调用入口是能力池。
        能力不可用（未声明 / 未注册）时才用本机兜底，保证点得动。
        """
        try:
            from app.plugins.sdk import caps_for

            api = caps_for("file.open", plugin="artifacts-manager", surface="ui")
            if api is not None:
                api["open"](path)
                return
        except Exception:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _show_context_menu(self, global_pos, path: str, kind: str) -> None:
        """右键菜单（主题样式与 TabPanel._build_context_menu 同款：运行时插值 QSS，

        裸 QMenu 不吃全局主题样式，深色主题下整块全黑——必须显式套。）
        """
        from app.utils.design_tokens import Colors
        from app.utils.utils import get_font_family_css

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {Colors.CARD_BG.format(alpha=255)};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(13)}
            }}
            QMenu::item:selected {{
                background: {Colors.HOVER_BG};
            }}
            QMenu::separator {{
                height: 1px;
                background: {Colors.BORDER};
                margin: 4px 8px;
            }}
        """)
        if kind == "image":
            act_preview = menu.addAction("预览")
            act_preview.triggered.connect(lambda: self.previewImageRequested.emit(path))
            menu.addSeparator()
        act_open = menu.addAction("打开文件")
        act_open.triggered.connect(lambda: self._open_with_opener_chain(path))
        act_folder = menu.addAction("打开所在文件夹")
        act_folder.triggered.connect(lambda: self._reveal_in_folder(path))
        act_copy = menu.addAction("复制路径")
        act_copy.triggered.connect(lambda: self._copy_path(path))
        menu.exec_(global_pos)
        menu.deleteLater()

    def _reveal_in_folder(self, path: str) -> None:
        """资源管理器定位文件（Windows explorer /select；其余平台打开父目录）"""
        if not path or not os.path.exists(path):
            return
        import subprocess
        import sys

        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path) or "/"))

    def _copy_path(self, path: str) -> None:
        from PyQt5.QtWidgets import QApplication

        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(path)

    def _style_row(self, row: QFrame) -> None:
        row.setStyleSheet(
            f"QFrame#artifactItem {{ background: transparent; border: none;"
            f" border-radius: {BorderRadius.SM}; }}"
            f"QFrame#artifactItem:hover {{ background: {Colors.HOVER_BG}; }}"
        )

    # ── 缩略图分帧解码 ──

    def _schedule_thumb_decode(self) -> None:
        """展开态逐帧解码缩略图：每拍一张让出事件循环，多张 4K 图不卡 UI"""
        if not self._pending_thumbs:
            return
        QTimer.singleShot(0, self._decode_next_thumb)

    def _decode_next_thumb(self) -> None:
        # 队首不可见（如刚又被折叠）就先不取：pop 掉会永久丢掉这张的解码机会，
        # 再展开时队列已空 → 缩略图永驻占位符。
        if self._collapsed or not self._pending_thumbs:
            return
        label, path, w, h = self._pending_thumbs[0]
        if not label.isVisible():
            return
        self._pending_thumbs.pop(0)
        self._apply_thumb(label, path, w, h)
        if self._pending_thumbs:
            QTimer.singleShot(10, self._decode_next_thumb)

    def _schedule_strip_decode(self) -> None:
        """折叠态缩略图带解码（与列表队列分离，互不阻塞）"""
        if not self._pending_strip_thumbs:
            return
        QTimer.singleShot(0, self._decode_next_strip_thumb)

    def _decode_next_strip_thumb(self) -> None:
        if not self._collapsed or not self._pending_strip_thumbs:
            return
        label, path, w, h = self._pending_strip_thumbs[0]
        if not label.isVisible():
            return
        self._pending_strip_thumbs.pop(0)
        self._apply_thumb(label, path, w, h)
        if self._pending_strip_thumbs:
            QTimer.singleShot(10, self._decode_next_strip_thumb)

    def _apply_thumb(self, label: QLabel, path: str, w: int = _THUMB_W, h: int = _THUMB_H) -> None:
        """解码并回填（缓存命中直接复用；失败显示类型徽标不报错）"""
        dpr = self._dpr()
        try:
            pix = self._thumb_pixmaps.get(_thumb_key(path, w, h, dpr))
            if pix is None:
                pix = self._load_thumb(path, w, h)
                if pix is not None and not pix.isNull():
                    self._thumb_pixmaps[_thumb_key(path, w, h, dpr)] = pix
            if not label.isVisible():
                return
            if pix is not None and not pix.isNull():
                label.setPixmap(pix)
            else:
                # 文件已清理/不存在：显示类型徽标而非无解释的 ✕（64 方格内 10 档/600 居中，不贴边）
                label.setText(_KIND_META["image"][0])
                f = label.font()
                f.setPixelSize(scale_font_size(10))
                f.setBold(True)
                label.setFont(f)
                label.setToolTip(f"{path}\n文件已不存在或无法读取（可能已被清理）")
        except (OSError, RuntimeError):
            pass

    def _load_thumb(self, path: str, w: int = _THUMB_W, h: int = _THUMB_H) -> Optional[QPixmap]:
        """按缩略尺寸解码并做 cover + 圆角（方向 C 固定方格）

        不再调用 QImageReader.size() 读元数据：方向 C 块尺寸恒为方格，
        直接解码后按目标物理尺寸（乘 DPR）KeepAspectRatioByExpanding 缩放，
        再由 ``_rounded_cover`` 居中裁切。代价是单张大图全量解码的瞬时峰值，
        由既有分帧队列（每拍一张）兜住，不阻塞 UI。
        """
        dpr = self._dpr()
        source = QPixmap(path)
        if source.isNull():
            return None
        radius = int(BorderRadius.SM.rstrip("px"))
        return _rounded_cover(source, w, h, radius, dpr)

    # ── 主题 ──

    def refresh_style(self) -> None:
        self._header.refresh_style()
        self._style_strip()
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if isinstance(w, QFrame) and w.objectName() == "artifactItem":
                self._style_row(w)
        # 徽标/角标是行/缩略图的子控件，旧实现按行根节点遍历永远命中不到
        for badge in self.findChildren(_KindBadge):
            badge.refresh_style()

    def _style_strip(self) -> None:
        """画廊 / 展开列表两张卡片的主题化样式（B 项：alpha250 + 派生边框）

        边框通过 ``_derived_card_border`` 保证对卡片合成底 ≥1.5；
        层级只用「底 + 1px 边框 + 圆角」，不用 DropShadow（原因见 helper 注释）。
        """
        from app.utils.design_tokens import Colors

        border = _derived_card_border()
        card_css = (
            f"background: {Colors.CARD_BG.format(alpha=250)};"
            f" border: 1px solid {border}; border-radius: {BorderRadius.SM};"
        )
        self._thumb_strip.setStyleSheet(f"QFrame#artifactStripCard {{ {card_css} }}")
        self._list_wrap.setStyleSheet(f"QFrame#artifactListCard {{ {card_css} }}")
        self._summary_label.setStyleSheet(
            f"QLabel#artifactSummary {{ color: {Colors.TEXT_MUTED}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(11)}; padding: 0 2px; }}"
        )
        for lab in self._thumb_strip.findChildren(QLabel):
            name = lab.objectName()
            if name == "artifactThumbnail":
                lab.setStyleSheet(
                    f"QLabel#artifactThumbnail {{ background: {Colors.HOVER_BG};"
                    f" border: 1px solid transparent; border-radius: {BorderRadius.SM}; }}"
                    f" QLabel#artifactThumbnail:hover {{ border-color: {Colors.BORDER_ACCENT}; }}"
                )
            elif name == "artifactOverflow":
                lab.setStyleSheet(
                    f"QLabel#artifactOverflow {{ color: {Colors.TEXT_SECONDARY};"
                    f" background: transparent; border: 1px dashed {Colors.BORDER};"
                    f" border-radius: {BorderRadius.SM}; {get_font_family_css()} {font_size_css(12)};"
                    f" font-weight: 600; }}"
                )
