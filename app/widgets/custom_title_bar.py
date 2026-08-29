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

from PyQt5.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from qframelesswindow.titlebar import TitleBarBase
from qframelesswindow.titlebar.title_bar_buttons import TitleBarButton, TitleBarButtonState

from app.utils.config import Settings
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

    @property
    def ICON(self) -> int:  # noqa: N802 - 兼容旧命名
        return self.ICON_SIZE

    def __init__(self, parent=None, *, danger: bool = False):
        super().__init__(parent)
        self.setFixedSize(46, 32)
        self._danger = danger
        self.apply_theme_colors()

    def apply_theme_colors(self) -> None:
        """按当前主题 token 重设四态颜色（Colors.refresh() 由调用方先执行）"""
        # 图标色：三态一致，取主题次级文字色（浅色=柔和灰，深色=柔和白）
        fg = _qcolor(Colors.TEXT_SECONDARY, "rgba(255,255,255,0.65)")
        hover_bg = _qcolor(Colors.HOVER_BG, "rgba(255,255,255,0.08)")
        pressed_bg = _qcolor(Colors.HOVER_BG_STRONG, "rgba(255,255,255,0.14)")

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
    """关闭：normal 用主题图标，hover/pressed（红底）强制白色图标"""

    ICON_NAME = "窗体-关闭"

    def _icon(self) -> QIcon:
        if self._state == TitleBarButtonState.NORMAL:
            return get_icon(self.ICON_NAME)
        # 红底上必须用白色版本：浅色主题的 #333 变体在红底上几乎看不见
        icon = QIcon(_WHITE_ICON_PATH.format(name=self.ICON_NAME))
        return icon if not icon.isNull() else get_icon(self.ICON_NAME)


class CustomTabButton(QWidget):
    """顶栏胶囊 tab 按钮（激活态高亮，样式随主题 token 刷新）

    可选关闭钮（closable=True）：右侧 × 子按钮，hover 显现；点击只发射
    ``close_clicked``，不触发 tab 切换（对齐 ReplaceTabButton 的事件模式：
    子控件事件不传播给父 widget，× 点击天然不会触发整卡点击）。
    """

    clicked = pyqtSignal(str)  # tab_id（整卡点击）
    close_clicked = pyqtSignal(str)  # tab_id（× 关闭钮）

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
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(26)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 6 if closable else 12, 0)
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
        self.refresh_style()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.tab_id)
        super().mousePressEvent(event)

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.refresh_style()

    def refresh_style(self):
        """按当前主题 token 重建样式（Colors.refresh() 由调用方负责）"""
        if self._active:
            self.setStyleSheet(
                "CustomTabButton {"
                f" background: {Colors.TAB_ACTIVE_BG};"
                " border: none; border-radius: 13px; }"
            )
            label_color = Colors.TEXT_PRIMARY
            close_hover = Colors.TEXT_PRIMARY
        else:
            self.setStyleSheet(
                "CustomTabButton {"
                " background: transparent;"
                " border: none; border-radius: 13px; }"
                "CustomTabButton:hover {"
                f" background: {Colors.TAB_HOVER_BG}; }}"
            )
            label_color = Colors.TEXT_SECONDARY
            close_hover = Colors.TEXT_PRIMARY
        self._label.setStyleSheet(
            f"color: {label_color}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(13)};"
            + (" font-weight: bold;" if self._active else "")
        )
        if self._close_btn is not None:
            self._close_btn.setStyleSheet(
                f"QPushButton {{ color: {Colors.TEXT_MUTED}; background: transparent;"
                " border: none; border-radius: 8px; font-size: 13px; padding: 0; }"
                f"QPushButton:hover {{ color: {close_hover};"
                f" background: {Colors.HOVER_BG}; }}"
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
    tab_close_clicked = pyqtSignal(str)
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
            b.set_active(tid == tab_id)

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
        # 版本号：纯文字（无背景/无边框），与标题同一基线的次要信息
        self._brand_version.setStyleSheet(
            f"QLabel {{ color: {Colors.TEXT_MUTED}; background: transparent;"
            f" border: none; padding: 0 0 0 1px;"
            f" {get_font_family_css()} {font_size_css(11)}; }}"
        )

    # ── 内部 ──

    def _on_tab_clicked(self, tab_id: str) -> None:
        self.set_active_tab(tab_id)
        self.tab_clicked.emit(tab_id)
