# -*- coding: utf-8 -*-
"""FileTreeCard 浮动卡片 — 当前项目文件树浏览

功能：
- 以树形结构展示当前工作项目的文件和目录
- 智能过滤：自动隐藏 .git/__pycache__/node_modules/.venv 等常见忽略目录
- 右键菜单：复制路径、复制文件名、在资源管理器中打开
- 实时文件监听：已展开的目录自动监听变更并更新
- 搜索过滤：顶栏搜索框实时过滤树节点
- 主题色跟随主程序

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作直接通过 stdlib 完成
- 基于 ctx["project_root"] 获取项目路径
"""

import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from PyQt5.QtCore import (
    QEvent,
    QFileInfo,
    QObject,
    QSize,
    QThread,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QFileIconProvider,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    ScrollArea,
    StrongBodyLabel,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)
from loguru import logger


# ══════════════════════════════════════════════════════════
# 常量与过滤规则
# ══════════════════════════════════════════════════════════

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# 始终隐藏的目录名（任何层级）
_HIDDEN_DIRS: Set[str] = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    ".env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    ".playwright-mcp",
}

# 始终隐藏的文件名
_HIDDEN_FILES: Set[str] = {
    ".DS_Store",
    "Thumbs.db",
}

# 文件监听最大路径数（超出则移除最早展开的路径）
_MAX_WATCH_PATHS = 50

# 搜索过滤防抖延迟（毫秒）
_SEARCH_DEBOUNCE_MS = 300

# 文件变更防抖延迟（毫秒）
_WATCH_DEBOUNCE_MS = 500


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════


def _text_color(secondary: bool = False) -> str:
    """fallback 文字颜色（无上下文时使用）"""
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    """从上下文 colors 中获取文字颜色"""
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    if val:
        return val
    return _text_color(secondary)


def _should_show(name: str, is_dir: bool) -> bool:
    """智能过滤：判断文件/目录是否应该显示

    Args:
        name: 文件或目录名
        is_dir: 是否为目录

    Returns:
        True 表示应该显示，False 表示应隐藏
    """
    if is_dir:
        if name in _HIDDEN_DIRS:
            return False
        return True

    # 文件
    if name in _HIDDEN_FILES:
        return False
    # 隐藏以 . 开头的文件（如 .gitkeep 等特殊文件保留）
    if name.startswith("."):
        return False
    return True


def _make_colors_from_context(ctx: dict) -> dict:
    """将 context 主题色映射为可用的颜色字典

    Args:
        ctx: UIPluginRegistry 注入的上下文

    Returns:
        包含 QColor 值的字典，用于树控件和 UI 组件
    """
    raw = ctx.get("colors", {})
    is_dark = ctx.get("is_dark", True)

    def _qcolor(key: str, fallback_light: str, fallback_dark: str) -> QColor:
        val = raw.get(key, "")
        if val:
            return QColor(val)
        return QColor(fallback_dark if is_dark else fallback_light)

    accent = _qcolor("accent", "#2878dc", "#62a0ea")
    border = _qcolor("border", "#cccccc80", "#ffffff1e")
    bg = ctx.get("card_bg", None)

    return {
        "accent": accent,
        "border": border,
        "text": _qcolor("text_primary", "#000000", "#ffffff"),
        "text_secondary": _qcolor("text_secondary", "#666666", "#aaaaaa"),
        "card_bg": QColor(bg) if bg else QColor(0, 0, 0, 0),
        "is_dark": is_dark,
        "font_family": ctx.get("font_family", "Microsoft YaHei"),
        "font_size": ctx.get("font_size", 14),
    }


# ── 文件类型图标缓存 ─────────────────────────────────────

_FILE_ICON_PROVIDER = None


def _get_file_icon(file_path: str) -> QIcon:
    """获取文件类型对应的系统图标（基于扩展名）

    使用 QFileIconProvider 查询系统关联的图标，比通用 SP_FileIcon 更直观。
    Args:
        file_path: 文件路径，用于 QFileInfo 推断扩展名
    Returns:
        系统关联的文件类型图标
    """
    global _FILE_ICON_PROVIDER
    if _FILE_ICON_PROVIDER is None:
        _FILE_ICON_PROVIDER = QFileIconProvider()

    info = QFileInfo(file_path)
    icon = _FILE_ICON_PROVIDER.icon(info)
    if icon and not icon.isNull():
        return icon
    # fallback: 通用文件图标
    return QApplication.style().standardIcon(QStyle.SP_FileIcon)


def _get_dir_icon() -> QIcon:
    """获取系统文件夹图标"""
    global _FILE_ICON_PROVIDER
    if _FILE_ICON_PROVIDER is None:
        _FILE_ICON_PROVIDER = QFileIconProvider()

    icon = _FILE_ICON_PROVIDER.icon(QFileIconProvider.Folder)
    if icon and not icon.isNull():
        return icon
    return QApplication.style().standardIcon(QStyle.SP_DirIcon)


def _get_dir_open_icon() -> QIcon:
    """获取系统文件夹打开图标"""
    global _FILE_ICON_PROVIDER
    if _FILE_ICON_PROVIDER is None:
        _FILE_ICON_PROVIDER = QFileIconProvider()

    icon = _FILE_ICON_PROVIDER.icon(QFileIconProvider.Folder)
    if icon and not icon.isNull():
        return icon
    return QApplication.style().standardIcon(QStyle.SP_DirOpenIcon)


# ══════════════════════════════════════════════════════════
# 目录扫描数据结构
# ══════════════════════════════════════════════════════════


class _DirEntry:
    """目录条目数据结构"""

    __slots__ = ("name", "path", "is_dir", "children")

    def __init__(self, name: str, path: str, is_dir: bool):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.children: Optional[List["_DirEntry"]] = None  # None=未扫描, []=空目录

    def __repr__(self):
        return f"<{('DIR' if self.is_dir else 'FILE')} {self.name}>"


# ══════════════════════════════════════════════════════════
# 异步目录扫描工作器
# ══════════════════════════════════════════════════════════


class _TreeScanner(QObject):
    """后台线程目录扫描器

    扫描指定目录的即时子条目（非递归），返回 _DirEntry 列表。
    """

    finished = pyqtSignal(object)  # List[_DirEntry]
    error = pyqtSignal(str)

    def __init__(self):
        super().__init__()

    def scan(self, directory: str):
        """扫描目录的即时子条目（在工作线程中调用）

        Args:
            directory: 要扫描的目录路径
        """
        try:
            entries: List[_DirEntry] = []
            if not os.path.isdir(directory):
                self.finished.emit(entries)
                return

            with os.scandir(directory) as it:
                for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
                    try:
                        name = entry.name
                        is_dir = entry.is_dir()

                        if not _should_show(name, is_dir):
                            continue

                        de = _DirEntry(
                            name=name,
                            path=entry.path,
                            is_dir=is_dir,
                        )
                        entries.append(de)
                    except OSError:
                        # 跳过权限不足的条目
                        continue

            self.finished.emit(entries)
        except Exception as e:
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ══════════════════════════════════════════════════════════
# 目录变更监听器
# ══════════════════════════════════════════════════════════


class _DirWatcher(QObject):
    """封装 QFileSystemWatcher，管理已展开目录的监听

    特性：
    - 自动限制最大监听路径数
    - 变更通知防抖（避免短时间内重复刷新）
    - 路径增删接口
    """

    dir_changed = pyqtSignal(str)  # 发生变更的目录路径

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt5.QtCore import QFileSystemWatcher

        self._watcher = QFileSystemWatcher(self)
        self._debounce_timers: Dict[str, QTimer] = {}
        self._watcher.directoryChanged.connect(self._on_dir_changed)

    def add_path(self, path: str):
        """添加要监听的目录路径"""
        if not os.path.isdir(path):
            return
        if path in self._watcher.directories():
            return
        if len(self._watcher.directories()) >= _MAX_WATCH_PATHS:
            # 超出限制：移除最早添加的路径
            oldest = self._watcher.directories()[0]
            self._watcher.removePath(oldest)
            logger.debug(f"[DirWatcher] 超出监听上限，移除: {oldest}")

        self._watcher.addPath(path)
        logger.debug(f"[DirWatcher] 开始监听: {path}")

    def remove_path(self, path: str):
        """移除监听的目录路径"""
        if path in self._watcher.directories():
            self._watcher.removePath(path)
            logger.debug(f"[DirWatcher] 停止监听: {path}")

        # 清理关联的防抖定时器
        if path in self._debounce_timers:
            self._debounce_timers[path].stop()
            self._debounce_timers[path].deleteLater()
            del self._debounce_timers[path]

    def clear(self):
        """移除所有监听路径"""
        paths = list(self._watcher.directories())
        for p in paths:
            self._watcher.removePath(p)
        for timer in self._debounce_timers.values():
            timer.stop()
            timer.deleteLater()
        self._debounce_timers.clear()
        logger.debug("[DirWatcher] 清空所有监听")

    def _on_dir_changed(self, path: str):
        """目录变更处理（带防抖）"""
        # 取消已有的防抖定时器
        if path in self._debounce_timers:
            self._debounce_timers[path].stop()
        else:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda p=path: self._emit_change(p))
            self._debounce_timers[path] = timer

        self._debounce_timers[path].start(_WATCH_DEBOUNCE_MS)

    def _emit_change(self, path: str):
        """防抖结束后发射变更信号"""
        if path in self._debounce_timers:
            self._debounce_timers[path].deleteLater()
            del self._debounce_timers[path]
        self.dir_changed.emit(path)


# ══════════════════════════════════════════════════════════
# 主卡片
# ══════════════════════════════════════════════════════════


class FileTreeCard(QWidget):
    """项目文件树浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._scanner: Optional[_TreeScanner] = None
        self._colors: dict = {}
        self._project_root: str = ""
        self._search_text: str = ""
        self._current_expanded_paths: Set[str] = set()

        self._setup_ui()
        self._setup_watcher()
        self._setup_connections()

        # 🛡️ 无论 widget 以何种方式销毁（deleteLater/父组件级联/GC），
        # 都确保工作线程被清理，防止 "QThread: Destroyed while thread is still running"
        self.destroyed.connect(self._cleanup_worker)

    # ── UI 初始化 ────────────────────────────────────────

    def _setup_ui(self):
        """构建 UI 布局"""
        self.setObjectName("file-tree-card")
        self.setAttribute(Qt.WA_StyledBackground, True)

        # 主布局
        self._vbox = QVBoxLayout(self)
        self._vbox.setContentsMargins(0, 0, 0, 0)
        self._vbox.setSpacing(0)

        # ── 顶栏 ──
        self._top_bar = QFrame(self)
        self._top_bar.setObjectName("file-tree-top-bar")
        self._top_bar.setFixedHeight(48)
        top_layout = QHBoxLayout(self._top_bar)
        top_layout.setContentsMargins(16, 0, 12, 0)
        top_layout.setSpacing(8)

        # 标题图标
        self._icon_widget = IconWidget(FluentIcon.FOLDER, self._top_bar)
        self._icon_widget.setFixedSize(20, 20)

        # 标题文字
        self._title_label = StrongBodyLabel("项目文件树", self._top_bar)
        self._title_label.setObjectName("file-tree-title")

        # 搜索输入框
        self._search_input = QLineEdit(self._top_bar)
        self._search_input.setObjectName("file-tree-search")
        self._search_input.setPlaceholderText("过滤文件...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setFixedWidth(200)
        self._search_input.setFixedHeight(32)

        # 刷新按钮
        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, self._top_bar)
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.setToolTip("刷新文件树")

        # 关闭按钮
        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, self._top_bar)
        self._close_btn.setFixedSize(32, 32)
        self._close_btn.setToolTip("关闭")

        top_layout.addWidget(self._icon_widget)
        top_layout.addWidget(self._title_label)
        top_layout.addStretch()
        top_layout.addWidget(self._search_input)
        top_layout.addWidget(self._refresh_btn)
        top_layout.addWidget(self._close_btn)

        # ── 树控件（放在 ScrollArea 中支持滚动） ──
        self._scroll_area = ScrollArea(self)
        self._scroll_area.setObjectName("file-tree-scroll")
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._tree_widget = QTreeWidget(self._scroll_area)
        self._tree_widget.setObjectName("file-tree-widget")
        self._tree_widget.setHeaderHidden(True)
        self._tree_widget.setAnimated(True)
        self._tree_widget.setIndentation(20)
        self._tree_widget.setRootIsDecorated(True)
        self._tree_widget.setIconSize(QSize(18, 18))
        self._tree_widget.setFrameShape(QFrame.NoFrame)
        self._tree_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # 占位提示
        self._placeholder = QLabel("正在加载文件树...", self._tree_widget)
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: rgba(255,255,255,0.4);")
        self._placeholder.setWordWrap(True)

        self._scroll_area.setWidget(self._tree_widget)

        # 组装
        self._vbox.addWidget(self._top_bar)
        self._vbox.addWidget(self._scroll_area, 1)

        # 初始样式
        self._apply_placeholder_style()

    def _setup_watcher(self):
        """初始化目录变更监听器"""
        self._watcher = _DirWatcher(self)
        self._watcher.dir_changed.connect(self._on_dir_changed_externally)

    def _setup_connections(self):
        """连接信号"""
        self._close_btn.clicked.connect(self._on_close)
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._search_input.textChanged.connect(self._on_search_debounced)
        self._tree_widget.itemExpanded.connect(self._on_item_expanded)
        self._tree_widget.itemCollapsed.connect(self._on_item_collapsed)
        self._tree_widget.customContextMenuRequested.connect(self._on_context_menu)
        self._tree_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree_widget.itemDoubleClicked.connect(self._on_item_double_clicked)

        # 搜索防抖定时器
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._do_filter_tree)

    # ── 公开接口 ──────────────────────────────────────────

    def set_context_provider(self, provider: Callable[[], dict]):
        """注入上下文提供函数（由 UIPluginRegistry 调用）"""
        self._context_provider = provider

    def show_card(self):
        """卡片显示时：用最新上下文刷新主题色 + 加载文件树"""
        self._apply_latest_theme()
        self._async_load_tree()
        self.setVisible(True)

    # ── 主题色 ────────────────────────────────────────────

    def _apply_latest_theme(self):
        """从上下文拉取最新主题色并刷新样式"""
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
        except Exception:
            return

        self._colors = _make_colors_from_context(ctx)
        self._project_root = ctx.get("project_root", "")

        tc = _ctx_text_color(ctx)
        tcs = _ctx_text_color(ctx, secondary=True)
        tc_qcolor = self._colors.get("text", QColor(255, 255, 255))
        border_c = self._colors.get("border", QColor(255, 255, 255, 30))

        # 标题
        self._title_label.setStyleSheet(f"color: {tc}; background: transparent;")

        # 顶栏背景
        self._top_bar.setStyleSheet(
            f"#file-tree-top-bar {{"
            f"  background: transparent;"
            f"  border-bottom: 1px solid {border_c.name() + hex(border_c.alpha())[2:].zfill(2)};"
            f"}}"
        )

        # 搜索框
        self._search_input.setStyleSheet(
            f"#file-tree-search {{"
            f"  background: rgba(255,255,255,0.08);"
            f"  border: 1px solid {border_c.name()};"
            f"  border-radius: 6px;"
            f"  padding: 0 10px;"
            f"  color: {tc};"
            f"  font-size: 13px;"
            f"}}"
            f"#file-tree-search:focus {{"
            f"  border: 1px solid {self._colors['accent'].name()};"
            f"}}"
        )

        # 树控件样式
        tc_hex = tc_qcolor.name()
        bg_hex = "transparent"
        accent_hex = self._colors["accent"].name()
        self._tree_widget.setStyleSheet(
            f"#file-tree-widget {{"
            f"  background: {bg_hex};"
            f"  border: none;"
            f"  color: {tc_hex};"
            f"  font-size: 13px;"
            f"}}"
            f"#file-tree-widget::item {{"
            f"  padding: 4px 8px;"
            f"  border-radius: 4px;"
            f"}}"
            f"#file-tree-widget::item:selected {{"
            f"  background: {accent_hex}40;"
            f"  color: {tc_hex};"
            f"}}"
            f"#file-tree-widget::item:hover {{"
            f"  background: rgba(255,255,255,0.08);"
            f"}}"
        )

        # ScrollArea 背景透明
        self._scroll_area.setStyleSheet(
            "#file-tree-scroll {"
            "  background: transparent;"
            "  border: none;"
            "}"
        )
        self._scroll_area.viewport().setStyleSheet("background: transparent; border: none;")

        # 刷新字体设置
        font_family = ctx.get("font_family", "Microsoft YaHei")
        font_size = ctx.get("font_size", 14)
        self._tree_widget.setFont(QFont(font_family, font_size - 2))
        self._search_input.setFont(QFont(font_family, font_size - 3))

    def _apply_placeholder_style(self):
        """占位提示的样式"""
        self._placeholder.setStyleSheet("color: rgba(255,255,255,0.4); font-size: 14px;")

    # ── 异步加载 ──────────────────────────────────────────

    def _async_load_tree(self, target_dir: Optional[str] = None):
        """异步加载文件树

        Args:
            target_dir: 要扫描的目录。None 表示扫描项目根目录
        """
        scan_dir = target_dir or self._project_root
        if not scan_dir or not os.path.isdir(scan_dir):
            self._show_error("项目目录不存在，请先设置工作目录")
            return

        self._cleanup_worker()

        # ⚠ 不要给 QThread 设 parent（widget），热重载时 widget 删除不会连带销毁
        # 运行中的线程，避免 "QThread: Destroyed while thread is still running"
        self._worker_thread = QThread()
        self._scanner = _TreeScanner()
        self._scanner.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(lambda: self._scanner.scan(scan_dir))
        self._scanner.finished.connect(lambda entries: self._on_scan_finished(entries, scan_dir))
        self._scanner.error.connect(self._on_scan_error)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def _cleanup_worker(self):
        """清理旧的工作线程"""
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(3000)
            except RuntimeError:
                pass
            self._worker_thread = None
            self._scanner = None

    def _on_scan_finished(self, entries: List[_DirEntry], scan_dir: str):
        """扫描完成 — 填充树控件"""
        self._worker_thread = None
        self._scanner = None

        # 判断是根扫描还是子目录扫描
        is_root = scan_dir == self._project_root

        if is_root:
            # 根扫描：清空并重建
            self._tree_widget.clear()
            self._placeholder.setVisible(False)

            if not entries:
                self._placeholder.setText("项目目录为空")
                self._placeholder.setVisible(True)
                return

            for entry in entries:
                self._create_tree_item(None, entry)

            # 展开根目录的第一层（如果是根目录本身，默认展开一级）
            # 实际上是根的子项，默认不展开，让用户自行展开

            # 将根目录加入监听
            if self._project_root and os.path.isdir(self._project_root):
                self._watcher.add_path(self._project_root)

        else:
            # 子目录扫描：找到对应的树节点并更新
            self._update_subtree(scan_dir, entries)

    def _on_scan_error(self, error_msg: str):
        """扫描出错"""
        self._worker_thread = None
        self._scanner = None
        logger.error(f"[FileTree] 扫描失败: {error_msg}")
        self._show_error("扫描目录时出错")

    def _show_error(self, message: str):
        """显示错误提示"""
        self._tree_widget.clear()
        self._placeholder.setText(f"⚠️ {message}")
        self._placeholder.setVisible(True)

    # ── 树节点管理 ────────────────────────────────────────

    def _create_tree_item(
        self,
        parent: Optional[QTreeWidgetItem],
        entry: _DirEntry,
    ) -> QTreeWidgetItem:
        """创建树节点

        Args:
            parent: 父节点，None 表示根节点
            entry: 目录条目数据

        Returns:
            创建的 QTreeWidgetItem
        """
        if parent is None:
            item = QTreeWidgetItem(self._tree_widget)
        else:
            item = QTreeWidgetItem(parent)

        item.setText(0, entry.name)
        item.setToolTip(0, entry.path)

        # 存储元数据
        item.setData(0, Qt.UserRole, entry.path)
        item.setData(0, Qt.UserRole + 1, entry.is_dir)
        item.setData(0, Qt.UserRole + 2, False)  # loaded flag

        if entry.is_dir:
            item.setIcon(0, _get_dir_icon())
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        else:
            item.setIcon(0, _get_file_icon(entry.path))

        return item

    def _update_subtree(self, dir_path: str, entries: List[_DirEntry]):
        """更新指定目录下的子树

        Args:
            dir_path: 目录路径
            entries: 扫描结果
        """
        # 查找对应节点
        target_item = self._find_item_by_path(self._tree_widget.invisibleRootItem(), dir_path)
        if target_item is None:
            return

        # 记录当前展开的子路径集合
        expanded_children = set()
        for i in range(target_item.childCount()):
            child = target_item.child(i)
            child_path = child.data(0, Qt.UserRole)
            if child.isExpanded():
                expanded_children.add(child_path)

        # 清空旧子节点
        target_item.takeChildren()

        # 重建
        if not entries:
            # 空目录：移除展开指示器
            target_item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicatorWhenChildless)
        else:
            target_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            for entry in entries:
                self._create_tree_item(target_item, entry)

            # 恢复之前展开的子节点
            for i in range(target_item.childCount()):
                child = target_item.child(i)
                child_path = child.data(0, Qt.UserRole)
                if child_path in expanded_children:
                    child.setExpanded(True)

        # 更新加载标志
        target_item.setData(0, Qt.UserRole + 2, True)

    def _find_item_by_path(self, root_item: QTreeWidgetItem, target_path: str) -> Optional[QTreeWidgetItem]:
        """在树中查找对应路径的节点"""
        # 检查 root_item 自身
        item_path = root_item.data(0, Qt.UserRole)
        if item_path and os.path.normpath(item_path) == os.path.normpath(target_path):
            return root_item

        # 递归检查子节点
        for i in range(root_item.childCount()):
            child = root_item.child(i)
            result = self._find_item_by_path(child, target_path)
            if result is not None:
                return result

        return None

    # ── 展开/折叠处理 ─────────────────────────────────────

    def _on_item_expanded(self, item: QTreeWidgetItem):
        """目录展开时：异步扫描子目录"""
        item_path = item.data(0, Qt.UserRole)
        is_dir = item.data(0, Qt.UserRole + 1)
        loaded = item.data(0, Qt.UserRole + 2)

        if not is_dir:
            return

        if not os.path.isdir(item_path):
            return

        # 记录已展开的路径
        self._current_expanded_paths.add(item_path)

        # 如果已经加载过，不需要重新扫描
        if loaded:
            return

        # 切换为加载中图标
        item.setIcon(0, _get_dir_open_icon())

        # 异步扫描
        self._cleanup_worker()
        # ⚠ 不要给 QThread 设 parent（widget），热重载时 widget 删除不会连带销毁
        # 运行中的线程，避免 "QThread: Destroyed while thread is still running"
        self._worker_thread = QThread()
        self._scanner = _TreeScanner()
        self._scanner.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(lambda: self._scanner.scan(item_path))
        self._scanner.finished.connect(lambda entries: self._on_subdir_scan_finished(entries, item, item_path))
        self._scanner.error.connect(self._on_scan_error)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def _on_subdir_scan_finished(self, entries: List[_DirEntry], item: QTreeWidgetItem, item_path: str):
        """子目录扫描完成"""
        self._worker_thread = None
        self._scanner = None

        # 检查 item 是否仍然有效
        try:
            _ = item.data(0, Qt.UserRole)
        except RuntimeError:
            return  # item 已经被销毁

        # 恢复图标
        item.setIcon(0, _get_dir_icon())

        # 清空占位子节点（如果有）
        item.takeChildren()

        if entries:
            for entry in entries:
                self._create_tree_item(item, entry)
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        else:
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicatorWhenChildless)

        item.setData(0, Qt.UserRole + 2, True)

        # 添加到监听
        if os.path.isdir(item_path):
            self._watcher.add_path(item_path)

    def _on_item_collapsed(self, item: QTreeWidgetItem):
        """目录折叠时：可选地从监听器中移除（保留已加载数据）"""
        item_path = item.data(0, Qt.UserRole)
        self._current_expanded_paths.discard(item_path)
        # 注意：不移除监听，因为用户可能再次展开，保留监听可立即感知变更
        # 但如果监听路径过多，在 _DirWatcher.add_path 中自动处理上限

    # ── 目录变更处理 ─────────────────────────────────────

    def _on_dir_changed_externally(self, dir_path: str):
        """外部目录变更时：重新扫描对应子树"""
        logger.debug(f"[FileTree] 目录变更: {dir_path}")

        # 查找对应的树节点
        target_item = self._find_item_by_path(self._tree_widget.invisibleRootItem(), dir_path)

        if target_item is None:
            # 不在已展开的树中，忽略
            return

        # 如果节点是展开状态，重新扫描
        try:
            is_expanded = target_item.isExpanded()
        except RuntimeError:
            return

        if is_expanded:
            # 重新扫描
            self._reload_children_async(target_item, dir_path)

    def _reload_children_async(self, item: QTreeWidgetItem, dir_path: str):
        """异步重新加载指定节点的子项"""
        self._cleanup_worker()

        # ⚠ 不要给 QThread 设 parent（widget），热重载时 widget 删除不会连带销毁
        # 运行中的线程，避免 "QThread: Destroyed while thread is still running"
        self._worker_thread = QThread()
        self._scanner = _TreeScanner()
        self._scanner.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(lambda: self._scanner.scan(dir_path))
        self._scanner.finished.connect(lambda entries: self._on_subdir_reload(entries, item, dir_path))
        self._scanner.error.connect(self._on_scan_error)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def _on_subdir_reload(self, entries: List[_DirEntry], item: QTreeWidgetItem, dir_path: str):
        """子树重新加载完成"""
        self._worker_thread = None
        self._scanner = None

        # 更新子树
        self._update_subtree(dir_path, entries)

    # ── 右键菜单 ──────────────────────────────────────────

    def _on_context_menu(self, pos):
        """右键菜单"""
        item = self._tree_widget.itemAt(pos)
        if not item:
            return

        item_path = item.data(0, Qt.UserRole)
        item_name = item.text(0)
        is_dir = item.data(0, Qt.UserRole + 1)

        if not item_path:
            return

        menu = QMenu(self)

        # 复制路径
        action_copy_path = menu.addAction("复制路径")
        action_copy_path.triggered.connect(lambda: self._copy_to_clipboard(item_path))

        # 复制文件名
        action_copy_name = menu.addAction("复制文件名")
        action_copy_name.triggered.connect(lambda: self._copy_to_clipboard(item_name))

        menu.addSeparator()

        # 在资源管理器中打开
        if is_dir:
            action_open = menu.addAction("在资源管理器中打开")
            action_open.triggered.connect(lambda: self._open_in_explorer(item_path))
        else:
            action_open = menu.addAction("打开所在文件夹")
            action_open.triggered.connect(lambda: self._open_in_explorer(os.path.dirname(item_path)))
            # 用默认程序打开文件
            action_open_file = menu.addAction("用默认程序打开")
            action_open_file.triggered.connect(lambda: self._open_file(item_path))

        menu.exec_(self._tree_widget.viewport().mapToGlobal(pos))

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        """双击文件：用默认程序打开；双击目录：展开/折叠（由 QTreeWidget 默认处理）"""
        item_path = item.data(0, Qt.UserRole)
        if item_path and os.path.isfile(item_path):
            self._open_file(item_path)

    @staticmethod
    def _copy_to_clipboard(text: str):
        """复制文本到剪贴板"""
        clipboard = QApplication.clipboard()
        clipboard.setText(text)

    @staticmethod
    def _open_in_explorer(path: str):
        """在文件资源管理器中打开并选中"""
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as e:
            logger.error(f"[FileTree] 打开资源管理器失败: {e}")

    @staticmethod
    def _open_file(path: str):
        """用默认程序打开文件"""
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            logger.error(f"[FileTree] 打开文件失败: {e}")

    # ── 搜索过滤 ──────────────────────────────────────────

    def _on_search_debounced(self, text: str):
        """搜索输入防抖"""
        self._search_text = text
        self._search_timer.start(_SEARCH_DEBOUNCE_MS)

    def _do_filter_tree(self):
        """执行树过滤"""
        text = self._search_text.lower()
        root = self._tree_widget.invisibleRootItem()
        self._filter_children(root, text)

    def _filter_children(self, parent: QTreeWidgetItem, text: str) -> bool:
        """递归过滤子节点，返回是否有可见子节点"""
        has_visible = False
        for i in range(parent.childCount()):
            item = parent.child(i)
            name = item.text(0).lower()
            name_match = not text or text in name

            # 递归处理子节点
            child_has_visible = False
            if item.childCount() > 0:
                child_has_visible = self._filter_children(item, text)

            visible = name_match or child_has_visible
            item.setHidden(not visible)
            if visible:
                has_visible = True

        return has_visible

    # ── 刷新与关闭 ────────────────────────────────────────

    def _on_refresh(self):
        """手动刷新：重新扫描根目录"""
        self._watcher.clear()
        self._current_expanded_paths.clear()
        self._async_load_tree()

    def _on_close(self):
        """关闭卡片"""
        self._cleanup_worker()
        self._watcher.clear()
        self.closed.emit()

    def deleteLater(self):
        """热重载/强制删除时清理工作线程，避免 QThread 随 widget 销毁时仍在运行"""
        self._cleanup_worker()
        self._watcher.clear()
        super().deleteLater()

    # ── 比例高度 ──────────────────────────────────────────

    def sizeHint(self):
        """与 SystemCardFrame proportional 模式一致：返回窗口高度的 85%"""
        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            return QSize(max(base.width(), 200), int(win.height() * 0.85))
        return base

    def showEvent(self, event):
        """显示时安装窗口 resize 事件过滤器"""
        super().showEvent(event)
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        """监听窗口 resize，触发 updateGeometry → CardContainer 重算高度"""
        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)
