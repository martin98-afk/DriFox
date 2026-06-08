# -*- coding: utf-8 -*-
"""
ElidedLabel - 自动根据可用宽度省略文本的 QLabel（中间省略）

从 mcp_setting_card._ElidedLabel 提取为共享模块，消除多处重复定义。
"""
import html

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QSizePolicy


class _ElidedLabel(QLabel):
    """自动根据可用宽度省略文本的 QLabel（中间省略），可选支持搜索匹配高亮"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self._hl_query = ""
        self._hl_color = ""
        # 防止布局根据文本内容自动扩展宽度，确保宽度由父布局决定
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str):
        """设置完整文本，同时清除高亮"""
        self._full_text = text
        self._hl_query = ""
        self._hl_color = ""
        self._update_elided()

    def setHighlight(self, query: str, color: str):
        """设置搜索高亮：匹配部分用 <span> 高亮

        Args:
            query: 搜索关键词（不区分大小写匹配）
            color: 高亮颜色（CSS 格式，如 "#FF6600"）
        """
        self._hl_query = query
        self._hl_color = color
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

        # 有搜索高亮时，对省略后的文本做匹配高亮
        if self._hl_query and self._hl_color:
            lower_elided = elided.lower()
            lower_query = self._hl_query.lower()
            idx = lower_elided.find(lower_query)
            if idx >= 0:
                before = html.escape(elided[:idx])
                match = html.escape(elided[idx:idx + len(self._hl_query)])
                after = html.escape(elided[idx + len(self._hl_query):])
                html_text = (
                    f'<span>{before}'
                    f'<span style="color: {self._hl_color}; font-weight: bold;">{match}</span>'
                    f'{after}</span>'
                )
                if self.text() != html_text:
                    super().setText(html_text)
                return

        # 无高亮或未匹配时，设置纯文本（自动省略）
        if self.text() != elided:
            super().setText(elided)
