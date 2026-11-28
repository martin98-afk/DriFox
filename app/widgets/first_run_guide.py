"""首次对话前的模型配置指引。"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, PrimaryPushButton, SubtitleLabel, TransparentPushButton

from app.utils.design_tokens import Colors


class FirstRunGuide(QScrollArea):
    addProvider = pyqtSignal()
    chooseModel = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        surface = QWidget()
        self.setWidget(surface)
        self.viewport().setAutoFillBackground(False)
        surface.setAutoFillBackground(False)
        outer = QHBoxLayout(surface)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.addStretch()
        column = QWidget(surface)
        column.setMaximumWidth(480)
        outer.addWidget(column, 6)
        outer.addStretch()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addStretch()
        layout.addWidget(SubtitleLabel("欢迎使用 DriFox", column))
        layout.addWidget(BodyLabel("连接一个聊天模型，开始你的第一段对话。", column))
        layout.addSpacing(18)
        self.provider_status = self._step(layout, "连接服务商")
        layout.addSpacing(8)
        self.model_status = self._step(layout, "选择聊天模型")
        layout.addSpacing(16)
        actions = QHBoxLayout()
        self.primary = PrimaryPushButton(column)
        self.primary.clicked.connect(self._advance)
        actions.addWidget(self.primary)
        skip = TransparentPushButton("跳过指引", column)
        skip.clicked.connect(self.finished)
        actions.addWidget(skip)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()
        self.update_progress(False, "")

    def _step(self, layout, title):
        heading = BodyLabel(title, self)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        status = CaptionLabel(self)
        status.setTextFormat(Qt.PlainText)
        status.setWordWrap(True)
        layout.addWidget(status)
        return status

    def update_progress(self, has_provider: bool, model: str):
        self.has_provider = has_provider
        self.model = model
        self.provider_status.setText("已配置聊天服务商" if has_provider else "选择预设服务商或自定义地址，按提示完成认证。")
        self.model_status.setText(f"当前模型 · {model}" if model else "从服务商提供的聊天模型中选择一个。")
        for label in (self.provider_status, self.model_status):
            label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; background: transparent;")
        if model:
            action = "开始对话"
        elif has_provider:
            action = "选择模型"
        else:
            action = "添加服务商"
        self.primary.setText(action)

    def _advance(self):
        if self.model:
            self.finished.emit()
        elif self.has_provider:
            self.chooseModel.emit()
        else:
            self.addProvider.emit()
