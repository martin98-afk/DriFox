# -*- coding: utf-8 -*-
"""消息身份行：圆形头像 + 显示名。

拆分
====

- ``IdentityAvatar`` 圆形头像控件：三种来源
    1. 本地图片路径 → 圆裁绘制
    2. 内置图标引用 ``builtin:drifox`` → DriFox 品牌图标
    3. 空 → 用显示名派生稳定色 + 首字母
- ``IdentityHeader`` 头像 + 名称的单行布局，支持左右对齐（助手在左 / 用户在右）
- ``resolve_avatar_pixmap(identity, size)`` 头像 pixmap 缓存（按 avatar 引用去重，
  同一头像 N 条消息只加载一次）

设计约束
========

- **不依赖 assistant_hub**：插件禁用时主程序仍须能画头像，故本模块自带圆形绘制，
  与插件内的 ``RoundAvatar`` 思路一致但代码独立。
- **颜色派生稳定**：同名 → 同色，跨会话一致（hash 派生，不用随机）。
- ``QColor`` 不认 ``rgba(...)`` 字符串且**不抛异常**（画成黑色）——统一走 ``_qcolor``
  显式解析，这是 assistant_hub 已踩过的坑。
"""

from __future__ import annotations

from typing import Dict, Optional

from PyQt5.QtCore import QRectF, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget

from app.core.message_identity import BUILTIN_AVATAR_DRIFOX, BUILTIN_AVATAR_PREFIX, MessageIdentity
from app.utils.design_tokens import Colors, scale_font_size
from app.utils.utils import get_icon, get_unified_font

# qrc 编译产物：**导入即注册资源**。主程序链路只 import 了深色 icons_rc，
# 浅色 icons_light_rc 从未被引入（同 scroll_to_bottom_button.py 的补齐），
# 不在这里补一次，`:/icons_light/...` 会全部解析失败 → 内置头像空白。
try:
    from app.utils import icons_light_rc as _icons_light_rc  # noqa: F401
    from app.utils import icons_rc as _icons_rc  # noqa: F401
except Exception:  # noqa: BLE001
    pass

# 头像尺寸（两行身份块：名称 + 时间，头像垂直居中于两行）
AVATAR_SIZE = 40

# 名称字号（基础值，实际经 scale_font_size 叠加用户字号档位）
NAME_FONT_SIZE = 15

# 时间字号（小于名称、字色更浅，弱化处理）
TIME_FONT_SIZE = 10

# 无头像时的候选主色（按显示名 hash 稳定选取）
_PALETTE = (
    "#7C3AED",  # 紫
    "#2E86DE",  # 蓝
    "#16A085",  # 青绿
    "#D35400",  # 橙
    "#C0392B",  # 红
    "#8E44AD",  # 深紫
    "#27AE60",  # 绿
    "#B7950B",  # 金
)

# 头像 pixmap 缓存：{cache_key: QPixmap}
# key 维度不含尺寸（尺寸由取值方 scaled 处理）；进程级，上限保护防无界增长。
_PIXMAP_CACHE: Dict[str, QPixmap] = {}
_PIXMAP_CACHE_MAX = 96


def _qcolor(spec: str, fallback: QColor) -> QColor:
    """安全解析颜色字符串（支持 #hex 与 rgba(r,g,b,a)）。

    ⚠️ ``QColor("rgba(...)")`` 返回**无效色且不抛异常**，QPainter 拿无效 brush
    会画成黑色。必须显式解析，解析失败回落 fallback。
    """
    if not spec:
        return fallback
    color = QColor(spec)
    if color.isValid():
        return color
    if spec.startswith("rgba") or spec.startswith("rgb"):
        import re

        match = re.match(r"rgba?\s*\(([^)]+)\)", spec)
        if match:
            parts = [p.strip() for p in match.group(1).split(",")]
            try:
                red, green, blue = (int(float(parts[i])) for i in range(3))
                alpha = int(float(parts[3])) if len(parts) > 3 else 255
                return QColor(red, green, blue, alpha)
            except ValueError, IndexError:
                pass
    return fallback


def _initials(name: str, max_chars: int = 1) -> str:
    """取显示名首字母（中文取首字，英文取首字母大写）"""
    text = (name or "").strip()
    if not text:
        return "?"
    for ch in text:
        if ch.isspace() or ch in "-_.":
            continue
        return ch.upper() if ch.isascii() else ch
    return "?"


def _color_for_name(name: str) -> str:
    """按显示名稳定派生主色（同名恒同色）"""
    text = (name or "").strip()
    if not text:
        return _PALETTE[0]
    return _PALETTE[sum(ord(c) for c in text) % len(_PALETTE)]


def resolve_avatar_pixmap(identity: MessageIdentity, size: int = AVATAR_SIZE) -> Optional[QPixmap]:
    """解析身份头像为 QPixmap（带缓存）。

    - 图片路径：加载并按 size 缩放（失败视作无色块头像）
    - ``builtin:`` 引用：取内置图标
    - 空：返回 None，由调用方画色块 + 首字母

    缓存 key 用头像引用本身，同一头像的多条消息只加载一次。
    """
    avatar = (identity.avatar or "").strip()
    if not avatar:
        return None
    cache_key = f"{avatar}@{size}"
    cached = _PIXMAP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    pixmap: Optional[QPixmap] = None
    if avatar.startswith(BUILTIN_AVATAR_PREFIX):
        icon_name = "drifox" if avatar == BUILTIN_AVATAR_DRIFOX else avatar[len(BUILTIN_AVATAR_PREFIX) :]
        try:
            icon = get_icon(icon_name)
            if not icon.isNull():
                pixmap = icon.pixmap(QSize(size, size))
        except Exception:
            pixmap = None
    else:
        try:
            candidate = QPixmap(avatar)
            if not candidate.isNull():
                pixmap = candidate.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        except Exception:
            pixmap = None

    if pixmap is None or pixmap.isNull():
        return None
    if len(_PIXMAP_CACHE) >= _PIXMAP_CACHE_MAX:
        _PIXMAP_CACHE.clear()
    _PIXMAP_CACHE[cache_key] = pixmap
    return pixmap


def clear_avatar_cache() -> None:
    """清空头像 pixmap 缓存（主题切换 / 插件热重载后调用）"""
    _PIXMAP_CACHE.clear()


class IdentityAvatar(QWidget):
    """圆形头像控件（图片 / 内置图标 / 首字母色块三合一）"""

    def __init__(self, identity: MessageIdentity, size: int = AVATAR_SIZE, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._identity = identity
        self._size = size
        self._pixmap = resolve_avatar_pixmap(identity, size)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_identity(self, identity: MessageIdentity) -> None:
        self._identity = identity
        self._pixmap = resolve_avatar_pixmap(identity, self._size)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = QRectF(0, 0, self._size, self._size)

        if self._pixmap is not None and not self._pixmap.isNull():
            path = QPainterPath()
            path.addEllipse(rect)
            painter.setClipPath(path)
            # 半透明 PNG：先铺底色，避免透明区透出卡片背景显得脏
            painter.setPen(Qt.NoPen)
            painter.setBrush(_qcolor(Colors.CARD_BG_SOLID, QColor(33, 33, 38, 250)))
            painter.drawRect(rect)
            source = self._pixmap
            offset_x = max(0, (source.width() - self._size) // 2)
            offset_y = max(0, (source.height() - self._size) // 2)
            painter.drawPixmap(
                0, 0, source, offset_x, offset_y, min(self._size, source.width()), min(self._size, source.height())
            )
            painter.setClipping(False)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(_qcolor(_color_for_name(self._identity.name), QColor(124, 58, 237)))
            painter.drawEllipse(rect)
            painter.setPen(QColor(255, 255, 255, 235))
            pixel = int(self._size * 0.46)
            try:
                from app.utils.utils import get_unified_font

                font = get_unified_font()
                font.setPixelSize(pixel)
                font.setBold(True)
            except Exception:
                font = QFont()
                font.setPixelSize(pixel)
                font.setBold(True)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, _initials(self._identity.name))
        painter.end()


class IdentityHeader(QWidget):
    """身份行：头像 + 显示名（单行）

    助手消息左对齐（头像在左、名称在右）；用户消息右对齐（名称在左、头像在右）。
    """

    def __init__(
        self,
        identity: MessageIdentity,
        align_right: bool = False,
        parent: Optional[QWidget] = None,
        timestamp: str = "",
    ):
        super().__init__(parent)
        self._align_right = align_right
        self.setStyleSheet("background: transparent;")

        # 名称在上、时间在下（同列两行）；头像在侧，垂直居中于两行整体。
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(10)

        self._avatar = IdentityAvatar(identity, AVATAR_SIZE, self)

        text_col = QWidget(self)
        text_col.setStyleSheet("background: transparent;")
        col = QVBoxLayout(text_col)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)

        self._name_label = QLabel(identity.name or "", text_col)
        self._apply_name_style()

        self._time_label = QLabel(timestamp or "", text_col)
        self._time_label.setVisible(bool(timestamp))
        self._apply_time_style()

        col.addWidget(self._name_label)
        col.addWidget(self._time_label)

        if align_right:
            layout.addStretch(1)
            layout.addWidget(text_col)
            layout.addWidget(self._avatar)
        else:
            layout.addWidget(self._avatar)
            layout.addWidget(text_col)
            layout.addStretch(1)
        self.setFixedHeight(AVATAR_SIZE + 2)

    def set_identity(self, identity: MessageIdentity) -> None:
        """更新身份（助手切换 / 主题刷新时调用）"""
        self._avatar.set_identity(identity)
        self._name_label.setText(identity.name or "")

    def set_timestamp(self, timestamp: str) -> None:
        """更新时间显示（空串隐藏）"""
        self._time_label.setText(timestamp or "")
        self._time_label.setVisible(bool(timestamp))

    def apply_text_color(self, color: str) -> None:
        """按卡片主题色刷新名称与时间颜色（refresh_theme 调用）"""
        self._apply_name_style(color)
        self._apply_time_style(color)

    def _apply_name_style(self, color: str = "") -> None:
        """名称样式统一出口：字族/字号走 setFont，QSS 只管颜色。

        ⚠ 两个踩过的坑：
        1. 字号曾经走 QSS 拼接：`font_size_css()` 返回的已是完整声明
           （`font-size: 20px;`），再生拼一层 `font-size:` → `font-size: font-size: 20px;;`，
           非法 CSS 被 Qt 静默丢弃、字号停在默认值。
        2. QSS 的 `font-family` 在 Qt 里解析不到中文字体名（实测 `'楷体'`、
           `楷体`、`KaiTi` 全部落到宋体，而 `QFont("楷体")` 能正确匹配）。
           故字族必须走 `setFont()`，QSS 只负责颜色。
        """
        font = get_unified_font()
        font.setPixelSize(scale_font_size(NAME_FONT_SIZE))
        font.setBold(True)
        self._name_label.setFont(font)
        color_part = f"color: {color};" if color else ""
        self._name_label.setStyleSheet(f"{color_part} background: transparent;")

    def _apply_time_style(self, color: str = "") -> None:
        """时间样式：小字号 + 更浅的字色（弱化，突出名称）。

        同样走 setFont（QSS 解析不到中文字体名），颜色用主题 muted 再降一档透明度。
        """
        font = get_unified_font()
        font.setPixelSize(max(10, scale_font_size(TIME_FONT_SIZE)))
        font.setBold(False)
        self._time_label.setFont(font)
        if color:
            # 名称色是 muted，时间再浅一档：附加 alpha 让它在同色系里更轻
            self._time_label.setStyleSheet(
                f"color: {color}; background: transparent;"
            )
            try:
                from PyQt5.QtWidgets import QGraphicsOpacityEffect

                eff = self._time_label.graphicsEffect()
                if eff is None:
                    eff = QGraphicsOpacityEffect(self._time_label)
                    self._time_label.setGraphicsEffect(eff)
                eff.setOpacity(0.72)
            except Exception:
                pass
        else:
            self._time_label.setStyleSheet("background: transparent;")


__all__ = [
    "AVATAR_SIZE",
    "IdentityAvatar",
    "IdentityHeader",
    "clear_avatar_cache",
    "resolve_avatar_pixmap",
]
