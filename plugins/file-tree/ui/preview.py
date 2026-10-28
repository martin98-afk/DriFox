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
from typing import Optional, Tuple

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImageReader, QPainter, QPixmap, QTextCursor
from PySide6.QtWidgets import (
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
    """只读代码视图（行号 + 等宽字体 + 跟随主题）"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFrameShape(QFrame.NoFrame)
        self.setUndoRedoEnabled(False)
        self.setCursorWidth(1)
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
    ) -> None:
        self._gutter_text = gutter_text
        self._gutter_bg = gutter_bg
        self.setFont(_mono_font(font_pixel_size))
        self._update_gutter_width()
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

    def set_content(self, text: str) -> None:
        self.setPlainText(text)
        self.moveCursor(QTextCursor.Start)
        self._update_gutter_width()


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
    """卡片内预览页：顶栏（返回 + 文件名 + 图片缩放键）+ 内容区"""

    back_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._path = ""
        self._kind = ""
        self._font_size = 14
        self._setup_ui()

    # ── 公开接口 ──

    def current_path(self) -> str:
        return self._path

    def current_kind(self) -> str:
        return self._kind

    def load(self, path: str) -> None:
        """载入文件并进入对应视图；读取失败时显示错误提示页"""
        self._path = path
        self._kind = detect_kind(path)
        name = os.path.basename(path) or path
        self._name_label.setText(name)
        self._set_hint("")
        self._set_zoom_visible(self._kind == "image")

        if self._kind == "image":
            try:
                pixmap, hint = read_image(path)
            except Exception as e:  # noqa: BLE001 — 磁盘/解码异常统一转提示
                self._show_message(f"⚠️ {e}")
                return
            self._image_view.set_pixmap(pixmap)
            self._content.setCurrentIndex(0)
            self._set_hint(hint)
        elif self._kind == "text":
            try:
                text, hint = read_text(path)
            except Exception as e:  # noqa: BLE001
                self._show_message(f"⚠️ {e}")
                return
            self._code_view.set_content(text)
            self._content.setCurrentIndex(1)
            self._set_hint(hint)
        else:
            self._show_message("该文件类型不支持预览，请用默认程序打开")

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
        family = font_family or colors.get("font_family", "Microsoft YaHei")
        self._font_size = int(font_size or colors.get("font_size", 14))
        text_color = colors.get("text", QColor(220, 220, 220))
        secondary = colors.get("text_secondary", QColor(150, 150, 150))
        accent = colors.get("accent", QColor(80, 150, 220))
        border = colors.get("border", QColor(255, 255, 255, 30))
        is_dark = colors.get("is_dark", True)

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
        )

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

        head.addWidget(self._back_btn)
        head.addWidget(self._name_label, 1)
        head.addWidget(self._zoom_out_btn)
        head.addWidget(self._zoom_in_btn)
        head.addWidget(self._fit_btn)

        self._hint = QLabel("", self)
        self._hint.setWordWrap(True)
        self._hint.setContentsMargins(10, 4, 10, 4)
        self._hint.setVisible(False)

        self._image_view = _ImageView(self)
        self._code_view = _CodeView(self)

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

    def _set_zoom_visible(self, visible: bool) -> None:
        for btn in (self._zoom_out_btn, self._zoom_in_btn, self._fit_btn):
            btn.setVisible(visible)

    def _set_hint(self, text: str) -> None:
        self._hint.setText(text or "")
        self._hint.setVisible(bool(text))

    def _show_message(self, text: str) -> None:
        self._msg_label.setText(text)
        self._content.setCurrentIndex(2)
