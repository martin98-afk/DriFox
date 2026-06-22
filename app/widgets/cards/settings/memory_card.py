# -*- coding: utf-8 -*-
"""
记忆管理卡片 - 重构为 3 Tab 结构
1. 条目记忆 - 列表 + 搜索 + 编辑
2. 项目笔记 - Markdown 编辑器
3. 关键文档 - 列表 + 拖拽添加
"""
import os
from typing import Dict

from PyQt5.QtCore import pyqtSignal, Qt, QSize, QTimer
from PyQt5.QtGui import QDropEvent, QDragEnterEvent, QDragMoveEvent, QColor, QKeyEvent
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
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
    MaskDialogBase,
    PushButton,
)


class EntryInputLineEdit(LineEdit):
    """自定义输入框 - 拦截回车键防止事件冒泡"""

    def keyPressEvent(self, event: QKeyEvent):
        """拦截回车键，阻止事件冒泡到父组件"""
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            # 先让父类处理（触发 returnPressed 信号），然后接受事件防止冒泡
            super().keyPressEvent(event)
            event.accept()
            return
        super().keyPressEvent(event)


from app.utils.design_tokens import scale_font_size, font_size_css, Colors
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.elided_label import _ElidedLabel
from app.utils.git_worktree import GitWorktreeDetector
from app.widgets.worktree_section import WorktreeSectionWidget
from app.widgets.cards.settings.project_selector_card import (
    get_project_color, _SquareAvatar, extract_project_initials,
)

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

        # 内容区域（stretch=1 让文本区优先吃满剩余空间，按钮区固定宽度在尾部）
        self.text_widget = QWidget(self)
        # 允许收缩，适应小窗口
        self.text_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.MinimumExpanding)
        self.text_widget.setMinimumWidth(0)
        text_layout = QVBoxLayout(self.text_widget)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)

        Colors.refresh()
        self.content_label = _ElidedLabel(self._content, self.text_widget)
        self.content_label.setToolTip(self._content)  # 悬浮显示完整内容
        self.content_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; padding: 4px; {get_font_family_css()} {font_size_css(12)}"
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
        Colors.refresh()
        self.edit_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                border: 1px solid {Colors.BORDER_ACCENT};
                color: {Colors.TEXT_PRIMARY};
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

        # 操作按钮 — 直接加入 main_layout，固定宽度不放 stretch，始终靠右
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

        # 按钮直接加入（text_widget stretch=1 自然将按钮推到右侧）
        main_layout.addWidget(self.edit_btn)
        main_layout.addWidget(self.delete_btn)
        main_layout.addWidget(self.switch)

    def sizeHint(self):
        """单行省略预览 + 编辑模式自适应高度"""
        if self._editing and self.edit_widget.isVisible():
            edit_height = self.edit_text.height()
            return QSize(0, max(44, edit_height + 16))

        # 单行省略：固定高度，字体 12px + padding 8px(上下) = 行高 28px，但保持最小 44px
        return QSize(0, 44)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_item_size()

    def _update_item_size(self):
        item = self._get_item()
        if item:
            item.setSizeHint(self.sizeHint())
            lst = self.parent()
            while lst and not isinstance(lst, ListWidget):
                lst = lst.parent()
            if lst:
                lst.doItemsLayout()

    def _adjust_edit_height(self):
        """根据内容调整编辑框高度，不超过最大高度，超出可滚动"""
        doc = self.edit_text.document()
        doc_height = int(doc.size().height() + 10)
        height = max(36, min(doc_height, 200))
        self.edit_text.setFixedHeight(height)
        self._update_item_size()
    
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
        # 编辑后更新自身的 sizeHint，让列表行高自适应
        self.updateGeometry()
        item = self._get_item()
        if item:
            item.setSizeHint(self.sizeHint())

    def _get_item(self):
        """反向查找当前 widget 所在的 QListWidgetItem"""
        from qfluentwidgets import ListWidget
        lst = self.parent()
        while lst and not isinstance(lst, ListWidget):
            lst = lst.parent()
        if lst:
            for i in range(lst.count()):
                if lst.itemWidget(lst.item(i)) is self:
                    return lst.item(i)
        return None

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


class SingleInputDialog(MaskDialogBase):
    """单输入弹框：标题 + 可选提示 + 1 个输入框 + 确认/取消按钮。

    修复 MaskDialogBase 默认 widget 被拉伸到父窗口的问题：
    - widget 大小固定（不跟随父窗口 resize 拉伸）
    - 始终居中显示

    Usage:
        dialog = SingleInputDialog(
            title="🔗 添加 URL 链接",
            hint="请输入网页链接，添加为关键文档引用",   # 可选
            placeholder="https://example.com",
            default_text="https://",                       # 可选
            confirm_text="添加",
            cancel_text="取消",
            parent=parent,
        )
        dialog.confirmed.connect(handler)
        dialog.exec_()
    """

    # 通用信号
    confirmed = pyqtSignal(str)
    # 兼容旧 URL 场景的别名
    urlConfirmed = pyqtSignal(str)

    DEFAULT_WIDTH = 420
    DEFAULT_HEIGHT = 220

    def __init__(
        self,
        title: str,
        placeholder: str = "",
        hint: str = "",
        default_text: str = "",
        confirm_text: str = "确认",
        cancel_text: str = "取消",
        parent=None,
    ):
        super().__init__(parent)
        self._init_ui(title, hint, placeholder, default_text, confirm_text, cancel_text)

    def _init_ui(self, title, hint, placeholder, default_text, confirm_text, cancel_text):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        # 对话框内容区背景
        # MaskDialogBase.widget 是 QFrame，用 objectName 选择器精确命中自身，
        # 避免 .QFrame 选择器误染所有子 QFrame（如 LineEdit 内部 QFrame 背景）。
        self.widget.setObjectName("singleInputDialogWidget")
        self.widget.setStyleSheet(f"""
            #singleInputDialogWidget {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        # 主布局
        self.vBoxLayout = QVBoxLayout(self.widget)
        self.vBoxLayout.setContentsMargins(24, 24, 24, 24)
        self.vBoxLayout.setSpacing(12)

        # 标题
        title_label = BodyLabel(title, self.widget)
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(16)}"
        )
        self.vBoxLayout.addWidget(title_label)

        # 提示文字（可选）
        self.hint_label = None
        if hint:
            self.hint_label = BodyLabel(hint, self.widget)
            self.hint_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)}"
            )
            self.vBoxLayout.addWidget(self.hint_label)

        # 输入框
        self.input = LineEdit(self.widget)
        if placeholder:
            self.input.setPlaceholderText(placeholder)
        if default_text:
            self.input.setText(default_text)
        self.input.setFixedHeight(36)
        self.input.setClearButtonEnabled(True)
        self.input.setStyleSheet(f"""
            LineEdit {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.INPUT_BG_START}, stop:1 {Colors.INPUT_BG_END});
                border: 1px solid {Colors.INPUT_BORDER};
                color: {Colors.INPUT_TEXT};
                padding: 4px 12px;
                border-radius: 6px;
                {get_font_family_css()} {font_size_css(13)}
            }}
            LineEdit:focus {{
                border-color: {Colors.INFO};
            }}
        """)
        # 回车键确认
        self.input.returnPressed.connect(self._on_accept)
        self.input.setFocus()
        self.vBoxLayout.addWidget(self.input)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        btn_layout.addStretch()

        # 取消按钮
        cancel_btn = PushButton(cancel_text, self.widget)
        cancel_btn.setStyleSheet(f"""
            PushButton {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px 20px;
                {font_size_css(12)}
            }}
            PushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.BORDER_ACCENT};
            }}
        """)
        cancel_btn.clicked.connect(self.close)

        # 确认按钮
        confirm_btn = PrimaryPushButton(confirm_text, self.widget)
        confirm_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                background-color: {Colors.INFO};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 4px 20px;
                {font_size_css(12)}
            }}
            PrimaryPushButton:hover {{
                background-color: {Colors.SEND_BTN_END};
            }}
        """)
        confirm_btn.clicked.connect(self._on_accept)

        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        self.vBoxLayout.addLayout(btn_layout)

        # 固定 widget 大小，避免被 MaskDialogBase 拉伸到与父窗口同步
        # （MaskDialogBase 强制 dialog 大小 = parent 大小，无 setMaximumSize 时
        #   QFrame 默认 sizePolicy 允许拉伸，widget 会被撑成全屏）
        self.widget.setFixedSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        # 初始居中显示；后续 dialog resize 时通过 _center_widget 保持居中
        self._center_widget()

    def _center_widget(self):
        """让 widget 在 dialog 中保持居中（MaskDialogBase 不自动居中）"""
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        # 先调 super 让 MaskDialogBase 把 windowMask 同步到 dialog 大小，
        # 然后重新居中 widget
        super().resizeEvent(e)
        self._center_widget()

    def _on_accept(self):
        text = self.input.text().strip()
        if not text:
            return
        self.confirmed.emit(text)
        self.urlConfirmed.emit(text)  # 兼容旧用法
        self.close()

    def showEvent(self, e):
        super().showEvent(e)
        self.input.setFocus()
        # 选中输入框中的文本（方便直接粘贴覆盖）
        self.input.selectAll()


# 向后兼容别名（旧的 URL 引用代码）
UrlInputDialog = SingleInputDialog


class KeyDocumentItemWidget(QWidget):
    """关键文档项组件"""

    removed = pyqtSignal(str)  # doc_id
    open_file = pyqtSignal(str)  # file_path
    open_folder = pyqtSignal(str)  # folder_path
    setAsWorkingDir = pyqtSignal(str)  # file_path
    worktreeChanged = pyqtSignal(str, str)  # (original_folder, worktree_path)

    @staticmethod
    def _is_url(path: str) -> bool:
        """判断路径是否为 URL"""
        return bool(path and (path.startswith('http://') or path.startswith('https://')))

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
        self._is_url = self._is_url(file_path)
        self._is_folder = (os.path.isdir(file_path) if file_path and not self._is_url else False)
        self._is_working_dir = is_working_dir and self._is_folder
        self._init_ui(file_name, file_path, added_by)

    def _get_icon(self, file_name: str, file_path: str) -> str:
        """根据文件类型获取对应图标，文件夹单独处理"""
        import os
        # URL 链接
        if self._is_url:
            return "🔗"
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

        # URL 使用域名作为名称，非 URL 使用文件名
        display_name = file_name
        if self._is_url and file_path:
            from urllib.parse import urlparse
            parsed = urlparse(file_path)
            domain = parsed.netloc or file_path
            display_name = domain
        name_label = BodyLabel(display_name, self)
        name_label.setWordWrap(False)
        name_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        name_label.setMinimumWidth(0)
        name_label.setStyleSheet(
            f"{get_font_family_css()} {font_size_css(12)} padding: 0 4px;"
        )

        main_layout.addWidget(icon_label)
        main_layout.addWidget(name_label)

        # 显示绝对路径/URL（自动中间省略，窗口缩小时优先压缩）
        self._path_label = BodyLabel("", self)
        Colors.refresh()
        path_display = self.file_path
        if self._is_url:
            # URL 显示完整链接，颜色用链接色
            self._path_label.setStyleSheet(
                f"color: {Colors.INFO}; {get_font_family_css()} {font_size_css(10)} text-decoration: underline;"
            )
        else:
            self._path_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(10)}"
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
        if self._is_url:
            self.open_btn.setToolTip("在浏览器中打开链接")
        else:
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
        Colors.refresh()
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                border: 2px dashed {Colors.BORDER};
                border-radius: 6px;
                {get_font_family_css()}
            }}
            QWidget:hover {{
                border-color: {Colors.BORDER_ACCENT};
                background-color: {Colors.CARD_BG.format(alpha=180)};
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
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}"
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
        # 多窗口隔离：实例级工作目录缓存（{project: workdir_path}）
        # 优先级：实例缓存 > DB；DB 写入仅作为新窗口的默认恢复值
        self._instance_workdir: Dict[str, str] = {}
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

    def _get_effective_workdir(self, project: str):
        """获取有效工作目录（多窗口隔离：实例缓存优先，回退 DB）

        实例缓存 _instance_workdir 记录了当前窗口用户的选择，
        优先于 DB 中其他窗口可能写入的值。
        DB 值仅作为首次启动时的回退默认值。
        """
        # 实例缓存优先（多窗口隔离：保持自身选择）
        workdir = self._instance_workdir.get(project)
        if workdir is not None:
            return workdir if workdir else None
        # 首次启动，从 DB 读取默认值（新窗口恢复用）
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            db_workdir = memory_mgr.get_working_directory(project)
            if db_workdir:
                self._instance_workdir[project] = db_workdir
            return db_workdir
        return None

    def _init_ui(self):
        Colors.refresh()
        self.setStyleSheet(f"""
            QWidget {{
                background: transparent;
            }}
            QListWidget {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                border: 1px solid {Colors.BORDER};
                color: {Colors.TEXT_PRIMARY};
                border-radius: 6px;
            }}
            QListWidget::item {{
                padding: 0;
                border-bottom: 1px solid {Colors.BORDER};
            }}
            QListWidget::item:selected {{
                background-color: {Colors.SELECTED_BG};
            }}
            BodyLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()}
            }}
            QTextEdit, QPlainTextEdit {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.INPUT_BG_START}, stop:1 {Colors.INPUT_BG_END});
                border: 1px solid {Colors.INPUT_BORDER};
                color: {Colors.INPUT_TEXT};
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

        # 添加区域（放在列表上方，方便添加新记忆后立即可见）
        add_layout = QHBoxLayout()
        add_layout.setSpacing(6)
        # 使用自定义输入框，拦截回车键防止事件冒泡
        self.entry_input = EntryInputLineEdit(self)
        self.entry_input.setFixedHeight(28)
        self.entry_input.setPlaceholderText("添加新的条目记忆...")
        self.entry_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                border: 1px solid {Colors.BORDER};
                color: {Colors.TEXT_PRIMARY};
                padding: 4px 8px;
                border-radius: 4px;
                {get_font_family_css()} {font_size_css(12)}
            }}
        """)
        self.entry_add_btn = PrimaryPushButton("添加", self)
        self.entry_add_btn.setFixedSize(50, 28)
        self.entry_add_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.INFO};
                color: white;
                border: none;
                border-radius: 4px;
                {font_size_css(12)}
            }}
            QPushButton:hover {{
                background-color: {Colors.BORDER_ACCENT};
            }}
        """)
        self.entry_add_btn.clicked.connect(self._add_entry)
        # 连接自定义输入框的回车信号
        self.entry_input.returnPressed.connect(self._add_entry)
        add_layout.addWidget(self.entry_input, 1)
        add_layout.addWidget(self.entry_add_btn)
        layout.addLayout(add_layout)

        # 记忆列表
        self.entries_list = ListWidget(self)
        self.entries_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.entries_list.setResizeMode(ListWidget.Adjust)
        self.entries_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                border: 1px solid {Colors.BORDER};
                color: {Colors.TEXT_PRIMARY};
                border-radius: 6px;
                {get_font_family_css()}
            }}
            QListWidget::item {{
                padding: 0;
                border-bottom: 1px solid {Colors.BORDER};
            }}
        """)
        layout.addWidget(self.entries_list, 1)

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

        # 项目方形 icon（缩写字母，18px 小尺寸）
        self._notes_project_avatar = _SquareAvatar(
            extract_project_initials(self._current_project),
            get_project_color(self._current_project),
            self, size=18
        )
        top_layout.addWidget(self._notes_project_avatar)

        # 项目名标签
        self.project_name_label = BodyLabel(f" {self._current_project}", self)
        Colors.refresh()
        self.project_name_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
        )
        top_layout.addWidget(self.project_name_label)

        # 占位拉伸
        top_layout.addStretch()

        # 字数/token统计标签
        self.notes_stats_label = BodyLabel("0 字 / 0 token", self)
        self.notes_stats_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
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
        self._auto_save_timer.setInterval(300)  # 300ms 去抖后保存
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

        # 文档容器（带虚线边框，内部含工具栏行+列表）
        docs_container = QWidget(widget)
        Colors.refresh()
        docs_container.setStyleSheet(f"""
            QWidget#docsContainer {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                border: 2px dashed {Colors.BORDER};
                border-radius: 6px;
            }}
        """)
        docs_container.setObjectName("docsContainer")
        docs_layout = QGridLayout(docs_container)
        docs_layout.setContentsMargins(8, 4, 8, 4)
        docs_layout.setSpacing(4)
        docs_layout.setRowStretch(0, 0)  # 工具栏行
        docs_layout.setRowStretch(1, 1)  # 列表行

        # ── 顶部工具栏行 ──
        toolbar = QWidget(docs_container)
        toolbar.setStyleSheet("background: transparent;")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(4)

        # 左侧标题+数量
        self._docs_header_label = BodyLabel("📁 关键文档", toolbar)
        self._docs_header_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {font_size_css(12)}"
        )
        toolbar_layout.addWidget(self._docs_header_label)

        self._docs_count_label = BodyLabel("", toolbar)
        self._docs_count_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {font_size_css(11)}"
        )
        toolbar_layout.addWidget(self._docs_count_label)

        toolbar_layout.addStretch()

        # 紧凑图标按钮
        self.add_doc_btn = TransparentToolButton(FluentIcon.ADD, toolbar)
        self.add_doc_btn.setFixedSize(24, 24)
        self.add_doc_btn.setToolTip("添加文件")
        self.add_doc_btn.clicked.connect(self._on_add_file_clicked)
        toolbar_layout.addWidget(self.add_doc_btn)

        self.add_folder_btn = TransparentToolButton(FluentIcon.FOLDER, toolbar)
        self.add_folder_btn.setFixedSize(24, 24)
        self.add_folder_btn.setToolTip("添加文件夹")
        self.add_folder_btn.clicked.connect(self._on_add_folder_clicked)
        toolbar_layout.addWidget(self.add_folder_btn)

        # 添加 URL 链接按钮
        self.add_url_btn = TransparentToolButton(FluentIcon.LINK, toolbar)
        self.add_url_btn.setFixedSize(24, 24)
        self.add_url_btn.setToolTip("添加 URL 链接")
        self.add_url_btn.clicked.connect(self._on_add_url_clicked)
        toolbar_layout.addWidget(self.add_url_btn)

        docs_layout.addWidget(toolbar, 0, 0)

        # ── 文档列表（无边框，由外层容器统一虚线边框）──
        self.docs_list = DocDropListWidget(docs_container)
        self.docs_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.docs_list.setResizeMode(ListWidget.Adjust)
        Colors.refresh()
        self.docs_list.setStyleSheet(f"""
            QListWidget {{
                background: transparent;
                border: none;
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()}
            }}
            QListWidget::item {{
                padding: 0;
                border-bottom: 1px solid {Colors.BORDER};
            }}
        """)
        self.docs_list.files_dropped.connect(self._on_files_dropped)
        docs_layout.addWidget(self.docs_list, 1, 0)

        # 空列表提示（叠加在列表中央）
        self._docs_empty_hint = BodyLabel("拖拽项目目录到此并选择设置为根目录即可开始项目开发", docs_container)
        self._docs_empty_hint.setAlignment(Qt.AlignCenter)
        self._docs_empty_hint.setWordWrap(True)
        self._docs_empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        Colors.refresh()
        self._docs_empty_hint.setStyleSheet(
            f"background: transparent; color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(15)} padding: 20px;"
        )
        self._docs_empty_hint.setVisible(False)
        docs_layout.addWidget(self._docs_empty_hint, 1, 0, Qt.AlignCenter)

        layout.addWidget(docs_container, 1)

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

    def _on_add_url_clicked(self):
        """点击添加 URL 链接按钮"""
        dialog = SingleInputDialog(
            title="🔗 添加 URL 链接",
            hint="请输入网页链接，添加为关键文档引用",
            placeholder="https://example.com",
            default_text="https://",
            confirm_text="添加",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(self._on_url_confirmed)
        dialog.exec_()

    def _on_url_confirmed(self, url: str):
        """URL 确认后的处理"""
        url = url.strip()
        # 补全协议前缀
        if not (url.startswith('http://') or url.startswith('https://')):
            url = 'https://' + url
        # 验证 URL 基本格式
        if '.' not in url.replace('https://', '').replace('http://', ''):
            from qfluentwidgets import InfoBar
            InfoBar.warning(
                title='URL 格式不正确',
                content='请输入有效的网页链接（如 https://example.com）',
                parent=self
            )
            return
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            memory_mgr.add_key_document(self._current_project, url, "manual")
        self._load_key_documents()

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

    def refresh_style(self):
        """响应主题切换：刷新所有样式"""
        Colors.refresh()
        self.setStyleSheet(f"""
            QWidget {{
                background: transparent;
            }}
            QListWidget {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                border: 1px solid {Colors.BORDER};
                color: {Colors.TEXT_PRIMARY};
                border-radius: 6px;
            }}
            QListWidget::item {{
                padding: 0;
                border-bottom: 1px solid {Colors.BORDER};
            }}
            QListWidget::item:selected {{
                background-color: {Colors.SELECTED_BG};
            }}
            BodyLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()}
            }}
            QTextEdit, QPlainTextEdit {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                border: 1px solid {Colors.BORDER};
                color: {Colors.TEXT_PRIMARY};
                border-radius: 6px;
                padding: 8px;
                {get_font_family_css()} {font_size_css(13)}
            }}
        """)
        # 刷新子组件的独立样式
        self._refresh_child_styles()

    def _refresh_child_styles(self):
        """刷新各个子组件独立样式（不继承自父级的）"""
        Colors.refresh()
        # 条目列表
        if hasattr(self, 'entries_list'):
            self.entries_list.setStyleSheet(f"""
                QListWidget {{
                    background-color: {Colors.CARD_BG.format(alpha=180)};
                    border: 1px solid {Colors.BORDER};
                    color: {Colors.TEXT_PRIMARY};
                    border-radius: 6px;
                    {get_font_family_css()}
                }}
                QListWidget::item {{
                    padding: 0;
                    border-bottom: 1px solid {Colors.BORDER};
                }}
            """)
        # 条目输入框
        if hasattr(self, 'entry_input'):
            self.entry_input.setStyleSheet(f"""
                QLineEdit {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                        stop:0 {Colors.INPUT_BG_START}, stop:1 {Colors.INPUT_BG_END});
                    border: 1px solid {Colors.INPUT_BORDER};
                    color: {Colors.INPUT_TEXT};
                    padding: 4px 8px;
                    border-radius: 4px;
                    {get_font_family_css()} {font_size_css(12)}
                }}
            """)
        if hasattr(self, 'entry_add_btn'):
            self.entry_add_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {Colors.INFO};
                    color: white;
                    border: none;
                    border-radius: 4px;
                    {font_size_css(12)}
                }}
                QPushButton:hover {{
                    background-color: {Colors.BORDER_ACCENT};
                }}
            """)
        # 项目笔记标签
        if hasattr(self, 'project_name_label'):
            self.project_name_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
            )
        if hasattr(self, 'notes_stats_label'):
            self.notes_stats_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)} padding: 0 4px;"
            )
        # 文档容器（虚线边框）
        docs_container = self.findChild(QWidget, "docsContainer")
        if docs_container:
            docs_container.setStyleSheet(f"""
                QWidget#docsContainer {{
                    background-color: {Colors.CARD_BG.format(alpha=180)};
                    border: 2px dashed {Colors.BORDER};
                    border-radius: 6px;
                }}
            """)
        # 文档列表（无边框，透明背景）
        if hasattr(self, 'docs_list'):
            self.docs_list.setStyleSheet(f"""
                QListWidget {{
                    background: transparent;
                    border: none;
                    color: {Colors.TEXT_PRIMARY};
                    {get_font_family_css()}
                }}
                QListWidget::item {{
                    padding: 0;
                    border-bottom: 1px solid {Colors.BORDER};
                }}
            """)
        # 文档工具栏标题
        if hasattr(self, '_docs_header_label'):
            self._docs_header_label.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; background: transparent; {font_size_css(12)}"
            )
        if hasattr(self, '_docs_count_label'):
            self._docs_count_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: transparent; {font_size_css(11)}"
            )

    def _load_entries(self):
        """加载条目记忆（使用 self._search_filter 过滤）"""
        self.entries_list.clear()
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        entries = memory_mgr.get_entry_memories(self._search_filter, limit=9999)
        for entry in entries:
            memory_id = entry.get("id", "")
            content = entry.get("content", "")
            enabled = entry.get("enabled", True)
            source = entry.get("source", "manual")

            item = QListWidgetItem()
            widget = EntryMemoryItemWidget(memory_id, content, enabled, source)
            widget.deleted.connect(self._delete_entry)
            widget.toggled.connect(self._toggle_entry)
            widget.edited.connect(self._edit_entry)
            self.entries_list.addItem(item)
            self.entries_list.setItemWidget(item, widget)
            item.setSizeHint(widget.sizeHint())

    def _get_entry_item_size(self, content: str):
        """仅作为 fallback 使用"""
        from PyQt5.QtCore import QSize
        return QSize(0, 44)

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
        # 清空后保持焦点在输入框，防止焦点转移导致卡片意外关闭
        self.entry_input.setFocus()
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
        """加载项目笔记（从当前 workdir 的 AGENTS.md）"""
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        workdir = self._get_effective_workdir(self._current_project)
        self.project_name_label.setText(f" {self._current_project}")
        # 同步更新方形 avatar
        if hasattr(self, '_notes_project_avatar'):
            self._notes_project_avatar.set_project(
                self._current_project,
                get_project_color(self._current_project)
            )
        note = memory_mgr.get_or_create_project_note(
            self._current_project, workdir=workdir
        )
        content = note.get("content", "") if note else ""
        # 临时阻止 textChanged 信号（避免去抖保存触发）
        self.notes_editor.blockSignals(True)
        self.notes_editor.setPlainText(content)
        self.notes_editor.blockSignals(False)
        self._update_notes_stats()

    def _save_project_note(self):
        """保存项目笔记（写入当前 workdir 的 AGENTS.md）"""
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            workdir = self._get_effective_workdir(self._current_project)
            content = self.notes_editor.toPlainText()
            success = memory_mgr.save_project_note(
                self._current_project, content, workdir=workdir
            )
            if success:
                self.projectNoteChanged.emit(self._current_project, content)

    # ==================== 关键文档操作 ====================

    def _load_key_documents(self):
        """加载关键文档（过滤掉 git_worktree 条目，但保留根目录视觉效果）"""
        self.docs_list.clear()
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        all_docs = memory_mgr.get_key_documents(self._current_project)

        # 获取实际工作目录（多窗口隔离：实例缓存优先）
        actual_wd = self._get_effective_workdir(self._current_project)

        # 预检：当前工作目录是否指向 worktree（需要在循环前确定，用于后续 git 检测判断）
        is_worktree_active = False
        if actual_wd:
            is_worktree_active = any(
                d.get("file_path") == actual_wd and d.get("added_by") == "git_worktree"
                for d in all_docs
            )

        # 查找原始 git 仓库路径（用于 worktree 模式下的显示和恢复）
        # 注意：worktree 本身不能作为 original_repo_path，必须是实际的 git 仓库文件夹
        # 优先级 1：DB 中 is_working_dir=1 的非 worktree 目录（用户手动设定的根目录）
        # 优先级 2：is_worktree_active 时，第一个非 worktree 的 git 仓库目录（兼容无根目录场景）
        original_repo_path = None
        if actual_wd:
            # 第一优先级：DB 中标记为 is_working_dir 的非 worktree 目录(即用户手动设定的根目录)
            for d in all_docs:
                if d.get("added_by") == "git_worktree":
                    continue
                if d.get("is_working_dir", False):
                    try:
                        if GitWorktreeDetector.detect_git(d.get("file_path", "")):
                            original_repo_path = d["file_path"]
                            break
                    except Exception:
                        pass
            # 第二优先级：worktree 激活时，取第一个非 worktree 的 git 仓库目录
            if original_repo_path is None and is_worktree_active:
                for d in all_docs:
                    if d.get("added_by") == "git_worktree":
                        continue
                    try:
                        if GitWorktreeDetector.detect_git(d.get("file_path", "")):
                            original_repo_path = d["file_path"]
                            break
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
                    item.setSizeHint(QSize(0, h)),
                    self.docs_list.update(),
                ))
                wt_widget.worktreeSwitched.connect(self._on_worktree_changed)
                wt_widget.worktreeDeleted.connect(self._on_worktree_deleted)
                self.docs_list.addItem(wt_item)
                self.docs_list.setItemWidget(wt_item, wt_widget)

        # 更新文件计数
        has_visible_items = self.docs_list.count() > 0
        self._docs_empty_hint.setVisible(not has_visible_items)
        if hasattr(self, '_docs_count_label'):
            count = len(docs)
            self._docs_count_label.setText(f"({count})" if count > 0 else "")

    def _get_worktree_section_size(self, repo_info):
        """计算 worktree 树状组件的高度"""
        from PyQt5.QtCore import QSize
        # 宽度随列表自适应，不设定固定宽度避免溢出
        wt_count = len(repo_info.worktrees) if repo_info.worktrees else 1
        height = wt_count * 24 + 24 + 4
        return QSize(0, height)

    def _get_doc_item_size(self):
        from PyQt5.QtCore import QSize
        # 宽度随列表自适应，不设定固定宽度避免溢出
        return QSize(0, 44)

    def _on_files_dropped(self, file_paths: list):
        """处理文件拖拽/选择"""
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        for path in file_paths:
            memory_mgr.add_key_document(self._current_project, path, "manual")

        self._load_key_documents()

    def _remove_key_document(self, doc_id: str):
        """移除关键文档（直接移除列表项，避免全量重建导致的卡顿）"""
        memory_mgr = self._get_memory_manager()
        if memory_mgr:
            memory_mgr.remove_key_document(doc_id)

        # 直接在列表中查找并移除对应项，不走 _load_key_documents() 全量重建
        for i in range(self.docs_list.count()):
            item = self.docs_list.item(i)
            widget = self.docs_list.itemWidget(item)
            if hasattr(widget, 'doc_id') and widget.doc_id == doc_id:
                taken = self.docs_list.takeItem(i)
                if taken:
                    widget.deleteLater()  # 主动释放 widget
                    del taken             # 释放 item
                break

        # 更新计数（只计 KeyDocumentItemWidget，排除 worktree 区域）
        doc_count = 0
        for i in range(self.docs_list.count()):
            w = self.docs_list.itemWidget(self.docs_list.item(i))
            if isinstance(w, KeyDocumentItemWidget):
                doc_count += 1
        self._docs_count_label.setText(f"({doc_count})" if doc_count > 0 else "")
        self._docs_empty_hint.setVisible(doc_count == 0)

    def _set_as_working_directory(self, file_path: str):
        """设置为工作目录（再次点击取消）

        多窗口隔离：DB 写入仅作为新窗口的默认恢复值；
        当前窗口通过 _instance_workdir 实例缓存保持自身选择独立。
        """
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return
        # 检查当前是否已经是工作目录（用实例缓存判断，不受其他窗口 DB 写入影响）
        current_wd = self._get_effective_workdir(self._current_project)
        if current_wd == file_path:
            # 取消设置：更新实例缓存 + 写入 DB（新窗口默认值）
            self._instance_workdir[self._current_project] = ""
            memory_mgr.set_working_directory(self._current_project, "clear")
            self.workingDirChanged.emit("")
        else:
            # 设置工作目录：更新实例缓存 + 写入 DB（新窗口默认值）
            self._instance_workdir[self._current_project] = file_path
            memory_mgr.set_working_directory(self._current_project, file_path)
            self.workingDirChanged.emit(file_path)
        self._load_key_documents()

    def _on_worktree_changed(self, original_folder: str, worktree_path: str):
        """Worktree 切换：写入 DB（新窗口默认值）+ 切换 workdir（UI 层过滤不显示 git_worktree 条目）

        多窗口隔离：DB 写入仅作为新窗口的默认恢复值；
        当前窗口通过 workingDirChanged 信号通知 main_widget 更新实例缓存。

        重要：在设置 worktree 为工作目录时，必须保留原有根目录的 is_working_dir 标记，
        否则 _load_key_documents 会因遍历顺序将根目录标记错误地分配给其他目录。
        """
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        # 先记住 DB 中当前的工作目录（即用户手动设定的根目录）
        db_wd = memory_mgr.get_working_directory(self._current_project)

        # 必须写入 DB，否则 set_working_directory 找不到路径
        # added_by="git_worktree" 标记，UI 显示时过滤掉
        memory_mgr.add_key_document(self._current_project, worktree_path, "git_worktree")

        # 设为工作目录（DB 写入：新窗口默认值 + 实例缓存更新）
        memory_mgr.set_working_directory(self._current_project, worktree_path)

        # 恢复原有根目录的 is_working_dir 标记（set_working_directory 会先清除所有标记）
        # 这样 _load_key_documents 才能正确识别哪个是用户真正设定的根目录
        if db_wd and db_wd != worktree_path and db_wd != "clear":
            memory_mgr.restore_working_directory_mark(
                self._current_project, db_wd
            )

        self._instance_workdir[self._current_project] = worktree_path
        self.workingDirChanged.emit(worktree_path)
        self._load_key_documents()

    def _on_worktree_deleted(self, worktree_path: str):
        """Worktree 被删除后：移除 DB 记录 + 恢复到主仓库 + 清除实例缓存

        多窗口隔离：通过 workingDirChanged 信号通知 main_widget 清除对应实例缓存。
        """
        memory_mgr = self._get_memory_manager()
        if not memory_mgr:
            return

        current_wd = self._get_effective_workdir(self._current_project)

        # 从关键文档中移除 worktree 路径（防止下次加载又显示）
        if memory_mgr._key_documents_repo:
            memory_mgr._key_documents_repo.remove_by_path(self._current_project, worktree_path)

        if current_wd == worktree_path:
            # 恢复到原始 git 仓库文件夹
            repo_root = GitWorktreeDetector.detect_git(self._original_folder_for_worktree)
            if repo_root:
                memory_mgr.set_working_directory(self._current_project, repo_root)
                self._instance_workdir[self._current_project] = repo_root
                self.workingDirChanged.emit(repo_root)
            else:
                memory_mgr.set_working_directory(self._current_project, "clear")
                self._instance_workdir.pop(self._current_project, None)
                self.workingDirChanged.emit("")

        self._load_key_documents()

    def _open_folder(self, path: str):
        """打开文件/文件夹/URL"""
        import subprocess
        import os
        import webbrowser
        try:
            # URL 链接：在浏览器中打开
            if path and (path.startswith('http://') or path.startswith('https://')):
                webbrowser.open(path)
                return
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