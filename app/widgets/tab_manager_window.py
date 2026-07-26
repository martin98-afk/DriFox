# -*- coding: utf-8 -*-
"""
TabManagerWindow — Tab 管理器宿主窗口

左侧 TabPanel + 右侧 QStackedWidget 嵌入 OpenAIChatToolWindow 实例。
支持模式切换（独立 ↔ 嵌入），窗口状态通过 setParent 迁移完全保留。
"""

import json
import platform
from typing import Any, Dict, List, Optional

from PyQt5 import sip as _sip

from loguru import logger
from PyQt5.QtCore import Qt, QPoint, QTimer, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QIcon, QMouseEvent
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import TransparentToolButton, FluentIcon as FIF

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css


# ── 边缘缩放热区尺寸（4px） ──
_EDGE_RESIZE_BORDER = 4

# ── Windows 原生消息常量（nativeEvent 边缘缩放用） ──
_WM_NCHITTEST = 0x0084
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17
_HTCAPTION = 2
_HTCLIENT = 1

if platform.system() == "Windows":
    import ctypes
    from ctypes import wintypes

    class _WINDOWS_MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", wintypes.POINT),
        ]


class _SidebarToggleButton(QPushButton):
    """侧栏折叠/展开按钮 — 绘制 <| / |> 图标"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("折叠侧栏")
        self.setStyleSheet("""
            QPushButton { background: transparent; border: none; border-radius: 5px; }
            QPushButton:hover { background: rgba(255,255,255,0.08); }
        """)

    def set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self.setToolTip("展开侧栏" if collapsed else "折叠侧栏")
        self.update()

    def paintEvent(self, event):
        from PyQt5.QtGui import QColor, QPainter, QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w // 2, h // 2
        pen = QPen(QColor(Colors.TEXT_MUTED), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)

        if self._collapsed:
            # 展开图标: ▷|
            p.drawLine(cx - 3, cy - 5, cx - 3, cy + 5)
            p.drawLine(cx + 3, cy - 5, cx - 1, cy)
            p.drawLine(cx + 3, cy + 5, cx - 1, cy)
        else:
            # 折叠图标: |◁
            p.drawLine(cx + 3, cy - 5, cx + 3, cy + 5)
            p.drawLine(cx - 3, cy - 5, cx + 1, cy)
            p.drawLine(cx - 3, cy + 5, cx + 1, cy)
        p.end()


class TabManagerTitleBar(QWidget):
    """TabManagerWindow 自定义标题栏（Frameless）

    风格与主 UI 一致：深色半透明背景 + 亚克力质感按钮。
    支持拖拽移动、双击最大化、右键系统菜单。
    """

    minimizeRequested = pyqtSignal()
    maximizeRestoreRequested = pyqtSignal()
    closeRequested = pyqtSignal()
    toggleSidebarRequested = pyqtSignal()

    # ── 右键系统菜单 Action IDs ──
    _ACTION_RESTORE = 1
    _ACTION_MOVE = 2
    _ACTION_SIZE = 3
    _ACTION_MINIMIZE = 4
    _ACTION_MAXIMIZE = 5
    _ACTION_CLOSE = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dragging = False
        self._drag_pos = None
        self._is_maximized = False
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        self.setFixedHeight(36)
        self.setMouseTracking(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 4, 0)
        layout.setSpacing(6)

        # ── 折叠/展开侧栏按钮 ──
        self._sidebar_toggle_btn = _SidebarToggleButton(self)
        self._sidebar_toggle_btn.setFixedSize(28, 26)
        self._sidebar_toggle_btn.clicked.connect(self.toggleSidebarRequested.emit)
        layout.addWidget(self._sidebar_toggle_btn)

        # ── 标题 ──
        self._title_label = QLabel("飘狐-DriFox", self)
        self._title_label.setObjectName("tabManagerTitle")
        self._title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self._title_label, 1)

        # ── 内存标签（预留，与 ToolWindowTitleBar 保持一致） ──
        self._memory_label = QLabel(self)
        self._memory_label.setObjectName("tabManagerMemoryLabel")
        self._memory_label.setFixedHeight(20)
        self._memory_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} font-size: {scale_font_size(11)}px; "
            f"padding: 1px 8px; background: transparent; border: none; border-radius: 3px;"
        )
        self._memory_label.hide()
        layout.addWidget(self._memory_label)

        # ── 窗口控制按钮 ──
        self._min_btn = TransparentToolButton(self)
        self._min_btn.setIcon(FIF.MINIMIZE)
        self._min_btn.setFixedSize(36, 30)
        self._min_btn.setToolTip("最小化")
        self._min_btn.clicked.connect(self.minimizeRequested.emit)

        self._max_btn = TransparentToolButton(self)
        self._max_btn.setIcon(self._draw_maximize_icon())
        self._max_btn.setFixedSize(36, 30)
        self._max_btn.setToolTip("最大化")
        self._max_btn.clicked.connect(self._on_max_clicked)

        self._close_btn = TransparentToolButton(self)
        self._close_btn.setIcon(FIF.CLOSE)
        self._close_btn.setFixedSize(36, 30)
        self._close_btn.setToolTip("关闭")
        self._close_btn.clicked.connect(self.closeRequested.emit)

        layout.addWidget(self._min_btn)
        layout.addWidget(self._max_btn)
        layout.addWidget(self._close_btn)

    def _draw_maximize_icon(self) -> "QIcon":
        """绘制 Fluent Design 风格最大化图标（空心方形）"""
        from PyQt5.QtGui import QPixmap, QPainter, QColor, QIcon, QPen

        size = 14
        pix = QPixmap(size, size)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(Colors.TEXT_PRIMARY), 1.5)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        margin = 2
        p.drawRect(margin, margin, size - 2 * margin, size - 2 * margin)
        p.end()
        return QIcon(pix)

    def _draw_restore_icon(self) -> "QIcon":
        """绘制 Fluent Design 风格还原图标（重叠双矩形）"""
        from PyQt5.QtGui import QPixmap, QPainter, QColor, QIcon, QPen

        size = 14
        pix = QPixmap(size, size)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(Colors.TEXT_PRIMARY), 1.5)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

        # 前层矩形（在前，偏右下）
        p.drawRect(1, 5, size - 5, size - 6)

        # 后层矩形（在后，偏左上），只画不被前层遮挡的三条边
        bx, by, bw, bh = 5, 1, size - 6, size - 6
        # 右、下、左三条边
        p.drawLine(bx + bw, by, bx + bw, by + bh)
        p.drawLine(bx + bw, by + bh, bx, by + bh)
        p.drawLine(bx, by + bh, bx, by)
        p.end()
        return QIcon(pix)

    def _apply_style(self):
        Colors.refresh()
        font_name = Settings.get_instance().llm_font_family.value

        self.setStyleSheet(f"""
            TabManagerTitleBar {{
                background: {Colors.CARD_BG.format(alpha=250)};
                border-bottom: 1px solid {Colors.BORDER};
            }}
            #tabManagerTitle {{
                color: {Colors.TEXT_PRIMARY};
                font-size: {scale_font_size(14)}px;
                font-weight: 600;
                font-family: "{font_name}";
                background: transparent;
                padding: 0 4px;
            }}
            #tabManagerMemoryLabel {{
                color: {Colors.TEXT_SECONDARY};
                font-size: {scale_font_size(11)}px;
                background: transparent;
            }}
        """)

        # 侧栏折叠按钮
        self._sidebar_toggle_btn.setStyleSheet("""
            QPushButton { background: transparent; border: none; border-radius: 5px; }
            QPushButton:hover { background: rgba(255,255,255,0.08); }
        """)

        # 关闭按钮 hover 特殊处理（红色）
        self._close_btn.setStyleSheet("""
            TransparentToolButton {
                background: transparent;
                border: none;
                border-radius: 4px;
            }
            TransparentToolButton:hover {
                background: #e81123;
            }
            TransparentToolButton:hover QToolTip {
                background: #e81123;
            }
            TransparentToolButton:pressed {
                background: #f1707a;
            }
        """)

    def refresh_style(self):
        """主题/字体变更时刷新"""
        self._apply_style()

    def set_title(self, title: str):
        self._title_label.setText(title)

    def set_maximized(self, maximized: bool):
        self._is_maximized = maximized
        if maximized:
            self._max_btn.setToolTip("还原")
            self._max_btn.setIcon(self._draw_restore_icon())
        else:
            self._max_btn.setToolTip("最大化")
            self._max_btn.setIcon(self._draw_maximize_icon())

    def _on_max_clicked(self):
        self.maximizeRestoreRequested.emit()

    # ── 鼠标事件：窗口拖拽 + 双击最大化 ──

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_pos = event.globalPos() - self.parentWidget().frameGeometry().topLeft()
            # 通知父窗口进入拖拽状态——nativeEvent 在此期间跳过 _nchittest
            self.parentWidget()._is_dragging = True
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._dragging and event.buttons() == Qt.LeftButton:
            win = self.parentWidget()
            if win.isMaximized():
                # 从最大化拖拽 → 先还原再继续拖
                win.showNormal()
                # 计算比例：拖拽点相对窗口宽度的比例
                ratio = self._drag_pos.x() / win.width() if win.width() > 0 else 0.5
                new_pos = event.globalPos() - QPoint(int(win.width() * ratio), self._drag_pos.y())
                win.move(new_pos)
                self._drag_pos = event.globalPos() - win.frameGeometry().topLeft()
            else:
                win.move(event.globalPos() - self._drag_pos)
            event.accept()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self.parentWidget()._is_dragging = False
            event.accept()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.maximizeRestoreRequested.emit()
            event.accept()
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        """右键系统菜单"""
        win = self.parentWidget()
        if not win:
            return

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {Colors.CARD_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {scale_font_size(13)}px;
            }}
            QMenu::item:selected {{
                background: {Colors.HOVER_BG};
            }}
            QMenu::separator {{
                height: 1px;
                background: {Colors.BORDER};
                margin: 4px 8px;
            }}
        """)

        is_maxed = win.isMaximized()

        restore_act = menu.addAction("还原")
        restore_act.setEnabled(is_maxed)
        restore_act.setData(self._ACTION_RESTORE)

        move_act = menu.addAction("移动")
        move_act.setEnabled(not is_maxed)
        move_act.setData(self._ACTION_MOVE)

        size_act = menu.addAction("大小")
        size_act.setEnabled(not is_maxed)
        size_act.setData(self._ACTION_SIZE)

        menu.addSeparator()

        min_act = menu.addAction("最小化")
        min_act.setData(self._ACTION_MINIMIZE)

        max_act = menu.addAction("最大化" if not is_maxed else "还原")
        max_act.setData(self._ACTION_MAXIMIZE)

        menu.addSeparator()

        close_act = menu.addAction("关闭")
        close_act.setData(self._ACTION_CLOSE)

        action = menu.exec_(event.globalPos())
        if action is None:
            return

        act_id = action.data()
        if act_id == self._ACTION_RESTORE:
            win.showNormal()
        elif act_id == self._ACTION_MOVE:
            # 使用 Windows WM_SYSCOMMAND SC_MOVE 模拟标准系统"移动"行为
            if is_maxed:
                win.showNormal()
            if platform.system() == "Windows":
                import ctypes

                ctypes.windll.user32.PostMessageW(int(win.winId()), 0x0112, 0xF010, 0)  # WM_SYSCOMMAND SC_MOVE
        elif act_id == self._ACTION_SIZE:
            # 模拟系统菜单的"大小"行为 - 用鼠标模拟调整
            pass
        elif act_id == self._ACTION_MINIMIZE:
            win.showMinimized()
        elif act_id == self._ACTION_MAXIMIZE:
            if is_maxed:
                win.showNormal()
            else:
                win.showMaximized()
        elif act_id == self._ACTION_CLOSE:
            win.close()


class EmptyStateWidget(QWidget):
    """空状态页 — 最后一个 Tab 关闭时显示"""

    newTabRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(16)

        icon_label = QLabel("📑", self)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet("font-size: 48px; background: transparent;")
        layout.addWidget(icon_label)

        text_label = QLabel("没有打开的窗口", self)
        text_label.setAlignment(Qt.AlignCenter)
        text_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; background: transparent; {font_size_css(14)}")
        layout.addWidget(text_label)

        new_btn = QPushButton("＋ 新建标签页", self)
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setFixedSize(160, 36)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {Colors.CARD_BG.format(alpha=200)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                {font_size_css(13)}
            }}
            QPushButton:hover {{
                background: {Colors.HOVER_BG};
                border-color: {Colors.INFO};
            }}
        """)
        new_btn.clicked.connect(self.newTabRequested.emit)
        layout.addWidget(new_btn)


def _find_edge_launchers(window):
    """查找窗口的所有 UIPluginEdgeLauncher 实例"""
    from PyQt5 import sip

    if window is None or sip.isdeleted(window):
        return []
    try:
        result = []
        for child in window.findChildren(QWidget):
            if sip.isdeleted(child):
                continue
            # 检查类名（PyQt5 中 Python 子类的 metaObject className）
            name = child.metaObject().className()
            if name and "UIPluginEdgeLauncher" in name:
                result.append(child)
        return result
    except Exception:
        return []


def _hide_edge_launcher(window):
    """隐藏窗口的 UIPluginEdgeLauncher"""
    for child in _find_edge_launchers(window):
        try:
            child.hide()
        except Exception:
            pass


def _show_edge_launcher(window):
    """显示窗口的 UIPluginEdgeLauncher"""
    for child in _find_edge_launchers(window):
        try:
            child.show()
        except Exception:
            pass


def _update_tab_icon(tab_idx: int, project: str):
    """更新指定 Tab 的项目图标

    使用系统配置字体 + scale_icon_size 缩放尺寸，保证字号变化后图标随之变化。
    """
    from PyQt5.QtGui import QPixmap, QColor as QClr, QPainter as QPnt

    tm = TabManagerWindow.get_instance()
    if tm is None:
        return
    try:
        from app.utils.design_tokens import scale_font_size, scale_icon_size
        from app.utils.utils import get_unified_font
        from app.widgets.cards.settings.project_selector_card import (
            extract_project_initials,
            get_project_color,
        )

        initials = extract_project_initials(project)
        color_str = get_project_color(project, alpha=255)
        parts = color_str.replace("rgba(", "").replace(")", "").split(",")
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])

        # 跟随系统字号缩放
        size = scale_icon_size(20)
        # icon 内文字：7px 为基准（小/中两档受 8px 下限保护保底在 8px），随系统字号缩放
        scaled_font_px = scale_font_size(7)
        radius = max(2, size * 4 // 20)

        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPnt(pix)
        p.setRenderHint(QPnt.Antialiasing)
        p.setBrush(QClr(r, g, b))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, size, size, radius, radius)
        p.setPen(QClr(255, 255, 255))
        p.setFont(get_unified_font(scaled_font_px, bold=True))
        p.drawText(pix.rect(), Qt.AlignCenter, initials)
        p.end()
        tm._tab_panel.update_tab_icon(tab_idx, pix)
    except Exception:
        pass


class TabManagerWindow(QWidget):
    """Tab 管理器宿主窗口（单例）"""

    _instance: Optional["TabManagerWindow"] = None
    _last_toggle_time: float = 0.0  # 上次模式切换时间戳（time.monotonic），防重入
    _is_dragging: bool = False  # 标题栏拖拽中 → nativeEvent 跳过 _nchittest
    tabCountChanged = pyqtSignal(int)
    activeTabChanged = pyqtSignal(int)

    @classmethod
    def get_instance(cls) -> Optional["TabManagerWindow"]:
        return cls._instance

    @classmethod
    def create_instance(cls, parent=None) -> "TabManagerWindow":
        if cls._instance is None:
            cls._instance = cls(parent)
        return cls._instance

    def __init__(self, parent=None):
        super().__init__(parent)
        if TabManagerWindow._instance is not None:
            raise RuntimeError("TabManagerWindow 是单例，请使用 get_instance() 获取")
        TabManagerWindow._instance = self

        self._windows: List = []  # List[OpenAIChatToolWindow]
        self._cached_dialogs: Dict[int, Any] = {}  # id → ToolPopupDialog
        self._is_transitioning: bool = False
        # ── 几何防抖保存：拖拽/缩放结束后 200ms 才写盘 ──
        self._geo_save_timer = QTimer(self)
        self._geo_save_timer.setSingleShot(True)
        self._geo_save_timer.setInterval(200)
        self._geo_save_timer.timeout.connect(self._do_save_geometry)

        self.setWindowTitle("飘狐-DriFox")
        self.setObjectName("tabManagerWindow")
        self.setMinimumSize(600, 450)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        # 窗口标志：Frameless 自定义标题栏 + 置顶
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setWindowIcon(QIcon(":/icons/drifox.ico"))

        # 确保 Colors 已刷新（主题色初始化）
        Colors.refresh()

        self._setup_ui()
        self._setup_signals()
        # 不在 __init__ 设位置，等第一次 showEvent 时再设

        # 刷新 Tab 面板内嵌的 UI 插件列表
        self._tab_panel.refresh_ui_plugins()
        # 注册到 TrayManager
        from app.tray_manager import TrayManager

        TrayManager.get_instance()._tab_manager_window = self

        # 注册主题刷新回调（虽主题切换路径不走 dispatch_refresh，
        # 但保持接口一致性便于将来扩展）
        theme_manager.register_refresh_target(self)

    def _on_theme_changed(self):
        """主题切换时刷新配色"""
        Colors.refresh()
        # 重建样式表
        self._apply_theme_stylesheet()
        # 刷新标题栏
        if hasattr(self, "_title_bar"):
            self._title_bar.refresh_style()
        # 刷新所有 Tab 项（标题颜色/字体/图标尺寸随主题或字号刷新）
        try:
            self._tab_panel.refresh_style()
            # 强制重绘子控件（setStyleSheet 后保险调用一次 update）
            self._tab_panel.repaint()
        except Exception:
            pass
        # 重画所有 tab 的项目图标（背景色来自项目，颜色不受主题影响，但
        # 随字号缩放 + 重画保证主题切换后图标尺寸与文字一致）
        for idx, win in enumerate(self._windows):
            try:
                project = getattr(win, "_current_project", None) or ""
                if project:
                    _update_tab_icon(idx, project)
            except Exception:
                pass

    def refresh_theme(self):
        """ThemeManager 统一刷新入口（dispatch_refresh 调用）"""
        self._on_theme_changed()

    def _apply_theme_stylesheet(self):
        """应用主题样式表

        使用 #objectName 选择器而非类选择器。PyQt5 中 Python QWidget 子类的
        metaObject().className() 统一返回 'QWidget'，类选择器（如
        'TabManagerWindow {...}'）无法匹配，导致样式失效。

        左侧 Tab 面板的右边框线由 splitter handle 区域绘制：
        QSplitter 自己的子控件绘制顺序会吞掉普通 widget 的 border-right，
        直接给 #tabPanel 设 border 看不到；用 1px handle + BORDER 颜色
        是最可靠的方案（拖拽热区收到 1px，但 Qt 对窄 handle 也有命中扩展）。
        """
        self.setStyleSheet(f"""
            #tabPanel {{
                background: {Colors.CARD_BG.format(alpha=240)};
            }}
            #tabManagerWindow {{
                background: {Colors.CONTENT_BG};
                border-radius: 8px;
            }}
            #tabManagerContent {{
                background: {Colors.CONTENT_BG};
                border-radius: 8px;
            }}
            #contentArea {{
                background: {Colors.CONTENT_BG};
            }}
        """)
        # splitter handle 区域显示为 BORDER 颜色，形成可视右边框线
        if getattr(self, "_splitter", None) is not None:
            self._splitter.setStyleSheet(f"""
                QSplitter::handle:horizontal {{
                    background: {Colors.BORDER};
                }}
            """)
        # 刷新标题栏样式
        if hasattr(self, "_title_bar"):
            self._title_bar.refresh_style()

    def _setup_ui(self):
        # ── 外层纵向布局：标题栏 + 内容区 ──
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── 自定义标题栏 ──
        self._title_bar = TabManagerTitleBar(self)
        self._title_bar.minimizeRequested.connect(self.showMinimized)
        self._title_bar.maximizeRestoreRequested.connect(self._on_titlebar_max_restore)
        self._title_bar.closeRequested.connect(self._on_titlebar_close)
        main_layout.addWidget(self._title_bar)

        # ── 内容区（水平：TabPanel + QStackedWidget） ──
        content_widget = QWidget(self)
        content_widget.setObjectName("tabManagerContent")
        content_layout = QHBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # 左侧 Tab 面板（可拖拽调整宽度）
        from app.widgets.tab_panel import TabPanel

        self._tab_panel = TabPanel(content_widget)
        self._tab_panel.setObjectName("tabPanel")
        self._tab_panel.setMinimumWidth(120)
        self._tab_panel.setMaximumWidth(400)

        # 右侧内容区
        self._content_area = QStackedWidget(content_widget)
        self._content_area.setObjectName("contentArea")

        # 空状态页（索引 0）
        self._empty_state = EmptyStateWidget(content_widget)
        self._empty_state.newTabRequested.connect(self._on_new_tab_requested)
        self._content_area.addWidget(self._empty_state)  # index 0

        # 使用 QSplitter 让左侧面板可拖拽
        from PyQt5.QtWidgets import QSplitter

        self._splitter = QSplitter(Qt.Horizontal, content_widget)
        self._splitter.addWidget(self._tab_panel)
        self._splitter.addWidget(self._content_area)
        self._splitter.setStretchFactor(0, 0)  # 左面板不拉伸
        self._splitter.setStretchFactor(1, 1)  # 右侧内容区拉伸
        # handle 宽设为 1 并由 _apply_theme_stylesheet 给它上 BORDER 颜色，
        # 形成清晰的"左边框线"；QSplitter 自身的子控件绘制顺序会吞掉普通
        # widget 的 border-right，所以用 handle 区域显示更可靠。
        self._splitter.setHandleWidth(1)
        self._splitter.setChildrenCollapsible(False)
        content_layout.addWidget(self._splitter)
        content_widget.setLayout(content_layout)
        main_layout.addWidget(content_widget, 1)

        # ── 右下角调整手柄 ──
        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(12, 12)
        self._size_grip.setStyleSheet("""
            QSizeGrip {
                background: transparent;
                border: none;
            }
        """)
        # 把 SizeGrip 放到右下角（在父窗口 resize 时通过 moveEvent 调整位置）
        self._size_grip.raise_()

        # 恢复面板宽度
        saved_w = Settings.get_instance().tab_panel_width.value
        if saved_w:
            self._splitter.setSizes([saved_w, self.width() - saved_w])

        # 应用样式（使用 _apply_theme_stylesheet 以确保 objectName 选择器生效）
        self._apply_theme_stylesheet()

    def _setup_signals(self):
        self._tab_panel.tabSelected.connect(self._on_tab_selected)
        self._tab_panel.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_panel.newTabRequested.connect(self._on_new_tab_requested)

    # ── 窗口管理 ──

    def add_window(self, window) -> int:
        """添加窗口到 Tab 管理器，返回索引"""
        if window in self._windows:
            return self._windows.index(window)

        self._windows.append(window)
        idx = len(self._windows)

        # 添加到 QStackedWidget（从索引 1 开始，0 是空状态页）
        self._content_area.addWidget(window)

        # 获取初始标题：优先用项目名，其次窗口标题，最后默认
        project = getattr(window, "_current_project", None) or ""
        title = window.windowTitle() or project or "新建会话"

        # 获取初始图标：使用项目选择器风格的项目头像
        from PyQt5.QtGui import QIcon, QPixmap, QColor as QClr, QPainter as QPnt

        tab_icon = None
        if project:
            try:
                from app.widgets.cards.settings.project_selector_card import (
                    extract_project_initials,
                    get_project_color,
                )

                initials = extract_project_initials(project)
                color_str = get_project_color(project, alpha=255)
                # 解析 "rgba(r,g,b,a)"
                parts = color_str.replace("rgba(", "").replace(")", "").split(",")
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                pix = QPixmap(20, 20)
                pix.fill(Qt.transparent)
                p = QPnt(pix)
                p.setRenderHint(QPnt.Antialiasing)
                p.setBrush(QClr(r, g, b))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(0, 0, 20, 20, 4, 4)
                p.setPen(QClr(255, 255, 255))
                f = p.font()
                f.setPixelSize(11)
                f.setBold(True)
                p.setFont(f)
                p.drawText(pix.rect(), Qt.AlignCenter, initials)
                p.end()
                tab_icon = pix
            except Exception:
                pass
        if tab_icon is None:
            raw_icon = getattr(window, "icon", None)
            if isinstance(raw_icon, QIcon):
                tab_icon = raw_icon.pixmap(20, 20)
            elif raw_icon is not None:
                tab_icon = raw_icon

        tab_idx = self._tab_panel.add_tab(title, tab_icon)

        # 统一回调：标题变更时同步更新 Tab 标题 + 项目图标 + 宿主窗口标题 + 团队胶囊
        # ★ 使用窗口对象引用 + 动态索引查找，防止删除前序 tab 后 _idx 漂移
        def _on_win_title_changed(_new_title, _win=window):
            if _sip.isdeleted(_win):
                return
            if _win not in self._windows:
                return
            cur_idx = self._windows.index(_win)
            # 更新 Tab 标题
            t = _win.windowTitle() or getattr(_win, "_current_project", None) or "对话"
            self._tab_panel.update_tab_title(cur_idx, t)
            # 更新项目图标
            p = getattr(_win, "_current_project", None) or ""
            _update_tab_icon(cur_idx, p)
            # 团队模式：显示角色胶囊
            team_agent = getattr(_win, "_team_agent_name", "") or ""
            if team_agent:
                self._tab_panel.update_tab_capsule(cur_idx, team_agent)
            else:
                self._tab_panel.clear_tab_capsule(cur_idx)
            # 如果该窗口是当前选中 Tab，同步宿主窗口标题
            if self._tab_panel.active_index == cur_idx:
                self._sync_window_title()

        window.windowTitleChanged.connect(_on_win_title_changed)

        # 监听 AI 状态变化（流式/错误/提问 → Tab 边框指示）
        def _on_ai_state_changed(state, _win=window):
            if _sip.isdeleted(_win):
                return
            if _win not in self._windows:
                return
            cur_idx = self._windows.index(_win)
            if state in ("streaming", "thinking"):
                self._tab_panel.update_tab_streaming(cur_idx, True, False)
                self._tab_panel.update_tab_question(cur_idx, False)  # 互斥：退出 question
            elif state == "error":
                self._tab_panel.update_tab_streaming(cur_idx, False, True)
                self._tab_panel.update_tab_question(cur_idx, False)
            elif state == "question":
                self._tab_panel.update_tab_question(cur_idx, True)
                self._tab_panel.update_tab_streaming(cur_idx, False, False)
            else:  # idle
                self._tab_panel.update_tab_streaming(cur_idx, False, False)
                self._tab_panel.update_tab_question(cur_idx, False)

        window.ai_state_changed.connect(_on_ai_state_changed)

        # 立即触发一次初始图标更新 + 团队胶囊状态同步
        logger.info(f"[TabMode] 初始图标: project={project!r}, tab_idx={tab_idx}")
        _update_tab_icon(tab_idx, project)
        team_agent = getattr(window, "_team_agent_name", "") or ""
        if team_agent:
            self._tab_panel.update_tab_capsule(tab_idx, team_agent)

        # 隐藏 EdgeLauncher（Tab 模式下每个窗口不应显示）
        _hide_edge_launcher(window)

        # 隐藏空状态页，切换到新窗口
        self._content_area.widget(0).hide()
        self._tab_panel.set_active_index(idx - 1)

        self.tabCountChanged.emit(len(self._windows))
        return idx - 1

    def remove_window(self, window):
        """从 Tab 管理器移除窗口"""
        if window not in self._windows:
            return

        idx = self._windows.index(window)
        self._windows.remove(window)

        # 从 QStackedWidget 移除
        self._content_area.removeWidget(window)

        # 从 Tab 面板移除
        self._tab_panel.remove_tab(idx)

        # 如果所有窗口都被移除，显示空状态页
        if not self._windows:
            self._content_area.widget(0).show()

        self.tabCountChanged.emit(len(self._windows))

    def get_current_window(self):
        """获取当前选中的窗口"""
        idx = self._tab_panel.active_index
        if 0 <= idx < len(self._windows):
            return self._windows[idx]
        return None

    @property
    def window_count(self) -> int:
        return len(self._windows)

    # ── Tab 回调 ──

    def _sync_window_title(self):
        """将标题同步为当前窗口的会话标题（标题栏 + windowTitle）"""
        win = self.get_current_window()
        if win:
            t = win.windowTitle()
            if t:
                self.setWindowTitle(t)
                if hasattr(self, "_title_bar"):
                    self._title_bar.set_title(t)
                return
        self.setWindowTitle("飘狐-DriFox")
        if hasattr(self, "_title_bar"):
            self._title_bar.set_title("飘狐-DriFox")

    def _on_tab_selected(self, index: int):
        if 0 <= index < len(self._windows):
            self._content_area.setCurrentWidget(self._windows[index])
            self.activeTabChanged.emit(index)
            # 切换 tab 时同步宿主窗口标题
            self._sync_window_title()
            # 刷新共享 Launcher 的卡片目标窗口为当前 Tab
            self._update_shared_launcher()

    def _on_tab_close_requested(self, index: int):
        if 0 <= index < len(self._windows):
            window = self._windows[index]
            # 先从列表中移除，避免后续操作访问到已销毁的窗口
            self._windows.pop(index)
            self._content_area.removeWidget(window)
            self._tab_panel.remove_tab(index)

            # 调用窗口的关闭逻辑（自动保存会话）
            try:
                window.close()
            except Exception as e:
                logger.error(f"[TabManager] 关闭窗口失败: {e}")

            # 如果所有窗口都被移除，显示空状态页
            if not self._windows:
                self._content_area.widget(0).show()

            self.tabCountChanged.emit(len(self._windows))

    def _on_new_tab_requested(self):
        """新建窗口 — 走当前窗口的复制逻辑，复用后端状态"""
        current = self.get_current_window()
        if current is not None and hasattr(current, "_duplicate_window"):
            # 从当前窗口复制（保留后端上下文）
            current._duplicate_window(branch=False)
        else:
            # 没有当前窗口时，走基础创建逻辑
            from app.main_widget import OpenAIChatToolWindow

            fake_page = self._create_fake_page()
            new_window = OpenAIChatToolWindow(fake_page)
            self.add_window(new_window)

    # ── Tab 面板 UI 插件列表 ──

    def _update_shared_launcher(self) -> None:
        """兼容旧调用方（main_widget.py 热重载和模式切换）并刷新内嵌列表"""
        self._tab_panel.refresh_ui_plugins()

    def _show_shared_launcher(self) -> None:
        """兼容模式切换调用：刷新始终显示在 TabPanel 中的插件列表"""
        self._tab_panel.refresh_ui_plugins()

    def _hide_shared_launcher(self) -> None:
        """兼容模式切换调用：插件列表内嵌在 TabPanel，无独立入口需要隐藏"""
        logger.debug("[TabMode] 跳过隐藏共享 EdgeLauncher：插件列表已内嵌在 TabPanel")

    @staticmethod
    def _create_fake_page():
        """创建一个临时的 FakePage 用于窗口初始化（与 main.py 类似）"""
        from PyQt5.QtWidgets import QWidget

        class FakePage(QWidget):
            def __init__(self):
                super().__init__()
                self.cfg = Settings.get_instance()

            def isActiveWindow(self):
                return True

            @property
            def workflow_name(self):
                return "tab_manager"

            @property
            def global_variables_changed(self):
                class FakeSignal:
                    def connect(self, *args, **kwargs):
                        pass

                return FakeSignal()

            def setUpdatesEnabled(self, enabled):
                pass

            def update(self):
                pass

            def show_splitter(self):
                pass

            def hide_splitter(self):
                pass

        return FakePage()

    # ── 模式切换 ──

    @classmethod
    def toggle_mode(cls, enable: bool):
        """切换 Tab 管理器模式的启用/关闭

        防重入守卫：1 秒内重复调用直接忽略，防止信号循环导致
        _enable_mode/_disable_mode 被反复触发，造成卡顿和日志洪泛。
        """
        import time as _time

        now = _time.monotonic()
        if now - cls._last_toggle_time < 1.0:
            logger.warning(
                f"[TabMode] 防重入守卫触发，忽略 toggle_mode({enable})，"
                f"距上次切换仅 {(now - cls._last_toggle_time) * 1000:.0f}ms"
            )
            return

        # 模式状态检查：如果已经在目标模式，跳过（不更新 _last_toggle_time）
        inst = cls.get_instance()
        if enable:
            if inst is not None and inst.isVisible():
                return
        else:
            if inst is None or not inst.isVisible():
                return

        # 通过所有检查，更新时间戳并执行切换
        cls._last_toggle_time = now

        from app.tray_manager import TrayManager

        tm = TrayManager.get_instance()

        if enable:
            cls._enable_mode(tm)
        else:
            cls._disable_mode(tm)

    @classmethod
    def _collect_windows_to_migrate(cls, tray_manager):
        """收集需要迁入 Tab 管理器的窗口（OpenAIChatToolWindow 实例列表）

        优先从 tray_manager._windows（ToolPopupDialog）中提取 tool_instance，
        兜底直接从 OpenAIChatToolWindow._instances 收集——防止因注册时序或异常
        导致 tray_manager._windows 为空时遗漏窗口。
        """
        # 方法一：从 TrayManager 已注册的 ToolPopupDialog 提取
        from app.main_widget import OpenAIChatToolWindow as _OCW

        seen_ids = set()
        collected = []

        for dialog in list(tray_manager._windows):
            try:
                if _sip.isdeleted(dialog):
                    tray_manager.unregister_window(dialog)
                    continue
                tool_instance = getattr(dialog, "tool_instance", None)
                if tool_instance is None or _sip.isdeleted(tool_instance):
                    tray_manager.unregister_window(dialog)
                    continue
                wid = getattr(tool_instance, "_window_id", id(tool_instance))
                seen_ids.add(wid)
                collected.append((dialog, tool_instance))
            except Exception:
                continue

        # 方法二（兜底）：直接从 _instances 收集未被上层覆盖的窗口
        for win in getattr(_OCW, "_instances", []):
            try:
                if _sip.isdeleted(win):
                    continue
                if getattr(win, "_is_destroyed", False):
                    continue
                wid = getattr(win, "_window_id", id(win))
                if wid in seen_ids:
                    continue
                # 这个窗口不在 TrayManager 注册列表中，直接作为 tool_instance 迁入
                logger.warning(f"[TabMode] 兜底收集未注册窗口: {win}, window_id={wid}")
                collected.append((None, win))
                seen_ids.add(wid)
            except Exception:
                continue

        return collected

    @classmethod
    def _force_close_system_cards(cls, tool_instance):
        """强制关闭指定窗口的所有系统卡片

        模式切换（多窗口→Tab）时，window 内可能开着设置/历史等系统卡片，
        其 CardManager 状态和 _system_cards_open 标记会残留，导致：
        - 输入区域持续隐藏（_on_system_card_closed 未触发）
        - 卡片无法正常交互关闭
        """
        try:
            from app.widgets.cards.card_manager import CardManager as _CM

            window_id = getattr(tool_instance, "_window_id", None)
            if not window_id:
                return

            mgr = _CM.get_instance()
            # 系统卡片 ID 列表（与 OpenAIChatToolWindow._BASE_SYSTEM_CARD_IDS 同步）
            system_card_ids = (
                "model_selector",
                "model_config",
                "memory",
                "history",
                "auto_loop_config",
                "auto_loop_running",
                "settings",
                "provider_edit",
                "mcp_edit",
                "hook_edit",
                "project_selector",
                "tool_control",
                "share",
                "history_questions",
            )
            for cid in system_card_ids:
                if mgr.is_card_visible(cid, window_id):
                    logger.info(f"[TabMode] 强制关闭系统卡片 [{cid}] window_id={window_id}")
                    mgr.hide_card(cid, window_id)
        except Exception as e:
            logger.warning(f"[TabMode] 强制关闭系统卡片时出错: {e}")

    @classmethod
    def _enable_mode(cls, tray_manager):
        """启用 Tab 模式：将所有独立窗口迁入"""
        tab_mgr = cls.get_instance()
        if tab_mgr is None:
            tab_mgr = cls.create_instance()

        if tab_mgr._is_transitioning:
            return
        tab_mgr._is_transitioning = True

        # 清理旧的 Tab 面板和内容区（防止上次 Tab 模式残留）
        for i in range(tab_mgr._tab_panel.count - 1, -1, -1):
            tab_mgr._tab_panel.remove_tab(i)
        while tab_mgr._content_area.count() > 1:
            w = tab_mgr._content_area.widget(1)
            tab_mgr._content_area.removeWidget(w)
        tab_mgr._windows.clear()
        tab_mgr._cached_dialogs.clear()
        tab_mgr._content_area.widget(0).show()  # 显示空状态，迁移后隐藏

        # 收集待迁入窗口
        pending = cls._collect_windows_to_migrate(tray_manager)
        logger.info(f"[TabMode] _enable_mode: 待迁入窗口数={len(pending)}")
        for dialog, tool in pending:
            logger.info(f"[TabMode]    dialog={dialog}, tool_instance={tool}")

        try:
            migrated_windows = []
            for dialog, tool_instance in pending:
                try:
                    # 如果有 dialog（ToolPopupDialog 模式），正确解绑
                    if dialog is not None and not _sip.isdeleted(dialog):
                        # 从 dialog 布局中移除 tool_instance（实际可能无效，因
                        # tool_instance 在 ToolPopupDialog 的 _fade_container 内层，
                        # 但 setParent 会处理真正的解绑）
                        old_layout = dialog.layout()
                        if old_layout:
                            old_layout.removeWidget(tool_instance)

                        # 隐藏 dialog（不 close/delete，避免触发 tool_instance.closeEvent）
                        dialog.hide()
                        tray_manager.unregister_window(dialog)
                    else:
                        # 无 dialog（兜底直采模式）：tool_instance 直接迁入
                        pass

                    # 迁移到 Tab 管理器
                    tool_instance.setParent(tab_mgr)
                    # 使用 add_window 统一处理（自动添加项、连接信号）
                    tab_mgr.add_window(tool_instance)

                    # ★ 关键修复：显式恢复窗口可见性
                    # dialog.hide() 会将 tool_instance 标记为隐藏(setParent 时 Qt
                    # 不自动恢复），而 QStackedWidget.setCurrentWidget 的内部 show()
                    # 在父级 QStackedWidget 未显示时可能无法正确设置可见状态。
                    # 这里显式 show() 确保标记正确，QStackedWidget 显示后会自动管理。
                    tool_instance.show()

                    migrated_windows.append(tool_instance)

                    # ★ 模式切换后强制关闭该窗口的所有系统卡片
                    # 多窗口模式下可能打开了设置/历史/记忆等系统卡片，这些卡片
                    # 的打开标记（CardManager.visible_cards + _system_cards_open）
                    # 在迁移后仍处于"已打开"状态 → 输入区域被隐藏且无法恢复。
                    # 这里强制关闭所有系统卡片，让卡片管理器触发 _on_system_card_closed
                    # 回调，从而恢复输入区域 + 清空系统卡片状态。
                    cls._force_close_system_cards(tool_instance)

                except Exception as e:
                    logger.error(f"[TabMode] 迁移窗口失败: {e}")
                    import traceback

                    logger.error(traceback.format_exc())

            # 更新 UI 状态
            if migrated_windows:
                tab_mgr._content_area.widget(0).hide()
                tab_mgr._tab_panel.set_active_index(0)
            else:
                tab_mgr._content_area.widget(0).show()

            # ★ 迁移后重新注册命令快捷键：窗口从 ToolPopupDialog 迁移到
            # TabManagerWindow 后，原有的 QShortcut 实例因其 parent widget
            # 的 window() 从 ToolPopupDialog 变为 TabManagerWindow，Qt
            # 内部 shortcut 上下文匹配可能失效（parentWidget()->window()
            # 变化后需重新注册才能保证 isActiveWindow() 判断正确）。
            # 对每个已迁入窗口重新调用 _register_command_shortcuts()，
            # 确保 QShortcut 与正确的窗口上下文绑定。
            for w in migrated_windows:
                try:
                    w._register_command_shortcuts()
                except Exception as exc:
                    logger.warning(f"[TabMode] 重新注册快捷键失败: {exc}")

            # 更新 Tray 菜单
            tray_manager._rebuild_context_menu()

            # 隐藏所有窗口的独立 EdgeLauncher
            for w in migrated_windows:
                _hide_edge_launcher(w)

            # Tab 模式下使用共享 Launcher（单例）
            tab_mgr._show_shared_launcher()

            # 显示 TabManagerWindow（位置由 showEvent 自动恢复）
            tab_mgr.show()
            tab_mgr.activateWindow()
            tab_mgr.raise_()

            logger.info(f"[TabMode] 已启用，迁入 {len(migrated_windows)} 个窗口")

        finally:
            tab_mgr._is_transitioning = False

    @classmethod
    def _disable_mode(cls, tray_manager):
        """禁用 Tab 模式：将所有窗口迁出为独立窗口"""
        from app.tool_popup import ToolPopupDialog

        tab_mgr = cls.get_instance()
        if tab_mgr is None:
            return

        if tab_mgr._is_transitioning:
            return
        tab_mgr._is_transitioning = True

        try:
            # 先清空引用，边恢复边移除
            windows = list(tab_mgr._windows)
            tab_mgr._windows.clear()

            for tool_instance in windows:
                try:
                    # 从 content_area 移除
                    tab_mgr._content_area.removeWidget(tool_instance)

                    # 确保标题栏尚存（_enable_mode 时可能被销毁），否则重新创建
                    title_bar = tool_instance.get_title_bar()
                    if title_bar is None or _sip.isdeleted(title_bar):
                        # 强制重建：先置 None 再调用 _init_title_bar
                        tool_instance._title_bar = None
                        tool_instance._init_title_bar()
                        title_bar = tool_instance.get_title_bar()

                    # 始终创建全新的 ToolPopupDialog（避免复用已关闭 dialog 的布局问题）
                    dialog = ToolPopupDialog(tool_instance, None)

                    # 确保标题栏可见
                    if title_bar and not _sip.isdeleted(title_bar):
                        title_bar.show()

                    # 恢复窗口位置：在屏幕中央显示
                    screen = QApplication.primaryScreen()
                    if screen:
                        rect = screen.availableGeometry()
                        dialog.setGeometry(
                            rect.x() + 50,
                            rect.y() + 50,
                            min(600, rect.width() - 100),
                            min(900, rect.height() - 100),
                        )

                    # 显示 dialog 并注册到 TrayManager
                    dialog.show()
                    dialog.activateWindow()
                    tray_manager.register_window(dialog)

                    # 恢复 EdgeLauncher
                    _show_edge_launcher(tool_instance)

                    # ★ 迁出后重新注册命令快捷键：窗口从 TabManagerWindow
                    # 移回独立 ToolPopupDialog，parent widget 的 window()
                    # 再次变化，需重新注册 QShortcut 以匹配新的窗口上下文。
                    try:
                        tool_instance._register_command_shortcuts()
                    except Exception as exc:
                        logger.warning(f"[TabMode] 迁出后重新注册快捷键失败: {exc}")

                except Exception as e:
                    logger.error(f"[TabMode] 恢复窗口失败: {e}")
                    import traceback

                    logger.error(traceback.format_exc())

            # 隐藏共享 Launcher（切换到独立模式后每个窗口使用自己的）
            tab_mgr._hide_shared_launcher()

            # 清空缓存
            tab_mgr._cached_dialogs.clear()
            tab_mgr.hide()

            # 更新 Tray 菜单
            tray_manager._tab_manager_window = None
            tray_manager._rebuild_context_menu()

            logger.info(f"[TabMode] 已禁用，{len(windows)} 个窗口恢复为独立模式")

        finally:
            tab_mgr._is_transitioning = False

    # ── 几何持久化（简化版）──

    def _save_geometry(self):
        """防抖：记录位置/尺寸，等拖拽/缩放结束后 200ms 再写盘"""
        self._geo_save_timer.start()  # 连续调用时不断重置计时器

    def _do_save_geometry(self):
        """实际写入配置（防抖回调，拖拽/缩放结束后执行一次）

        ★ 必须使用 geometry() 而不是 x()/y()：
        对于顶层窗口，x()/y() 返回 FRAME 在屏幕上的位置（含标题栏），
        而 setGeometry(x, y, w, h) 把 (x, y) 当作 CLIENT 区域位置。
        若保存 frame 位置再用 setGeometry 还原，每次恢复窗口会向上偏移
        title bar 高度（约 65px）—— 这就是 tab 模式最小化恢复后窗口
        「往上跑」的根因。
        """
        g = self.geometry()
        geo = {
            "x": g.x(),
            "y": g.y(),
            "w": g.width(),
            "h": g.height(),
        }
        Settings.get_instance().tab_manager_geometry.value = json.dumps(geo)
        # 保存面板宽度
        if hasattr(self, "_splitter"):
            sizes = self._splitter.sizes()
            if sizes:
                Settings.get_instance().tab_panel_width.value = sizes[0]

    def _restore_geometry(self):
        """恢复窗口位置（屏幕居中），确保不超出屏幕"""
        screen = QApplication.primaryScreen()
        screen_rect = screen.availableGeometry() if screen else None
        if not screen_rect:
            self.resize(960, 640)
            return

        w, h = 960, 640
        try:
            geo_str = Settings.get_instance().tab_manager_geometry.value
            if geo_str:
                g = json.loads(geo_str)
                x = max(screen_rect.x(), min(g["x"], screen_rect.right() - 100))
                y = max(screen_rect.y(), min(g["y"], screen_rect.bottom() - 50))
                self.setGeometry(x, y, g["w"], g["h"])
                return
        except Exception:
            pass

        # 首次：居中
        self.setGeometry(
            screen_rect.x() + (screen_rect.width() - w) // 2,
            screen_rect.y() + (screen_rect.height() - h) // 2,
            w,
            h,
        )

    def showEvent(self, event):
        """每次显示时恢复位置 + 启用 Windows 窗口阴影 + 圆角"""
        super().showEvent(event)
        self._restore_geometry()
        # 启用 DWM 窗口阴影 + 圆角（仅首次）
        if not getattr(self, "_shadow_enabled", False):
            self._enable_shadow()
            self._apply_rounded_corners()

    def _enable_shadow(self):
        """通过 DWM API 为 Frameless 窗口启用原生阴影 (Windows only)

        使用 DwmExtendFrameIntoClientArea + WM_NCCALCSIZE 返回 0
        的标准方案保留 Windows 窗口阴影。
        """
        if platform.system() != "Windows":
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            margins = ctypes.wintypes.RECT(-1, -1, -1, -1)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(ctypes.wintypes.HWND(hwnd), ctypes.byref(margins))
            self._shadow_enabled = True
        except Exception:
            self._shadow_enabled = False

    def _apply_rounded_corners(self):
        """通过 DWM API 为窗口启用圆角 (Windows 11)

        使用 DWMWA_WINDOW_CORNER_PREFERENCE (33) 设置圆角风格，
        DWMWCP_ROUND (2) 为标准圆角。
        Windows 11 原生支持该属性，最大化时自动变为直角。
        """
        if platform.system() != "Windows":
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
            corner_pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.wintypes.HWND(hwnd),
                33,
                ctypes.byref(corner_pref),
                ctypes.sizeof(ctypes.c_int),
            )
        except Exception:
            pass

    def moveEvent(self, event):
        super().moveEvent(event)
        self._save_geometry()

    def _on_titlebar_max_restore(self):
        """标题栏最大化/还原按钮触发"""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _on_titlebar_close(self):
        """标题栏关闭按钮触发（隐藏窗口，不销毁）"""
        self.hide()

    def changeEvent(self, event):
        """监听窗口状态变化（最大化/还原），同步标题栏按钮图标"""
        if event.type() == event.WindowStateChange:
            is_maxed = self.isMaximized()
            if hasattr(self, "_title_bar"):
                self._title_bar.set_maximized(is_maxed)
            # 最大化时不需要 size grip
            if hasattr(self, "_size_grip"):
                self._size_grip.setVisible(not is_maxed)
        super().changeEvent(event)

    def _nchittest(self, msg) -> int:
        """WM_NCHITTEST 处理：边缘缩放

        拖拽期间（_is_dragging=True）完全跳过，由 Python mouseMoveEvent 负责。
        """
        import ctypes.wintypes as wintypes

        x = wintypes.LOWORD(msg.lParam)
        y = wintypes.HIWORD(msg.lParam)
        pt = self.mapFromGlobal(QPoint(x, y))

        # 标题栏区域 → 返回 HTCLIENT，让 Qt 正常处理鼠标事件
        # drag 由 mousePressEvent 中的 WM_NCLBUTTONDOWN 发起
        if self._title_bar.geometry().contains(pt):
            return _HTCLIENT

        # 边缘缩放热区（_EDGE_RESIZE_BORDER px）
        w, h = self.width(), self.height()
        left = pt.x() < _EDGE_RESIZE_BORDER
        right = pt.x() > w - _EDGE_RESIZE_BORDER
        top = pt.y() < _EDGE_RESIZE_BORDER
        bottom = pt.y() > h - _EDGE_RESIZE_BORDER

        if top and left:
            return _HTTOPLEFT
        if top and right:
            return _HTTOPRIGHT
        if bottom and left:
            return _HTBOTTOMLEFT
        if bottom and right:
            return _HTBOTTOMRIGHT
        if top:
            return _HTTOP
        if bottom:
            return _HTBOTTOM
        if left:
            return _HTLEFT
        if right:
            return _HTRIGHT
        return _HTCLIENT

    def nativeEvent(self, eventType, message):
        """处理 Windows 原生消息

        - WM_NCHITTEST: 边缘缩放（_EDGE_RESIZE_BORDER px 热区）
        - WM_NCCALCSIZE: 保留 DWM 阴影
        拖拽期间 _nchittest 被跳过，消除与 Python 拖拽的冲突。
        """
        if platform.system() == "Windows" and eventType == "windows_generic_MSG":
            try:
                import ctypes

                msg = ctypes.cast(int(message), ctypes.POINTER(_WINDOWS_MSG))[0]
                # 拖拽期间跳过 WM_NCHITTEST——避免 _nchittest 与 Python
                # mouseMoveEvent 中的 win.move() 产生双重开销。边缘缩放
                # 在拖拽期间也不需要。
                if msg.message == _WM_NCHITTEST:
                    if self._is_dragging:
                        return (True, _HTCLIENT)
                    return (True, self._nchittest(msg))
                if msg.message == 0x0083:  # WM_NCCALCSIZE
                    return (True, 0)
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def resizeEvent(self, event):
        """保持右下角 QSizeGrip 的位置 + 防抖保存几何"""
        super().resizeEvent(event)
        if hasattr(self, "_size_grip") and not self.isMaximized():
            g = self._size_grip
            g.move(self.width() - g.width() - 2, self.height() - g.height() - 2)
        self._save_geometry()  # 防抖，缩放结束后才真正写盘

    def closeEvent(self, event: QCloseEvent):
        """关闭 TabManagerWindow 时不销毁，仅隐藏"""
        event.ignore()
        self.hide()

    # ── 资源清理 ──

    def cleanup(self):
        """清理所有窗口和资源"""
        TabManagerWindow._instance = None
        for w in list(self._windows):
            try:
                w.close()
            except Exception:
                pass
        self._windows.clear()
        self._cached_dialogs.clear()
        self.close()
