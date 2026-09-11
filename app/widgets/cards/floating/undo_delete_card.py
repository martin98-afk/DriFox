# -*- coding: utf-8 -*-
"""撤销删除卡片 —— 消息删除 / 撤销后提供回退入口

行为契约
--------
- 主程序通过 ``CardManager`` 显隐本卡片；卡片**不自作主张** ``setVisible``，
  避免出现 widget 可见性与 CardManager 记录失配（旧实现的双状态源问题）；
- 用户点「撤销」→ ``restoreRequested``；用户点「✕」或 TTL 到期 →
  ``dismissRequested``（两者语义都是「撤销窗口关闭」）；
- ``hide_card()`` 由 CardManager 调用，只发 ``dismissed`` 通知、**不清空**
  回退条目 —— 被别的卡片遮挡不等于用户放弃撤销；
- TTL 固定 :data:`UndoDeleteCard.TTL_SECONDS`（60s）自动消失，鼠标悬停时暂停，
  移开后按剩余时间续跑，避免用户正在读文案时卡片突然消失。

参考 CommandCard 的样式设计。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css


class UndoDeleteCard(QWidget):
    """撤销删除卡片（单行紧凑：文案 + 撤销 + 关闭）"""

    # 撤销窗口时长（秒）：到期后条目整体失效、卡片淡出
    TTL_SECONDS = 60

    restoreRequested = pyqtSignal()  # 用户点击撤销
    dismissRequested = pyqtSignal()  # 撤销窗口关闭（用户点 ✕ / TTL 到期）
    dismissed = pyqtSignal()  # 卡片被隐藏（CardManager 调用 hide_card）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ttl_ms = 0
        self._ttl_remaining = 0
        self._ttl_timer = QTimer(self)
        self._ttl_timer.setSingleShot(True)
        self._ttl_timer.timeout.connect(self._on_ttl_timeout)
        self.setVisible(False)
        self._setup_ui()

    # ───────────────────────────────────────────────────────────
    # 样式
    # ───────────────────────────────────────────────────────────

    def refresh_style(self):
        """刷新样式（主题切换 / 字号变更时调用）"""
        Colors.refresh()
        self.setStyleSheet(f"""
            UndoDeleteCard {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }}
        """)
        self._hint_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_SECONDARY};
                {get_font_family_css()} {font_size_css(13)};
                background: transparent;
            }}
        """)
        self._depth_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_SECONDARY};
                {get_font_family_css()} {font_size_css(12)};
                background: transparent;
            }}
        """)
        self._restore_btn.setStyleSheet(f"""
            QPushButton {{
                color: {Colors.TAG_ACCENT};
                {get_font_family_css()} {font_size_css(13)};
                font-weight: bold;
                background: transparent;
                border: none;
                padding: 0px 6px;
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
                color: {Colors.TAG_ACCENT_TEXT};
            }}
        """)
        self._close_btn.setStyleSheet(f"""
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

    # ───────────────────────────────────────────────────────────
    # 构建
    # ───────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 8, 0)
        layout.setSpacing(6)

        self._hint_label = QLabel("消息已删除", self)
        self._hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._hint_label)

        # 剩余可回退步数（仅多步时显示）
        self._depth_label = QLabel("", self)
        self._depth_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._depth_label.setVisible(False)
        layout.addWidget(self._depth_label)

        layout.addStretch()

        self._restore_btn = QPushButton("撤销", self)
        self._restore_btn.setObjectName("undoRestoreBtn")
        self._restore_btn.setCursor(Qt.PointingHandCursor)
        self._restore_btn.setFixedHeight(22)
        self._restore_btn.setToolTip("恢复被删除的消息")
        self._restore_btn.clicked.connect(self.restoreRequested.emit)
        layout.addWidget(self._restore_btn)

        self._close_btn = QPushButton("✕", self)
        self._close_btn.setObjectName("undoCloseBtn")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setToolTip("关闭")
        self._close_btn.clicked.connect(self.dismissRequested.emit)
        layout.addWidget(self._close_btn)

        # 样式必须在子控件创建后应用（refresh_style 直接引用这些属性）
        self.refresh_style()

    # ───────────────────────────────────────────────────────────
    # 对外 API
    # ───────────────────────────────────────────────────────────

    def set_entry(self, label: str, depth: int = 1, note: str = ""):
        """更新文案与可回退步数

        Args:
            label: 主文案（由 UndoEntry.label 生成，单一数据源）
            depth: 当前栈深（1 = 仅一步可回退）
            note: 被删内容摘要，作为 tooltip
        """
        self._hint_label.setText(label or "消息已删除")
        if depth > 1:
            self._depth_label.setText(f"· 可回退 {depth} 步")
            self._depth_label.setVisible(True)
        else:
            self._depth_label.setVisible(False)
        self.setToolTip(note or "")

    def restart_ttl(self):
        """（重新）开始撤销窗口计时"""
        self._ttl_ms = max(0, int(self.TTL_SECONDS)) * 1000
        self._ttl_remaining = self._ttl_ms
        if self._ttl_ms > 0:
            self._ttl_timer.start(self._ttl_ms)
        else:
            self._ttl_timer.stop()

    def show_card(self):
        """显示卡片（由 CardManager 调用）"""
        self.restart_ttl()
        self.setVisible(True)

    def hide_card(self):
        """隐藏卡片（由 CardManager 调用）

        注意：这里**不**通知主程序清空回退条目 —— 遮挡 ≠ 放弃。
        条目失效只由 dismissRequested（✕ / TTL）或会话切换触发。
        """
        self.setVisible(False)
        self.dismissed.emit()

    # ───────────────────────────────────────────────────────────
    # TTL
    # ───────────────────────────────────────────────────────────

    def _on_ttl_timeout(self):
        self._ttl_remaining = 0
        self.dismissRequested.emit()

    def enterEvent(self, event):
        # 悬停暂停：用户正在看文案时不该消失
        if self._ttl_timer.isActive():
            self._ttl_remaining = max(0, self._ttl_timer.remainingTime())
            self._ttl_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._ttl_remaining > 0:
            self._ttl_timer.start(self._ttl_remaining)
        super().leaveEvent(event)
