# -*- coding: utf-8 -*-
"""文件树控件 — QTreeView + FileTreeModel + RenameDelegate + FilterProxy

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块

架构：
  FileTreeModel (QAbstractItemModel) — 懒加载数据模型
      ↓ need_scan 信号
  FileTreeFilterProxy (QSortFilterProxyModel) — 搜索过滤
      ↓
  FileTreeView (QTreeView) — 多选/拖拽/键盘
      ↓ delete/move/copy 信号
  cards.py 响应信号，处理实际操作
"""

import os
import shutil
import traceback
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from loguru import logger
from PyQt5.QtCore import (
    QAbstractItemModel,
    QFileInfo,
    QMimeData,
    QModelIndex,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QUrl,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QIcon, QKeyEvent, QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileIconProvider,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, MaskDialogBase, isDarkTheme

# ══════════════════════════════════════════════════════════
# 文件图标工具
# ══════════════════════════════════════════════════════════

_FILE_ICON_PROVIDER: Optional[QFileIconProvider] = None


def _get_file_icon(file_path: str) -> QIcon:
    global _FILE_ICON_PROVIDER
    if _FILE_ICON_PROVIDER is None:
        _FILE_ICON_PROVIDER = QFileIconProvider()
    info = QFileInfo(file_path)
    icon = _FILE_ICON_PROVIDER.icon(info)
    if icon and not icon.isNull():
        return icon
    return QApplication.style().standardIcon(QStyle.SP_FileIcon)


def _get_dir_icon() -> QIcon:
    global _FILE_ICON_PROVIDER
    if _FILE_ICON_PROVIDER is None:
        _FILE_ICON_PROVIDER = QFileIconProvider()
    icon = _FILE_ICON_PROVIDER.icon(QFileIconProvider.Folder)
    if icon and not icon.isNull():
        return icon
    return QApplication.style().standardIcon(QStyle.SP_DirIcon)


# ══════════════════════════════════════════════════════════
# 数据节点
# ══════════════════════════════════════════════════════════


@dataclass
class _FileNode:
    """文件树数据节点"""

    name: str
    path: str
    is_dir: bool
    children: Optional[List["_FileNode"]] = None
    loaded: bool = False
    parent_node: Optional["_FileNode"] = None


# ══════════════════════════════════════════════════════════
# FileTreeModel — 数据模型
# ══════════════════════════════════════════════════════════


class FileTreeModel(QAbstractItemModel):
    """自定义文件树 Model — 懒加载 + 排序（文件夹优先）"""

    need_scan = pyqtSignal(str)  # 请求异步扫描 dir_path

    COL_PATH = Qt.UserRole
    COL_IS_DIR = Qt.UserRole + 1

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._root_entries: List[_FileNode] = []
        self._project_root: str = ""
        self._loading: set = set()  # 正在扫描中的目录路径

    # ── 公开 API（cards.py 调用） ──

    def set_project(self, project_root: str, entries):
        """设置项目根目录并加载顶级条目（scanner 结果）"""
        self.beginResetModel()
        self._project_root = os.path.normpath(project_root)
        self._root_entries = self._build_nodes(entries, None)
        self.endResetModel()

    def populate_children(self, dir_path: str, entries) -> bool:
        """扫描完成后填充子节点"""
        node = self._find_node(dir_path)
        if not node:
            self._loading.discard(dir_path)
            return False
        parent_idx = self._index_for_node(node)
        if not parent_idx.isValid() and node.parent_node is not None:
            self._loading.discard(dir_path)
            return False
        if node.loaded:
            self._clear_children(node, parent_idx)
        self._insert_children(node, parent_idx, entries)
        node.loaded = True
        self._loading.discard(dir_path)
        return True

    def refresh_children(self, dir_path: str, entries):
        """外部变更后刷新子节点"""
        node = self._find_node(dir_path)
        if not node or not node.loaded:
            return
        parent_idx = self._index_for_node(node)
        self._clear_children(node, parent_idx)
        self._insert_children(node, parent_idx, entries)

    def refresh_root(self, entries):
        """外部变更后刷新根目录顶级条目（根目录自身无节点，单独处理）"""
        self.beginResetModel()
        self._root_entries = self._build_nodes(entries, None)
        self.endResetModel()

    def rename_node(self, old_path: str, new_name: str) -> Optional[str]:
        """重命名节点，返回新路径；失败返回 None"""
        node = self._find_node(old_path)
        if not node:
            return None
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        node.name = new_name
        node.path = new_path
        idx = self._index_for_node(node)
        if idx.isValid():
            self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.ToolTipRole])
        return new_path

    def remove_node(self, path: str):
        """从模型移除节点"""
        node = self._find_node(path)
        if not node:
            return
        parent = node.parent_node
        if parent is None:
            # 顶级
            row = self._find_row_in_list(self._root_entries, node)
            if row >= 0:
                self.beginRemoveRows(QModelIndex(), row, row)
                self._root_entries.pop(row)
                self.endRemoveRows()
        elif parent.children is not None:
            parent_idx = self._index_for_node(parent)
            row = self._find_row_in_list(parent.children, node)
            if row >= 0:
                self.beginRemoveRows(parent_idx, row, row)
                parent.children.pop(row)
                self.endRemoveRows()

    def add_node(self, parent_path: str, entry) -> Optional[QModelIndex]:
        """添加新子节点（按排序位置插入），返回 index"""
        parent_node = self._find_node(parent_path)
        if not parent_node:
            return None
        if parent_node.children is None:
            parent_node.children = []
            parent_node.loaded = True

        parent_idx = self._index_for_node(parent_node)
        insert_row = self._sorted_insert_pos(parent_node.children, entry)
        self.beginInsertRows(parent_idx, insert_row, insert_row)
        new_node = _FileNode(
            name=entry.name,
            path=entry.path,
            is_dir=entry.is_dir,
            parent_node=parent_node,
            loaded=False,
        )
        parent_node.children.insert(insert_row, new_node)
        self.endInsertRows()
        return self._index_for_node(new_node)

    def find_node(self, path: str) -> Optional[_FileNode]:
        return self._find_node(path)

    def node_is_loading(self, path: str) -> bool:
        """检查某路径是否正在异步扫描中"""
        return os.path.normpath(path) in self._loading

    def clear_loading(self, path: str):
        """扫描失败时清除加载标记"""
        self._loading.discard(os.path.normpath(path))

    def node_is_loaded(self, idx: QModelIndex) -> bool:
        if not idx.isValid():
            return False
        node: _FileNode = idx.internalPointer()
        return node.loaded

    # ── QAbstractItemModel 接口 ──

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            if row < len(self._root_entries):
                return self.createIndex(row, column, self._root_entries[row])
        else:
            parent_node: _FileNode = parent.internalPointer()
            if parent_node.children is not None and row < len(parent_node.children):
                return self.createIndex(row, column, parent_node.children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node: _FileNode = index.internalPointer()
        if node.parent_node is None:
            return QModelIndex()
        return self._index_for_node(node.parent_node)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if not parent.isValid():
            return len(self._root_entries)
        node: _FileNode = parent.internalPointer()
        if node.children is not None:
            return len(node.children)
        return 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        """告知 Qt 该节点是否有子节点（未加载的目录假设有）"""
        if not parent.isValid():
            return len(self._root_entries) > 0
        node: _FileNode = parent.internalPointer()
        if not node.is_dir:
            return False
        if node.loaded:
            return bool(node.children)
        return True  # 未加载的目录假设有子节点

    def canFetchMore(self, parent: QModelIndex) -> bool:
        """Qt 在展开节点前调用 — 未加载且未在扫描中才返回 True"""
        if not parent.isValid():
            return False
        node: _FileNode = parent.internalPointer()
        if not node.is_dir:
            return False
        if node.loaded:
            return False
        if node.path in self._loading:
            return False
        return True

    def fetchMore(self, parent: QModelIndex):
        """Qt 自动调用 — 触发异步扫描"""
        node: _FileNode = parent.internalPointer()
        if not node or not node.is_dir:
            return
        self._loading.add(node.path)
        self.need_scan.emit(node.path)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        node: _FileNode = index.internalPointer()
        if role == Qt.DisplayRole:
            return node.name
        if role == Qt.DecorationRole:
            return _get_dir_icon() if node.is_dir else _get_file_icon(node.path)
        if role == self.COL_PATH:
            return node.path
        if role == self.COL_IS_DIR:
            return node.is_dir
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        node: _FileNode = index.internalPointer()
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
        if node.is_dir:
            flags |= Qt.ItemIsDropEnabled
        return flags

    def supportedDropActions(self) -> Qt.DropActions:
        return Qt.MoveAction | Qt.CopyAction

    def mimeTypes(self) -> List[str]:
        return ["text/uri-list", "application/x-drifox-filelist"]

    def mimeData(self, indexes: List[QModelIndex]) -> QMimeData:
        mime_data = QMimeData()
        paths = [idx.data(self.COL_PATH) for idx in indexes if idx.isValid()]
        if paths:
            mime_data.setUrls([QUrl.fromLocalFile(p) for p in paths])
            mime_data.setData("application/x-drifox-filelist", "\n".join(paths).encode("utf-8"))
        return mime_data

    def canDropMimeData(self, data, action, row, column, parent):
        if not parent.isValid():
            return False
        node: _FileNode = parent.internalPointer()
        return node.is_dir

    def dropMimeData(self, data, action, row, column, parent):
        # 实际操作由 FileTreeView.dropEvent 处理
        return False

    # ── 内部辅助 ──

    def _build_nodes(self, entries, parent) -> List[_FileNode]:
        nodes = []
        for e in entries:
            nodes.append(
                _FileNode(
                    name=e.name,
                    path=e.path,
                    is_dir=e.is_dir,
                    parent_node=parent,
                    loaded=False,
                )
            )
        return nodes

    def _insert_children(self, node: _FileNode, parent_idx: QModelIndex, entries):
        children = self._build_nodes(entries, node)
        if not children:
            node.children = []
            return
        self.beginInsertRows(parent_idx, 0, len(children) - 1)
        node.children = children
        self.endInsertRows()

    def _clear_children(self, node: _FileNode, parent_idx: QModelIndex):
        if node.children:
            count = len(node.children)
            self.beginRemoveRows(parent_idx, 0, count - 1)
            node.children = None
            self.endRemoveRows()

    def _find_node(self, norm_path: str) -> Optional[_FileNode]:
        target = os.path.normpath(norm_path)
        for entry in self._root_entries:
            result = self._find_node_recursive(entry, target)
            if result:
                return result
        return None

    def _find_node_recursive(self, node: _FileNode, target: str) -> Optional[_FileNode]:
        if os.path.normpath(node.path) == target:
            return node
        if node.children:
            for child in node.children:
                result = self._find_node_recursive(child, target)
                if result:
                    return result
        return None

    def _index_for_node(self, node: _FileNode) -> QModelIndex:
        if node is None:
            return QModelIndex()
        if node.parent_node is None:
            row = self._find_row_in_list(self._root_entries, node)
            return self.createIndex(row, 0, node) if row >= 0 else QModelIndex()
        parent_idx = self._index_for_node(node.parent_node)
        if not parent_idx.isValid():
            return QModelIndex()
        if node.parent_node.children is None:
            return QModelIndex()
        row = self._find_row_in_list(node.parent_node.children, node)
        return self.createIndex(row, 0, node) if row >= 0 else QModelIndex()

    @staticmethod
    def _find_row_in_list(lst: List[_FileNode], node: _FileNode) -> int:
        for i, item in enumerate(lst):
            if item is node:
                return i
        return -1

    @staticmethod
    def _sorted_insert_pos(children: List[_FileNode], entry) -> int:
        """找到排序插入位置：文件夹在前，按名称小写字母序"""
        for i, child in enumerate(children):
            if child.is_dir and not entry.is_dir:
                continue
            if not child.is_dir and entry.is_dir:
                return i
            if entry.name.lower() < child.name.lower():
                return i
        return len(children)


# ══════════════════════════════════════════════════════════
# FileTreeFilterProxy — 搜索过滤
# ══════════════════════════════════════════════════════════


class FileTreeFilterProxy(QSortFilterProxyModel):
    """文件树搜索过滤代理 — 仅根据文件名过滤"""

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterRole(Qt.DisplayRole)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, FileTreeModel):
            return super().filterAcceptsRow(source_row, source_parent)

        pattern = self.filterRegularExpression().pattern()
        if not pattern:
            return True

        idx = model.index(source_row, 0, source_parent)
        name = model.data(idx, Qt.DisplayRole) or ""
        is_dir = model.data(idx, model.COL_IS_DIR)

        if pattern.lower() in name.lower():
            return True

        # 未加载的目录 — 乐观显示（可能子节点会匹配）
        if is_dir:
            node = model.find_node(model.data(idx, model.COL_PATH))
            if node and node.children is None:
                return True

        # 已加载的依赖 recursiveFilteringEnabled 检查子节点
        return False


# ══════════════════════════════════════════════════════════
# RenameDelegate — 原地重命名
# ══════════════════════════════════════════════════════════


class RenameDelegate(QStyledItemDelegate):
    """文件/文件夹原地重命名编辑器"""

    rename_requested = pyqtSignal(str, str)  # old_path, new_name

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._colors: dict = {}

    def set_colors(self, colors: dict):
        self._colors = colors

    def createEditor(self, parent_widget, option, index):
        editor = QLineEdit(parent_widget)
        accent = self._colors.get("accent", QColor("#62a0ea"))
        tc = self._colors.get("text", QColor(255, 255, 255))
        ff = self._colors.get("font_family", "Microsoft YaHei")
        fs = self._colors.get("font_size", 14)

        editor.setStyleSheet(
            f"QLineEdit {{"
            f"  background: rgba(255,255,255,0.08);"
            f"  border: 2px solid {accent.name()};"
            f"  border-radius: 3px;"
            f"  padding: 2px 6px;"
            f"  color: {tc.name()};"
            f"  font-family: '{ff}';"
            f"  font-size: {fs}px;"
            f"}}"
        )
        return editor

    def setEditorData(self, editor, index):
        name = index.data(Qt.DisplayRole) or ""
        editor.setText(name)
        is_dir = index.data(FileTreeModel.COL_IS_DIR)
        if not is_dir:
            dot = name.rfind(".")
            if dot > 0:
                editor.setSelection(0, dot)
            else:
                editor.selectAll()
        else:
            editor.selectAll()

    def setModelData(self, editor, model, index):
        new_name = editor.text().strip()
        old_name = index.data(Qt.DisplayRole)
        if not new_name or new_name == old_name:
            return
        if "/" in new_name or "\\" in new_name:
            logger.warning("[FileTree] 文件名含非法字符，取消重命名")
            return
        old_path = index.data(FileTreeModel.COL_PATH)
        self.rename_requested.emit(old_path, new_name)


# ══════════════════════════════════════════════════════════
# FileTreeView — 视图控件
# ══════════════════════════════════════════════════════════


class FileTreeView(QTreeView):
    """多选文件树视图 — 键盘/拖拽/右键"""

    # 请求卡片处理实际操作
    delete_requested = pyqtSignal(list)  # List[str] paths
    move_requested = pyqtSignal(list, str)  # paths, dest_dir
    copy_requested = pyqtSignal(list, str)  # paths, dest_dir
    context_menu_requested = pyqtSignal(QModelIndex, object)  # index, global_pos
    double_clicked_file = pyqtSignal(str)  # file_path
    enter_expand_requested = pyqtSignal(str)  # dir_path (Enter 键展开)
    clipboard_copy = pyqtSignal()  # Ctrl+C
    clipboard_cut = pyqtSignal()  # Ctrl+X
    clipboard_paste = pyqtSignal()  # Ctrl+V

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAnimated(True)
        self.setIndentation(20)
        self.setHeaderHidden(True)
        self.setIconSize(QSize(18, 18))
        self.setFrameShape(QFrame.NoFrame)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setExpandsOnDoubleClick(False)
        self._drag_action: Optional[Qt.DropAction] = None
        self.project_root: str = ""

    # ── 键盘 ──

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Delete:
            self._emit_delete()
        elif event.key() == Qt.Key_F2:
            self._start_rename()
        elif event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            self._handle_enter()
        elif event.matches(QKeySequence.Copy):
            self.clipboard_copy.emit()
        elif event.matches(QKeySequence.Cut):
            self.clipboard_cut.emit()
        elif event.matches(QKeySequence.Paste):
            self.clipboard_paste.emit()
        elif event.matches(QKeySequence.SelectAll):
            self.selectAll()
        else:
            super().keyPressEvent(event)

    # ── 双击 ──

    def mouseDoubleClickEvent(self, event):
        idx = self.indexAt(event.pos())
        if not idx.isValid():
            super().mouseDoubleClickEvent(event)
            return
        src_idx = self._map_to_source(idx)
        is_dir = src_idx.data(FileTreeModel.COL_IS_DIR)
        if is_dir:
            # 双击目录：展开/折叠
            self.setExpanded(idx, not self.isExpanded(idx))
        else:
            path = src_idx.data(FileTreeModel.COL_PATH)
            if path:
                self.double_clicked_file.emit(path)
        # 不调用 super，避免 QTreeView 默认行为

    # ── 右键菜单 ──

    def contextMenuEvent(self, event):
        idx = self.indexAt(event.pos())
        src_idx = self._map_to_source(idx) if idx.isValid() else QModelIndex()
        self.context_menu_requested.emit(src_idx, event.globalPos())

    # ── 拖拽 ──

    def dragEnterEvent(self, event):
        if event.source() is self:
            event.acceptProposedAction()
            super().dragEnterEvent(event)
        elif event.mimeData().hasUrls():
            # 直接 accept，不走 super → super 会咨询 canDropMimeData，
            # 空白区域返回 False 导致事件被吞
            event.accept()
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        # ── 外部拖拽（系统文件管理器） ──
        if event.source() is not self:
            if not event.mimeData().hasUrls():
                event.ignore()
                return
            target_idx = self.indexAt(event.pos())
            if target_idx.isValid():
                # 有效节点（文件/目录）→ 让 QTreeView 显示高亮或行间指示器
                super().dragMoveEvent(event)
                return
            # 空白区域 → 有根目录才接受（不能走 super，理由同上）
            if not self.project_root or not os.path.isdir(self.project_root):
                event.ignore()
                return
            event.accept()
            return

        # 根据键盘修饰符切换移动/复制
        if event.keyboardModifiers() & Qt.ControlModifier:
            self._drag_action = Qt.CopyAction
            event.setDropAction(Qt.CopyAction)
        else:
            self._drag_action = Qt.MoveAction
            event.setDropAction(Qt.MoveAction)

        super().dragMoveEvent(event)

    def dropEvent(self, event):
        # ── 外部拖拽（系统文件管理器） ──
        if event.source() is not self:
            if not event.mimeData().hasUrls():
                event.ignore()
                return

            # 从 URLs 提取本地文件路径
            source_paths = []
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    file_path = url.toLocalFile()
                    if os.path.exists(file_path):
                        source_paths.append(file_path)

            if not source_paths:
                event.ignore()
                return

            # 确定目标目录
            target_idx = self.indexAt(event.pos())
            if target_idx.isValid():
                target_src = self._map_to_source(target_idx)
                target_path = target_src.data(FileTreeModel.COL_PATH)
                target_is_dir = target_src.data(FileTreeModel.COL_IS_DIR)
                if target_path:
                    dest_dir = target_path if target_is_dir else os.path.dirname(target_path)
                else:
                    dest_dir = ""
            else:
                # 空白区域 → 回退到项目根目录
                dest_dir = self.project_root

            if not dest_dir or not os.path.isdir(dest_dir):
                event.ignore()
                return

            # 防拖拽到自身
            for src in source_paths:
                if os.path.normpath(src) == os.path.normpath(dest_dir):
                    logger.warning(f"[FileTree] 禁止拖拽到自身: {src}")
                    event.ignore()
                    return

            action = event.dropAction()
            if action == Qt.CopyAction:
                self.copy_requested.emit(source_paths, dest_dir)
            else:
                self.move_requested.emit(source_paths, dest_dir)

            event.acceptProposedAction()
            return

        # ── 内部拖拽（树内节点移动/复制） ──
        action = self._drag_action or event.dropAction()
        self._drag_action = None

        target_idx = self.indexAt(event.pos())
        if not target_idx.isValid():
            event.ignore()
            return
        target_src = self._map_to_source(target_idx)
        target_path = target_src.data(FileTreeModel.COL_PATH)
        target_is_dir = target_src.data(FileTreeModel.COL_IS_DIR)
        if not target_path:
            event.ignore()
            return

        dest_dir = target_path if target_is_dir else os.path.dirname(target_path)
        if not dest_dir:
            event.ignore()
            return

        selected_idxs = self.selectionModel().selectedRows()
        source_paths: List[str] = []
        for idx in selected_idxs:
            src_idx = self._map_to_source(idx)
            p = src_idx.data(FileTreeModel.COL_PATH)
            if not p:
                continue
            norm_src = os.path.normpath(p)
            norm_dst = os.path.normpath(dest_dir)
            if norm_src == norm_dst:
                continue
            if norm_dst.startswith(norm_src + os.sep):
                logger.warning(f"[FileTree] 禁止循环拖拽: {p} → {dest_dir}")
                continue
            source_paths.append(p)

        if not source_paths:
            event.ignore()
            return

        if action == Qt.CopyAction:
            self.copy_requested.emit(source_paths, dest_dir)
        else:
            self.move_requested.emit(source_paths, dest_dir)

        event.acceptProposedAction()

    # ── 重命名 ──

    def start_rename_at_index(self, src_idx: QModelIndex):
        """外部触发重命名（右键菜单或 F2）"""
        if not src_idx.isValid():
            return
        proxy_idx = self._map_from_source(src_idx)
        if proxy_idx.isValid():
            self.edit(proxy_idx)

    def _start_rename(self):
        idxs = self.selectionModel().selectedRows()
        if not idxs:
            return
        self.edit(idxs[0])

    # ── Delete ──

    def _emit_delete(self):
        idxs = self.selectionModel().selectedRows()
        paths = []
        for idx in idxs:
            src_idx = self._map_to_source(idx)
            p = src_idx.data(FileTreeModel.COL_PATH)
            if p:
                paths.append(p)
        if paths:
            self.delete_requested.emit(paths)

    # ── Enter ──

    def _handle_enter(self):
        idxs = self.selectionModel().selectedRows()
        if not idxs:
            return
        src_idx = self._map_to_source(idxs[0])
        is_dir = src_idx.data(FileTreeModel.COL_IS_DIR)
        path = src_idx.data(FileTreeModel.COL_PATH)
        if not path:
            return
        if is_dir:
            self.enter_expand_requested.emit(path)
        else:
            self.double_clicked_file.emit(path)

    # ── 索引映射（source ↔ proxy） ──

    def _map_to_source(self, proxy_idx: QModelIndex) -> QModelIndex:
        p = self.model()
        if isinstance(p, QSortFilterProxyModel):
            return p.mapToSource(proxy_idx)
        return proxy_idx

    def _map_from_source(self, src_idx: QModelIndex) -> QModelIndex:
        p = self.model()
        if isinstance(p, QSortFilterProxyModel):
            return p.mapFromSource(src_idx)
        return src_idx

    def selected_source_paths(self) -> List[str]:
        """获取选中项路径（直接查 source model）"""
        paths = []
        for idx in self.selectionModel().selectedRows():
            src_idx = self._map_to_source(idx)
            p = src_idx.data(FileTreeModel.COL_PATH)
            if p:
                paths.append(p)
        return paths
