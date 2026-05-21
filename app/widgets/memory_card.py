# -*- coding: utf-8 -*-
"""
记忆管理卡片 - 重构为 3 Tab 结构
1. 条目记忆 - 列表 + 搜索 + 编辑
2. 项目笔记 - Markdown 编辑器
3. 关键文档 - 列表 + 拖拽添加
"""
import os

from PyQt5.QtCore import pyqtSignal, Qt, QSize
from PyQt5.QtGui import QDropEvent, QDragEnterEvent, QDragMoveEvent, QColor
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QListWidgetItem,
    QFileDialog,
    QSizePolicy,
    QMenu,
    QAction,
)
from qfluentwidgets import (
    BodyLabel,
    LineEdit,
    PrimaryPushButton,
    SwitchButton,
    FluentIcon,
    TransparentToolButton,
    ListWidget,
    TextEdit,
)

from app.utils.design_tokens import scale_font_size, font_size_css, Colors
from app.utils.utils import get_font_family_css, get_icon
from app.utils.git_worktree import GitWorktreeDetector
from app.widgets.worktree_section import WorktreeSectionWidget

# Tab 标识
TAB_ENTRY_MEMORIES = "entries"
TAB_PROJECT_NOTES = "notes"
TAB_KEY_DOCUMENTS = "docs"


class DocDropListWidget(ListWidget):
    """支持拖拽文件的列表控件"""
    files_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._is_drag_over = False

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._is_drag_over = True
            self.update()

    def dragMoveEvent(self, event: QDragMoveEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._is_drag_over = False
        self.update()

    def dropEvent(self, event: QDropEvent):
        self._is_drag_over = False
        self.update()

        file_paths = []
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                # 文件和文件夹都接受
                if path and (os.path.isfile(path) or os.path.isdir(path)):
                    file_paths.append(path)

        if file_paths:
            self.files_dropped.emit(file_paths)


class EntryMemoryItemWidget(QWidget):
    """条目记忆项组件"""

    deleted = pyqtSignal(str)  # memory_id
    toggled = pyqtSignal(str, bool)
    edited = pyqtSignal(str, str)  # memory_id, new_content

    def __init__(
        self,
        memory_id: str,
        content: str,
        enabled: bool = True,
        source: str = "manual",
        parent=None,
    ):
        super().__init__(parent)
        self.memory_id = memory_id
        self._content = content
        self._editing = False
        self._init_ui(enabled, source)

    def _init_ui(self, enabled, source):
        # 高度自适应内容，不固定高度
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self.setMinimumHeight(44)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # 内容区域
        self.text_widget = QWidget(self)
        # 允许收缩，适应小窗口
        self.text_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)
        text_layout = QVBoxLayout(self.text_widget)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)

        self.content_label = BodyLabel(self._content, self.text_widget)
        self.content_label.setWordWrap(True)
        # 允许收缩，最小宽度小一点，适应小窗口
        self.content_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)
        self.content_label.setToolTip(self._content)  # 悬浮显示完整内容
        self.content_label.setStyleSheet(
            f"padding: 4px; {get_font_family_css()} {font_size_css(12)}"
        )
        text_layout.addWidget(self.content_label)

        main_layout.addWidget(self.text_widget, 1)

        # 编辑输入框（初始隐藏，使用 TextEdit 支持多行）
        self.edit_widget = QWidget(self)
        self.edit_widget.setSizePolicy(1, QSizePolicy.MinimumExpanding)
        self.edit_widget.setVisible(False)
        edit_layout = QVBoxLayout(self.edit_widget)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        edit_layout.setSpacing(0)

        from qfluentwidgets import TextEdit
        self.edit_text = TextEdit(self.edit_widget)
        self.edit_text.setText(self._content)
        self.edit_text.setPlaceholderText("编辑条目记忆...")
        self.edit_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: rgba(50, 50, 50, 200);
                border: 1px solid rgba(14, 99, 156, 200);
                color: #e0e0e0;
                padding: 4px 6px;
                border-radius: 3px;
                {get_font_family_css()} {font_size_css(12)}
            }}
        """)
        self.edit_text.setMinimumHeight(36)
        self.edit_text.setMaximumHeight(200)  # 限制最大高度，超出可滚动
        self.edit_text.document().documentLayout().documentSizeChanged.connect(self._adjust_edit_height)
        # 失去焦点自动保存
        self.edit_text.focusOutEvent = lambda e: self._on_focus_out(e)
        edit_layout.addWidget(self.edit_text)

        main_layout.addWidget(self.edit_widget, 1)

        # 操作按钮
        self.edit_btn = TransparentToolButton(FluentIcon.EDIT, self)
        self.edit_btn.setToolTip("编辑")
        self.edit_btn.clicked.connect(self._start_edit)

        self.delete_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.delete_btn.setToolTip("删除")
        self.delete_btn.clicked.connect(lambda: self.deleted.emit(self.memory_id))

        self.switch = SwitchButton(self)
        self.switch.setChecked(enabled)
        from app.utils.design_tokens import SwitchStyles
        SwitchStyles.configure(self.switch)
        self.switch.checkedChanged.connect(
            lambda checked: self.toggled.emit(self.memory_id, checked)
        )

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(2)
        btn_layout.addWidget(self.edit_btn)
        btn_layout.addWidget(self.delete_btn)
        btn_layout.addWidget(self.switch)

        main_layout.addLayout(btn_layout)

    def _adjust_edit_height(self):
        """根据内容调整编辑框高度，不超过最大高度，超出可滚动"""
        doc = self.edit_text.document()
        doc_height = int(doc.size().height() + 10)
        height = max(36, min(doc_height, 200))
        self.edit_text.setFixedHeight(height)
        # 如果内容超过最大高度，QTextEdit 会自动出现滚动条，可以滚动查看
    
    def _start_edit(self):
        """开始编辑"""
        self._editing = True
        self.text_widget.setVisible(False)
        self.edit_widget.setVisible(True)
        self._adjust_edit_height()
        self.edit_text.setFocus()
        # 选中文本
        cursor = self.edit_text.textCursor()
        cursor.select(cursor.Document)
        self.edit_text.setTextCursor(cursor)

    def _finish_edit(self):
        """完成编辑"""
        new_content = self.edit_text.toPlainText().strip()
        if new_content and new_content != self._content:
            self.edited.emit(self.memory_id, new_content)
            self._content = new_content
            self.content_label.setText(new_content)
        self._cancel_edit()

    def _on_focus_out(self, event):
        """失去焦点时自动保存完成编辑"""
        if self._editing:
            self._finish_edit()
        # 继续传递事件
        if event:
            event.ignore()
    
    def _cancel_edit(self):
        """取消编辑"""
        self._editing = False
        self.text_widget.setVisible(True)
        self.edit_widget.setVisible(False)
        self.edit_text.setText(self._content)


class KeyDocumentItemWidget(QWidget):
    """关键文档项组件"""

    removed = pyqtSignal(str)  # doc_id
    open_file = pyqtSignal(str)  # file_path
    open_folder = pyqtSignal(str)  # folder_path
    setAsWorkingDir = pyqtSignal(str)  # file_path
    worktreeChanged = pyqtSignal(str, str)  # (original_folder, worktree_path)

    def __init__(
        self,
        doc_id: str,
        file_name: str,
        file_path: str,
        added_by: str = "manual",
        is_working_dir: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.doc_id = doc_id
        self.file_path = file_path
        self._is_folder = os.path.isdir(file_path) if file_path else False
        self._is_working_dir = is_working_dir and self._is_folder
        self._init_ui(file_name, file_path, added_by)

    def _get_icon(self, file_name: str, file_path: str) -> str:
        """根据文件类型获取对应图标，文件夹单独处理"""
        import os
        # 先判断是否是文件夹
        if os.path.isdir(file_path):
            return "📁"
        
        ext = file_name.lower().split('.')[-1] if '.' in file_name else ''
        
        icon_map = {
            # 代码文件
            'py': '🐍', 'python': '🐍',
            'js': '🟨', 'javascript': '🟨',
            'ts': '🔷', 'typescript': '🔷',
            'jsx': '⚛️', 'tsx': '⚛️',
            'java': '☕',
            'go': '🐹',
            'rs': '🦀', 'rust': '🦀',
            'c': '🔶', 'cpp': '🔶', 'h': '🔶',
            'cs': '🔷',
            'php': '🐘',
            'rb': '💎',
            'swift': '🍎',
            'kt': '🤖',
            # 文档
            'md': '📝', 'markdown': '📝',
            'txt': '📄',
            'rtf': '📄',
            'pdf': '📕',
            'doc': '📘', 'docx': '📘',
            'xls': '📊', 'xlsx': '📊', 'csv': '📊',
            'ppt': '📙', 'pptx': '📙',
            'html': '🌐', 'htm': '🌐',
            'css': '🎨',
            'scss': '🎨', 'less': '🎨',
            'json': '🔧',
            'yaml': '🔧', 'yml': '🔧',
            'toml': '🔧',
            'ini': '🔧',
            'cfg': '🔧',
            'conf': '🔧',
            'xml': '🔧',
            # 图片
            'png': '🖼️', 'jpg': '🖼️', 'jpeg': '🖼️',
            'gif': '🖼️', 'bmp': '🖼️', 'svg': '🖼️',
            'webp': '🖼️',
            # 视频音频
            'mp4': '🎬', 'webm': '🎬',
            'mp3': '🎵', 'wav': '🎵', 'ogg': '🎵',
            # 存档
            'zip': '📦', 'rar': '📦', '7z': '📦',
            'tar': '📦', 'gz': '📦',
            # git
            'gitignore': '🌱',
            # license/readme
            'license': '📜', 'licence': '📜',
            'readme': '📖', 'readme.md': '📖',
        }
        
        return icon_map.get(ext, icon_map.get(file_name.lower(), '📄'))

    def _init_ui(self, file_name, file_path, added_by):
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        # 工作目录高亮背景（用 Palette 方式避免 QListWidget 样式表冲突）
        if self._is_working_dir:
            palette = self.palette()
            palette.setColor(self.backgroundRole(), QColor(46, 160, 67, 35))
            self.setPalette(palette)
            self.setAutoFillBackground(True)
            self.setStyleSheet("border-radius: 4px;")
        else:
            self.setAutoFillBackground(False)
            self.setPalette(self.style().standardPalette())
            self.setStyleSheet("")

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(8, 4, 8, 4)
        main_layout.setSpacing(4)

        # 文件/文件夹图标（根据类型显示不同图标）
        icon = self._get_icon(file_name, file_path)
        icon_label = BodyLabel(icon, self)
        icon_label.setStyleSheet(f"{font_size_css(16)} padding: 0 4px;")

        name_label = BodyLabel(file_name, self)
        name_label.setWordWrap(False)
        name_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        name_label.setMinimumWidth(0)
        name_label.setStyleSheet(
            f"{get_font_family_css()} {font_size_css(12)} padding: 0 4px;"
        )

        main_layout.addWidget(icon_label)
        main_layout.addWidget(name_label)

        # 显示绝对路径（自动中间省略，窗口缩小时优先压缩）
        self._path_label = BodyLabel("", self)
        self._path_label.setStyleSheet(
            f"color: #8c99ad; {get_font_family_css()} {font_size_css(10)}"
        )
        self._path_label.setToolTip(self.file_path)  # 悬浮显示完整路径
        self._path_label.setWordWrap(False)
        self._path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._path_label.setMinimumWidth(0)
        main_layout.addWidget(self._path_label, 1)

        # 操作按钮
        # 工作目录按钮（仅文件夹显示）
        self.wd_btn = None
        if self._is_folder:
            self.wd_btn = TransparentToolButton(get_icon("根目录"), self)
            self.wd_btn.setToolTip("设置为工作目录（工具将在此目录下使用相对路径）")
            self.wd_btn.setFixedSize(24, 24)
            if self._is_working_dir:
                self.wd_btn.setStyleSheet("""
                    TransparentToolButton {
                        background-color: rgba(46, 160, 67, 0.3);
                        border: 1px solid rgba(46, 160, 67, 0.6);
                        border-radius: 4px;
                    }
                """)
            self.wd_btn.clicked.connect(lambda: self.setAsWorkingDir.emit(self.file_path))
            main_layout.addWidget(self.wd_btn)

        # 检测 git worktree（仅当是文件夹且被标记为根目录时才检测）
        self._repo_info = None
        if self._is_folder and self._is_working_dir:
            from app.utils.git_worktree import GitWorktreeDetector
            self._repo_info = GitWorktreeDetector.get_repo_info(self.file_path)

        self.open_btn = TransparentToolButton(FluentIcon.FOLDER, self)
        self.open_btn.setToolTip("打开所在文件夹")
        self.open_btn.clicked.connect(lambda: self.open_folder.emit(self.file_path))

        self.remove_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.remove_btn.setToolTip("移除")
        self.remove_btn.clicked.connect(lambda: self.removed.emit(self.doc_id))

        main_layout.addWidget(self.open_btn)
        main_layout.addWidget(self.remove_btn)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_path_elision()

    def _update_path_elision(self):
        """根据可用宽度自动省略路径（中间截断），窗口缩小时优先压缩路径"""
        if not hasattr(self, '_path_label') or self._path_label is None:
            return
        full_path = self.file_path
        available_width = self._path_label.width()
        if available_width <= 0:
            self._path_label.setText(full_path)
            return
        fm = self._path_label.fontMetrics()
        elided = fm.elidedText(full_path, Qt.ElideMiddle, available_width)
        self._path_label.setText(elided)


class DropZoneWidget(QWidget):
    """拖拽区域组件"""

    files_dropped = pyqtSignal(list)  # file_paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        self.setMinimumHeight(60)
        self.setMaximumHeight(80)
        self.setAcceptDrops(True)
        self.setStyleSheet(f"""
            QWidget {{
                background-color: rgba(37, 37, 38, 150);
                border: 2px dashed rgba(62, 62, 66, 200);
                border-radius: 6px;
                {get_font_family_css()}
            }}
            QWidget:hover {{
                border-color: rgba(14, 99, 156, 200);
                background-color: rgba(50, 50, 55, 150);
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        icon_label = BodyLabel("📁", self)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet(font_size_css(20))
        layout.addWidget(icon_label)

        label = BodyLabel("拖拽文件到此处 或 点击选择文件", self)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(
            f"color: #8c99ad; {get_font_family_css()} {font_size_css(11)}"
        )
        layout.addWidget(label)

        self._is_drag_over = False

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._is_drag_over = True
            self.update()

    def dragLeaveEvent(self, event):
        self._is_drag_over = False
        self.update()

    def dropEvent(self, event: QDropEvent):
        self._is_drag_over = False
        self.update()

        file_paths = []
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path and os.path.isfile(path):
                    file_paths.append(path)

        if file_paths:
            self.files_dropped.emit(file_paths)

    def mousePressEvent(self, event):
        """点击打开文件选择对话框"""
        if event.button() == Qt.LeftButton:
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "选择关键文档",
                "",
                "所有文件 (*.*);;文本文件 (*.txt *.md);;代码文件 (*.py *.js *.ts)"
            )
            if files:
                self.files_dropped.emit(files)


class MemoryCardContent(QWidget):
    """记忆卡片内容区域 - 3 Tab 结构"""

    memorySaved = pyqtSignal(list)
    projectNoteChanged = pyqtSignal(str, str)  # project, content
    workingDirChanged = pyqtSignal(str)  # 工作目录路径，空字符串=清除

    def __init__(self, memory_manager, parent=None):
        super().__init__(parent)
        self._memory_manager = memory_manager
        self._current_project = "默认项目"
        self._current_tab = TAB_ENTRY_MEMORIES
        self._search_filter = ""  # 搜索过滤文本
        self._init_ui()

    def _get_memory_manager(self):
        """获取 memory_manager"""
        if self._memory_manager:
            return self._memory_manager
        parent = self.parent()
        while parent:
            if hasattr(parent, '_memory_manager'):
                return parent._memory_manager
            parent = parent.parent()
        return None

    def set_project(self, project: str):
        """设置当前项目"""
        if self._current_project != project:
            self._current_project = project
        # 强制刷新项目笔记和关键文档
        self._load_project_note()
        self._load_key_documents()

    def _init_ui(self):
        self.setStyleSheet(f"""
            QWidget {{
                background: transparent;
            }}
            QListWidget {{
                background-color: rgba(37, 37, 38, 180);
                border: 1px solid rgba(62, 62, 66, 150);
                color: #e0e0e0;
                border-radius: 6px;
            }}
            QListWidget::item {{
                padding: 0;
                border-bottom: 1px solid rgba(62, 62, 66, 80);
            }}
            QListWidget::item:selected {{
                background-color: rgba(9, 71, 113, 150);
            }}
            BodyLabel {{
                color: #e0e0e0;
                {get_font_family_css()}
            }}
            QTextEdit, QPlainTextEdit {{
                background-color: rgba(37, 37, 38, 180);
                border: 1px solid rgba(62, 62, 66, 150);
                color: #e0e0e0;
                border-radius: 6px;
                padding: 8px;
                {get_font_family_css()} {font_size_css(13)}
            }}
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(6)

        # 内容区域容器
        self.content_stack = QWidget(self)
        stack_layout = QVBoxLayout(self.content_stack)
        stack_layout.setContentsMargins(0, 0, 0, 0)
        stack_layout.setSpacing(0)

        # Tab 1: 条目记忆
        self._tab_entries = self._create_entries_tab()
        stack_layout.addWidget(self._tab_entries)

        # Tab 2: 项目笔记
        self._tab_notes = self._create_notes_tab()
        self._tab_notes.setVisible(False)
        stack_layout.addWidget(self._tab_notes)

        # Tab 3: 关键文档
        self._tab_docs = self._create_docs_tab()
        self._tab_docs.setVisible(False)
        stack_layout.addWidget(self._tab_docs)

        main_layout.addWidget(self.content_stack, 1)

    def _create_entries_tab(self) -> QWidget:
        """创建条目记忆 Tab（搜索移到了头部）"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 记忆列表
        self.entries_list = ListWidget(self)
        self.entries_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.entries_list.setStyleSheet(f"""
            QListWidget {{
                background-color: rgba(37, 37, 38, 180);
                border: 1px solid rgba(62, 62, 66, 150);
                color: #e0e0e0;
                border-radius: 6px;
                {get_font_family_css()}
            }}
            QListWidget::item {{
                padding: 0;
                border-bottom: 1px solid rgba(62, 62, 66, 80);
            }}
        """)
        layout.addWidget(self.entries_list, 1)

        # 添加区域
        add_layout = QHBoxLayout()
        add_layout.setSpacing(6)
        self.entry_input = LineEdit(self)
        self.entry_input.setFixedHeight(28)
        self.entry_input.setPlaceholderText("添加新的条目记忆...")
        self.entry_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: rgba(37, 37, 38, 180);
                border: 1px solid rgba(62, 62, 66, 150);
                color: #e0e0e0;
                padding: 4px 8px;
                border-radius: 4px;
                {get_font_family_css()} {font_size_css(12)}
            }}
        """)
        self.entry_add_btn = PrimaryPushButton("添加", self)
        self.entry_add_btn.setFixedSize(50, 28)
        self.entry_add_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #0e639c;
                color: white;
                border: none;
                border-radius: 4px;
                {font_size_css(12)}
            }}
            QPushButton:hover {{
                background-color: #1177bb;
            }}
        """)
        self.entry_add_btn.clicked.connect(self._add_entry)
        self.entry_input.returnPressed.connect(self._add_entry)
        add_layout.addWidget(self.entry_input, 1)
        add_layout.addWidget(self.entry_add_btn)
        layout.addLayout(add_layout)

        return widget

    def _create_notes_tab(self) -> QWidget:
        """创建项目笔记 Tab"""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(6)

        # 顶部水平布局：左边项目名，右边字数/token统计
        top_layout = QHBoxLayout()
        top_layout.setSpacing(0)

        # 项目名标签
        self.project_name_label = BodyLabel(f"项目: {self._current_project}", self)
        self.project_name_label.setStyleSheet(
            f"color: #8c99ad; {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
        )
        top_layout.addWidget(self.project_name_label)

        # 占位拉伸
        top_layout.addStretch()

        # 字数/token统计标签
        self.notes_stats_label = BodyLabel("0 字 / 0 token", self)
        self.notes_stats_label.setStyleSheet(
            f"color: #8c99ad; {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
        )
        top_layout.addWidget(self.notes_stats_label)

        main_layout.addLayout(top_layout)

        # Markdown 编辑器
        self.notes_editor = TextEdit(self)
        self.notes_editor.setPlaceholderText("在此记录项目笔记，支持 Markdown 格式...")
        # 监听内容变化更新统计并触发自动保存（带节流）
        self.notes_editor.textChanged.connect(self._update_notes_stats)
        self.notes_editor.textChanged.connect(self._on_notes_changed)
        
        # 自动保存定时器（节流防频繁保存）
        from PyQt5.QtCore import QTimer
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(1000)  # 1秒后保存
        self._auto_save_timer.timeout.connect(self._save_project_note)
        
        main_layout.addWidget(self.notes_editor, 1)

        return widget

    def _update_notes_stats(self):
        """更新字数和 token 统计"""
        content = self.notes_editor.toPlainText()
        char_count = len(content)
        # 简单估算 token：按中文字符约 1:1，英文约 4:1，这里用近似算法
        # 中文占比高，按字符数的 0.8 估算
        token_estimate = int(char_count * 0.8)
        self.notes_stats_label.setText(f"{char_count:,} 字 / {token_estimate:,} token")

    def _on_notes_changed(self):
        """内容变化时触发自动保存（带节流）"""
        # 重置定时器，用户持续输入时不会保存，停止 1 秒后才保存
        if hasattr(self, '_auto_save_timer') and self._auto_save_timer:
            self._auto_save_timer.start()

    def _create_docs_tab(self) -> QWidget:
        """创建关键文档 Tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 文档列表（支持拖拽）
        self.docs_list = DocDropListWidget(self)  # 使用支持拖拽的列表
        self.docs_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.docs_list.setStyleSheet(f"""
            QListWidget {{
                background-color: rgba(37, 37, 38, 180);
                border: 2px dashed rgba(62, 62, 66, 200);
                color: #e0e0e0;
                border-radius: 6px;
                {get_font_family_css()}
            }}
            QListWidget::item {{
                padding: 0;
                border-bottom: 1px solid rgba(62, 62, 66, 80);
            }}
        """)
        self.docs_list.files_dropped.connect(self._on_files_dropped)
        layout.addWidget(self.docs_list, 1)

        # 添加按钮（右对齐）
        add_btn_layout = QHBoxLayout()
        add_btn_layout.setSpacing(6)
        add_btn_layout.addStretch()
        
        self.add_doc_btn = PrimaryPushButton("📄 添加文件", self)
        self.add_doc_btn.setFixedHeight(28)
        self.add_doc_btn.setFixedWidth(110)
        self.add_doc_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #0e639c;
                color: white;
                border: none;
                border-radius: 4px;
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
                padding: 0 8px;
            }}
            QPushButton:hover {{
                background-color: #1177bb;
            }}
        """)
        self.add_doc_btn.clicked.connect(self._on_add_file_clicked)
        
        self.add_folder_btn = PrimaryPushButton("📁 添加文件夹", self)
        self.add_folder_btn.setFixedHeight(28)
        self.add_folder_btn.setFixedWidth(120)
        self.add_folder_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #2d882d;
                color: white;
                border: none;
                border-radius: 4px;
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
                padding: 0 8px;
            }}
            QPushButton:hover {{
                background-color: #3a9e3a;
            }}
        """)
        self.add_folder_btn.clicked.connect(self._on_add_folder_clicked)
        
        add_btn_layout.addWidget(self.add_doc_btn)
        add_btn_layout.addWidget(self.add_folder_btn)
        layout.addLayout(add_btn_layout)

        return widget

    def _on_add_file_clicked(self):
        """点击添加文件按钮"""
        from PyQt5.QtWidgets import QFileDialog
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择关键文档",
            "",
            "所有文件 (*.*)"
        )
        if files:
            self._on_files_dropped(files)

    def _on_add_folder_clicked(self):
        """点击添加文件夹按钮"""
        from PyQt5.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择文件夹",
            "",
            QFileDialog.ShowDirsOnly
        )
        if folder:
            self._on_files_dropped([folder])

    def _on_tab_changed(self, tab_key: str):
        """切换 Tab"""
        self._current_tab = tab_key

        self._tab_entries.setVisible(tab_key == TAB_ENTRY_MEMORIES)
        self._tab_notes.setVisible(tab_key == TAB_PROJECT_NOTES)
        self._tab_docs.setVisible(tab_key == TAB_KEY_DOCUMENTS)

        self._refresh_current_tab()

    def _refresh_current_tab(self):
        """刷新当前 Tab 的内容"""
        if self._current_tab == TAB_ENTRY_MEMORIES:
            self._load_entries()
        elif self._current_tab == TAB_PROJECT_NOTES:
            self._load_project_note()
        elif self._current_tab == TAB_KEY_DOCUMENTS:
            self._load_key_documents()

    # ==================== 条目记忆操作 ====================

    def _load_entries(self):
        """加载条目记忆（使用 self._search_filter 过滤）"""
        self.entries_list.clear()
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        entries = memory_mgr.get_entry_memories(self._search_filter)
        for entry in entries:
            memory_id = entry.get("id", "")
            content = entry.get("content", "")
            enabled = entry.get("enabled", True)
            source = entry.get("source", "manual")

            item = QListWidgetItem()
            item.setSizeHint(self._get_entry_item_size(content))
            widget = EntryMemoryItemWidget(memory_id, content, enabled, source)
            widget.deleted.connect(self._delete_entry)
            widget.toggled.connect(self._toggle_entry)
            widget.edited.connect(self._edit_entry)
            self.entries_list.addItem(item)
            self.entries_list.setItemWidget(item, widget)

    def _get_entry_item_size(self, content: str):
        from PyQt5.QtCore import QSize
        width = self.entries_list.size().width()
        if width <= 0:
            width = 400
        
        # 根据内容估算行数，12px字体，每行约30个中文，加上边距
        lines = content.count('\n') + 1
        # 自动换行，按宽度估算额外行数
        chars_per_line = int(width / 7)  # 每个中文字符约7-8px
        if chars_per_line > 0:
            lines += (len(content) + chars_per_line - 1) // chars_per_line - 1
        
        # 行高约 20px，上下边距 + 按钮空间，多留一些余量避免遮挡
        height = max(48, int(20 * lines) + 20)
        return QSize(width, height)

    def set_search_filter(self, text: str):
        """设置搜索过滤文本"""
        self._search_filter = text.strip()
        if self._current_tab == TAB_ENTRY_MEMORIES:
            self._load_entries()
        elif self._current_tab == TAB_PROJECT_NOTES:
            self._search_in_notes()
        elif self._current_tab == TAB_KEY_DOCUMENTS:
            self._load_key_documents()

    def _search_in_notes(self):
        """在笔记编辑器内搜索文本"""
        if not self._search_filter:
            return
        from PyQt5.QtGui import QTextCursor
        # 查找文本并选中
        cursor = self.notes_editor.textCursor()
        cursor.movePosition(QTextCursor.Start)
        self.notes_editor.setTextCursor(cursor)
        found = self.notes_editor.find(self._search_filter)
        if not found:
            # 未找到时重置光标位置
            cursor.movePosition(QTextCursor.Start)
            self.notes_editor.setTextCursor(cursor)

    def switch_tab(self, tab_id: str):
        """切换标签（由头部标签按钮触发）"""
        if self._current_tab != tab_id:
            self._current_tab = tab_id
            self._on_tab_changed(tab_id)

    def _add_entry(self):
        """添加条目"""
        content = self.entry_input.text().strip()
        if not content:
            return

        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            memory_mgr.add_entry_memory(content)

        self.entry_input.clear()
        self._load_entries()

    def _delete_entry(self, memory_id: str):
        """删除条目"""
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            memory_mgr.delete_entry_memory(memory_id)
        self._load_entries()

    def _toggle_entry(self, memory_id: str, enabled: bool):
        """切换条目"""
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            memory_mgr.toggle_entry_memory(memory_id, enabled)

    def _edit_entry(self, memory_id: str, content: str):
        """编辑条目"""
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            memory_mgr.update_entry_memory(memory_id, content)

    # ==================== 项目笔记操作 ====================

    def _load_project_note(self):
        """加载项目笔记"""
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        self.project_name_label.setText(f"项目: {self._current_project}")
        note = memory_mgr.get_or_create_project_note(self._current_project)
        content = note.get("content", "") if note else ""
        self.notes_editor.setPlainText(content)
        self._update_notes_stats()

    def _save_project_note(self):
        """保存项目笔记"""
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            content = self.notes_editor.toPlainText()
            memory_mgr.save_project_note(self._current_project, content)
            self.projectNoteChanged.emit(self._current_project, content)

    # ==================== 关键文档操作 ====================

    def _load_key_documents(self):
        """加载关键文档（过滤掉 git_worktree 条目，但保留根目录视觉效果）"""
        self.docs_list.clear()
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        all_docs = memory_mgr.get_key_documents(self._current_project)

        # 获取实际工作目录
        actual_wd = memory_mgr.get_working_directory(self._current_project)

        # 预检：当前工作目录是否指向 worktree（需要在循环前确定，用于后续 git 检测判断）
        is_worktree_active = False
        if actual_wd:
            is_worktree_active = any(
                d.get("file_path") == actual_wd and d.get("added_by") == "git_worktree"
                for d in all_docs
            )

        # 查找原始 git 仓库路径（用于 worktree 模式下的显示和恢复）
        # 注意：worktree 本身不能作为 original_repo_path，必须是实际的 git 仓库文件夹
        original_repo_path = None
        if actual_wd:
            for d in all_docs:
                # 排除 worktree 条目本身
                if d.get("added_by") == "git_worktree":
                    continue
                # 仅检测：根目录 或 worktree 激活时
                if original_repo_path is None and (d.get("is_working_dir", False) or is_worktree_active):
                    try:
                        if GitWorktreeDetector.detect_git(d.get("file_path", "")):
                            original_repo_path = d["file_path"]
                    except Exception:
                        pass

        # 过滤掉 git_worktree（不显示在 UI 中）
        docs = [d for d in all_docs if d.get("added_by") != "git_worktree"]

        # 搜索过滤
        if self._search_filter:
            keyword = self._search_filter.lower()
            docs = [d for d in docs if keyword in d.get("file_name", "").lower() or keyword in d.get("file_path", "").lower()]

        # 工作目录置顶（如果 worktree 激活则原始仓库置顶，否则按 DB 标记）
        if is_worktree_active and original_repo_path:
            docs.sort(key=lambda d: (0 if d.get("file_path") == original_repo_path else 1, d.get("added_at", "")))
        else:
            docs.sort(key=lambda d: (0 if d.get("is_working_dir") else 1, d.get("added_at", "")))

        inserted_worktree = False
        self._original_folder_for_worktree = None
        for doc in docs:
            doc_id = doc.get("id", "")
            file_name = doc.get("file_name", "")
            file_path = doc.get("file_path", "")
            added_by = doc.get("added_by", "manual")
            # 根目录标记：如果 worktree 激活且是原始仓库，显示为根目录
            db_is_wd = doc.get("is_working_dir", False)
            show_as_wd = db_is_wd or (is_worktree_active and file_path == original_repo_path)

            item = QListWidgetItem()
            item.setSizeHint(self._get_doc_item_size())
            widget = KeyDocumentItemWidget(
                doc_id, file_name, file_path, added_by,
                is_working_dir=show_as_wd,
            )
            # worktree 激活时：原仓库虽不是根目录，但需要 _repo_info 来显示 worktree 区域
            if is_worktree_active and file_path == original_repo_path and not widget._repo_info:
                widget._repo_info = GitWorktreeDetector.get_repo_info(file_path)
            widget.removed.connect(self._remove_key_document)
            widget.open_folder.connect(self._open_folder)
            widget.setAsWorkingDir.connect(self._set_as_working_directory)
            widget.worktreeChanged.connect(self._on_worktree_changed)
            self.docs_list.addItem(item)
            self.docs_list.setItemWidget(item, widget)

            # 在第一个 git 仓库文件夹后插入 worktree 树（仅当有活跃的工作目录）
            has_active_wd = actual_wd is not None and actual_wd != "clear"
            if has_active_wd and not inserted_worktree and widget._repo_info:
                inserted_worktree = True
                self._original_folder_for_worktree = file_path
                wt_item = QListWidgetItem()
                wt_item.setSizeHint(self._get_worktree_section_size(widget._repo_info))
                wt_widget = WorktreeSectionWidget(
                    widget._repo_info, file_path, self,
                    current_workdir=actual_wd,
                )
                wt_widget.sizeChanged.connect(lambda h, item=wt_item: (
                    item.setSizeHint(QSize(self.docs_list.size().width() or 400, h)),
                    self.docs_list.update(),
                ))
                wt_widget.worktreeSwitched.connect(self._on_worktree_changed)
                wt_widget.worktreeDeleted.connect(self._on_worktree_deleted)
                self.docs_list.addItem(wt_item)
                self.docs_list.setItemWidget(wt_item, wt_widget)

    def _get_worktree_section_size(self, repo_info):
        """计算 worktree 树状组件的高度"""
        from PyQt5.QtCore import QSize
        width = self.docs_list.size().width()
        if width <= 0:
            width = 400
        # 每个 worktree 行 24px + 新建行 24px + 边距
        wt_count = len(repo_info.worktrees) if repo_info.worktrees else 1
        height = wt_count * 24 + 24 + 4
        return QSize(width, height)

    def _get_doc_item_size(self):
        from PyQt5.QtCore import QSize
        width = self.docs_list.size().width()
        if width <= 0:
            width = 400
        return QSize(width, 44)

    def _on_files_dropped(self, file_paths: list):
        """处理文件拖拽/选择"""
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        for path in file_paths:
            memory_mgr.add_key_document(self._current_project, path, "manual")

        self._load_key_documents()

    def _remove_key_document(self, doc_id: str):
        """移除关键文档"""
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            memory_mgr.remove_key_document(doc_id)
        self._load_key_documents()

    def _set_as_working_directory(self, file_path: str):
        """设置为工作目录（再次点击取消）"""
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return
        # 检查当前是否已经是工作目录（如果再次点击则取消）
        current_wd = memory_mgr.get_working_directory(self._current_project)
        if current_wd == file_path:
            # 取消设置
            memory_mgr.set_working_directory(self._current_project, "clear")
            self.workingDirChanged.emit("")
        else:
            memory_mgr.set_working_directory(self._current_project, file_path)
            self.workingDirChanged.emit(file_path)
        self._load_key_documents()

    def _on_worktree_changed(self, original_folder: str, worktree_path: str):
        """Worktree 切换：写入 DB + 切换 workdir（UI 层过滤不显示 git_worktree 条目）"""
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return
        
        # 必须写入 DB，否则 set_working_directory 找不到路径
        # added_by="git_worktree" 标记，UI 显示时过滤掉
        memory_mgr.add_key_document(self._current_project, worktree_path, "git_worktree")
        
        # 设为工作目录
        memory_mgr.set_working_directory(self._current_project, worktree_path)
        self.workingDirChanged.emit(worktree_path)
        self._load_key_documents()

    def _on_worktree_deleted(self, worktree_path: str):
        """Worktree 被删除后：移除 DB 记录 + 恢复到主仓库"""
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        current_wd = memory_mgr.get_working_directory(self._current_project)

        # 从关键文档中移除 worktree 路径（防止下次加载又显示）
        if memory_mgr._key_documents_repo:
            memory_mgr._key_documents_repo.remove_by_path(self._current_project, worktree_path)

        if current_wd == worktree_path:
            # 恢复到原始 git 仓库文件夹
            repo_root = GitWorktreeDetector.detect_git(self._original_folder_for_worktree)
            if repo_root:
                memory_mgr.set_working_directory(self._current_project, repo_root)
                self.workingDirChanged.emit(repo_root)
            else:
                memory_mgr.set_working_directory(self._current_project, "clear")
                self.workingDirChanged.emit("")

        self._load_key_documents()

    def _open_folder(self, path: str):
        """打开文件或文件夹"""
        import subprocess
        import os
        try:
            # 优先判断路径类型
            if os.path.isdir(path):
                # 文件夹：直接打开
                os.startfile(path) if os.name == 'nt' else subprocess.Popen(['xdg-open', path])
            elif os.path.isfile(path):
                # 文件：直接打开
                os.startfile(path) if os.name == 'nt' else subprocess.Popen(['open', path])
            else:
                # 路径不存在，尝试打开父目录
                folder = os.path.dirname(path)
                if folder and os.path.exists(folder):
                    subprocess.Popen(['explorer', '/select,', path])
        except Exception as e:
            from loguru import logger
            logger.error(f"Failed to open: {e}")

    def refresh(self):
        """刷新所有数据"""
        self._refresh_current_tab()

    def refresh_from_db(self):
        """刷新所有数据（兼容旧接口）"""
        self._refresh_current_tab()