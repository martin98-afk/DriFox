# -*- coding: utf-8 -*-
"""系统插件版产物页（SystemArtifactsPage）

注册到 WorkbenchPanel 的 "system_artifacts" tab（由 plugins/system/ui/__init__.py
register_ui 完成），作为 register_workbench_tab 通道的示例。

与内置产物页（app.widgets.workbench_panel.ArtifactsPage）功能对齐，但
通过插件通道加载，便于后续彻底替代内置版。区别：
- 数据由 context 注入（context["backend"] / context["session_id"]）
- 差异请求通过 context["diff_requested_callback"] 转发
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, TransparentToolButton

# 复用内置 _EmptyHint / _SectionHeader（共享模块，避免重复定义）
from app.widgets._workbench_helpers import _EmptyHint, _SectionHeader

from app.utils.design_tokens import BorderRadius, Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_icon


def _relative_time(created_at: str) -> str:
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return created_at or ""
    delta = datetime.now() - dt
    seconds = delta.total_seconds()
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 86400 * 7:
        return f"{int(seconds // 86400)} 天前"
    return dt.strftime("%m-%d %H:%M")


class _SystemArtifactItem(QFrame):
    """单条产物条目（系统插件版，复用内置样式）"""

    diff_requested = pyqtSignal(list)

    def __init__(self, op: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.setObjectName("systemArtifactItem")
        self._file_path = op.get("file_path", "")
        self.setToolTip(self._file_path)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 6, 5)
        layout.setSpacing(8)
        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name = Path(self._file_path).name or self._file_path
        self._name_label = QLabel(name, self)
        self._meta_label = QLabel(
            f"{op.get('tool_name', '')} · {_relative_time(op.get('created_at', ''))}", self
        )
        text_col.addWidget(self._name_label)
        text_col.addWidget(self._meta_label)
        layout.addLayout(text_col, 1)
        self._diff_btn = TransparentToolButton(get_icon("差异对比"), self)
        self._diff_btn.setToolTip("查看该文件差异")
        self._diff_btn.setFixedSize(24, 24)
        self._diff_btn.clicked.connect(self._emit_diff)
        layout.addWidget(self._diff_btn)
        self.refresh_style()

    def refresh_style(self) -> None:
        self.setStyleSheet(
            "QFrame#systemArtifactItem {"
            f" background: {Colors.CARD_BG.format(alpha=120)};"
            f" border: 1px solid {Colors.BORDER};"
            f" border-radius: {BorderRadius.MD};"
            " }"
            "QFrame#systemArtifactItem:hover {"
            f" background: {Colors.HOVER_BG};"
            f" border-color: {Colors.BORDER_ACCENT}; }}"
            f" QLabel {{ color: {Colors.TEXT_PRIMARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(12)}; }}"
        )
        self._name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(12)};"
        )
        self._meta_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; background: transparent; {get_font_family_css()} {font_size_css(11)};"
        )

    def _emit_diff(self) -> None:
        if self._file_path:
            self.diff_requested.emit([self._file_path])


class SystemArtifactsPage(QWidget):
    """产物页（系统插件版）

    通过 context 接收数据与回调：
    - context["backend"]: 当前会话 backend（含 file_recorder + session_store）
    - context["session_id"]: 当前会话 id（None 表示无）
    - context["diff_requested_callback"]: 差异回调 (file_paths: Optional[List[str]]) -> None
    """

    def __init__(self, parent=None, context: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self._context = context or {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._header = _SectionHeader("产物", "根目录", self)
        layout.addWidget(self._header)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }\n" + get_unified_scrollbar_style(6)
        )
        self._list_wrap = QWidget()
        self._list_wrap.setStyleSheet("background: transparent;")
        self._scroll.setWidget(self._list_wrap)
        self._list_layout = QVBoxLayout(self._list_wrap)
        self._list_layout.setContentsMargins(0, 0, 2, 0)
        self._list_layout.setSpacing(2)
        self._empty_hint = _EmptyHint("本次会话暂无产物（系统插件版）", self._list_wrap)
        self._list_layout.addWidget(self._empty_hint)
        self._list_layout.addStretch(1)
        layout.addWidget(self._scroll, 1)
        self._header.hide_action()
        # 「查看所有产物差异」按钮
        self._header.set_action("差异对比", "查看所有产物差异", self._emit_diff_all)
        # 构造期不拉数据：context 里的 backend/session_id 未必就绪，
        # 由宿主 refresh_workbench → panel.update_artifacts(ops) 推送。

    def _emit_diff_all(self) -> None:
        cb = self._context.get("diff_requested_callback")
        if cb:
            try:
                cb(None)
            except Exception:
                pass

    def get_operations(self) -> List[Dict[str, Any]]:
        """从 context 拉数据（宿主未推送时的备用路径）"""
        backend = self._context.get("backend")
        session_id = self._context.get("session_id")
        if backend is None or not session_id:
            return []
        try:
            recorder = getattr(backend, "file_recorder", None)
            if recorder is None:
                return []
            return recorder.get_all_operations_for_session(session_id)
        except Exception:
            return []

    def refresh_data(self, operations: Optional[List[Dict[str, Any]]] = None) -> None:
        """渲染产物列表

        Args:
            operations: 宿主推送的文件操作记录；为 None 时从 context 自行拉取。
        """
        if operations is None:
            operations = self.get_operations()
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None and w is not self._empty_hint:
                w.deleteLater()
        latest: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for op in operations or []:
            fp = op.get("file_path") or ""
            if not fp:
                continue
            if fp not in latest:
                order.append(fp)
            latest[fp] = op
        ordered = [latest[fp] for fp in reversed(order)]
        self._empty_hint.setVisible(not ordered)
        self._header.set_extra(f"{len(ordered)} 个文件" if ordered else "")
        if ordered:
            self._header.show_action()
        else:
            self._header.hide_action()
        for op in ordered:
            item = _SystemArtifactItem(op, self._list_wrap)
            item.diff_requested.connect(self._on_item_diff)
            self._list_layout.addWidget(item)

    def set_operations(self, operations: List[Dict[str, Any]]) -> None:
        """宿主数据入口（与内置 ArtifactsPage 同名契约）

        WorkbenchPanel.update_artifacts 会调 ``artifacts_page.set_operations(ops)``，
        插件版必须实现同名方法才能被覆盖替换。
        """
        self.refresh_data(operations)

    def set_diff_all_callback(self, callback) -> None:
        """宿主注入差异回调（与内置 ArtifactsPage 同名契约）"""
        self._context["diff_requested_callback"] = callback

    def _on_item_diff(self, file_paths: List[str]) -> None:
        cb = self._context.get("diff_requested_callback")
        if cb:
            try:
                cb(file_paths)
            except Exception:
                pass

    def refresh_style(self) -> None:
        self._header.refresh_style()
        self._empty_hint.refresh_style()
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if isinstance(w, _SystemArtifactItem):
                w.refresh_style()
