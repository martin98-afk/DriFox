# -*- coding: utf-8 -*-
"""
ElidedLabel - 自动根据可用宽度省略文本的 QLabel（中间省略）

从 mcp_setting_card._{ElidedLabel} 提取为共享模块，消除多处重复定义。
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QSizePolicy


class _ElidedLabel(QLabel):
    """自动根据可用宽度省略文本的 QLabel（中间省略）"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = text
        # 防止布局根据文本内容自动扩展宽度，确保宽度由父布局决定
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str):
        self._full_text = text
        self._update_elided()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self):
        w = self.width()
        if w <= 0:
            # 还没布局完成（width=0），先显示完整文本，等 resizeEvent 时再省略
            if self.text() != self._full_text:
                super().setText(self._full_text)
            return
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full_text, Qt.ElideMiddle, w)
        # 只在文本变化时更新，避免触发不必要的 updateGeometry → 布局重算
        if self.text() != elided:
            super().setText(elided)
