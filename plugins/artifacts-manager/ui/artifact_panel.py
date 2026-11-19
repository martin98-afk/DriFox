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
4K PNG 全量解码约百毫秒级，一帧一张不会卡死 UI 线程。折叠画廊按当前
宽度计算可见槽位（首图略宽），溢出收进「+N」，窄栏自动减少缩略图。

数据入口按 ``path`` 合并（同路径原位更新、新路径追加），同轮多次推送不会
互相覆盖；历史会话加载时由 ``render_batch_to_assistant_card`` 从 tool 消息的
``artifacts`` 字段重建。
"""

import os
from typing import Any, Dict, List, Optional
from app.widgets._bounded_caches import _BoundedLRUCache

from PyQt5.QtCore import QAbstractAnimation, QEvent, QEasingCurve, QMargins, QPropertyAnimation, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QImageReader, QMouseEvent, QPainter, QPixmap
from PyQt5.QtCore import QUrl
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget
from qfluentwidgets import TransparentToolButton

from app.utils.design_tokens import Animations, BorderRadius, Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon
from app.widgets._workbench_helpers import _SectionHeader
from app.widgets.elided_label import _ElidedLabel

# 单卡最多渲染条数：插件一次性推几百个文件时把卡片撑爆（上限外折叠进「还有 N 个」）
MAX_VISIBLE_ITEMS = 20
# 折叠态画廊：固定单行不换行，块高统一、块宽按图片宽高比自适应（竖图窄条/横图宽块）；
# 可见数由宽度决定（放得下就铺满），放不下收进「+N」；上限仅防病态超宽
_COLLAPSED_THUMB_H = 64
_COLLAPSED_THUMB_MIN_W = 32
_COLLAPSED_THUMB_MAX_W = 160
# 图片宽高不可得时的兜底块宽（首图略宽）
_COLLAPSED_FIRST_W = 112
_COLLAPSED_ITEM_W = 72
_COLLAPSED_GAP = 6
_COLLAPSED_MAX_THUMBS = 12
# 摘要行（「另有 N 个文件」）高度
_COLLAPSED_SUMMARY_H = 18
# 缩略图预览块尺寸（展开列表行用；画廊用上面折叠常量，缓存按尺寸区分）
_THUMB_W, _THUMB_H = 64, 48
# 展开列表限高放行值（Qt 伪无限，同 markdown 折叠卡惯例）
_QWIDGETSIZE_MAX = 16777215

# kind → (徽标短标签, 主色)；主色同时用于徽标底与 meta 强调
_KIND_META = {
    "image": ("IMG", "#3b82f6"),
    "video": ("VID", "#8b5cf6"),
    "html": ("HTML", "#f59e0b"),
    "text": ("TXT", "#10b981"),
    "file": ("FILE", "#6b7280"),
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


def _thumb_key(path: str, w: int, h: int) -> str:
    """缩略图缓存 key：同路径不同尺寸（折叠画廊 vs 展开列表）分开存，不拉小图"""
    return f"{path}|{w}x{h}"


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
    """非图片类条目的类型徽标：圆角色块 + 扩展名短标签"""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        label, color = _KIND_META.get(kind, _KIND_META["file"])
        self._label, self._color = label, color
        self.setFixedSize(_THUMB_W, _THUMB_H)
        self.setAlignment(Qt.AlignCenter)
        # 徽标字与样式必须构造期落地：此前只在主题刷新里补，而刷新路径遍历的是
        # 列表根节点（徽标是行的子控件，永远命中不到）→ 徽标一直是空白色块。
        self.setText(label)
        self.refresh_style()

    def refresh_style(self) -> None:
        self.setStyleSheet(
            f"QLabel {{ background: {self._color}22; color: {self._color};"
            f" border: 1px solid {self._color}55; border-radius: 6px;"
            f" {get_font_family_css()} {font_size_css(11)}; font-weight: 600; }}"
        )


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

        # 折叠态画廊：浅底圆角容器 = 缩略图行 +（可选）非图片摘要行。
        # 只有折叠 + 有图片成果时可见，展开后让位给完整清单。
        self._thumb_strip = QFrame(self)
        self._thumb_strip.setObjectName("artifactStripCard")
        strip_box = QVBoxLayout(self._thumb_strip)
        strip_box.setContentsMargins(6, 6, 6, 6)
        strip_box.setSpacing(4)
        strip_row = QWidget(self._thumb_strip)
        strip_row.setStyleSheet("background: transparent;")
        self._strip_layout = QHBoxLayout(strip_row)
        self._strip_layout.setContentsMargins(0, 0, 0, 0)
        self._strip_layout.setSpacing(_COLLAPSED_GAP)
        self._strip_layout.addStretch(1)
        strip_box.addWidget(strip_row)
        self._summary_label = QLabel(self._thumb_strip)
        self._summary_label.setObjectName("artifactSummary")
        self._summary_label.setVisible(False)
        strip_box.addWidget(self._summary_label)
        self._thumb_strip.setVisible(False)
        layout.addWidget(self._thumb_strip)

        # 列表容器：同款浅底圆角卡片；不用 QScrollArea（sizeHint 缓存失真，见模块注释），全量展示
        self._list_wrap = QFrame(self)
        self._list_wrap.setObjectName("artifactListCard")
        self._list_layout = QVBoxLayout(self._list_wrap)
        self._list_layout.setContentsMargins(6, 4, 6, 6)
        self._list_layout.setSpacing(4)
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
        """按当前宽度算可见槽位：首图 1 + 常规图 N，上限 _COLLAPSED_MAX_THUMBS"""
        if image_count <= 1:
            return max(0, min(image_count, 1))
        strip_w = self._thumb_strip.width()
        usable = strip_w - 12 if strip_w > 24 else _COLLAPSED_FIRST_W + _COLLAPSED_GAP + _COLLAPSED_ITEM_W
        rest_slots = max(0, (usable - _COLLAPSED_FIRST_W - _COLLAPSED_GAP) // (_COLLAPSED_ITEM_W + _COLLAPSED_GAP))
        return max(1, min(image_count, 1 + rest_slots, _COLLAPSED_MAX_THUMBS))

    # ── 折叠 ──

    def eventFilter(self, obj, event) -> bool:
        if obj is self._header and event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            self._on_collapse_clicked()
            return True
        return super().eventFilter(obj, event)

    def _on_collapse_clicked(self) -> None:
        self._collapsed = not self._collapsed
        icon = "展开" if self._collapsed else "折叠"
        self._collapse_btn.setIcon(get_icon(icon))
        tip = "展开成果列表" if self._collapsed else "折叠成果列表"
        self._collapse_btn.setToolTip(tip)
        self._header.setToolTip(tip)
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

    def _strip_thumb_width(self, path: str, is_first: bool) -> int:
        """画廊块宽按图片宽高比自适应（等高 64）：竖图窄条/横图宽块，杜绝固定块留白。

        QImageReader.size() 只读元数据不解码；宽高不可得（不存在/损坏）回退兜底宽。
        """
        size = QImageReader(path).size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            w = round(_COLLAPSED_THUMB_H * size.width() / size.height())
            return max(_COLLAPSED_THUMB_MIN_W, min(w, _COLLAPSED_THUMB_MAX_W))
        return _COLLAPSED_FIRST_W if is_first else _COLLAPSED_ITEM_W

    def _rebuild_thumb_strip(self) -> None:
        """折叠态单行画廊：等高自适应宽，溢出收进 +N，非图片只入摘要行

        折叠时只占画廊高度（混合成果多一行摘要），生图类产出直接可见；
        槽位按当前宽度算，窄栏自动减常规缩略图，不换行不横向滚动。
        """
        self._clear_strip()
        images = [it for it in self._items if it["kind"] == "image"]
        if not images:
            self._thumb_strip.setVisible(False)
            self._summary_label.setVisible(False)
            self._strip_slot_count = 0
            return
        visible = self._collapsed_slot_count(len(images))
        self._strip_slot_count = visible
        for idx, item in enumerate(images[:visible]):
            path = item["path"]
            w = self._strip_thumb_width(path, idx == 0)
            thumb = QLabel(self._thumb_strip)
            thumb.setObjectName("artifactThumbnail")
            thumb.setFixedSize(w, _COLLAPSED_THUMB_H)
            thumb.setAlignment(Qt.AlignCenter)
            thumb.setCursor(Qt.PointingHandCursor)
            thumb.setToolTip(f"{item['name']}\n单击预览，左键拖拽可引用到其他应用")
            pix = self._thumb_pixmaps.get(_thumb_key(path, w, _COLLAPSED_THUMB_H))
            if pix is not None and not pix.isNull():
                thumb.setPixmap(pix)
            else:
                thumb.setText("…")
                self._pending_strip_thumbs.append((thumb, path, w, _COLLAPSED_THUMB_H))
            thumb.mousePressEvent = lambda ev, p=path: self._on_strip_press(ev, p)
            thumb.mouseReleaseEvent = lambda ev, p=path: self._on_strip_release(ev, p)
            thumb.mouseMoveEvent = lambda ev, p=path, w=thumb: self._on_row_move(ev, p, w)
            self._strip_layout.addWidget(thumb)
        rest = len(images) - visible
        if rest > 0:
            more = QLabel(f"+{rest}", self._thumb_strip)
            more.setObjectName("artifactOverflow")
            more.setAlignment(Qt.AlignCenter)
            more.setCursor(Qt.PointingHandCursor)
            more.setToolTip(f"还有 {rest} 张图片，点击展开全部")
            # 点 +N 直接进完整列表（折叠看图，展开找文件）
            more.mousePressEvent = lambda _ev: self._on_collapse_clicked()
            self._strip_layout.addWidget(more)
        self._strip_layout.addStretch(1)
        files_n = len(self._items) - len(images)
        if files_n > 0:
            self._summary_label.setText(f"另有 {files_n} 个文件")
            self._summary_label.setVisible(True)
        else:
            self._summary_label.setVisible(False)
        total_h = 12 + _COLLAPSED_THUMB_H + ((4 + _COLLAPSED_SUMMARY_H) if files_n > 0 else 0)
        self._thumb_strip.setFixedHeight(total_h)
        self._style_strip()
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
        lay.setContentsMargins(2, 4, 6, 4)
        lay.setSpacing(8)

        kind = item["kind"]
        path = item["path"]

        # 预览块
        if kind == "image":
            thumb = QLabel(row)
            thumb.setObjectName("artifactRowThumb")
            thumb.setFixedSize(_THUMB_W, _THUMB_H)
            thumb.setAlignment(Qt.AlignCenter)
            pix = self._thumb_pixmaps.get(_thumb_key(path, _THUMB_W, _THUMB_H))
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
        """解码并回填（缓存命中直接复用；失败显示 ✕ 不报错）"""
        try:
            pix = self._thumb_pixmaps.get(_thumb_key(path, w, h))
            if pix is None:
                pix = self._load_thumb(path, w, h)
                if pix is not None and not pix.isNull():
                    self._thumb_pixmaps[_thumb_key(path, w, h)] = pix
            if not label.isVisible():
                return
            if pix is not None and not pix.isNull():
                label.setPixmap(pix)
            else:
                # 文件已清理/不存在：显示类型徽标而非无解释的 ✕
                label.setText(_KIND_META["image"][0])
                label.setToolTip(f"{path}\n文件已不存在或无法读取（可能已被清理）")
        except (OSError, RuntimeError):
            pass

    def _load_thumb(self, path: str, w: int = _THUMB_W, h: int = _THUMB_H) -> Optional[QPixmap]:
        """按缩略尺寸解码（QImageReader.setScaledSize 避免 4K 全量位图驻留内存）

        KeepAspectRatio 适配进显示块：产出 pixmap 必然 ≤ 块尺寸，不依赖
        QLabel 的溢出裁剪行为（曾用 2 倍尺寸 + ByExpanding，pixmap 比控件大，
        渲染溢出不可控）。
        """
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            reader.setScaledSize(size.scaled(w, h, Qt.KeepAspectRatio))
        img = reader.read()
        if img.isNull():
            return None
        return QPixmap.fromImage(img)

    # ── 主题 ──

    def refresh_style(self) -> None:
        self._header.refresh_style()
        self._style_strip()
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if isinstance(w, QFrame) and w.objectName() == "artifactItem":
                self._style_row(w)
                # 徽标是行的子控件，不在列表根节点上（旧实现永远命中不到）
                for badge in w.findChildren(_KindBadge):
                    badge.refresh_style()

    def _style_strip(self) -> None:
        """画廊 / 展开列表两张卡片及具内部动态控件的主题化样式（浅底卡片层级）"""
        from app.utils.design_tokens import Colors

        card_css = (
            f"background: {Colors.CARD_BG.format(alpha=110)};"
            f" border: 1px solid {Colors.BORDER}; border-radius: {BorderRadius.MD};"
        )
        self._thumb_strip.setStyleSheet(f"QFrame#artifactStripCard {{ {card_css} }}")
        self._list_wrap.setStyleSheet(f"QFrame#artifactListCard {{ {card_css} }}")
        self._summary_label.setStyleSheet(
            f"QLabel#artifactSummary {{ color: {Colors.TEXT_SECONDARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(11)}; padding: 0 2px; }}"
        )
        for lab in self._thumb_strip.findChildren(QLabel):
            name = lab.objectName()
            if name == "artifactThumbnail":
                lab.setStyleSheet(
                    f"QLabel#artifactThumbnail {{ background: {Colors.HOVER_BG};"
                    f" border: 1px solid {Colors.BORDER}; border-radius: 6px; }}"
                )
            elif name == "artifactOverflow":
                lab.setStyleSheet(
                    f"QLabel#artifactOverflow {{ color: {Colors.TEXT_SECONDARY};"
                    f" background: {Colors.HOVER_BG}; border: 1px dashed {Colors.BORDER};"
                    f" border-radius: 6px; {get_font_family_css()} {font_size_css(12)};"
                    f" font-weight: 600; }}"
                )
