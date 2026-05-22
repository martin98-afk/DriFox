# -*- coding: utf-8 -*-
"""
全局唯一托盘图标管理器（单例）
所有 ToolPopupDialog 共享同一个 QSystemTrayIcon，避免多个托盘图标。
"""
import platform
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QSystemTrayIcon, QMenu, QAction, QApplication
from loguru import logger


class TrayManager(QObject):
    """全局唯一托盘图标管理器，管理所有聊天窗口的托盘行为"""
    
    # 信号：当窗口数量变为0时发出
    allWindowsClosed = pyqtSignal()

    _instance = None

    @classmethod
    def get_instance(cls) -> "TrayManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def window_count(self) -> int:
        """返回当前活跃窗口数量"""
        # 过滤掉已销毁的窗口引用
        valid_windows = [w for w in self._windows if self._is_window_valid(w)]
        return len(valid_windows)

    def _is_window_valid(self, w) -> bool:
        """检查窗口是否仍然有效"""
        try:
            return w is not None
        except Exception:
            return False

    def __init__(self, parent=None):
        super().__init__(parent)
        if TrayManager._instance is not None:
            raise RuntimeError("TrayManager 是单例，请使用 get_instance() 获取")
        TrayManager._instance = self

        self._windows: list = []  # 已注册的 ToolPopupDialog 列表

        # 创建托盘图标
        self._tray_icon = QSystemTrayIcon(self)
        self._tray_icon.setIcon(QIcon(":/icons/drifox.ico"))
        self._tray_icon.setToolTip("Drifox")

        # 创建右键菜单
        tray_menu = QMenu()
        show_action = QAction("显示窗口", tray_menu)
        show_action.triggered.connect(self._show_all_windows)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        quit_action = QAction("退出", tray_menu)
        quit_action.triggered.connect(self._quit_application)
        tray_menu.addAction(quit_action)

        self._tray_icon.setContextMenu(tray_menu)
        
        # 监听托盘图标点击（Windows: 单击恢复窗口）
        if platform.system() == "Windows":
            self._tray_icon.activated.connect(self._on_tray_activated)
        
        self._tray_icon.show()

        logger.info("TrayManager 初始化完成")

    def register_window(self, window) -> None:
        """注册一个窗口到托盘管理器"""
        if window not in self._windows:
            self._windows.append(window)
            logger.debug(f"窗口已注册到 TrayManager: {window.windowTitle()}")

    def unregister_window(self, window) -> None:
        """窗口销毁时注销"""
        if window in self._windows:
            self._windows.remove(window)
            logger.debug(f"窗口已从 TrayManager 注销: {window.windowTitle()}")

    def notify(self, title: str, message: str) -> None:
        """发送 Windows 通知"""
        if self._tray_icon.isVisible():
            self._tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon(1), 4000)

    def _show_all_windows(self) -> None:
        """显示所有已注册的窗口"""
        has_visible = False
        for w in list(self._windows):
            try:
                if w.isHidden():
                    w.show()
                    w.activateWindow()
                    if w.isMinimized():
                        w.showNormal()
                    w.raise_()
                    has_visible = True
                else:
                    w.activateWindow()
                    has_visible = True
            except RuntimeError:
                # 窗口已被 C++ 销毁，清理引用
                self._windows = [x for x in self._windows if x is not w]
        
        if not has_visible and self._windows:
            # 没有可见窗口，显示第一个
            w = self._windows[0]
            try:
                w.show()
                w.activateWindow()
                w.raise_()
            except RuntimeError:
                pass

    def _on_tray_activated(self, reason):
        """Windows 托盘图标点击处理"""
        # QSystemTrayIcon.Trigger = 单击, DoubleClick = 双击
        if reason == QSystemTrayIcon.Trigger:
            self._show_or_create_window()

    def _show_or_create_window(self) -> None:
        """显示所有窗口或创建新窗口"""
        logger.info(f"[_show_or_create] windows count: {len(self._windows)}")
        
        if not self._windows:
            logger.warning("[_show_or_create] 没有已注册的窗口")
            return
        
        # 找到所有有效的窗口（包括隐藏的）
        valid_windows = [w for w in self._windows if self._is_window_valid(w)]
        
        if not valid_windows:
            logger.warning("[_show_or_create] 没有有效窗口")
            return
        
        # 显示第一个有效窗口
        w = valid_windows[0]
        try:
            logger.info(f"[_show_or_create] 显示窗口: {w.windowTitle()}")
            w.show()
            w.activateWindow()
            w.raise_()
            if w.isMinimized():
                w.showNormal()
        except Exception as e:
            logger.error(f"[_show_or_create] 显示窗口失败: {e}")

    def _quit_application(self) -> None:
        """退出应用：强制关闭所有窗口后退出"""
        # 先注销所有窗口，防止 closeEvent 再次调用 unregister
        all_windows = list(self._windows)
        self._windows.clear()
        
        for w in all_windows:
            try:
                if hasattr(w, "_is_closing"):
                    w._is_closing = True
                w.close()
            except Exception:
                pass
        
        QApplication.instance().quit()
