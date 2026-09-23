# -*- coding: utf-8 -*-
"""消息气泡容器：圆角底色块 + 顶部小引脚（指向身份行头像侧）

用户消息引脚在右上（头像在身份行右缘）、助手消息引脚在左上（头像在左缘）。
背景与引脚由 paintEvent 一体自绘，不走样式表：
- 三角引脚与圆角矩形同源同色，无叠加色差；
- 样式表的 QWidget 类型选择器会命中全部后代控件（viewer 容器/正文视图等），
  自绘后子控件保持默认透明，不再被父级 QSS 污染。
"""

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QWidget

from app.widgets.modules.identity_header import _qcolor

# 引脚几何（逻辑像素）
PIN_HEIGHT = 7  # 引脚突出高度
PIN_WIDTH = 13  # 引脚底边宽度
PIN_INSET = 22  # 引脚贴边一侧距气泡边缘的距离（避开 12px 圆角区；尖端 lean 后对齐头像中心）
RADIUS = 12  # 气泡圆角（与原 _user_bubble 样式表的 12px 一致）

# 流式光带参数（与 MessageCard 卡片层原视觉一致：_STREAM_BAND_*）
BAND_H = 3  # 光块高度(px)
BAND_BOTTOM = 3  # 光块距气泡底边(px)
BAND_RATIO = 0.4  # 光块宽度占气泡宽比例

# 气泡可读性保障参数
MIN_L_DIFF = 44  # 与背景 HSL 明度差低于此值视为「看不清」
MIN_L_PUSH = 56  # 明度差不足时推开的距离（半透明叠加会稀释对比，故略高于阈值）
MIN_S = 0.14  # 明度调整后的最低饱和度（仅原有色感时生效，保色相可感知）
MAX_S = 0.30  # 明度调整后的饱和度上限（极简：艳色压柔，不张扬）
# assistant 专属（贴近背景的极简策略：靠边框/引脚辨识，不靠色块）
ASSIST_L_OFFSET = 14  # 明度相对背景的微偏移量
ASSIST_S_MAX = 0.10  # 饱和度上限（压到几乎无色）
ASSIST_ALPHA = 150  # alpha 上限：主题原值普遍 180~225，视觉近实心；压到 150 保半透明质感
PIN_TIP_LEAN = 5  # 引脚尖端向头像外上方偏移量（指向感）
BORDER_WIDTH = 1.0  # 气泡整体细边框宽度(px)


def ensure_bubble_contrast(bg_spec: str, backdrop_spec: str, role: str = "user") -> str:
    """气泡底色可读性保障（2026-09-23）。

    双策略（不改 17 个 yaml，alpha 半透明质感/色相全程保留主题原样）：
    - assistant：贴近背景的极简策略 —— 低饱和（≤ ASSIST_S_MAX）+ 明度微偏移
      （ASSIST_L_OFFSET），颜色不抢戏，辨识靠边框与引脚；
      主题原色再重（如琥珀暖黄）也收敛到背景近邻。
    - user：保留主题色相作身份色，仅当与背景明度过近时拉开，
      饱和度夹在 [MIN_S, MAX_S]。

    Args:
        bg_spec: 主题气泡色（CSS rgba/十六进制），alpha 原样保留。
        backdrop_spec: 聊天背景基准色（Colors.CONTENT_BG）。
        role: "user" / "assistant"（welcome 不调用）。

    Returns:
        rgba(r, g, b, a) 字符串；输入无效时原样返回。
    """
    color = _qcolor(bg_spec, QColor(Qt.transparent))
    if not color.isValid() or color.alpha() == 0:
        return bg_spec
    backdrop = _qcolor(backdrop_spec, QColor(Qt.transparent))

    h, s, l, a = color.getHslF()
    if backdrop.isValid():
        _, _, bl, _ = backdrop.getHslF()
        if role == "assistant":
            # 极简：贴背景微偏移 + 低饱和（无论主题原色多重都收敛到背景近邻）
            l = (bl - ASSIST_L_OFFSET / 255.0) if bl >= 0.5 else (bl + ASSIST_L_OFFSET / 255.0)
            l = min(1.0, max(0.0, l))
            s = min(s, ASSIST_S_MAX)
            out = QColor.fromHslF(h, s, l)
            # 半透明观感：主题 alpha 原值普遍 180~225（视觉近实心），统一钳到上限
            out.setAlpha(min(int(round(a * 255)), ASSIST_ALPHA))
            r, g, b, alpha = out.getRgb()
            return f"rgba({r}, {g}, {b}, {alpha})"
        if abs(l - bl) * 255 < MIN_L_DIFF:
            target = (bl - MIN_L_PUSH / 255.0) if bl >= 0.5 else (bl + MIN_L_PUSH / 255.0)
            l = min(1.0, max(0.0, target))
            if s > 0:  # 无色感（灰/白）不硬加色相，纯灰拉开明度即可
                s = min(max(s, MIN_S), MAX_S)  # 极简：压艳留色相
            out = QColor.fromHslF(h, s, l)
        else:
            out = QColor(color)
    else:
        out = QColor(color)
    out.setAlpha(int(round(a * 255)))
    r, g, b, alpha = out.getRgb()
    return f"rgba({r}, {g}, {b}, {alpha})"


class MessageBubble(QWidget):
    """圆角气泡容器，可带顶部小引脚。

    pin_side: "left" / "right" / None。引脚向上突出，指向气泡上方身份行的头像；
    内容区顶部自动留出引脚高度（contentsMargins），不被引脚遮挡。
    """

    def __init__(self, pin_side: Optional[str] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._pin_side = pin_side
        self._color = QColor(Qt.transparent)
        self._border_color = QColor(Qt.transparent)
        # 流式视觉帧：phase = 三角波相位（0~1，None = 非流式）；tint = 描边/光带色。
        # 由 MessageCard._update_anim 每帧推送（气泡自绘在底色之上，卡片层画会被
        # 气泡不透明底色盖住 —— 2026-09-23 光带不可见根因）。
        self._stream_phase: Optional[float] = None
        self._stream_tint: Optional[QColor] = None
        # ⚠️ 不在这里建 layout：调用方（_setup_user_bubble / _setup_ui）会建自己的
        # layout —— widget 已有 layout 时再 Q-XxxLayout(self) 会产生孤儿 layout，
        # 子控件不再被管理（2026-09-23 用户气泡内容塌缩根因）。
        # 内容边距：顶部含引脚高度 + 视觉呼吸（5px，2026-09-23 收敛消息间距），左右 12、底部 8
        self.setContentsMargins(12, (PIN_HEIGHT + 5) if pin_side else 5, 12, 8)

    def set_bubble_color(self, color: str) -> None:
        """更新气泡底色（幂等；主题切换时由 MessageCard._apply_card_style 调用）

        ⚠️ 颜色串走 _qcolor 安全解析：主题色常是 CSS rgba(...) 格式，
        QColor 直接解析返回无效色，QPainter 拿无效 brush 会画成纯黑。
        """
        c = _qcolor(color, QColor(Qt.transparent))
        if c == self._color:
            return
        self._color = c
        self.update()

    def set_border_color(self, color: str) -> None:
        """设置气泡整体（含引脚）的细边框色（幂等；透明 = 不画）"""
        c = _qcolor(color, QColor(Qt.transparent))
        if c == self._border_color:
            return
        self._border_color = c
        self.update()

    def set_stream_frame(self, phase: Optional[float], tint: Optional[QColor] = None) -> None:
        """设置流式视觉帧（phase=None 退出流式态）。幂等；变化才重绘。"""
        if phase == self._stream_phase and tint == self._stream_tint:
            return
        self._stream_phase = phase
        self._stream_tint = tint
        self.update()

    def paintEvent(self, event):
        if self._color.alpha() == 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        top = PIN_HEIGHT if self._pin_side else 0
        w, h = self.width(), self.height()
        path = QPainterPath()
        path.addRoundedRect(0.0, float(top), float(w), float(max(0, h - top)), RADIUS, RADIUS)

        if self._pin_side == "right":
            px = float(w - PIN_INSET)
            pin = self._make_pin_path(px - PIN_WIDTH, px, top, lean=+PIN_TIP_LEAN)
        elif self._pin_side == "left":
            px = float(PIN_INSET)
            pin = self._make_pin_path(px, px + PIN_WIDTH, top, lean=-PIN_TIP_LEAN)
        else:
            pin = None
        shape = path.united(pin) if pin is not None else path

        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawPath(shape)

        # 整体细边框（含引脚）：气泡轮廓可辨识，避免与背景同色时「隐身」
        if self._border_color.isValid() and self._border_color.alpha() > 0:
            pen = QPen(self._border_color)
            pen.setWidthF(BORDER_WIDTH)
            painter.setPen(pen)
            painter.setBrush(QBrush(Qt.NoBrush))
            painter.drawPath(shape)

        # 流式光带：底部往返（只要动效，不画描边 —— 2026-09-23 用户明确要求）
        if self._stream_phase is not None and self._stream_tint is not None and self._stream_tint.isValid():
            tint = self._stream_tint
            band_w = int(BAND_RATIO * w)
            band_x = int(
                (0.5 * BAND_RATIO + (1.0 - BAND_RATIO) * self._stream_phase) * w - 0.5 * band_w
            )
            band_y = h - BAND_BOTTOM - BAND_H
            band = QLinearGradient(band_x, 0, band_x + band_w, 0)
            e0 = QColor(tint)
            e0.setAlpha(0)
            e1 = QColor(tint)
            e1.setAlpha(170)
            band.setColorAt(0.0, e0)
            band.setColorAt(0.5, e1)
            band.setColorAt(1.0, e0)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(band))
            painter.drawRoundedRect(band_x, band_y, band_w, BAND_H, 1.5, 1.5)
        painter.end()

    @staticmethod
    def _make_pin_path(base_l: float, base_r: float, top: float, lean: float = 0.0) -> QPainterPath:
        """引脚三角：底边贴气泡上缘（base_l→base_r），尖端向头像外上方偏移 lean

        lean 偏移让尖端「斜指向」头像而非垂直朝上，指向感更明确。
        """
        pin = QPainterPath()
        pin.moveTo(base_l, top + 0.5)
        pin.lineTo(base_r, top + 0.5)
        if base_r > base_l:  # right：头像在右，尖向右上偏
            pin.lineTo(base_r + lean, 0.0)
        else:  # left：头像在左，尖向左上偏
            pin.lineTo(base_l + lean, 0.0)
        pin.closeSubpath()
        return pin
