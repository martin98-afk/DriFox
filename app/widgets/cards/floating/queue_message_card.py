# -*- coding: utf-8 -*-
"""排队消息卡片 —— 智能体运行时用户排队的待发送消息

行为契约
--------
- 主程序通过 CardManager 显隐本卡片；队列空时主程序 hide_card；
- 每条排队消息一行：文本摘要 + 「立即插入」 + 「✕」；
- 「立即插入」→ insertRequested(msg_id)；「✕」→ removeRequested(msg_id)；
- 卡片不自管数据，set_entries 全量刷新（单一数据源在 main_widget）；
- 无 TTL（区别于 UndoDeleteCard）。

样式参考 UndoDeleteCard。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css


def summarize_entry_text(text: str, limit: int = 60) -> str:
    """排队条目摘要：压缩空白 + 单行截断"""
    collapsed = " ".join((text or "").split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


class QueueMessageCard(QWidget):
    """排队消息卡片（标题行 + 多条排队消息）"""

    insertRequested = pyqtSignal(str)  # msg_id：立即注入该条（不停 worker）
    removeRequested = pyqtSignal(str)  # msg_id：移出队列

    def __init__(self, parent=None):
        super().__init__(parent)
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
        for label in self._list_container.findChildren(QLabel):
            label.setStyleSheet(f"""
                QLabel {{
                    color: {Colors.TEXT_PRIMARY};
                    {get_font_family_css()} {font_size_css(13)};
                    background: transparent;
                }}
            """)
        self._refresh_rows_style()

    def _refresh_rows_style(self):
        """条目行样式（行控件在 set_entries 时动态创建，统一在此应用）"""
        for btn in self._list_container.findChildren(QPushButton):
            if btn.objectName() == "queueInsertBtn":
                btn.setStyleSheet(f"""
                    QPushButton {{
                        color: {Colors.TAG_ACCENT};
                        {get_font_family_css()} {font_size_css(12)};
                        font-weight: bold;
                        background: transparent;
                        border: none;
                        padding: 2px 8px;
                        border-radius: 4px;
                    }}
                    QPushButton:hover {{
                        color: {Colors.TAG_ACCENT_TEXT};
                        background: {Colors.HOVER_BG};
                    }}
                    QPushButton:pressed {{
                        background: {Colors.HOVER_BG_STRONG};
                    }}
                    QPushButton:focus {{
                        outline: none;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        color: {Colors.TEXT_SECONDARY};
                        {get_font_family_css()} {font_size_css(12)};
                        background: transparent;
                        border: none;
                        border-radius: 4px;
                    }}
                    QPushButton:hover {{
                        color: {Colors.TEXT_PRIMARY};
                        background: {Colors.HOVER_BG};
                    }}
                    QPushButton:pressed {{
                        background: {Colors.HOVER_BG_STRONG};
                    }}
                    QPushButton:focus {{
                        outline: none;
                    }}
                """)
        for row in self._list_container.findChildren(QWidget):
            if row.objectName() == "queueRow":
                row.setStyleSheet(f"""
                    QWidget#queueRow {{
                        border-top: 1px solid {Colors.REALTIME_BORDER};
                        background: transparent;
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
        header_layout.setContentsMargins(16, 7, 12, 7)
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
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for i, entry in enumerate(entries):
            self._list_layout.addWidget(self._build_row(entry, is_last=(i == len(entries) - 1)))
        self._count_label.setText(f"· {len(entries)} 条" if len(entries) > 1 else "")
        self._refresh_rows_style()

    def _build_row(self, entry: dict, is_last: bool) -> QWidget:
        row = QWidget(self._list_container)
        row.setObjectName("queueRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(16, 6, 12, 6)
        layout.setSpacing(10)
        msg_id = str(entry.get("id", ""))

        text_label = QLabel(summarize_entry_text(str(entry.get("text", ""))), row)
        text_label.setToolTip(str(entry.get("text", "")))
        layout.addWidget(text_label, stretch=1)

        insert_btn = QPushButton("立即插入", row)
        insert_btn.setObjectName("queueInsertBtn")
        insert_btn.setCursor(Qt.PointingHandCursor)
        insert_btn.setFixedHeight(22)
        insert_btn.setToolTip("立即把该条插入当前对话流（不打断智能体）")
        insert_btn.clicked.connect(lambda _=False, mid=msg_id: self.insertRequested.emit(mid))
        layout.addWidget(insert_btn)

        remove_btn = QPushButton("✕", row)
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.setFixedSize(20, 20)
        remove_btn.setToolTip("移出队列")
        remove_btn.clicked.connect(lambda _=False, mid=msg_id: self.removeRequested.emit(mid))
        layout.addWidget(remove_btn)
        return row

    def show_card(self):
        """显示卡片（由 CardManager 调用；容器默认隐藏，显示动作自负责）"""
        self.setVisible(True)

    def hide_card(self):
        """隐藏卡片（由 CardManager 调用）"""
        self.setVisible(False)
