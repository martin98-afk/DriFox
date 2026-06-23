# -*- coding: utf-8 -*-
"""
极简模型列表编辑器
Enter 新增，Delete 删除，双击编辑，拖拽排序
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from app.utils.design_tokens import scale_font_size
from app.utils.utils import get_font_family_css


class ModelListEditDialog(QDialog):
    """极简模型列表编辑器"""

    def __init__(self, models: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑模型列表")
        self.setMinimumWidth(380)
        self.setMinimumHeight(320)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: #1e1e1e;
            }}
            QListWidget {{
                background-color: #252526;
                color: #cccccc;
                border: 1px solid #3c3c3c;
                border-radius: 6px;
                {get_font_family_css()}
                font-size: {scale_font_size(13)}px;
                outline: none;
            }}
            QListWidget::item {{
                background-color: #252526;
                padding: 4px 8px;
                border-radius: 3px;
            }}
            QListWidget::item:hover {{
                background-color: #2a2d2e;
            }}
            QListWidget::item:selected {{
                background-color: #094771;
                color: #ffffff;
            }}
            QListWidget::item:selected:hover {{
                background-color: #1177bb;
            }}
            QPushButton {{
                background: transparent;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                color: #cccccc;
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
                padding: 4px 12px;
            }}
            QPushButton:hover {{
                border-color: #007acc;
                color: #ffffff;
            }}
        """)
        self._init_ui(models)

    def _init_ui(self, models):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # 提示行
        hint_label = QLabel("<span style='color:#808080;'>双击编辑</span> · <span style='color:#606060;'>Enter 新增</span> · <span style='color:#606060;'>Delete 删除</span> · <span style='color:#606060;'>拖拽排序</span>")
        hint_label.setStyleSheet(f"background: transparent; border: none; {get_font_family_css()} font-size: {scale_font_size(11)}px; padding: 0;")
        layout.addWidget(hint_label)

        # 列表
        self.listWidget = QListWidget()
        self.listWidget.setDragDropMode(QListWidget.InternalMove)
        self.listWidget.setDefaultDropAction(Qt.MoveAction)
        self.listWidget.setSelectionBehavior(QListWidget.SelectRows)
        self.listWidget.setEditTriggers(
            QListWidget.DoubleClicked | QListWidget.EditKeyPressed
        )
        self.listWidget.itemDoubleClicked.connect(self._start_edit)
        self.listWidget.addItems(models)
        layout.addWidget(self.listWidget)

        # 底部按钮
        bottom = QHBoxLayout()
        bottom.addStretch()

        cancelBtn = QPushButton("取消")
        cancelBtn.setFixedSize(60, 28)
        cancelBtn.clicked.connect(self.reject)
        bottom.addWidget(cancelBtn)

        okBtn = QPushButton("确定")
        okBtn.setFixedSize(60, 28)
        okBtn.setStyleSheet(f"""
            QPushButton {{
                background: #0078d4;
                border: none;
                border-radius: 4px;
                color: #fff;
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
            }}
            QPushButton:hover {{
                background: #1a8ae5;
            }}
        """)
        okBtn.clicked.connect(self.accept)
        bottom.addWidget(okBtn)

        layout.addLayout(bottom)

        self.listWidget.setFocus()

    def _start_edit(self, item):
        """双击开始编辑"""
        self.listWidget.editItem(item)

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        if key in (Qt.Key_Return, Qt.Key_Enter) and not mods:
            self._add_new()
            return

        if key == Qt.Key_Delete:
            self._delete_selected()
            return

        if key == Qt.Key_Escape:
            self.reject()
            return

        super().keyPressEvent(event)

    def _add_new(self):
        """添加新项并立即编辑"""
        item = QListWidgetItem("新模型")
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self.listWidget.addItem(item)
        self.listWidget.setCurrentItem(item)
        self.listWidget.editItem(item)

    def _delete_selected(self):
        """删除选中项"""
        row = self.listWidget.currentRow()
        if row >= 0:
            self.listWidget.takeItem(row)

    def get_models(self):
        return [self.listWidget.item(i).text() for i in range(self.listWidget.count())]
