# -*- coding: utf-8 -*-
"""文件预览页 — 图片 / 文本类文件的卡片内预览

功能：
- 类型判定：图片（按 Qt 实际支持的格式）/ 文本（后缀白名单）/ 其他（交默认程序）
- 图片：适应窗口为基准，Ctrl+滚轮或按钮缩放，左键拖动平移，双击复位
- 文本：只读 + 等宽字体 + 左侧行号区
- 限流：超大文本只显示前 N 行，超大图片按长边缩放载入，超标文件拒绝预览

设计约束（闭包）：
- 不导入 app.core / app.widgets 内部的任何模块（与 cards.py / tree_widget.py 同规则）
- 纯 PyQt5 + qfluentwidgets，文件读写走 stdlib
"""

import os
import time
from typing import Optional, Tuple

from loguru import logger

from PyQt5.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QImageReader, QPainter, QPen, QPixmap, QTextCursor
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, TransparentToolButton

from .highlight import CodeHighlighter, analyze

# ── 限流阈值 ──────────────────────────────────────────────

TEXT_MAX_BYTES = 1024 * 1024  # 超过只读前 1MB
TEXT_MAX_LINES = 2000  # 显示行数上限
IMAGE_MAX_BYTES = 50 * 1024 * 1024  # 超过拒绝预览
IMAGE_MAX_PIXELS = 20_000_000  # 超过按长边缩放载入（防 300MB+ QImage 卡死）
IMAGE_MAX_SIDE = 4096

# ── 后缀集合 ──────────────────────────────────────────────

# 文本类：按后缀白名单（宁缺毋滥，未知后缀交系统默认程序）
_TEXT_EXTS = {
    # 代码
    ".py", ".pyi", ".pyx", ".pxd", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm",
    ".cs", ".java", ".kt", ".kts", ".scala", ".go", ".rs", ".swift", ".dart",
    ".rb", ".php", ".lua", ".pl", ".pm", ".r", ".jl", ".sql", ".vue", ".svelte",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd",
    # 配置 / 数据
    ".json", ".json5", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".properties", ".env", ".lock", ".csv", ".tsv", ".xml", ".plist", ".gradle",
    ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore",
    # 文档 / 标记
    ".md", ".markdown", ".rst", ".txt", ".log", ".tex", ".bib",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".svg",
}
# 无后缀但确定是文本的常见文件名（小写比较）
_TEXT_NAMES = {
    "dockerfile", "makefile", "cmakelists.txt", "license", "readme", "changelog",
    "authors", "notice", "procfile", "gemfile", "rakefile", "vagrantfile", ".gitignore",
}


def _image_exts() -> frozenset:
    """Qt 实际支持的图片后缀（不硬编码，平台差异自动跟随）"""
    try:
        formats = QImageReader.supportedImageFormats()
    except Exception:
        formats = []
    exts = set()
    for fmt in formats:
        if isinstance(fmt, (bytes, bytearray)):
            name = fmt.decode("ascii", "ignore")
        elif hasattr(fmt, "data"):
            name = bytes(fmt.data()).decode("ascii", "ignore")
        else:
            name = str(fmt)
        name = name.strip().lower()
        if name:
            exts.add("." + name)
    if not exts:
        exts = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".ico", ".tif", ".tiff"}
    return frozenset(exts)


_IMAGE_EXTS = _image_exts()


def detect_kind(path: str) -> str:
    """判定预览类型：``"image"`` / ``"text"`` / ``""``（不支持）"""
    try:
        if not path or not os.path.isfile(path):
            return ""
    except OSError:
        return ""
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _TEXT_EXTS or name in _TEXT_NAMES:
        return "text"
    return ""


# ── 读取 ──────────────────────────────────────────────────


def _decode_bytes(raw: bytes) -> str:
    """UTF-8（含 BOM）→ GBK → UTF-8 替换非法字节，全程不抛"""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def read_text(path: str) -> Tuple[str, str]:
    """读文本，返回 (内容, 限流提示)。二进制内容抛 ``ValueError``。"""
    size = os.path.getsize(path)
    over_bytes = size > TEXT_MAX_BYTES
    with open(path, "rb") as f:
        raw = f.read(TEXT_MAX_BYTES if over_bytes else size + 1)
    if b"\x00" in raw[:8192]:
        raise ValueError("该文件含二进制内容，无法作为文本预览")
    text = _decode_bytes(raw)

    over_lines = False
    lines = text.splitlines()
    if len(lines) > TEXT_MAX_LINES:
        text = "\n".join(lines[:TEXT_MAX_LINES])
        over_lines = True
    if over_bytes and over_lines:
        hint = f"文件较大（{size / 1024 / 1024:.1f} MB），仅显示前 {TEXT_MAX_LINES} 行"
    elif over_bytes:
        hint = f"文件较大（{size / 1024 / 1024:.1f} MB），仅显示前 {TEXT_MAX_BYTES // 1024} KB"
    elif over_lines:
        hint = f"文件行数较多，仅显示前 {TEXT_MAX_LINES} 行"
    else:
        hint = ""
    return text, hint


def read_image(path: str) -> Tuple[QPixmap, str]:
    """读图片，返回 (pixmap, 缩放提示)。超限或解码失败抛 ``ValueError``。"""
    size = os.path.getsize(path)
    if size > IMAGE_MAX_BYTES:
        raise ValueError(f"图片过大（{size / 1024 / 1024:.1f} MB），请用默认程序打开")

    reader = QImageReader(path)
    reader.setAutoTransform(True)  # 按 EXIF 方向摆正
    hint = ""
    dim = reader.size()
    if dim.isValid() and dim.width() > 0 and dim.height() > 0:
        w, h = dim.width(), dim.height()
        if w * h > IMAGE_MAX_PIXELS:
            scale = IMAGE_MAX_SIDE / float(max(w, h))
            reader.setScaledSize(QSize(max(1, int(w * scale)), max(1, int(h * scale))))
            hint = f"图片尺寸 {w}×{h}，已缩小载入"
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString() or "无法读取该图片")
    return QPixmap.fromImage(image), hint


def _mono_font(pixel_size: int) -> QFont:
    font = QFont()
    font.setStyleHint(QFont.Monospace)
    try:
        font.setFamilies(["Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New"])
    except (AttributeError, TypeError):
        font.setFamily("Consolas")
    font.setPixelSize(max(11, int(pixel_size)))
    return font

class _OpenWithButton(QWidget):
    """「默认程序图标 + 下拉箭头」分裂按钮（纯自绘）

    为什么不用 qfluentwidgets 的 SplitToolButton：它把图标绘制托管给内部
    ToolButton，程序图标（QIcon）在部分版本/打包环境下整块不显示，而且
    无法在本机验证打包环境的具体版本。自绘只依赖 QPainter，画什么是什么。
    """

    default_clicked = pyqtSignal()
    menu_clicked = pyqtSignal()

    ICON_ZONE = 36  # 左半（图标区）宽度
    ICON_SIZE = 16

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._icon = QIcon()
        self._pixmap = QPixmap()
        self._pixmap_dpr = 0.0
        self._fg = QColor(150, 150, 150)
        self._hover_bg = QColor(255, 255, 255, 20)
        self._hover_zone = 0  # 0=无 / 1=左半 / 2=右半
        self.setFixedSize(58, 26)
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)

    # ── 外部接口 ──

    def set_icon(self, icon: QIcon) -> None:
        """外部图标一次性转成 pixmap

        ★ paintEvent 里绝不能直接调 ``QIcon.pixmap()``：QFileIconProvider 返回的
        QIcon 每次都可能重新走一次 shell 图标提取。动画壁纸主题下按钮每帧重绘，
        实测主线程被单次 5199ms 阻塞卡死（watchdog 栈顶就在这行）。
        """
        self._icon = icon or QIcon()
        self._pixmap_dpr = 0.0  # 强制按当前 DPR 重建
        self._rebuild_pixmap()
        self.update()

    def icon(self) -> QIcon:
        return self._icon

    def has_icon(self) -> bool:
        return not self._pixmap.isNull()

    def set_colors(self, fg: QColor, hover_bg: QColor) -> None:
        self._fg = QColor(fg)
        self._hover_bg = QColor(hover_bg)
        self.update()

    def _rebuild_pixmap(self) -> None:
        self._pixmap = QPixmap()
        if self._icon.isNull():
            return
        dpr = self.devicePixelRatioF() or 1.0
        size = max(1, int(round(self.ICON_SIZE * dpr)))
        pixmap = self._icon.pixmap(QSize(size, size))
        if pixmap.isNull():
            return
        pixmap.setDevicePixelRatio(dpr)
        self._pixmap = pixmap
        self._pixmap_dpr = dpr

    # ── 绘制 ──

    def _zone_at(self, pos: QPoint) -> int:
        return 1 if pos.x() < self.ICON_ZONE else 2

    def paintEvent(self, event):  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        if self._hover_zone:
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._hover_bg)
            painter.drawRoundedRect(rect, 5, 5)

        # 左半：默认程序图标（预渲染的 pixmap，绘制期零 QIcon 调用）
        dpr = self.devicePixelRatioF() or 1.0
        if not self._icon.isNull() and abs(dpr - self._pixmap_dpr) > 1e-3:
            self._rebuild_pixmap()  # 窗口搬去另一块屏（DPR 变化）时补一次
        if not self._pixmap.isNull():
            ratio = self._pixmap.devicePixelRatio() or 1.0
            draw_w = self._pixmap.width() / ratio
            draw_h = self._pixmap.height() / ratio
            x = (self.ICON_ZONE - draw_w) / 2.0
            y = (self.height() - draw_h) / 2.0
            painter.drawPixmap(QPointF(x, y), self._pixmap)

        # 分隔线
        pen = QPen(self._fg)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(QPointF(self.ICON_ZONE, 6), QPointF(self.ICON_ZONE, self.height() - 6))

        # 右半：下拉箭头
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        cx = self.ICON_ZONE + (self.width() - self.ICON_ZONE) / 2.0
        cy = self.height() / 2.0
        painter.drawLine(QPointF(cx - 3.2, cy - 1.5), QPointF(cx, cy + 1.7))
        painter.drawLine(QPointF(cx + 3.2, cy - 1.5), QPointF(cx, cy + 1.7))
        painter.end()

    # ── 交互 ──

    def mousePressEvent(self, event):  # noqa: N802 (Qt 命名)
        if event.button() == Qt.LeftButton:
            if self._zone_at(event.pos()) == 1:
                self.default_clicked.emit()
            else:
                self.menu_clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 (Qt 命名)
        zone = self._zone_at(event.pos())
        if zone != self._hover_zone:
            self._hover_zone = zone
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # noqa: N802 (Qt 命名)
        if self._hover_zone:
            self._hover_zone = 0
            self.update()
        super().leaveEvent(event)


def _wrap_icon(color: QColor) -> QIcon:
    """自绘「自动换行」图标（折返箭头）—— FluentIcon 里没有对应资源"""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(color)
    pen.setWidthF(2.0)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.scale(2.0, 2.0)  # 以 16×16 逻辑坐标绘制，物理 2 倍更清晰
    painter.drawLine(QPointF(2.5, 4.0), QPointF(13.0, 4.0))
    painter.drawLine(QPointF(2.5, 7.5), QPointF(11.0, 7.5))
    painter.drawLine(QPointF(11.0, 7.5), QPointF(11.0, 11.5))
    painter.drawLine(QPointF(11.0, 11.5), QPointF(6.0, 11.5))
    painter.drawLine(QPointF(6.0, 11.5), QPointF(8.0, 9.6))
    painter.drawLine(QPointF(6.0, 11.5), QPointF(8.0, 13.4))
    painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)


# ── 省略名标签 ────────────────────────────────────────────


class _ElidedLabel(QLabel):
    """按控件宽度中间省略的 QLabel（窄栏里文件名常超出）"""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full_text = text or ""
        # Ignored 宽度策略：省略后的文本不参与布局宽度计算，否则名字短时
        # 标签自己先缩窄，省略结果来回跳（stretch 区由布局分配后 resizeEvent 重算）
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        super().setText(self._full_text)

    def setText(self, text: str):  # noqa: N802 (Qt 命名)
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._update_elide()

    def full_text(self) -> str:
        return self._full_text

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._update_elide()

    def _update_elide(self):
        avail = max(0, self.width() - 2)
        elided = self.fontMetrics().elidedText(self._full_text, Qt.ElideMiddle, avail)
        super().setText(elided)


# ── 行号区 ────────────────────────────────────────────────


class _LineNumberArea(QWidget):
    def __init__(self, editor: "_CodeView"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):  # noqa: N802
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):  # noqa: N802
        self._editor.paint_line_numbers(event)


class _CodeView(QPlainTextEdit):
    """只读代码视图（行号 + 等宽字体 + 语法高亮 + 换行开关，跟随主题）"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFrameShape(QFrame.NoFrame)
        self.setUndoRedoEnabled(False)
        self.setCursorWidth(1)
        self._font_px = 14
        self._highlight_on = True
        self._language = ""
        self._source_path = ""
        self._source_text = ""
        self._highlighter = CodeHighlighter(self.document())
        self._gutter_bg = QColor(255, 255, 255, 16)
        self._gutter_text = QColor(150, 150, 150)
        self._gutter = _LineNumberArea(self)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._on_update_request)
        self._update_gutter_width()

    # ── 主题 ──

    def apply_theme(
        self,
        text_color: QColor,
        gutter_text: QColor,
        gutter_bg: QColor,
        selection: QColor,
        font_pixel_size: int,
        extra_qss: str = "",
        is_dark: bool = True,
    ) -> None:
        self._gutter_text = gutter_text
        self._gutter_bg = gutter_bg
        self._font_px = max(11, int(font_pixel_size))
        self.setFont(_mono_font(self._font_px))
        self._update_gutter_width()
        self._highlighter.set_theme(is_dark)
        self.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  background: transparent; border: none;"
            f"  color: {text_color.name()};"
            f"  selection-background-color: {selection.name(QColor.HexArgb)};"
            f"}}"
            f"{extra_qss}"
        )
        self._gutter.update()

    # ── 行号区几何 ──

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, *_args):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _on_update_request(self, rect: QRect, dy: int):
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def paint_line_numbers(self, event):
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), self._gutter_bg)
        painter.setFont(self.font())
        painter.setPen(self._gutter_text)

        block = self.firstVisibleBlock()
        number = block.blockNumber()
        offset = self.contentOffset()
        top = int(self.blockBoundingGeometry(block).translated(offset).top())
        bottom_limit = event.rect().bottom()
        line_height = self.fontMetrics().height()
        gutter_width = self._gutter.width()

        while block.isValid():
            height = int(self.blockBoundingRect(block).height())
            bottom = top + height
            if top > bottom_limit:
                break
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0,
                    top,
                    gutter_width - 6,
                    line_height,
                    Qt.AlignRight | Qt.AlignTop,
                    str(number + 1),
                )
            top = bottom
            block = block.next()
            number += 1

    # ── 内容 ──

    def set_source(self, path: str, text: str) -> Tuple[str, str]:
        """载入文本并按后缀上色，返回 ``(语言名, 高亮跳过原因)``"""
        self._source_path = path
        self._source_text = text
        self._highlighter.clear()  # 先清旧 spans：setPlainText 会触发 rehighlight
        self.setPlainText(text)
        self.moveCursor(QTextCursor.Start)
        self._update_gutter_width()
        return self._apply_highlight()

    def _apply_highlight(self) -> Tuple[str, str]:
        if not self._highlight_on or not self._source_text:
            self._highlighter.clear()
            return "", "off"
        spans, language, skipped = analyze(self._source_path, self._source_text)
        if spans:
            self._highlighter.set_spans(spans)
        else:
            self._highlighter.clear()
        self._language = language
        return language, skipped

    def set_highlight_enabled(self, enabled: bool) -> Tuple[str, str]:
        self._highlight_on = bool(enabled)
        return self._apply_highlight()

    def highlight_enabled(self) -> bool:
        return self._highlight_on

    def language_name(self) -> str:
        return self._language

    # ── 换行 / 字号 ──

    def set_wrap(self, enabled: bool) -> None:
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if enabled else QPlainTextEdit.NoWrap)

    def wrap_enabled(self) -> bool:
        return self.lineWrapMode() == QPlainTextEdit.WidgetWidth

    def set_font_pixel_size(self, pixel_size: int) -> None:
        self._font_px = max(10, min(32, int(pixel_size)))
        self.setFont(_mono_font(self._font_px))
        self._update_gutter_width()

    def font_pixel_size(self) -> int:
        return self._font_px

    def wheelEvent(self, event):  # noqa: N802 (Qt 命名)
        # Ctrl+滚轮缩放字号：长文件看细节/缩略都常用，和图片视图保持一致的手感
        if event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            steps = event.angleDelta().y() / 120.0
            if steps:
                step = 2 if abs(steps) >= 2 else 1
                self.set_font_pixel_size(self._font_px + (step if steps > 0 else -step))
            event.accept()
            return
        super().wheelEvent(event)


# ── 图片视图 ──────────────────────────────────────────────


class _ImageView(QWidget):
    """自绘图片视图：以「适应窗口」为 1x，支持缩放与平移"""

    MIN_ZOOM = 0.05
    MAX_ZOOM = 20.0

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._zoom = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._drag_origin: Optional[QPoint] = None
        self._drag_start_offset = QPointF(0.0, 0.0)
        self.setCursor(Qt.OpenHandCursor)
        self.setFocusPolicy(Qt.WheelFocus)
        self.setMinimumSize(1, 1)

    # ── 状态 ──

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self.reset_zoom()

    def has_image(self) -> bool:
        return not self._pixmap.isNull()

    @property
    def zoom(self) -> float:
        return self._zoom

    def reset_zoom(self) -> None:
        self._zoom = 1.0
        self._offset = QPointF(0.0, 0.0)
        self.update()

    def zoom_in(self) -> None:
        self._apply_zoom(self._zoom * 1.25, None)

    def zoom_out(self) -> None:
        self._apply_zoom(self._zoom / 1.25, None)

    # ── 缩放数学 ──

    def _fit_scale(self) -> float:
        """适应窗口的缩放比（小图不放大，保持 1:1）"""
        if self._pixmap.isNull():
            return 1.0
        vw = max(1, self.width() - 8)
        vh = max(1, self.height() - 8)
        iw = max(1, self._pixmap.width())
        ih = max(1, self._pixmap.height())
        return min(vw / iw, vh / ih, 1.0)

    def _apply_zoom(self, new_zoom: float, anchor: Optional[QPointF]) -> None:
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, new_zoom))
        if abs(new_zoom - self._zoom) < 1e-6 or self._pixmap.isNull():
            return
        scale_old = self._fit_scale() * self._zoom
        scale_new = self._fit_scale() * new_zoom
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        point = anchor if anchor is not None else QPointF(cx, cy)
        if scale_old > 0:
            # 光标下的图像点相对图像中心的偏移（图像像素），缩放后保持该点不动
            ux = (point.x() - cx - self._offset.x()) / scale_old
            uy = (point.y() - cy - self._offset.y()) / scale_old
        else:
            ux = uy = 0.0
        self._offset = QPointF(
            point.x() - cx - ux * scale_new,
            point.y() - cy - uy * scale_new,
        )
        self._zoom = new_zoom
        self._clamp_offset()
        self.update()

    def _clamp_offset(self) -> None:
        """图像小于视口时锁定居中；大于视口时限制边缘不越界"""
        if self._pixmap.isNull():
            self._offset = QPointF(0.0, 0.0)
            return
        scale = self._fit_scale() * self._zoom
        draw_w = self._pixmap.width() * scale
        draw_h = self._pixmap.height() * scale
        limit_x = max(0.0, (draw_w - self.width()) / 2.0)
        limit_y = max(0.0, (draw_h - self.height()) / 2.0)
        self._offset = QPointF(
            max(-limit_x, min(limit_x, self._offset.x())),
            max(-limit_y, min(limit_y, self._offset.y())),
        )

    # ── 绘制 ──

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        if self._pixmap.isNull():
            return
        scale = self._fit_scale() * self._zoom
        draw_w = self._pixmap.width() * scale
        draw_h = self._pixmap.height() * scale
        cx = self.width() / 2.0 + self._offset.x()
        cy = self.height() / 2.0 + self._offset.y()
        target = QRectF(cx - draw_w / 2.0, cy - draw_h / 2.0, draw_w, draw_h)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))

    # ── 交互 ──

    def wheelEvent(self, event):  # noqa: N802
        if event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            steps = event.angleDelta().y() / 120.0
            if steps:
                self._apply_zoom(self._zoom * (1.2**steps), QPointF(event.pos()))
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and not self._pixmap.isNull():
            self._drag_origin = event.pos()
            self._drag_start_offset = QPointF(self._offset)
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag_origin is not None:
            delta = event.pos() - self._drag_origin
            self._offset = QPointF(
                self._drag_start_offset.x() + delta.x(),
                self._drag_start_offset.y() + delta.y(),
            )
            self._clamp_offset()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self._drag_origin is not None:
            self._drag_origin = None
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.reset_zoom()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._clamp_offset()


# ── 预览页 ────────────────────────────────────────────────


class FilePreviewPage(QWidget):
    """卡片内预览页：顶栏（返回 + 文件名 + 视图开关 + 打开方式）+ 内容区"""

    back_requested = pyqtSignal()
    open_with_requested = pyqtSignal(str)  # 展开程序列表（参数为当前文件路径）
    open_default_requested = pyqtSignal(str)  # 直接用默认程序打开
    context_menu_requested = pyqtSignal(QPoint)  # 代码区右键（全局坐标）

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._path = ""
        self._kind = ""
        self._font_size = 14
        self._text_color = QColor(220, 220, 220)
        self._secondary_color = QColor(150, 150, 150)
        self._accent_color = QColor(80, 150, 220)
        self._is_dark = True
        self._icon_cache: dict = {}
        self._colors_signature = None
        self._setup_ui()

    # ── 公开接口 ──

    def current_path(self) -> str:
        return self._path

    def current_kind(self) -> str:
        return self._kind

    def load(self, path: str) -> bool:
        """载入文件并进入对应视图；返回是否载入成功（失败时显示错误提示页）"""
        self._path = path
        self._kind = detect_kind(path)
        name = os.path.basename(path) or path
        self._name_label.setText(name)
        self._name_label.setToolTip(path)
        self._set_hint("")
        self._set_zoom_visible(self._kind == "image")
        self._set_text_controls_visible(self._kind == "text")

        if self._kind == "image":
            try:
                pixmap, hint = read_image(path)
            except Exception as e:  # noqa: BLE001 — 磁盘/解码异常统一转提示
                self._show_message(f"⚠️ {e}")
                return False
            self._image_view.set_pixmap(pixmap)
            self._content.setCurrentIndex(0)
            self._set_hint(hint)
            self._update_default_opener_icon(path)
            return True
        if self._kind == "text":
            t0 = time.perf_counter()
            try:
                text, hint = read_text(path)
            except Exception as e:  # noqa: BLE001
                self._show_message(f"⚠️ {e}")
                return False
            t_read = time.perf_counter()
            language, skipped = self._code_view.set_source(path, text)
            logger.debug(
                f"[FileTree] 读 {os.path.basename(path)} "
                f"read {(t_read - t0) * 1000:.0f}ms / 高亮 {(time.perf_counter() - t_read) * 1000:.0f}ms"
            )
            self._content.setCurrentIndex(1)
            self._sync_highlight_button(language, skipped)
            self._set_hint(self._compose_hint(hint, skipped))
            self._update_default_opener_icon(path)
            return True
        self._show_message("该文件类型不支持预览，请用默认程序打开")
        return False

    @staticmethod
    def _compose_hint(base: str, skipped: str) -> str:
        """限流提示 + 高亮降级提示合为一条，避免两行小黄条挤占高度"""
        extra = {
            "too_large": "文件较大，已关闭语法高亮",
            "too_many_lines": "行数过多，已关闭语法高亮",
            "failed": "语法高亮失败，按纯文本显示",
        }.get(skipped, "")
        return "；".join(part for part in (base, extra) if part)

    def reload_current(self) -> bool:
        """文件被外部修改后重读（卡片刷新按钮在预览态走这里）"""
        if not self._path or not os.path.isfile(self._path):
            return False
        self.load(self._path)
        return True

    def set_colors(
        self,
        colors: dict,
        font_family: str = "",
        font_size: int = 0,
        scrollbar_qss: str = "",
    ) -> None:
        """应用卡片主题色（字体 / 边框 / 行号 / 选区 / 提示条）"""
        colors = colors or {}
        # 签名去重：主题未变时跳过整套 setStyleSheet（会触发全窗重排与重绘，
        # 动画壁纸主题下每次打开预览都白跑一遍）
        signature = (
            font_family,
            font_size,
            scrollbar_qss,
            tuple(
                (key, value.name() if isinstance(value, QColor) else str(value))
                for key, value in sorted(colors.items())
            ),
        )
        if signature == self._colors_signature:
            return
        self._colors_signature = signature
        family = font_family or colors.get("font_family", "Microsoft YaHei")
        self._font_size = int(font_size or colors.get("font_size", 14))
        text_color = colors.get("text", QColor(220, 220, 220))
        secondary = colors.get("text_secondary", QColor(150, 150, 150))
        accent = colors.get("accent", QColor(80, 150, 220))
        border = colors.get("border", QColor(255, 255, 255, 30))
        is_dark = colors.get("is_dark", True)
        self._text_color = text_color
        self._secondary_color = secondary
        self._accent_color = accent
        self._is_dark = bool(is_dark)

        selection = QColor(accent)
        selection.setAlpha(70)
        gutter_bg = QColor(255, 255, 255, 14) if is_dark else QColor(0, 0, 0, 10)

        # ★ QSS/QColor 的 8 位 hex 是 #AARRGGBB（实测 #ffffff1e → rgb(255,255,30)），
        # 直接 name()+alpha 拼接会把 alpha 写进蓝通道 → 分隔线变黄/洋红亮线。
        # 主题 border 本身不透明，压到 ~35% 才与卡片顶栏观感一致。
        border_css = f"rgba({border.red()},{border.green()},{border.blue()},90)"
        self._header.setStyleSheet(
            f"#file-preview-header {{ background: transparent;"
            f"  border-bottom: 1px solid {border_css}; }}"
        )
        self._name_label.setStyleSheet(
            f"color: {text_color.name()}; background: transparent;"
            f"  font-family: '{family}'; font-size: {self._font_size}px;"
        )
        self._hint.setStyleSheet(
            f"color: {'#ffc861' if is_dark else '#8a6d3b'};"
            f"  background: {'rgba(255,193,7,0.12)' if is_dark else 'rgba(255,193,7,0.18)'};"
            f"  border-radius: 4px; padding: 4px 8px;"
            f"  font-family: '{family}'; font-size: {max(11, self._font_size - 3)}px;"
        )
        self._msg_label.setStyleSheet(
            f"color: {secondary.name()}; background: transparent;"
            f"  font-family: '{family}'; font-size: {self._font_size}px;"
        )
        self._code_view.apply_theme(
            text_color=text_color,
            gutter_text=secondary,
            gutter_bg=gutter_bg,
            selection=selection,
            font_pixel_size=max(11, self._font_size - 1),
            extra_qss=scrollbar_qss,
            is_dark=is_dark,
        )
        self._refresh_control_icons()

    def _refresh_control_icons(self) -> None:
        """顶栏按钮图标按主题着色；开关类按钮用强调色表示「已开启」

        ★ 图标按（资源, 颜色）缓存：FluentIcon.icon(color=) 每次都重新渲染 SVG，
        在动画主题（每帧全窗重绘）下反复重建是纯浪费。
        """
        accent = self._accent_color
        inactive = self._secondary_color
        try:
            self._wrap_btn.setIcon(
                self._cached_wrap_icon(accent if self._code_view.wrap_enabled() else inactive)
            )
        except Exception:
            pass
        try:
            self._highlight_btn.setIcon(
                self._tool_icon(FluentIcon.CODE, accent if self._code_view.highlight_enabled() else inactive)
            )
            self._reload_btn.setIcon(self._tool_icon(FluentIcon.SYNC, inactive))
            hover_bg = QColor(255, 255, 255, 26) if self._is_dark else QColor(0, 0, 0, 18)
            self._open_with_btn.set_colors(inactive, hover_bg)
        except Exception:
            pass

    def _tool_icon(self, icon: FluentIcon, color: QColor) -> QIcon:
        """FluentIcon → 预渲染 pixmap（按当前 DPR），并缓存

        直接给按钮 QIcon 的话，每次重绘都会请求 pixmap，动画壁纸主题下
        就是每帧一次图标引擎调用；转成 pixmap 后重绘只是 blit。
        """
        dpr = self.devicePixelRatioF() or 1.0
        key = (str(getattr(icon, "name", icon)), color.name(), round(dpr, 2))
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached
        base = icon.icon(color=color)
        size = max(1, int(round(20 * dpr)))
        pixmap = base.pixmap(QSize(size, size))
        if pixmap.isNull():
            cached = base
        else:
            pixmap.setDevicePixelRatio(dpr)
            cached = QIcon(pixmap)
        self._icon_cache[key] = cached
        return cached

    def _cached_wrap_icon(self, color: QColor) -> QIcon:
        dpr = self.devicePixelRatioF() or 1.0
        key = ("wrap", color.name(), round(dpr, 2))
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached
        icon = _wrap_icon(color)
        pixmap = icon.pixmap(QSize(max(1, int(round(20 * dpr))), max(1, int(round(20 * dpr)))))
        if not pixmap.isNull():
            pixmap.setDevicePixelRatio(dpr)
            icon = QIcon(pixmap)
        self._icon_cache[key] = icon
        return icon

    # ── UI ──

    def _setup_ui(self) -> None:
        self.setObjectName("file-preview-page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QFrame(self)
        self._header.setObjectName("file-preview-header")
        self._header.setFixedHeight(38)
        head = QHBoxLayout(self._header)
        head.setContentsMargins(6, 0, 6, 0)
        head.setSpacing(2)

        self._back_btn = TransparentToolButton(FluentIcon.LEFT_ARROW, self._header)
        self._back_btn.setFixedSize(28, 28)
        self._back_btn.setToolTip("返回文件树")
        self._back_btn.clicked.connect(self.back_requested.emit)

        self._name_label = _ElidedLabel("", self._header)
        self._name_label.setMinimumWidth(36)

        self._zoom_out_btn = TransparentToolButton(FluentIcon.ZOOM_OUT, self._header)
        self._zoom_out_btn.setFixedSize(26, 26)
        self._zoom_out_btn.setToolTip("缩小（Ctrl+滚轮）")
        self._zoom_out_btn.clicked.connect(lambda: self._image_view.zoom_out())

        self._zoom_in_btn = TransparentToolButton(FluentIcon.ZOOM_IN, self._header)
        self._zoom_in_btn.setFixedSize(26, 26)
        self._zoom_in_btn.setToolTip("放大（Ctrl+滚轮）")
        self._zoom_in_btn.clicked.connect(lambda: self._image_view.zoom_in())

        self._fit_btn = TransparentToolButton(FluentIcon.FIT_PAGE, self._header)
        self._fit_btn.setFixedSize(26, 26)
        self._fit_btn.setToolTip("适应窗口（双击图片）")
        self._fit_btn.clicked.connect(lambda: self._image_view.reset_zoom())

        # ── 文本视图控件（图片态隐藏）──
        self._wrap_btn = TransparentToolButton(self._header)
        self._wrap_btn.setFixedSize(26, 26)
        self._wrap_btn.setIcon(_wrap_icon(QColor(150, 150, 150)))
        self._wrap_btn.setToolTip("自动换行：关（点击切换）")
        self._wrap_btn.clicked.connect(self._on_toggle_wrap)

        self._highlight_btn = TransparentToolButton(FluentIcon.CODE, self._header)
        self._highlight_btn.setFixedSize(26, 26)
        self._highlight_btn.setToolTip("语法高亮：开（点击切换）")
        self._highlight_btn.clicked.connect(self._on_toggle_highlight)

        # ── 两种视图共用 ──
        self._reload_btn = TransparentToolButton(FluentIcon.SYNC, self._header)
        self._reload_btn.setFixedSize(26, 26)
        self._reload_btn.setToolTip("重新加载（文件被外部修改后重读）")
        self._reload_btn.clicked.connect(self.reload_current)

        self._open_with_btn = _OpenWithButton(self._header)
        self._open_with_btn.setToolTip("打开方式")
        self._open_with_btn.default_clicked.connect(self._emit_open_default)
        self._open_with_btn.menu_clicked.connect(self._emit_open_with)

        head.addWidget(self._back_btn)
        head.addWidget(self._name_label, 1)
        head.addWidget(self._wrap_btn)
        head.addWidget(self._highlight_btn)
        head.addWidget(self._zoom_out_btn)
        head.addWidget(self._zoom_in_btn)
        head.addWidget(self._fit_btn)
        head.addWidget(self._reload_btn)
        head.addWidget(self._open_with_btn)

        self._hint = QLabel("", self)
        self._hint.setWordWrap(True)
        self._hint.setContentsMargins(10, 4, 10, 4)
        self._hint.setVisible(False)

        self._image_view = _ImageView(self)
        self._code_view = _CodeView(self)
        # 右键菜单必须接管：QPlainTextEdit 自带的是原生黑菜单，与卡片主题两张皮
        self._code_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._code_view.customContextMenuRequested.connect(self._emit_context_menu)

        self._msg_label = QLabel("", self)
        self._msg_label.setAlignment(Qt.AlignCenter)
        self._msg_label.setWordWrap(True)
        self._msg_page = QWidget(self)
        msg_layout = QVBoxLayout(self._msg_page)
        msg_layout.setContentsMargins(16, 16, 16, 16)
        msg_layout.addWidget(self._msg_label)

        self._content = QStackedWidget(self)
        self._content.addWidget(self._image_view)  # 0: 图片
        self._content.addWidget(self._code_view)  # 1: 文本
        self._content.addWidget(self._msg_page)  # 2: 提示

        layout.addWidget(self._header)
        layout.addWidget(self._hint)
        layout.addWidget(self._content, 1)

        self._set_zoom_visible(False)
        self._set_text_controls_visible(False)
        self._refresh_control_icons()

    def _set_zoom_visible(self, visible: bool) -> None:
        for btn in (self._zoom_out_btn, self._zoom_in_btn, self._fit_btn):
            btn.setVisible(visible)

    def _set_text_controls_visible(self, visible: bool) -> None:
        self._wrap_btn.setVisible(visible)
        self._highlight_btn.setVisible(visible)

    def _emit_open_with(self) -> None:
        if self._path:
            self.open_with_requested.emit(self._path)

    def _emit_open_default(self) -> None:
        if self._path:
            self.open_default_requested.emit(self._path)

    def _emit_context_menu(self, pos: QPoint) -> None:
        self.context_menu_requested.emit(self._code_view.viewport().mapToGlobal(pos))

    # ── 打开方式（左侧：默认程序图标） ──

    def configure_open_with(self, icon: QIcon, label: str) -> None:
        """设左半边的默认程序图标与悬停提示（图标缺失时退回通用应用图标）"""
        if icon is None or icon.isNull():
            icon = self._tool_icon(FluentIcon.APPLICATION, self._secondary_color)
        self._open_with_btn.set_icon(icon)
        self._open_with_btn.setToolTip(f"用 {label} 打开" if label else "用默认程序打开")

    def open_with_anchor(self) -> QPoint:
        """分裂按钮左下角的全局坐标（菜单右对齐到这里）"""
        return self._open_with_btn.mapToGlobal(QPoint(0, self._open_with_btn.height()))

    def _update_default_opener_icon(self, path: str) -> None:
        """按当前文件的系统关联刷新左半边图标

        ★ 由预览页自己负责而不是等外部调用：载入流程内部完成，「什么时候
        显示了文件却没有默认程序图标」这类时序问题直接消失。
        """
        opener = None
        try:
            from .openwith import list_openers

            openers = list_openers(path)
        except Exception:
            openers = []
        for candidate in openers:
            if candidate.is_default:
                opener = candidate
                break
        if opener is None and openers:
            opener = openers[0]
        icon = QIcon()
        if opener is not None:
            try:
                from .openwith import icon_for_exe

                icon = icon_for_exe(opener.exe)
            except Exception:
                icon = QIcon()
        self.configure_open_with(icon, opener.label if opener else "")

    # ── 代码区能力（供右键菜单调用） ──

    def code_view(self) -> "_CodeView":
        return self._code_view

    def copy_selection(self) -> None:
        self._code_view.copy()

    def select_all_text(self) -> None:
        self._code_view.selectAll()

    def has_selection(self) -> bool:
        cursor = self._code_view.textCursor()
        return cursor is not None and cursor.hasSelection()

    def toggle_wrap(self) -> None:
        self._on_toggle_wrap()

    def toggle_highlight(self) -> None:
        self._on_toggle_highlight()

    def wrap_enabled(self) -> bool:
        return self._code_view.wrap_enabled()

    def highlight_enabled(self) -> bool:
        return self._code_view.highlight_enabled()

    # ── 视图开关 ──

    def _on_toggle_wrap(self) -> None:
        self._code_view.set_wrap(not self._code_view.wrap_enabled())
        on = self._code_view.wrap_enabled()
        self._wrap_btn.setToolTip(f"自动换行：{'开' if on else '关'}（点击切换）")
        self._refresh_control_icons()

    def _on_toggle_highlight(self) -> None:
        enabled = not self._code_view.highlight_enabled()
        language, skipped = self._code_view.set_highlight_enabled(enabled)
        self._sync_highlight_button(language, skipped)
        self._refresh_control_icons()

    def _sync_highlight_button(self, language: str, skipped: str) -> None:
        """高亮按钮的可用性与悬停说明：无词法器 / 被限流时置灰"""
        if not self._code_view.highlight_enabled():
            # 用户手动关闭：保持可点（用于重新开启）
            self._highlight_btn.setEnabled(True)
            self._highlight_btn.setToolTip("语法高亮：关（点击开启）")
            return
        reasons = {
            "unknown": "该文件类型无语法高亮",
            "too_large": "文件过大，已关闭语法高亮",
            "too_many_lines": "行数过多，已关闭语法高亮",
            "failed": "语法高亮失败",
        }
        if skipped in reasons:
            self._highlight_btn.setEnabled(False)
            self._highlight_btn.setToolTip(reasons[skipped])
            return
        self._highlight_btn.setEnabled(True)
        self._highlight_btn.setToolTip(f"语法高亮：{language or '开'}（点击关闭）")

    def _set_hint(self, text: str) -> None:
        self._hint.setText(text or "")
        self._hint.setVisible(bool(text))

    def _show_message(self, text: str) -> None:
        self._msg_label.setText(text)
        self._content.setCurrentIndex(2)
