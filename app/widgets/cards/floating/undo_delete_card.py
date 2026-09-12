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

设计说明（2026-09-12 视觉重做）
------------------------------
旧版 32px 单行：``REALTIME_BG`` 底（浅色主题下与对话区同色 → 卡片不可见）
+ 纯文字「撤销」按钮，观感就是"一行悬浮的文字"。

重做后：卡片自绘表面 + 左侧警示色圆点 + 「撤销」改为描边 pill 按钮 +
关闭按钮 icon-only + 底部 2px TTL 进度条（让"还剩多久"可感知，
避免用户错过撤销窗口）。视觉家族与 :class:`QueueMessageCard` 对齐。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon


class _TtlBar(QWidget):
    """卡片底部 TTL 进度条（只读展示，鼠标事件穿透）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ratio = 1.0
        self._color = "#2563eb"
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_ratio(self, ratio: float):
        ratio = max(0.0, min(1.0, float(ratio)))
        if abs(ratio - self._ratio) < 0.004:
            return
        self._ratio = ratio
        self.update()

    def set_color(self, color: str):
        self._color = color
        self.update()

    def paintEvent(self, event):
        if self._ratio <= 0.0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = QColor(self._color)
        c.setAlphaF(0.55)
        w = max(2, int(round(self.width() * self._ratio)))
        p.fillRect(0, 0, w, self.height(), c)


class UndoDeleteCard(QWidget):
    """撤销删除卡片（单行紧凑：状态点 + 文案 + 撤销 pill + 关闭）"""

    # 撤销窗口时长（秒）：到期后条目整体失效、卡片淡出
    TTL_SECONDS = 60

    # ── 视觉常量（与 QueueMessageCard 同族）──
    RADIUS = 10
    CARD_H = 36
    BAR_H = 2

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
        # 进度条刷新：只读 _ttl_timer.remainingTime()，
        # 因此悬停暂停（_ttl_timer.stop）会自动同步到进度条，无需额外同步逻辑
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(100)
        self._progress_timer.timeout.connect(self._tick_progress)
        self.setAttribute(Qt.WA_StyledBackground, True)
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
                background-color: {Colors.CARD_BG.format(alpha=250)};
                border: 1px solid {Colors.BORDER};
                border-radius: {self.RADIUS}px;
            }}
        """)
        self._dot.setStyleSheet(f"""
            QLabel#undoDot {{
                background: {Colors.REALTIME_ACCENT_WARM};
                border-radius: 3px;
            }}
        """)
        self._hint_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(13)};
                background: transparent;
            }}
        """)
        self._depth_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_SECONDARY};
                {get_font_family_css()} {font_size_css(11)};
                background: transparent;
            }}
        """)
        # 「撤销」：描边 pill 按钮（旧版纯文字按钮，无按键感）
        self._restore_btn.setStyleSheet(f"""
            QPushButton {{
                color: {Colors.TAG_ACCENT_TEXT};
                {get_font_family_css()} {font_size_css(12)};
                font-weight: 600;
                background: {Colors.REALTIME_TAG_BG};
                border: 1px solid {Colors.REALTIME_TAG_BORDER};
                border-radius: 11px;
                padding: 0px 10px;
            }}
            QPushButton:hover {{
                background: {Colors.REALTIME_TAG_BORDER};
                border: 1px solid {Colors.TAG_ACCENT};
            }}
            QPushButton:pressed {{
                background: {Colors.HOVER_BG_STRONG};
            }}
            QPushButton:focus {{
                outline: none;
            }}
        """)
        self._close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 6px;
            }}
            QPushButton:hover {{
                background: {Colors.HOVER_BG};
            }}
            QPushButton:pressed {{
                background: {Colors.HOVER_BG_STRONG};
            }}
            QPushButton:focus {{
                outline: none;
            }}
        """)
        self._close_btn.setIcon(get_icon("关闭"))
        self._ttl_bar.set_color(Colors.TAG_ACCENT)

    # ───────────────────────────────────────────────────────────
    # 构建
    # ───────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(self.CARD_H)

        layout = QHBoxLayout(self)
        # 底部留 3px 给进度条（圆角区外），右侧给关闭按钮留窄边距
        layout.setContentsMargins(12, 0, 8, 3)
        layout.setSpacing(8)

        self._dot = QLabel(self)
        self._dot.setObjectName("undoDot")
        self._dot.setFixedSize(6, 6)
        self._dot.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._dot)

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

        self._close_btn = QPushButton(self)
        self._close_btn.setObjectName("undoCloseBtn")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.setIconSize(QSize(14, 14))
        self._close_btn.setToolTip("关闭")
        self._close_btn.clicked.connect(self.dismissRequested.emit)
        layout.addWidget(self._close_btn)

        # TTL 进度条：绝对定位在卡片底部（左右内缩 3px 避开圆角）
        self._ttl_bar = _TtlBar(self)
        self._ttl_bar.setFixedHeight(self.BAR_H)

        # 样式必须在子控件创建后应用（refresh_style 直接引用这些属性）
        self.refresh_style()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        inset = 3
        self._ttl_bar.setGeometry(
            inset,
            max(0, self.height() - self.BAR_H - 2),
            max(0, self.width() - inset * 2),
            self.BAR_H,
        )

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
            self._ttl_bar.set_ratio(1.0)
            self._progress_timer.start()
        else:
            self._ttl_timer.stop()
            self._progress_timer.stop()
            self._ttl_bar.set_ratio(0.0)

    def _tick_progress(self):
        """按 TTL 剩余时间刷新进度条（悬停暂停由 _ttl_timer 状态自动体现）"""
        remain = self._ttl_timer.remainingTime()
        if remain < 0:
            # 计时器已停（悬停暂停）：沿用暂停时的剩余量，视觉保持不动
            remain = self._ttl_remaining
        total = max(1, self._ttl_ms)
        self._ttl_bar.set_ratio(remain / total)

    def show_card(self):
        """显示卡片（由 CardManager 调用）"""
        self.restart_ttl()
        self.setVisible(True)

    def hide_card(self):
        """隐藏卡片（由 CardManager 调用）

        注意：这里**不**通知主程序清空回退条目 —— 遮挡 ≠ 放弃。
        条目失效只由 dismissRequested（✕ / TTL）或会话切换触发。
        """
        self._progress_timer.stop()
        self.setVisible(False)
        self.dismissed.emit()

    # ───────────────────────────────────────────────────────────
    # TTL
    # ───────────────────────────────────────────────────────────

    def _on_ttl_timeout(self):
        self._ttl_remaining = 0
        self._progress_timer.stop()
        self._ttl_bar.set_ratio(0.0)
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
