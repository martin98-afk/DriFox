# -*- coding: utf-8 -*-
"""rename_dialog.py — 通用命名弹窗（新建助手 / 新建技能）"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from qfluentwidgets import BodyLabel, LineEdit, MaskDialogBase

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css


class RenameDialog(MaskDialogBase):
    """命名弹窗：确认后发出 confirmed(str)"""

    confirmed = pyqtSignal(str)

    def __init__(self, title: str, hint: str = "", default: str = "", parent=None):
        super().__init__(parent)
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))
        self.widget.setObjectName("renameDialog")
        self.widget.setStyleSheet(
            f"""
            #renameDialog {{
                background: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """
        )
        v = QVBoxLayout(self.widget)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(10)

        self._title = BodyLabel(title, self.widget)
        self._title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(14)}; font-weight: 600;"
        )
        v.addWidget(self._title)

        if hint:
            h = QLabel(hint, self.widget)
            h.setWordWrap(True)
            h.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; {get_font_family_css()} {font_size_css(11)}"
            )
            v.addWidget(h)

        self._input = LineEdit(self.widget)
        self._input.setPlaceholderText("输入名称")
        self._input.setText(default)
        self._input.setFixedHeight(34)
        self._input.setClearButtonEnabled(True)
        self._input.setStyleSheet(
            f"""
            LineEdit {{
                background: {Colors.INPUT_BG_START};
                border: 1px solid {Colors.INPUT_BORDER};
                color: {Colors.INPUT_TEXT};
                padding: 4px 10px;
                border-radius: 6px;
                {get_font_family_css()} {font_size_css(13)}
            }}
            LineEdit:focus {{ border-color: {Colors.INFO}; }}
        """
        )
        self._input.returnPressed.connect(self._accept)
        v.addWidget(self._input)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("取消", self.widget)
        cancel.setFixedHeight(30)
        cancel.clicked.connect(self.close)
        confirm = QPushButton("确定", self.widget)
        confirm.setFixedHeight(30)
        confirm.setDefault(True)
        confirm.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(confirm)
        v.addLayout(btns)

        self.widget.setFixedSize(380, self.widget.sizeHint().height() + 40)
        self._center()

    def _center(self) -> None:
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center()

    def _accept(self) -> None:
        txt = self._input.text().strip()
        if not txt:
            return
        self.confirmed.emit(txt)
        self.accept()

    def showEvent(self, e):
        super().showEvent(e)
        self._input.setFocus()
        self._input.selectAll()
