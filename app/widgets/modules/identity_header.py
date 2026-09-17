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

from typing import Dict, Optional, Tuple

from PyQt5.QtCore import QRectF, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPixmap
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

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

# 头像尺寸（与两行文本块等高，兼顾紧凑与辨识度）
# 2026-09-17：32 → 40。源图为 250px 上下，40px 仍在高质量降采样的安全区
# （6:1 以内），既提升辨识度又不引入新的插值损失。
AVATAR_SIZE = 40

# 名称字号（基础值，实际经 scale_font_size 叠加用户字号档位）
NAME_FONT_SIZE = 15

# 时间字号（小于名称、字色更浅，弱化处理）
TIME_FONT_SIZE = 9

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


def _device_pixel_ratio() -> float:
    """取主屏 devicePixelRatio（HiDPI 屏 >1.0），取不到回落 1.0。"""
    try:
        from PyQt5.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return 1.0
        return float(screen.devicePixelRatio()) or 1.0
    except Exception:
        return 1.0


def _pil_resample() -> Optional[int]:
    """取 Pillow 的高质量重采样滤镜（LANCZOS）。

    Pillow ≥9.1 起 ``Image.LANCZOS`` 仍在（``Image.Resampling.LANCZOS`` 的别名），
    兼容 ``Image.Resampling`` 缺失的旧版本。取不到返回 None → 调用方回落 Qt 缩放。
    """
    try:
        from PIL import Image as _Image

        resampling = getattr(_Image, "Resampling", None)
        if resampling is not None:
            return int(getattr(resampling, "LANCZOS"))
        return int(getattr(_Image, "LANCZOS"))
    except Exception:
        return None


def _hq_square(source: "QPixmap", size: int, dpr: float) -> Tuple[Optional["QPixmap"], bool]:
    """把任意尺寸源图高质量降采样为正方形头像。

    **为什么不用 Qt 一步 scaled**：250×250 → 32×32 是 7.8:1 的降采样，
    Qt 的 ``SmoothTransformation`` 在大比例缩放下高频细节丢失明显（实测边缘能量
    stddev 92.9，而 PIL LANCZOS 可达 99.8，肉眼即「发虚」）。逐级折半更糟——
    Qt 每步双线性会累积插值损失（实测反降到 90.8）。故走 Pillow LANCZOS 一次性重采样。

    Args:
        source: 源 pixmap（任意尺寸）
        size: 逻辑边长
        dpr: devicePixelRatio（物理像素 = 逻辑 × dpr；<1 钳制为 1）

    Returns:
        ``(pixmap, ok)``；ok=False 表示 Pillow 不可用，调用方回落 Qt 缩放。
    """
    resample = _pil_resample()
    if resample is None:
        return None, False
    phys = max(1, int(round(size * max(dpr, 1.0))))
    try:
        from PIL import Image as _Image

        image = source.toImage().convertToFormat(QImage.Format_RGBA8888)
        width, height = image.width(), image.height()
        if width <= 0 or height <= 0:
            return None, False
        bits = image.constBits()
        if bits is None:
            return None, False
        bits.setsize(height * width * 4)
        # PyQt5 的 constBits() 返回 sip.voidptr，stub 未声明其 buffer 协议 → 类型债
        pil_image = _Image.frombytes("RGBA", (width, height), bytes(bits))  # type: ignore[arg-type]

        # 居中正方形裁剪（与 KeepAspectRatioByExpanding 语义一致，保留主体）
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        pil_image = pil_image.crop((left, top, left + side, top + side))
        pil_image = pil_image.resize((phys, phys), resample)

        # 直传 RGBA 内存构造 QImage，省掉 PNG 编解码往返（更快、无压缩损失）
        raw = pil_image.tobytes()
        result_image = QImage(bytes(raw), phys, phys, phys * 4, QImage.Format_RGBA8888)
        if result_image.isNull():
            return None, False
        result = QPixmap.fromImage(result_image.copy())
        result.setDevicePixelRatio(dpr)
        return result, True
    except Exception:
        # 静默回落：Pillow 缺依赖 / 打包裁剪 / 图像损坏时，走原 Qt 缩放而非让头像消失
        return None, False


def resolve_avatar_pixmap(identity: MessageIdentity, size: int = AVATAR_SIZE) -> Optional[QPixmap]:
    """解析身份头像为 QPixmap（带缓存）。

    - 图片路径：加载并**高质量**降采样为正方形（Pillow LANCZOS，见 ``_hq_square``）
    - ``builtin:`` 引用：取内置图标
    - 空：返回 None，由调用方画色块 + 首字母

    缓存 key 同时含尺寸与 dpr（HiDPI 与常规屏产出不同，不可混用）。
    """
    avatar = (identity.avatar or "").strip()
    if not avatar:
        return None
    dpr = _device_pixel_ratio()
    cache_key = f"{avatar}@{size}@{dpr:.2f}"
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
                hq, ok = _hq_square(candidate, size, dpr)
                if ok:
                    pixmap = hq
                else:
                    # Pillow 不可用（打包裁剪 / 导入失败）→ 回落原 Qt 缩放，功能不降级
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
            # ⚠️ QPixmap 的 width()/height() 是**设备像素**，self._size 是**逻辑像素**。
            # HiDPI（dpr>1）下二者不等：此前用 `source.width() - self._size` 算偏移、
            # 又用 `min(self._size, source.width())` 当源矩形边长 —— 只取到源图左上
            # 1/(dpr²) 区域，表现为「头像只剩左上 1/4」（2026-09-17 用户反馈）。
            # drawPixmap(目标矩形, pixmap, 源矩形) 重载里两个矩形各归各的单位：
            # 目标=逻辑，源=设备，Qt 自动按 dpr 换算，故这里直接用全图作源矩形。
            painter.drawPixmap(rect, source, QRectF(source.rect()))
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
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(7)

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
        # ⚠️ QLabel 默认 sizeHint 高度含内边距（实测 30px），两行就是 60px，
        # 远超身份行的固定高 → 第二行（时间）被推到可视区外，表现为「时间不显示」。
        # 显式压缩为字高，让两行真能装进固定高度（2026-09-16 用户反馈根因）。
        self._name_label.setFixedHeight(self._name_label.fontMetrics().height())
        self._time_label.setFixedHeight(self._time_label.fontMetrics().height())

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
        # 高度 = max(头像, 两行文本) + 余量。此前写死 AVATAR_SIZE+2，两行文本装不下
        # （名称 30 + 时间 30 > 42）→ 时间标签被裁掉看不见（2026-09-16 用户反馈
        # 「时间只显示一瞬间就消失」的真因：布局压缩后文本落到可视区外）。
        # ⚠️ 余量不可省：字号档位放大后两行需求可能恰好等于算出的高度，任何
        # 亚像素误差都会让第二行（时间）被裁，留 2px 兜底。
        text_h = self._name_label.sizeHint().height() + (
            self._time_label.sizeHint().height() + col.spacing() if timestamp else 0
        )
        self.setFixedHeight(max(AVATAR_SIZE + 2, text_h) + 2)

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

        ⚠️ 弱化只用水色（QSS 的 rgba/浅色），**不用 QGraphicsOpacityEffect**：
        卡片自身有 fade_in 的 QGraphicsOpacityEffect（见 fade_in_widget），
        Qt 在父级已有 effect 时对子级 effect 的合成不可靠 —— 表现为时间标签
        「先显示一瞬间、随后消失」（2026-09-16 用户反馈的真因）。
        同样走 setFont 设字族（QSS 解析不到中文字体名）。
        """
        font = get_unified_font()
        font.setPixelSize(max(10, scale_font_size(TIME_FONT_SIZE)))
        font.setBold(False)
        self._time_label.setFont(font)
        if color:
            # 在同色系里调低不透明度：解析 #rrggbb / rgb() 后附加 alpha
            light = self._with_alpha(color, 0.62)
            self._time_label.setStyleSheet(f"color: {light}; background: transparent;")
        else:
            self._time_label.setStyleSheet("background: transparent;")

    @staticmethod
    def _with_alpha(color: str, alpha: float) -> str:
        """把 #rrggbb / rgb() / rgba() 转成带目标 alpha 的 rgba() 串。

        QColor 不认 CSS 的 rgba 写法（会退化成黑），故手工拼接供 QSS 使用。
        """
        s = str(color or "").strip()
        try:
            if s.startswith("#"):
                hex_part = s[1:]
                if len(hex_part) == 3:
                    hex_part = "".join(c * 2 for c in hex_part)
                r, g, b = (int(hex_part[i : i + 2], 16) for i in (0, 2, 4))
            elif s.lower().startswith(("rgba(", "rgb(")):
                inner = s[s.index("(") + 1 : s.rindex(")")]
                parts = [p.strip() for p in inner.split(",")]
                r, g, b = (int(float(parts[i])) for i in range(3))
            else:
                return s  # 具名颜色（如 red）原样返回
            return f"rgba({r}, {g}, {b}, {alpha:.2f})"
        except Exception:
            return s


__all__ = [
    "AVATAR_SIZE",
    "IdentityAvatar",
    "IdentityHeader",
    "clear_avatar_cache",
    "resolve_avatar_pixmap",
]
