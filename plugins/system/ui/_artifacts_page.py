# -*- coding: utf-8 -*-
"""系统插件版产物页（SystemArtifactsPage）

注册到 WorkbenchPanel 的 "system_artifacts" tab（由 plugins/system/ui/__init__.py
register_ui 完成），作为 register_workbench_tab 通道的示例。

与内置产物页（app.widgets.workbench_panel.ArtifactsPage）功能对齐，但
通过插件通道加载，便于后续彻底替代内置版。区别：
- 数据由 context 注入（context["backend"] / context["session_id"]）
- 差异请求通过 context["diff_requested_callback"] 转发

产物按「用户问题」分组：每个 user 消息开启一组，该轮 assistant 的
tool_calls（call_id）命中的文件操作归属该组；组头部可折叠/展开，并支持
一键查看组内全部文件差异。消息不可用时回退平铺列表。
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import TransparentToolButton

# 复用内置 _EmptyHint / _SectionHeader（共享模块，避免重复定义）
from app.widgets._workbench_helpers import _EmptyHint, _SectionHeader
from app.widgets.elided_label import _ElidedLabel

from app.utils.design_tokens import BorderRadius, Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_icon


def _relative_time(created_at: str) -> str:
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except TypeError, ValueError:
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


def _message_text(content: Any) -> str:
    """提取消息文本（兼容 str 与多模态 list 两种 content 形态）"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text") or ""
                if t:
                    parts.append(str(t))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts).strip()
    return ""


def _is_hook_message(msg: Dict[str, Any]) -> bool:
    """判断是否为 hook 注入的 user 消息（不开新分组，只有用户真实输入开组）

    特征：_hook_event 结构化标记（主判）；旧数据无该字段时以
    <system-reminder> 包裹 + <xxx-hook> 标记兜底。
    """
    if msg.get("_hook_event"):
        return True
    content = msg.get("content")
    return isinstance(content, str) and content.lstrip().startswith("<system-reminder>") and "-hook>" in content


def _question_summary(content: Any, limit: int = 80) -> str:
    """用户问题摘要：取首个非空行并截断"""
    text = _message_text(content)
    for line in text.splitlines():
        line = line.strip()
        if line:
            text = line
            break
    text = text.replace("\n", " ").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


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
        self._name_label = _ElidedLabel(name, self)
        self._meta_label = QLabel(f"{op.get('tool_name', '')} · {_relative_time(op.get('created_at', ''))}", self)
        text_col.addWidget(self._name_label)
        text_col.addWidget(self._meta_label)
        layout.addLayout(text_col, 1)
        self._open_btn = TransparentToolButton(get_icon("read"), self)
        self._open_btn.setToolTip("打开文件")
        self._open_btn.setFixedSize(24, 24)
        self._open_btn.clicked.connect(self._open_file)
        layout.addWidget(self._open_btn)
        self._folder_btn = TransparentToolButton(get_icon("folder"), self)
        self._folder_btn.setToolTip("打开文件路径")
        self._folder_btn.setFixedSize(24, 24)
        self._folder_btn.clicked.connect(self._reveal_file)
        layout.addWidget(self._folder_btn)
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

    def _open_file(self) -> None:
        """用系统默认程序打开产物文件"""
        p = Path(self._file_path)
        if not self._file_path or not p.exists():
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception:
            pass

    def _reveal_file(self) -> None:
        """在文件管理器中定位产物文件"""
        if not self._file_path:
            return
        p = Path(self._file_path)
        try:
            if sys.platform == "win32":
                if p.exists():
                    subprocess.Popen(["explorer", "/select,", str(p)])
                elif p.parent.exists():
                    subprocess.Popen(["explorer", str(p.parent)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(p)])
            elif p.parent.exists():
                subprocess.Popen(["xdg-open", str(p.parent)])
        except Exception:
            pass


class _QuestionGroupCard(QFrame):
    """单个用户问题分组卡片：问题摘要 + 文件数 + 组差异按钮 + 可折叠文件列表"""

    diff_requested = pyqtSignal(list)

    def __init__(self, question: str, ops: List[Dict[str, Any]], expanded: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("questionGroupCard")
        self._expanded = expanded
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 6, 6)
        root.setSpacing(4)

        # ── 头部：箭头 + 问题摘要 + 统计 + 组差异按钮 ──
        header = QWidget(self)
        header.setCursor(Qt.PointingHandCursor)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(6)
        self._arrow_label = QLabel(header)
        self._arrow_label.setFixedSize(14, 14)
        self._arrow_label.setScaledContents(True)
        self._question_label = _ElidedLabel(question, header)
        self._question_label.setToolTip(question)
        self._count_label = QLabel(f"{len(ops)} 个文件", header)
        h_layout.addWidget(self._arrow_label)
        h_layout.addWidget(self._question_label, 1)
        h_layout.addWidget(self._count_label)
        self._diff_btn = TransparentToolButton(get_icon("差异对比"), header)
        self._diff_btn.setToolTip("查看该问题下全部文件差异")
        self._diff_btn.setFixedSize(24, 24)
        self._diff_btn.clicked.connect(self._emit_diff)
        h_layout.addWidget(self._diff_btn)
        root.addWidget(header)

        # ── 主体：该问题下的产物条目（最新在前） ──
        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(14, 0, 0, 0)
        self._body_layout.setSpacing(2)
        for op in reversed(ops):
            item = _SystemArtifactItem(op, self._body)
            item.diff_requested.connect(self.diff_requested)
            self._body_layout.addWidget(item)
        root.addWidget(self._body)

        header.mousePressEvent = lambda _e: self.toggle()  # type: ignore[assignment]
        self._apply_expand()
        self.refresh_style()

    # ── 折叠 ──

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._apply_expand()

    def _apply_expand(self) -> None:
        self._body.setVisible(self._expanded)
        # 收起态显示「展开」箭头，展开态显示「折叠」箭头
        # 状态语义：折叠态显示向右箭头（▶，点击展开），展开态显示向下箭头（▼）
        self._arrow_label.setPixmap(get_icon("折叠" if not self._expanded else "展开").pixmap(14, 14))

    def _emit_diff(self) -> None:
        paths: List[str] = []
        for i in range(self._body_layout.count()):
            item = self._body_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, _SystemArtifactItem) and w._file_path:
                paths.append(w._file_path)
        if paths:
            self.diff_requested.emit(paths)

    # ── 样式 ──

    def refresh_style(self) -> None:
        self.setStyleSheet(
            "QFrame#questionGroupCard {"
            f" background: {Colors.CARD_BG.format(alpha=60)};"
            " border: none;"
            f" border-radius: {BorderRadius.MD};"
            " }"
            f" QLabel {{ background: transparent; {get_font_family_css()} }}"
        )
        self._question_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(12)};"
        )
        self._count_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; background: transparent; {get_font_family_css()} {font_size_css(11)};"
        )
        for i in range(self._body_layout.count()):
            item = self._body_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, _SystemArtifactItem):
                w.refresh_style()


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
        self._list_layout.setSpacing(4)
        self._empty_hint = _EmptyHint("本次会话暂无产物", self._list_wrap)
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
        """渲染产物列表（按用户问题分组，消息不可用时回退平铺）

        Args:
            operations: 宿主推送的文件操作记录；为 None 时从 context 自行拉取。
        """
        if operations is None:
            operations = self.get_operations()
        self._clear_list()
        groups, flat_mode = self._build_groups(operations or [])
        if flat_mode:
            self._render_flat(operations or [])
        else:
            self._render_groups(groups)

    # ── 布局清理 ──

    def _clear_list(self) -> None:
        """全部移出布局：条目销毁；empty_hint（实例复用）与 spacer 仅移出，随后统一重建。"""
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None and w is not self._empty_hint:
                w.deleteLater()
            del item  # spacer 等 non-widget item 的 C++ 所有权已转到 Python，及时释放

    # ── 分组构建 ──

    def _build_groups(self, operations: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
        """按用户问题分组文件操作

        Returns:
            (groups, flat_mode)。groups 元素: {"question": str, "ops": list}，
            时间倒序（最新问题在前）；flat_mode=True 表示应回退平铺渲染。
        """
        session = None
        try:
            # 宿主 _build_ui_context 不注入 backend，需从 main_widget 兜底获取
            backend = self._context.get("backend")
            if backend is None:
                backend = getattr(self._context.get("main_widget"), "backend", None)
            session = backend.get_current_session() if backend is not None else None
        except Exception:
            session = None
        if session is None:
            return [], True
        messages = getattr(session, "messages", None) or []

        # 1) 遍历消息：user 开新组，assistant 的 tool_calls 归入当前组
        groups: List[Dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "user" and not _is_hook_message(msg):
                groups.append(
                    {
                        "question": _question_summary(msg.get("content")) or "（空问题）",
                        "call_ids": set(),
                        "files": {},
                    }
                )
            elif role == "assistant" and groups:
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        groups[-1]["call_ids"].add(tc["id"])
        if not groups:
            return [], True

        # 2) 分配文件操作：call_id 命中归组；未命中（compaction 裁剪/旧数据）进兜底组
        call_to_group: Dict[str, int] = {}
        for i, g in enumerate(groups):
            for cid in g["call_ids"]:
                call_to_group[cid] = i
        orphan: Dict[str, Dict[str, Any]] = {}
        for op in operations:
            fp = op.get("file_path") or ""
            if not fp:
                continue
            cid = op.get("call_id")
            idx = call_to_group.get(cid) if isinstance(cid, str) else None
            target = groups[idx]["files"] if idx is not None else orphan
            target[fp] = op  # 同文件多次操作只保留最新一条

        # 3) 组装结果：有产物的组按时间倒序（最新问题在前），兜底组固定排最后
        result = list(
            reversed([{"question": g["question"], "ops": list(g["files"].values())} for g in groups if g["files"]])
        )
        if orphan:
            result.append({"question": "更早的产物（未关联到当前问题）", "ops": list(orphan.values())})
        if not result:
            return [], True
        return result, False

    # ── 渲染 ──

    def _render_groups(self, groups: List[Dict[str, Any]]) -> None:
        """按问题分组渲染（最新组在前且默认展开）"""
        total_files = sum(len(g["ops"]) for g in groups)
        self._empty_hint.setVisible(not groups)
        self._header.set_extra(f"{total_files} 个文件" if groups else "")
        if groups:
            self._header.show_action()
        else:
            self._header.hide_action()
        self._list_layout.addWidget(self._empty_hint)
        for i, g in enumerate(groups):
            card = _QuestionGroupCard(g["question"], g["ops"], expanded=(i == 0), parent=self._list_wrap)
            card.diff_requested.connect(self._on_item_diff)
            self._list_layout.addWidget(card)
        self._list_layout.addStretch(1)

    def _render_flat(self, operations: List[Dict[str, Any]]) -> None:
        """平铺渲染（消息不可用时的回退；与旧版行为一致）"""
        latest: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for op in operations:
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
        self._list_layout.addWidget(self._empty_hint)
        for op in ordered:
            item = _SystemArtifactItem(op, self._list_wrap)
            item.diff_requested.connect(self._on_item_diff)
            self._list_layout.addWidget(item)
        self._list_layout.addStretch(1)

    # ── 宿主契约（与内置 ArtifactsPage 同名） ──

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
            if isinstance(w, (_SystemArtifactItem, _QuestionGroupCard)):
                w.refresh_style()
