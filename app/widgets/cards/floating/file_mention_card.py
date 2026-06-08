# -*- coding: utf-8 -*-
"""
文件提及卡片 - 输入 @ 时展开，显示当前根目录文件列表

触发方式：在输入框输入 @ 后，卡片自动展开
数据来源：当前工作目录下的文件列表
交互方式：↑/↓ 导航，Enter 选中，Esc 关闭
选中后：文件以 attachment chip 形式添加到输入框上方
"""
import fnmatch
import os
from pathlib import Path
from typing import List, Dict, Set

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QFrame, QSizePolicy,
)

from app.utils.utils import get_font_family_css
from app.utils.design_tokens import Colors, font_size_css


ITEM_HEIGHT = 36
MAX_VISIBLE_ITEMS = 10


class FileMentionItemWidget(QWidget):
    """文件列表单项"""

    clicked = pyqtSignal()

    # 扩展名 → emoji 映射（复用 memory_card 的风格，比 IconWidget 轻量无数倍）
    _EMOJI_MAP = {
        # 代码
        '.py': '🐍', '.pyw': '🐍', '.pyx': '🐍',
        '.js': '🟨', '.jsx': '⚛️', '.mjs': '🟨', '.cjs': '🟨',
        '.ts': '🔷', '.tsx': '⚛️',
        '.html': '🌐', '.htm': '🌐',
        '.css': '🎨', '.scss': '🎨', '.less': '🎨',
        '.java': '☕',
        '.go': '🐹',
        '.rs': '🦀',
        '.rb': '💎',
        '.php': '🐘',
        '.swift': '🍎',
        '.kt': '🤖',
        '.c': '🔶', '.cpp': '🔶', '.h': '🔶', '.hpp': '🔶',
        '.cs': '🔷',
        '.sql': '🗃️',
        '.sh': '💻', '.bash': '💻', '.zsh': '💻', '.ps1': '💻', '.cmd': '💻',
        # 文档
        '.md': '📝', '.rst': '📝',
        '.txt': '📄', '.log': '📄',
        '.json': '🔧', '.xml': '🔧',
        '.yaml': '🔧', '.yml': '🔧',
        '.toml': '🔧', '.ini': '🔧', '.cfg': '🔧', '.conf': '🔧',
        '.csv': '📊', '.tsv': '📊',
        '.pdf': '📕',
        # 图片
        '.png': '🖼️', '.jpg': '🖼️', '.jpeg': '🖼️',
        '.gif': '🖼️', '.bmp': '🖼️', '.svg': '🖼️',
        '.webp': '🖼️', '.ico': '🖼️',
        # 视频
        '.mp4': '🎬', '.avi': '🎬', '.mov': '🎬', '.mkv': '🎬',
        '.webm': '🎬', '.flv': '🎬', '.m4v': '🎬',
        # 音频
        '.mp3': '🎵', '.wav': '🎵', '.flac': '🎵', '.aac': '🎵',
        '.ogg': '🎵', '.wma': '🎵', '.m4a': '🎵',
        # 压缩包
        '.zip': '📦', '.rar': '📦', '.7z': '📦',
        '.tar': '📦', '.gz': '📦', '.bz2': '📦', '.xz': '📦', '.zst': '📦',
    }

    def __init__(self, file_data: Dict[str, str], query: str, parent=None):
        super().__init__(parent)
        self._data = file_data
        self._query = query
        self._hovered = False
        self._selected = False
        self.setFixedHeight(ITEM_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    @staticmethod
    def _get_file_emoji(filepath: str) -> str:
        """根据文件路径返回对应的 emoji 图标"""
        if os.path.isdir(filepath):
            return "📁"
        ext = os.path.splitext(filepath)[1].lower()
        return FileMentionItemWidget._EMOJI_MAP.get(ext, "📄")

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 12, 0)
        layout.setSpacing(6)

        # 文件类型 emoji（QLabel 一文本，零 QWidget 开销）
        self._icon_label = QLabel(self._get_file_emoji(self._data["path"]))
        self._icon_label.setFixedWidth(20)
        self._icon_label.setAlignment(Qt.AlignCenter)
        self._icon_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._icon_label)

        # 文件名标签
        self._name_label = QLabel()
        self._name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._name_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self._name_label)

        # 相对路径标签（省略）
        rel = self._data.get("relative_path", "")
        self._path_label = QLabel(rel)
        self._path_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._path_label.setMinimumWidth(0)
        self._path_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self._path_label, 1)

        self._apply_style()
        self._update_display()

    def _apply_style(self):
        if self._selected:
            bg = Colors.REALTIME_TAG_BG
        elif self._hovered:
            bg = Colors.HOVER_BG
        else:
            bg = "transparent"

        self.setStyleSheet(f"""
            FileMentionItemWidget {{
                background-color: {bg};
                border: none;
                border-radius: 4px;
            }}
        """)

        # 图标 emoji 标签样式
        self._icon_label.setStyleSheet(f"""
            QLabel {{
                background: transparent;
                {get_font_family_css()} {font_size_css(14)};
            }}
        """)

        # 路径标签样式
        path_fg = Colors.TEXT_SECONDARY
        self._path_label.setStyleSheet(f"""
            QLabel {{
                color: {path_fg};
                {get_font_family_css()} {font_size_css(10)};
                background: transparent;
            }}
        """)

        # 名称样式
        fg = Colors.TEXT_PRIMARY
        self._name_label.setStyleSheet(f"""
            QLabel {{
                color: {fg};
                {get_font_family_css()} {font_size_css(13)};
                background: transparent;
            }}
        """)

    def _update_display(self):
        """更新名称显示（含查询高亮）"""
        name = self._data["name"]
        query = self._query

        if query:
            lower_text = name.lower()
            lower_query = query.lower()
            idx = lower_text.find(lower_query)
            if idx >= 0:
                html = name[:idx]
                html += f'<span style="color: {Colors.SEND_BTN_START}; font-weight: bold;">{name[idx:idx + len(query)]}</span>'
                html += name[idx + len(query):]
                self._name_label.setText(html)
            else:
                self._name_label.setText(name)
        else:
            self._name_label.setText(name)

    def set_selected(self, selected: bool):
        self._selected = selected
        if selected:
            self._hovered = False
        self._apply_style()

    def enterEvent(self, event):
        self._hovered = True
        if not self._selected:
            self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        if not self._selected:
            self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    @property
    def item_data(self) -> Dict[str, str]:
        return self._data


class FileMentionCard(QWidget):
    """文件提及卡片 — 输入 @ 时展开显示当前目录文件"""

    fileSelected = pyqtSignal(str)  # file path
    dismissed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_items: List[Dict[str, str]] = []
        self._filtered_items: List[Dict[str, str]] = []
        self._selected_index = 0
        self._last_selected_index = -1
        self._item_widgets: List[FileMentionItemWidget] = []
        self._visible = False
        self._current_query = ""
        self._root_dir: str = ""
        self._file_cache: List[Dict[str, str]] = []
        self._cache_dirty = True
        self._async_pending = False  # 异步扫描进行中标志，防重复调度

        self.setVisible(False)
        self._setup_ui()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        Colors.refresh()
        self.setStyleSheet(f"""
            FileMentionCard {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 滚动区域
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QScrollArea.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setStyleSheet(f"""
            QScrollArea, QScrollArea * {{
                background: transparent;
                border: none;
                padding: 0;
                margin: 0;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 12px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {Colors.SCROLLBAR_HANDLE_BG};
                border-radius: 6px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {Colors.SCROLLBAR_HANDLE_HOVER_BG};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)

        self._scroll_content = QWidget()
        self._scroll_content.setStyleSheet("background: transparent; border: none;")
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll_layout.setSpacing(0)

        self._scroll_area.setWidget(self._scroll_content)
        self._scroll_area.viewport().setStyleSheet(
            "background: transparent; border: none; padding: 0; margin: 0;"
        )
        layout.addWidget(self._scroll_area)

    def set_root_dir(self, root_dir: str):
        """设置根目录"""
        if root_dir != self._root_dir:
            self._root_dir = root_dir
            self._cache_dirty = True

    def invalidate_cache(self):
        """使缓存失效，下次 show 时重建"""
        self._cache_dirty = True

    def ensure_cache(self, root_dir: str):
        """确保文件缓存已就绪（异步预扫描）

        在下列时机调用：
        - main_widget 初始化完成后
        - 工作目录变更时

        扫描在下一轮事件循环执行，不阻塞 UI。
        用户按 @ 时若扫描未完成，show_card 也会自动触发异步扫描。
        """
        if not root_dir:
            return
        self.set_root_dir(root_dir)
        if self._cache_dirty:
            QTimer.singleShot(0, self._scan_files)

    # 总是忽略的目录名（不区分大小写匹配）
    _IGNORED_DIRS: Set[str] = {
        '.git', '.idea', '.vscode', '.venv', 'venv', 'env',
        '__pycache__', 'build', 'dist', 'node_modules',
        '.svn', '.hg', '.bzr',
        '.mypy_cache', '.pytest_cache', '.ruff_cache',
        '.drifox',  # 项目数据目录
    }

    # 总是忽略的扩展名
    _IGNORED_EXT: Set[str] = {
        '.pyc', '.pyo', '.so', '.dll', '.dylib',
        '.egg-info', '.whl',
    }

    @staticmethod
    def _parse_gitignore(root: Path) -> List[str]:
        """解析 .gitignore 文件，返回模式列表（正反模式均已处理）

        返回的是 fnmatch 可用的模式，相对于根目录匹配。
        只支持基本的 gitignore 语法：
        - # 注释
        - ! 取反
        - / 结尾表示目录
        - 标准 glob (*, ?, [abc])
        """
        gitignore_path = root / '.gitignore'
        if not gitignore_path.exists():
            return []

        patterns = []
        try:
            text = gitignore_path.read_text(encoding='utf-8', errors='replace')
            for line in text.split('\n'):
                line = line.strip()
                # 跳过空行和注释
                if not line or line.startswith('#'):
                    continue
                patterns.append(line)
        except Exception:
            pass
        return patterns

    @staticmethod
    def _is_ignored(rel_path: str, is_dir: bool, gitignore_patterns: List[str]) -> bool:
        """检查相对路径是否被忽略

        规则：
        1. 检查是否命中 always-ignored 目录名
        2. 检查是否命中 always-ignored 扩展名
        3. 检查 .gitignore 模式
        """
        path_parts = rel_path.replace('\\', '/').split('/')
        name = path_parts[-1]
        name_lower = name.lower()

        # 1. 始终忽略的目录/文件
        for part in path_parts:
            if part.lower() in FileMentionCard._IGNORED_DIRS:
                return True

        # 2. 始终忽略的扩展名
        if not is_dir:
            for ext in FileMentionCard._IGNORED_EXT:
                if name_lower.endswith(ext):
                    return True

        # 3. .gitignore 模式
        if not gitignore_patterns:
            return False

        # 尝试匹配所有模式，最后匹配的生效
        ignored = False
        for pattern in gitignore_patterns:
            # 处理取反
            negate = False
            p = pattern
            if pattern.startswith('!'):
                negate = True
                p = pattern[1:]

            # 去掉尾部 /
            p_trail = p.rstrip('/')

            # 构建匹配路径（用 / 分隔）
            match_path = rel_path.replace('\\', '/')

            # 尝试直接匹配
            if fnmatch.fnmatch(match_path, p_trail):
                ignored = not negate
                continue

            # 尝试匹配文件名（模式中没有 / 时匹配任何层级的文件名）
            if '/' not in p:
                if fnmatch.fnmatch(name, p_trail):
                    ignored = not negate
                    continue

            # 尝试前缀匹配（e.g. "build/" 匹配 "build/xxx"）
            if p.endswith('/'):
                if match_path.startswith(p) or match_path == p.rstrip('/'):
                    ignored = not negate
                    continue

        return ignored

    def _scan_files(self):
        """扫描根目录下的文件（全递归，最多 2000 项）

        使用 os.scandir — Python 最快目录遍历 API（C 实现，
        不创建 Path 对象）。忽略 .gitignore 和内置忽略规则。
        只在缓存脏时调用一次，后续 @ 即时过滤。
        """
        self._file_cache = []
        if not self._root_dir or not os.path.isdir(self._root_dir):
            return

        gitignore_patterns = self._parse_gitignore(Path(self._root_dir))
        max_items = 500

        try:
            # 递归扫描——无深度限制，忽略目录不进入
            self._scan_dir(self._root_dir, "", gitignore_patterns, max_items)
        except Exception:
            pass

        # 一次性排序（目录优先，同名不区分大小写）
        self._file_cache.sort(
            key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower())
        )
        self._cache_dirty = False

    def _scan_dir(self, dirpath: str, rel_prefix: str,
                  gitignore_patterns: List[str], max_items: int):
        """递归扫描单层目录（os.scandir 实现）"""
        try:
            with os.scandir(dirpath) as it:
                for entry in it:
                    if len(self._file_cache) >= max_items:
                        return

                    name = entry.name
                    rel = f"{rel_prefix}/{name}" if rel_prefix else name
                    is_dir = entry.is_dir(follow_symlinks=False)

                    # 检查是否被忽略
                    if self._is_ignored(rel, is_dir, gitignore_patterns):
                        continue

                    item = {
                        "name": name,
                        "path": entry.path,
                        "relative_path": rel,
                        "type": "dir" if is_dir else "file",
                    }
                    self._file_cache.append(item)

                    # 递归子目录
                    if is_dir:
                        self._scan_dir(entry.path, rel,
                                       gitignore_patterns, max_items)
        except PermissionError:
            pass

    def load_items(self, query: str = ""):
        """根据 query 即时筛选并渲染列表

        假设缓存已就绪（由 ensure_cache / _scan_files 预先填充），
        此处仅做 O(n) 子串匹配 + O(k) widget 创建，无 I/O。
        """
        query = query.strip().lower()
        self._current_query = query

        if not query:
            self._filtered_items = list(self._file_cache)
        else:
            q_lower = query.lower()
            self._filtered_items = [
                item for item in self._file_cache
                if q_lower in item["name"].lower()
                or q_lower in item["relative_path"].lower()
            ]

        # 缓存已排序，筛选结果保持顺序
        self._render()

        if self._filtered_items:
            self._selected_index = 0
            self._update_selection()

    def _render(self):
        """渲染当前筛选结果
        
        创建全部 _filtered_items 的 widget 以支持滚动，
        _file_cache 自动限制在 MAX_CACHE_ITEMS 以内，总量可控。
        """
        # 清除旧 widget
        for w in self._item_widgets:
            try:
                self._scroll_layout.removeWidget(w)
                w.deleteLater()
            except RuntimeError:
                pass
        self._item_widgets.clear()

        for item in self._filtered_items:
            w = FileMentionItemWidget(item, self._current_query, self._scroll_content)
            w.clicked.connect(self._on_item_clicked)
            self._scroll_layout.addWidget(w)
            self._item_widgets.append(w)

        # 计算卡片高度
        item_count = len(self._item_widgets)
        if item_count == 0:
            self.setFixedHeight(0)
        else:
            visible = min(item_count, MAX_VISIBLE_ITEMS)
            self.setFixedHeight(visible * ITEM_HEIGHT)

    def _on_item_clicked(self):
        """item 被鼠标点击"""
        sender = self.sender()
        if sender in self._item_widgets:
            idx = self._item_widgets.index(sender)
            self._selected_index = idx
            self._update_selection()
            self.select_current()

    def _update_selection(self):
        """更新选中高亮"""
        old_idx = self._last_selected_index
        new_idx = self._selected_index

        if old_idx != new_idx:
            if 0 <= old_idx < len(self._item_widgets):
                self._item_widgets[old_idx].set_selected(False)
            if 0 <= new_idx < len(self._item_widgets):
                self._item_widgets[new_idx].set_selected(True)

        self._last_selected_index = new_idx

        # 滚动到可见
        if 0 <= self._selected_index < len(self._item_widgets):
            self._scroll_area.ensureWidgetVisible(
                self._item_widgets[self._selected_index], 0, 0
            )

    def select_next(self) -> bool:
        """选择下一项"""
        if self._item_widgets and self._selected_index < len(self._item_widgets) - 1:
            self._selected_index += 1
            self._update_selection()
            return True
        return False

    def select_prev(self) -> bool:
        """选择上一项"""
        if self._item_widgets and self._selected_index > 0:
            self._selected_index -= 1
            self._update_selection()
            return True
        return False

    def select_current(self):
        """确认选中当前文件"""
        if 0 <= self._selected_index < len(self._filtered_items):
            item = self._filtered_items[self._selected_index]
            self.fileSelected.emit(item["path"])
            self.dismiss()

    def show_card(self, root_dir: str = "", query: str = ""):
        """加载并显示卡片（始终不阻塞 UI）

        无参调用（被 CardManager 调用）：仅使卡片可见，不做任何 I/O。
        带参调用则加载指定目录和查询。

        关键设计：
        - 缓存就绪 → 即时 O(n) 过滤 + O(k) widget 创建
        - 缓存未就绪 → 立即显示空卡片，QTimer 延迟扫描，
          扫描完成后自动刷新显示。
        - 绝不阻塞 UI 线程。
        """
        Colors.refresh()
        if not root_dir and not self._root_dir:
            # CardManager 纯可视性调用，尚无根目录 → 仅显示空白卡片
            self._visible = True
            self.setVisible(True)
            self.updateGeometry()
            return
        if root_dir:
            if root_dir != self._root_dir:
                self._root_dir = root_dir
                self._cache_dirty = True
        self._current_query = query if root_dir else self._current_query

        if self._cache_dirty and not self._async_pending:
            # 🚀 立即显示卡片（哪怕为空），异步扫描
            self._visible = True
            self.setVisible(True)
            self.updateGeometry()
            self._pending_query = self._current_query
            self._async_pending = True
            QTimer.singleShot(0, self._async_scan_and_refresh)
            return

        # 缓存就绪 → 即时过滤，无 I/O
        self.load_items(self._current_query)
        has_items = len(self._filtered_items) > 0
        self._visible = has_items
        self.setVisible(has_items)
        self.updateGeometry()

    def _async_scan_and_refresh(self):
        """异步扫描完成后刷新显示"""
        self._async_pending = False
        self._scan_files()
        self.load_items(self._pending_query)
        has_items = len(self._filtered_items) > 0
        self._visible = has_items
        self.setVisible(has_items)
        self.updateGeometry()

    def dismiss(self):
        """关闭卡片"""
        self._visible = False
        self.setVisible(False)
        self.dismissed.emit()

    @property
    def is_card_visible(self) -> bool:
        return self._visible

    @property
    def filtered_count(self) -> int:
        return len(self._filtered_items)
