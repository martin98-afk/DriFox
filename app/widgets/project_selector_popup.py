# -*- coding: utf-8 -*-
"""
项目选择弹窗 - 点击标题栏项目名时弹出
支持选择已有项目或新建项目
"""
from PyQt5.QtCore import Qt, pyqtSignal, QPoint, QTimer
from PyQt5.QtWidgets import (
    QWidget,
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QLineEdit,
    QApplication,
    QSizePolicy,
)
from qfluentwidgets import TransparentToolButton, FluentIcon

from app.utils.utils import get_font_family_css, get_icon
from app.utils.design_tokens import Colors, font_size_css


class ProjectItem(QWidget):
    """单个项目项"""
    clicked = pyqtSignal(str)
    archiveClicked = pyqtSignal(str)

    def __init__(self, name: str, is_current: bool = False, parent=None):
        super().__init__(parent)
        self._name = name
        self._is_current = is_current
        self.setFixedHeight(36)
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 4, 0)
        layout.setSpacing(8)

        # 项目图标
        icon_label = QLabel("📁", self)
        icon_label.setStyleSheet("font-size: 14px;")
        layout.addWidget(icon_label)

        # 项目名
        self._name_label = QLabel(self._name, self)
        self._apply_name_style()
        layout.addWidget(self._name_label, 1)

        # 当前项目指示
        if self._is_current:
            check_label = QLabel("✓", self)
            check_label.setStyleSheet(f"color: {Colors.BORDER_ACCENT}; font-size: 14px;")
            layout.addWidget(check_label)

        # 归档按钮（默认隐藏）
        self._archive_btn = TransparentToolButton(get_icon("归档"), self)
        self._archive_btn.setFixedSize(24, 24)
        self._archive_btn.setStyleSheet("""
            QToolButton {
                background: transparent;
                border: none;
                font-size: 12px;
            }
            QToolButton:hover {
                background: rgba(255, 255, 255, 50);
                border-radius: 4px;
            }
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

    def enterEvent(self, event):
        self._name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(13)};"
        )
        self._archive_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._apply_name_style()
        self._archive_btn.hide()
        super().leaveEvent(event)


class ProjectSelectorPopup(QWidget):
    """项目选择弹窗"""
    projectSelected = pyqtSignal(str)
    newProjectCreated = pyqtSignal(str)
    archiveProject = pyqtSignal(str)

    def __init__(self, projects: list, current_project: str, parent=None):
        super().__init__(parent)
        self._projects = list(projects)
        self._current_project = current_project

        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 注意：不设置 WA_ShowWithoutActivating
        # 否则窗口不会被激活，中文 IME 的 composition 事件会发送到
        # 之前活跃的主窗口输入框，导致中文输入跑到其他地方

        # 注意：不在 __init__ 安装事件过滤器！
        # 因为当前 mousePressEvent 还在传播中，
        # 如果在此时安装，同一个鼠标按下事件会被过滤器捕获
        # 导致弹窗被立即关闭
        # 事件过滤器在 show_at() 中安装

        self._setup_ui()

    def _setup_ui(self):
        Colors.refresh()
        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("projectPopupFrame")
        self.main_frame.setStyleSheet(f"""
            QFrame#projectPopupFrame {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.main_frame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # 标题
        title = QLabel("选择项目", self)
        title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-weight: bold; {get_font_family_css()} {font_size_css(13)}; "
            f"padding: 4px 8px;"
        )
        layout.addWidget(title)

        # 新建项目输入框
        new_proj_layout = QHBoxLayout()
        new_proj_layout.setSpacing(6)

        self._new_project_edit = QLineEdit(self)
        self._new_project_edit.setPlaceholderText("新建项目...")
        # 不设置 InputMethodHints，让中文输入法正常工作
        # ImhPreferLatin 会阻止中文输入
        # ImhSensitiveData/ImhNoPredictiveText 会干扰 IME composition
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

        # 分隔线
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background-color: {Colors.BORDER}; max-height: 1px; margin: 4px 0;")
        layout.addWidget(sep)

        # 项目列表滚动区域
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

        # 填充项目列表
        self._refresh_project_list()

        # 整体窗口布局
        window_layout = QVBoxLayout(self)
        window_layout.setContentsMargins(0, 0, 0, 0)
        window_layout.addWidget(self.main_frame)

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
        """刷新弹窗主题样式"""
        Colors.refresh()
        self.main_frame.setStyleSheet(f"""
            QFrame#projectPopupFrame {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)
        self._apply_new_project_edit_style()

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
            item.clicked.connect(self._on_project_item_clicked)
            item.archiveClicked.connect(self._on_archive_clicked)
            self._content_layout.addWidget(item)

        self._content_layout.addStretch(1)

    def _on_project_item_clicked(self, name: str):
        """项目被点击"""
        self.projectSelected.emit(name)
        self.close()

    def _on_archive_clicked(self, project_name: str):
        """归档按钮被点击"""
        from PyQt5.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self,
            "归档确认",
            f"确定归档项目「{project_name}」吗？\n归档后该项目的所有会话将移动到归档区。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.archiveProject.emit(project_name)
            self.close()

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
        self.projectSelected.emit(name)
        self.close()

    def show_at(self, reference_widget: QWidget):
        """在参考控件下方显示"""
        self.show()
        QApplication.processEvents()

        self.main_frame.layout().activate()
        content_size = self.main_frame.sizeHint()

        screen = reference_widget.screen() or QApplication.primaryScreen()
        if screen:
            screen_geom = screen.availableGeometry()
            max_width = min(350, screen_geom.width() - 40)
            max_height = min(400, screen_geom.height() - 120)
        else:
            max_width = 350
            max_height = 400

        self.setMaximumSize(max_width, max_height)
        self.resize(min(content_size.width(), max_width),
                    min(content_size.height(), max_height))

        ref_rect = reference_widget.rect()
        ref_global = reference_widget.mapToGlobal(ref_rect.topLeft())

        popup_w = min(self.width(), self.maximumWidth())
        popup_h = min(self.height(), self.maximumHeight())

        # 左边对齐到参考控件
        x = ref_global.x()
        # 下方显示
        y = ref_global.y() + ref_rect.height() + 4

        if screen:
            screen_geom = screen.availableGeometry()
            if x < screen_geom.left():
                x = screen_geom.left() + 10
            if x + popup_w > screen_geom.right():
                x = screen_geom.right() - popup_w - 10
            if y + popup_h > screen_geom.bottom():
                y = ref_global.y() - popup_h - 4

        self.move(x, y)
        self.raise_()

        # 激活窗口并设置焦点到输入框，确保中文 IME 能正确工作
        self.activateWindow()
        self._new_project_edit.setFocus()

        # 安装事件过滤器，检测外部点击关闭弹窗
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        """检测外部点击，关闭弹窗"""
        if event.type() == event.MouseButtonPress:
            global_pos = event.globalPos()
            popup_geo = self.geometry()
            if not popup_geo.contains(global_pos):
                # 输入框有焦点时，可能是用户在点击 IME 候选词窗口
                # IME 候选窗口在弹窗外部，此时不应关闭弹窗
                if self._new_project_edit.hasFocus():
                    return False
                self.close()
        return super().eventFilter(obj, event)

    def close(self):
        """关闭弹窗时移除事件过滤器"""
        QApplication.instance().removeEventFilter(self)
        super().close()
