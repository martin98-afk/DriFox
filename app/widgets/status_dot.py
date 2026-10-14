# -*- coding: utf-8 -*-
"""CSS 圆点状态灯 — 全软件统一的小圆点指示组件

替代「● 文本字符」方案：小字号字符圆抗锯齿发虚，且全角字形 advance
宽度超出定宽 QLabel 时两侧被裁切（2026-10-14 MCP 设置卡实测）。
QSS border-radius 圆为矢量绘制，任意缩放不发虚不裁切。

用法：
    dot = StatusDot(10, color="#22c55e")   # 实心状态灯
    dot.set_color("#f59e0b")               # 状态切换换色
    ring = StatusDot(12, hollow=True)      # 空心圆环（radio 未选中态）
    ring.set_hollow(False)                 # 切实心（选中态）
"""

from PyQt5.QtWidgets import QLabel


class StatusDot(QLabel):
    """实心/空心 CSS 圆点。颜色与空心态均支持动态切换，样式只在变化时重写。"""

    def __init__(self, diameter: int = 10, color: str = "#6b7280", hollow: bool = False, parent=None):
        super().__init__(parent)
        self._diameter = max(4, int(diameter))
        self._color = color
        self._hollow = hollow
        self.setFixedSize(self._diameter, self._diameter)
        self._apply()

    def set_color(self, color: str) -> None:
        if color != self._color:
            self._color = color
            self._apply()

    def set_hollow(self, hollow: bool) -> None:
        if hollow != self._hollow:
            self._hollow = hollow
            self._apply()

    def _apply(self) -> None:
        radius = self._diameter // 2
        if self._hollow:
            # 空心圆环：边框太细会在高 DPI 下糊掉，按直径取 1/8 且至少 1px
            bw = max(1, self._diameter // 8)
            self.setStyleSheet(
                f"background-color: transparent; border: {bw}px solid {self._color};"
                f" border-radius: {radius}px; padding: 0;"
            )
        else:
            self.setStyleSheet(
                f"background-color: {self._color}; border: none; border-radius: {radius}px; padding: 0;"
            )
