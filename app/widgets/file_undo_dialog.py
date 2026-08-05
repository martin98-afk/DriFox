# -*- coding: utf-8 -*-
"""
文件撤销预览对话框
"""

from pathlib import Path
from typing import Dict, List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    PrimaryPushButton,
    PushButton,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)

from app.utils.design_tokens import scale_font_size
from app.utils.utils import get_font_family_css, get_icon


class FileUndoPreviewDialog(QDialog):
    """文件撤销预览对话框"""

    # 结果类型
    CANCEL = 0      # 取消撤销
    KEEP_CARD = 1   # 不还原文件（卡片也撤销）
    RESTORE = 2     # 还原所选文件

    def __init__(self, operations: List[Dict], file_recorder=None, parent=None):
        super().__init__(parent)
        self.operations = operations
        self.file_recorder = file_recorder
        self._selected_set = set(range(len(operations)))  # 默认全选
        self._result = self.CANCEL
        self._init_ui()

    def _init_ui(self):
        dark = isDarkTheme()
        bg_color = "#1e1e1e" if dark else "#ffffff"
        text_color = "#e0e0e0" if dark else "#333333"
        border_color = "#3a3a3a" if dark else "#d0d0d0"

        self.setWindowTitle("还原文件")
        self.setMinimumSize(800, 600)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg_color};
                color: {text_color};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # 标题
        title_label = QLabel("确认撤销以下文件操作？", self)
        title_label.setStyleSheet(f"{get_font_family_css()} font-size: {scale_font_size(16)}px; font-weight: bold; color: {text_color};")
        layout.addWidget(title_label)

        # 说明
        desc_label = QLabel(
            "点击「确认撤销」将恢复这些文件到操作前的状态。\n"
            "此操作不可撤销。",
            self
        )
        desc_label.setStyleSheet(f"color: #8c99ad; {get_font_family_css()} font-size: {scale_font_size(13)}px;")
        layout.addWidget(desc_label)

        # 文件列表
        list_label = QLabel(f"共 {len(self.operations)} 个文件将被回滚：", self)
        list_label.setStyleSheet(f"font-weight: bold; color: {text_color};")
        layout.addWidget(list_label)

        # 全选
        select_layout = QHBoxLayout()
        self.select_all_cb = QCheckBox("全选", self)
        self.select_all_cb.setChecked(True)
        self.select_all_cb.stateChanged.connect(self._on_select_all_changed)
        select_layout.addWidget(self.select_all_cb)
        select_layout.addStretch()
        layout.addLayout(select_layout)

        # 文件列表
        list_widget = QListWidget(self)
        list_widget.setStyleSheet(f"""
            QListWidget {{
                border: 1px solid {border_color};
                border-radius: 8px;
                background-color: {bg_color};
            }}
            QListWidget::item {{
                padding: 4px;
                border-bottom: 1px solid {border_color};
            }}
        """)

        self.file_cbs = []
        for i, op in enumerate(self.operations):
            file_path = op.get("file_path", "")
            file_name = Path(file_path).name if file_path else "未知"
            tool_name = op.get("tool_name", "")

            item = QListWidgetItem(list_widget)
            item_widget = QWidget()
            item_layout = QHBoxLayout(item_widget)
            item_layout.setContentsMargins(10, 8, 10, 8)

            cb = QCheckBox()
            cb.setChecked(True)
            cb.index = i
            cb.stateChanged.connect(self._on_item_check_changed)
            self.file_cbs.append(cb)

            name_label = QLabel(file_name)
            name_label.setStyleSheet(f"color: {text_color}; font-weight: bold;")

            path_label = QLabel(file_path)
            path_label.setStyleSheet(f"color: #8c99ad; {get_font_family_css()} font-size: {scale_font_size(12)}px;")
            path_label.setWordWrap(True)

            item_layout.addWidget(cb, 0)
            item_layout.addWidget(name_label, 0)
            item_layout.addWidget(path_label, 1)

            # 添加差异查看按钮
            diff_btn = TransparentToolButton(get_icon("差异对比"), self)
            diff_btn.setFixedSize(24, 24)
            diff_btn.setToolTip("查看差异")
            diff_btn.op_index = i
            diff_btn.clicked.connect(lambda _, idx=i: self._show_diff(idx))
            item_layout.addWidget(diff_btn, 0)

            # 添加单独撤销按钮
            undo_btn = ToolButton(get_icon("撤销"), self)
            undo_btn.setFixedSize(24, 24)
            undo_btn.op_index = i
            undo_btn.clicked.connect(lambda _, idx=i: self._undo_single_operation(idx))
            item_layout.addWidget(undo_btn, 0)

            item.setSizeHint(item_widget.sizeHint())
            list_widget.addItem(item)
            list_widget.setItemWidget(item, item_widget)

        layout.addWidget(list_widget, 1)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.cancel_btn = PushButton("取消撤销", self)
        self.cancel_btn.setDefault(False)
        self.cancel_btn.clicked.connect(self._on_cancel)

        self.keep_btn = PushButton("不还原更改", self)
        self.keep_btn.clicked.connect(self._on_keep)

        self.restore_btn = PrimaryPushButton("还原所选文件", self)
        self.restore_btn.setDefault(True)
        self.restore_btn.clicked.connect(self._on_restore)

        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.keep_btn)
        btn_layout.addWidget(self.restore_btn)
        layout.addLayout(btn_layout)

    def _on_select_all_changed(self, state):
        checked = state == Qt.Checked
        self._selected_set = set(range(len(self.operations))) if checked else set()
        for cb in self.file_cbs:
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)

    def _on_item_check_changed(self, state):
        cb = self.sender()
        if state == Qt.Checked:
            self._selected_set.add(cb.index)
        else:
            self._selected_set.discard(cb.index)

        all_checked = len(self._selected_set) == len(self.operations)
        none_checked = len(self._selected_set) == 0
        self.select_all_cb.blockSignals(True)
        self.select_all_cb.setCheckState(Qt.Checked if all_checked else (Qt.Unchecked if none_checked else Qt.PartiallyChecked))
        self.select_all_cb.blockSignals(False)

    def _on_cancel(self):
        """取消撤销"""
        self._result = self.CANCEL
        self.reject()

    def _on_keep(self):
        """不还原文件（但卡片撤销）"""
        self._result = self.KEEP_CARD
        self.selected_ops = []
        self.accept()

    def _on_restore(self):
        """还原所选文件"""
        self._result = self.RESTORE
        self.selected_ops = [self.operations[i] for i in sorted(self._selected_set)]
        if not self.selected_ops:
            return
        self.accept()

    def _show_diff(self, index: int):
        """显示指定操作的差异"""
        from app.utils.diff_viewer import DiffHtmlGenerator

        op = self.operations[index]
        file_path = op.get("file_path", "")
        backup_path = op.get("backup_path", "")

        if not file_path or not backup_path:
            return

        try:
            # 读取备份文件和当前文件
            with open(backup_path, 'r', encoding='utf-8', errors='replace') as f:
                old_content = f.read()
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                new_content = f.read()

            # 生成 unified diff
            import difflib
            old_lines = old_content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)

            diff = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=Path(file_path).name,
                tofile=Path(file_path).name,
                lineterm='\n',
                n=10,
            )

            diff_output = ''.join(diff)
            html = DiffHtmlGenerator.generate_html_report(diff_output, "")

            # 内嵌显示差异（覆盖右侧对话区域）
            from app.widgets.cards.global_card_controller import get_global_card_controller

            controller = get_global_card_controller()
            if controller is not None:
                controller.show_diff_viewer(html)
            else:
                # 回退：弹窗模式
                from app.utils.diff_viewer import DiffViewerWindow

                viewer = DiffViewerWindow(parent=self)
                viewer.load_html(html)
                viewer.show()

        except Exception as e:
            from loguru import logger
            logger.error(f"[FileUndo] 显示差异失败: {e}")

    def get_result(self) -> int:
        """获取用户的选择"""
        return self._result

    def get_selected_operations(self) -> List[Dict]:
        """获取选中的操作列表"""
        return getattr(self, 'selected_ops', [])

    def _undo_single_operation(self, index: int):
        """单独撤销指定索引的操作"""
        if index < 0 or index >= len(self.operations):
            return

        from loguru import logger
        from qfluentwidgets import InfoBar

        # InfoBar 挂到 tab 管理器顶层窗口（未就绪时兜底对话框本身）
        from app.widgets.tab_manager_window import TabManagerWindow

        bar_parent = TabManagerWindow.get_instance() or self.window() or self

        op = self.operations[index]
        if not self.file_recorder:
            InfoBar.warning(
                "无法撤销",
                "文件记录器未初始化",
                parent=bar_parent
            )
            return

        # 执行回滚
        result = self.file_recorder.rollback_operations([op])

        # 显示结果
        if result.success_count > 0:
            InfoBar.success(
                "撤销成功",
                f"已成功撤销操作: {Path(op.get('file_path', '未知')).name}",
                parent=bar_parent
            )
            logger.info(f"[FileUndo] 单个操作撤销成功: {op.get('file_path')}")

            # 从列表移除该操作
            self.operations.pop(index)
            self._selected_set.discard(index)

            # 更新选中集合中大于 index 的索引（因为列表变短了）
            new_selected = set()
            for s in self._selected_set:
                if s > index:
                    new_selected.add(s - 1)
                else:
                    new_selected.add(s)
            self._selected_set = new_selected

            # 刷新列表
            # 重新构建整个列表比较简单
            list_widget = self.findChild(QListWidget)
            if list_widget:
                list_widget.clear()
                self.file_cbs = []
                dark = isDarkTheme()
                border_color = "#3a3a3a" if dark else "#d0d0d0"
                text_color = "#e0e0e0" if dark else "#333333"

                for i, op_cur in enumerate(self.operations):
                    file_path = op_cur.get("file_path", "")
                    file_name = Path(file_path).name if file_path else "未知"
                    tool_name = op_cur.get("tool_name", "")

                    item = QListWidgetItem(list_widget)
                    item_widget = QWidget()
                    item_layout = QHBoxLayout(item_widget)
                    item_layout.setContentsMargins(10, 8, 10, 8)

                    cb = QCheckBox()
                    cb.setChecked(i in self._selected_set)
                    cb.index = i
                    cb.stateChanged.connect(self._on_item_check_changed)
                    self.file_cbs.append(cb)

                    name_label = QLabel(file_name)
                    name_label.setStyleSheet(f"color: {text_color}; font-weight: bold;")

                    path_label = QLabel(file_path)
                    path_label.setStyleSheet(f"color: #8c99ad; {get_font_family_css()} font-size: 12px;")
                    path_label.setWordWrap(True)

                    item_layout.addWidget(cb, 0)
                    item_layout.addWidget(name_label, 0)
                    item_layout.addWidget(path_label, 1)

                    # 添加差异查看按钮
                    diff_btn = TransparentToolButton(get_icon("差异对比"), self)
                    diff_btn.setFixedSize(24, 24)
                    diff_btn.setToolTip("查看差异")
                    diff_btn.op_index = i
                    diff_btn.clicked.connect(lambda _, idx=i: self._show_diff(idx))
                    item_layout.addWidget(diff_btn, 0)

                    # 添加单独撤销按钮
                    undo_btn = ToolButton(get_icon("撤销"), self)
                    undo_btn.setFixedSize(24, 24)
                    undo_btn.op_index = i
                    undo_btn.clicked.connect(lambda _, idx=i: self._undo_single_operation(idx))
                    item_layout.addWidget(undo_btn, 0)

                    item.setSizeHint(item_widget.sizeHint())
                    list_widget.addItem(item)
                    list_widget.setItemWidget(item, item_widget)

                # 更新计数标签
                parent_layout = self.layout()
                for i in range(parent_layout.count()):
                    item = parent_layout.itemAt(i)
                    if item and item.widget() and isinstance(item.widget(), QLabel):
                        label = item.widget()
                        if "共" in label.text() and "个文件将被回滚" in label.text():
                            label.setText(f"共 {len(self.operations)} 个文件将被回滚：")
                            break

                # 更新全选状态
                if len(self.operations) == 0:
                    self.select_all_cb.setCheckState(Qt.Unchecked)
                else:
                    all_checked = len(self._selected_set) == len(self.operations)
                    none_checked = len(self._selected_set) == 0
                    self.select_all_cb.setCheckState(Qt.Checked if all_checked else (Qt.Unchecked if none_checked else Qt.PartiallyChecked))

        else:
            InfoBar.error(
                "撤销失败",
                f"撤销操作失败: {result.failed_files}",
                parent=bar_parent
            )
            logger.error(f"[FileUndo] 单个操作撤销失败: {result.failed_files}")
