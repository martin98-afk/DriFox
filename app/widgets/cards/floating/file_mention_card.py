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
from typing import Dict, List, Set

from PyQt5.QtCore import QFileSystemWatcher, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css
from app.widgets.elided_label import _ElidedLabel

ITEM_HEIGHT = 36
MAX_VISIBLE_ITEMS = 8
# 最大渲染 widget 数。超过此数量的匹配项仅保留在 _filtered_items 中供搜索，
# 不创建 widget（不可见不可选中），但打分排序仍然参与——用户输更具体的 query
# 就可能把目标文件"拉"进可见区。
# 注：删除/修改此值前请同步更新 _render_incremental 的切片逻辑。
MAX_RENDERED_ITEMS = 300


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
    def _get_file_emoji(filepath: str, item_type: str = "") -> str:
        """根据文件路径返回对应的 emoji 图标

        优先使用 item_type 判断目录（来自缓存的 type 字段，避免 os.path.isdir I/O）
        """
        if item_type == "dir":
            return "📁"
        ext = os.path.splitext(filepath)[1].lower()
        return FileMentionItemWidget._EMOJI_MAP.get(ext, "📄")

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 12, 0)
        layout.setSpacing(6)

        emoji = self._get_file_emoji(self._data["path"], self._data.get("type", ""))
        self._icon_label = QLabel(emoji)
        self._icon_label.setFixedWidth(20)
        self._icon_label.setAlignment(Qt.AlignCenter)
        self._icon_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 图标静态样式——创建时设置一次，永不变化
        self._icon_label.setStyleSheet(f"""
            QLabel {{
                background: transparent;
                {get_font_family_css()} {font_size_css(14)};
            }}
        """)
        layout.addWidget(self._icon_label)

        # 相对路径标签（ElidedLabel，自动省略防止超出可见区域）
        rel = self._data.get("relative_path", "")
        self._path_label = _ElidedLabel(rel)
        self._path_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._path_label.setMinimumWidth(0)
        self._path_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(13)};
                background: transparent;
            }}
        """)
        layout.addWidget(self._path_label, 1)

        self._apply_bg()
        self._update_display()

    def _apply_bg(self):
        """只更新背景色——比完整的 _apply_style（4×setStyleSheet）轻量得多

        setStyleSheet 只设置自身背景，不触碰子 QLabel 样式。
        调用 unpolish/polish 强制 Qt 立即重新计算样式，防止新/复用 widget
        的 setStyleSheet 延迟到下一帧才生效导致选中高亮不可见。
        """
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
        self.style().unpolish(self)
        self.style().polish(self)

    @staticmethod
    def _all_highlight_queries(text: str, query: str) -> List[str]:
        """从多关键字 query 中提取所有能匹配到 text 的关键字

        返回列表可用于 _ElidedLabel.setHighlights()。
        """
        if not query or not text:
            return []
        text_lower = text.lower()
        found = []
        # 先找完整 query
        if query.lower() in text_lower:
            found.append(query)
            return found
        # 按 | & 拆解
        for or_term in query.split('|'):
            or_term = or_term.strip()
            if not or_term:
                continue
            for and_part in or_term.split('&'):
                and_part = and_part.strip()
                if and_part and and_part.lower() in text_lower and and_part not in found:
                    found.append(and_part)
        return found

    def _update_display(self):
        """更新路径显示（_ElidedLabel 自动处理省略，含多关键字高亮）"""
        rel = self._data.get("relative_path", "")
        self._path_label.setText(rel)
        if self._query:
            hls = self._all_highlight_queries(rel, self._query)
            if hls:
                self._path_label.setHighlights(hls, Colors.SEND_BTN_START)

    def update_data(self, file_data: Dict[str, str], query: str):
        """更新 widget 数据并刷新显示（用于回收复用）

        避免创建新 QWidget 的开销。
        """
        self._data = file_data
        self._query = query
        # 更新 emoji（类型可能变化）
        emoji = self._get_file_emoji(file_data["path"], file_data.get("type", ""))
        self._icon_label.setText(emoji)
        # 更新路径显示
        self._update_display()

    def reuse(self, file_data: Dict[str, str], query: str):
        """复用 widget，重置状态并更新数据

        与 update_data 的区别：额外重置 _hovered 和 _selected，
        防止前一次鼠标悬停/选中的状态残留到新生命周期。
        """
        self._hovered = False
        self._selected = False
        self.update_data(file_data, query)
        self._apply_bg()

    def set_selected(self, selected: bool):
        self._selected = selected
        if selected:
            self._hovered = False
        self._apply_bg()

    def enterEvent(self, event):
        self._hovered = True
        if not self._selected:
            self._apply_bg()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        if not self._selected:
            self._apply_bg()
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
        self._gitignore_patterns: List[str] = []  # .gitignore 模式缓存
        self._async_pending = False  # 异步扫描进行中标志，防重复调度
        self._last_query = ""  # 上次过滤的 query，用于增量剪枝

        # ---- 文件系统监视器：本地新增/删除文件时自动标记缓存失效 ----
        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(self._on_directory_changed)
        self._watched_dirs: Set[str] = set()
        self._scanning = False  # 扫描进行中标志，防止自身扫描触发目录变化信号

        # ---- 目录快照：记录每个目录下的文件名集合，用于增量 diff ----
        self._dir_snapshots: Dict[str, Set[str]] = {}

        # ---- 增量更新防抖计时器：合并连续文件系统事件 ----
        self._incr_timer = QTimer(self)
        self._incr_timer.setSingleShot(True)
        self._incr_timer.setInterval(200)  # 200ms 覆盖 IDE 批量创建/删除
        self._incr_timer.timeout.connect(self._do_incremental_update)
        self._changed_dirs: Set[str] = set()

        # ---- 防抖计时器：合并连续快速敲键的筛选调用 ----
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(0)  # 0ms → 下一轮事件循环执行
        self._filter_timer.timeout.connect(self._do_filter_and_render)
        self._pending_filter_query = ""  # 待处理的筛选 query

        self.setVisible(False)
        self._setup_ui()

    def sizeHint(self):
        """返回卡片期望高度。

        Qt 的 QWidgetItem::sizeHint() 不尊重 setFixedHeight/setMinimumHeight 约束，
        它只读 raw sizeHint()。这里显式返回 minimumHeight() 确保容器 _do_expand
        能读到正确的卡片高度，而不是空 QScrollArea 返回的 QSize(0, 0)。
        """
        mh = self.minimumHeight()
        if mh > 0:
            w = super().sizeHint().width()
            if w <= 0:
                w = self.width()
            return QSize(w, mh)
        return super().sizeHint()

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
            self._update_fs_watcher()

    def _update_fs_watcher(self):
        """重置文件系统监视器：移除旧路径，监视新根目录

        切换项目/根目录时，同时清理旧快照和监视器，避免累积泄漏。
        """
        if self._watched_dirs:
            self._fs_watcher.removePaths(list(self._watched_dirs))
            self._watched_dirs.clear()
        self._dir_snapshots.clear()
        if self._root_dir and os.path.isdir(self._root_dir):
            self._watched_dirs.add(self._root_dir)
            self._fs_watcher.addPath(self._root_dir)

    def _on_directory_changed(self, path: str):
        """目录内容变化 → 收集变化目录，启动防抖定时器，到期后增量更新缓存

        排除自身扫描期间触发的信号（某些平台 os.scandir 读目录时可能触发事件）。
        """
        if self._scanning:
            return
        self._changed_dirs.add(path)
        self._incr_timer.start()

    def _do_incremental_update(self):
        """防抖定时器超时后执行增量更新：处理所有累积的变化目录"""
        dirs = self._changed_dirs.copy()
        self._changed_dirs.clear()

        if not self._root_dir or not os.path.isdir(self._root_dir):
            return

        any_change = False
        for d in dirs:
            if self._incremental_update_one(d):
                any_change = True

        if any_change:
            self._sort_file_cache()
            # 如果卡片当前可见，即时刷新显示
            if self._visible:
                self.load_items(self._current_query)

    def _get_rel_prefix(self, abs_path: str) -> str:
        """根据根目录计算绝对路径对应的相对路径前缀"""
        if not self._root_dir:
            return ""
        try:
            rel = os.path.relpath(abs_path, self._root_dir)
            if rel == ".":
                return ""
            return rel.replace("\\", "/")
        except ValueError:
            return ""

    def _sort_file_cache(self):
        """重新排序文件缓存（目录优先，同名不区分大小写）"""
        self._file_cache.sort(
            key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower())
        )

    def _incremental_update_one(self, changed_dir: str) -> bool:
        """增量处理单个变化目录：diff 快照后增删缓存条目

        返回 True 表示有实际变化（新增或删除），False 表示无变化。
        """
        old_names = self._dir_snapshots.get(changed_dir, set())
        rel_prefix = self._get_rel_prefix(changed_dir)
        new_names: Set[str] = set()
        changed = False

        try:
            with os.scandir(changed_dir) as it:
                for entry in it:
                    name = entry.name
                    rel = f"{rel_prefix}/{name}" if rel_prefix else name
                    is_dir = entry.is_dir(follow_symlinks=False)

                    if self._is_ignored(rel, is_dir, self._gitignore_patterns):
                        continue

                    new_names.add(name)

                    if name not in old_names:
                        # ---- 新增条目 ----
                        item = {
                            "name": name,
                            "path": entry.path,
                            "relative_path": rel,
                            "type": "dir" if is_dir else "file",
                        }
                        self._file_cache.append(item)
                        changed = True

                        if is_dir:
                            # 新目录：递归扫描子文件 + 注册监视
                            self._incremental_scan_new_dir(entry.path, rel)
        except (PermissionError, FileNotFoundError, OSError):
            # 目录可能已被删除
            new_names = set()

        # ---- 删除条目 ----
        removed = old_names - new_names
        for name in removed:
            rel = f"{rel_prefix}/{name}" if rel_prefix else name
            # 移除缓存中以此路径为前缀的所有条目
            prefix = rel if rel.endswith("/") else rel + "/"
            before = len(self._file_cache)
            self._file_cache = [
                item for item in self._file_cache
                if item["relative_path"] != rel
                and not item["relative_path"].startswith(prefix)
            ]
            if len(self._file_cache) < before:
                changed = True

            # 清理快照和监视器
            self._cleanup_deleted_path(rel)

        # 更新快照
        self._dir_snapshots[changed_dir] = new_names
        return changed

    def _incremental_scan_new_dir(self, dirpath: str, rel_prefix: str):
        """递归扫描新创建的目录：填满缓存、快照、监视器

        与 _scan_dir 一致地用 max_items=500 防止大目录导致缓存膨胀失控。
        """
        # 注册监视器
        if dirpath not in self._watched_dirs:
            self._watched_dirs.add(dirpath)
            self._fs_watcher.addPath(dirpath)

        # 记录快照
        max_items = 2000
        names: Set[str] = set()
        try:
            with os.scandir(dirpath) as it:
                for entry in it:
                    if len(self._file_cache) >= max_items:
                        break

                    name = entry.name
                    rel = f"{rel_prefix}/{name}" if rel_prefix else name
                    is_dir = entry.is_dir(follow_symlinks=False)

                    if self._is_ignored(rel, is_dir, self._gitignore_patterns):
                        continue

                    names.add(name)
                    item = {
                        "name": name,
                        "path": entry.path,
                        "relative_path": rel,
                        "type": "dir" if is_dir else "file",
                    }
                    self._file_cache.append(item)

                    if is_dir:
                        self._incremental_scan_new_dir(entry.path, rel)
        except (PermissionError, FileNotFoundError, OSError):
            pass

        self._dir_snapshots[dirpath] = names

    def _cleanup_deleted_path(self, rel_path: str):
        """清理已删除路径相关的快照和监视器"""
        prefix = rel_path if rel_path.endswith("/") else rel_path + "/"
        # 清理快照
        keys_to_remove = []
        for watched_path in list(self._dir_snapshots):
            # 用 _get_rel_prefix 判断是否在已删除路径下
            item_rel = self._get_rel_prefix(watched_path)
            if item_rel == rel_path or item_rel.startswith(prefix):
                keys_to_remove.append(watched_path)
        for k in keys_to_remove:
            del self._dir_snapshots[k]

        # 清理监视器
        watcher_paths_to_remove = []
        for wp in list(self._watched_dirs):
            wp_rel = self._get_rel_prefix(wp)
            if wp_rel == rel_path or wp_rel.startswith(prefix):
                watcher_paths_to_remove.append(wp)
        if watcher_paths_to_remove:
            self._fs_watcher.removePaths(watcher_paths_to_remove)
            for wp in watcher_paths_to_remove:
                self._watched_dirs.discard(wp)

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

    # 总是忽略的目录名（不区分大小写精确匹配）
    _IGNORED_DIRS: Set[str] = {
        '.git', '.idea', '.vscode', '.venv', 'venv', 'env',
        '__pycache__', 'build', 'dist', 'node_modules',
        '.svn', '.hg', '.bzr',
        '.mypy_cache', '.pytest_cache', '.ruff_cache',
        '.drifox',  # 项目数据目录
        # 构建输出目录
        'target',  # Rust cargo build
        'bin', 'obj',  # C#/.NET 构建
        # 前端框架构建缓存
        '.next', '.nuxt',
        # 基础设施构建缓存
        '.serverless', '.terraform',
        # 测试覆盖率报告
        'coverage', 'htmlcov', '.coverage',
        # 日志/临时文件
        'logs', 'log', 'tmp', 'temp',
        # 第三方依赖目录
        'vendor',  # Go/PHP
        'Pods',    # CocoaPods
        'Carthage',  # Carthage
        '.gradle',  # Gradle 缓存
        # Python 测试
        '.tox',
        # Python 包目录
        'site-packages', 'lib', 'lib64',
    }

    # 模糊匹配忽略的目录模式（fnmatch 通配符，不区分大小写）
    _IGNORED_DIR_PATTERNS: Set[str] = {
        '*venv*',        # 匹配 venv, .venv, myvenv, venv38, .venv38 等虚拟环境目录
        '*.egg-info*',   # Python egg-info 元数据目录
    }

    # 总是忽略的扩展名
    _IGNORED_EXT: Set[str] = {
        '.pyc', '.pyo', '.so', '.dll', '.dylib',
        '.egg-info', '.whl',
    }

    @staticmethod
    def _fuzzy_match(text: str, query: str) -> bool:
        """fuzzy 匹配：query 的字符按顺序出现在 text 中即可

        例如 "rqrmnts" 可匹配 "requirements" → r→q→r→m→n→t→s 均在 "requirements" 中按序出现。
        适用于拼写不精确但字符顺序正确的场景。
        """
        q_idx = 0
        q_len = len(query)
        for ch in text:
            if ch == query[q_idx]:
                q_idx += 1
                if q_idx == q_len:
                    return True
        return False

    @staticmethod
    def _score_item(item: Dict[str, str], query: str) -> int:
        """根据匹配质量对单个 item 评分，0 表示不匹配

        得分梯度（确保唯一语义层级）：
          100 = 完全匹配
          80  = 文件名前缀匹配
          60  = 文件名子串匹配
          55  = 路径子串匹配（优先级低于文件名子串）
          40  = 文件名 fuzzy 匹配
          35  = 路径 fuzzy 匹配
          0   = 不匹配
        """
        name_lower = item["name"].lower()
        rel_lower = item["relative_path"].lower()

        # 完全一致
        if query == name_lower:
            return 100
        # 前缀匹配
        if name_lower.startswith(query):
            return 80
        # 子串匹配（文件名优先于完整路径）
        if query in name_lower:
            return 60
        if query in rel_lower:
            return 55
        # fuzzy 匹配
        if FileMentionCard._fuzzy_match(name_lower, query):
            return 40
        if FileMentionCard._fuzzy_match(rel_lower, query):
            return 35
        return 0

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
        1. 检查是否命中 always-ignored 目录名（精确匹配）
        2. 检查是否命中 always-ignored 目录模式（fnmatch 通配符匹配）
        3. 检查是否命中 always-ignored 扩展名
        4. 检查 .gitignore 模式
        """
        path_parts = rel_path.replace('\\', '/').split('/')
        name = path_parts[-1]
        name_lower = name.lower()

        # 1. 始终忽略的目录名（精确匹配）
        for part in path_parts:
            if part.lower() in FileMentionCard._IGNORED_DIRS:
                return True

        # 1b. 始终忽略的目录模式（fnmatch 通配符匹配）
        for part in path_parts:
            part_lower = part.lower()
            for pattern in FileMentionCard._IGNORED_DIR_PATTERNS:
                if fnmatch.fnmatch(part_lower, pattern):
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
        """扫描根目录下的文件（全递归，最多 2000 项，常量定义在调用处）

        使用 os.scandir — Python 最快目录遍历 API（C 实现，
        不创建 Path 对象）。忽略 .gitignore 和内置忽略规则。
        只在缓存脏时调用一次，后续 @ 即时过滤。
        """
        self._file_cache = []
        if not self._root_dir or not os.path.isdir(self._root_dir):
            return

        self._scanning = True
        try:
            gitignore_patterns = self._parse_gitignore(Path(self._root_dir))
            self._gitignore_patterns = gitignore_patterns
            max_items = 2000

            try:
                # 递归扫描——无深度限制，忽略目录不进入
                self._scan_dir(self._root_dir, "", gitignore_patterns, max_items)
            except Exception:
                pass

            # 一次性排序（目录优先，同名不区分大小写）
            self._sort_file_cache()
            self._cache_dirty = False
        finally:
            self._scanning = False

    def _scan_dir(self, dirpath: str, rel_prefix: str,
                  gitignore_patterns: List[str], max_items: int):
        """递归扫描单层目录（os.scandir 实现）

        同时将扫描到的目录注册到文件系统监视器，以便实时检测新增/删除文件。
        """
        # 将当前目录加入文件系统监视器（去重）
        if dirpath not in self._watched_dirs:
            self._watched_dirs.add(dirpath)
            self._fs_watcher.addPath(dirpath)

        names: Set[str] = set()
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

                    names.add(name)
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

            self._dir_snapshots[dirpath] = names
        except PermissionError:
            pass

    def _render_incremental(self):
        """增量渲染——复用现有 widget，避免反复创建/销毁

        只渲染 _filtered_items 的前 MAX_RENDERED_ITEMS 项（通常 300）。
        超过此数的匹配项不会创建 widget，但保留在 _filtered_items 中——
        用户继续输入更精确的 query 即可将其"拉"进可见列表。

        快速路径：新旧 items 完全相同则跳过全量重建，仅刷新 query 高亮。
        """
        # 仅取前 MAX_RENDERED_ITEMS 项用于 widget 创建，避免大规模 widget 管理开销
        new_items = self._filtered_items[:MAX_RENDERED_ITEMS]

        # ---- 快速路径：新旧 items 完全一致，仅更新高亮 ----
        if len(self._item_widgets) == len(new_items):
            same = all(
                self._item_widgets[i].item_data["path"] == new_items[i]["path"]
                for i in range(len(new_items))
            )
            if same:
                for w, item in zip(self._item_widgets, new_items):
                    w.update_data(item, self._current_query)
                return

        old_widgets = list(self._item_widgets)  # 复制

        # 构建旧 widget 的 path→widget 映射
        old_by_key = {}
        for w in old_widgets:
            try:
                _ = w.isVisible()
                old_by_key[w.item_data["path"]] = w
            except RuntimeError:
                continue

        # 按新顺序：复用匹配的旧 widget，多余的新建，多余的删除
        new_widgets: List[FileMentionItemWidget] = []
        leftover = dict(old_by_key)  # 消耗副本
        seen_paths = set()

        for item in new_items:
            path = item["path"]
            if path in leftover and path not in seen_paths:
                w = leftover.pop(path)
                seen_paths.add(path)
                w.reuse(item, self._current_query)
                new_widgets.append(w)
            else:
                w = FileMentionItemWidget(item, self._current_query, self._scroll_content)
                w.clicked.connect(self._on_item_clicked)
                new_widgets.append(w)

        self._item_widgets = new_widgets

        # 删除不再需要的旧 widget
        for w in leftover.values():
            try:
                self._scroll_layout.removeWidget(w)
                w.deleteLater()
            except RuntimeError:
                pass

        # 清空布局（确保顺序正确）
        for i, w in enumerate(self._item_widgets):
            idx = self._scroll_layout.indexOf(w)
            if idx != i:
                self._scroll_layout.insertWidget(i, w)

        # 计算卡片高度
        item_count = len(self._item_widgets)
        if item_count == 0:
            self.setFixedHeight(0)
        else:
            visible = min(item_count, MAX_VISIBLE_ITEMS)
            self.setFixedHeight(visible * ITEM_HEIGHT)
        # 渲染重建后强制滚动回顶部，防止 Qt 在布局变化期间自动调整
        # 滚动位置导致 _update_selection 的 ensureWidgetVisible 滚动到中间
        self._scroll_area.verticalScrollBar().setValue(0)

    @staticmethod
    def _matches_multi(item: Dict[str, str], query: str) -> bool:
        """多关键字匹配：| = OR, & = AND

        例如 query="doc|config&json" 表示匹配文件名/路径包含 "doc"
        或同时包含 "config" 与 "json" 的项。
        尾部 &（如 "config&"）自动忽略空 AND 部分，不会导致全不匹配。
        """
        if not query:
            return True
        text = (item["name"] + " " + item["relative_path"]).lower()

        for or_term in query.split('|'):
            or_term = or_term.strip()
            if not or_term:
                continue
            # 过滤空 AND 部分：让 "config&" 等价于 "config"
            and_parts = [p.strip() for p in or_term.split('&') if p.strip()]
            if not and_parts:
                continue
            if all(part in text for part in and_parts):
                return True
        return False

    def load_items(self, query: str = ""):
        """根据 query 即时筛选并防抖渲染（多关键字 + fuzzy）

        _filtered_items 始终包含**全量匹配结果**，搜索永不丢失文件。
        视觉上的 widget 创建由 _render_incremental 截断到 MAX_RENDERED_ITEMS 项。

        无 query 时按「目录深度浅→深 + 目录优先 → 同名不区分大小写」排序，
        保证各个文件夹的文件都能出现在列表前端，不会出现一个文件夹霸占全部显示。
        """
        query = query.strip().lower()
        self._current_query = query

        if not query:
            # 按目录深度排序（浅的在前），同深度目录优先，再按名不区分大小写
            self._filtered_items = sorted(
                self._file_cache,
                key=lambda x: (
                    x["relative_path"].count('/'),  # 深度，浅在前
                    0 if x["type"] == "dir" else 1,  # 目录优先
                    x["name"].lower(),  # 不区分大小写
                ),
            )
        else:
            q_lower = query.lower()

            if '|' in query or '&' in query:
                # 多关键字模式：布尔匹配
                self._filtered_items = [
                    item for item in self._file_cache
                    if self._matches_multi(item, query)
                ]
            else:
                # 单关键字模式：全量评分 + 全排序
                scored = [
                    (self._score_item(item, q_lower), item)
                    for item in self._file_cache
                ]
                scored = [(s, item) for s, item in scored if s > 0]
                scored.sort(key=lambda x: -x[0])
                self._filtered_items = [item for _, item in scored]

        # 防抖调度渲染
        self._filter_timer.stop()
        self._filter_timer.start()

    def _do_filter_and_render(self):
        """防抖定时器超时后执行增量渲染"""
        self._render_incremental()
        if self._filtered_items:
            # 强制重置 _last_selected_index 为 -1，确保 _update_selection 总能生效
            # 因为 _render_incremental 可能创建了新 widget，而 _last_selected_index
            # 若仍为 0（上次选中值），会导致 old_idx == new_idx 守卫条件跳过，
            # 新 widget 的 index 0 得不到选中状态。
            self._last_selected_index = -1
            self._selected_index = 0
            self._update_selection()
        # 无匹配项时清除残留 widget
        elif self._item_widgets:
            self._render_incremental()

    def _deferred_select_first(self):
        """延迟一帧选中第一项（新 widget 创建后 style 可能延迟生效）"""
        try:
            if self._item_widgets:
                self._selected_index = 0
                self._update_selection()
        except RuntimeError:
            pass

    def _deferred_container_expand(self):
        """延迟一帧触父容器 _schedule_expand（等 LayoutRequest 传播完）"""
        try:
            parent = self.parentWidget()
            if parent and hasattr(parent, '_schedule_expand'):
                parent._schedule_expand()
        except RuntimeError:
            pass

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
        - 缓存未就绪 → 立即显示卡片（设占位高度），QTimer 延迟扫描，
          扫描完成后自动刷新显示。
        - 绝不阻塞 UI 线程。

        ⚠️ 修复：CardManager 无参调用 (root_dir="") 时，仅使卡片可见，
        不加载/过滤内容，防止脏 _current_query 泄露导致容器展开到错误高度。
        真正的加载由 main_widget 通过 show_card(workdir, query) 执行。

        ⚠️ 修复2：异步扫描路径在 delay 前设至少 ITEM_HEIGHT 占位高度，
        防止容器 _do_expand 读到 natural_h ≈ 0 导致卡片首次展开非常矮。
        """
        Colors.refresh()
        if not root_dir:
            # CardManager 纯可视性调用 → 仅显示，不刷新内容
            # 不做任何 I/O 或过滤，防止陈旧 _current_query 污染容器展开高度
            if not self._root_dir:
                # 尚无根目录 → 仅显示空白卡片
                self._visible = True
                self.setVisible(True)
                self.updateGeometry()
                return
            # 已有根目录 → 仅显示，保留当前内容状态
            self._visible = True
            self.setVisible(True)
            self.updateGeometry()
            return
        if root_dir:
            if root_dir != self._root_dir:
                self._root_dir = root_dir
                self._cache_dirty = True
        self._current_query = query  # root_dir 有值时始终更新 query

        if self._cache_dirty and not self._async_pending:
            # 🚀 立即显示卡片（占位高度），异步扫描
            # 设占位高度确保容器 _do_expand 不会读到 natural_h ≈ 0
            self.setFixedHeight(ITEM_HEIGHT)
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
        # 🎯 立即设置高度，不等防抖定时器
        # 第一次打开时无旧 widget 残留，不立即 setFixedHeight 则高度为 0
        if has_items:
            visible = min(len(self._filtered_items), MAX_VISIBLE_ITEMS)
            self.setFixedHeight(visible * ITEM_HEIGHT)
        else:
            self.setFixedHeight(0)
        self._visible = has_items
        self.setVisible(has_items)
        self.updateGeometry()
        # 安全网：延迟一帧触发容器 _schedule_expand，防止极端时序下
        # setFixedHeight 的 Resize 事件被容器动画钳制而未触发重新展开
        if has_items:
            QTimer.singleShot(0, self._deferred_container_expand)

    def _async_scan_and_refresh(self):
        """异步扫描完成后立即渲染（跳过防抖）

        扫描本身已异步耗时（50-200ms），完成后直接渲染，不再等 20ms 防抖。
        首次打开时还需延迟一帧让 Qt 完成布局后再应用选中状态。

        ⚠️ 关键修复：容器 _do_expand 的动画可能正在约束 maximumHeight（0→ITEM_HEIGHT），
        导致卡片 setFixedHeight(N*ITEM_HEIGHT) 后无法触发 Resize 事件（卡片已处于被约束
        的几何尺寸）。这里触父容器的 _schedule_expand 停掉旧动画，重读 sizeHint 重新展开。
        """
        self._async_pending = False
        self._scan_files()
        self._current_query = self._pending_query
        self.load_items(self._pending_query)
        self._filter_timer.stop()  # 取消防抖
        self._do_filter_and_render()  # 立即渲染

        # ---- 强制容器重算布局 ----
        # _render_incremental 已调用了 setFixedHeight(N*ITEM_HEIGHT)，
        # 但容器动画可能仍在约束 maxHeight 在旧值（ITEM_HEIGHT），导致
        # 卡片无法触发 Resize → 容器不会重新 _do_expand → 高度永久卡住。
        # ⚠️ 必须延迟到下一帧：setFixedHeight 发出的 LayoutRequest 需要
        # 通过事件循环传播到父容器布局后才能被 _do_expand→sizeHint() 读到新值。
        parent = self.parentWidget()
        if parent and hasattr(parent, '_schedule_expand'):
            QTimer.singleShot(0, self._deferred_container_expand)

        has_items = len(self._filtered_items) > 0
        self._visible = has_items
        self.setVisible(has_items)
        self.updateGeometry()
        # 延迟一帧重新应用选中状态：新 widget 刚被创建并加入布局，
        # setStyleSheet 可能延迟到下一轮事件循环的 style 事件才生效，
        # 延迟刷新确保选中高亮可见
        if has_items and self._item_widgets:
            self._last_selected_index = -1
            QTimer.singleShot(0, self._deferred_select_first)

    def dismiss(self):
        """关闭卡片"""
        self._filter_timer.stop()
        self._visible = False
        self.setVisible(False)
        self._current_query = ""  # 重置陈旧 query，防止下次 @ 触发时泄漏
        self.dismissed.emit()

    @property
    def is_card_visible(self) -> bool:
        return self._visible

    @property
    def filtered_count(self) -> int:
        return len(self._filtered_items)
