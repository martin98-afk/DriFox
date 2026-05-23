# -*- coding: utf-8 -*-
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QPainter, QColor
from PyQt5.QtWidgets import (QVBoxLayout, QLabel, QHBoxLayout,
                              QScrollArea, QSizePolicy, QWidget)
from qfluentwidgets import SimpleCardWidget, FluentIcon, TransparentToolButton

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font

_MAX_VISIBLE_ITEMS = 5
_MIN_SCROLL_HEIGHT = 60   # 拖拽时滚动区最小高度


class _DragHandle(QWidget):
    """底部拖拽把手：长按拖动调整高度，双击重置为自适应"""

    reset_requested = pyqtSignal()

    def __init__(self, card, parent=None):
        super().__init__(parent)
        self._card = card
        self.setFixedHeight(10)
        self.setCursor(Qt.SizeVerCursor)
        self._dragging = False
        self._start_y = 0
        self._start_h = 0

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color = QColor(Colors.REALTIME_BORDER)
        color.setAlpha(120)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        cx, cy = self.width() // 2, self.height() // 2
        # 6 个小圆点 (2 列 × 3 行)
        for r in range(3):
            for c in range(2):
                p.drawEllipse(cx - 6 + c * 12, cy - 5 + r * 4, 3, 2)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._start_y = event.globalY()
            # 记录拖拽开始时滚动区的实际高度
            sa = self._card.scroll_area
            self._start_h = sa.height() if sa.height() > 0 else 100
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            delta = event.globalY() - self._start_y
            new_h = max(_MIN_SCROLL_HEIGHT, self._start_h + delta)
            self._card._apply_user_height(new_h)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            event.accept()

    def mouseDoubleClickEvent(self, event):
        self.reset_requested.emit()
        event.accept()


_SCROLL_AREA_STYLE = """
    QScrollArea {
        background: transparent;
        border: none;
    }
    QScrollArea > QWidget > QWidget {
        background: transparent;
    }
    QScrollBar:vertical {
        background: transparent;
        width: 6px;
        margin: 0;
    }
    QScrollBar::handle:vertical {
        background: rgba(255, 255, 255, 0.2);
        border-radius: 3px;
        min-height: 20px;
    }
    QScrollBar::handle:vertical:hover {
        background: rgba(255, 255, 255, 0.35);
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0px;
    }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: transparent;
    }
"""


class TodoFloatingWidget(SimpleCardWidget):
    """TODO 悬浮框组件"""

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._todo_list = []
        self._item_height_px = 30  # 会被动态更新
        self._user_scroll_height = None  # 用户拖拽覆盖的高度，None=自适应
        self._setup_ui()

    def _setup_ui(self):
        # Preferred 垂直策略：允许组件随内容增长
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._apply_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 10, 14, 6)
        main_layout.setSpacing(6)

        # ---- 标题栏 ----
        header = QHBoxLayout()
        header.setSpacing(10)

        title_icon = QLabel("📋", self)
        title_icon.setFont(get_unified_font(14))

        title = QLabel("待办事项", self)
        title.setFont(get_unified_font(11, True))
        title.setStyleSheet(f"color: {Colors.REALTIME_TEXT};")

        self.progress_label = QLabel("", self)
        self.progress_label.setFont(get_unified_font(10, True))
        self.progress_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT}; font-weight: bold;")

        header.addWidget(title_icon)
        header.addWidget(title)
        header.addWidget(self.progress_label)
        header.addStretch()

        self.close_btn = TransparentToolButton(FluentIcon.CLOSE)
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.clicked.connect(self._on_close)
        header.addWidget(self.close_btn)

        # ---- 内容滚动区 ----
        self.content_label = QLabel("暂无待办", self)
        self.content_label.setFont(get_unified_font(10))
        self.content_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self.content_label.setWordWrap(True)
        self.content_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.scroll_area.setStyleSheet(_SCROLL_AREA_STYLE)
        self.scroll_area.viewport().setAutoFillBackground(False)
        self.scroll_area.setWidget(self.content_label)

        # ---- 底部拖拽把手 ----
        self._drag_handle = _DragHandle(self, self)
        self._drag_handle.reset_requested.connect(self._reset_user_height)

        main_layout.addLayout(header)
        main_layout.addWidget(self.scroll_area, 1)
        main_layout.addWidget(self._drag_handle)

    # ---- 高度管理 ----

    def _apply_user_height(self, scroll_height: int):
        """由拖拽把手调用，设置用户偏好高度"""
        self._user_scroll_height = scroll_height
        self.scroll_area.setMaximumHeight(scroll_height)
        self.scroll_area.setMinimumHeight(scroll_height)
        self.updateGeometry()

    def _reset_user_height(self):
        """双击把手：重置为自适应高度"""
        self._user_scroll_height = None
        self.scroll_area.setMinimumHeight(0)
        self._adjust_scroll_height()

    def _adjust_scroll_height(self):
        """计算并应用滚动区高度：5项内自适应，超出则限高"""
        count = len(self._todo_list)
        if count <= 0:
            return

        # 用户手动覆盖时，只需刷新布局
        if self._user_scroll_height is not None:
            self.scroll_area.setMaximumHeight(self._user_scroll_height)
            self.updateGeometry()
            return

        # 使用 heightForWidth 获取更精确的高度（优先）
        viewport_w = self.scroll_area.viewport().width()
        if viewport_w > 0:
            content_height = self.content_label.heightForWidth(viewport_w)
            if content_height < 0:
                content_height = 0
        # 回退：adjustSize + sizeHint
        if content_height <= 0:
            self.content_label.adjustSize()
            content_height = self.content_label.sizeHint().height()
        # 二次回退：基于字体估算
        if content_height <= 0:
            fm = self.content_label.fontMetrics()
            content_height = count * (fm.height() + 10)

        self._item_height_px = max(content_height / count, 1)

        padding = 6
        if count <= _MAX_VISIBLE_ITEMS:
            # 5 项内：全显，不限高度
            max_h = int(content_height) + padding
        else:
            # 超过 5 项：限高为 5 项高度
            max_h = int(self._item_height_px * _MAX_VISIBLE_ITEMS) + padding

        self.scroll_area.setMinimumHeight(0)
        self.scroll_area.setMaximumHeight(max_h)
        self.updateGeometry()

    # ---- 样式 ----

    def _apply_style(self):
        Colors.refresh()
        self.setStyleSheet(f"""
            CardWidget {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 10px;
            }}
        """)

    def refresh_style(self):
        """响应主题切换"""
        self._apply_style()
        for child in self.findChildren(QLabel):
            text = child.text()
            if text == "待办事项":
                child.setStyleSheet(f"color: {Colors.REALTIME_TEXT};")
            elif child == self.progress_label:
                pass  # 进度标签颜色由 update_todos 控制
            elif child == self.content_label:
                pass  # 内容颜色由 update_todos 控制
        if self._todo_list:
            self.update_todos(self._todo_list)

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    # ---- 数据更新 ----

    def update_todos(self, todos):
        """更新 TODO 列表显示（不自行控制可见性，由调用方决定）"""
        self._todo_list = todos or []

        if not self._todo_list:
            self.setVisible(False)
            return

        lines = []
        completed = 0
        in_progress = 0
        first_in_progress_idx = None
        for i, todo in enumerate(self._todo_list):
            status = todo.get("status", "")
            content = todo.get("content", "")
            priority = todo.get("priority", "medium")

            if status == "completed":
                completed += 1
                status_icon = "✓"
            elif status == "in_progress":
                if first_in_progress_idx is None:
                    first_in_progress_idx = i
                in_progress += 1
                status_icon = "▶"
            else:
                status_icon = "○"

            priority_colors = {"high": Colors.REALTIME_ERROR, "medium": Colors.REALTIME_ACCENT_WARM, "low": Colors.REALTIME_SUCCESS}
            priority_color = priority_colors.get(priority, Colors.REALTIME_ACCENT_WARM)

            priority_labels = {"high": "🔴", "medium": "🟡", "low": "🟢"}
            priority_icon = priority_labels.get(priority, "🟡")

            if status == "completed":
                content_style = f"color: {Colors.REALTIME_TEXT_SECONDARY}; text-decoration: line-through;"
            elif status == "in_progress":
                content_style = f"color: {Colors.REALTIME_ACCENT}; font-weight: bold;"
            else:
                content_style = f"color: {Colors.REALTIME_TEXT};"

            lines.append(
                f'<span style="color: {Colors.REALTIME_ACCENT}; font-weight: bold;">{status_icon}</span> '
                f'<span style="color: {priority_color};">{priority_icon}</span> '
                f'<span style="{content_style}">{content}</span>'
            )

        total = len(self._todo_list)
        done_count = completed + in_progress
        if done_count == total and done_count > 0:
            if in_progress > 0:
                progress_text = f"⏳ {in_progress}进行中 + {completed}完成"
                self.progress_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT}; font-weight: bold;")
            else:
                progress_text = f"🎉 {completed}/{total} 全部完成"
                self.progress_label.setStyleSheet(f"color: {Colors.REALTIME_SUCCESS}; font-weight: bold;")
        else:
            progress_text = f"{completed}完成/{in_progress}进行中/{total}"
            self.progress_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT}; font-weight: bold;")

        self.progress_label.setText(progress_text)
        self.content_label.setText("<br>".join(lines))

        # 延迟计算高度（等布局完成）
        QTimer.singleShot(0, self._adjust_scroll_height)

        # 滚动到第一个 in_progress 项
        if first_in_progress_idx is not None:
            scroll_to = first_in_progress_idx * self._item_height_px
            QTimer.singleShot(1, lambda: self.scroll_area.verticalScrollBar().setValue(int(scroll_to)))

    def clear(self):
        """清空 TODO 显示"""
        self._todo_list = []
        self._user_scroll_height = None
        self.scroll_area.setMinimumHeight(0)
        self.setVisible(False)

    def set_opacity(self, opacity: float):
        """设置透明度，用于响应全局透明度变化"""
        Colors.refresh()
        bg = Colors.REALTIME_BG
        if bg.startswith("rgba("):
            # 最小 alpha 为 1，避免完全透明导致卡片"消失"
            alpha = max(1, int(opacity * 255))
            bg = bg.rsplit(",", 1)[0] + f", {alpha})"
        self.setStyleSheet(f"""
            CardWidget {{
                background-color: {bg};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 10px;
            }}
        """)