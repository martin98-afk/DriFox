from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, RadioButton, StrongBodyLabel


class _Choice(QPushButton):
    def __init__(self, name, description, parent):
        super().__init__(parent)
        self.setObjectName('outputChoice')
        self.setCheckable(True)
        self.setAccessibleName(name)
        self.setAccessibleDescription(description)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setStyleSheet('QPushButton#outputChoice { background: transparent; border: 1px solid transparent; border-radius: 6px; }'
                           'QPushButton#outputChoice:hover { background: rgba(128,128,128,0.09); }'
                           'QPushButton#outputChoice:checked { background: rgba(128,128,128,0.12); }'
                           'QPushButton#outputChoice:focus { border-color: palette(highlight); }')
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 10, 6)
        labels = QVBoxLayout()
        labels.setSpacing(1)
        title = BodyLabel(name, self)
        hint = CaptionLabel(description, self)
        hint.setWordWrap(True)
        for label in (title, hint):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            labels.addWidget(label)
        row.addLayout(labels, 1)
        mark = RadioButton(self)
        mark.setFocusPolicy(Qt.NoFocus)
        mark.setAttribute(Qt.WA_TransparentForMouseEvents)
        mark.setFixedWidth(22)
        row.addWidget(mark)
        self.toggled.connect(mark.setChecked)


class OutputSettingsCard(QWidget):
    def __init__(self, choices, current, choose, parent=None):
        super().__init__(parent)
        self._modes, self._current, self._choose = choices, current, choose
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        layout.addWidget(StrongBodyLabel('输出方式', self))
        self.rows = QVBoxLayout()
        self.rows.setSpacing(2)
        layout.addLayout(self.rows)
        layout.addWidget(CaptionLabel('从下一条消息起生效', self))
        self.buttons = QButtonGroup(self)
        self._choices = {}
        self._signature = None
        self._refresh()
        self.timer = QTimer(self)
        self.timer.setInterval(1200)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

    def _refresh(self):
        if self._signature is not None and not self.isVisible():
            return
        modes = self._modes()
        if modes != self._signature:
            while self.rows.count():
                w = self.rows.takeAt(0).widget()
                self.buttons.removeButton(w)
                w.hide()
                w.deleteLater()
            self._choices.clear()
            for mid, name, description in modes:
                button = _Choice(name, description, self)
                button.clicked.connect(lambda _, key=mid: self._select(key))
                self.buttons.addButton(button)
                self.rows.addWidget(button)
                self._choices[mid] = button
            self._signature = modes
        self._choices.get(self._current(), self._choices['original']).setChecked(True)

    def _select(self, mode_id):
        if not self._choose(mode_id):
            self._choices.get(self._current(), self._choices['original']).setChecked(True)
