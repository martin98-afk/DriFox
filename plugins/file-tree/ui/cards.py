# -*- coding: utf-8 -*-
"""FileTreeCard 浮动卡片 — 项目文件树浏览（重构版）

功能：
- QTreeView + FileTreeModel + FilterProxy：多选 / 懒加载 / 搜索过滤
- 完整的文件 CRUD：新建 / 重命名(F2) / 删除 / 剪切 / 复制 / 粘贴
- 键盘快捷键：Ctrl+C/V/X、F2、Delete、Ctrl+A
- 右键菜单全功能
- 内部拖拽移动 + Ctrl拖拽复制
- 实时文件监听 + 异步扫描
- 主题色跟随主程序

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from loguru import logger
from PyQt5.QtCore import QEvent, QModelIndex, QSize, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QKeySequence
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QShortcut,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    IconWidget,
    MaskDialogBase,
    ScrollArea,
    StrongBodyLabel,
    TransparentPushButton,
    TransparentToolButton,
    isDarkTheme,
)

from .scanner import _DirEntry, _TreeScanner
from .tree_widget import (
    FileTreeFilterProxy,
    FileTreeModel,
    FileTreeView,
    RenameDelegate,
)
from .watcher import _DirWatcher

# ── 路径常量 ──────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ── 主题色辅助 ────────────────────────────────────────────


def _text_color(secondary: bool = False) -> str:
    if isDarkTheme():
        return "rgba(255,255,255,0.55)" if secondary else "rgba(255,255,255,0.9)"
    return "rgba(0,0,0,0.45)" if secondary else "rgba(0,0,0,0.85)"


def _ctx_text_color(ctx: dict, secondary: bool = False) -> str:
    colors = ctx.get("colors", {})
    key = "text_secondary" if secondary else "text_primary"
    val = colors.get(key, "")
    return val if val else _text_color(secondary)


def _parse_theme_color(color_str: str) -> QColor:
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
# 主题对话框辅助
# ══════════════════════════════════════════════════════════


def _styled_message_box(
    colors: dict,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButtons = QMessageBox.Yes | QMessageBox.No,
    default_button: QMessageBox.StandardButton = QMessageBox.No,
    parent_widget: Optional[QWidget] = None,
) -> int:
    """创建适配主题色的消息框 — 统一 MaskDialogBase 风格"""
    bg = colors.get("card_bg", QColor(33, 33, 38))
    tc = colors.get("text", QColor(255, 255, 255))
    accent = colors.get("accent", QColor(102, 198, 255))
    border = colors.get("border", QColor(61, 61, 61))
    font_size = colors.get("font_size", 14)
    ff = colors.get("font_family", "Microsoft YaHei")
    is_dark = colors.get("is_dark", True)

    hover_bg = bg.lighter(115) if is_dark else bg.darker(110)

    has_yes = bool(buttons & QMessageBox.Yes)
    has_no = bool(buttons & QMessageBox.No)
    has_ok = bool(buttons & QMessageBox.Ok)
    has_cancel = bool(buttons & QMessageBox.Cancel)

    class _Dialog(MaskDialogBase):
        def __init__(self, pw):
            super().__init__(pw)
            self._result = QMessageBox.No
            self._setup()

        def _setup(self):
            self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
            self.setClosableOnMaskClicked(True)
            self.setDraggable(True)
            self.setMaskColor(QColor(0, 0, 0, 76))

            self.widget.setObjectName("fileTreeStyledDialog")
            self.widget.setStyleSheet(
                f"#fileTreeStyledDialog {{"
                f"  background-color: {bg.name()};"
                f"  border: 1px solid {border.name()};"
                f"  border-radius: 8px;"
                f"}}"
            )

            layout = QVBoxLayout(self.widget)
            layout.setContentsMargins(28, 28, 28, 20)
            layout.setSpacing(0)

            title_lb = BodyLabel(title, self.widget)
            title_lb.setWordWrap(True)
            title_lb.setStyleSheet(
                f"color: {tc.name()}; background: transparent; "
                f"font-family: '{ff}'; font-size: {font_size + 2}px; font-weight: bold;"
            )
            layout.addWidget(title_lb)
            layout.addSpacing(6)

            content_lb = BodyLabel(text, self.widget)
            content_lb.setWordWrap(True)
            content_lb.setStyleSheet(
                f"color: {tc.name()}; background: transparent; "
                f"font-family: '{ff}'; font-size: {font_size - 1}px; line-height: 1.6;"
            )
            layout.addWidget(content_lb)
            layout.addStretch()

            btn_layout = QHBoxLayout()
            btn_layout.setSpacing(10)

            def _make_btn(label_text: str, result_code, is_default: bool, is_primary: bool):
                btn = TransparentPushButton(label_text, self.widget)
                btn.setCursor(Qt.PointingHandCursor)
                btn.setFixedHeight(36)
                if is_primary:
                    btn.setStyleSheet(
                        f"QPushButton {{"
                        f"  background-color: {accent.name()}; color: #ffffff;"
                        f"  border: none; border-radius: 8px; padding: 4px 28px;"
                        f"  font-family: '{ff}'; font-size: {font_size - 1}px; font-weight: bold;"
                        f"}}"
                    )
                else:
                    btn.setStyleSheet(
                        f"QPushButton {{"
                        f"  background-color: {bg.name()}; color: {tc.name()};"
                        f"  border: 1px solid {border.name()}; border-radius: 8px;"
                        f"  padding: 4px 28px; font-family: '{ff}';"
                        f"  font-size: {font_size - 1}px;"
                        f"}}"
                        f"QPushButton:hover {{"
                        f"  background-color: {hover_bg.name()}; border-color: {accent.name()};"
                        f"}}"
                    )
                if is_default:
                    btn.setDefault(True)
                    btn.setFocus()
                btn.clicked.connect(lambda: [setattr(self, "_result", result_code), self.close()])
                return btn

            btn_layout.addStretch()

            if has_ok:
                btn_layout.addWidget(_make_btn("确定", QMessageBox.Ok, True, True))
            else:
                if has_no:
                    btn_layout.addWidget(_make_btn("否", QMessageBox.No, default_button == QMessageBox.No, False))
                if has_yes:
                    btn_layout.addWidget(_make_btn("是", QMessageBox.Yes, default_button == QMessageBox.Yes, True))
                if has_cancel:
                    btn_layout.addWidget(
                        _make_btn("取消", QMessageBox.Cancel, default_button == QMessageBox.Cancel, False)
                    )

            layout.addLayout(btn_layout)
            # 内容自适应：最小 420×200，最大 620×720；水平 Fixed 防拉伸，垂直 Preferred 跟内容
            self.widget.setMinimumSize(420, 200)
            self.widget.setMaximumSize(620, 720)
            sp = self.widget.sizePolicy()
            sp.setHorizontalPolicy(QSizePolicy.Fixed)
            sp.setVerticalPolicy(QSizePolicy.Preferred)
            self.widget.setSizePolicy(sp)
            self.widget.adjustSize()

    dialog = _Dialog(parent_widget)
    dialog.exec_()
    return dialog._result


# ══════════════════════════════════════════════════════════
# FileTreeCard — 主卡片
# ══════════════════════════════════════════════════════════


class FileTreeCard(QWidget):
    """项目文件树浮动卡片（重构版）"""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._scanner: Optional[_TreeScanner] = None
        self._colors: dict = {}
        self._project_root: str = ""
        self._current_scan_target: str = ""  # 当前扫描目标目录

        # 剪贴板: [(路径, 是否剪切)]
        self._clipboard: List[Tuple[str, bool]] = []

        self._setup_ui()
        self._setup_model()
        self._setup_watcher()
        self._setup_connections()
        self._setup_shortcuts()

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

        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, self._top_bar)
        self._refresh_btn.setFixedSize(32, 32)
        self._refresh_btn.setToolTip("刷新文件树")

        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, self._top_bar)
        self._close_btn.setFixedSize(32, 32)
        self._close_btn.setToolTip("关闭")

        top_layout.addWidget(self._icon_widget)
        top_layout.addWidget(self._title_label)
        top_layout.addStretch()
        top_layout.addWidget(self._refresh_btn)
        top_layout.addWidget(self._close_btn)

        # ── 树控件区域 ──
        self._scroll_area = ScrollArea(self)
        self._scroll_area.setObjectName("file-tree-scroll")
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._tree_view = FileTreeView(self._scroll_area)
        self._tree_view.setObjectName("file-tree-widget")
        self._tree_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # 占位页面（QStackedWidget 切换，避免子控件无布局时左上角残留）
        self._stack = QStackedWidget(self._scroll_area)
        self._stack.setObjectName("file-tree-stack")
        self._stack.addWidget(self._tree_view)  # index 0: 文件树视图

        self._placeholder = QLabel("正在加载文件树...")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._ph_page = QWidget()
        _ph_layout = QVBoxLayout(self._ph_page)
        _ph_layout.setAlignment(Qt.AlignCenter)
        _ph_layout.addWidget(self._placeholder)
        self._stack.addWidget(self._ph_page)  # index 1: 居中占位页
        # 初始显示占位页，等待扫描完成
        self._stack.setCurrentIndex(1)

        self._scroll_area.setWidget(self._stack)
        self._vbox.addWidget(self._top_bar)
        self._vbox.addWidget(self._scroll_area, 1)

    def _setup_model(self):
        """创建 Model → Proxy → View 链路"""
        self._source_model = FileTreeModel(self)
        self._proxy_model = FileTreeFilterProxy(self)
        self._proxy_model.setSourceModel(self._source_model)
        self._tree_view.setModel(self._proxy_model)

        # RenameDelegate
        self._rename_delegate = RenameDelegate(self._tree_view)
        self._tree_view.setItemDelegateForColumn(0, self._rename_delegate)

    def _setup_watcher(self):
        self._watcher = _DirWatcher(self)
        self._watcher.dir_changed.connect(self._on_dir_changed_externally)

    def _setup_connections(self):
        # 顶栏按钮
        self._close_btn.clicked.connect(self._on_close)
        self._refresh_btn.clicked.connect(self._on_refresh)

        # Model 信号
        self._source_model.need_scan.connect(self._on_lazy_scan)

        # View 信号
        self._tree_view.delete_requested.connect(self._on_delete_requested)
        self._tree_view.move_requested.connect(self._on_move_requested)
        self._tree_view.copy_requested.connect(self._on_copy_drop_requested)
        self._tree_view.double_clicked_file.connect(self._open_file)
        self._tree_view.enter_expand_requested.connect(self._on_enter_expand)
        self._tree_view.context_menu_requested.connect(self._on_context_menu)
        self._tree_view.clipboard_copy.connect(self.copy_to_clipboard)
        self._tree_view.clipboard_cut.connect(self.cut_to_clipboard)
        self._tree_view.clipboard_paste.connect(self.paste_from_clipboard)

        # 目录展开/折叠 — 通过 expanded/collapsed 信号代理
        self._tree_view.expanded.connect(self._on_proxy_expanded)
        self._tree_view.collapsed.connect(self._on_proxy_collapsed)

        # RenameDelegate
        self._rename_delegate.rename_requested.connect(self._on_rename_file)

    def _setup_shortcuts(self):
        """快捷键已通过 FileTreeView 信号处理（clipboard_copy/cut/paste）"""
        pass

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
        self._tree_view.project_root = self._project_root
        self._watcher.set_root(self._project_root)

        tc = _ctx_text_color(ctx)
        border_c = self._colors.get("border", QColor(255, 255, 255, 30))
        font_family = ctx.get("font_family", "Microsoft YaHei")
        font_size = ctx.get("font_size", 14)

        self._title_label.setStyleSheet(
            f"color: {tc}; background: transparent; "
            f"font-family: '{font_family}'; font-size: {font_size}px; font-weight: bold;"
        )

        self._top_bar.setStyleSheet(
            f"#file-tree-top-bar {{"
            f"  background: transparent;"
            f"  border-bottom: 1px solid {border_c.name() + hex(border_c.alpha())[2:].zfill(2)};"
            f"}}"
        )

        is_dark = ctx.get("is_dark", True)
        hover_bg = "rgba(255,255,255,0.08)" if is_dark else "rgba(0,0,0,0.06)"

        tc_hex = self._colors["text"].name()
        accent_hex = self._colors["accent"].name()
        try:
            from app.utils.design_tokens import get_unified_scrollbar_style

            scrollbar_style = get_unified_scrollbar_style()
        except Exception:
            scrollbar_style = ""
        self._tree_view.setStyleSheet(
            f"#file-tree-widget {{"
            f"  background: transparent; border: none;"
            f"  color: {tc_hex}; font-size: {font_size}px;"
            f"}}"
            f"#file-tree-widget::item:selected {{"
            f"  background: {accent_hex}40; color: {tc_hex};"
            f"}}"
            f"#file-tree-widget::item:hover {{"
            f"  background: {hover_bg};"
            f"}}"
            f"{scrollbar_style}"
        )

        self._scroll_area.setStyleSheet("#file-tree-scroll { background: transparent; border: none;}")
        self._scroll_area.viewport().setStyleSheet("background: transparent; border: none;")

        self._tree_view.setFont(QFont(font_family, font_size))

        # 占位页主题色
        _ph_color = "rgba(255,255,255,0.4)" if is_dark else "rgba(0,0,0,0.4)"
        self._placeholder.setStyleSheet(
            f"color: {_ph_color}; background: transparent; font-family: '{font_family}'; font-size: {font_size}px;"
        )
        self._ph_page.setStyleSheet("background: transparent;")
        self._stack.setStyleSheet("background: transparent;")

        # 通知 delegate 更新主题色
        self._rename_delegate.set_colors(self._colors)

    # ── 异步加载 ──

    def _async_load_tree(self, target_dir: Optional[str] = None):
        scan_dir = target_dir or self._project_root
        if not scan_dir or not os.path.isdir(scan_dir):
            self._show_error("项目目录不存在，请先设置工作目录")
            return

        self._current_scan_target = scan_dir
        self._cleanup_worker()

        self._worker_thread = QThread()
        self._scanner = _TreeScanner()
        self._scanner.moveToThread(self._worker_thread)

        _scanner = self._scanner
        self._worker_thread.started.connect(lambda: _scanner.scan(scan_dir))
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
            self._source_model.set_project(scan_dir, entries)
            self._stack.setCurrentIndex(0 if entries else 1)
            if not entries:
                self._placeholder.setText("项目目录为空")

            if self._project_root and os.path.isdir(self._project_root):
                self._watcher.add_path(self._project_root)
        else:
            self._source_model.populate_children(scan_dir, entries)

    def _on_scan_error(self, error_msg: str):
        self._worker_thread = None
        self._scanner = None
        logger.error(f"[FileTree] 扫描失败: {error_msg}")
        self._show_error("扫描目录时出错")
        # 清除 model 中的加载标记
        if self._current_scan_target and self._current_scan_target != self._project_root:
            self._source_model.clear_loading(self._current_scan_target)

    def _show_error(self, message: str):
        self._placeholder.setText(f"⚠️ {message}")
        self._stack.setCurrentIndex(1)

    # ── 懒加载（展开触发扫描） ──

    def _on_lazy_scan(self, dir_path: str):
        """Model 请求扫描某目录的子节点"""
        self._current_scan_target = dir_path
        self._cleanup_worker()

        self._worker_thread = QThread()
        self._scanner = _TreeScanner()
        self._scanner.moveToThread(self._worker_thread)

        _scanner = self._scanner
        self._worker_thread.started.connect(lambda: _scanner.scan(dir_path))
        self._scanner.finished.connect(lambda entries: self._on_lazy_scan_done(entries, dir_path))
        self._scanner.error.connect(self._on_scan_error)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def _on_lazy_scan_done(self, entries: List[_DirEntry], dir_path: str):
        self._worker_thread = None
        self._scanner = None
        self._source_model.populate_children(dir_path, entries)
        if os.path.isdir(dir_path):
            self._watcher.add_path(dir_path)

    # ── 展开/折叠 ──

    def _on_proxy_expanded(self, proxy_idx: QModelIndex):
        src_idx = self._proxy_model.mapToSource(proxy_idx)
        if not src_idx.isValid():
            return
        dir_path = src_idx.data(FileTreeModel.COL_PATH)
        if not dir_path or not os.path.isdir(dir_path):
            return
        # fetchMore 已在 Qt 层自动触发扫描，这里只管理文件监听
        if not self._source_model.node_is_loading(dir_path) and not self._source_model.node_is_loaded(src_idx):
            logger.debug(f"[FileTree] 展开未加载目录(应由 fetchMore 触发): {dir_path}")
        self._watcher.add_path(dir_path)

    def _on_proxy_collapsed(self, proxy_idx: QModelIndex):
        src_idx = self._proxy_model.mapToSource(proxy_idx)
        if not src_idx.isValid():
            return
        dir_path = src_idx.data(FileTreeModel.COL_PATH)
        if dir_path:
            self._watcher.remove_path(dir_path)

    # ── Enter 展开 ──

    def _on_enter_expand(self, dir_path: str):
        node = self._source_model.find_node(dir_path)
        if not node:
            return
        src_idx = self._source_model._index_for_node(node)
        if not src_idx.isValid():
            return
        proxy_idx = self._proxy_model.mapFromSource(src_idx)
        if proxy_idx.isValid():
            if self._tree_view.isExpanded(proxy_idx):
                self._tree_view.collapse(proxy_idx)
            else:
                self._tree_view.expand(proxy_idx)

    # ── 目录变更监听 ──

    def _on_dir_changed_externally(self, dir_path: str):
        logger.debug(f"[FileTree] 外部变更: {dir_path}")
        # 根目录自身在 model 中无节点（_root_entries 只含根下条目），需单独处理
        is_root = os.path.normpath(dir_path) == os.path.normpath(self._project_root)
        if not is_root:
            node = self._source_model.find_node(dir_path)
            if node is None or not node.loaded:
                return
        # 异步重新扫描
        self._current_scan_target = dir_path
        self._cleanup_worker()
        self._worker_thread = QThread()
        self._scanner = _TreeScanner()
        self._scanner.moveToThread(self._worker_thread)
        _scanner = self._scanner
        self._worker_thread.started.connect(lambda: _scanner.scan(dir_path))
        self._scanner.finished.connect(lambda entries: self._on_watcher_reload(entries, dir_path))
        self._scanner.error.connect(self._on_scan_error)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_watcher_reload(self, entries: List[_DirEntry], dir_path: str):
        self._worker_thread = None
        self._scanner = None
        if os.path.normpath(dir_path) == os.path.normpath(self._project_root):
            self._source_model.refresh_root(entries)
            if entries:
                self._stack.setCurrentIndex(0)
            else:
                self._placeholder.setText("项目目录为空")
                self._stack.setCurrentIndex(1)
        else:
            self._source_model.refresh_children(dir_path, entries)

    # ── 剪贴板操作 ──

    def copy_to_clipboard(self):
        """Ctrl+C — 复制到内部剪贴板"""
        paths = self._tree_view.selected_source_paths()
        if not paths:
            return
        self._clipboard = [(p, False) for p in paths]
        logger.debug(f"[FileTree] 复制 {len(paths)} 项到剪贴板")

    def cut_to_clipboard(self):
        """Ctrl+X — 剪切到内部剪贴板"""
        paths = self._tree_view.selected_source_paths()
        if not paths:
            return
        # 排除项目根目录
        valid = [p for p in paths if os.path.normpath(p) != os.path.normpath(self._project_root)]
        if not valid:
            return
        self._clipboard = [(p, True) for p in valid]
        logger.debug(f"[FileTree] 剪切 {len(valid)} 项到剪贴板")

    def paste_from_clipboard(self):
        """Ctrl+V — 粘贴剪贴板内容"""
        if not self._clipboard:
            return

        # 找到目标目录
        idxs = self._tree_view.selectionModel().selectedRows()
        if idxs:
            src_idx = self._proxy_model.mapToSource(idxs[0])
            target_path = src_idx.data(FileTreeModel.COL_PATH)
            target_is_dir = src_idx.data(FileTreeModel.COL_IS_DIR)
            if target_is_dir:
                dest = target_path
            else:
                dest = os.path.dirname(target_path)
        else:
            dest = self._project_root

        if not dest or not os.path.isdir(dest):
            return

        # 执行操作
        for src_path, is_cut in self._clipboard:
            if not os.path.exists(src_path):
                logger.warning(f"[FileTree] 剪贴板项不存在: {src_path}")
                continue
            name = os.path.basename(src_path)
            dst = os.path.join(dest, name)
            if os.path.normpath(src_path) == os.path.normpath(dst):
                continue
            try:
                if is_cut:
                    if os.path.exists(dst):
                        reply = _styled_message_box(
                            self._colors,
                            QMessageBox.Warning,
                            "确认覆盖",
                            f"「{name}」已存在，确定覆盖？",
                            parent_widget=self.window(),
                        )
                        if reply != QMessageBox.Yes:
                            continue
                        if os.path.isdir(dst):
                            shutil.rmtree(dst)
                        else:
                            os.remove(dst)
                    shutil.move(src_path, dst)
                    self._source_model.remove_node(src_path)
                    logger.info(f"[FileTree] 移动: {src_path} → {dst}")
                else:
                    if os.path.exists(dst):
                        base, ext = os.path.splitext(name)
                        counter = 1
                        while os.path.exists(dst):
                            dst = os.path.join(dest, f"{base} ({counter}){ext}")
                            counter += 1
                    if os.path.isdir(src_path):
                        shutil.copytree(src_path, dst)
                    else:
                        shutil.copy2(src_path, dst)
                    logger.info(f"[FileTree] 复制: {src_path} → {dst}")

                # 更新目标目录
                self._refresh_dir_in_model(dest)
            except Exception as e:
                logger.error(f"[FileTree] 粘贴失败 {src_path}: {e}")
                _styled_message_box(
                    self._colors,
                    QMessageBox.Critical,
                    "操作失败",
                    f"无法粘贴「{name}」:\n{e}",
                    QMessageBox.Ok,
                    QMessageBox.Ok,
                    parent_widget=self.window(),
                )

        # 剪切后清空
        if self._clipboard and self._clipboard[0][1]:
            self._clipboard.clear()

    # ── 删除 ──

    def _on_delete_requested(self, paths: List[str]):
        deletable = [p for p in paths if os.path.normpath(p) != os.path.normpath(self._project_root)]
        if not deletable:
            _styled_message_box(
                self._colors,
                QMessageBox.Information,
                "提示",
                "不能删除项目根目录",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        if len(deletable) == 1:
            msg = f"确定要永久删除「{os.path.basename(deletable[0])}」？\n\n路径: {deletable[0]}\n\n⚠️ 此操作不可撤销！"
        else:
            names = "\n".join(f"• {os.path.basename(p)}" for p in deletable)
            msg = f"确定要永久删除以下 {len(deletable)} 个项目？\n\n{names}\n\n⚠️ 此操作不可撤销！"

        reply = _styled_message_box(
            self._colors,
            QMessageBox.Warning,
            "确认永久删除",
            msg,
            parent_widget=self.window(),
        )
        if reply != QMessageBox.Yes:
            return

        deleted = 0
        for path in deletable:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                self._source_model.remove_node(path)
                deleted += 1
                logger.info(f"[FileTree] 已删除: {path}")
            except Exception as e:
                logger.error(f"[FileTree] 删除失败: {path}: {e}")
                _styled_message_box(
                    self._colors,
                    QMessageBox.Critical,
                    "删除失败",
                    f"无法删除「{os.path.basename(path)}」:\n{e}",
                    QMessageBox.Ok,
                    QMessageBox.Ok,
                    parent_widget=self.window(),
                )

    # ── 拖拽移动/复制 ──

    def _on_move_requested(self, source_paths: List[str], dest_dir: str):
        names = "\n".join(os.path.basename(p) for p in source_paths)
        target_name = os.path.basename(dest_dir) or dest_dir
        reply = _styled_message_box(
            self._colors,
            QMessageBox.Question,
            "确认移动",
            f"确定要将以下项目移动到「{target_name}」？\n\n{names}",
            parent_widget=self.window(),
        )
        if reply != QMessageBox.Yes:
            return

        for src in source_paths:
            dst = os.path.join(dest_dir, os.path.basename(src))
            if os.path.exists(dst):
                logger.warning(f"[FileTree] 目标已存在，跳过: {dst}")
                continue
            try:
                shutil.move(src, dst)
                self._source_model.remove_node(src)
                logger.info(f"[FileTree] 移动: {src} → {dst}")
            except Exception as e:
                logger.error(f"[FileTree] 移动失败 {src}: {e}")
        self._refresh_dir_in_model(dest_dir)

    def _on_copy_drop_requested(self, source_paths: List[str], dest_dir: str):
        """拖拽复制 — 外部/内部统一确认"""
        names = "\n".join(os.path.basename(p) for p in source_paths)
        target_name = os.path.basename(dest_dir) or dest_dir
        reply = _styled_message_box(
            self._colors,
            QMessageBox.Question,
            "确认复制",
            f"确定要将以下项目复制到「{target_name}」？\n\n{names}",
            parent_widget=self.window(),
        )
        if reply != QMessageBox.Yes:
            return

        for src in source_paths:
            name = os.path.basename(src)
            dst = os.path.join(dest_dir, name)
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                logger.info(f"[FileTree] 拖拽复制: {src} → {dst}")
            except Exception as e:
                logger.error(f"[FileTree] 拖拽复制失败 {src}: {e}")
        self._refresh_dir_in_model(dest_dir)

    # ── 重命名 ──

    def _on_rename_file(self, old_path: str, new_name: str):
        if not os.path.exists(old_path):
            logger.warning(f"[FileTree] 重命名失败，路径不存在: {old_path}")
            return

        new_path = os.path.join(os.path.dirname(old_path), new_name)
        if os.path.exists(new_path):
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "重命名失败",
                f"「{new_name}」已存在",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        try:
            os.rename(old_path, new_path)
            result = self._source_model.rename_node(old_path, new_name)
            if result:
                logger.info(f"[FileTree] 重命名: {old_path} → {result}")
        except Exception as e:
            logger.error(f"[FileTree] 重命名失败: {e}")
            _styled_message_box(
                self._colors,
                QMessageBox.Critical,
                "重命名失败",
                str(e),
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )

    # ── 新建文件/文件夹 ──

    def _create_new_file(self, parent_path: str):
        name, ok = QInputDialog.getText(
            self.window(),
            "新建文件",
            "请输入文件名:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if "/" in name or "\\" in name:
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "无效名称",
                "文件名不能包含路径分隔符",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        new_path = os.path.join(parent_path, name)
        if os.path.exists(new_path):
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "创建失败",
                f"「{name}」已存在",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        try:
            Path(new_path).touch()
            logger.info(f"[FileTree] 新建文件: {new_path}")
            self._refresh_dir_in_model(parent_path)
        except Exception as e:
            logger.error(f"[FileTree] 新建文件失败: {e}")

    def _create_new_dir(self, parent_path: str):
        name, ok = QInputDialog.getText(
            self.window(),
            "新建文件夹",
            "请输入文件夹名:",
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if "/" in name or "\\" in name:
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "无效名称",
                "文件夹名不能包含路径分隔符",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        new_path = os.path.join(parent_path, name)
        if os.path.exists(new_path):
            _styled_message_box(
                self._colors,
                QMessageBox.Warning,
                "创建失败",
                f"「{name}」已存在",
                QMessageBox.Ok,
                QMessageBox.Ok,
                parent_widget=self.window(),
            )
            return

        try:
            os.mkdir(new_path)
            logger.info(f"[FileTree] 新建文件夹: {new_path}")
            self._refresh_dir_in_model(parent_path)
        except Exception as e:
            logger.error(f"[FileTree] 新建文件夹失败: {e}")

    def _refresh_dir_in_model(self, dir_path: str):
        """刷新模型中的单个目录（同步重扫）"""
        if not os.path.isdir(dir_path):
            return
        try:
            entries: List[_DirEntry] = []
            with os.scandir(dir_path) as it:
                from .scanner import _should_show

                for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
                    try:
                        if not _should_show(entry.name, entry.is_dir()):
                            continue
                        entries.append(_DirEntry(name=entry.name, path=entry.path, is_dir=entry.is_dir()))
                    except OSError:
                        continue
            self._source_model.refresh_children(dir_path, entries)
        except Exception as e:
            logger.error(f"[FileTree] 刷新目录失败: {e}")

    # ── 右键菜单 ──

    def _on_context_menu(self, src_idx: QModelIndex, global_pos):
        menu = QMenu(self)
        # 应用统一字体和主题样式
        ff = self._colors.get("font_family", "Microsoft YaHei")
        fs = self._colors.get("font_size", 14)
        bg = self._colors.get("card_bg", QColor(33, 33, 38))
        tc = self._colors.get("text", QColor(255, 255, 255))
        border = self._colors.get("border", QColor(255, 255, 255, 30))
        is_dark = self._colors.get("is_dark", True)
        hover_bg = bg.lighter(120) if is_dark else bg.darker(110)
        sep_color = border.name()

        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {bg.name()};
                border: 1px solid {sep_color};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 24px 6px 10px;
                color: {tc.name()};
                font-family: '{ff}';
                font-size: {fs - 2}px;
            }}
            QMenu::item:selected {{
                background-color: {hover_bg.name()};
                border-radius: 4px;
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {sep_color};
                margin: 4px 8px;
            }}
        """)

        # 获取右键目标路径
        if src_idx.isValid():
            item_path = src_idx.data(FileTreeModel.COL_PATH)
            item_name = src_idx.data(Qt.DisplayRole)
            is_dir = src_idx.data(FileTreeModel.COL_IS_DIR)
            parent_path = os.path.dirname(item_path) if not is_dir else item_path
        else:
            # 点击空白 → 在根目录操作
            item_path = self._project_root
            item_name = ""
            is_dir = True
            parent_path = self._project_root

        # 信息类
        if item_path:
            action_copy_path = menu.addAction("📋 复制路径")
            action_copy_path.triggered.connect(lambda: self._copy_to_clipboard(item_path))

        if item_name:
            action_copy_name = menu.addAction("📋 复制文件名")
            action_copy_name.triggered.connect(lambda: self._copy_to_clipboard(item_name))

        if item_path:
            menu.addSeparator()

        # 剪贴板操作
        has_selection = bool(self._tree_view.selected_source_paths())
        action_cut = menu.addAction("✂️  剪切")
        action_cut.setEnabled(has_selection)
        action_cut.triggered.connect(self.cut_to_clipboard)

        action_copy = menu.addAction("📄 复制")
        action_copy.setEnabled(has_selection)
        action_copy.triggered.connect(self.copy_to_clipboard)

        action_paste = menu.addAction("📌 粘贴")
        action_paste.setEnabled(bool(self._clipboard))
        action_paste.triggered.connect(self.paste_from_clipboard)

        menu.addSeparator()

        # 编辑操作
        if item_path:
            action_rename = menu.addAction("✏️  重命名")
            action_rename.triggered.connect(lambda: self._tree_view.start_rename_at_index(src_idx))

            action_delete = menu.addAction("🗑️  删除")
            action_delete.triggered.connect(lambda: self._on_delete_requested([item_path]))

        menu.addSeparator()

        # 新建（在目录内操作）
        action_new_file = menu.addAction("📄 新建文件...")
        action_new_file.triggered.connect(lambda: self._create_new_file(parent_path))

        action_new_dir = menu.addAction("📁 新建文件夹...")
        action_new_dir.triggered.connect(lambda: self._create_new_dir(parent_path))

        menu.addSeparator()

        # 刷新
        if item_path and is_dir:
            action_refresh = menu.addAction("🔄 刷新")
            action_refresh.triggered.connect(lambda: self._refresh_dir_in_model(item_path))

        menu.addSeparator()

        # 系统操作
        if item_path:
            if is_dir:
                action_explorer = menu.addAction("📂 在资源管理器中打开")
                action_explorer.triggered.connect(lambda: self._open_in_explorer(item_path))
            else:
                action_explorer = menu.addAction("📂 打开所在文件夹")
                action_explorer.triggered.connect(lambda: self._open_in_explorer(item_path))
                action_open = menu.addAction("📄 用默认程序打开")
                action_open.triggered.connect(lambda: self._open_file(item_path))

        menu.exec_(global_pos)

    # ── 刷新与关闭 ──

    def _on_refresh(self):
        self._watcher.clear()
        self._async_load_tree()

    def _on_close(self):
        self._cleanup_worker()
        self._watcher.clear()
        self.closed.emit()

    def deleteLater(self):
        self._cleanup_worker()
        self._watcher.clear()
        super().deleteLater()

    # ── 系统操作 ──

    @staticmethod
    def _copy_to_clipboard(text: str):
        QApplication.clipboard().setText(text)

    @staticmethod
    def _open_in_explorer(path: str):
        """在系统文件管理器中打开路径

        - 目录：直接打开并展示其内容（explorer /select 会打开父目录，
          导致层级往外多一层）
        - 文件：打开所在文件夹并选中该文件（/select 对文件行为正确）
        """
        norm = os.path.normpath(path)
        try:
            if os.name == "nt":
                if os.path.isdir(norm):
                    subprocess.Popen(["explorer", norm])
                else:
                    subprocess.Popen(["explorer", "/select,", norm])
            elif sys.platform == "darwin":
                if os.path.isdir(norm):
                    subprocess.Popen(["open", norm])
                else:
                    subprocess.Popen(["open", "-R", norm])
            else:
                if os.path.isdir(norm):
                    subprocess.Popen(["xdg-open", norm])
                else:
                    subprocess.Popen(["xdg-open", os.path.dirname(norm)])
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

    # ── 比例尺寸（按停靠方位自适应） ──

    def _in_horizontal_dock(self) -> bool:
        """是否停靠在左/右横向停靠区（容器沿宽度轴折叠）"""
        try:
            from app.widgets.cards.card_manager import ContainerType

            ct = getattr(self.parent(), "container_type", None)
            return ct in (ContainerType.LEFT, ContainerType.RIGHT)
        except Exception:
            return False

    def sizeHint(self):
        base = super().sizeHint()
        win = self.window()
        if self._in_horizontal_dock():
            # 左/右停靠：自然宽度 300px（展开动画目标），高度填满停靠区；
            # 展开后宽度由 dockSplitter 拖拽控制，此处仅提供初始值
            h = win.height() if win and win.height() > 0 else base.height()
            return QSize(300, h)
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
