# -*- coding: utf-8 -*-
"""可拖拽文件树控件 — FileTreeWidget

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
"""

import os
import shutil
import traceback
from typing import List, Optional

from PyQt5.QtCore import QEvent, QFileInfo, QMimeData, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QIcon
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileIconProvider,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, MaskDialogBase, isDarkTheme
from loguru import logger


# ── 文件类型图标缓存 ─────────────────────────────────────

_FILE_ICON_PROVIDER = None


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


def _get_dir_open_icon() -> QIcon:
    global _FILE_ICON_PROVIDER
    if _FILE_ICON_PROVIDER is None:
        _FILE_ICON_PROVIDER = QFileIconProvider()

    icon = _FILE_ICON_PROVIDER.icon(QFileIconProvider.Folder)
    if icon and not icon.isNull():
        return icon
    return QApplication.style().standardIcon(QStyle.SP_DirOpenIcon)


# ══════════════════════════════════════════════════════════
# 可拖拽文件树控件
# ══════════════════════════════════════════════════════════


class FileTreeWidget(QTreeWidget):
    """支持拖拽和删除的文件树控件"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self._tree_card: Optional[QWidget] = None
        self._drag_hover_item: Optional[QTreeWidgetItem] = None
        logger.debug("[FileTreeWidget] 已创建，拖拽模式=DragDrop")

    def set_tree_card(self, card):
        """设置所属的 FileTreeCard 引用"""
        self._tree_card = card

    # ── 拖拽数据（拖到外部） ──

    def mimeData(self, items: List[QTreeWidgetItem]) -> QMimeData:
        mime_data = super().mimeData(items)
        paths: List[str] = []
        for item in items:
            path = item.data(0, Qt.UserRole)
            if path:
                paths.append(path)

        if not paths:
            return mime_data

        urls = [QUrl.fromLocalFile(p) for p in paths]
        mime_data.setUrls(urls)
        mime_data.setText("\n".join(paths))
        return mime_data

    # ── 内部拖放移动 ──

    def dragEnterEvent(self, event):
        if event.source() is self:
            super().dragEnterEvent(event)
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return

        self._clear_drag_highlight()
        super().dragMoveEvent(event)

        target_item = self.itemAt(event.pos())
        if target_item is None:
            return

        target_is_dir = target_item.data(0, Qt.UserRole + 1)
        if target_is_dir:
            accent = self._theme_accent_color()
            accent.setAlpha(40)
            target_item.setBackground(0, accent)
            self._drag_hover_item = target_item

    def dragLeaveEvent(self, event):
        self._clear_drag_highlight()
        super().dragLeaveEvent(event)

    def _clear_drag_highlight(self):
        if self._drag_hover_item is not None:
            try:
                self._drag_hover_item.setBackground(0, QColor())
            except RuntimeError:
                pass
            self._drag_hover_item = None

    def _theme_accent_color(self) -> QColor:
        if self._tree_card is not None:
            colors = getattr(self._tree_card, "_colors", {})
            if colors:
                return colors.get("accent", QColor("#62a0ea"))
        return QColor("#62a0ea" if isDarkTheme() else "#2878dc")

    def _styled_message_box(
        self,
        icon: QMessageBox.Icon,
        title: str,
        text: str,
        buttons: QMessageBox.StandardButtons = QMessageBox.Yes | QMessageBox.No,
        default_button: QMessageBox.StandardButton = QMessageBox.No,
    ) -> int:
        """创建适配主题色的消息框 — 统一 MaskDialogBase 风格"""
        # 从 card 上下文获取主题色
        if self._tree_card is not None:
            colors = getattr(self._tree_card, "_colors", {})
            if colors:
                bg = colors.get("card_bg", QColor(33, 33, 38))
                tc = colors.get("text", QColor(255, 255, 255))
                accent = colors.get("accent", QColor(102, 198, 255))
                border = colors.get("border", QColor(61, 61, 61))
                font_size = colors.get("font_size", 14)
                ff = colors.get("font_family", "Microsoft YaHei")
                is_dark = colors.get("is_dark", True)
            else:
                bg = QColor(33, 33, 38)
                tc = QColor(255, 255, 255)
                accent = QColor(102, 198, 255)
                border = QColor(61, 61, 61)
                font_size = 14
                ff = "Microsoft YaHei"
                is_dark = True
        else:
            bg = QColor(33, 33, 38)
            tc = QColor(255, 255, 255)
            accent = QColor(102, 198, 255)
            border = QColor(61, 61, 61)
            font_size = 14
            ff = "Microsoft YaHei"
            is_dark = True

        hover_bg = bg.lighter(115) if is_dark else bg.darker(110)

        has_yes = bool(buttons & QMessageBox.Yes)
        has_no = bool(buttons & QMessageBox.No)
        has_ok = bool(buttons & QMessageBox.Ok)
        has_cancel = bool(buttons & QMessageBox.Cancel)

        class _Dialog(MaskDialogBase):
            def __init__(self, parent_widget):
                super().__init__(parent_widget)
                self._result = QMessageBox.No
                self._setup()

            def _setup(self):
                self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
                self.setClosableOnMaskClicked(True)
                self.setDraggable(True)
                self.setMaskColor(QColor(0, 0, 0, 76))

                self.widget.setObjectName("fileTreeStyledDialog")
                self.widget.setStyleSheet(f"""
                    #fileTreeStyledDialog {{
                        background-color: {bg.name()};
                        border: 1px solid {border.name()};
                        border-radius: 8px;
                    }}
                """)

                layout = QVBoxLayout(self.widget)
                layout.setContentsMargins(28, 28, 28, 20)
                layout.setSpacing(0)

                title_lb = BodyLabel(title, self.widget)
                title_lb.setWordWrap(True)
                title_lb.setStyleSheet(
                    f"color: {tc.name()}; background: transparent; "
                    f"font-family: '{ff}';"
                    f"font-size: {font_size + 2}px; font-weight: bold;"
                )
                layout.addWidget(title_lb)
                layout.addSpacing(6)

                content_lb = BodyLabel(text, self.widget)
                content_lb.setWordWrap(True)
                content_lb.setStyleSheet(
                    f"color: {tc.name()}; background: transparent; "
                    f"font-family: '{ff}';"
                    f"font-size: {font_size - 1}px; line-height: 1.6;"
                )
                layout.addWidget(content_lb)
                layout.addStretch()

                btn_layout = QHBoxLayout()
                btn_layout.setSpacing(10)

                def _make_btn(label_text: str, result_code, is_default: bool, is_primary: bool):
                    btn = QPushButton(label_text, self.widget)
                    btn.setCursor(Qt.PointingHandCursor)
                    btn.setFixedHeight(36)
                    if is_primary:
                        btn.setStyleSheet(f"""
                            QPushButton {{
                                background-color: {accent.name()};
                                color: #ffffff;
                                border: none;
                                border-radius: 8px;
                                padding: 4px 28px;
                                font-family: '{ff}';
                                font-size: {font_size - 1}px;
                                font-weight: bold;
                            }}
                            QPushButton:hover {{
                                background-color: {accent.name()};
                            }}
                        """)
                    else:
                        btn.setStyleSheet(f"""
                            QPushButton {{
                                background-color: {bg.name()};
                                color: {tc.name()};
                                border: 1px solid {border.name()};
                                border-radius: 8px;
                                padding: 4px 28px;
                                font-family: '{ff}';
                                font-size: {font_size - 1}px;
                            }}
                            QPushButton:hover {{
                                background-color: {hover_bg.name()};
                                border-color: {accent.name()};
                            }}
                        """)
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
                self.widget.setFixedSize(400, 200)

        dialog = _Dialog(self.window())
        dialog.exec_()
        return dialog._result

    def dropEvent(self, event):
        self._clear_drag_highlight()

        if event.source() is not self:
            event.ignore()
            return

        target_item = self.itemAt(event.pos())
        if target_item is None:
            event.ignore()
            return

        target_path = target_item.data(0, Qt.UserRole)
        target_is_dir = target_item.data(0, Qt.UserRole + 1)

        if not target_path:
            event.ignore()
            return

        if not target_is_dir:
            target_path = os.path.dirname(target_path)
            if not target_path:
                event.ignore()
                return

        source_items = self.selectedItems()
        if not source_items:
            event.ignore()
            return

        source_paths: List[str] = []
        for item in source_items:
            path = item.data(0, Qt.UserRole)
            if not path:
                continue
            norm_src = os.path.normpath(path)
            norm_dst = os.path.normpath(target_path)
            if norm_src == norm_dst:
                continue
            if norm_dst.startswith(norm_src + os.sep):
                logger.warning(f"[FileTree] 循环移动被阻止: {path} → {target_path}")
                continue
            source_paths.append(path)

        if not source_paths:
            event.ignore()
            return

        names = "\n".join(os.path.basename(p) for p in source_paths)
        target_name = os.path.basename(target_path) or target_path
        reply = self._styled_message_box(
            QMessageBox.Question,
            "确认移动",
            f"确定要将以下项目移动到「{target_name}」？\n\n{names}",
        )
        if reply != QMessageBox.Yes:
            event.ignore()
            return

        self._move_files(source_paths, target_path)
        event.acceptProposedAction()

    def _move_files(self, source_paths: List[str], target_dir: str):
        moved_count = 0
        for src in source_paths:
            dest = os.path.join(target_dir, os.path.basename(src))
            if os.path.exists(dest):
                logger.warning(f"[FileTree] 目标已存在，跳过: {dest}")
                continue
            try:
                shutil.move(src, dest)
                moved_count += 1
                logger.info(f"[FileTree] 已移动: {src} → {dest}")
            except Exception as e:
                logger.error(f"[FileTree] 移动失败: {src} → {dest}: {e}")
                self._styled_message_box(
                    QMessageBox.Critical,
                    "移动失败",
                    f"无法移动「{os.path.basename(src)}」:\n{e}",
                    QMessageBox.Ok,
                    QMessageBox.Ok,
                )

        if moved_count > 0 and self._tree_card is not None:
            self._tree_card._on_refresh()

    # ── Delete 键删除 ──

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self._delete_selected()
        else:
            super().keyPressEvent(event)

    def _delete_selected(self):
        items = self.selectedItems()
        if not items:
            return

        deletable: List[QTreeWidgetItem] = []
        for item in items:
            path = item.data(0, Qt.UserRole)
            if not path:
                continue
            if (
                self._tree_card is not None
                and getattr(self._tree_card, "_project_root", None)
                and os.path.normpath(path) == os.path.normpath(self._tree_card._project_root)
            ):
                continue
            deletable.append(item)

        if not deletable:
            self._styled_message_box(
                QMessageBox.Information,
                "提示",
                "不能删除项目根目录",
                QMessageBox.Ok,
                QMessageBox.Ok,
            )
            return

        if len(deletable) == 1:
            name = deletable[0].text(0)
            path = deletable[0].data(0, Qt.UserRole)
            msg = f"确定要永久删除「{name}」？\n\n路径: {path}\n\n⚠️ 此操作不可撤销！"
        else:
            names = "\n".join(f"• {item.text(0)}" for item in deletable)
            msg = f"确定要永久删除以下 {len(deletable)} 个项目？\n\n{names}\n\n⚠️ 此操作不可撤销！"

        reply = self._styled_message_box(QMessageBox.Warning, "确认永久删除", msg)
        if reply != QMessageBox.Yes:
            return

        deleted_count = 0
        for item in deletable:
            path = item.data(0, Qt.UserRole)
            if not path:
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                deleted_count += 1
                logger.info(f"[FileTree] 已删除: {path}")
            except Exception as e:
                logger.error(f"[FileTree] 删除失败: {path}: {e}")
                self._styled_message_box(
                    QMessageBox.Critical,
                    "删除失败",
                    f"无法删除「{item.text(0)}」:\n{e}",
                    QMessageBox.Ok,
                    QMessageBox.Ok,
                )

        if deleted_count > 0 and self._tree_card is not None:
            self._tree_card._on_refresh()
