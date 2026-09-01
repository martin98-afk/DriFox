# -*- coding: utf-8 -*-
"""assistant_avatar.py — 助手圆形头像组件

复用 DriFox 既有 Color tokens / _SquareAvatar 思路，但用 QPainter 直接画圆形
（参考 OpenHanako AgentActivityCard 的 round avatar）。不依赖外部 SVG。

两种模式：
1. 有头像文件：QPixmap 圆裁（最大性能路径）
2. 无头像文件：纯色 + 白色缩写（与 _SquareAvatar 同种风格），同时可作为
   fallback avatar，在左侧列表 / TabPanel 投影里都能复用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import QWidget


def _parse_rgba(rgba_str: str) -> QColor:
    if rgba_str.startswith("#"):
        return QColor(rgba_str)
    import re

    m = re.match(
        r"rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(\d+))?\s*\)",
        rgba_str,
    )
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = int(m.group(4)) if m.group(4) else 255
        return QColor(r, g, b, a)
    return QColor(128, 128, 128)


def _initials_of(text: str, max_chars: int = 2) -> str:
    text = (text or "").strip()
    if not text:
        return "?"
    chars: list[str] = []
    for ch in text:
        if ch.isspace() or ch in "-_.":
            continue
        chars.append(ch)
        if len(chars) >= max_chars:
            break
    if not chars:
        return "?"
    return "".join(chars).upper()


class RoundAvatar(QWidget):
    """圆形头像：图片优先；缺图回落色块 + 缩写"""

    def __init__(
        self,
        size: int = 36,
        text: str = "?",
        color: str = "#7C3AED",
        image_path: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._size = size
        self._text = _initials_of(text, max_chars=2)
        self._color = _parse_rgba(color)
        self._pixmap: Optional[QPixmap] = None
        if image_path and Path(image_path).exists():
            self._pixmap = QPixmap(image_path)
        self.setFixedSize(size, size)

    def set_text(self, text: str) -> None:
        self._text = _initials_of(text, max_chars=2)
        self.update()

    def set_color(self, color: str) -> None:
        self._color = _parse_rgba(color)
        self.update()

    def set_image(self, image_path: Optional[str]) -> None:
        self._pixmap = QPixmap(image_path) if image_path and Path(image_path).exists() else None
        self.update()

    # ── 绘制 ──

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = QRectF(0, 0, self._size, self._size)

        if self._pixmap and not self._pixmap.isNull():
            # 圆裁
            path = QPainterPath()
            path.addEllipse(rect)
            p.setClipPath(path)
            scaled = self._pixmap.scaled(
                self._size,
                self._size,
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            x = (scaled.width() - self._size) // 2
            y = (scaled.height() - self._size) // 2
            p.drawPixmap(0, 0, scaled, x, y, self._size, self._size)
            p.setClipping(False)
            # 细描边
            p.setPen(QColor(255, 255, 255, 60))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        else:
            # 色块 + 缩写
            p.setPen(Qt.NoPen)
            p.setBrush(self._color)
            p.drawEllipse(rect)

            p.setPen(QColor(255, 255, 255, 230))
            pixel = int(self._size * 0.42) if len(self._text) <= 1 else int(self._size * 0.36)
            try:
                from app.utils.utils import get_unified_font

                font = get_unified_font()
                font.setPixelSize(pixel)
                font.setBold(True)
            except Exception:
                font = QFont()
                font.setPixelSize(pixel)
                font.setBold(True)
            p.setFont(font)
            p.drawText(rect, Qt.AlignCenter, self._text)
        p.end()


def avatar_pixmap_for_assistant(
    aid: str,
    name: str,
    color: str,
    size: int = 36,
) -> QPixmap:
    """便捷：根据 assistant_id 从 AssistantManager 取头像，失败返回色块头像"""
    try:
        from assistant_hub_manager import AssistantManager

        mgr = AssistantManager.get_instance()
        a = mgr.get(aid)
        ap = mgr.avatar_path(aid) if a else None
    except Exception:
        ap = None
    widget = RoundAvatar(
        size=size,
        text=name,
        color=color,
        image_path=str(ap) if ap else None,
    )
    return widget.grab()
