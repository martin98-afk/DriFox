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

from app.widgets.elided_label import _ElidedLabel

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

    def _update_display(self):
        """更新路径显示（_ElidedLabel 自动处理省略，含搜索高亮）"""
        rel = self._data.get("relative_path", "")
        self._path_label.setText(rel)
        if self._query:
            self._path_label.setHighlight(self._query, Colors.SEND_BTN_START)

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
        self._async_pending = False  # 异步扫描进行中标志，防重复调度
        self._last_query = ""  # 上次过滤的 query，用于增量剪枝

        # ---- 防抖计时器：合并连续快速敲键的筛选调用 ----
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(20)  # 20ms，足够覆盖快速打字
        self._filter_timer.timeout.connect(self._do_filter_and_render)
        self._pending_filter_query = ""  # 待处理的筛选 query

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

    def _render_incremental(self):
        """增量渲染——复用现有 widget，避免反复创建/销毁

        核心优化：只变化的部分才动，匹配的 item 更新数据后复用。
        目录有 500 项 + 用户只改一个字符 → 最多只创建/删除几个 widget。
        """
        new_items = self._filtered_items
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
                # 更新数据（可能在路径不变但查询变化时刷新名称高亮）
                w.update_data(item, self._current_query)
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

    def load_items(self, query: str = ""):
        """根据 query 即时筛选并防抖渲染

        假设缓存已就绪（由 ensure_cache / _scan_files 预先填充），
        此处仅做 O(n) 评分过滤 + fuzzy 匹配，无 I/O。
        筛选结果按匹配质量排序（完全匹配 > 前缀 > 子串 > fuzzy），
        实际 widget 渲染由 20ms 防抖定时器触发，快速敲键只执行最后一次。

        增量剪枝：当用户连续追加字符时（'re' → 'req'），
        新结果是旧结果的子集，直接在上次 _filtered_items 上过滤，
        避免全量遍历 500 项 _file_cache。
        """
        query = query.strip().lower()
        self._current_query = query

        if not query:
            self._filtered_items = list(self._file_cache)
        else:
            q_lower = query.lower()
            # 增量剪枝：新 query 是上次 query 的扩展 → 在上次结果上继续过滤
            if self._last_query and query.startswith(self._last_query):
                source = self._filtered_items
            else:
                source = self._file_cache
            # 评分过滤：fuzzy 匹配 + 按匹配质量排序
            scored = [
                (item, self._score_item(item, q_lower))
                for item in source
            ]
            scored = [(item, s) for item, s in scored if s > 0]
            scored.sort(key=lambda x: -x[1])  # 高得分在前
            self._filtered_items = [item for item, _ in scored]

        self._last_query = query
        # 防抖调度渲染
        self._filter_timer.stop()
        self._filter_timer.start()

    def _do_filter_and_render(self):
        """防抖定时器超时后执行增量渲染"""
        self._render_incremental()
        if self._filtered_items:
            self._selected_index = 0
            self._update_selection()
        # 无匹配项时清除残留 widget
        elif self._item_widgets:
            self._render_incremental()

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

    def _async_scan_and_refresh(self):
        """异步扫描完成后立即渲染（跳过防抖）

        扫描本身已异步耗时（50-200ms），完成后直接渲染，不再等 20ms 防抖。
        """
        self._async_pending = False
        self._scan_files()
        self._current_query = self._pending_query
        self.load_items(self._pending_query)
        self._filter_timer.stop()  # 取消防抖
        self._do_filter_and_render()  # 立即渲染
        has_items = len(self._filtered_items) > 0
        self._visible = has_items
        self.setVisible(has_items)
        self.updateGeometry()

    def dismiss(self):
        """关闭卡片"""
        self._filter_timer.stop()
        self._visible = False
        self.setVisible(False)
        self.dismissed.emit()

    @property
    def is_card_visible(self) -> bool:
        return self._visible

    @property
    def filtered_count(self) -> int:
        return len(self._filtered_items)
