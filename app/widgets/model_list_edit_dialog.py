# -*- coding: utf-8 -*-
"""
极简模型列表编辑器（嵌入式 Widget）
Enter 新增，Delete 删除，双击编辑，拖拽排序
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from app.utils.design_tokens import Colors, get_unified_scrollbar_style, scale_font_size
from app.utils.utils import get_font_family_css


class ModelListEditorWidget(QWidget):
    """极简模型列表编辑器 — 可内嵌到表单卡片中，点击按钮切换显隐"""

    def __init__(self, models: list | None = None, parent=None):
        super().__init__(parent)
        self._init_ui(models or [])
        self.refresh_style()

    def _build_qss(self) -> str:
        """构建主题 QSS（refresh_style 时重建，保证颜色/字号随系统）"""
        Colors.refresh()
        return f"""
            QWidget {{
                background: transparent;
            }}
            QListWidget {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                {get_font_family_css()}
                font-size: {scale_font_size(13)}px;
                outline: none;
            }}
            QListWidget::item {{
                background-color: transparent;
                padding: 4px 8px;
                border-radius: 3px;
            }}
            QListWidget::item:hover {{
                background-color: {Colors.HOVER_BG};
            }}
            QListWidget::item:selected {{
                background-color: {Colors.INPUT_FOCUS_BORDER};
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item:selected:hover {{
                background-color: {Colors.HOVER_BG_STRONG};
            }}
        """ + get_unified_scrollbar_style(6)

    def refresh_style(self):
        """主题/字号变更时刷新样式（由宿主卡片 refresh_style 链调用）"""
        self.setStyleSheet(self._build_qss())
        self.hint_label.setStyleSheet(
            f"background: transparent; border: none; {get_font_family_css()}"
            f" font-size: {scale_font_size(11)}px; padding: 0;"
        )

    def _init_ui(self, models: list):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 提示行
        self.hint_label = QLabel(
            "<span style='color:#808080;'>双击编辑</span> · <span style='color:#606060;'>Enter 新增</span>"
            " · <span style='color:#606060;'>Delete 删除</span> · <span style='color:#606060;'>拖拽排序</span>"
        )
        self.hint_label.setStyleSheet(
            f"background: transparent; border: none; {get_font_family_css()}"
            f" font-size: {scale_font_size(11)}px; padding: 0;"
        )
        layout.addWidget(self.hint_label)

        # 列表
        self.listWidget = QListWidget()
        # 最大高度：内容少时自适应矮，超出封顶后内部滚动
        self.listWidget.setMaximumHeight(200)
        self.listWidget.setDragDropMode(QListWidget.InternalMove)
        self.listWidget.setDefaultDropAction(Qt.MoveAction)
        self.listWidget.setSelectionBehavior(QListWidget.SelectRows)
        self.listWidget.setEditTriggers(QListWidget.DoubleClicked | QListWidget.EditKeyPressed)
        self.listWidget.itemDoubleClicked.connect(self._start_edit)
        self.listWidget.addItems(models)
        layout.addWidget(self.listWidget)

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

    def set_models(self, models: list):
        """装载模型列表（清空后填入）"""
        self.listWidget.clear()
        self.listWidget.addItems(models)

    def get_models(self) -> list:
        return [self.listWidget.item(i).text() for i in range(self.listWidget.count())]
