"""自定义无边框窗口标题栏

布局：[侧栏开关][DriFox][版本徽章] ... [中央自定义 tab 区] ... [最小化][最大化][关闭]

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

from PyQt5.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPen
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from qframelesswindow.titlebar import TitleBarBase
from qframelesswindow.titlebar.title_bar_buttons import TitleBarButton

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon

# ── Windows 原生消息常量（仅 win32 分支使用，模块级常量避免热路径重复定义）──
_WM_SYSCOMMAND = 0x0112
_SC_MOVE = 0xF010
_HTCAPTION = 0x0002

# 关闭按钮的 hover / pressed 底色（Windows 系统红，与主题无关）
_CLOSE_HOVER_BG = QColor(232, 17, 35)
_CLOSE_PRESSED_BG = QColor(241, 112, 122)


def _qcolor(css: str, fallback: str) -> QColor:
    """把主题 token 的 CSS 颜色串转为 QColor

    token 可能是 ``#ffffff`` / ``rgba(255, 255, 255, 0.5)`` 等形式；解析失败时
    回退到 fallback，保证任何主题下按钮图标都可见（不出现黑叠黑）。
    """
    c = QColor(css)
    if not c.isValid():
        c = QColor(fallback)
    return c


class _BaseWinButton(TitleBarButton):
    """自绘系统按钮基类：图标/底色全部来自主题 token

    与基类 ``TitleBarButton`` 的差异：
    - 图标按 10px 外接方框居中绘制（Win11 视觉规范），非硬编码像素位置；
    - 颜色通过 ``apply_theme_colors()`` 注入，随主题刷新。
    """

    ICON = 10  # 图标外接方框边长（px）

    def __init__(self, parent=None, *, danger: bool = False):
        super().__init__(parent)
        self.setFixedSize(46, 32)
        self._danger = danger
        self.apply_theme_colors()

    def apply_theme_colors(self) -> None:
        """按当前主题 token 重设四态颜色（Colors.refresh() 由调用方先执行）"""
        fg = _qcolor(Colors.TEXT_PRIMARY, "#ffffff")
        hover_fg = _qcolor(Colors.TEXT_PRIMARY, "#ffffff")
        pressed_fg = _qcolor(Colors.TEXT_SECONDARY, "#c8c8c8")
        hover_bg = _qcolor(Colors.HOVER_BG, "rgba(255,255,255,0.08)")
        pressed_bg = _qcolor(Colors.HOVER_BG_STRONG, "rgba(255,255,255,0.14)")

        self.setNormalColor(fg)
        self.setPressedColor(pressed_fg)
        self.setNormalBackgroundColor(QColor(0, 0, 0, 0))
        if self._danger:
            # 关闭：hover/pressed 走系统红 + 纯白图标（保持 Windows 习惯）
            self.setHoverColor(QColor(255, 255, 255))
            self.setPressedColor(QColor(255, 255, 255))
            self.setHoverBackgroundColor(_CLOSE_HOVER_BG)
            self.setPressedBackgroundColor(_CLOSE_PRESSED_BG)
        else:
            self.setHoverColor(hover_fg)
            self.setPressedColor(pressed_fg)
            self.setHoverBackgroundColor(hover_bg)
            self.setPressedBackgroundColor(pressed_bg)

    # ── 绘制工具：锐利 1px 线条（关闭反锯齿，整数像素对齐）──

    def _begin(self) -> tuple:
        """准备画笔：返回 (painter, color, bgColor, cx, cy)"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
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

    def _paint_icon(self, painter: QPainter, color: QColor, bg_color: QColor, cx: float, cy: float) -> None:
        raise NotImplementedError


class MinimizeButton(_BaseWinButton):
    """最小化：一条 10px 横线"""

    def _paint_icon(self, painter, color, bg_color, cx, cy):
        # fillRect 保证任意 DPR 下都是 1 逻辑像素的锐利实线（QPen 会糊）
        painter.fillRect(QRectF(cx - self.ICON / 2, round(cy), self.ICON, 1), color)


class MaximizeButton(_BaseWinButton):
    """最大化 / 还原：方框（最大化）或双框错位（还原）"""

    def __init__(self, parent=None, *, danger: bool = False):
        self._isMax = False
        super().__init__(parent, danger=danger)

    def setMaxState(self, isMax: bool) -> None:
        """由 TitleBarBase.eventFilter 在 WindowStateChange 时调用"""
        if self._isMax == isMax:
            return
        self._isMax = isMax
        self.update()

    def _paint_icon(self, painter, color, bg_color, cx, cy):
        h = self.ICON
        if not self._isMax:
            # 最大化：单个 10x10 方框（1px 描边，整数坐标 → 锐利）
            x0 = round(cx - h / 2)
            y0 = round(cy - h / 2)
            self._stroke_rect(painter, QRectF(x0 + 0.5, y0 + 0.5, h - 1, h - 1), color, 1)
        else:
            # 还原：后框（左上）+ 前框（右下），前框填背景色遮住后框重叠部分
            back = QRectF(round(cx - h / 2) + 0.5, round(cy - h / 2) + 2.5, h - 3, h - 3)
            front = QRectF(round(cx - h / 2) + 2.5, round(cy - h / 2) + 0.5, h - 3, h - 3)
            self._stroke_rect(painter, back, color, 1)
            painter.setBrush(bg_color)
            painter.setPen(Qt.NoPen)
            painter.drawRect(front)
            self._stroke_rect(painter, front, color, 1)

    @staticmethod
    def _stroke_rect(painter: QPainter, rect: QRectF, color: QColor, width: int) -> None:
        painter.setBrush(Qt.NoBrush)
        pen = QPen(color, width)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(rect)


class CloseButton(_BaseWinButton):
    """关闭：10x10 交叉线（斜线需抗锯齿，单独开启）"""

    def _paint_icon(self, painter, color, bg_color, cx, cy):
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.ICON / 2 - 0.5
        pen = QPen(color, 1)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(cx - r, cy - r), QPointF(cx + r, cy + r))
        painter.drawLine(QPointF(cx + r, cy - r), QPointF(cx - r, cy + r))


class CustomTabButton(QPushButton):
    """顶栏胶囊 tab 按钮（激活态高亮，样式随主题 token 刷新）"""

    def __init__(self, tab_id: str, text: str, parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.tab_id = tab_id
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(26)

    def refresh_style(self):
        """按当前主题 token 重建样式（Colors.refresh() 由调用方负责）"""
        if self.isChecked():
            self.setStyleSheet(
                "QPushButton {"
                f" background: {Colors.TAB_ACTIVE_BG}; color: {Colors.TEXT_PRIMARY};"
                " border: none; border-radius: 13px; padding: 0 16px;"
                f" {font_size_css(13)}; font-weight: bold; }}"
            )
        else:
            self.setStyleSheet(
                "QPushButton {"
                f" background: transparent; color: {Colors.TEXT_SECONDARY};"
                " border: none; border-radius: 13px; padding: 0 16px;"
                f" {font_size_css(13)}; }}"
                "QPushButton:hover {"
                f" background: {Colors.TAB_HOVER_BG}; color: {Colors.TEXT_PRIMARY}; }}"
            )


class CustomTitleBar(TitleBarBase):
    """无边框窗口自定义标题栏（现代化自绘版）

    TitleBarBase 已提供：双击最大化/还原、拖拽判定（``canDrag`` 自动排除
    右侧系统按钮区）。本类负责三区布局、自绘系统按钮与 tab 扩展 API。

    注意：基类的三个内置按钮（纯黑硬编码图标）在 ``__init__`` 中被本模块的
    自绘按钮替换；``minBtn`` / ``maxBtn`` / ``closeBtn`` 属性名保持不变，
    外部（含基类的 ``eventFilter``）无需改动。
    """

    HEIGHT = 38
    MAC_TRAFFIC_LIGHT_PAD = 70  # macOS 系统交通灯左侧留白

    tab_clicked = pyqtSignal(str)
    sidebar_toggle_requested = pyqtSignal()

    def __init__(self, parent):
        self._is_mac: bool = sys.platform == "darwin"
        super().__init__(parent)
        self._tabs: Dict[str, CustomTabButton] = {}
        self._active_id: Optional[str] = None

        self.setFixedHeight(self.HEIGHT)

        # ── 用自绘主题按钮替换基类内置的黑色老式按钮 ──
        self._replace_system_buttons()

        # ── 左区：侧栏开关（透明无边框，图标主题感知） + 品牌 + 版本徽章 ──
        self._sidebar_btn = QPushButton(self)
        self._sidebar_btn.setIcon(get_icon("侧边栏"))
        self._sidebar_btn.setFixedSize(30, 28)
        self._sidebar_btn.setIconSize(QSize(17, 17))
        self._sidebar_btn.setCursor(Qt.PointingHandCursor)
        self._sidebar_btn.setToolTip("收起/展开侧边栏")
        self._sidebar_btn.clicked.connect(self.sidebar_toggle_requested.emit)

        self._brand_title = QLabel("DriFox", self)
        self._brand_version = QLabel(Settings.current_version, self)

        # ── 中央区：tab 容器 ──
        self._tab_container = QWidget(self)
        self._tab_layout = QHBoxLayout(self._tab_container)
        self._tab_layout.setContentsMargins(0, 0, 0, 0)
        self._tab_layout.setSpacing(6)

        # ── 三区布局（mac 隐藏系统按钮 + 左侧留白给交通灯）──
        left_pad = self.MAC_TRAFFIC_LIGHT_PAD if self._is_mac else 8
        layout = QHBoxLayout(self)
        layout.setContentsMargins(left_pad, 3, 0, 3)
        layout.setSpacing(4)
        # AlignVCenter 保证 32px 系统按钮在 38px 栏内垂直居中
        layout.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        layout.addWidget(self._sidebar_btn)
        layout.addSpacing(2)
        layout.addWidget(self._brand_title)
        layout.addSpacing(2)
        layout.addWidget(self._brand_version)
        layout.addStretch(1)
        layout.addWidget(self._tab_container, 0, Qt.AlignCenter)
        layout.addStretch(1)
        if not self._is_mac:
            layout.addWidget(self.minBtn, 0, Qt.AlignRight)
            layout.addWidget(self.maxBtn, 0, Qt.AlignRight)
            layout.addWidget(self.closeBtn, 0, Qt.AlignRight)
        else:
            self.minBtn.hide()
            self.maxBtn.hide()
            self.closeBtn.hide()

        self.refresh_style()

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
        icon: Optional[QIcon] = None,
        on_click: Optional[Callable] = None,
    ) -> None:
        """注册顶部 tab；首个注册的 tab 自动激活"""
        if tab_id in self._tabs:
            return
        btn = CustomTabButton(tab_id, text, self._tab_container)
        if icon is not None:
            btn.setIcon(icon)
        btn.clicked.connect(lambda _checked=False, tid=tab_id: self._on_tab_clicked(tid))
        if on_click is not None:
            btn.clicked.connect(lambda _checked=False: on_click())
        self._tab_layout.addWidget(btn)
        self._tabs[tab_id] = btn
        btn.refresh_style()
        if self._active_id is None:
            self.set_active_tab(tab_id)

    def remove_tab(self, tab_id: str) -> None:
        """移除 tab；若移除的是激活 tab 则自动激活剩余第一个"""
        btn = self._tabs.pop(tab_id, None)
        if btn is None:
            return
        self._tab_layout.removeWidget(btn)
        btn.deleteLater()
        if self._active_id == tab_id:
            self._active_id = None
            if self._tabs:
                self.set_active_tab(next(iter(self._tabs)))

    def set_active_tab(self, tab_id: str) -> None:
        """设置激活 tab（胶囊高亮）"""
        if tab_id not in self._tabs:
            return
        self._active_id = tab_id
        for tid, b in self._tabs.items():
            b.setChecked(tid == tab_id)
            b.refresh_style()

    def refresh_style(self) -> None:
        """主题切换后刷新样式（Colors.refresh() 由调用方先执行）"""
        for b in self._tabs.values():
            b.refresh_style()
        # 系统按钮：自绘色取自主题 token（深色主题下不再是黑叠黑）
        for b in (self.minBtn, self.maxBtn, self.closeBtn):
            if hasattr(b, "apply_theme_colors"):
                b.apply_theme_colors()
        # 侧栏开关：透明背景无边框，仅 hover 显底（图标由 _ThemeIconEngine 主题感知）
        self._sidebar_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 6px; padding: 3px; }"
            f"QPushButton:hover {{ background: {Colors.TAB_HOVER_BG}; }}"
        )
        # 品牌：标题 + 版本号徽章
        self._brand_title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {font_size_css(14)}; font-weight: 600;"
            f" background: transparent; {get_font_family_css()}"
        )
        self._brand_version.setStyleSheet(
            f"QLabel {{ color: {Colors.TEXT_MUTED}; background: {Colors.HOVER_BG};"
            f" border: none; border-radius: 4px; padding: 1px 5px;"
            f" {get_font_family_css()} {font_size_css(10)}; }}"
        )

    # ── 内部 ──

    def _on_tab_clicked(self, tab_id: str) -> None:
        self.set_active_tab(tab_id)
        self.tab_clicked.emit(tab_id)
