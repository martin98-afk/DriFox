# -*- coding: utf-8 -*-
"""SquircleAvatar — 椭方块形插件头像

仿照 app/widgets/cards/settings/project_selector_card.py 的 _SquareAvatar
实现：纯色圆角矩形 + 白色加粗缩写。用于插件列表行，每个插件得到唯一
的视觉标识（颜色由名称 CRC32 哈希而来，缩写由智能规则提取）。

尺寸自适应：接受 font_size 参数，按 font_size * 1.7 缩放图标边长
（最低 20px）。字号变化时调用 set_font_size() 实时更新。

闭包约束（来自 plugin-manager 注释）：
- 不导入 app.core 或 app.widgets
- 不导入 app.utils.utils（避免与 Settings 单例耦合）
- 仅依赖 PyQt5 + stdlib
"""

import colorsys
import re
import zlib
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtSvg import QSvgWidget
from PyQt5.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import isDarkTheme


# ── 尺寸自适应常量 ──────────────────────────────────
# 图标边长 = max(20, int(font_size * 1.7))
# 12px 字体 → 20px 图标
# 14px 字体 → 23px 图标（默认）
# 16px 字体 → 27px 图标
# 18px 字体 → 30px 图标
_AVATAR_SIZE_RATIO = 1.7
_AVATAR_MIN_SIZE = 20


def _compute_avatar_size(font_size: int) -> int:
    """根据上下文字体大小计算头像边长（px）"""
    if font_size <= 0:
        return _AVATAR_MIN_SIZE
    return max(_AVATAR_MIN_SIZE, int(font_size * _AVATAR_SIZE_RATIO))


def extract_initials(name: str) -> str:
    """从插件名提取最多 2 个字符的缩写

    优先级：中文 > 分隔符（_/-/空格）> 驼峰/帕斯卡 > 前 2 字母大写。
    与 project_selector_card.extract_project_initials 行为一致。
    """
    if not name:
        return "??"

    # ── 中文：首个汉字 ──
    has_cjk = any("\u4e00" <= c <= "\u9fff" for c in name)
    if has_cjk:
        for c in name:
            if "\u4e00" <= c <= "\u9fff":
                return c
        return name[0]

    # ── 分隔符拆分（下划线/中划线/空格）──
    for delim in ("_", "-", " "):
        if delim in name:
            parts = [p for p in name.split(delim) if p]
            if len(parts) >= 2:
                return (parts[0][0] + parts[-1][0]).upper()
            name = parts[0]
            break

    # ── 驼峰/帕斯卡：拆分为词 ──
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1|\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1|\2", s)
    words = [w for w in s.split("|") if w]

    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()

    # ── 普通单词：前 2 字母大写 ──
    if len(name) >= 2:
        return name[:2].upper()
    return name.upper()


def name_color(name: str, alpha: int = 255) -> str:
    """根据插件名计算固定 RGBA 颜色（HSL 全空间哈希）

    与 project_selector_card.get_project_color 算法完全一致：
    - H ∈ [0°, 360°)  crc % 360
    - S ∈ [55%, 85%]  55 + ((crc >> 8) % 31)
    - L ∈ [50%, 65%]  50 + ((crc >> 16) % 16)

    用 zlib.crc32 而非内置 hash()，避免 PYTHONHASHSEED 随机化导致颜色漂移。
    """
    crc = zlib.crc32(name.encode("utf-8"))
    h = crc % 360
    s = 55 + ((crc >> 8) % 31)
    l = 50 + ((crc >> 16) % 16)

    r, g, b = colorsys.hls_to_rgb(h / 360.0, l / 100.0, s / 100.0)
    return f"rgba({int(round(r * 255))}, {int(round(g * 255))}, {int(round(b * 255))}, {alpha})"


class SquircleAvatar(QWidget):
    """椭方块形插件头像 — flat design squircle 风格

    纯色圆角矩形 + 白色 1-2 字符缩写。QPainter 精确绘制，
    避免 QSS 在小尺寸下 border + border-radius 抗锯齿走样。

    尺寸自适应：
        构造时传入 font_size（如 14），按 font_size * 1.7 缩放图标边长。
        字号变化时调用 set_font_size() 实时更新，无需重建 widget。
    """

    def __init__(
        self,
        text: str,
        color: str,
        parent=None,
        size: int = 0,
        font_size: int = 0,
    ):
        """初始化头像

        Args:
            text: 缩写文字
            color: RGBA 颜色字符串
            parent: 父控件
            size: 显式尺寸（font_size > 0 时被忽略）
            font_size: 上下文字体大小（px），> 0 时按 1.7 倍放大作为图标尺寸
        """
        super().__init__(parent)
        self._text = text if text else "?"
        self._color = self._parse_rgba(color)
        # font_size 优先于 size
        if font_size > 0:
            self._size = _compute_avatar_size(font_size)
        elif size > 0:
            self._size = size
        else:
            self._size = _AVATAR_MIN_SIZE
        self._font_size = font_size if font_size > 0 else 0
        self.setFixedSize(self._size, self._size)

    @staticmethod
    def _parse_rgba(rgba_str: str) -> QColor:
        """解析 'rgba(r,g,b,a)' 为 QColor，失败回退灰色"""
        if rgba_str.startswith("#"):
            return QColor(rgba_str)
        try:
            m = re.match(
                r"rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
                r"(?:\s*,\s*(\d+))?\s*\)",
                rgba_str,
            )
            if m:
                r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
                a = int(m.group(4)) if m.group(4) else 255
                return QColor(r, g, b, a)
        except Exception:
            pass
        return QColor(128, 128, 128)

    def set_avatar(self, text: str, color: str):
        """更新缩写和颜色（用于状态变化场景）"""
        self._text = extract_initials(text) if text else "?"
        self._color = self._parse_rgba(color)
        self.update()

    def set_font_size(self, font_size: int):
        """根据上下文字体大小动态调整头像尺寸

        Args:
            font_size: 上下文字体大小（px），<= 0 时无操作
        """
        if font_size <= 0:
            return
        self._font_size = font_size
        new_size = _compute_avatar_size(font_size)
        if new_size != self._size:
            self._size = new_size
            self.setFixedSize(new_size, new_size)
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = self.rect()
        # 微妙圆角（约 5px，like VS Code squircle）
        corner_radius = 5

        # 纯色填充背景
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawRoundedRect(rect, corner_radius, corner_radius)

        # 居中白字
        painter.setPen(Qt.white)
        font = painter.font()
        # 字号按 size 比例缩放（参考源算法：14/24 ≈ 0.58）
        font.setPixelSize(max(8, self._size * 14 // 24))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, self._text)


# ── PluginIconWidget ──────────────────────────────────


class PluginIconWidget(QWidget):
    """插件图标组件：SVG 图标 + SquircleAvatar fallback

    根据当前主题自动选择 light/dark SVG。
    无 SVG 时回退到缩写哈希头像（SquircleAvatar）。
    尺寸自适应：font_size * 1.7，最低 20px。
    """

    def __init__(
        self,
        plugin_dir: Path,
        manifest: dict,
        font_size: int = 0,
        parent=None,
    ):
        super().__init__(parent)
        self._plugin_dir = plugin_dir
        self._manifest = manifest
        self._font_size = font_size
        self._svg_widget: Optional["QSvgWidget"] = None
        self._avatar: Optional[SquircleAvatar] = None
        self._setup_ui()

    def _resolve_icon_path(self) -> Optional[Path]:
        """根据 manifest 和当前主题解析实际图标路径"""
        raw = self._manifest.get("icon")
        if not raw:
            default = self._plugin_dir / "icon.svg"
            return default if default.exists() else None
        theme = "dark" if isDarkTheme() else "light"
        if isinstance(raw, str):
            p = self._plugin_dir / raw
            return p if p.exists() else None
        if isinstance(raw, dict):
            path_str = raw.get(theme) or raw.get("light", "")
            if path_str:
                p = (self._plugin_dir / path_str).resolve()
                return p if p.exists() else None
        return None

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        icon_path = self._resolve_icon_path()
        if icon_path is not None:
            size = max(20, int(self._font_size * 1.7)) if self._font_size > 0 else 24
            self._svg_widget = QSvgWidget(str(icon_path), self)
            self._svg_widget.setFixedSize(size, size)
            layout.addWidget(self._svg_widget)
        else:
            plugin_name = self._manifest.get("name", "?")
            self._avatar = SquircleAvatar(
                extract_initials(plugin_name),
                name_color(plugin_name),
                self,
                font_size=self._font_size,
            )
            layout.addWidget(self._avatar)

    def set_font_size(self, font_size: int):
        """更新字号并重建组件（主题切换时也调用此方法）"""
        self._font_size = font_size
        while self.layout().count():
            item = self.layout().takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._setup_ui()

    def reload_icon(self):
        """主题变化后刷新图标（深浅切换）"""
        self.set_font_size(self._font_size)
