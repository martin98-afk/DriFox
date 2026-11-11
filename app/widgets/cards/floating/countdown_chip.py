# -*- coding: utf-8 -*-
"""环形倒计时胶囊 —— 提问卡与审批卡共用的倒计时徽标。

外观：进度环（轨道底弧 + 前景弧，RoundCap）+ 剩余时间。无底板无边框。
分档：> WARN_SEC 主题强调色；<= WARN_SEC 琥珀；<= DANGER_SEC 红色加粗 + 呼吸光晕；
非告警文字用系统文字主色，告警文字随档位色。

宽度在 start 时按总时长格式化结果定死，之后每秒只 update() 重绘、不触发布局
重排（浮动卡 header 里的控件必须尺寸稳定，提问卡有高度抖动前科）。
"""

import math

from PySide6.QtCore import QRectF, QVariantAnimation, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font

# 分档阈值
WARN_SEC = 30
DANGER_SEC = 10

class CountdownChip(QWidget):
    """环形倒计时徽标（start/stop/set_left 驱动，paint 自绘）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFont(get_unified_font(10))
        self.setFixedHeight(18)
        self._total = 0
        self._left = 0
        self._glow = 0.0  # danger 呼吸相位 0..1（弦式包络）
        self._breath = QVariantAnimation(self)
        self._breath.setStartValue(0.0)
        self._breath.setEndValue(1.0)
        self._breath.setDuration(1200)
        self._breath.setLoopCount(-1)
        self._breath.valueChanged.connect(self._on_breath)
        self.setVisible(False)

    def start(self, total_sec: int) -> None:
        self._total = max(1, int(total_sec))
        self._left = self._total
        self._fit_size()
        self.setVisible(True)
        self.update()

    def stop(self) -> None:
        self._breath.stop()
        self._glow = 0.0
        self.setVisible(False)

    def set_left(self, left: int) -> None:
        self._left = max(0, int(left))
        if self._left <= DANGER_SEC and not self._breath.state():
            self._breath.start()
        elif self._left > DANGER_SEC and self._breath.state():
            self._breath.stop()
            self._glow = 0.0
        self.update()

    def refresh_theme(self) -> None:
        """主题热切换后重绘（色值在 paint 时实时取令牌）"""
        self.update()

    def _on_breath(self, value) -> None:
        self._glow = (1.0 - math.cos(2.0 * math.pi * float(value))) / 2.0
        self.update()

    @staticmethod
    def _fmt(sec: int) -> str:
        if sec >= 60:
            return f"{sec // 60}:{sec % 60:02d}"
        return f"{sec}s"

    def _fit_size(self) -> None:
        """按总时长格式化结果定宽：后续每秒变化不改变控件宽度（防 header 重排）"""
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self._fmt(self._total))
        # 进度环 14 + 环与文字间距 4（无底板，宽度即内容）
        self.setFixedWidth(14 + 4 + text_w)

    def _level(self) -> str:
        if self._left <= DANGER_SEC:
            return "danger"
        if self._left <= WARN_SEC:
            return "warn"
        return "normal"

    def paintEvent(self, event):
        Colors.refresh()
        level = self._level()
        accent = {
            "normal": Colors.REALTIME_ACCENT,
            "warn": Colors.WARNING,
            "danger": Colors.REALTIME_ERROR,
        }[level]

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # 进度环：轨道底弧（次要文字色低透明）+ 前景弧，-90°（12 点钟）顺时针走剩余比例
        ring_rect = QRectF(0, (self.height() - 14) / 2, 14, 14)
        track_color = QColor(Colors.REALTIME_TEXT_SECONDARY)
        track_color.setAlpha(60)
        p.setPen(QPen(track_color, 2))
        p.setBrush(Qt.NoBrush)
        p.drawArc(ring_rect, 0, 360 * 16)

        frac = 0.0 if self._total <= 0 else max(0.0, min(1.0, self._left / self._total))
        pen = QPen(QColor(accent), 2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(ring_rect, 90 * 16, int(-360 * 16 * frac))

        # danger 呼吸：环外扩一圈低 alpha 描边，alpha 随相位起伏
        if level == "danger" and self._glow > 0.01:
            halo = QColor(accent)
            halo.setAlpha(int(30 + 80 * self._glow))
            p.setPen(QPen(halo, 3))
            p.drawArc(ring_rect.adjusted(-1.5, -1.5, 1.5, 1.5), 90 * 16, int(-360 * 16 * frac))

        # 剩余时间：非告警用系统文字主色；warn/danger 文字随档位色强化警示
        # （start 时已定宽，文本变化不会引起布局重排）
        p.setPen(QColor(Colors.REALTIME_TEXT if level == "normal" else accent))
        f = self.font()
        f.setBold(level == "danger")
        p.setFont(f)
        text_rect = QRectF(14 + 4, 0, self.width() - 14 - 4, self.height())
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self._fmt(self._left))
        p.end()
