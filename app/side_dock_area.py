# -*- coding: utf-8 -*-
import platform
from PyQt5.QtCore import Qt, QSize, QTimer, QEvent, QPoint, pyqtSignal
from PyQt5.QtGui import QPainter, QColor
from PyQt5.QtWidgets import (
    QWidget,
    QStackedWidget,
    QDialog,
    QVBoxLayout,
    QApplication,
)
from loguru import logger
from qfluentwidgets import (
    isDarkTheme,
    FluentIcon as FIF,
    TransparentToolButton,
    FluentIcon,
)

from app.tray_manager import TrayManager
from app.utils.config import Settings
from app.utils.utils import get_icon


class OpacitySlider(QWidget):
    opacityChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._opacity = 100
        self.setFixedWidth(36)
        self.setFixedHeight(200)
        self._is_dragging = False
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._knob_height = 12
        self._track_padding = 10

    def paintEvent(self, e):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        bg_color = (
            QColor(38, 38, 38, 230) if isDarkTheme() else QColor(245, 245, 245, 230)
        )
        painter.setBrush(bg_color)
        painter.drawRoundedRect(self.rect(), 8, 8)

        track_height = self.height() - 2 * self._track_padding
        track_width = 4
        track_x = (self.width() - track_width) // 2
        track_y = self._track_padding

        track_bg = (
            QColor(100, 100, 100, 150) if isDarkTheme() else QColor(180, 180, 180, 150)
        )
        painter.setBrush(track_bg)
        painter.drawRoundedRect(track_x, track_y, track_width, track_height, 2, 2)

        fill_height = int(track_height * self._opacity / 100)
        fill_color = QColor("#0078d4")
        painter.setBrush(fill_color)
        painter.drawRoundedRect(
            track_x,
            track_y + track_height - fill_height,
            track_width,
            fill_height,
            2,
            2,
        )

        knob_y = track_y + track_height - fill_height - self._knob_height // 2
        knob_color = QColor(255, 255, 255) if isDarkTheme() else QColor(80, 80, 80)
        painter.setBrush(knob_color)
        painter.drawEllipse(
            QPoint(self.width() // 2, knob_y + self._knob_height // 2), 7, 7
        )

        painter.setPen(QColor(200, 200, 200) if isDarkTheme() else QColor(80, 80, 80))
        painter.setFont(self.font())
        painter.drawText(
            self.rect(), Qt.AlignBottom | Qt.AlignHCenter, f"{self._opacity}%"
        )

    def setOpacity(self, value: int, *, emit_change: bool = True):
        clamped = max(0, min(100, value))
        changed = clamped != self._opacity
        self._opacity = clamped
        self.update()
        if emit_change and changed:
            self.opacityChanged.emit(self._opacity)

    def opacity(self) -> int:
        return self._opacity

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._is_dragging = True
            self._update_from_mouse(e.pos())
            self.update()

    def mouseMoveEvent(self, e):
        if self._is_dragging:
            self._update_from_mouse(e.pos())
            self.update()

    def mouseReleaseEvent(self, e):
        self._is_dragging = False

    def enterEvent(self, e):
        super().enterEvent(e)
        if (
            hasattr(self.parent(), "_hide_timer")
            and self.parent()._hide_timer.isActive()
        ):
            self.parent()._hide_timer.stop()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        if hasattr(self.parent(), "_hide_timer"):
            self.parent()._hide_timer.start()

    def _update_from_mouse(self, pos: QPoint):
        track_height = self.height() - 2 * self._track_padding
        rel_y = pos.y() - self._track_padding
        value = int((1 - rel_y / track_height) * 100)
        self.setOpacity(value)

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        self.setOpacity(self._opacity + (delta // 120) * 5)


class LockButtonWidget(QWidget):
    """独立的锁定按钮小部件，在穿透模式下可独立显示和交互"""
    lockClicked = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_locked = False
        self.setFixedSize(26, 26)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._setup_ui()
        self._update_icon()

    def _setup_ui(self):
        from qfluentwidgets import ToolButton
        self._btn = ToolButton(self)
        self._btn.setFixedSize(26, 26)
        self._btn.clicked.connect(self._on_click)
        self._btn.setIconSize(QSize(16, 16))
        self._btn.move(0, 0)

    def _on_click(self):
        self._is_locked = not self._is_locked
        self._update_icon()
        self.lockClicked.emit(self._is_locked)

    def _update_icon(self):
        if self._is_locked:
            self._btn.setIcon(get_icon("锁定"))
            self._btn.setToolTip("取消锁定（恢复交互）")
            self._btn.setStyleSheet("""
                QToolButton {
                    background-color: rgba(0, 120, 212, 200);
                    border-radius: 4px;
                    color: #e0e0e0;
                }
                QToolButton:hover {
                    background-color: rgba(0, 120, 212, 240);
                }
                QToolButton:pressed {
                    background-color: rgba(0, 120, 212, 180);
                }
            """)
        else:
            self._btn.setIcon(get_icon("解锁"))
            self._btn.setToolTip("锁定窗口（鼠标穿透）")
            self._btn.setStyleSheet("""
                QToolButton {
                    background-color: transparent;
                    border-radius: 4px;
                    color: #c0c0c0;
                }
                QToolButton:hover {
                    background-color: rgba(255, 255, 255, 15);
                    color: #ffffff;
                }
            """)

    def setLocked(self, locked: bool):
        if self._is_locked != locked:
            self._is_locked = locked
            self._update_icon()

    def isLocked(self) -> bool:
        return self._is_locked

    def paintEvent(self, e):
        # 深色背景，和标题栏风格一致
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        bg_color = QColor(45, 45, 45)  # 深色背景
        painter.setBrush(bg_color)
        painter.drawRoundedRect(self.rect(), 4, 4)


class AdaptiveStackedWidget(QStackedWidget):
    def sizeHint(self) -> QSize:
        current = self.currentWidget()
        return current.sizeHint() if current else QSize(0, 0)

    def minimumSizeHint(self) -> QSize:
        current = self.currentWidget()
        return current.minimumSizeHint() if current else QSize(0, 0)


class ResizeEdge(QWidget):
    """边缘拖拽区域"""

    EDGE_NONE = 0
    EDGE_TOP = 1
    EDGE_BOTTOM = 2
    EDGE_LEFT = 4
    EDGE_RIGHT = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._edge = ResizeEdge.EDGE_NONE
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)
        self._update_cursor()

    def set_edge(self, edge):
        self._edge = edge
        self._update_cursor()

    def _update_cursor(self):
        if self._edge == ResizeEdge.EDGE_TOP or self._edge == ResizeEdge.EDGE_BOTTOM:
            self.setCursor(Qt.SizeVerCursor)
        elif self._edge == ResizeEdge.EDGE_LEFT or self._edge == ResizeEdge.EDGE_RIGHT:
            self.setCursor(Qt.SizeHorCursor)
        elif self._edge == (
            ResizeEdge.EDGE_TOP | ResizeEdge.EDGE_LEFT
        ) or self._edge == (ResizeEdge.EDGE_BOTTOM | ResizeEdge.EDGE_RIGHT):
            self.setCursor(Qt.SizeFDiagCursor)
        elif self._edge == (
            ResizeEdge.EDGE_TOP | ResizeEdge.EDGE_RIGHT
        ) or self._edge == (ResizeEdge.EDGE_BOTTOM | ResizeEdge.EDGE_LEFT):
            self.setCursor(Qt.SizeBDiagCursor)
        else:
            self.setCursor(Qt.ArrowCursor)


class ToolPopupDialog(QDialog):
    popupClosed = pyqtSignal(str, bool, object)
    globalOpacityChanged = pyqtSignal(float)  # 透明度变化信号，参数为 0.0-1.0

    def __init__(self, tool_instance, parent=None, border_color: str = "none"):
        super().__init__(parent)
        self.tool_instance = tool_instance
        self._border_color = border_color
        self._drag_pos = None
        self._is_maximized = False
        self._restore_tool_name = None
        self._restore_was_in_top = False
        self._restore_btn = None
        self._normal_geometry = None
        self._is_closing = False
        self._was_minimized = False  # 标记是否刚从最小化恢复
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(300)  # 增加防抖，减少频繁保存
        self._geometry_save_timer.timeout.connect(self._save_geometry)
        self._resize_edge = ResizeEdge.EDGE_NONE
        self._resize_start_geometry = None
        self._edge_size = 6  # 边缘检测区域宽度
        self.setWindowTitle(tool_instance.name)
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(400, 300)
        self.setSizeGripEnabled(True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        title_bar = tool_instance.get_title_bar()
        title_bar.popupRequested.disconnect()
        title_bar.popupRequested.connect(self.close)
        title_bar.show()

        # macOS: 使用 hide/show 代替 showMinimized/showNormal
        if platform.system() == "Darwin":
            title_bar._min_btn.clicked.connect(self.hide)
        else:
            title_bar._min_btn.clicked.connect(self.showMinimized)

        # 隐藏标题栏的锁定按钮，改用独立的 LockButtonWidget
        lock_btn = title_bar._lock_btn
        lock_btn.hide()

        main_layout.addWidget(title_bar)
        main_layout.addWidget(tool_instance, 1)

        self.destroyed.connect(self._on_destroyed)

        self._opacity_slider = None
        self._original_opacity = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(200)
        self._hide_timer.timeout.connect(self._check_hide_slider)
        self.setMouseTracking(True)

        self._lock_mode = False
        self._slider_desktop_pos = None

        # 注册到全局 TrayManager（确保只有一个托盘图标）
        TrayManager.get_instance().register_window(self)

        # 创建独立的锁定按钮（在穿透模式下仍可交互）
        self._lock_btn_widget = LockButtonWidget()
        self._lock_btn_widget.lockClicked.connect(self._on_lock_changed)

        # 连接标题栏锁定信号（用于同步状态）
        title_bar.lockRequested.connect(self._on_title_bar_lock_changed)

        # macOS: 始终安装事件过滤器监听应用激活（Dock 点击）
        if platform.system() == "Darwin":
            QApplication.instance().installEventFilter(self)
            logger.info("[DockRestore] EventFilter installed for macOS Dock restore")

    def _on_title_bar_lock_changed(self, locked: bool):
        """响应标题栏锁定信号，同步到独立锁定按钮"""
        self._lock_btn_widget.setLocked(locked)

    def _on_lock_changed(self, locked: bool):
        """处理窗口锁定状态变化"""
        self._lock_mode = locked
        if locked:
            self._reparent_lock_btn_to_desktop()
        self._set_window_passthrough(locked)
        if not locked:
            self._reparent_lock_btn_to_dialog()
        self._sync_lock_btn_position()

    def _reparent_lock_btn_to_desktop(self):
        """重新设置 lock button widget 的父对象为桌面，使其在穿透模式下仍可交互"""
        if self._lock_btn_widget:
            # 在透明度条上方，透明度条右边再往左一个按钮宽度
            pos = self.mapToGlobal(QPoint(self.width() + 3, 3))
            self._lock_btn_widget.setParent(None)
            self._lock_btn_widget.move(pos)
            self._lock_btn_widget.show()
            self._lock_btn_widget.raise_()

    def _reparent_lock_btn_to_dialog(self):
        """恢复 lock button widget 的父对象为对话框"""
        if self._lock_btn_widget:
            # 保持为独立窗口，只是改变父对象
            self._sync_lock_btn_position()
            self._lock_btn_widget.show()

    def _sync_lock_btn_position(self):
        """同步 lock button 位置到透明度条上方"""
        if self._lock_btn_widget:
            # 在透明度条上方
            pos = self.mapToGlobal(QPoint(self.width() + 3, 3))
            self._lock_btn_widget.move(pos)

    def _reparent_slider_to_dialog(self):
        """恢复 opacity slider 的父对象为对话框"""
        if self._opacity_slider:
            self._opacity_slider.setParent(self)
            self._opacity_slider.hide()

    def _get_current_screen(self):
        """获取当前窗口所在的屏幕索引"""
        desktop = QApplication.desktop()
        return desktop.screenNumber(self)

    def _sync_slider_position(self):
        """同步 slider 位置到对话框右侧（锁定按钮下方）"""
        if self._opacity_slider and not self._lock_mode:
            pos = self.mapToGlobal(QPoint(self.width(), 10 + 30))  # 往下移30px，避开锁定按钮
            self._opacity_slider.move(pos)

    def _set_window_passthrough(self, enabled: bool):
        """使用 Windows API 实现真正的鼠标穿透到下层软件（仅 Windows）"""
        import platform

        # macOS 不支持这种方式，透明穿透在 macOS 上会导致窗口系统问题
        if platform.system() != "Windows":
            logger.debug(f"[ToolPopupDialog] 穿透模式仅支持 Windows，macOS 跳过")
            return

        import ctypes

        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020

        user32 = ctypes.windll.user32
        GetWindowLongW = user32.GetWindowLongW
        SetWindowLongW = user32.SetWindowLongW

        hwnd = int(self.winId())

        if enabled:
            ex_style = GetWindowLongW(hwnd, GWL_EXSTYLE)
            SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_TRANSPARENT)
        else:
            ex_style = GetWindowLongW(hwnd, GWL_EXSTYLE)
            SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style & ~WS_EX_TRANSPARENT)

    def _show_settings(self):
        """ "显示设置弹窗 - 已被移除，按钮已移到主窗口"""
        pass

    def setRestoreInfo(self, tool_name, was_in_top, btn):
        self._restore_tool_name = tool_name
        self._restore_was_in_top = was_in_top
        self._restore_btn = btn

    def showEvent(self, event):
        super().showEvent(event)
        self._restore_geometry()
        self.tool_instance.show()
        # 显示锁定按钮在窗口右侧
        self._sync_lock_btn_position()
        self._lock_btn_widget.show()

    def hideEvent(self, event):
        """窗口隐藏时（包括最小化）同步隐藏锁定按钮"""
        super().hideEvent(event)
        if self._lock_btn_widget:
            self._lock_btn_widget.hide()

    def _restore_geometry(self):
        from PyQt5.QtCore import QSettings

        settings = QSettings("WorkFlowGUI", "ToolPopup")
        key = f"popup_geometry_{self.tool_instance.name}"
        geometry = settings.value(key)
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(600, 450)
            self._center_on_screen()

    def _save_geometry(self):
        from PyQt5.QtCore import QSettings

        if self._is_maximized:
            return
        settings = QSettings("WorkFlowGUI", "ToolPopup")
        key = f"popup_geometry_{self.tool_instance.name}"
        settings.setValue(key, self.saveGeometry())

    def _center_on_screen(self):
        from PyQt5.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen:
            rect = screen.availableGeometry()
            x = (rect.width() - self.width()) // 2 + rect.x()
            y = (rect.height() - self.height()) // 2 + rect.y()
            self.move(x, y)

    def keyPressEvent(self, event):
        # ESC 不做任何操作，忽略事件
        if event.key() == Qt.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._is_closing:
            event.accept()
            return
        self._is_closing = True
        # 关闭时同时隐藏锁定按钮
        if self._lock_btn_widget:
            self._lock_btn_widget.hide()

        # 通知 tool_instance 标记为已销毁，防止异步回调继续执行
        try:
            from PyQt5 import sip
            if self.tool_instance and not sip.isdeleted(self.tool_instance):
                self.tool_instance._is_destroyed = True
                # 通知父窗口移除引用，防止内存泄漏
                if hasattr(self.tool_instance, '_popup_refs'):
                    refs = list(self.tool_instance._popup_refs)
                    if self in refs:
                        refs.remove(self)
                        self.tool_instance._popup_refs = refs
        except Exception:
            pass
        
        self.deleteLater()
        super().closeEvent(event)

    def changeEvent(self, event):
        """监听窗口状态变化"""
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            logger.debug(f"[DockRestore] WindowStateChange - isMinimized={self.isMinimized()}, isVisible={self.isVisible()}")
            if platform.system() == "Darwin":
                # macOS: 如果窗口被隐藏了，尝试恢复
                if not self.isVisible():
                    logger.info("[DockRestore] Window hidden on macOS, showing...")
                    self._restore_geometry()
                    self.show()
                    self.activateWindow()
                    self.raise_()
                    if self._lock_btn_widget:
                        self._sync_lock_btn_position()
                        self._lock_btn_widget.show()

    def _restore_geometry(self):
        """恢复窗口几何信息"""
        from PyQt5.QtCore import QSettings

        settings = QSettings("WorkFlowGUI", "ToolPopup")
        key = f"popup_geometry_{self.tool_instance.name}"
        geometry = settings.value(key)
        if geometry:
            self.restoreGeometry(geometry)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        opacity = self.windowOpacity()

        if isDarkTheme():
            bg_color = QColor(38, 38, 38, int(255 * opacity))
            shadow_color = QColor(0, 0, 0, int(120 * opacity))
        else:
            bg_color = QColor(245, 245, 245, int(255 * opacity))
            shadow_color = QColor(0, 0, 0, int(50 * opacity))

        # 根据配置设置边框颜色
        border_color_map = {
            "white": QColor(255, 255, 255, int(255 * opacity)),
            "yellow": QColor(255, 200, 0, int(255 * opacity)),
        }
        if self._border_color == "none":
            if isDarkTheme():
                border_color = QColor(55, 55, 55, int(255 * opacity))
            else:
                border_color = QColor(200, 200, 200, int(255 * opacity))
        else:
            border_color = border_color_map.get(
                self._border_color,
                QColor(55, 55, 55, int(255 * opacity)),
            )

        painter.setBrush(shadow_color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(4, 4, self.width() - 4, self.height() - 4, 10, 10)

        painter.setBrush(bg_color)
        painter.setPen(border_color)
        painter.drawRoundedRect(0, 0, self.width() - 4, self.height() - 4, 10, 10)

    def _get_edge_at_pos(self, pos):
        """检测指定位置位于哪个边缘"""
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        edge = ResizeEdge.EDGE_NONE

        # 检测顶部边缘
        if y < self._edge_size:
            edge |= ResizeEdge.EDGE_TOP
        # 检测底部边缘
        elif y > h - self._edge_size:
            edge |= ResizeEdge.EDGE_BOTTOM
        # 检测左边缘
        if x < self._edge_size:
            edge |= ResizeEdge.EDGE_LEFT
        # 检测右边缘
        elif x > w - self._edge_size:
            edge |= ResizeEdge.EDGE_RIGHT

        return edge

    def _perform_resize(self, global_pos):
        """执行边缘缩放"""
        if self._resize_edge == ResizeEdge.EDGE_NONE or not self._resize_start_geometry:
            return

        delta = global_pos - self._resize_start_pos
        geom = self._resize_start_geometry
        x, y, w, h = geom.x(), geom.y(), geom.width(), geom.height()
        min_w, min_h = self.minimumSize().width(), self.minimumSize().height()

        edge = self._resize_edge

        # 处理左右边缘
        if edge & ResizeEdge.EDGE_LEFT:
            new_x = x + delta.x()
            new_w = w - delta.x()
            if new_w >= min_w:
                x = new_x
                w = new_w
        elif edge & ResizeEdge.EDGE_RIGHT:
            w = max(min_w, w + delta.x())

        # 处理上下边缘
        if edge & ResizeEdge.EDGE_TOP:
            new_y = y + delta.y()
            new_h = h - delta.y()
            if new_h >= min_h:
                y = new_y
                h = new_h
        elif edge & ResizeEdge.EDGE_BOTTOM:
            h = max(min_h, h + delta.y())

        self.setGeometry(x, y, w, h)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            title_bar = self.tool_instance.get_title_bar()
            if title_bar and event.y() < title_bar.height():
                self._hide_opacity_slider()
                self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
                event.accept()
                return

            # 检查是否在边缘区域开始拖拽
            edge = self._get_edge_at_pos(event.pos())
            if edge != ResizeEdge.EDGE_NONE:
                self._resize_edge = edge
                self._resize_start_pos = event.globalPos()
                self._resize_start_geometry = self.geometry()
                self._hide_opacity_slider()
                event.accept()
                return

    def mouseMoveEvent(self, event):
        # 始终更新光标（不受拖拽状态影响）
        title_bar = self.tool_instance.get_title_bar()
        title_height = title_bar.height() if title_bar else 0
        if event.y() <= title_height:
            # 标题栏区域：保持正常光标
            self.setCursor(Qt.ArrowCursor)
        else:
            # 内容区域：根据边缘位置更新光标
            edge = self._get_edge_at_pos(event.pos())
            if edge == ResizeEdge.EDGE_TOP or edge == ResizeEdge.EDGE_BOTTOM:
                self.setCursor(Qt.SizeVerCursor)
            elif edge == ResizeEdge.EDGE_LEFT or edge == ResizeEdge.EDGE_RIGHT:
                self.setCursor(Qt.SizeHorCursor)
            elif edge == (ResizeEdge.EDGE_TOP | ResizeEdge.EDGE_LEFT) or edge == (
                ResizeEdge.EDGE_BOTTOM | ResizeEdge.EDGE_RIGHT
            ):
                self.setCursor(Qt.SizeFDiagCursor)
            elif edge == (ResizeEdge.EDGE_TOP | ResizeEdge.EDGE_RIGHT) or edge == (
                ResizeEdge.EDGE_BOTTOM | ResizeEdge.EDGE_LEFT
            ):
                self.setCursor(Qt.SizeBDiagCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

        if event.buttons() == Qt.LeftButton:
            # 正在边缘拖拽缩放
            if self._resize_edge != ResizeEdge.EDGE_NONE:
                self._perform_resize(event.globalPos())
                event.accept()
                return

            # 标题栏拖拽移动
            if self._drag_pos:
                self.move(event.globalPos() - self._drag_pos)
                event.accept()
                return
        else:
            self._show_opacity_slider()
            self._hide_timer_start()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self._resize_edge = ResizeEdge.EDGE_NONE
        self._resize_start_geometry = None
        if event.button() == Qt.LeftButton:
            self._save_geometry()
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._is_closing:
            self._geometry_save_timer.start()
            # 同步 lock button 和 opacity slider 位置
            if self._lock_mode:
                self._reparent_lock_btn_to_desktop()
            else:
                self._sync_lock_btn_position()
                if self._opacity_slider and self._opacity_slider.isVisible():
                    self._sync_slider_position()

    def moveEvent(self, event):
        super().moveEvent(event)
        if self._is_maximized or self._is_closing:
            return
        self._geometry_save_timer.start()
        # 同步 lock button 位置
        if self._lock_mode:
            self._reparent_lock_btn_to_desktop()
        else:
            self._sync_lock_btn_position()

    def _on_destroyed(self):
        if hasattr(self.tool_instance, "set_allowed_update"):
            self.tool_instance.set_allowed_update(False)
        # 从全局 TrayManager 注销
        try:
            from app.tray_manager import TrayManager
            TrayManager.get_instance().unregister_window(self)
        except Exception:
            pass

    def _show_opacity_slider(self):
        if self._opacity_slider is None:
            self._opacity_slider = OpacitySlider(self)
            self._opacity_slider.opacityChanged.connect(self._on_opacity_changed)
        # 同步滑块位置但不触发 opacityChanged 信号（避免无意义的重绘链）
        self._opacity_slider.setOpacity(int(self.windowOpacity() * 100), emit_change=False)
        if self._lock_mode:
            self._reparent_slider_to_desktop()
        else:
            self._opacity_slider.setParent(self)
            pos = self.mapToGlobal(QPoint(self.width(), 10 + 30))  # 往下移30px
            self._opacity_slider.move(pos)
            self._opacity_slider.show()
            self._opacity_slider.raise_()

    def _hide_opacity_slider(self):
        if self._opacity_slider:
            self._opacity_slider.hide()

    def _hide_timer_start(self):
        self._hide_timer.start()

    def _on_opacity_changed(self, value: int):
        self.setWindowOpacity(value / 100)
        self.globalOpacityChanged.emit(value / 100)

    def _check_hide_slider(self):
        if not self._opacity_slider or self._opacity_slider._is_dragging:
            return
        # 窗口非激活时跳过检查，避免鼠标在其他应用移动时误触发 show/hide 循环
        if not self.isActiveWindow():
            self._hide_opacity_slider()
            return
        slider_pos = self._opacity_slider.mapFromGlobal(self.cursor().pos())
        if self._opacity_slider.rect().contains(slider_pos):
            return
        dialog_pos = self.mapFromGlobal(self.cursor().pos())
        if not self.rect().contains(dialog_pos):
            self._hide_opacity_slider()

    def eventFilter(self, obj, event):
        if obj == self._popup_btn and event.type() == QEvent.Enter:
            self._popup_btn.setStyleSheet(
                "background-color: #e81123; border-radius: 4px;"
            )
        elif obj == self._popup_btn and event.type() == QEvent.Leave:
            self._popup_btn.setStyleSheet("")
        
        # macOS: 监听应用激活事件，当 Dock 图标被点击时恢复窗口
        if platform.system() == "Darwin":
            if event.type() == QEvent.ApplicationActivate:
                logger.info(f"[DockRestore] ApplicationActivate - isMinimized={self.isMinimized()}, isVisible={self.isVisible()}")
                if self.isMinimized() or not self.isVisible():
                    logger.info("[DockRestore] Restoring from minimized/hidden...")
                    self._was_minimized = False
                    self._restore_geometry()
                    self.show()
                    self.activateWindow()
                    self.raise_()
                    self.setFocus()
                    if self._lock_btn_widget:
                        self._sync_lock_btn_position()
                        self._lock_btn_widget.show()
                    logger.info("[DockRestore] Restored via ApplicationActivate")
        
        return super().eventFilter(obj, event)

    def enterEvent(self, e):
        super().enterEvent(e)
        # 仅在窗口激活时显示透明度滑块，避免鼠标在其他窗口移动时反复触发
        if self.isActiveWindow():
            self._show_opacity_slider()
            self._hide_timer.stop()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._hide_timer_start()
