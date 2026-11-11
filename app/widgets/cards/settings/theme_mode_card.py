# -*- coding: utf-8 -*-
"""外观模式设置卡：自动 / 深色 / 浅色 三态自绘分段控件

为什么不用 SwitchSettingCard：深浅是三种状态（跟随系统 / 固定深 / 固定浅），
布尔开关表达不了「自动」，也无法自己说明当前跟的是哪种。

控件规格（与顶栏 tab 同一套交互语言，见 custom_title_bar.TabIndicatorController）：
- 胶囊容器 + 滑动指示器，切换时指示器滑过去而不是淡入淡出
- 每段「手绘图标 + 文字」：太阳 / 月牙 / 半明半暗圆，不依赖图标资源，
  任意字号缩放下不发虚
- 选中段文字 TEXT_PRIMARY，未选中 TEXT_SECONDARY；hover 段叠一层轨道色
- 支持键盘 Left/Right/Home/End 与焦点圈（可 Tab 到达）

颜色全部走 Colors token，refresh_style() 内重读 —— 主题切换后设置卡的
刷新链会调它；字号变化同样经此重算尺寸。
"""

from __future__ import annotations

import math
import re
import weakref

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget
from qfluentwidgets import SettingCard

from app.utils import theme_mode
from app.utils.design_tokens import Colors, scale_font_size
from app.utils.utils import get_icon


def _color(spec: str, fallback: str = "#ffffff") -> QColor:
    """Colors token（hex / rgba()）→ QColor；非法值回退，绝不返回无效色"""
    text = str(spec or "").strip()
    match = re.match(r"rgba?\(([^)]+)\)", text)
    if match:
        parts = [part.strip() for part in match.group(1).split(",")]
        try:
            alpha = float(parts[3]) if len(parts) > 3 else 1.0
            if alpha > 1.0:  # 0~255 写法
                alpha /= 255.0
            return QColor(
                int(float(parts[0])),
                int(float(parts[1])),
                int(float(parts[2])),
                int(max(0.0, min(1.0, alpha)) * 255),
            )
        except Exception:
            return QColor(fallback)
    parsed = QColor(text)
    return parsed if parsed.isValid() else QColor(fallback)


def _with_alpha(color: QColor, alpha: float) -> QColor:
    out = QColor(color)
    out.setAlphaF(max(0.0, min(1.0, alpha)))
    return out


# ── 主题切换重绘：EV_THEME_CHANGED（不是 qconfig.themeChanged）──
# 深色主题之间互切时 qfluentwidgets 的 Theme 枚举不变，themeChanged 不发射；
# 项目自己的 on_theme_changed() 会无条件 publish EV_THEME_CHANGED，才是可靠广播源。
# UIEventBus 强引用回调 → 只订阅模块级函数，实例用 WeakSet 登记。
_SELECTORS: "weakref.WeakSet" = weakref.WeakSet()
_selector_theme_subscribed = False


def _on_selector_theme_changed(payload=None):
    for selector in list(_SELECTORS):
        try:
            selector.update()
        except Exception:  # noqa: BLE001
            pass


def _ensure_selector_theme_subscription():
    global _selector_theme_subscribed
    if _selector_theme_subscribed:
        return
    try:
        from app.core.infra.ui_event_bus import EV_THEME_CHANGED, UIEventBus

        UIEventBus.get_instance().subscribe(EV_THEME_CHANGED, _on_selector_theme_changed)
        _selector_theme_subscribed = True
    except Exception:  # noqa: BLE001
        pass


class ThemeModeSelector(QWidget):
    """三态分段控件（自动 / 深色 / 浅色）"""

    modeChanged = Signal(str)

    _SEGMENTS = (
        (theme_mode.MODE_AUTO, "自动"),
        (theme_mode.MODE_DARK, "深色"),
        (theme_mode.MODE_LIGHT, "浅色"),
    )

    def __init__(self, parent=None, *, compact: bool = False):
        super().__init__(parent)
        # compact：只画图标（菜单行右侧等窄位场景），宽度约为常规版一半
        self._compact = compact
        self._mode = theme_mode.MODE_AUTO
        self._hover_index = -1
        self._indicator_rect = QRectF()
        self._seg_widths: list = []
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_value)
        # 颜色过渡：切走段淡出、新段淡入（与指示器滑动同拍，避免"硬跳"）
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(200)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.valueChanged.connect(self._on_fade_value)
        self._fade_progress = 1.0
        self._fade_index = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("外观模式")
        self.setToolTip("自动：跟随系统深浅色设置；深色 / 浅色：固定配色")
        self._apply_size()
        self.resize(self._ideal_width, self.height())
        # 主题切换自愈：宿主（设置卡 / 菜单行 / 弹窗）漏刷时也不会停在旧配色
        try:
            _SELECTORS.add(self)
            _ensure_selector_theme_subscription()
        except Exception:  # noqa: BLE001
            pass

    # ── 外部接口 ──────────────────────────────────────────

    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str, *, emit: bool = False, animate: bool = True) -> None:
        """更新选中项；emit 只在用户点击时为 True，配置回写走 False 防递归"""
        if mode not in [key for key, _ in self._SEGMENTS]:
            return
        changed = mode != self._mode
        previous_index = self._active_index()
        self._mode = mode
        # ★ 只有真的换段才重定位：点击后配置回写会再调一次 set_mode(同值)，
        # 若无条件 _move_indicator(animate=False) 会把刚起步的滑动动画瞬杀
        if changed or not self._indicator_rect.isValid():
            self._move_indicator(animate=animate and changed)
            if changed and animate:
                self._fade_index = previous_index
                self._fade_progress = 0.0
                self._fade.stop()
                self._fade.start()
            else:
                self._fade_index = -1
                self._fade_progress = 1.0
        self.update()
        if changed and emit:
            self.modeChanged.emit(mode)

    def refresh_style(self) -> None:
        """主题 / 字号变化后重算尺寸并重绘（设置卡刷新链调用）"""
        self._apply_size()
        self.update()

    # ── 尺寸与几何 ────────────────────────────────────────

    def _text_font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(scale_font_size(11))
        font.setWeight(QFont.Medium)
        return font

    def _apply_size(self) -> None:
        """算理想宽度与最小宽度；高度固定，宽度允许被父布局压缩

        ★ 不能用 setFixedSize：菜单行（QPushButton）文字占位后留给右侧的空间
        可能小于理想宽度，固定尺寸的控件会溢出父矩形，第三段被裁掉（真机
        只剩两个图标）。改为 min/max + 按实际宽度均分，空间不足时各段等比
        收缩，三段始终在界内。
        """
        count = len(self._SEGMENTS)
        if self._compact:
            # 段内留白刻意收紧：菜单行里胶囊与左侧文字并排，pad 偏大时整块
            # 显得挤在文字边上（真机反馈「胶囊有点拥挤」）
            icon_w = scale_font_size(11)
            pad = scale_font_size(6)
            seg = icon_w + pad * 2
            ideal = seg * count + 4
            self.setMinimumWidth(int(seg * 0.62) * count + 4)
            self.setMaximumWidth(ideal)
        else:
            metrics = QFontMetrics(self._text_font())
            icon_w = scale_font_size(10)
            gap = scale_font_size(3)
            pad = scale_font_size(7)
            widths = [
                max(scale_font_size(46), metrics.horizontalAdvance(label) + icon_w + gap + pad * 2)
                for _, label in self._SEGMENTS
            ]
            ideal = sum(widths) + 4
            self.setMinimumWidth(int(sum(widths) * 0.7) + 4)
            self.setMaximumWidth(ideal)
        self.setFixedHeight(scale_font_size(26 if self._compact else 28))
        self._ideal_width = ideal
        self._min_width = self.minimumWidth()
        self._seg_widths = [self.width() / float(count)] * count
        self.updateGeometry()

    def sizeHint(self):
        # 布局按 sizeHint 给宽度：不给就会被默认值（minimumSizeHint）压到最小，
        # 表现为控件永远只有最小宽度、图标挤成一团
        return QSize(getattr(self, "_ideal_width", super().sizeHint().width()), self.height())

    def minimumSizeHint(self):
        return QSize(getattr(self, "_min_width", super().minimumSizeHint().width()), self.height())

    def _segment_rects(self) -> list:
        count = len(self._SEGMENTS)
        segment = max(1.0, (self.width() - 4.0) / count)
        height = float(self.height())
        return [QRectF(2.0 + index * segment, 1.0, segment, height - 2.0) for index in range(count)]

    def resizeEvent(self, event):
        # 父布局压缩/拉伸后重新均分，并钉住指示器（尺寸变化不播动画，避免抖动）
        count = len(self._SEGMENTS)
        self._seg_widths = [max(1.0, (self.width() - 4.0) / count)] * count
        self._move_indicator(animate=False)
        super().resizeEvent(event)

    def _active_index(self) -> int:
        for index, (key, _label) in enumerate(self._SEGMENTS):
            if key == self._mode:
                return index
        return 0

    def _index_at(self, pos) -> int:
        for index, rect in enumerate(self._segment_rects()):
            if rect.contains(QPointF(pos)):
                return index
        return -1
    # ── 绘制 ──────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        radius = self.height() / 2.0
        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        border = _color(Colors.BORDER, "#3d3d3d")
        painter.setPen(QPen(_with_alpha(border, 0.65), 1))
        painter.setBrush(_color(Colors.HOVER_BG, "rgba(255,255,255,0.08)"))
        painter.drawRoundedRect(outer, radius, radius)

        rects = self._segment_rects()
        active = self._active_index()

        # hover 段：只给未选中段，选中段靠指示器表达
        if 0 <= self._hover_index < len(rects) and self._hover_index != active:
            painter.setPen(Qt.NoPen)
            painter.setBrush(_with_alpha(_color(Colors.HOVER_BG, "rgba(255,255,255,0.08)"), 0.9))
            painter.drawRoundedRect(
                rects[self._hover_index].adjusted(2, 2, -2, -2), radius - 2, radius - 2
            )

        # 滑动指示器
        if self._indicator_rect.isValid() and not self._indicator_rect.isEmpty():
            fill = _color(Colors.SELECTED_BG, "rgba(255,255,255,0.18)")
            edge = _with_alpha(_color(Colors.TEXT_PRIMARY, "#ffffff"), 0.22)
            painter.setPen(QPen(edge, 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(
                self._indicator_rect,
                self._indicator_rect.height() / 2.0,
                self._indicator_rect.height() / 2.0,
            )

        # 各段图标 + 文字
        font = self._text_font()
        metrics = QFontMetrics(font)
        icon_size = float(scale_font_size(11 if self._compact else 10))
        gap = float(scale_font_size(3))
        for index, ((key, label), rect) in enumerate(zip(self._SEGMENTS, rects)):
            selected = index == active
            # 未选中态用 TEXT_PRIMARY 降透明度派生：部分主题的 text_secondary
            # 只有 0.5~0.6 alpha，叠在同样偏暗的轨道上，细线图标（太阳）几乎
            # 看不见（真机截图只剩两个图标）。
            base = _color(Colors.TEXT_PRIMARY, "#ffffff")
            if selected:
                color = _with_alpha(base, 0.78 + 0.22 * self._fade_progress)
            elif index == self._fade_index:
                color = _with_alpha(base, 1.0 - 0.22 * self._fade_progress)
            elif index == self._hover_index:
                color = _with_alpha(base, 0.95)
            else:
                color = _with_alpha(base, 0.78)

            if self._compact:
                icon_rect = QRectF(
                    rect.center().x() - icon_size / 2.0,
                    rect.center().y() - icon_size / 2.0,
                    icon_size,
                    icon_size,
                )
                self._draw_icon(painter, key, icon_rect, color)
                continue

            text_width = float(metrics.horizontalAdvance(label))
            content_width = icon_size + gap + text_width
            start_x = rect.center().x() - content_width / 2.0
            icon_rect = QRectF(start_x, rect.center().y() - icon_size / 2.0, icon_size, icon_size)
            self._draw_icon(painter, key, icon_rect, color)

            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(
                QRectF(start_x + icon_size + gap, rect.top(), text_width + 2.0, rect.height()),
                int(Qt.AlignVCenter | Qt.AlignLeft),
                label,
            )

        if self.hasFocus():
            painter.setPen(QPen(_with_alpha(_color(Colors.TEXT_ACCENT, "#f59e0b"), 0.8), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(outer.adjusted(1.0, 1.0, -1.0, -1.0), radius - 1, radius - 1)

    @classmethod
    def _draw_icon(cls, painter: QPainter, mode: str, rect: QRectF, color: QColor) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        cx = rect.center().x()
        cy = rect.center().y()
        radius = min(rect.width(), rect.height()) / 2.0
        if mode == theme_mode.MODE_LIGHT:
            cls._draw_sun(painter, cx, cy, radius, color)
        elif mode == theme_mode.MODE_DARK:
            cls._draw_moon(painter, cx, cy, radius, color)
        else:
            cls._draw_auto(painter, cx, cy, radius, color)
        painter.restore()

    @staticmethod
    def _draw_sun(painter: QPainter, cx: float, cy: float, radius: float, color: QColor) -> None:
        core = radius * 0.46
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(cx, cy), core, core)
        painter.setPen(QPen(color, max(1.3, radius * 0.2), Qt.SolidLine, Qt.RoundCap))
        inner = radius * 0.74
        outer = radius * 0.98
        for step in range(8):
            angle = math.pi / 4.0 * step
            dx, dy = math.cos(angle), math.sin(angle)
            painter.drawLine(
                QPointF(cx + dx * inner, cy + dy * inner),
                QPointF(cx + dx * outer, cy + dy * outer),
            )

    @staticmethod
    def _draw_moon(painter: QPainter, cx: float, cy: float, radius: float, color: QColor) -> None:
        disk = QPainterPath()
        disk.addEllipse(QPointF(cx, cy), radius * 0.98, radius * 0.98)
        bite = QPainterPath()
        bite.addEllipse(QPointF(cx + radius * 0.58, cy - radius * 0.32), radius * 0.92, radius * 0.92)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPath(disk.subtracted(bite))

    @staticmethod
    def _draw_auto(painter: QPainter, cx: float, cy: float, radius: float, color: QColor) -> None:
        """半明半暗圆：左半实心（日）、右半只留轮廓（月），表达「自动切换」"""
        circle = QPainterPath()
        circle.addEllipse(QPointF(cx, cy), radius * 0.95, radius * 0.95)
        left_half = QPainterPath()
        left_half.addRect(QRectF(cx - radius * 1.2, cy - radius * 1.2, radius * 1.2, radius * 2.4))
        painter.save()
        painter.setClipPath(left_half)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPath(circle)
        painter.restore()

        painter.setPen(QPen(color, max(1.3, radius * 0.2)))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), radius * 0.95, radius * 0.95)

    # ── 交互 ──────────────────────────────────────────────

    def _on_anim_value(self, value) -> None:
        self._indicator_rect = QRectF(value)
        self.update()

    def _on_fade_value(self, value) -> None:
        self._fade_progress = float(value)
        self.update()

    def _move_indicator(self, *, animate: bool) -> None:
        rects = self._segment_rects()
        if not rects:
            return
        index = min(self._active_index(), len(rects) - 1)
        target = rects[index].adjusted(2.5, 2.5, -2.5, -2.5)
        if not animate or not self._indicator_rect.isValid() or self._indicator_rect.isEmpty():
            self._anim.stop()
            self._indicator_rect = target
            self.update()
            return
        self._anim.stop()
        self._anim.setStartValue(self._indicator_rect)
        self._anim.setEndValue(target)
        self._anim.start()

    def _activate(self, index: int) -> None:
        if not 0 <= index < len(self._SEGMENTS):
            return
        key = self._SEGMENTS[index][0]
        if key != self._mode:
            self.set_mode(key, emit=True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            index = self._index_at(event.pos())
            if index >= 0:
                self._activate(index)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        index = self._index_at(event.pos())
        if index != self._hover_index:
            self._hover_index = index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hover_index != -1:
            self._hover_index = -1
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        index = self._active_index()
        key = event.key()
        if key in (Qt.Key_Left, Qt.Key_Up):
            index -= 1
        elif key in (Qt.Key_Right, Qt.Key_Down):
            index += 1
        elif key == Qt.Key_Home:
            index = 0
        elif key == Qt.Key_End:
            index = len(self._SEGMENTS) - 1
        else:
            super().keyPressEvent(event)
            return
        self._activate(max(0, min(len(self._SEGMENTS) - 1, index)))

    def focusInEvent(self, event):
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.update()
        super().focusOutEvent(event)


class ThemeModeSettingCard(SettingCard):
    """外观模式设置卡：标题 + 说明 + 右侧三态控件"""

    def __init__(self, configItem, parent=None):
        super().__init__(
            get_icon("主题风格"),
            "外观模式",
            "跟随系统，或固定深 / 浅",
            parent,
        )
        self.configItem = configItem
        self.selector = ThemeModeSelector(self)
        self.selector.set_mode(self._config_value(), emit=False, animate=False)
        self.hBoxLayout.addWidget(self.selector, 0, Qt.AlignRight)
        self.hBoxLayout.addSpacing(16)
        self.selector.modeChanged.connect(self._on_mode_changed)
        if configItem is not None:
            configItem.valueChanged.connect(self.setValue)

    def _config_value(self) -> str:
        value = self.configItem.value if self.configItem is not None else theme_mode.MODE_AUTO
        return value if value in theme_mode.MODES else theme_mode.MODE_AUTO

    def _on_mode_changed(self, mode: str) -> None:
        # set_mode 内部负责写两个配置项 + 对齐主题 + 广播窗口刷新；
        # 控件自身已由点击路径更新，这里只兜底同步一次（幂等）
        theme_mode.set_mode(mode)
        self.selector.set_mode(mode, emit=False)

    def setValue(self, value: str) -> None:
        """配置外部变更（云同步 / 快捷菜单）→ 同步控件选中态"""
        mode = value if value in theme_mode.MODES else theme_mode.MODE_AUTO
        self.selector.set_mode(mode, emit=False)

    def refresh_style(self) -> None:
        self.selector.refresh_style()