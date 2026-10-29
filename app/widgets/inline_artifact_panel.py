# -*- coding: utf-8 -*-
"""卡片内嵌成果栏（InlineArtifactPanel）

展示工具/插件推送的成果条目（生成图片、视频、HTML、文本、任意文件），挂在
assistant 气泡布局末尾（任务看板之后、页脚之前）。与 ``InlineTodoPanel`` 同构：

1. **默认折叠**：卡片空间宝贵，常态只占「分隔条 + 统计」高度。
2. **展开全量**：列表不限高、不内滚（QScrollArea sizeHint 缓存会让宿主高度链
   拿到滞后值，见 InlineTodoPanel 同款注释）。
3. **无成果整区隐藏**：零高度，不占布局。

与任务看板的关键语义差异——**不迁卡**：任务列表是"当前状态"，每轮工具都会
迁到最新卡；成果属于产生它的那一轮，新卡不继承旧卡的成果。宿主侧
``_sync_artifact_panel`` 不做旧卡卸载。

五类条目分控件渲染（spec: 2026-10-24-message-artifacts-panel-design）：

=====  ========  ==============================
kind   预览      交互
=====  ========  ==============================
image  真缩略图  单击内置预览；右键菜单
video  占位封面  双击系统播放器；右键菜单
html   徽标+名   双击浏览器；右键菜单
text   徽标+名   双击系统程序；右键菜单
file   徽标+名   双击系统程序；右键菜单
=====  ========  ==============================

缩略图懒解码：折叠态零解码开销；展开时经 ``QTimer`` 分帧解码（每拍一张），
4K PNG 全量解码约百毫秒级，一帧一张不会卡死 UI 线程。
"""

import os
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QImageReader, QMouseEvent, QPainter, QPixmap
from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget
from qfluentwidgets import TransparentToolButton

from app.utils.design_tokens import BorderRadius, Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon
from app.widgets._workbench_helpers import _SectionHeader
from app.widgets.elided_label import _ElidedLabel

# 单卡最多渲染条数：插件一次性推几百个文件时把卡片撑爆（上限外折叠进「还有 N 个」）
MAX_VISIBLE_ITEMS = 20
# 缩略图预览块尺寸
_THUMB_W, _THUMB_H = 64, 48

# kind → (徽标字, 主色)；主色同时用于徽标底与 meta 强调
_KIND_META = {
    "image": ("图", "#3b82f6"),
    "video": ("视", "#8b5cf6"),
    "html": ("H", "#f59e0b"),
    "text": ("T", "#10b981"),
    "file": ("文", "#6b7280"),
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
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


class _ThumbLabel(QLabel):
    """视频占位封面：圆角深色块 + 播放三角 + 可选时长徽标（不做真实解码）"""

    def __init__(self, duration_text: str = "", parent=None):
        super().__init__(parent)
        self._duration = duration_text
        self.setFixedSize(_THUMB_W, _THUMB_H)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(30, 30, 34))
        p.drawRoundedRect(self.rect(), 6, 6)
        # 播放三角
        p.setBrush(QColor(255, 255, 255, 220))
        cx, cy, r = self.width() / 2, self.height() / 2, 10
        from PyQt5.QtCore import QPointF

        p.drawPolygon(QPointF(cx - r * 0.6, cy - r), QPointF(cx - r * 0.6, cy + r), QPointF(cx + r, cy))
        if self._duration:
            p.setPen(QColor(255, 255, 255, 200))
            f = self.font()
            f.setPointSize(7)
            p.setFont(f)
            p.drawText(self.rect().adjusted(0, 0, -4, -3), Qt.AlignBottom | Qt.AlignRight, self._duration)


class _KindBadge(QLabel):
    """非图片类条目的类型徽标：圆角色块 + 单字"""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        ch, color = _KIND_META.get(kind, _KIND_META["file"])
        self._ch, self._color = ch, color
        self.setFixedSize(_THUMB_W, _THUMB_H)
        self.setAlignment(Qt.AlignCenter)

    def refresh_style(self) -> None:
        self.setStyleSheet(
            f"QLabel {{ background: {self._color}22; color: {self._color};"
            f" border: 1px solid {self._color}55; border-radius: 6px;"
            f" {get_font_family_css()} {font_size_css(16)}; font-weight: 600; }}"
        )


class InlineArtifactPanel(QWidget):
    """成果栏：分隔条 + 行式清单（默认折叠）

    数据入口 ``update_artifacts(items)``，接 ``ToolResult.artifacts`` 或能力
    ``ui.artifacts.show`` 推送的条目列表。高度变化经 ``heightChanged`` 上报宿主
    （MessageCard 据此做外层滚动锚定补偿，契约同 InlineTodoPanel）。
    """

    heightChanged = pyqtSignal()
    # 图片单击 → 宿主内置预览（复用 message_card._on_preview_image 通道）
    previewImageRequested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._collapsed = True
        self._items: List[Dict[str, Any]] = []
        self._last_sig: Optional[str] = None
        self._pending_thumbs: List[tuple] = []  # [(QLabel, path)] 展开后的分帧解码队列
        self._thumb_pixmaps: Dict[str, QPixmap] = {}  # path → 缩略图缓存

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)

        self._header = _SectionHeader("成果", "upload-file", self)
        self._collapse_btn = TransparentToolButton(get_icon("展开"), self)
        self._collapse_btn.setFixedSize(22, 22)
        self._collapse_btn.setToolTip("展开成果列表")
        self._collapse_btn.clicked.connect(self._on_collapse_clicked)
        hdr_layout = self._header.layout()
        hdr_layout.insertWidget(hdr_layout.count(), self._collapse_btn)
        self._header.installEventFilter(self)
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.setToolTip("点击展开成果列表")
        layout.addWidget(self._header)

        # 列表容器：不用 QScrollArea（sizeHint 缓存失真，见模块注释），全量展示
        self._list_wrap = QWidget(self)
        self._list_wrap.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_wrap)
        self._list_layout.setContentsMargins(0, 0, 2, 0)
        self._list_layout.setSpacing(4)
        self._list_layout.addStretch(1)
        self._list_wrap.setVisible(False)
        layout.addWidget(self._list_wrap)

        self.hide()  # 无成果整区隐藏
        self.refresh_style()

    # ── 折叠 ──

    def eventFilter(self, obj, event) -> bool:
        if obj is self._header and event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            self._on_collapse_clicked()
            return True
        return super().eventFilter(obj, event)

    def _on_collapse_clicked(self) -> None:
        self._collapsed = not self._collapsed
        self._list_wrap.setVisible(not self._collapsed)
        icon = "展开" if self._collapsed else "折叠"
        self._collapse_btn.setIcon(get_icon(icon))
        tip = "展开成果列表" if self._collapsed else "折叠成果列表"
        self._collapse_btn.setToolTip(tip)
        self._header.setToolTip(tip)
        if not self._collapsed:
            self._schedule_thumb_decode()
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
        self.heightChanged.emit()

    # ── 数据 ──

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
            self._clear_items()
            self._header.set_extra("")
            self.hide()
            self.heightChanged.emit()
            return

        sig = repr([(it["path"], it["size"]) for it in normalized])
        if sig == self._last_sig and self._items:
            return
        self._last_sig = sig
        self._items = normalized
        self._rebuild()
        self.show()
        self._header.set_extra(f"{len(normalized)} 项")
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
        self.updateGeometry()
        if not self._collapsed:
            self._schedule_thumb_decode()
        self.heightChanged.emit()

    def clear_artifacts(self) -> None:
        """清空（实例保留，仅隐藏——面板随卡片存活，重建时直接复用）"""
        self._items = []
        self._last_sig = None
        self._pending_thumbs = []
        self._clear_items()
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
        visible = self._items[:MAX_VISIBLE_ITEMS]
        for item in visible:
            self._list_layout.addWidget(self._build_row(item))
        overflow = len(self._items) - len(visible)
        if overflow > 0:
            more = QLabel(f"… 还有 {overflow} 项未展示", self._list_wrap)
            more.setStyleSheet(
                f"QLabel {{ color: {Colors.TEXT_SECONDARY}; background: transparent;"
                f" {get_font_family_css()} {font_size_css(11)}; padding: 2px 4px; }}"
            )
            self._list_layout.addWidget(more)

    # ── 条目行 ──

    def _build_row(self, item: Dict[str, Any]) -> QFrame:
        row = QFrame(self._list_wrap)
        row.setObjectName("artifactItem")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        kind = item["kind"]
        path = item["path"]

        # 预览块
        if kind == "image":
            thumb = QLabel(row)
            thumb.setFixedSize(_THUMB_W, _THUMB_H)
            thumb.setAlignment(Qt.AlignCenter)
            pix = self._thumb_pixmaps.get(path)
            if pix is not None and not pix.isNull():
                thumb.setPixmap(pix)
            else:
                thumb.setText("…")
                # 折叠态也入队但不调度（_decode_next_thumb 检查折叠态即停）；
                # 展开切换时 _on_collapse_clicked 统一调度。此前版本折叠态
                # 直接剔除队列，导致「先推数据后展开」的场景缩略图永不解码。
                self._pending_thumbs.append((thumb, path))
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

        meta_bits = [_KIND_META.get(kind, _KIND_META["file"])[0]]
        if item.get("width") and item.get("height"):
            meta_bits.append(f"{item['width']}×{item['height']}")
        size_text = _human_size(item.get("size"))
        if size_text:
            meta_bits.append(size_text)
        if item.get("caption") and kind in ("text", "html"):
            meta_bits.append(item["caption"][:40])
        meta = _ElidedLabel(" · ".join(b for b in meta_bits if b), row)
        meta.setStyleSheet(
            f"QLabel {{ color: {Colors.TEXT_SECONDARY}; background: transparent;"
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

        row.setToolTip(f"{path}\n（双击打开，右键更多操作）")
        row.setCursor(Qt.PointingHandCursor if kind == "image" else Qt.ArrowCursor)

        row.mouseDoubleClickEvent = lambda ev, p=path, k=kind: self._on_row_activate(p, k)
        row.mousePressEvent = lambda ev, p=path, k=kind, r=row: self._on_row_press(ev, p, k, r)
        row.setContextMenuPolicy(Qt.CustomContextMenu)
        self._style_row(row)
        return row

    def _on_row_press(self, ev: QMouseEvent, path: str, kind: str, row: QFrame) -> None:
        if ev.button() == Qt.LeftButton and kind == "image":
            self.previewImageRequested.emit(path)
        elif ev.button() == Qt.RightButton:
            self._show_context_menu(ev.globalPos(), path, kind)

    def _on_row_activate(self, path: str, kind: str) -> None:
        """双击：图片走内置预览，其余交系统默认程序"""
        if kind == "image":
            self.previewImageRequested.emit(path)
            return
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
        act_open.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
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
            "QFrame#artifactItem { background: transparent; border: none;"
            f" border-radius: {BorderRadius.SM}; }}"
            "QFrame#artifactItem:hover { background: rgba(128, 128, 128, 0.08); }"
        )

    # ── 缩略图分帧解码 ──

    def _schedule_thumb_decode(self) -> None:
        """展开态逐帧解码缩略图：每拍一张让出事件循环，多张 4K 图不卡 UI"""
        if not self._pending_thumbs:
            return
        QTimer.singleShot(0, self._decode_next_thumb)

    def _decode_next_thumb(self) -> None:
        if self._collapsed or not self._pending_thumbs:
            return
        label, path = self._pending_thumbs.pop(0)
        try:
            if label.isVisible() and path not in self._thumb_pixmaps:
                pix = self._load_thumb(path)
                if pix is not None and not pix.isNull():
                    self._thumb_pixmaps[path] = pix
                if label.isVisible():
                    if pix is not None and not pix.isNull():
                        label.setPixmap(pix)
                    else:
                        label.setText("✕")
        except (OSError, RuntimeError):
            pass
        if self._pending_thumbs:
            QTimer.singleShot(10, self._decode_next_thumb)

    def _load_thumb(self, path: str) -> Optional[QPixmap]:
        """按缩略尺寸解码（QImageReader.setScaledSize 避免 4K 全量位图驻留内存）

        KeepAspectRatio 适配进显示块：产出 pixmap 必然 ≤ 块尺寸，不依赖
        QLabel 的溢出裁剪行为（曾用 2 倍尺寸 + ByExpanding，pixmap 比控件大，
        渲染溢出不可控）。
        """
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            reader.setScaledSize(size.scaled(_THUMB_W, _THUMB_H, Qt.KeepAspectRatio))
        img = reader.read()
        if img.isNull():
            return None
        return QPixmap.fromImage(img)

    # ── 主题 ──

    def refresh_style(self) -> None:
        self._header.refresh_style()
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if isinstance(w, QFrame) and w.objectName() == "artifactItem":
                self._style_row(w)
            elif isinstance(w, _KindBadge):
                w.refresh_style()
