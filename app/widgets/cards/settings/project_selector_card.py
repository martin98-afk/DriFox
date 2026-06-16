# -*- coding: utf-8 -*-
"""
项目选择卡片内容 - 卡片形式展示所有项目，支持选择、新建、归档
替代原来的 ProjectSelectorPopup 弹窗
"""
from typing import Dict

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QSizePolicy,
)
from qfluentwidgets import TransparentToolButton

from app.utils.utils import get_font_family_css, get_icon
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.widgets.cards.settings.mcp_setting_card import _ElidedLabel

# 项目颜色调色板（12 色，色相均匀分布，每 30° 一跳）
# 从红色 (0°) 开始，经橙黄绿青蓝紫玫红回到深橙 (330°)
# 深色主题优化版：所有颜色 HSL 亮度 ≥ 51%，确保在 #212126 深色背景上清晰可见
# 同时作为头像圆圈背景时白色文字仍具可读性（亮度 ≤ 63%）
PROJECT_COLORS = [
    "#ef5350",  # 红    0°  (L:62%, 原 #e53935 L:55%)
    "#ff9800",  # 橙   30°  (L:56%, 原 #f57c00 L:48%)
    "#ffc107",  # 琥珀 60°  (L:58%, 原 #fdd835 L:66%)
    "#9ccc65",  # 亮绿 90°  (L:60%, 原 #7cb342 L:48%)
    "#66bb6a",  # 绿  120°  (L:57%, 原 #43a047 L:45%)
    "#4db6ac",  # 墨绿150°  (L:51%, 原 #00897b L:27%) ★
    "#26c6da",  # 青  180°  (L:55%, 原 #00acc1 L:37%) ★
    "#42a5f5",  # 蓝  210°  (L:61%, 原 #1e88e5 L:51%)
    "#5c6bc0",  # 靛蓝240°  (L:56%, 原 #3949ab L:45%) ★
    "#ab47bc",  # 紫  270°  (L:51%, 原 #8e24aa L:40%) ★
    "#ec407a",  # 玫红300°  (L:59%, 原 #d81b60 L:47%)
    "#ff7043",  # 深橙330°  (L:63%, 原 #ff5722 L:56%)
]


def get_project_color(name: str, alpha: int = 255) -> str:
    """根据项目名计算固定颜色（确定性哈希分配）

    使用 zlib.crc32 替代内置 hash()，避免 Python 的
    进程间随机化种子（PYTHONHASHSEED）导致每次启动颜色不一致。

    Args:
        name: 项目名
        alpha: 透明度 0-255

    Returns:
        RGBA 颜色字符串，如 "rgba(33, 139, 255, 255)"
    """
    import zlib
    color_index = zlib.crc32(name.encode("utf-8")) % len(PROJECT_COLORS)
    hex_color = PROJECT_COLORS[color_index]
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


class _CircleAvatar(QWidget):
    """使用 QPainter 绘制的圆形项目头像，替代 QLabel + CSS border-radius

    Qt QSS 在小尺寸（24×24）上同时渲染 border + border-radius 时存在
    抗锯齿走样问题，导致圆形不够圆。本类用 QPainter 精确绘制，保证像素完美。
    """

    def __init__(self, text: str, color: str, parent=None):
        super().__init__(parent)
        self._text = text[0] if text else "?"
        # get_project_color() 返回 "rgba(r,g,b,a)" 格式，
        # QColor(string) 不解析此 CSS 格式，需拆解为数值构造
        self._color = self._parse_rgba(color)
        self.setFixedSize(24, 24)

    @staticmethod
    def _parse_rgba(rgba_str: str) -> QColor:
        """解析 "rgba(r,g,b,a)" 字符串为 QColor，失败时返回灰色"""
        if rgba_str.startswith("#"):
            return QColor(rgba_str)
        try:
            import re
            m = re.match(r'rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(\d+))?\s*\)', rgba_str)
            if m:
                r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
                a = int(m.group(4)) if m.group(4) else 255
                return QColor(r, g, b, a)
        except Exception:
            pass
        return QColor(128, 128, 128)  # fallback 灰色

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = self.rect()
        # 留出 1px 边距使 2px 宽度的画笔不超出 widget 边界
        draw_rect = rect.adjusted(1, 1, -1, -1)

        # 圆形背景
        painter.setBrush(self._color)
        painter.setPen(QPen(QColor(255, 255, 255, 38), 2))  # rgba(255,255,255,0.15)
        painter.drawEllipse(draw_rect)

        # 居中文字
        painter.setPen(Qt.white)
        font = painter.font()
        font.setPixelSize(scale_font_size(12))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self._text)


class ProjectItem(QWidget):
    """单个项目项 - 卡片内项目选择列表项"""
    clicked = pyqtSignal(str)
    archiveClicked = pyqtSignal(str)

    # 单行高度（无根目录）；有根目录时切换为 _DOUBLE_LINE_HEIGHT
    _SINGLE_LINE_HEIGHT = 30
    _DOUBLE_LINE_HEIGHT = 44

    def __init__(self, name: str, is_current: bool = False, parent=None):
        super().__init__(parent)
        self._name = name
        self._is_current = is_current
        self._session_count = 0
        self._worktree_count = 0
        self._project_color = get_project_color(name)
        self.setFixedHeight(self._SINGLE_LINE_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        # 上下边距 0、单行 30px：紧凑布局，让项目之间视觉密度更高
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(6)

        # 项目彩色圆形标识（首字符 + 项目专属色）
        # 使用 QPainter 绘制的 _CircleAvatar，避免 QSS border-radius 走样
        first_char = self._name.strip()[0] if self._name.strip() else "?"
        self._avatar_label = _CircleAvatar(first_char, self._project_color, self)
        layout.addWidget(self._avatar_label)

        # 中间：项目名 + 根目录（垂直布局）
        text_vbox = QVBoxLayout()
        text_vbox.setContentsMargins(0, 0, 0, 0)
        text_vbox.setSpacing(0)
        text_vbox.setAlignment(Qt.AlignVCenter)

        # 项目名
        self._name_label = QLabel(self._name, self)
        self._apply_name_style()
        text_vbox.addWidget(self._name_label)

        # 项目根目录（默认隐藏：未设置时由 set_root_dir 保持隐藏）
        # 使用 _ElidedLabel 根据可用宽度自动省略长路径
        self._root_dir_label = _ElidedLabel("", self)
        self._root_dir_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};"
        )
        self._root_dir_label.hide()
        text_vbox.addWidget(self._root_dir_label)

        layout.addLayout(text_vbox, 1)

        # 元数据（会话数 · 工作目录数），灰色小字
        self._meta_label = QLabel("", self)
        self._meta_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};"
        )
        self._meta_label.setAlignment(Qt.AlignVCenter)
        layout.addWidget(self._meta_label)

        # 当前项目指示
        if self._is_current:
            check_label = QLabel("✓", self)
            check_label.setStyleSheet(
                f"color: {Colors.BORDER_ACCENT}; font-size: {scale_font_size(14)}px;"
            )
            check_label.setAlignment(Qt.AlignVCenter)
            layout.addWidget(check_label)

        # 归档按钮（默认隐藏）
        self._archive_btn = TransparentToolButton(get_icon("归档"), self)
        self._archive_btn.setFixedSize(24, 24)
        self._archive_btn.setStyleSheet(f"""
            QToolButton {{
                background: transparent;
                border: none;
                font-size: {scale_font_size(12)}px;
            }}
            QToolButton:hover {{
                background: rgba(255, 255, 255, 50);
                border-radius: 4px;
            }}
        """)
        self._archive_btn.clicked.connect(self._emit_archive)
        self._archive_btn.setToolTip("归档此项目")
        self._archive_btn.hide()
        layout.addWidget(self._archive_btn)

    def _apply_name_style(self):
        if self._is_current:
            self._name_label.setStyleSheet(
                f"color: {self._project_color}; font-weight: bold; {get_font_family_css()} {font_size_css(13)};"
            )
        else:
            # 非当前项目用半透明版本
            semi_color = get_project_color(self._name, alpha=160)
            self._name_label.setStyleSheet(
                f"color: {semi_color}; {get_font_family_css()} {font_size_css(13)};"
            )

    def _emit_archive(self):
        self.archiveClicked.emit(self._name)

    def mousePressEvent(self, event):
        self.clicked.emit(self._name)
        super().mousePressEvent(event)

    def set_meta(self, session_count: int, worktree_count: int):
        """设置项目元数据（会话数、工作目录数）"""
        self._session_count = session_count
        self._worktree_count = worktree_count
        parts = []
        if session_count > 0:
            parts.append(f"{session_count}会话")
        if worktree_count > 0:
            parts.append(f"{worktree_count}工作目录")
        Colors.refresh()
        self._meta_label.setText(" · ".join(parts) if parts else "")
        self._meta_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};"
        )

    def set_root_dir(self, root_dir: str):
        """设置项目根目录路径（空字符串/None 则隐藏根目录行）"""
        if not root_dir:
            # 切换回单行高度，与未设置根目录的项目保持紧凑
            self._root_dir_label.hide()
            self.setFixedHeight(self._SINGLE_LINE_HEIGHT)
            return
        Colors.refresh()
        self._root_dir_label.setText(root_dir)
        self._root_dir_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};"
        )
        self._root_dir_label.setToolTip(root_dir)  # tooltip 展示完整路径
        self._root_dir_label.show()
        self.setFixedHeight(self._DOUBLE_LINE_HEIGHT)

    def enterEvent(self, event):
        # hover 时：整行加半透明背景 + 更亮的项目颜色 + 元数据提亮
        Colors.refresh()
        self.setStyleSheet(f"""
            ProjectItem {{
                background: {Colors.HOVER_BG};
                border-radius: 6px;
                border: none;
            }}
        """)
        hover_color = get_project_color(self._name, alpha=240)
        self._name_label.setStyleSheet(
            f"color: {hover_color}; font-weight: bold; {get_font_family_css()} {font_size_css(13)};"
        )
        self._meta_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(10)};"
        )
        self._archive_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setStyleSheet("")
        self._apply_name_style()
        Colors.refresh()
        self._meta_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};"
        )
        self._archive_btn.hide()
        super().leaveEvent(event)


class ProjectSelectorCardContent(QWidget):
    """项目选择卡片内容"""

    projectSelected = pyqtSignal(str)
    newProjectCreated = pyqtSignal(str)
    archiveProject = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._projects: list = []
        self._current_project: str = ""
        self._meta_map: Dict[str, Dict[str, int]] = {}
        self._root_dir_map: Dict[str, str] = {}
        self._setup_ui()

    def _setup_ui(self):
        Colors.refresh()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # ── 项目列表滚动区域 ──
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self._scroll_area.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                border: none;
                background: transparent;
                width: 12px;
                margin: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {Colors.BORDER};
                border-radius: 6px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {Colors.TEXT_MUTED};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)

        self._content_widget = QWidget()
        self._content_widget.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        # 项目之间用 1px 细缝：避免 0 完全相连导致看不出分隔，又比 2px 紧凑
        self._content_layout.setSpacing(1)

        self._scroll_area.setWidget(self._content_widget)
        self._scroll_area.setMinimumHeight(40)
        layout.addWidget(self._scroll_area, 1)

    def refresh_style(self):
        """刷新主题样式"""
        Colors.refresh()

    # ── 公有方法 ──────────────────────────────────────

    def set_projects_data(self, projects: list, current_project: str,
                          meta_map: Dict[str, Dict[str, int]] = None,
                          root_dir_map: Dict[str, str] = None):
        """设置项目列表数据

        Args:
            projects: 项目名列表
            current_project: 当前项目名
            meta_map: {project: {"sessions": int, "worktrees": int}} 可选元数据
            root_dir_map: {project: root_dir_path} 可选根目录映射
        """
        self._projects = list(projects)
        self._current_project = current_project
        self._meta_map = meta_map or {}
        self._root_dir_map = root_dir_map or {}
        self._refresh_project_list()

    def _refresh_project_list(self):
        """刷新项目列表"""
        # 清空现有项
        while self._content_layout.count():
            child = self._content_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        # 添加项目
        for proj_name in self._projects:
            is_current = proj_name == self._current_project
            item = ProjectItem(proj_name, is_current, self)
            # 设置元数据
            meta = self._meta_map.get(proj_name, {})
            item.set_meta(
                session_count=meta.get("sessions", 0),
                worktree_count=meta.get("worktrees", 0),
            )
            # 设置根目录（空字符串时 ProjectItem 内部隐藏该行）
            item.set_root_dir(self._root_dir_map.get(proj_name, ""))
            item.clicked.connect(self._on_project_item_clicked)
            item.archiveClicked.connect(self._on_archive_clicked)
            self._content_layout.addWidget(item)

        self._content_layout.addStretch(1)

    def _on_project_item_clicked(self, name: str):
        """项目被点击"""
        self.projectSelected.emit(name)

    def _on_archive_clicked(self, project_name: str):
        """归档按钮被点击"""
        from PyQt5.QtWidgets import QMessageBox

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("归档确认")
        msg_box.setText(f"确定归档项目「{project_name}」吗？\n归档后该项目的所有会话将移动到归档区。")
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        # 应用主题样式（避免深色主题下黑底黑字）
        Colors.refresh()
        # 1) 先设 QMessageBox 自身 stylesheet（背景、文字）
        msg_box.setStyleSheet(f"""
            QMessageBox {{
                background-color: {Colors.CARD_BG.format(alpha=240)};
                color: {Colors.TEXT_PRIMARY};
            }}
            QMessageBox QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(13)};
                background: transparent;
            }}
        """)
        # 2) Windows 原生对话框样式下按钮不受 stylesheet 控制，
        #    必须直接遍历按钮单独设置样式
        for btn in msg_box.findChildren(QMessageBox.StandardButton.__class__) if False else []:
            pass  # 上面那行仅占位，避免导入循环；真正遍历见下方
        button_style_default = f"""
            QPushButton {{
                background-color: {Colors.BORDER_ACCENT};
                color: {Colors.BUTTON_TEXT_ON_ACCENT};
                border: 1px solid {Colors.BORDER_ACCENT};
                border-radius: 4px;
                padding: 6px 18px;
                min-width: 64px;
                {get_font_family_css()} {font_size_css(13)};
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {Colors.SEND_BTN_HOVER_START};
                border-color: {Colors.SEND_BTN_HOVER_START};
            }}
            QPushButton:pressed {{
                background-color: {Colors.SELECTED_BG};
            }}
        """
        button_style_normal = f"""
            QPushButton {{
                background-color: {Colors.TOOLBAR_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 6px 18px;
                min-width: 64px;
                {get_font_family_css()} {font_size_css(13)};
            }}
            QPushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.BORDER_ACCENT};
            }}
            QPushButton:pressed {{
                background-color: {Colors.SELECTED_BG};
            }}
        """
        # 3) 找到所有按钮并单独应用样式（"是" 是默认按钮，用强调色）
        default_btn = msg_box.defaultButton()
        for button in msg_box.buttons():
            if button is default_btn:
                button.setStyleSheet(button_style_default)
            else:
                button.setStyleSheet(button_style_normal)
            # 强制使用样式背景（Windows 原生渲染下必须显式开启）
            button.setAutoFillBackground(True)
        reply = msg_box.exec_()
        if reply == QMessageBox.Yes:
            self.archiveProject.emit(project_name)


