# -*- coding: utf-8 -*-
"""排队消息卡片 —— 智能体运行时用户排队的待发送消息

行为契约
--------
- 主程序通过 CardManager 显隐本卡片；队列空时主程序 hide_card；
- 每条排队消息一行：文本（15px 加粗）+「插入」「编辑」「删除」三个图标按钮；
- 「插入」→ insertRequested(msg_id)：立即注入当前对话流（不停 worker）；
- 「编辑」→ 行内编辑，回车/失焦保存 emit editRequested(msg_id, new_text)，Esc 取消；
- 「删除」→ removeRequested(msg_id)：移出队列；
- 卡片不自管数据，set_entries 全量刷新（单一数据源在 main_widget）；
- 无 TTL（区别于 UndoDeleteCard）。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css
from qfluentwidgets import FluentIcon, TransparentToolButton


def summarize_entry_text(text: str, limit: int = 80) -> str:
    """排队条目摘要：压缩空白 + 单行截断"""
    collapsed = " ".join((text or "").split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


class QueueMessageCard(QWidget):
    """排队消息卡片（标题行 + 多条排队消息）"""

    insertRequested = pyqtSignal(str)  # msg_id：立即注入该条（不停 worker）
    removeRequested = pyqtSignal(str)  # msg_id：移出队列
    editRequested = pyqtSignal(str, str)  # msg_id, new_text：编辑保存

    def __init__(self, parent=None):
        super().__init__(parent)
        self._editor: QLineEdit | None = None  # 当前行内编辑器
        self._editing_id: str | None = None
        self.setVisible(False)
        self._setup_ui()

    # ───────────────────────────────────────────────────────────
    # 样式
    # ───────────────────────────────────────────────────────────

    def refresh_style(self):
        """刷新样式（主题切换 / 字号变更时调用）"""
        Colors.refresh()
        self.setStyleSheet(f"""
            QueueMessageCard {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }}
        """)
        for label in (self._title_label, self._count_label, self._hint_label):
            label.setStyleSheet(f"""
                QLabel {{
                    color: {Colors.TEXT_SECONDARY};
                    {get_font_family_css()} {font_size_css(13)};
                    background: transparent;
                }}
            """)
        self._refresh_rows_style()

    def _refresh_rows_style(self):
        """条目行样式（行控件在 set_entries 时动态创建，统一在此应用）"""
        for row in self._list_container.findChildren(QWidget):
            if row.objectName() != "queueRow":
                continue
            row.setStyleSheet(f"""
                QWidget#queueRow {{
                    border-top: 1px solid {Colors.REALTIME_BORDER};
                    background: transparent;
                }}
                QWidget#queueRow:hover {{
                    background: {Colors.HOVER_BG};
                }}
            """)
            for label in row.findChildren(QLabel):
                if label.objectName() == "queueRowText":
                    label.setStyleSheet(f"""
                        QLabel#queueRowText {{
                            color: {Colors.TEXT_PRIMARY};
                            {get_font_family_css()} {font_size_css(15)};
                            font-weight: bold;
                            background: transparent;
                        }}
                    """)
            for edit in row.findChildren(QLineEdit):
                edit.setStyleSheet(f"""
                    QLineEdit {{
                        background: {Colors.HOVER_BG};
                        border: 1px solid {Colors.TAG_ACCENT};
                        border-radius: 6px;
                        padding: 4px 8px;
                        {get_font_family_css()} {font_size_css(15)};
                        font-weight: bold;
                        color: {Colors.TEXT_PRIMARY};
                    }}
                    QLineEdit:focus {{
                        border: 1px solid {Colors.TAG_ACCENT};
                    }}
                """)

    # ───────────────────────────────────────────────────────────
    # 构建
    # ───────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题行
        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 8, 16, 8)
        header_layout.setSpacing(6)
        self._title_label = QLabel("⏳ 排队消息", header)
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header_layout.addWidget(self._title_label)
        self._count_label = QLabel("", header)
        self._count_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header_layout.addWidget(self._count_label)
        header_layout.addStretch()
        self._hint_label = QLabel("智能体结束后自动依次发送", header)
        self._hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header_layout.addWidget(self._hint_label)
        root.addWidget(header)

        # 条目区
        self._list_container = QWidget(self)
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        root.addWidget(self._list_container)

        # 样式必须在子控件创建后应用（refresh_style 直接引用这些属性）
        self.refresh_style()

    # ───────────────────────────────────────────────────────────
    # 对外 API
    # ───────────────────────────────────────────────────────────

    def set_entries(self, entries: list):
        """全量重建条目列表

        Args:
            entries: [{"id": str, "text": str}]，顺序即发送顺序
        """
        self._editor = None
        self._editing_id = None
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for entry in entries:
            self._list_layout.addWidget(self._build_row(entry))
        self._count_label.setText(f"· {len(entries)} 条" if len(entries) > 1 else "")
        self._refresh_rows_style()

    def _build_row(self, entry: dict) -> QWidget:
        row = QWidget(self._list_container)
        row.setObjectName("queueRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(16, 10, 12, 10)
        layout.setSpacing(4)
        msg_id = str(entry.get("id", ""))
        raw_text = str(entry.get("text", ""))
        row.setProperty("msg_id", msg_id)

        text_label = QLabel(summarize_entry_text(raw_text), row)
        text_label.setObjectName("queueRowText")
        text_label.setToolTip(raw_text)
        layout.addWidget(text_label, stretch=1)

        # 插入：立即注入当前对话流（不停 worker）
        insert_btn = TransparentToolButton(FluentIcon.SEND.icon(), row)
        insert_btn.setFixedSize(28, 28)
        insert_btn.setToolTip("立即插入当前对话流（不打断智能体）")
        insert_btn.clicked.connect(lambda _=False, mid=msg_id: self.insertRequested.emit(mid))
        layout.addWidget(insert_btn)

        # 编辑：行内编辑待发送文本
        edit_btn = TransparentToolButton(FluentIcon.EDIT.icon(), row)
        edit_btn.setFixedSize(28, 28)
        edit_btn.setToolTip("编辑消息")
        edit_btn.clicked.connect(
            lambda _=False, r=row, mid=msg_id, t=raw_text: self._begin_edit(r, mid, t)
        )
        layout.addWidget(edit_btn)

        # 删除：移出队列
        delete_btn = TransparentToolButton(FluentIcon.DELETE.icon(), row)
        delete_btn.setFixedSize(28, 28)
        delete_btn.setToolTip("移出队列")
        delete_btn.clicked.connect(lambda _=False, mid=msg_id: self.removeRequested.emit(mid))
        layout.addWidget(delete_btn)
        return row

    # ───────────────────────────────────────────────────────────
    # 行内编辑
    # ───────────────────────────────────────────────────────────

    def _begin_edit(self, row: QWidget, msg_id: str, current_text: str):
        """把该行文本切换为行内编辑器（回车/失焦保存，Esc 取消）"""
        if self._editor is not None:
            return
        layout = row.layout()
        label = row.findChild(QLabel, "queueRowText")
        if layout is None or label is None:
            return
        editor = QLineEdit(current_text, row)
        editor.returnPressed.connect(lambda: self._commit_edit(editor))
        editor.installEventFilter(self)
        index = layout.indexOf(label)
        layout.insertWidget(index, editor, stretch=1)
        label.hide()
        self._editor = editor
        self._editing_id = msg_id
        self._refresh_rows_style()
        editor.setFocus()
        editor.selectAll()

    def _commit_edit(self, editor: QLineEdit):
        """保存编辑（回车 / 失焦）"""
        if editor is not self._editor:
            return
        new_text = editor.text().strip()
        msg_id = self._editing_id or ""
        # 先清引用再 emit：set_entries 全量重建会移除编辑器
        self._editor = None
        self._editing_id = None
        if new_text:
            self.editRequested.emit(msg_id, new_text)
        else:
            # 空文本视为取消：全量重建还原显示
            self.set_entries(self._entries_snapshot())

    def _cancel_edit(self):
        """取消编辑（Esc）：丢弃修改，还原显示"""
        if self._editor is None:
            return
        self._editor = None
        self._editing_id = None
        self.set_entries(self._entries_snapshot())

    def _entries_snapshot(self) -> list:
        """从当前行还原条目快照（取消编辑时用；编辑中的行保留原文）"""
        entries = []
        for row in self._list_container.findChildren(QWidget):
            if row.objectName() != "queueRow":
                continue
            label = row.findChild(QLabel, "queueRowText")
            if label is None:
                continue
            entries.append({"id": row.property("msg_id") or "", "text": label.toolTip()})
        return entries

    def eventFilter(self, watched, event):
        from PyQt5.QtCore import QEvent

        if event.type() == QEvent.KeyPress and watched is self._editor:
            from PyQt5.QtGui import QKeyEvent

            if event.key() == Qt.Key_Escape:
                self._cancel_edit()
                return True
        elif event.type() == QEvent.FocusOut and watched is self._editor:
            self._commit_edit(self._editor)
            return True
        return super().eventFilter(watched, event)

    # ───────────────────────────────────────────────────────────
    # 显隐（由 CardManager 调用）
    # ───────────────────────────────────────────────────────────

    def show_card(self):
        """显示卡片（容器默认隐藏，显示动作自负责）"""
        self.setVisible(True)

    def hide_card(self):
        """隐藏卡片"""
        self.setVisible(False)
