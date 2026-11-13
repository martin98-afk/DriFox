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

from loguru import logger
from PyQt5.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QImageReader,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QTextCursor,
    QTextDocument,
)
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
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


# markdown：渲染态 + 三态循环（渲染 / 源码高亮 / 源码纯文本）
_MD_EXTS = {".md", ".markdown"}
_MD_CYCLE = {"render": "source_hl", "source_hl": "source_plain", "source_plain": "render"}

# 预览页 _content 页索引
_PAGE_TEXT = 1
_PAGE_MD = 3

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
    """判定预览类型：``"image"`` / ``"markdown"`` / ``"text"`` / ``""``（不支持）"""
    try:
        if not path or not os.path.isfile(path):
            return ""
    except OSError:
        return ""
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _MD_EXTS:
        return "markdown"
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


class _SplitButton(QWidget):
    """「默认程序图标 + 下拉箭头」分裂按钮（两个独立按钮拼成）

    ★ 不用 QToolButton 的 MenuButtonPopup：``QToolButton::showMenu`` 不是虚函数，
    C++ 点箭头时直接调自身实现去弹内置空菜单，Python 重写拦不住（表现为点了没反应），
    且它自绘的 menu indicator 在透明背景上就是一块黑方块。
    两个 TransparentToolButton 拼装后，行为全在 Python 侧，hover 也由组件自带。
    """

    default_clicked = pyqtSignal()
    menu_clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(46, 26)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setMouseTracking(True)
        self._hover_bg = QColor(255, 255, 255, 18)
        self._hover = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 子按钮只负责点与图标，hover 底色统一由本控件画（整块亮，不碎成两块）
        plain_qss = (
            "QToolButton { background: transparent; border: none; }"
            "QToolButton:hover { background: transparent; }"
            "QToolButton:pressed { background: transparent; }"
        )
        self._icon_btn = QToolButton(self)
        self._icon_btn.setAutoRaise(True)
        self._icon_btn.setStyleSheet(plain_qss)
        self._icon_btn.setFixedSize(28, 26)
        self._icon_btn.setIconSize(QSize(16, 16))
        self._icon_btn.setCursor(Qt.PointingHandCursor)
        self._icon_btn.clicked.connect(self.default_clicked.emit)
        self._icon_btn.setIcon(FluentIcon.APPLICATION.icon())

        self._arrow_btn = QToolButton(self)
        self._arrow_btn.setAutoRaise(True)
        self._arrow_btn.setStyleSheet(plain_qss)
        self._arrow_btn.setFixedSize(18, 26)
        self._arrow_btn.setIconSize(QSize(10, 10))
        self._arrow_btn.setCursor(Qt.PointingHandCursor)
        self._arrow_btn.setIcon(FluentIcon.CARE_DOWN_SOLID.icon())
        self._arrow_btn.clicked.connect(self.menu_clicked.emit)

        layout.addWidget(self._icon_btn)
        layout.addWidget(self._arrow_btn)

    # ── 外观 ──

    def set_hover_color(self, color: QColor) -> None:
        self._hover_bg = QColor(color)
        self.update()

    def paintEvent(self, event):  # noqa: N802 (Qt 命名)
        if not self._hover:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._hover_bg)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 5, 5)

    def enterEvent(self, event):  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        # 鼠标移到子按钮上时也会收到 leave，用全局位置复核是否还在本控件内
        rect = self.rect()
        inside = rect.contains(self.mapFromGlobal(self.cursor().pos()))
        self._hover = inside
        self.update()
        super().leaveEvent(event)

    def set_icon(self, icon: QIcon) -> None:
        self._icon_btn.setIcon(icon)

    def icon(self) -> QIcon:
        return self._icon_btn.icon()

    def set_tooltip(self, text: str) -> None:
        self._icon_btn.setToolTip(text)
        self._arrow_btn.setToolTip(text)

    def set_icon_color(self, color: QColor) -> None:
        self._arrow_btn.setIcon(FluentIcon.CARE_DOWN_SOLID.icon(color=color))


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

    context_menu_requested = pyqtSignal(QPoint)  # 参数为全局坐标
    highlight_ready = pyqtSignal(str, str)  # (语言名, 跳过原因) — 异步上色完成

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFrameShape(QFrame.NoFrame)
        self.setUndoRedoEnabled(False)
        self.setCursorWidth(1)
        # 接管右键：QPlainTextEdit 自带的菜单是原生英文黑菜单，与卡片两张皮
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
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

    # ── 右键菜单 ──

    def _on_context_menu(self, pos: QPoint) -> None:
        self.context_menu_requested.emit(self.viewport().mapToGlobal(pos))

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
        """载入文本：先出字，语法高亮延后一拍（词法分析是同步 CPU 活）

        实测 1800 行 Python：读取 0.5ms、pygments 176ms、格式化重绘 150ms。
        串行做会让点开文件有 300ms+ 白屏，先 setPlainText 让内容立刻可读，
        高亮补上后重绘一次，代价远小于首帧延迟。
        """
        self._source_path = path
        self._source_text = text
        self._highlighter.clear()  # 先清旧 spans：setPlainText 会触发 rehighlight
        self.setPlainText(text)
        self.moveCursor(QTextCursor.Start)
        self._update_gutter_width()
        if self._highlight_on and text:
            QTimer.singleShot(0, self._run_highlight_async)
        else:
            self._language = ""
        return "", "" if self._highlight_on else "off"

    def _run_highlight_async(self) -> None:
        """下一拍事件循环里做词法分析（不堵首帧）"""
        text = self._source_text
        if not text or not self._highlight_on:
            return
        path = self._source_path
        spans, language, skipped = analyze(path, text)
        if text is not self._source_text or path != self._source_path:
            return  # 期间换了文件，旧结果直接丢弃
        if spans:
            self._highlighter.set_spans(spans)
        else:
            self._highlighter.clear()
        self._language = language
        self.highlight_ready.emit(language, skipped)

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
    open_default_requested = pyqtSignal(str)  # 用系统默认程序直接打开
    context_menu_requested = pyqtSignal(QPoint)  # 代码区右键（全局坐标）

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._path = ""
        self._kind = ""
        self._md_mode = "render"  # markdown 三态：render / source_hl / source_plain
        self._md_text = ""
        self._md_skipped = ""
        self._md_source_loaded = False
        self._md_render_key: Tuple = ()
        self._md_token = 0
        self._font_size = 14
        self._text_color = QColor(220, 220, 220)
        self._secondary_color = QColor(150, 150, 150)
        self._accent_color = QColor(80, 150, 220)
        self._is_dark = True
        self._has_opener_icon = False
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
        self._md_mode = "render"
        self._md_text = ""
        self._md_skipped = ""
        self._md_source_loaded = False
        text_like = self._kind in ("text", "markdown")
        name = os.path.basename(path) or path
        self._name_label.setText(name)
        self._name_label.setToolTip(path)
        self._set_hint("")
        self._set_zoom_visible(self._kind == "image")
        self._set_text_controls_visible(text_like)
        # 诊断：右上角按钮消失问题取证（定位后移除）
        logger.info(
            f"[FilePreview] load {name!r} kind={self._kind} header_w={self._header.width()} "
            f"wrap={self._wrap_btn.isVisible()} hl={self._highlight_btn.isVisible()} "
            f"reload={self._reload_btn.isVisible()} open_with={self._open_with_btn.isVisible()}"
        )
        # 默认程序图标延迟到下一拍：读注册表 + 取 exe 图标不能堵在首帧渲染上
        QTimer.singleShot(0, lambda p=path: self._refresh_opener_icon(p))

        if self._kind == "image":
            try:
                pixmap, hint = read_image(path)
            except Exception as e:  # noqa: BLE001 — 磁盘/解码异常统一转提示
                self._show_message(f"⚠️ {e}")
                return False
            self._image_view.set_pixmap(pixmap)
            self._content.setCurrentIndex(0)
            self._set_hint(hint)
            return True
        if self._kind in ("text", "markdown"):
            try:
                text, hint = read_text(path)
            except Exception as e:  # noqa: BLE001
                self._show_message(f"⚠️ {e}")
                return False
            self._base_hint = hint
            if self._kind == "markdown":
                # 渲染放下一拍：布局 18k 字符的文档要 ~120ms，先让点击有响应。
                # 源码态内容懒加载（切到源码才跑语法分析）。
                self._md_text = text
                self._content.setCurrentIndex(_PAGE_MD)
                self._wrap_btn.setVisible(False)
                self._md_view.setDocument(QTextDocument(self._md_view))
                self._refresh_control_icons()
                self._md_token += 1
                QTimer.singleShot(0, lambda t=self._md_token: self._render_md_async(t))
                return True
            language, skipped = self._code_view.set_source(path, text)
            self._content.setCurrentIndex(_PAGE_TEXT)
            self._sync_highlight_button(language, skipped)
            self._set_hint(self._compose_hint(hint, skipped))
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

    # ── 打开方式（默认程序图标 + 下拉） ──

    def open_with_anchor(self) -> QPoint:
        """分裂按钮右下角的全局坐标（菜单以此右对齐，不会向右溢出窗口）"""
        btn = self._open_with_btn
        return btn.mapToGlobal(QPoint(btn.width(), btn.height()))

    def _refresh_opener_icon(self, path: str) -> None:
        """按系统关联刷新左半边的默认程序图标（载入后延迟一拍调）"""
        if not path or path != self._path:
            return
        try:
            from .openwith import icon_for_exe, list_openers

            openers = list_openers(path)
        except Exception:
            openers = []
        opener = next((o for o in openers if o.is_default), None)
        if opener is None:
            self._has_opener_icon = False
            self._open_with_btn.set_icon(
                FluentIcon.APPLICATION.icon(color=self._secondary_color)
            )
            self._open_with_btn.set_tooltip("用默认程序打开")
            return
        icon = icon_for_exe(opener.exe)
        if icon.isNull():
            self._has_opener_icon = False
            self._open_with_btn.set_icon(
                FluentIcon.APPLICATION.icon(color=self._secondary_color)
            )
        else:
            self._has_opener_icon = True
            self._open_with_btn.set_icon(icon)
        self._open_with_btn.set_tooltip(f"用 {opener.label} 打开")

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
        # 分裂按钮整块 hover 底色：深色下用白 10%，亮色下用黑 7%
        self._open_with_btn.set_hover_color(
            QColor(255, 255, 255, 26) if is_dark else QColor(0, 0, 0, 18)
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
        # markdown 渲染视图：字体/颜色跟主题；文档内联字体是 setMarkdown 时固化的，
        # 主题切换需要重渲染才算真正生效
        self._md_view.setStyleSheet(
            f"QTextBrowser {{ background: transparent; border: none;"
            f"  color: {text_color.name()};"
            f"  font-family: '{family}'; font-size: {self._font_size}px; }}"
            f"{scrollbar_qss}"
        )
        md_palette = self._md_view.palette()
        md_palette.setColor(QPalette.Text, text_color)
        md_palette.setColor(QPalette.WindowText, text_color)
        self._md_view.setPalette(md_palette)
        md_font = QFont(family, self._font_size)
        self._md_view.setFont(md_font)
        self._md_view.document().setDefaultFont(md_font)
        # 只有主题/字体真的变了才重渲染：打开新预览时 set_colors 也会走这里，
        # 不加这道判据会把上一个文件白渲染一遍（实测 240ms）
        if self._kind == "markdown" and self._md_text and self._md_render_key != self._theme_key():
            self._render_markdown(self._path, self._md_text)
        self._refresh_control_icons()

    def _refresh_control_icons(self) -> None:
        """顶栏按钮图标按主题着色；开关类按钮用强调色表示「已开启」"""
        accent = self._accent_color
        inactive = self._secondary_color
        try:
            self._wrap_btn.setIcon(_wrap_icon(accent if self._code_view.wrap_enabled() else inactive))
        except Exception:
            pass
        try:
            if self._kind == "markdown":
                mode = self._md_mode
                icon = FluentIcon.VIEW if mode == "render" else FluentIcon.CODE
                self._highlight_btn.setIcon(icon.icon(color=inactive if mode == "source_plain" else accent))
                self._highlight_btn.setToolTip(
                    {
                        "render": "Markdown 渲染（点击看源码）",
                        "source_hl": "源码 · 高亮开（点击切纯文本）",
                        "source_plain": "源码 · 纯文本（点击回渲染）",
                    }[mode]
                )
                self._highlight_btn.setEnabled(True)
            else:
                self._highlight_btn.setIcon(
                    FluentIcon.CODE.icon(color=accent if self._code_view.highlight_enabled() else inactive)
                )
            self._reload_btn.setIcon(FluentIcon.SYNC.icon(color=inactive))
            self._open_with_btn.set_icon_color(inactive)
            if not self._has_opener_icon:
                # 已取到默认程序图标时不能被主题刷新回通用图标
                self._open_with_btn.set_icon(FluentIcon.APPLICATION.icon(color=inactive))
        except Exception:
            pass

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
        self._highlight_btn.clicked.connect(self._on_code_button)

        # ── 两种视图共用 ──
        self._reload_btn = TransparentToolButton(FluentIcon.SYNC, self._header)
        self._reload_btn.setFixedSize(26, 26)
        self._reload_btn.setToolTip("重新加载（文件被外部修改后重读）")
        self._reload_btn.clicked.connect(self.reload_current)

        self._open_with_btn = _SplitButton(self._header)
        self._open_with_btn.set_tooltip("用默认程序打开")
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
        self._code_view.context_menu_requested.connect(self.context_menu_requested.emit)
        self._code_view.highlight_ready.connect(self._on_highlight_ready)

        # markdown 渲染视图：QTextBrowser（只读、无边框、背景交给主题）
        self._md_view = QTextBrowser(self)
        self._md_view.setObjectName("file-preview-md")
        self._md_view.setFrameShape(QFrame.NoFrame)
        self._md_view.setOpenExternalLinks(True)
        self._md_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._md_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

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
        self._content.addWidget(self._md_view)  # 3: markdown 渲染

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

    # ── 文本操作（右键菜单复用） ──

    def has_selection(self) -> bool:
        return self._code_view.textCursor().hasSelection()

    def copy_selection(self) -> None:
        self._code_view.copy()

    def select_all_text(self) -> None:
        self._code_view.selectAll()

    def toggle_wrap(self) -> None:
        self._on_toggle_wrap()

    def toggle_highlight(self) -> None:
        self._on_code_button()

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

    # ── markdown 三态 ──

    def _on_code_button(self) -> None:
        """顶栏 CODE 按钮：markdown 循环三态，其余文件仍是高亮开关"""
        if self._kind == "markdown":
            self._md_mode = _MD_CYCLE[self._md_mode]
            self._apply_md_mode()
            return
        self._on_toggle_highlight()

    def _apply_md_mode(self) -> None:
        """按三态切换内容页：渲染 / 源码高亮 / 源码纯文本"""
        if self._kind != "markdown":
            return
        if self._md_mode == "render":
            self._content.setCurrentIndex(_PAGE_MD)
            self._wrap_btn.setVisible(False)
            self._set_hint(getattr(self, "_base_hint", ""))
        else:
            # 源码内容懒加载：渲染态不预先跑语法分析（打开 md 常是看文档）
            if not self._md_source_loaded:
                language, skipped = self._code_view.set_source(self._path, self._md_text)
                self._md_skipped = skipped
                self._md_source_loaded = True
            self._code_view.set_highlight_enabled(self._md_mode == "source_hl")
            self._content.setCurrentIndex(_PAGE_TEXT)
            self._wrap_btn.setVisible(True)
            self._wrap_btn.setToolTip(
                f"自动换行：{'开' if self._code_view.wrap_enabled() else '关'}（点击切换）"
            )
            self._set_hint(self._compose_hint(getattr(self, "_base_hint", ""), self._md_skipped))
        self._refresh_control_icons()

    def _render_md_async(self, token: int) -> None:
        """下一拍执行的实际渲染（token 防快速切文件时旧任务覆盖新内容）"""
        if token != self._md_token or self._kind != "markdown" or not self._md_text:
            return
        self._render_markdown(self._path, self._md_text)
        self._apply_md_mode()

    def _theme_key(self) -> Tuple:
        """影响渲染产出外观的主题要素（判断是否需要重渲染）"""
        return (
            bool(self._is_dark),
            self._text_color.name(),
            self._font_size,
            self._md_view.font().family(),
        )

    def _render_markdown(self, path: str, text: str) -> None:
        """setMarkdown 渲染，并补 Qt 不生成的代码块/引用底色

        ★ 先在未挂到视图上的 QTextDocument 里渲染完、改完块格式，最后
        setDocument 一次挂载。直接在 QTextBrowser.document() 上逐块
        setBlockFormat，每次修改都会触发一次重排（实测 424 块的 30KB
        文档 3.7ms → 243ms），差了两个量级。
        """
        self._md_text = text
        doc = QTextDocument(self._md_view)
        doc.setDefaultFont(self._md_view.font())
        # 相对路径图片靠 baseUrl 解析（QTextBrowser 的资源查找起点）
        doc.setBaseUrl(QUrl.fromLocalFile(os.path.dirname(path) + os.sep))
        doc.setMarkdown(text)
        self._apply_md_block_style(doc)
        self._md_view.setDocument(doc)
        self._md_render_key = self._theme_key()

    def _apply_md_block_style(self, doc: QTextDocument) -> None:
        """给等宽块（代码）与缩进块（引用）加底色

        QTextMarkdownImporter 不生成 ``<pre>`` / ``<blockquote>``：代码块只有
        Courier 字体，引用只有左右 40px 缩进，深色主题下与正文完全糊在一起。
        QTextBlockFormat 不支持边框，因此只加底色（代码块额外缩进一档）。
        """
        code_bg = QColor(255, 255, 255, 20) if self._is_dark else QColor(0, 0, 0, 14)
        quote_bg = QColor(255, 255, 255, 12) if self._is_dark else QColor(0, 0, 0, 8)
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        block = doc.begin()
        while block.isValid():
            fmt = block.blockFormat()
            if self._block_is_code(block):
                fmt.setBackground(QBrush(code_bg))
                if int(fmt.leftMargin()) < 8:
                    fmt.setLeftMargin(8)
                    fmt.setRightMargin(max(8, int(fmt.rightMargin())))
                cursor.setPosition(block.position())
                cursor.setBlockFormat(fmt)
            elif int(fmt.leftMargin()) >= 30:
                fmt.setBackground(QBrush(quote_bg))
                cursor.setPosition(block.position())
                cursor.setBlockFormat(fmt)
            block = block.next()
        cursor.endEditBlock()

    @staticmethod
    def _block_is_code(block) -> bool:
        """块内全部非空 fragment 都是等宽字体 → 代码块"""
        seen = False
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.text().strip():
                seen = True
                family = (frag.charFormat().fontFamily() or "").lower()
                if "courier" not in family and "mono" not in family:
                    return False
            it += 1
        return seen

    def _on_highlight_ready(self, language: str, skipped: str) -> None:
        """异步上色完成：补上语言名与降级提示（markdown 渲染态不抢提示位）"""
        if self._kind == "markdown":
            self._md_skipped = skipped
            if self._md_mode != "render":
                self._set_hint(self._compose_hint(getattr(self, "_base_hint", ""), skipped))
            return
        self._sync_highlight_button(language, skipped)
        self._set_hint(self._compose_hint(getattr(self, "_base_hint", ""), skipped))

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
