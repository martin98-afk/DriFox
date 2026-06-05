# -*- coding: utf-8 -*-
"""
项目选择卡片内容 - 卡片形式展示所有项目，支持选择、新建、归档
替代原来的 ProjectSelectorPopup 弹窗
"""
from typing import Dict

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QLineEdit, QSizePolicy, QFrame,
)
from qfluentwidgets import TransparentToolButton, FluentIcon

from app.utils.utils import get_font_family_css, get_icon
from app.utils.design_tokens import Colors, font_size_css, scale_font_size


class ProjectItem(QWidget):
    """单个项目项 - 卡片内项目选择列表项"""
    clicked = pyqtSignal(str)
    archiveClicked = pyqtSignal(str)

    def __init__(self, name: str, is_current: bool = False, parent=None):
        super().__init__(parent)
        self._name = name
        self._is_current = is_current
        self._session_count = 0
        self._worktree_count = 0
        self.setFixedHeight(36)
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 4, 0)
        layout.setSpacing(6)

        # 项目图标
        icon_label = QLabel("📁", self)
        icon_label.setStyleSheet(f"font-size: {scale_font_size(14)}px;")
        layout.addWidget(icon_label)

        # 项目名
        self._name_label = QLabel(self._name, self)
        self._apply_name_style()
        layout.addWidget(self._name_label, 1)

        # 元数据（会话数 · 工作目录数），灰色小字
        self._meta_label = QLabel("", self)
        self._meta_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)};"
        )
        layout.addWidget(self._meta_label)

        # 当前项目指示
        if self._is_current:
            check_label = QLabel("✓", self)
            check_label.setStyleSheet(
                f"color: {Colors.BORDER_ACCENT}; font-size: {scale_font_size(14)}px;"
            )
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
        Colors.refresh()
        if self._is_current:
            self._name_label.setStyleSheet(
                f"color: {Colors.BORDER_ACCENT}; font-weight: bold; {get_font_family_css()} {font_size_css(13)};"
            )
        else:
            self._name_label.setStyleSheet(
                f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(13)};"
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

    def enterEvent(self, event):
        self._name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(13)};"
        )
        Colors.refresh()
        self._meta_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(10)};"
        )
        self._archive_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
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
        self._setup_ui()

    def _setup_ui(self):
        Colors.refresh()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # ── 新建项目输入区域 ──
        new_proj_layout = QHBoxLayout()
        new_proj_layout.setSpacing(6)
        new_proj_layout.setContentsMargins(4, 4, 4, 0)

        self._new_project_edit = QLineEdit(self)
        self._new_project_edit.setPlaceholderText("新建项目...")
        self._new_project_edit.setInputMethodHints(Qt.ImhNoAutoUppercase)
        self._apply_new_project_edit_style()
        self._new_project_edit.returnPressed.connect(self._on_create_project)
        new_proj_layout.addWidget(self._new_project_edit, 1)

        self._add_btn = TransparentToolButton(FluentIcon.ADD, self)
        self._add_btn.setFixedSize(28, 28)
        self._add_btn.setToolTip("创建项目")
        self._add_btn.clicked.connect(self._on_create_project)
        new_proj_layout.addWidget(self._add_btn)

        layout.addLayout(new_proj_layout)

        # ── 分隔线 ──
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background-color: {Colors.BORDER}; max-height: 1px; margin: 4px 0;")
        layout.addWidget(sep)

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
        self._content_layout.setSpacing(2)

        self._scroll_area.setWidget(self._content_widget)
        self._scroll_area.setMinimumHeight(50)
        self._scroll_area.setMaximumHeight(300)
        layout.addWidget(self._scroll_area, 1)

    def _apply_new_project_edit_style(self):
        """应用新建项目输入框样式"""
        Colors.refresh()
        self._new_project_edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 5px 8px;
                selection-background-color: {Colors.BORDER_ACCENT};
                selection-color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(12)};
            }}
            QLineEdit:focus {{
                border-color: {Colors.BORDER_ACCENT};
            }}
            QLineEdit::placeholder {{
                color: {Colors.TEXT_MUTED};
            }}
        """)

    def refresh_style(self):
        """刷新主题样式"""
        Colors.refresh()
        self._apply_new_project_edit_style()

    # ── 公有方法 ──────────────────────────────────────

    def set_projects_data(self, projects: list, current_project: str,
                          meta_map: Dict[str, Dict[str, int]] = None):
        """设置项目列表数据

        Args:
            projects: 项目名列表
            current_project: 当前项目名
            meta_map: {project: {"sessions": int, "worktrees": int}} 可选元数据
        """
        self._projects = list(projects)
        self._current_project = current_project
        self._meta_map = meta_map or {}
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

        reply = QMessageBox.question(
            self,
            "归档确认",
            f"确定归档项目「{project_name}」吗？\n归档后该项目的所有会话将移动到归档区。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.archiveProject.emit(project_name)

    def _on_create_project(self):
        """创建新项目"""
        name = self._new_project_edit.text().strip()
        if not name:
            return

        if name not in self._projects:
            self._projects.append(name)
            self._refresh_project_list()
            self._new_project_edit.clear()

        self.newProjectCreated.emit(name)
