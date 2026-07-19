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
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from PyQt5.QtCore import QEvent, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QSizePolicy,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    ScrollArea,
    StrongBodyLabel,
    TransparentToolButton,
    isDarkTheme,
)
from loguru import logger

from .scanner import _DirEntry, _TreeScanner
from .tree_widget import FileTreeWidget, _get_dir_icon, _get_dir_open_icon, _get_file_icon
from .watcher import _DirWatcher


# ── 路径常量 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SEARCH_DEBOUNCE_MS = 300


# ── 主题色辅助 ────────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    """fallback 文字颜色（无上下文时使用）"""
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    return val if val else _text_color(secondary)


def _parse_theme_color(color_str: str) -> QColor:
    """解析主题色字符串为 QColor

    支持格式：'#RRGGBB'、'#RGB'、'rgb(r,g,b)'、'rgba(r,g,b,a)'。
    """
    if not color_str:
        return QColor(33, 33, 38)
    m = re.match(r"rgba\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", color_str)
    if m:
        return QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    m = re.match(r"rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", color_str)
    if m:
        return QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return QColor(color_str)


def _make_colors_from_context(ctx: dict) -> dict:
    """将 context 主题色映射为可用的颜色字典"""
    raw = ctx.get("colors", {})
    is_dark = ctx.get("is_dark", True)

    def _qcolor(key: str, fallback_light: str, fallback_dark: str) -> QColor:
        val = raw.get(key, "")
        if val:
            return _parse_theme_color(val)
        return QColor(fallback_dark if is_dark else fallback_light)

    accent = _qcolor("accent", "#2878dc", "#62a0ea")
    border = _qcolor("border", "#cccccc80", "#ffffff1e")
    bg = ctx.get("card_bg", None) or ctx.get("colors", {}).get("card_bg", None)

    return {
        "accent": accent,
        "border": border,
        "text": _qcolor("text_primary", "#000000", "#ffffff"),
        "text_secondary": _qcolor("text_secondary", "#666666", "#aaaaaa"),
        "card_bg": _parse_theme_color(bg) if bg else QColor(33, 33, 38),
        "is_dark": is_dark,
        "font_family": ctx.get("font_family", "Microsoft YaHei"),
        "font_size": ctx.get("font_size", 14),
    }


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

        self.destroyed.connect(self._cleanup_worker)

    # ── UI 初始化 ──

    def _setup_ui(self):
        self.setObjectName("file-tree-card")
        self.setAttribute(Qt.WA_StyledBackground, True)

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

        self._icon_widget = IconWidget(FluentIcon.FOLDER, self._top_bar)
        self._icon_widget.setFixedSize(20, 20)

        self._title_label = StrongBodyLabel("项目文件树", self._top_bar)
        self._title_label.setObjectName("file-tree-title")

        self._search_input = QLineEdit(self._top_bar)
        self._search_input.setObjectName("file-tree-search")
        self._search_input.setPlaceholderText("过滤文件...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setFixedWidth(200)
        self._search_input.setFixedHeight(32)

        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, self._top_bar)
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.setToolTip("刷新文件树")

        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, self._top_bar)
        self._close_btn.setFixedSize(32, 32)
        self._close_btn.setToolTip("关闭")

        top_layout.addWidget(self._icon_widget)
        top_layout.addWidget(self._title_label)
        top_layout.addStretch()
        top_layout.addWidget(self._search_input)
        top_layout.addWidget(self._refresh_btn)
        top_layout.addWidget(self._close_btn)

        # ── 树控件 ──
        self._scroll_area = ScrollArea(self)
        self._scroll_area.setObjectName("file-tree-scroll")
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._tree_widget = FileTreeWidget(self._scroll_area)
        self._tree_widget.set_tree_card(self)
        self._tree_widget.setObjectName("file-tree-widget")
        self._tree_widget.setHeaderHidden(True)
        self._tree_widget.setAnimated(True)
        self._tree_widget.setIndentation(20)
        self._tree_widget.setRootIsDecorated(True)
        self._tree_widget.setIconSize(QSize(18, 18))
        self._tree_widget.setFrameShape(QFrame.NoFrame)
        self._tree_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        _dark = isDarkTheme()
        _ph_color = "rgba(255,255,255,0.4)" if _dark else "rgba(0,0,0,0.4)"
        self._placeholder = QLabel("正在加载文件树...", self._tree_widget)
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(f"color: {_ph_color};")
        self._placeholder.setWordWrap(True)

        self._scroll_area.setWidget(self._tree_widget)
        self._vbox.addWidget(self._top_bar)
        self._vbox.addWidget(self._scroll_area, 1)

        self._apply_placeholder_style()

    def _setup_watcher(self):
        self._watcher = _DirWatcher(self)
        self._watcher.dir_changed.connect(self._on_dir_changed_externally)

    def _setup_connections(self):
        self._close_btn.clicked.connect(self._on_close)
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._search_input.textChanged.connect(self._on_search_debounced)
        self._tree_widget.itemExpanded.connect(self._on_item_expanded)
        self._tree_widget.itemCollapsed.connect(self._on_item_collapsed)
        self._tree_widget.customContextMenuRequested.connect(self._on_context_menu)
        self._tree_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree_widget.itemDoubleClicked.connect(self._on_item_double_clicked)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._do_filter_tree)

    # ── 公开接口 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        self._context_provider = provider

    def show_card(self):
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._async_load_tree()
        self.setVisible(True)

    def _apply_plugin_icon(self):
        if self._context_provider is None or self._icon_widget is None:
            return
        try:
            ctx = self._context_provider()
            icon_info = ctx.get("plugin_icon", {})
            theme = "dark" if isDarkTheme() else "light"
            icon_path = icon_info.get(theme, "")
            if icon_path:
                self._icon_widget.setIcon(QIcon(icon_path))
        except Exception:
            pass

    # ── 主题色 ──

    def _apply_latest_theme(self):
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

        font_family = ctx.get("font_family", "Microsoft YaHei")
        font_size = ctx.get("font_size", 14)

        self._title_label.setStyleSheet(f"color: {tc}; background: transparent;")

        self._top_bar.setStyleSheet(
            f"#file-tree-top-bar {{"
            f"  background: transparent;"
            f"  border-bottom: 1px solid {border_c.name() + hex(border_c.alpha())[2:].zfill(2)};"
            f"}}"
        )

        is_dark = ctx.get("is_dark", True)
        search_bg = "rgba(255,255,255,0.08)" if is_dark else "rgba(0,0,0,0.06)"
        hover_bg = "rgba(255,255,255,0.08)" if is_dark else "rgba(0,0,0,0.06)"
        self._search_input.setStyleSheet(
            f"#file-tree-search {{"
            f"  background: {search_bg};"
            f"  border: 1px solid {border_c.name()};"
            f"  border-radius: 6px;"
            f"  padding: 0 10px;"
            f"  color: {tc};"
            f"  font-size: {font_size - 3}px;"
            f"}}"
            f"#file-tree-search:focus {{"
            f"  border: 1px solid {self._colors['accent'].name()};"
            f"}}"
        )

        tc_hex = tc_qcolor.name()
        bg_hex = "transparent"
        accent_hex = self._colors["accent"].name()
        self._tree_widget.setStyleSheet(
            f"#file-tree-widget {{"
            f"  background: {bg_hex};"
            f"  border: none;"
            f"  color: {tc_hex};"
            f"  font-size: {font_size - 2}px;"
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
            f"  background: {hover_bg};"
            f"}}"
        )

        self._scroll_area.setStyleSheet("#file-tree-scroll {  background: transparent;  border: none;}")
        self._scroll_area.viewport().setStyleSheet("background: transparent; border: none;")

        self._tree_widget.setFont(QFont(font_family, font_size - 2))
        self._search_input.setFont(QFont(font_family, font_size - 3))

    def _apply_placeholder_style(self):
        is_dark = True
        if hasattr(self, "_colors") and self._colors:
            is_dark = self._colors.get("is_dark", True)
        else:
            from qfluentwidgets import isDarkTheme as _isdark

            is_dark = _isdark()
        color = "rgba(255,255,255,0.4)" if is_dark else "rgba(0,0,0,0.4)"
        ph_font_size = self._colors.get("font_size", 14) if hasattr(self, "_colors") and self._colors else 14
        self._placeholder.setStyleSheet(f"color: {color}; font-size: {ph_font_size - 1}px;")

    # ── 异步加载 ──

    def _async_load_tree(self, target_dir: Optional[str] = None):
        scan_dir = target_dir or self._project_root
        if not scan_dir or not os.path.isdir(scan_dir):
            self._show_error("项目目录不存在，请先设置工作目录")
            return

        self._cleanup_worker()

        self._worker_thread = QThread()
        self._scanner = _TreeScanner()
        self._scanner.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(lambda: self._scanner.scan(scan_dir))
        self._scanner.finished.connect(lambda entries: self._on_scan_finished(entries, scan_dir))
        self._scanner.error.connect(self._on_scan_error)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def _cleanup_worker(self):
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(3000)
            except RuntimeError:
                pass
            self._worker_thread = None
            self._scanner = None

    def _on_scan_finished(self, entries: List[_DirEntry], scan_dir: str):
        self._worker_thread = None
        self._scanner = None

        is_root = scan_dir == self._project_root

        if is_root:
            self._tree_widget.clear()
            self._placeholder.setVisible(False)

            if not entries:
                self._placeholder.setText("项目目录为空")
                self._placeholder.setVisible(True)
                return

            for entry in entries:
                self._create_tree_item(None, entry)

            if self._project_root and os.path.isdir(self._project_root):
                self._watcher.add_path(self._project_root)
        else:
            self._update_subtree(scan_dir, entries)

    def _on_scan_error(self, error_msg: str):
        self._worker_thread = None
        self._scanner = None
        logger.error(f"[FileTree] 扫描失败: {error_msg}")
        self._show_error("扫描目录时出错")

    def _show_error(self, message: str):
        self._tree_widget.clear()
        self._placeholder.setText(f"⚠️ {message}")
        self._placeholder.setVisible(True)

    # ── 树节点管理 ──

    def _create_tree_item(
        self,
        parent: Optional[QTreeWidgetItem],
        entry: _DirEntry,
    ):
        if parent is None:
            item = QTreeWidgetItem(self._tree_widget)
        else:
            item = QTreeWidgetItem(parent)

        item.setText(0, entry.name)
        item.setToolTip(0, entry.path)

        item.setData(0, Qt.UserRole, entry.path)
        item.setData(0, Qt.UserRole + 1, entry.is_dir)
        item.setData(0, Qt.UserRole + 2, False)

        if entry.is_dir:
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled
            item.setIcon(0, _get_dir_icon())
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        else:
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
            item.setIcon(0, _get_file_icon(entry.path))

        item.setFlags(flags)
        return item

    def _update_subtree(self, dir_path: str, entries: List[_DirEntry]):
        target_item = self._find_item_by_path(self._tree_widget.invisibleRootItem(), dir_path)
        if target_item is None:
            return

        expanded_children = set()
        for i in range(target_item.childCount()):
            child = target_item.child(i)
            child_path = child.data(0, Qt.UserRole)
            if child.isExpanded():
                expanded_children.add(child_path)

        target_item.takeChildren()

        if not entries:
            target_item.setChildIndicatorPolicy(target_item.DontShowIndicatorWhenChildless)
        else:
            target_item.setChildIndicatorPolicy(target_item.ShowIndicator)
            for entry in entries:
                self._create_tree_item(target_item, entry)

            for i in range(target_item.childCount()):
                child = target_item.child(i)
                child_path = child.data(0, Qt.UserRole)
                if child_path in expanded_children:
                    child.setExpanded(True)

        target_item.setData(0, Qt.UserRole + 2, True)

    def _find_item_by_path(self, root_item, target_path: str):
        item_path = root_item.data(0, Qt.UserRole)
        if item_path and os.path.normpath(item_path) == os.path.normpath(target_path):
            return root_item

        for i in range(root_item.childCount()):
            child = root_item.child(i)
            result = self._find_item_by_path(child, target_path)
            if result is not None:
                return result

        return None

    # ── 展开/折叠处理 ──

    def _on_item_expanded(self, item):
        item_path = item.data(0, Qt.UserRole)
        is_dir = item.data(0, Qt.UserRole + 1)
        loaded = item.data(0, Qt.UserRole + 2)

        if not is_dir:
            return
        if not os.path.isdir(item_path):
            return

        self._current_expanded_paths.add(item_path)

        if loaded:
            return

        item.setIcon(0, _get_dir_open_icon())
        self._cleanup_worker()

        self._worker_thread = QThread()
        self._scanner = _TreeScanner()
        self._scanner.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(lambda: self._scanner.scan(item_path))
        self._scanner.finished.connect(lambda entries: self._on_subdir_scan_finished(entries, item, item_path))
        self._scanner.error.connect(self._on_scan_error)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def _on_subdir_scan_finished(self, entries: List[_DirEntry], item, item_path: str):
        self._worker_thread = None
        self._scanner = None

        try:
            _ = item.data(0, Qt.UserRole)
        except RuntimeError:
            return

        item.setIcon(0, _get_dir_icon())
        item.takeChildren()

        if entries:
            for entry in entries:
                self._create_tree_item(item, entry)
            item.setChildIndicatorPolicy(item.ShowIndicator)
        else:
            item.setChildIndicatorPolicy(item.DontShowIndicatorWhenChildless)

        item.setData(0, Qt.UserRole + 2, True)

        if os.path.isdir(item_path):
            self._watcher.add_path(item_path)

    def _on_item_collapsed(self, item):
        item_path = item.data(0, Qt.UserRole)
        self._current_expanded_paths.discard(item_path)

    # ── 目录变更处理 ──

    def _on_dir_changed_externally(self, dir_path: str):
        logger.debug(f"[FileTree] 目录变更: {dir_path}")
        target_item = self._find_item_by_path(self._tree_widget.invisibleRootItem(), dir_path)
        if target_item is None:
            return
        try:
            is_expanded = target_item.isExpanded()
        except RuntimeError:
            return

        if is_expanded:
            self._reload_children_async(target_item, dir_path)

    def _reload_children_async(self, item, dir_path: str):
        self._cleanup_worker()
        self._worker_thread = QThread()
        self._scanner = _TreeScanner()
        self._scanner.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(lambda: self._scanner.scan(dir_path))
        self._scanner.finished.connect(lambda entries: self._on_subdir_reload(entries, item, dir_path))
        self._scanner.error.connect(self._on_scan_error)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def _on_subdir_reload(self, entries: List[_DirEntry], item, dir_path: str):
        self._worker_thread = None
        self._scanner = None
        self._update_subtree(dir_path, entries)

    # ── 右键菜单 ──

    def _on_context_menu(self, pos):
        item = self._tree_widget.itemAt(pos)
        if not item:
            return

        item_path = item.data(0, Qt.UserRole)
        item_name = item.text(0)
        is_dir = item.data(0, Qt.UserRole + 1)

        if not item_path:
            return

        menu = QMenu(self)

        action_copy_path = menu.addAction("复制路径")
        action_copy_path.triggered.connect(lambda: self._copy_to_clipboard(item_path))

        action_copy_name = menu.addAction("复制文件名")
        action_copy_name.triggered.connect(lambda: self._copy_to_clipboard(item_name))

        menu.addSeparator()

        if is_dir:
            action_open = menu.addAction("在资源管理器中打开")
            action_open.triggered.connect(lambda: self._open_in_explorer(item_path))
        else:
            action_open = menu.addAction("打开所在文件夹")
            action_open.triggered.connect(lambda: self._open_in_explorer(os.path.dirname(item_path)))
            action_open_file = menu.addAction("用默认程序打开")
            action_open_file.triggered.connect(lambda: self._open_file(item_path))

        menu.exec_(self._tree_widget.viewport().mapToGlobal(pos))

    def _on_item_double_clicked(self, item, column: int):
        item_path = item.data(0, Qt.UserRole)
        if item_path and os.path.isfile(item_path):
            self._open_file(item_path)

    @staticmethod
    def _copy_to_clipboard(text: str):
        clipboard = QApplication.clipboard()
        clipboard.setText(text)

    @staticmethod
    def _open_in_explorer(path: str):
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
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            logger.error(f"[FileTree] 打开文件失败: {e}")

    # ── 搜索过滤 ──

    def _on_search_debounced(self, text: str):
        self._search_text = text
        self._search_timer.start(_SEARCH_DEBOUNCE_MS)

    def _do_filter_tree(self):
        text = self._search_text.lower()
        root = self._tree_widget.invisibleRootItem()
        self._filter_children(root, text)

    def _filter_children(self, parent, text: str) -> bool:
        has_visible = False
        for i in range(parent.childCount()):
            item = parent.child(i)
            name = item.text(0).lower()
            name_match = not text or text in name

            child_has_visible = False
            if item.childCount() > 0:
                child_has_visible = self._filter_children(item, text)

            visible = name_match or child_has_visible
            item.setHidden(not visible)
            if visible:
                has_visible = True

        return has_visible

    # ── 刷新与关闭 ──

    def _on_refresh(self):
        self._watcher.clear()
        self._current_expanded_paths.clear()
        self._async_load_tree()

    def _on_close(self):
        self._cleanup_worker()
        self._watcher.clear()
        self.closed.emit()

    def deleteLater(self):
        self._cleanup_worker()
        self._watcher.clear()
        super().deleteLater()

    # ── 比例高度 ──

    def sizeHint(self):
        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            return QSize(max(base.width(), 200), int(win.height() * 0.85))
        return base

    def showEvent(self, event):
        super().showEvent(event)
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)
