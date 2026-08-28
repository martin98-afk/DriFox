# -*- coding: utf-8 -*-
"""内嵌文件撤销预览卡片。"""
from pathlib import Path
import difflib
from typing import Dict, List

from PySide6.QtCore import QEventLoop, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import PrimaryPushButton, PushButton, ToolButton, TransparentToolButton, isDarkTheme

from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.diff_viewer import DiffHtmlGenerator
from app.utils.utils import get_font_family_css, get_icon, get_unified_font
from app.widgets.cards.settings.base_settings_card import BaseSettingsCard


class FileUndoCard(BaseSettingsCard):
    """在全局卡片容器中确认文件撤销。"""

    finished = Signal(int, list)
    diffRequested = Signal(str, str)
    CANCEL, KEEP_CARD, RESTORE = 0, 1, 2

    def __init__(self, operations: List[Dict], file_recorder=None, parent=None):
        super().__init__("还原文件", "↩️", parent)
        self.operations = list(operations)
        self.file_recorder = file_recorder
        self._selected_set = set(range(len(self.operations)))
        self._result = self.CANCEL
        self._selected_ops = []
        self._loop = None
        self._build()

    def exec(self):
        from app.widgets.cards.global_card_controller import get_global_card_controller
        controller = get_global_card_controller()
        if controller is None:
            return self.CANCEL
        controller.show_file_undo(self.operations, self.file_recorder, self._finish)
        self._loop = QEventLoop()
        self._loop.exec()
        return self._result

    def _finish(self, result, selected):
        self._result, self._selected_ops = result, selected
        if self._loop is not None:
            self._loop.quit()
        else:
            self.finished.emit(result, selected)

    def get_selected_operations(self):
        return self._selected_ops

    def _build(self):
        title_label = QLabel("确认撤销以下文件操作？")
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(13)}; background: transparent;"
        )
        self.content_layout.addWidget(title_label)

        desc_label = QLabel("点击「还原所选文件」将恢复这些文件到操作前的状态。")
        desc_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(12)}; background: transparent;"
        )
        self.content_layout.addWidget(desc_label)

        self.select_all = QCheckBox("全选")
        self.select_all.setFont(get_unified_font(12))
        self.select_all.setChecked(True)
        self.select_all.stateChanged.connect(self._select_all)
        self.content_layout.addWidget(self.select_all)
        self.list = QListWidget()
        self.content_layout.addWidget(self.list, 1)
        self._refresh_list()
        row = QHBoxLayout()
        row.addStretch()
        cancel = PushButton("取消撤销")
        cancel.setFont(get_unified_font(12))
        keep = PushButton("不还原更改")
        keep.setFont(get_unified_font(12))
        restore = PrimaryPushButton("还原所选文件")
        restore.setFont(get_unified_font(12, bold=True))
        cancel.clicked.connect(lambda: self.finished.emit(self.CANCEL, []))
        keep.clicked.connect(lambda: self.finished.emit(self.KEEP_CARD, []))
        restore.clicked.connect(self._restore)
        row.addWidget(cancel); row.addWidget(keep); row.addWidget(restore)
        self.content_layout.addLayout(row)

    def _refresh_list(self):
        self.list.clear()
        for i, op in enumerate(self.operations):
            item = QListWidgetItem(self.list)
            w = QWidget(); row = QHBoxLayout(w); row.setContentsMargins(8, 4, 8, 4)
            cb = QCheckBox(); cb.setChecked(i in self._selected_set); cb.stateChanged.connect(lambda state, n=i: self._checked(n, state))
            row.addWidget(cb)
            name_label = QLabel(Path(op.get("file_path", "未知")).name)
            name_label.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}; background: transparent;"
            )
            row.addWidget(name_label)
            path = QLabel(op.get("file_path", ""))
            path.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(12)}; background: transparent;"
            )
            row.addWidget(path, 1)
            diff = TransparentToolButton(get_icon("差异对比")); diff.clicked.connect(lambda _, n=i: self._show_diff(n)); row.addWidget(diff)
            undo = ToolButton(get_icon("撤销")); undo.clicked.connect(lambda _, n=i: self._undo_one(n)); row.addWidget(undo)
            item.setSizeHint(w.sizeHint()); self.list.setItemWidget(item, w)

    def _select_all(self, state):
        self._selected_set = set(range(len(self.operations))) if state == Qt.Checked else set()
        self._refresh_list()

    def _checked(self, index, state):
        (self._selected_set.add if state == Qt.Checked else self._selected_set.discard)(index)

    def _restore(self):
        selected = [self.operations[i] for i in sorted(self._selected_set)]
        if selected: self.finished.emit(self.RESTORE, selected)

    def _show_diff(self, index):
        op = self.operations[index]
        try:
            old = Path(op["backup_path"]).read_text(encoding="utf-8", errors="replace")
            new = Path(op["file_path"]).read_text(encoding="utf-8", errors="replace")
            diff = ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=Path(op["file_path"]).name, tofile=Path(op["file_path"]).name, lineterm="\n", n=10))
            self.diffRequested.emit(DiffHtmlGenerator.generate_html_report(diff, ""), "文件差异对比")
        except Exception:
            return

    def _undo_one(self, index):
        if not self.file_recorder: return
        result = self.file_recorder.rollback_operations([self.operations[index]])
        if result.success_count:
            self.operations.pop(index); self._selected_set = {i - (i > index) for i in self._selected_set if i != index}; self._refresh_list()
