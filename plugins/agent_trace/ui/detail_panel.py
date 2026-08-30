# -*- coding: utf-8 -*-
"""agent_trace.DetailPanel — 右侧详情面板。

**不再对所有条目套同一组 Summary/Preview/Raw/Source**（旧版问题），改为按条目
类型给出**该类型真正有的内容**，对齐 DeepSeek Harness 的轨迹详情 + Chrome
DevTools Network 的 Request/Response/Timing 三段式：

| 条目类型 | Tabs |
|---|---|
| SYSTEM | ``System Prompt`` / ``Tools Schema`` / ``Headers`` |
| TOOL | ``Request`` / ``Response`` / ``Raw`` / ``Timing`` |
| ASSISTANT | ``Preview`` / ``Raw`` / ``Timing`` |
| USER / HOOK | ``Content`` / ``Raw`` / ``Headers`` |

页面是**固定 6 个实例**、tab 只是切换映射（避免每次选中都销毁重建 widget，
也规避了 QStackedWidget 反复 add/remove 的闪烁）：

- ``_page_text``：通用长文本（System Prompt / Content / Preview）
- ``_page_request``：工具入参
- ``_page_response``：工具结果
- ``_page_raw``：完整原始内容
- ``_page_tools``：工具目录（左列表 + 右 JSON schema）
- ``_page_kv``：键值面板（Headers / Timing 共用，内容动态填充）

⚠️ 颜色一律走 :class:`ThemePalette`（QColor 已解析 rgba），禁止
``QColor(colors["text_secondary"])`` —— 主题里它是 rgba 字符串，解析失败变黑。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QRect, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import SegmentedWidget

from .trace_models import EntryKind, ThemePalette, TraceRecord, format_duration, kind_color, pretty_json, with_alpha
from .turn_list_widget import unified_scrollbar

# tab key → 页面实例槽位
_SLOT_OF_TAB = {
    "system": "text",
    "content": "text",
    "preview": "text",
    "request": "request",
    "response": "response",
    "raw": "raw",
    "tools": "tools",
    "headers": "kv",
    "timing": "kv",
}

_TABS_BY_KIND: Dict[EntryKind, Tuple[Tuple[str, str], ...]] = {
    EntryKind.SYSTEM: (
        ("system", "System Prompt"),
        ("tools", "Tools Schema"),
        ("headers", "Headers"),
    ),
    EntryKind.TOOL: (
        ("request", "Request"),
        ("response", "Response"),
        ("raw", "Raw"),
        ("timing", "Timing"),
    ),
    EntryKind.ASSISTANT: (
        ("preview", "Preview"),
        ("raw", "Raw"),
        ("timing", "Timing"),
    ),
    EntryKind.USER: (
        ("content", "Content"),
        ("raw", "Raw"),
        ("headers", "Headers"),
    ),
    EntryKind.CONTEXT: (
        ("content", "Content"),
        ("raw", "Raw"),
        ("headers", "Headers"),
    ),
}

_SEG_HEIGHT = 32

# 详情正文是否对 JSON 用等宽字体。
# ⚠️ 默认 False —— 用户明确要求「全部应用系统字体」；改成 True 即恢复
# 「JSON/代码区等宽、自然语言系统字体」的混合策略。
_MONO_FOR_JSON = False


class _KVPage(QScrollArea):
    """键值面板（Headers / Timing 共用）— 顶部可挂一条自绘时间条。"""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self._pal = ThemePalette()
        self._base_px = 13
        self._rows: List[Tuple[QWidget, QLabel, QLabel]] = []
        self._bar: Optional["_TimingBar"] = None

        body = QWidget()
        self._body = body
        self._layout = QVBoxLayout(body)
        # 键值面板向上贴齐（顶部 8px，与文本页的 padding-top 一致）
        self._layout.setContentsMargins(16, 8, 16, 12)
        self._layout.setSpacing(0)
        self._layout.addStretch(1)
        self.setWidget(body)

    def set_palette(self, pal: ThemePalette, base_px: int) -> None:
        self._pal = pal
        self._base_px = base_px
        self._apply_theme()

    def set_rows(self, rows: List[Tuple[str, str]], timing: Optional[Tuple[float, float, float]] = None) -> None:
        """填充键值行；``timing`` = (start_rel_ms, duration_ms, total_ms) 时显示时间条。"""
        self._clear_rows()
        if timing is not None:
            self._bar = _TimingBar(self._body)
            self._bar.set_palette(self._pal, self._base_px)
            self._bar.set_timing(*timing)
            self._layout.insertWidget(0, self._bar)
            self._layout.insertSpacing(1, 10)
        for key, val in rows:
            row = QWidget(self._body)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(12)
            k = QLabel(key, row)
            k.setFixedWidth(92)
            k.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            v = QLabel(val or "-", row)
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            rl.addWidget(k, 0, Qt.AlignTop)
            rl.addWidget(v, 1)
            self._layout.addWidget(row)
            self._layout.addSpacing(6)
            self._rows.append((row, k, v))
        self._apply_theme()

    def _clear_rows(self) -> None:
        # ⚠️ 只删顶层行 widget：k/v 是它的子对象，会随父一起析构；
        # 再逐个 deleteLater 会触发双重删除。
        for row, _k, _v in self._rows:
            self._layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        if self._bar is not None:
            self._layout.removeWidget(self._bar)
            self._bar.setParent(None)
            self._bar.deleteLater()
            self._bar = None
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.spacerItem() is not None:
                continue
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._layout.addStretch(1)

    def _apply_theme(self) -> None:
        pal = self._pal
        fs = max(9, self._base_px - 1)
        ui = pal.font_family
        self.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QWidget { background: transparent; }"
            + (unified_scrollbar(8) or "")
        )
        for _row, k, v in self._rows:
            k.setStyleSheet(f"color: {pal.q('text_muted')}; font-family: '{ui}'; font-size: {fs}px; padding: 2px 0;")
            v.setStyleSheet(f"color: {pal.q('text')}; font-family: '{ui}'; font-size: {fs}px; padding: 2px 0;")
        if self._bar is not None:
            self._bar.set_palette(pal, self._base_px)


class _TimingBar(QWidget):
    """相对整段会话的时间条（对齐 DevTools Timing 的可视化条）。"""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._pal = ThemePalette()
        self._base_px = 13
        self._offset = 0.0
        self._dur = 0.0
        self._total = 1.0
        self.setFixedHeight(34)

    def set_palette(self, pal: ThemePalette, base_px: int) -> None:
        self._pal = pal
        self._base_px = base_px
        self.update()

    def set_timing(self, offset_ms: float, duration_ms: float, total_ms: float) -> None:
        self._offset = max(0.0, offset_ms)
        self._dur = max(0.0, duration_ms)
        self._total = max(1.0, total_ms)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            pal = self._pal
            track = QRect(0, 14, self.width(), 10)
            p.setPen(Qt.NoPen)
            p.setBrush(pal.track)
            p.drawRoundedRect(QRectF(track), 3, 3)
            a = min(1.0, self._offset / self._total)
            b = min(1.0, (self._offset + max(self._dur, 8)) / self._total)
            x0 = track.x() + a * track.width()
            w = max(4.0, (b - a) * track.width())
            p.setBrush(with_alpha(QColor(pal.accent), 220))
            p.drawRoundedRect(QRectF(x0, track.y(), w, track.height()), 3, 3)
            f = QFont(pal.mono_family)
            f.setPixelSize(max(9, self._base_px - 2))
            p.setFont(f)
            p.setPen(QColor(pal.text_muted))
            p.drawText(QRect(0, 0, self.width(), 12), Qt.AlignLeft | Qt.AlignVCenter, format_duration(int(self._offset)))
            p.drawText(
                QRect(0, 0, self.width(), 12), Qt.AlignRight | Qt.AlignVCenter, format_duration(int(self._dur))
            )
        finally:
            p.end()


class _ToolsPage(QWidget):
    """Tools Schema：左侧工具目录 + 右侧 JSON schema（避免一坨几千行 JSON）。"""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._pal = ThemePalette()
        self._base_px = 13
        self._items: List[Dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QFrame(self)
        head.setFixedHeight(28)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 0, 16, 0)
        self._count = QLabel("0 个工具", head)
        hl.addWidget(self._count)
        hl.addStretch(1)
        self._summary = QLabel("", head)
        hl.addWidget(self._summary)
        root.addWidget(head)

        split = QSplitter(Qt.Horizontal, self)
        split.setHandleWidth(2)
        split.setChildrenCollapsible(False)
        self._list = QListWidget(split)
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setUniformItemSizes(True)
        self._list.currentRowChanged.connect(self._on_row)
        self._json = QPlainTextEdit(split)
        self._json.setReadOnly(True)
        self._json.setFrameShape(QFrame.NoFrame)
        self._json.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._json.setUndoRedoEnabled(False)
        self._json.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        split.addWidget(self._list)
        split.addWidget(self._json)
        split.setSizes([220, 380])
        root.addWidget(split, 1)

    # ── 数据 ──

    def set_schemas(self, schemas: List[Dict[str, Any]]) -> None:
        items: List[Dict[str, Any]] = []
        for sc in schemas or []:
            fn = sc.get("function") if isinstance(sc, dict) else None
            if not isinstance(fn, dict):
                fn = sc if isinstance(sc, dict) else {}
            name = fn.get("name") or sc.get("name") or "(匿名)"
            items.append(
                {
                    "name": str(name),
                    "description": str(fn.get("description") or "").strip(),
                    "json": pretty_json(json.dumps(fn, ensure_ascii=False)),
                    "params": len((fn.get("parameters") or {}).get("properties") or {}),
                }
            )
        items.sort(key=lambda d: d["name"])
        self._items = items
        self._list.clear()
        for it in items:
            li = QListWidgetItem(it["name"])
            li.setToolTip(it["description"] or it["name"])
            self._list.addItem(li)
        total_params = sum(i["params"] for i in items)
        self._count.setText(f"{len(items)} 个工具")
        self._summary.setText(f"共 {total_params} 个参数" if items else "")
        if items:
            self._list.setCurrentRow(0)
        else:
            self._json.setPlainText("（当前没有可用工具）")

    def set_palette(self, pal: ThemePalette, base_px: int) -> None:
        self._pal = pal
        self._base_px = base_px
        self._apply_theme()

    def _on_row(self, row: int) -> None:
        if 0 <= row < len(self._items):
            it = self._items[row]
            head = f"// {it['name']}" + (f" — {it['description']}" if it["description"] else "")
            self._json.setPlainText(f"{head}\n\n{it['json']}")

    def _apply_theme(self) -> None:
        pal = self._pal
        fs = max(9, self._base_px)
        small = max(9, self._base_px - 2)
        self.setStyleSheet("QWidget { background: transparent; }")
        self._count.setStyleSheet(
            f"color: {pal.q('text_muted')}; font-family: '{pal.font_family}'; font-size: {small}px;"
        )
        self._summary.setStyleSheet(
            f"color: {pal.q('text_muted')}; font-family: '{pal.font_family}'; font-size: {small}px;"
        )
        self._list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; color: %s;"
            " font-family: '%s'; font-size: %dpx; }"
            "QListWidget::item { padding: 5px 12px; border-radius: 4px; }"
            "QListWidget::item:hover { background: %s; }"
            "QListWidget::item:selected { background: %s; color: %s; }"
            % (
                pal.q("text"),
                pal.font_family,
                small + 1,
                pal.q("line", 26),
                pal.q("selected_bg"),
                pal.q("text"),
            )
            + (unified_scrollbar(8) or "")
        )
        self._json.setStyleSheet(
            f"QPlainTextEdit {{ background: transparent; color: {pal.q('text')}; border: none;"
            f" font-family: '{pal.mono_family if _MONO_FOR_JSON else pal.font_family}';"
            f" font-size: {fs - 1}px; padding: 8px 16px; }}"
            + (unified_scrollbar(8) or "")
        )


class DetailPanel(QWidget):
    """右侧详情面板。"""

    dismissRequested = pyqtSignal()  # 点击 × → 清除选中

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._current_idx: Optional[int] = None
        self._pal = ThemePalette()
        self._base_px = 13
        self._tabs: Tuple[Tuple[str, str], ...] = ()
        self._tab_keys: List[str] = []
        self._active_tab = ""
        self._rebuilding = False
        self._tools_schemas: List[Dict[str, Any]] = []
        self._system_sections: List[Tuple[str, str]] = []
        self._bounds = (0.0, 1.0)
        self._build_ui()

    # ──────────────────── 搭建 ────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 标题行：徽章 + 标题 + 状态/时长 + ×
        self._title_bar = QFrame(self)
        self._title_bar.setObjectName("agentTraceDetailTitle")
        self._title_bar.setFixedHeight(46)
        title = QVBoxLayout(self._title_bar)
        # 内容向上贴齐：上边距 6、行间距 1（旧版 52px 高 + 6/2 留白看起来"浮在中间"）
        title.setContentsMargins(16, 6, 8, 4)
        title.setSpacing(1)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._badge = QLabel("----", self._title_bar)
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedHeight(18)
        self._badge.setMinimumWidth(72)
        row1.addWidget(self._badge, 0, Qt.AlignVCenter)
        self._title_label = QLabel("未选中条目", self._title_bar)
        row1.addWidget(self._title_label, 1)
        self._close_btn = _CloseButton(self._title_bar)
        self._close_btn.clicked.connect(self.dismissRequested.emit)
        row1.addWidget(self._close_btn, 0, Qt.AlignVCenter)
        title.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(0)
        self._meta_label = QLabel("", self._title_bar)
        row2.addWidget(self._meta_label, 1)
        title.addLayout(row2)
        outer.addWidget(self._title_bar)

        # tabs
        self._segmented = SegmentedWidget(self)
        self._segmented.setFixedHeight(_SEG_HEIGHT)
        self._segmented.currentItemChanged.connect(self._on_tab_changed)
        outer.addWidget(self._segmented)

        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack, 1)

        self._page_text = self._build_text_page(wrap=True)
        self._page_request = self._build_text_page(wrap=True)
        self._page_response = self._build_text_page(wrap=True)
        self._page_raw = self._build_text_page(wrap=False)
        self._page_tools = _ToolsPage(self)
        self._page_kv = _KVPage(self)
        self._slots = {
            "text": self._page_text,
            "request": self._page_request,
            "response": self._page_response,
            "raw": self._page_raw,
            "tools": self._page_tools,
            "kv": self._page_kv,
        }
        for w in self._slots.values():
            self._stack.addWidget(w)

        self._apply_idle()

    def _build_text_page(self, wrap: bool) -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setFrameShape(QFrame.NoFrame)
        edit.setLineWrapMode(QPlainTextEdit.WidgetWidth if wrap else QPlainTextEdit.NoWrap)
        edit.setUndoRedoEnabled(False)
        edit.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        return edit

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord]) -> None:
        self._records = list(records)
        if self._current_idx is not None and self._current_idx >= len(self._records):
            self._current_idx = None
            self._apply_idle()
        else:
            self._refresh_current()

    def select(self, idx: Optional[int]) -> None:
        self._current_idx = idx
        self._refresh_current()

    @property
    def current_idx(self) -> Optional[int]:
        return self._current_idx

    def clear(self) -> None:
        self._records = []
        self._current_idx = None
        self._apply_idle()

    def set_tools_schema(self, schemas: List[Dict[str, Any]]) -> None:
        """注入当前可用工具的 function schema（SYSTEM → Tools Schema tab）。"""
        self._tools_schemas = list(schemas or [])
        self._page_tools.set_schemas(self._tools_schemas)

    def set_system_sections(self, sections: List[Tuple[str, str]]) -> None:
        """注入系统提示词分段 [(标题, 正文)]（SYSTEM → System Prompt tab）。"""
        self._system_sections = list(sections or [])
        if self._current_idx is not None:
            self._refresh_current()

    def set_bounds(self, t0: float, t1: float) -> None:
        """会话时间边界（Timing tab 的相对位置条）。"""
        self._bounds = (t0, t1 if t1 > t0 else t0 + 1.0)

    def set_colors(self, colors: dict, is_dark: bool = True) -> None:
        if not colors:
            return
        self._pal = ThemePalette.from_theme(colors, is_dark, mono_family=self._pal.mono_family)
        self._apply_theme()
        self._refresh_current()

    def set_palette(self, pal: ThemePalette) -> None:
        self._pal = pal
        self._apply_theme()
        self._refresh_current()

    def _apply_font(self, font: QFont) -> None:
        px = font.pixelSize()
        if px <= 0:
            ptf = font.pointSizeF()
            px = int(round(ptf * 4 / 3)) if ptf > 0 else 13
        self._base_px = max(10, min(24, px))
        fam = font.family()
        if fam:
            self._pal.font_family = fam
        self._apply_theme()
        self._refresh_current()
        try:  # SegmentedWidget 内部硬编码 setFont(widget, 18)，只能用专用 API
            from PyQt5.QtGui import QColor as _QColor

            self._segmented.setItemFontSize(max(10, self._base_px - 2))
            self._segmented.setIndicatorColor(_QColor(self._pal.accent), _QColor(self._pal.accent))
        except Exception:
            pass

    # ──────────────────── 主题 ────────────────────

    def paintEvent(self, _event) -> None:  # noqa: N802
        """面板底色 + 左侧 1px 分隔线：列表区全透明，靠底色差异区分两栏。"""
        p = QPainter(self)
        try:
            p.setPen(Qt.NoPen)
            p.setBrush(with_alpha(self._pal.line, 8))
            p.drawRect(self.rect())
            p.setBrush(QColor(self._pal.border))
            p.drawRect(0, 0, 1, self.height())
        finally:
            p.end()

    def _apply_theme(self) -> None:
        pal = self._pal
        fs = max(9, self._base_px - 1)
        ui = pal.font_family  # 一律系统 UI 字体（_MONO_FOR_JSON 时才对 JSON 用等宽）
        self.setStyleSheet("QWidget { background: transparent; }")
        self._title_bar.setStyleSheet(
            f"QFrame#agentTraceDetailTitle {{ background: transparent;"
            f" border-bottom: 1px solid {pal.q('border')}; }}"
        )
        self._close_btn.set_palette(pal)
        self._title_label.setStyleSheet(f"color: {pal.q('text')}; font-family: '{ui}'; font-size: {self._base_px}px;")
        self._meta_label.setStyleSheet(f"color: {pal.q('text_muted')}; font-family: '{ui}'; font-size: {fs}px;")
        for edit in (self._page_text, self._page_request, self._page_response, self._page_raw):
            # 正文一律**系统 UI 字体**；padding-top 8 → 与标题栏、键值面板「向上贴齐」
            edit.setStyleSheet(self._edit_qss(edit))
        self._page_tools.set_palette(pal, self._base_px)
        self._page_kv.set_palette(pal, self._base_px)
        self._badge.setStyleSheet(self._badge_qss(QColor("#888888")))
        self._refresh_badge()

    def _edit_qss(self, edit: QPlainTextEdit) -> str:
        """正文编辑框 QSS：系统 UI 字体（JSON 可选等宽，见 ``_MONO_FOR_JSON``）。"""
        pal = self._pal
        fs = max(9, self._base_px - 1)
        if _MONO_FOR_JSON:
            head = (edit.toPlainText() or "").lstrip()[:1]
            fam = pal.mono_family if head in "{[" else pal.font_family
        else:
            fam = pal.font_family
        return (
            f"QPlainTextEdit {{ background: transparent; color: {pal.q('text')}; border: none;"
            f" font-family: '{fam}'; font-size: {fs + 1}px; padding: 8px 16px; }}"
            + (unified_scrollbar(8) or "")
        )

    def _set_content(self, edit: QPlainTextEdit, text: str) -> None:
        """填正文 + 同步字体（内容类型变了字体可能要换）。"""
        edit.setPlainText(text or "")
        if _MONO_FOR_JSON:
            edit.setStyleSheet(self._edit_qss(edit))

    def _badge_qss(self, color: QColor) -> str:
        pal = self._pal
        fs = max(9, self._base_px - 3)
        return (
            "QLabel {"
            f" color: {color.name()};"
            f" background-color: rgba({color.red()},{color.green()},{color.blue()},0.14);"
            " border-radius: 3px; padding: 0 8px;"
            f" font-family: '{pal.mono_family}'; font-size: {fs}px; font-weight: 700; letter-spacing: 0.5px;"
            "}"
        )

    # ──────────────────── 刷新 ────────────────────

    def _on_tab_changed(self, key: str) -> None:
        if self._rebuilding or not key:
            return
        self._active_tab = key
        slot = _SLOT_OF_TAB.get(key)
        if slot is not None:
            self._stack.setCurrentWidget(self._slots[slot])
        self._fill_active_tab()

    def _rebuild_tabs(self, tabs: Tuple[Tuple[str, str], ...]) -> None:
        if tabs == self._tabs:
            return
        self._tabs = tabs
        self._rebuilding = True
        try:
            self._segmented.clear()
            for key, title in tabs:
                self._segmented.addItem(key, title)
        finally:
            self._rebuilding = False
        self._tab_keys = [k for k, _t in tabs]

    def _fill_active_tab(self) -> None:
        """把当前记录的内容填进激活 tab 对应的页面。"""
        if self._current_idx is None or self._current_idx >= len(self._records):
            return
        rec = self._records[self._current_idx]
        key = self._active_tab

        if key == "system":
            self._set_content(self._page_text, self._system_prompt_text(rec))
        elif key in ("content", "preview"):
            self._set_content(self._page_text, rec.raw or "（空）")
        elif key == "request":
            self._set_content(self._page_request, self._tool_request(rec))
        elif key == "response":
            self._set_content(self._page_response, self._tool_response(rec))
        elif key == "raw":
            self._set_content(self._page_raw, pretty_json(rec.raw or "（空）"))
        elif key == "tools":
            self._page_tools.set_schemas(self._tools_schemas)
        elif key == "headers":
            self._page_kv.set_rows(self._headers_rows(rec))
        elif key == "timing":
            self._page_kv.set_rows(self._timing_rows(rec), timing=self._timing(rec))

    def _refresh_current(self) -> None:
        if not self._records or self._current_idx is None or self._current_idx >= len(self._records):
            self._apply_idle()
            return
        rec = self._records[self._current_idx]

        self._rebuild_tabs(_TABS_BY_KIND.get(rec.kind, _TABS_BY_KIND[EntryKind.USER]))
        # 切换条目类型后原 tab 可能不存在 → 回落到第一个 tab。
        # ⚠️ clear() 会把 currentRouteKey 置空但不发信号，所以**每次重建后都要
        # 显式 setCurrentItem**，否则 segmented 无高亮而 stack 仍停在旧页面。
        if self._active_tab not in self._tab_keys:
            self._active_tab = self._tab_keys[0]
        self._rebuilding = True
        try:
            self._segmented.setCurrentItem(self._active_tab)
        finally:
            self._rebuilding = False
        slot = _SLOT_OF_TAB.get(self._active_tab)
        if slot is not None:
            self._stack.setCurrentWidget(self._slots[slot])

        self._refresh_badge()
        turn_part = f"Turn {rec.turn_no} · " if rec.turn_no > 0 else ""
        self._title_label.setText(f"{turn_part}{rec.label}")
        bits = [
            rec.status,
            format_duration(rec.duration_ms) if rec.duration_ms > 0 else "—",
            rec.absolute_time,
            f"{len(rec.raw or ''):,} 字符",
        ]
        self._meta_label.setText("  ·  ".join(bits))
        self._fill_active_tab()

    def _refresh_badge(self) -> None:
        if self._current_idx is None or self._current_idx >= len(self._records):
            self._badge.setText("----")
            self._badge.setStyleSheet(self._badge_qss(QColor("#888888")))
            return
        rec = self._records[self._current_idx]
        color = self._pal.danger if rec.is_error else kind_color(rec.kind)
        self._badge.setText(rec.kind.label)
        self._badge.setStyleSheet(self._badge_qss(color))

    def _apply_idle(self) -> None:
        self._rebuild_tabs(_TABS_BY_KIND[EntryKind.USER])
        self._active_tab = "content"
        self._rebuilding = True
        try:
            self._segmented.setCurrentItem("content")
        finally:
            self._rebuilding = False
        self._stack.setCurrentWidget(self._page_text)
        self._badge.setText("----")
        self._badge.setStyleSheet(self._badge_qss(QColor("#888888")))
        self._title_label.setText("未选中条目")
        self._meta_label.setText("点击左侧任意条目查看完整内容")
        for edit in (self._page_text, self._page_request, self._page_response, self._page_raw):
            self._set_content(edit, "")
        self._page_kv.set_rows([])

    # ──────────────────── 内容拼装 ────────────────────

    def _system_prompt_text(self, rec: TraceRecord) -> str:
        """SYSTEM → System Prompt：会话系统提示词 + agent 提示词分段。"""
        parts: List[str] = []
        sections = [(t, b) for t, b in self._system_sections if (b or "").strip()]
        if sections:
            for title, body in sections:
                parts.append(f"── {title} ──\n\n{body.strip()}\n")
        elif (rec.raw or "").strip():
            parts.append(rec.raw.strip())
        else:
            parts.append("（当前会话没有系统提示词）")
        return "\n\n".join(parts)

    @staticmethod
    def _tool_request(rec: TraceRecord) -> str:
        args = rec.meta.get("arguments") or ""
        if not args and rec.raw:
            args = rec.raw.split("\n\n── result ──\n")[0]
        if args:
            return pretty_json(args)
        # 入参为空：区分「占位（流式还没接收完）」和「真的没有参数」
        if rec.meta.get("args_placeholder"):
            return "（参数仍在接收中，尚未拿到完整入参）"
        return "（该工具调用没有入参）"

    @staticmethod
    def _tool_response(rec: TraceRecord) -> str:
        result = rec.meta.get("result")
        if result:
            return pretty_json(str(result))
        raw = rec.raw or ""
        if "\n\n── result ──\n" in raw:
            return pretty_json(raw.split("\n\n── result ──\n", 1)[1])
        return raw or "（无结果）"

    def _headers_rows(self, rec: TraceRecord) -> List[Tuple[str, str]]:
        rows: List[Tuple[str, str]] = [
            ("Kind", rec.kind.label),
            ("Label", rec.label),
            ("Status", rec.status),
            ("Source", rec.source or "-"),
            ("Turn", str(rec.turn_no) if rec.turn_no > 0 else "-"),
            ("Size", f"{len(rec.raw or ''):,} 字符"),
        ]
        for k, v in (rec.meta or {}).items():
            if k in ("arguments", "result", "turn_start"):
                continue
            rows.append((k, str(v)))
        return rows

    def _timing_rows(self, rec: TraceRecord) -> List[Tuple[str, str]]:
        t0, _t1 = self._bounds
        offset_ms = max(0.0, (rec.start_ts - t0) * 1000) if rec.start_ts > 0 and t0 > 0 else 0.0
        rows = [
            ("Start", rec.absolute_time),
            # Duration = 真实耗时（TOOL/ASSISTANT 由实时信号回填；消息类为 0）
            ("Duration", format_duration(rec.duration_ms) if rec.duration_ms > 0 else "—"),
            # Span = 时间线上的占用宽度（瞬时项 —，封顶项 ≥3.00 s）
            ("Span", rec.span_label),
            ("Offset", format_duration(int(offset_ms)) if offset_ms > 0 else "—"),
            ("End", _hms(rec.span_end_ts) if rec.span_end_ts > 0 else "—"),
            ("Status", rec.status),
        ]
        # 分阶段耗时（仅 TOOL）：perm 里包含权限弹窗等待用户点确认的时间，
        # 这段原本完全不可见（worker 侧是 sleep(0.1) 轮询）。
        phases = rec.meta.get("phases")
        if isinstance(phases, dict) and phases:
            rows.append(("── 分阶段 ──", ""))
            for key, title in (
                ("perm", "权限等待"),
                ("exec", "执行"),
                ("other", "其它"),
                ("total", "合计"),
            ):
                val = phases.get(key)
                if isinstance(val, (int, float)):
                    rows.append((title, format_duration(int(val))))
        if rec.meta.get("tool_call_id"):
            rows.append(("Tool Call", str(rec.meta["tool_call_id"])))
        return rows

    def _timing(self, rec: TraceRecord) -> Tuple[float, float, float]:
        t0, t1 = self._bounds
        total_ms = max(1.0, (t1 - t0) * 1000)
        offset = max(0.0, (rec.start_ts - t0) * 1000) if rec.start_ts > 0 else 0.0
        # 条带按 span（占用）画，与列表 Waterfall 保持一致
        dur = max(0.0, float(rec.span_ms))
        return offset, dur, total_ms


class _CloseButton(QFrame):
    """标题栏 × 按钮（自绘，跟随主题）。"""

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setFixedSize(24, 24)
        self.setCursor(Qt.PointingHandCursor)
        self._pal = ThemePalette()
        self._hover = False

    def set_palette(self, pal: ThemePalette) -> None:
        self._pal = pal
        self.update()

    def enterEvent(self, _e) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, _e) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self.clicked.emit()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            if self._hover:
                p.setPen(Qt.NoPen)
                p.setBrush(self._pal.line_at(26))
                p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 5, 5)
            p.setPen(QPen(QColor(self._pal.text_muted), 1.8))
            r = self.rect().adjusted(8, 8, -8, -8)
            p.drawLine(r.topLeft(), r.bottomRight())
            p.drawLine(r.topRight(), r.bottomLeft())
        finally:
            p.end()


def _hms(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
