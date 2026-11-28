from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QButtonGroup, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, RadioButton

from app.plugins.contracts.message_layout import output_modes
from app.widgets.cards.settings.base_settings_card import BaseSettingsCard


class OutputSettingsCard(BaseSettingsCard):
    def __init__(self, registry, current, choose, parent=None):
        super().__init__('流式输出方式', '', parent=parent)
        self.set_height_mode('content')
        self.close_btn.hide()
        self._registry, self._current, self._choose = registry, current, choose
        self.content_layout.addWidget(CaptionLabel('切换从下一条消息生效，不打断当前输出。', self))
        self.box = QWidget(self)
        self.rows = QVBoxLayout(self.box)
        self.rows.setContentsMargins(0, 4, 0, 0)
        self.content_layout.addWidget(self.box)
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
        modes = output_modes(self._registry)
        signature = tuple((m.id, m.name, m.description) for m in modes)
        if signature != self._signature:
            while self.rows.count():
                w = self.rows.takeAt(0).widget()
                if isinstance(w, RadioButton):
                    self.buttons.removeButton(w)
                w.hide()
                w.deleteLater()
            self._choices.clear()
            for mode in modes:
                button = RadioButton(mode.name, self.box)
                button.clicked.connect(lambda _, mid=mode.id: self._select(mid))
                self.buttons.addButton(button)
                self.rows.addWidget(button)
                hint = CaptionLabel(mode.description, self.box)
                hint.setWordWrap(True)
                self.rows.addWidget(hint)
                self._choices[mode.id] = button
            self._signature = signature
        self._choices.get(self._current(), self._choices['original']).setChecked(True)

    def _select(self, mode_id):
        if not self._choose(mode_id):
            self._refresh()
