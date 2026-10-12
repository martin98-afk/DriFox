# -*- coding: utf-8 -*-
"""方形项目头像 — QPainter 绘制的 flat design squircle 小组件

从原 ``app/widgets/cards/settings/project_selector_card.py`` 拆出（2026-10：
项目选择卡片 UI 本体迁入 history-manager 插件，头像组件保留在宿主公共层，
供标题栏项目按钮与插件项目面板共同使用）。
"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QWidget

from app.utils.design_tokens import scale_font_size
from app.utils.project_meta import contrast_text_color, extract_project_initials
from app.utils.utils import get_unified_font


class _SquareAvatar(QWidget):
    """使用 QPainter 绘制的方形项目头像 — flat design squircle 风格

    纯色圆角矩形 + 1-2 个白色缩写字母，用于项目卡片列表和面包屑标签。
    Qt QSS 在小尺寸（24×24）上同时渲染 border + border-radius 时存在
    抗锯齿走样问题。本类用 QPainter 精确绘制，保证像素完美。
    """

    def __init__(self, text: str, color: str, parent=None, size: int = 24):
        super().__init__(parent)
        self._text = text if text else "?"
        # get_project_color() 返回 "rgba(r,g,b,a)" 格式，
        # QColor(string) 不解析此 CSS 格式，需拆解为数值构造
        self._color = self._parse_rgba(color)
        # 缩写字色按色块亮度自适应（深底白字 / 浅底黑字）：固定白字在明亮
        # 色块（如黄绿系）上对比度低至 1.25:1，缩写完全糊掉
        self._text_color = QColor(contrast_text_color(color))
        self._size = size
        self.setFixedSize(size, size)

    @staticmethod
    def _parse_rgba(rgba_str: str) -> QColor:
        """解析 "rgba(r,g,b,a)" 字符串为 QColor，失败时返回灰色"""
        if rgba_str.startswith("#"):
            return QColor(rgba_str)
        try:
            import re

            m = re.match(r"rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(\d+))?\s*\)", rgba_str)
            if m:
                r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
                a = int(m.group(4)) if m.group(4) else 255
                return QColor(r, g, b, a)
        except Exception:
            pass
        return QColor(128, 128, 128)  # fallback 灰色

    def set_project(self, name: str, color: str):
        """更新项目名和颜色（用于面包屑等动态场景）"""
        self._text = extract_project_initials(name)
        self._color = self._parse_rgba(color)
        self._text_color = QColor(contrast_text_color(color))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = self.rect()
        # 微妙圆角（约 5px，like VS Code squircle）
        corner_radius = 5

        # 纯色填充背景（无边框，flat design）
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawRoundedRect(rect, corner_radius, corner_radius)

        # 居中缩写，字色按色块亮度自适应，使用系统配置字体
        painter.setPen(self._text_color)
        font = get_unified_font()
        # 单字符（中文）字号稍大，双字符略小
        if len(self._text) <= 1:
            font.setPixelSize(scale_font_size(self._size * 14 // 24))
        else:
            font.setPixelSize(scale_font_size(self._size * 14 // 24))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self._text)

    # ── 立即显示 tooltip ─────────────────────────────────────
    # 默认 Qt tooltip 延迟约 700ms，用户体验偏慢。
    # 这里重写 enter/leave，直接用自绘 tooltip 立即显示，
    # 鼠标移出立即隐藏，体感与按钮 hover 行为一致。
    def enterEvent(self, event):
        tip = self.toolTip()
        if tip:
            from app.widgets.simple_hover_tooltip import show_immediate_tooltip

            self._avatar_tip = show_immediate_tooltip(self, tip, duration_ms=0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        tip = getattr(self, "_avatar_tip", None)
        if tip is not None:
            tip.hide()
            tip.deleteLater()
            self._avatar_tip = None
        super().leaveEvent(event)
