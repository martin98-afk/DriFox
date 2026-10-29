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
``_sync_artifact_panel`` 不做旧卡卸载。

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

缩略图懒解码：折叠态只解码缩略图带（最多 ``_COLLAPSED_THUMB_LIMIT`` 张），
展开时经 ``QTimer`` 分帧解码（每拍一张），4K PNG 全量解码约百毫秒级，
一帧一张不会卡死 UI 线程。

数据入口按 ``path`` 合并（同路径原位更新、新路径追加），同轮多次推送不会
互相覆盖；历史会话加载时由 ``render_batch_to_assistant_card`` 从 tool 消息的
``artifacts`` 字段重建。
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
# 折叠态缩略图带：图片是最高频产出，折叠时也要看得见，只展示前 N 张
_COLLAPSED_THUMB_LIMIT = 5
# 缩略图预览块尺寸（折叠带与列表行共用，缓存可复用）
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
        # 徽标字与样式必须构造期落地：此前只在主题刷新里补，而刷新路径遍历的是
        # 列表根节点（徽标是行的子控件，永远命中不到）→ 徽标一直是空白色块。
        self.setText(ch)
        self.refresh_style()

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
        self._show_all = False  # 点过「还有 N 项」后不再截断
        self._items: List[Dict[str, Any]] = []
        self._last_sig: Optional[str] = None
        self._pending_thumbs: List[tuple] = []  # [(QLabel, path)] 展开态列表行的分帧解码队列
        self._pending_strip_thumbs: List[tuple] = []  # [(QLabel, path)] 折叠态缩略图带的解码队列
        self._thumb_pixmaps: Dict[str, QPixmap] = {}  # path → 缩略图缓存
        # 左键拖拽导出（拖到资源管理器/聊天软件即引用该文件）
        self._drag_press_pos = None
        self._drag_started = False

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

        # 折叠态缩略图带：只有折叠 + 有图片成果时可见，展开后让位给完整清单
        self._thumb_strip = QWidget(self)
        self._thumb_strip.setStyleSheet("background: transparent;")
        # 固定高度：垂直 policy 默认 Preferred 会吸收多余空间，把缩略图拉到面板中部
        self._thumb_strip.setFixedHeight(_THUMB_H)
        self._strip_layout = QHBoxLayout(self._thumb_strip)
        self._strip_layout.setContentsMargins(2, 0, 2, 0)
        self._strip_layout.setSpacing(6)
        self._thumb_strip.setVisible(False)
        layout.addWidget(self._thumb_strip)

        # 列表容器：不用 QScrollArea（sizeHint 缓存失真，见模块注释），全量展示
        self._list_wrap = QWidget(self)
        self._list_wrap.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_wrap)
        self._list_layout.setContentsMargins(0, 0, 2, 0)
        self._list_layout.setSpacing(4)
        self._list_layout.addStretch(1)
        self._list_wrap.setVisible(False)
        layout.addWidget(self._list_wrap)
        # 底部弹性空白：面板被给到高于 sizeHint 的空间时，多余高度落在这里，
        # 而不是被 header 吸收（否则折叠带的缩略图会被顶到面板底部）
        layout.addStretch(1)

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
        # 折叠带与清单互斥：折叠看缩略图，展开看完整行
        self._thumb_strip.setVisible(self._collapsed and self._strip_layout.count() > 1)
        icon = "展开" if self._collapsed else "折叠"
        self._collapse_btn.setIcon(get_icon(icon))
        tip = "展开成果列表" if self._collapsed else "折叠成果列表"
        self._collapse_btn.setToolTip(tip)
        self._header.setToolTip(tip)
        if self._collapsed:
            self._schedule_strip_decode()
        else:
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
        if self._collapsed:
            self._schedule_strip_decode()
        else:
            self._schedule_thumb_decode()
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
        self._header.set_extra(f"{len(normalized)} 项")
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
                f"QLabel {{ color: {Colors.TEXT_SECONDARY}; background: transparent;"
                f" {get_font_family_css()} {font_size_css(11)}; padding: 2px 4px; }}"
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

    def _rebuild_thumb_strip(self) -> None:
        """折叠态缩略图带：只放图片类成果，超上限显示 +N

        折叠时只占一条缩略图高度，却能让生图类产出直接可见（旧行为折叠态
        只有「成果 N 项」一行字，图片必须手动展开才能看到）。
        """
        self._clear_strip()
        images = [it for it in self._items if it["kind"] == "image"]
        if not images:
            self._thumb_strip.setVisible(False)
            return
        for item in images[:_COLLAPSED_THUMB_LIMIT]:
            path = item["path"]
            thumb = QLabel(self._thumb_strip)
            thumb.setFixedSize(_THUMB_W, _THUMB_H)
            thumb.setAlignment(Qt.AlignCenter)
            thumb.setCursor(Qt.PointingHandCursor)
            thumb.setToolTip(f"{item['name']}\n单击预览，左键拖拽可引用到其他应用")
            pix = self._thumb_pixmaps.get(path)
            if pix is not None and not pix.isNull():
                thumb.setPixmap(pix)
            else:
                thumb.setText("…")
                self._pending_strip_thumbs.append((thumb, path))
            thumb.mousePressEvent = lambda ev, p=path: self._on_strip_press(ev, p)
            thumb.mouseReleaseEvent = lambda ev, p=path: self._on_strip_release(ev, p)
            thumb.mouseMoveEvent = lambda ev, p=path, w=thumb: self._on_row_move(ev, p, w)
            self._strip_layout.addWidget(thumb)
        rest = len(images) - _COLLAPSED_THUMB_LIMIT
        if rest > 0:
            more = QLabel(f"+{rest}", self._thumb_strip)
            more.setAlignment(Qt.AlignCenter)
            more.setStyleSheet(
                f"QLabel {{ color: {Colors.TEXT_SECONDARY}; background: transparent;"
                f" {get_font_family_css()} {font_size_css(11)}; }}"
            )
            self._strip_layout.addWidget(more)
        self._strip_layout.addStretch(1)
        self._thumb_strip.setVisible(self._collapsed)

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
            pix = row.grab()
            if not pix.isNull():
                drag.setPixmap(pix.scaledToWidth(min(240, max(64, pix.width())), Qt.SmoothTransformation))
        except RuntimeError, AttributeError:
            pass
        return drag

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
        # 队首不可见（如刚又被折叠）就先不取：pop 掉会永久丢掉这张的解码机会，
        # 再展开时队列已空 → 缩略图永驻占位符。
        if self._collapsed or not self._pending_thumbs:
            return
        label, path = self._pending_thumbs[0]
        if not label.isVisible():
            return
        self._pending_thumbs.pop(0)
        self._apply_thumb(label, path)
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
        label, path = self._pending_strip_thumbs[0]
        if not label.isVisible():
            return
        self._pending_strip_thumbs.pop(0)
        self._apply_thumb(label, path)
        if self._pending_strip_thumbs:
            QTimer.singleShot(10, self._decode_next_strip_thumb)

    def _apply_thumb(self, label: QLabel, path: str) -> None:
        """解码并回填（缓存命中直接复用；失败显示 ✕ 不报错）"""
        try:
            pix = self._thumb_pixmaps.get(path)
            if pix is None:
                pix = self._load_thumb(path)
                if pix is not None and not pix.isNull():
                    self._thumb_pixmaps[path] = pix
            if not label.isVisible():
                return
            if pix is not None and not pix.isNull():
                label.setPixmap(pix)
            else:
                label.setText("✕")
        except OSError, RuntimeError:
            pass

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
                # 徽标是行的子控件，不在列表根节点上（旧实现永远命中不到）
                for badge in w.findChildren(_KindBadge):
                    badge.refresh_style()
