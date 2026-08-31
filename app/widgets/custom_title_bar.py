"""自定义无边框窗口标题栏

布局：[侧栏开关][DriFox][版本号] ... [中央自定义 tab 区] ... [最小化][最大化][关闭]

设计要点
--------
- **系统按钮自绘**：不用 ``qframelesswindow`` 内置的 ``MinimizeButton``/
  ``MaximizeButton``/``CloseButton`` —— 那三个按钮图标颜色硬编码为纯黑，
  深色主题下完全不可见。本模块改为自绘（Win11 / Fluent 风格细线图标），
  前景/背景色全部取自 ``Colors`` 主题 token，深浅色自适应。
- **主题适配**：``Colors.refresh()`` 后由 ``refresh_style()`` 重建 qss 并重设
  自绘按钮的 QColor 属性。
- **拖拽**：Windows 走系统 ``WM_SYSCOMMAND/SC_MOVE``（原生 Aero Snap 分屏）。
  ★ ``qframelesswindow`` 的 ``startSystemMove`` 固定传 ``lParam=0``，Windows
  会把拖拽锚点当成屏幕左上角，拖到屏幕边缘不触发分屏填充。本模块自行发送
  带光标屏幕坐标的 lParam。
- macOS：隐藏三按钮（系统交通灯渲染于左上），左区预留 70px。
- 顶部 tab 为可注册扩展点：add_tab/remove_tab/set_active_tab + tab_clicked 信号。
"""

import sys
from typing import Callable, Dict, Optional

_IS_MAC = sys.platform == "darwin"

from PyQt5.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QCursor, QIcon, QPainter, QPainterPath
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from qframelesswindow.titlebar import TitleBarBase
from qframelesswindow.titlebar.title_bar_buttons import TitleBarButton, TitleBarButtonState

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon

# ── Windows 原生消息常量（仅 win32 分支使用，模块级常量避免热路径重复定义）──
_WM_SYSCOMMAND = 0x0112
_SC_MOVE = 0xF010
_HTCAPTION = 0x0002

# 白色（深色主题）图标的资源路径模板：关闭按钮在红底上必须用它，
# 浅色主题变体是 #333，红底上几乎不可见。
_WHITE_ICON_PATH = ":/icons/{name}.svg"

# 关闭按钮的 hover / pressed 底色（Windows 系统红，与主题无关）
_CLOSE_HOVER_BG = QColor(232, 17, 35)
_CLOSE_PRESSED_BG = QColor(241, 112, 122)


def _hover_tint() -> tuple:
    """按当前主题明暗给出 hover / pressed 的叠加底色（前景色低透明度）

    浅色主题 → 叠黑（淡灰）；深色主题 → 叠白（淡白）。这样在任何主题下
    都只是一层干净的明暗变化，不会出现"hover 发黑/发脏"的色块。
    """
    try:
        from app.utils.utils import _is_current_theme_light

        light = _is_current_theme_light()
    except Exception:
        light = False

    base = QColor(0, 0, 0) if light else QColor(255, 255, 255)
    hover = QColor(base)
    hover.setAlpha(8 if light else 12)
    pressed = QColor(base)
    pressed.setAlpha(14 if light else 18)
    return hover, pressed


def _qcolor(css: str, fallback: str) -> QColor:
    """把主题 token 的 CSS 颜色串转为 QColor

    token 可能是 ``#ffffff`` / ``rgba(255, 255, 255, 0.5)`` 等形式；解析失败时
    回退到 fallback，保证任何主题下按钮图标都可见（不出现黑叠黑）。

    ★ 必须用 ``qcolor_from_token``，不能直接 ``QColor(css)``：
    PyQt5 的 QColor 构造函数不认 CSS 的 ``rgba(r,g,b,a)`` 写法，会静默
    返回 invalid 的黑色 QColor，导致深色主题（text_secondary 为
    ``rgba(..., 0.74)``）下 tab 未选中的文字渲染成黑叠黑，肉眼"看不见"。
    """
    from app.utils.design_tokens import qcolor_from_token

    c = qcolor_from_token(css)
    # qcolor_from_token 在解析失败时返回 QColor(33, 33, 38, 246)（不会 invalid）
    # 但仍按 fallback 语义优先：解析失败时回退到 fallback
    if c.alpha() == 255 and c.red() == 33 and c.green() == 33 and c.blue() == 38:
        # 仅在主值"明显解析失败"时尝试 fallback（避免主题恰好用同色误判）
        try:
            # 主值若本身就是合法 CSS 颜色，qcolor_from_token 不会走到 fallback 分支
            # 简单判别：QColor(css).isValid() 为真就直接用主值
            if QColor(css).isValid():
                return c
        except Exception:
            pass
        fb = qcolor_from_token(fallback)
        # 同样：fallback 若 invalid，qcolor_from_token 也只会返回上面那个默认深灰
        # 这里再保险一次：如果两者都解析不出来，给一个不会黑叠黑的安全色
        if fb.alpha() == 255 and fb.red() == 33 and fb.green() == 33 and fb.blue() == 38:
            return QColor(255, 255, 255, 200)  # 浅灰半透明，保证不黑叠黑
        return fb
    return c


class _BaseWinButton(TitleBarButton):
    """系统按钮基类：SVG 图标渲染 + 主题 token 配色

    图标取自 ``icons/窗体-*.svg``（1024 视图），经 ``get_icon()`` 的
    ``_ThemeIconEngine`` 按当前主题自动选深色（白）或浅色（#333）版本，
    无需在代码里判断主题，主题切换后 Qt pixmap 缓存自动失效重取。

    ★ 为什么 ``ICON_SIZE=12``：SVG 的线宽约占视图 8.4%，12px 下正好
    ≈1px，与 Win11 标题栏图标的视觉重量一致；图标外接方框 ≈10px，
    与自绘时代的 10x10 视口保持同样的视觉尺寸。

    配色策略（对齐 Win11）：三态**图标同色**，状态差异只由背景色表达。
    浅色主题下若让 hover 切到 ``TEXT_PRIMARY``（如 #0f172a），图标会在
    hover 瞬间变成"全黑"块，观感很重；改为常态取 ``TEXT_SECONDARY``
    （主题里的柔和次级色），hover/pressed 只加深底色。
    """

    ICON_NAME = ""  # 图标名（不含扩展名），子类覆盖
    ICON_SIZE = 12  # 图标渲染边长（px）

    #: 按钮占满标题栏高度（不留上下边距），hover 区域才能贴齐窗口顶边，
    #: 关闭按钮的圆角也才能与窗口圆角同心。
    HEIGHT = 38
    WIDTH = 46

    @property
    def ICON(self) -> int:  # noqa: N802 - 兼容旧命名
        return self.ICON_SIZE

    def __init__(self, parent=None, *, danger: bool = False):
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self._danger = danger
        self.apply_theme_colors()

    def apply_theme_colors(self) -> None:
        """按当前主题的明暗重设四态颜色

        ★ hover 底色**不用** ``Colors.HOVER_BG``：那是"内容区"的悬停色，
        各主题取值差异很大（深色主题里可能是带饱和度的蓝/紫的 12%），
        铺在窗口标题栏这块大面积上会显脏；而且部分主题给了接近黑的取值，
        直接表现为"hover 变黑块"。这里改为对齐 Win11 的做法——**用前景色的
        低透明度叠加**：浅色主题叠黑、深色主题叠白，任何主题下都是干净的
        一层轻灰/轻白。
        """
        fg = _qcolor(Colors.TEXT_SECONDARY, "rgba(255,255,255,0.65)")
        hover_bg, pressed_bg = _hover_tint()

        self.setNormalColor(fg)
        self.setHoverColor(fg)
        self.setPressedColor(fg)
        self.setNormalBackgroundColor(QColor(0, 0, 0, 0))
        if self._danger:
            # 关闭：hover/pressed 走系统红 + 纯白图标（保持 Windows 习惯）
            self.setHoverColor(QColor(255, 255, 255))
            self.setPressedColor(QColor(255, 255, 255))
            self.setHoverBackgroundColor(_CLOSE_HOVER_BG)
            self.setPressedBackgroundColor(_CLOSE_PRESSED_BG)
        else:
            self.setHoverBackgroundColor(hover_bg)
            self.setPressedBackgroundColor(pressed_bg)

    # ── 绘制工具 ──

    def _begin(self) -> tuple:
        """准备画笔：返回 (painter, color, bgColor, cx, cy)"""
        painter = QPainter(self)
        # SVG 图标缩放渲染依赖抗锯齿，否则斜边（关闭的 ×）会呈锯齿状
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        color, bg_color = self._getColors()
        return painter, color, bg_color, self.width() / 2.0, self.height() / 2.0

    def _draw_bg(self, painter: QPainter, bg_color: QColor) -> None:
        painter.setBrush(bg_color)
        painter.setPen(Qt.NoPen)
        painter.drawRect(self.rect())

    def paintEvent(self, e):  # pragma: no cover - 纯绘制
        painter, color, bg_color, cx, cy = self._begin()
        self._draw_bg(painter, bg_color)
        self._paint_icon(painter, color, bg_color, cx, cy)

    def _icon(self) -> QIcon:
        """当前应显示的图标

        ``get_icon()`` 内部用 ``_ThemeIconEngine``，按当前主题返回深色（白）
        或浅色（#333）版本；主题切换后 Qt 的 pixmap 缓存按 key 自动失效。
        """
        return get_icon(self._icon_name())

    def _icon_name(self) -> str:
        return self.ICON_NAME

    def _paint_icon(self, painter: QPainter, color: QColor, bg_color: QColor, cx: float, cy: float) -> None:
        s = self.ICON_SIZE
        target = QRect(int(cx - s / 2), int(cy - s / 2), s, s)
        self._icon().paint(painter, target, Qt.AlignCenter)


class MinimizeButton(_BaseWinButton):
    """最小化"""

    ICON_NAME = "窗体-最小化"


class MaximizeButton(_BaseWinButton):
    """最大化 / 还原：图标随窗口状态切换"""

    def __init__(self, parent=None, *, danger: bool = False):
        self._isMax = False
        super().__init__(parent, danger=danger)

    def setMaxState(self, isMax: bool) -> None:
        """由 TitleBarBase.eventFilter 在 WindowStateChange 时调用"""
        if self._isMax == isMax:
            return
        self._isMax = isMax
        self.update()

    def _icon_name(self) -> str:
        # 图标表达"点击后会发生什么"：未最大化 → 显示最大化；已最大化 → 显示还原
        return "窗体-向下还原" if self._isMax else "窗体-最大化"


class CloseButton(_BaseWinButton):
    """关闭：normal 用主题图标，hover/pressed（红底）强制白色图标

    额外处理圆角：本按钮紧贴窗口右上角，而窗口是 DWM 圆角。若 hover 底色
    画成直角矩形，右上角会"戳出"窗口圆角之外，看起来和窗口对不齐。
    这里把**右上角**裁成与窗口相同的圆角（最大化/全屏时窗口无圆角 → 直角）。
    """

    ICON_NAME = "窗体-关闭"

    #: 与 DWM 圆角（Win11 DWMCP_ROUND）一致
    WINDOW_CORNER = 8

    def _icon(self) -> QIcon:
        if self._state == TitleBarButtonState.NORMAL:
            return get_icon(self.ICON_NAME)
        # 红底上必须用白色版本：浅色主题的 #333 变体在红底上几乎看不见
        icon = QIcon(_WHITE_ICON_PATH.format(name=self.ICON_NAME))
        return icon if not icon.isNull() else get_icon(self.ICON_NAME)

    def _corner_radius(self) -> int:
        """窗口右上角的圆角半径（最大化 / 全屏时为 0）"""
        win = self.window()
        if win is not None and (win.isMaximized() or win.isFullScreen()):
            return 0
        return min(self.WINDOW_CORNER, self.width(), self.height())

    def _draw_bg(self, painter: QPainter, bg_color: QColor) -> None:
        if bg_color.alpha() == 0:
            return  # normal 态全透明，不必绘制
        r = self._corner_radius()
        if r <= 0:
            super()._draw_bg(painter, bg_color)
            return

        painter.setBrush(bg_color)
        painter.setPen(Qt.NoPen)
        path = QPainterPath()
        w, h = self.width(), self.height()
        path.moveTo(0, 0)
        path.lineTo(w - r, 0)
        # 右上角圆弧：外接正方形 (w-2r, 0, 2r, 2r)，从 90°(顶) 顺时针到 0°(右)
        path.arcTo(QRectF(w - 2 * r, 0, 2 * r, 2 * r), 90, -90)
        path.lineTo(w, h)
        path.lineTo(0, h)
        path.closeSubpath()
        painter.fillPath(path, bg_color)


class CustomTabButton(QWidget):
    """顶栏 tab（分段控件风格）：自绘胶囊底 + 状态过渡动画

    ★ 为什么自绘而不用 stylesheet：stylesheet 只能做离散状态切换，做不了
    进度插值，hover 淡入淡出只能靠 CSS transition（Qt 不支持）。这里把状态
    量化成两个 0..1 的进度（``_hover_t`` / ``_active_t``），由
    QVariantAnimation 驱动，paintEvent 按当前进度插值绘制。

    选中态的表达（三档拉开，不靠花哨的指示条）：

    ============  =====================================================
    未选中         透明底，文字 TEXT_SECONDARY，400 字重
    hover          前景色 6% 淡入，文字提亮到半程
    选中           前景色 14% 底，文字 TEXT_PRIMARY，600 字重
    ============  =====================================================

    可选关闭钮（closable=True）：右侧 × 子按钮；点击只发射 ``close_clicked``，
    不触发 tab 切换（对齐 ReplaceTabButton 的事件模式：子控件事件不传播给
    父 widget，× 点击天然不会触发整卡点击）。
    """

    clicked = pyqtSignal(str)  # tab_id（整卡点击）
    close_clicked = pyqtSignal(str)  # tab_id（× 关闭钮）

    HEIGHT = 28  # Windows / Linux（标题栏 38，容器上下各留 5）
    #: macOS 用 22：标题栏只有 28pt，tab 加容器 3px 内边距正好铺满
    MAC_HEIGHT = 22

    #: 圆角取 BorderRadius.SM(6px)：窗口圆角是 8px，但那是相对整窗尺寸的；
    #: 放在 28px 高的 tab 上 8px 会显得过圆。6px 是"和窗口同一弧度语言、
    #: 但按控件尺寸收敛"的结果。
    RADIUS = 6

    # 叠加不透明度（相对前景色）：三档拉开，取消指示条后靠底+字重区分选中
    ALPHA_HOVER = 0.06
    ALPHA_ACTIVE = 0.14

    ANIM_MS = 180

    def __init__(
        self,
        tab_id: str,
        text: str,
        parent: Optional[QWidget] = None,
        *,
        closable: bool = False,
        icon_path: str = "",
    ):
        super().__init__(parent)
        self.tab_id = tab_id
        self._closable = closable
        self._active = False
        self._hovered = False
        # 动画进度（0..1），paintEvent 按此插值
        self._hover_t = 0.0
        self._active_t = 0.0
        # 文字色 QSS 缓存键（量化进度 + 字重 + 主题色），见 _apply_label_color
        self._label_css_key = None
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(self.MAC_HEIGHT if _IS_MAC else self.HEIGHT)
        self.setAttribute(Qt.WA_Hover, True)  # 确保 enter/leave 事件可达

        layout = QHBoxLayout(self)
        layout.setContentsMargins(11, 0, 4 if closable else 11, 0)
        layout.setSpacing(2)
        self._icon_label: Optional[QLabel] = None
        if icon_path:
            self._icon_label = QLabel(self)
            self._icon_label.setPixmap(QIcon(icon_path).pixmap(14, 14))
            self._icon_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(self._icon_label)
        self._label = QLabel(text, self)
        # 鼠标事件穿透到父 widget，使整卡点击触发切换
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._label)
        self._close_btn: Optional[QPushButton] = None
        if closable:
            self._close_btn = QPushButton("×", self)
            self._close_btn.setFixedSize(16, 16)
            self._close_btn.setCursor(Qt.PointingHandCursor)
            self._close_btn.clicked.connect(lambda: self.close_clicked.emit(self.tab_id))
            layout.addWidget(self._close_btn)

        self._anim_hover = self._make_anim(self._on_hover_value)
        self._anim_active = self._make_anim(self._on_active_value)
        self.refresh_style()

    # ── 动画 ──

    def _make_anim(self, slot) -> QVariantAnimation:
        anim = QVariantAnimation(self)
        anim.setDuration(self.ANIM_MS)
        anim.setEasingCurve(QEasingCurve.InOutQuad)
        anim.valueChanged.connect(slot)
        return anim

    @staticmethod
    def _start(anim: QVariantAnimation, current: float, target: float) -> None:
        """从当前进度续接到目标进度（避免连续 hover 时跳变）"""
        if abs(current - target) < 0.001:
            return
        anim.stop()
        anim.setStartValue(current)
        anim.setEndValue(target)
        anim.start()

    def _on_hover_value(self, value) -> None:
        self._hover_t = float(value)
        self._apply_label_color()
        self.update()

    def _on_active_value(self, value) -> None:
        self._active_t = float(value)
        self._apply_label_color()
        self.update()

    # ── 交互 ──

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.tab_id)
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:
        self.set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.set_hover(False)
        super().leaveEvent(event)

    # ── 状态写入（幂等 + 可自愈）──
    #
    # 两个 set_* 都是「意图去重 + 进度收敛」，而不是早期版本的「值相同即
    # return」：动画可能被新事件打断而停在中途（_hover_t / _active_t 停在
    # 0.4 之类），此时若因为「意图没变」就什么都不做，视觉状态会永久错位
    # ——这正是「高亮/悬浮赖在 tab 上退不掉」的根因。
    # 用 Running 状态兜住：动画在跑时绝不重启，否则缓动曲线被反复重置，
    # 进度永远走不到 1.0（表现为 hover 淡入淡出永远差一口气）。

    @staticmethod
    def _settled(current: float, target: float) -> bool:
        """进度是否已就位（收尾误差 0.001 内视作到位）"""
        return abs(current - target) < 0.001

    def set_hover(self, hovered: bool) -> None:
        """设置悬浮态（幂等；进度卡在中途会自动补一次收尾）"""
        target = 1.0 if hovered else 0.0
        if self._hovered == hovered and (
            self._anim_hover.state() == QAbstractAnimation.Running
            or self._settled(self._hover_t, target)
        ):
            return
        self._hovered = hovered
        self._start(self._anim_hover, self._hover_t, target)
        self._apply_label_color()

    def set_active(self, active: bool) -> None:
        """切换选中态：底色 / 文字 / 字重全部走动画（幂等 + 自愈）"""
        target = 1.0 if active else 0.0
        if self._active == active and (
            self._anim_active.state() == QAbstractAnimation.Running
            or self._settled(self._active_t, target)
        ):
            return
        self._active = active
        self._start(self._anim_active, self._active_t, target)
        self._apply_label_color()

    # ── 绘制 ──

    def _fg(self) -> QColor:
        """前景基色（用于叠加出 hover/active 底色与文字色插值）"""
        return _qcolor(Colors.TEXT_PRIMARY, "#ffffff")

    def _bg_alpha(self) -> float:
        """当前底色不透明度：选中与 hover 取较大者，不叠加（避免过深）"""
        return min(1.0, max(self._active_t * self.ALPHA_ACTIVE, self._hover_t * self.ALPHA_HOVER))

    def paintEvent(self, e):  # pragma: no cover - 纯绘制
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)

        # 只画胶囊底色：选中态靠"底更深 + 文字更亮更粗"表达
        # （早期版本在 tab 下方画过 accent 指示条，反馈太花，已移除）
        alpha = self._bg_alpha()
        if alpha > 0.002:
            c = self._fg()
            c.setAlphaF(alpha)
            painter.setBrush(c)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)

    def refresh_style(self):
        """按当前主题 token 刷新（Colors.refresh() 由调用方负责）

        不重置动画进度——主题切换时保持当前 hover/active 视觉状态。
        """
        self._apply_label_color()
        if self._close_btn is not None:
            self._close_btn.setStyleSheet(
                f"QPushButton {{ color: {Colors.TEXT_MUTED}; background: transparent;"
                " border: none; border-radius: 8px; font-size: 13px; padding: 0; }"
                f"QPushButton:hover {{ color: {Colors.TEXT_PRIMARY};"
                f" background: {Colors.HOVER_BG}; }}"
            )
        self.update()

    #: 文字色插值量化步长。``setStyleSheet`` 会触发整棵子树的 QSS 重解析 +
    #: polish，在 180ms 动画里逐帧调用是标题栏掉帧的主要来源；量化到 1/64
    #: 色阶（肉眼无差）后，一次状态切换的 setStyleSheet 次数从 ~11 降到 ~4。
    COLOR_STEPS = 64

    def _apply_label_color(self) -> None:
        """按进度插值文字颜色：未选中 → hover(半程) → 选中(全程)

        结果按「量化进度 + 字重 + 主题色」缓存，key 不变则跳过 setStyleSheet。
        主题色进了 key，所以 refresh_style() 走同一条路径也能正确失效。
        """
        if not hasattr(self, "_label"):
            return
        from_c = _qcolor(Colors.TEXT_SECONDARY, "rgba(255,255,255,0.65)")
        to_c = _qcolor(Colors.TEXT_PRIMARY, "#ffffff")
        t = min(1.0, max(self._active_t, self._hover_t * 0.5))
        weight = 600 if self._active else 400
        q = int(t * self.COLOR_STEPS + 0.5)
        key = (q, weight, from_c.rgb(), to_c.rgb())
        if key == self._label_css_key:
            return
        self._label_css_key = key
        t = q / self.COLOR_STEPS
        r = int(round(from_c.red() + (to_c.red() - from_c.red()) * t))
        g = int(round(from_c.green() + (to_c.green() - from_c.green()) * t))
        b = int(round(from_c.blue() + (to_c.blue() - from_c.blue()) * t))
        self._label.setStyleSheet(
            f"color: rgb({r}, {g}, {b}); background: transparent;"
            f" {get_font_family_css()} {font_size_css(13)}; font-weight: {weight};"
        )


class CustomTitleBar(TitleBarBase):
    """无边框窗口自定义标题栏（现代化自绘版）

    TitleBarBase 已提供：双击最大化/还原、拖拽判定（``canDrag`` 自动排除
    右侧系统按钮区）。本类负责三区布局、自绘系统按钮与 tab 扩展 API。

    注意：基类的三个内置按钮（纯黑硬编码图标）在 ``__init__`` 中被本模块的
    自绘按钮替换；``minBtn`` / ``maxBtn`` / ``closeBtn`` 属性名保持不变，
    外部（含基类的 ``eventFilter``）无需改动。
    """

    HEIGHT = 38  # Windows / Linux
    #: macOS 用 28 —— 与 NSWindow 标题栏容器同高。系统交通灯由 AppKit 绘制，
    #: 尺寸固定（约 12pt），放进 38pt 高的栏里比例会明显偏小；设为 28 才是
    #: macOS 的标准观感。
    MAC_HEIGHT = 28
    MAC_TRAFFIC_LIGHT_PAD = 70  # macOS 系统交通灯左侧留白

    tab_clicked = pyqtSignal(str)
    tab_close_clicked = pyqtSignal(str)
    sidebar_toggle_requested = pyqtSignal()
    workbench_toggle_requested = pyqtSignal()  # 右侧工作台浮层开关（按钮在最小化左侧）

    def __init__(self, parent):
        self._is_mac: bool = sys.platform == "darwin"
        super().__init__(parent)
        self._tabs: Dict[str, CustomTabButton] = {}
        self._active_id: Optional[str] = None
        # hover 重算合并标志（见 _schedule_tab_hover_sync）
        self._hover_sync_pending = False

        self.setFixedHeight(self.MAC_HEIGHT if self._is_mac else self.HEIGHT)

        # ── 用自绘主题按钮替换基类内置的黑色老式按钮 ──
        self._replace_system_buttons()

        # ── 左区：仅侧栏开关（透明无边框，图标主题感知） ──
        # 品牌（DriFox + 版本号）已移除：顶栏走极简，只保留功能控件。
        self._sidebar_btn = QPushButton(self)
        self._sidebar_btn.setIcon(get_icon("侧边栏"))
        self._sidebar_btn.setFixedSize(30, 28)
        self._sidebar_btn.setIconSize(QSize(17, 17))
        self._sidebar_btn.setCursor(Qt.PointingHandCursor)
        self._sidebar_btn.setToolTip("收起/展开侧边栏")
        self._sidebar_btn.clicked.connect(self.sidebar_toggle_requested.emit)

        # ── 右区：工作台浮层开关（右侧边栏图标，位于最小化按钮左侧）──
        # mac 上系统交通灯由 NSWindow 提供且系统按钮隐藏，此按钮仍保留（浮层开关独立于系统按钮）
        self._workbench_btn = QPushButton(self)
        self._workbench_btn.setIcon(get_icon("右侧边栏"))
        self._workbench_btn.setFixedSize(30, 28)
        self._workbench_btn.setIconSize(QSize(17, 17))
        self._workbench_btn.setCursor(Qt.PointingHandCursor)
        self._workbench_btn.setToolTip("打开/关闭工作台")
        self._workbench_btn.clicked.connect(self.workbench_toggle_requested.emit)

        # ── 中央区：tab 容器 ──
        self._tab_container = QWidget(self)
        self._tab_container.setObjectName("titlebarTabSegment")
        self._tab_layout = QHBoxLayout(self._tab_container)
        self._tab_layout.setContentsMargins(3, 3, 3, 3)
        self._tab_layout.setSpacing(0)
        # 无 tab 时隐藏空容器（add/remove_tab 时同步）
        self._tab_container.hide()

        # ── 左右平衡占位（见 _sync_tab_centering 的说明）──
        self._left_balance = QWidget(self)
        self._right_balance = QWidget(self)
        for w in (self._left_balance, self._right_balance):
            w.setFixedWidth(0)
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # ── 三区布局 ──
        # mac：左侧留白给系统交通灯；Windows：常规 8px
        left_pad = self.MAC_TRAFFIC_LIGHT_PAD if self._is_mac else 8
        layout = QHBoxLayout(self)
        # 上下 margin 必须为 0：系统按钮高度 = 标题栏高度，hover 底色才能
        # 贴齐窗口顶边，关闭按钮的圆角也才能与窗口圆角同心（否则圆角差 3px，
        # 视觉上就是"hover 和窗口对不齐"）。
        layout.setContentsMargins(left_pad, 0, 0, 0)
        layout.setSpacing(4)
        # AlignVCenter 保证左侧 30x28 折叠钮在 38px 栏内垂直居中
        layout.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        layout.addWidget(self._sidebar_btn)
        layout.addWidget(self._left_balance)
        layout.addStretch(1)
        layout.addWidget(self._tab_container, 0, Qt.AlignVCenter)
        layout.addStretch(1)
        layout.addWidget(self._right_balance)
        if not self._is_mac:
            layout.addWidget(self._workbench_btn, 0, Qt.AlignRight)
            layout.addWidget(self.minBtn, 0, Qt.AlignRight)
            layout.addWidget(self.maxBtn, 0, Qt.AlignRight)
            layout.addWidget(self.closeBtn, 0, Qt.AlignRight)
        else:
            layout.addWidget(self._workbench_btn, 0, Qt.AlignRight)
            self.minBtn.hide()
            self.maxBtn.hide()
            self.closeBtn.hide()
            # ★ macOS 的系统交通灯（关闭/最小化/最大化）由 NSWindow 提供，
            # 但 qframelesswindow 的 updateFrameless() 默认把它关掉了
            # （_isSystemButtonVisible=False），必须显式打开才显示得出来。
            try:
                self.window().setSystemTitleBarButtonVisible(True)
            except Exception:
                pass

        self.refresh_style()
        self._sync_tab_centering()

    # ── 布局 ──

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_tab_centering()
        # 居中占位随宽度变化 → 整组 tab 平移，光标静止时 Qt 不补发 enter/leave
        self._schedule_tab_hover_sync()

    def _sync_tab_centering(self) -> None:
        """让中央 tab 容器真正落在窗口正中

        两个等分 stretch 只在**两侧固定占位等宽**时才能让中间控件居中。实际
        布局并不对称：左边是折叠钮（约 30，mac 上还要加上交通灯留白 70），
        右边是三个系统按钮（46×3 + 间距，mac 上为 0）。窄的一侧缺少的那一截
        会让 tab 中心整体偏向它——Windows 上约偏左 52px。

        这里给窄的一侧补一个等宽占位 widget，使两侧固定宽度相等。
        """
        layout = self.layout()
        if layout is None:
            return
        spacing = layout.spacing()

        left = self._sidebar_btn.width() + layout.contentsMargins().left()
        right = self._workbench_btn.width()
        if not self._is_mac:
            right += (
                self.minBtn.width() + self.maxBtn.width() + self.closeBtn.width() + spacing * 3
            )
        else:
            right += spacing

        if left < right:
            left_pad, right_pad = right - left, 0
        else:
            left_pad, right_pad = 0, left - right

        for widget, width in ((self._left_balance, left_pad), (self._right_balance, right_pad)):
            if widget.width() != width:
                widget.setFixedWidth(width)
        # 占位宽度变化会让整组 tab 平移，重算 hover
        self._schedule_tab_hover_sync()

    # ── 系统按钮 ──

    def _replace_system_buttons(self) -> None:
        """用自绘按钮替换 TitleBarBase 内置的纯黑图标按钮

        基类在 ``__init__`` 中创建了 ``MinimizeButton`` / ``MaximizeButton`` /
        ``CloseButton`` 并已连接 ``window().showMinimized/close``。此处整体替换，
        因此必须重新连线；旧的先 hide（``canDrag`` 的拖拽热区按可见按钮宽度
        计算，hide 后不再占位）再延迟销毁。
        """
        old = (self.minBtn, self.maxBtn, self.closeBtn)

        self.minBtn = MinimizeButton(self)
        self.maxBtn = MaximizeButton(self)
        self.closeBtn = CloseButton(self, danger=True)

        for btn in old:
            btn.hide()
            btn.setParent(None)
            btn.deleteLater()

        self.minBtn.clicked.connect(self.window().showMinimized)
        self.maxBtn.clicked.connect(self._toggle_max_state)
        self.closeBtn.clicked.connect(self.window().close)

    def _toggle_max_state(self) -> None:
        """最大化 / 还原切换（基类同名方法为私有，无法复用）"""
        win = self.window()
        if win.isMaximized():
            win.showNormal()
        else:
            win.showMaximized()

    # ── 拖拽：修正 lParam，恢复原生 Aero Snap ──

    def mousePressEvent(self, e):
        if sys.platform == "win32" or not self.canDrag(e.pos()):
            return super().mousePressEvent(e)
        self._start_system_move(e.globalPos())

    def mouseMoveEvent(self, e):
        if sys.platform != "win32" or not self.canDrag(e.pos()):
            return super().mouseMoveEvent(e)
        self._start_system_move(e.globalPos())

    def _start_system_move(self, global_pos) -> None:
        """发起系统级窗口移动（支持拖到屏幕边缘触发分屏填充）

        ``qframelesswindow.utils.startSystemMove`` 固定传 ``lParam=0``，Windows
        会把拖拽锚点当作屏幕左上角，Aero Snap / 分屏布局因此失效。这里自行发送
        ``WM_SYSCOMMAND``，lParam 低 16 位=光标 x、高 16 位=光标 y（屏幕坐标），
        系统据此计算拖拽偏移并在边缘触发 snap。
        """
        try:
            import ctypes
            from ctypes import wintypes

            win = self.window()
            if win is None:
                return
            hwnd = int(win.winId())
            lparam = (global_pos.y() << 16) | (global_pos.x() & 0xFFFF)
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(
                wintypes.HWND(hwnd), _WM_SYSCOMMAND, _SC_MOVE | _HTCAPTION, wintypes.LPARAM(lparam)
            )
        except Exception:
            # 失败不阻断 UI；退化到基类行为（可移动但没有 snap）
            self._fallback_system_move(global_pos)

    def _fallback_system_move(self, global_pos) -> None:
        """兜底：走 qframelesswindow 内置实现（无 snap，但保证能拖动）"""
        try:
            from qframelesswindow.utils import startSystemMove

            startSystemMove(self.window(), global_pos)
        except Exception:
            pass

    # ── tab 扩展 API ──

    def add_tab(
        self,
        tab_id: str,
        text: str,
        on_click: Optional[Callable] = None,
        *,
        closable: bool = False,
        icon_path: str = "",
    ) -> None:
        """注册顶部 tab；首个注册的 tab 自动激活

        Args:
            closable: True 时带 × 关闭钮（full 卡片等非常驻 tab）；
                      关闭钮点击发射 ``tab_close_clicked(tab_id)``，不切 tab。
            icon_path: 可选图标资源路径（按钮内左侧 14px 图标）。
        """
        if tab_id in self._tabs:
            return
        btn = CustomTabButton(
            tab_id, text, self._tab_container, closable=closable, icon_path=icon_path
        )
        btn.clicked.connect(self._on_tab_clicked)
        btn.close_clicked.connect(self.tab_close_clicked.emit)
        if on_click is not None:
            btn.clicked.connect(lambda _tid: on_click())
        self._tab_layout.addWidget(btn)
        self._tabs[tab_id] = btn
        btn.refresh_style()
        self._tab_container.setVisible(True)
        if self._active_id is None:
            self.set_active_tab(tab_id)
        # 新增 tab 会让后面的 tab 右移；若光标正好落在某个新位置上，
        # Qt 不会补发 enter/leave
        self._schedule_tab_hover_sync()

    def remove_tab(self, tab_id: str) -> None:
        """移除 tab；若移除的是激活 tab 则自动激活剩余第一个"""
        btn = self._tabs.pop(tab_id, None)
        if btn is None:
            return
        self._tab_layout.removeWidget(btn)
        btn.deleteLater()
        if not self._tabs:
            self._tab_container.setVisible(False)
        if self._active_id == tab_id:
            self._active_id = None
            if self._tabs:
                self.set_active_tab(next(iter(self._tabs)))
        # 剩余 tab 会平移到光标下；被删的 tab 若正处于 hover 态，它的
        # leaveEvent 永远不会到达（对象已被 deleteLater）
        self._schedule_tab_hover_sync()

    def set_active_tab(self, tab_id: str) -> None:
        """设置激活 tab（胶囊高亮）

        全量遍历而非只改差异项：``CustomTabButton.set_active`` 自带「去重 +
        收敛」，重复设置同一项不会重启动画，但会把**卡在中途的进度**补回
        目标——这是高亮与当前状态错位后的自愈通道。
        """
        if tab_id not in self._tabs:
            return
        self._active_id = tab_id
        for tid, b in self._tabs.items():
            b.set_active(tid == tab_id)

    # ── hover 仲裁 ──

    def sync_tab_hover(self) -> None:
        """按光标真实位置重新仲裁 hover（自愈入口，可安全重复调用）

        ★ 为什么不能只靠 enter/leave：``add_tab`` / ``remove_tab`` /
        ``_sync_tab_centering`` 都会在**光标静止**时让整组 tab 平移，而 Qt
        既不为「被移到光标下」的 widget 补发 enterEvent、也不为「被移走」的
        widget 补发 leaveEvent。两种后果：

        - 鼠标压在新 tab 上却不亮（缺 enter → 与当前不同步）
        - 底色糊在被删掉的 tab 上 / 相邻 tab 一直亮着（缺 leave → 卡住）

        这里显式按 ``QCursor.pos()`` 做命中测试，天然保证任意时刻**至多一个**
        tab 处于 hover，并在每次布局变化后立刻重算。
        """
        if not self._tabs or not self._tab_container.isVisible():
            return
        global_pos = QCursor.pos()
        hit_id: Optional[str] = None
        if self._tab_container.rect().contains(self._tab_container.mapFromGlobal(global_pos)):
            for tab_id, btn in self._tabs.items():
                if btn.isVisible() and btn.rect().contains(btn.mapFromGlobal(global_pos)):
                    hit_id = tab_id
                    break
        for tab_id, btn in self._tabs.items():
            btn.set_hover(tab_id == hit_id)

    def _schedule_tab_hover_sync(self) -> None:
        """合并同一事件循环内的多次重算请求（resize / 增删 tab 常成串到达）

        延迟一拍是必要的：增删 tab 后要等布局真正跑完才能拿到新几何，
        立刻命中测试会读到旧 rect。
        """
        if self._hover_sync_pending:
            return
        self._hover_sync_pending = True
        QTimer.singleShot(0, self._run_tab_hover_sync)

    def _run_tab_hover_sync(self) -> None:
        self._hover_sync_pending = False
        self.sync_tab_hover()

    def _clear_tab_hover(self) -> None:
        """光标离开标题栏：清掉所有 hover"""
        for btn in self._tabs.values():
            btn.set_hover(False)

    def leaveEvent(self, event) -> None:
        # 拖到屏幕边缘触发分屏、或被系统菜单/其它窗口抢走焦点时，子 widget 的
        # leaveEvent 不保证到达 —— 在标题栏这一层兜底清空
        self._clear_tab_hover()
        super().leaveEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # show 之前 widget 无有效几何，命中测试无意义；首帧后再补一次
        self._schedule_tab_hover_sync()

    def refresh_style(self) -> None:
        """主题切换后刷新样式（Colors.refresh() 由调用方先执行）"""
        for b in self._tabs.values():
            b.refresh_style()
        # tab 分段槽：整体透明，不画外围胶囊。
        # 早期版本给整组 tab 套了一层淡底 + 1px 边框的胶囊槽，反馈是"多了一圈
        # 多余的框"；现在改由每个 tab 自己的底色（hover 6% / 选中 14%）承担状态表达，
        # tab 直接浮在标题栏上，更干净也更接近 VS Code / Edge 的顶栏。
        self._tab_container.setStyleSheet(
            "QWidget#titlebarTabSegment { background: transparent; border: none; }"
        )
        # 系统按钮：自绘色取自主题 token（深色主题下不再是黑叠黑）
        for b in (self.minBtn, self.maxBtn, self.closeBtn):
            if hasattr(b, "apply_theme_colors"):
                b.apply_theme_colors()
        # 侧栏开关：透明背景无边框，仅 hover 显底（图标由 _ThemeIconEngine 主题感知）
        self._sidebar_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 6px; padding: 3px; }"
            f"QPushButton:hover {{ background: {Colors.TAB_HOVER_BG}; }}"
        )
        # 工作台开关：与侧栏开关同款（图标"右侧边栏"主题感知）
        self._workbench_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 6px; padding: 3px; }"
            f"QPushButton:hover {{ background: {Colors.TAB_HOVER_BG}; }}"
        )
        # 品牌（DriFox + 版本号）已按极简要求移除，顶栏只留功能控件。

    # ── 内部 ──

    def _on_tab_clicked(self, tab_id: str) -> None:
        self.set_active_tab(tab_id)
        self.tab_clicked.emit(tab_id)
