# -*- coding: utf-8 -*-
"""
全局唯一托盘图标管理器（单例）
所有 ToolPopupDialog 共享同一个 QSystemTrayIcon，避免多个托盘图标。
"""

import ctypes
import math
import platform
import time
import uuid
from pathlib import Path

import keyboard
from loguru import logger
from PyQt5.QtCore import QAbstractNativeEventFilter, QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon


class _HotkeyBridge(QObject):
    """从 keyboard 库线程桥接到 Qt 主线程的信号桥"""

    toggle_all_windows = pyqtSignal()


# ========== Windows 原生全局热键（RegisterHotKey）相关 ==========
# 使用 RegisterHotKey 而非 keyboard 库的 LL 钩子：前者是 Windows 官方的
# 应用级热键机制，不受 LL 钩子被 Windows 静默移除（睡眠/锁屏/UAC/300ms 超时）
# 的影响，是唯一能根治 toggle-window 热键“用久了失效”的方案。
if platform.system() == "Windows":
    _user32 = ctypes.windll.user32
    _RegisterHotKey = _user32.RegisterHotKey
    _RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    _RegisterHotKey.restype = ctypes.c_bool
    _UnregisterHotKey = _user32.UnregisterHotKey
    _UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _UnregisterHotKey.restype = ctypes.c_bool
    _VkKeyScanW = _user32.VkKeyScanW
    _VkKeyScanW.argtypes = [ctypes.c_wchar]
    _VkKeyScanW.restype = ctypes.c_short

    WM_HOTKEY = 0x0312
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    MOD_NOREPEAT = 0x4000  # 按住不重复触发
    _HOTKEY_ID = 0x0001  # 线程关联热键，id 用 0x0000~0xBFFF

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", ctypes.c_void_p),
            ("lParam", ctypes.c_void_p),
            ("time", ctypes.c_uint),
            ("pt", _POINT),
        ]


class _HotkeyNativeFilter(QAbstractNativeEventFilter):
    """拦截主线程消息循环中的 WM_HOTKEY，桥接到 Qt 信号

    RegisterHotKey 把 WM_HOTKEY 投递到调用线程（Qt 主线程）的消息队列，
    通过 QAbstractNativeEventFilter 即可在 Qt 处理前捕获，无需子类化窗口。
    """

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, eventType, message):
        if platform.system() == "Windows" and eventType == "windows_generic_MSG":
            try:
                msg = ctypes.cast(int(message), ctypes.POINTER(_MSG))[0]
                if msg.message == WM_HOTKEY and int(msg.wParam) == _HOTKEY_ID:
                    self._callback()
                    return (True, 0)  # 已处理，停止继续分发
            except Exception:
                pass
        return (False, 0)


# 托盘显隐切换防抖间隔（毫秒）：全局热键 + QShortcut 兜底同时触发时去重
_TRAY_TOGGLE_DEBOUNCE_MS = 500


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
        """检查窗口是否仍然有效（C++ 对象是否存活）

        通过调用 isVisible() 验证底层 QWidget 是否已被销毁，
        与 _prune_dead_windows 使用相同模式。
        """
        try:
            if w is None:
                return False
            # 已销毁的 C++ 对象调用 isVisible 会抛 RuntimeError
            w.isVisible()
            return True
        except Exception:
            return False

    def __init__(self, parent=None):
        super().__init__(parent)
        if TrayManager._instance is not None:
            raise RuntimeError("TrayManager 是单例，请使用 get_instance() 获取")
        TrayManager._instance = self

        self._windows: list = []  # 已注册的 ToolPopupDialog 列表
        self._pending_notification: dict = {}  # 当前待处理的托盘通知 {notification_id: window}
        self._last_notification_tab_index: int = -1  # 最近通知关联的 tab 索引（Tab 模式）

        # 创建托盘图标
        self._tray_icon = QSystemTrayIcon(self)
        self._tray_icon.setIcon(QIcon(":/icons/drifox.ico"))
        self._tray_icon.setToolTip("Drifox")

        # 创建右键菜单（动态重建，连接 aboutToShow 信号）
        self._tray_menu = QMenu()
        self._tray_menu.aboutToShow.connect(self._rebuild_context_menu)
        self._tray_menu.addAction("加载中…")  # 占位，显示前会重建
        self._tray_icon.setContextMenu(self._tray_menu)

        # 监听托盘图标点击（Windows: 单击恢复窗口）
        if platform.system() == "Windows":
            self._tray_icon.activated.connect(self._on_tray_activated)

        # 监听托盘消息点击（点击通知时显示对应窗口）
        self._tray_icon.messageClicked.connect(self._on_message_clicked)

        self._tray_icon.show()

        # ========== 全局热键 ==========
        self._hotkey_bridge = _HotkeyBridge()
        self._hotkey_bridge.toggle_all_windows.connect(self._toggle_all_windows)
        self._hotkey_handle = None
        self._hotkey_id = None
        self._hotkey_mode = None  # "win"=原生RegisterHotKey / "kbd"=keyboard兜底钩子
        self._hotkey_filter = None
        self._hotkey_failed_once = False
        self._hotkey_failed_hotkey = None
        self._registered_hotkey = None
        self._last_toggle_time = 0.0  # 防重复触发时间戳
        # Windows: 安装 WM_HOTKEY 原生事件过滤器（需在 QApplication 存在后）
        if platform.system() == "Windows":
            self._install_hotkey_filter()
        self._setup_global_hotkey()

        # 定时重建全局热键（应对 sleep/resume 等导致的钩子丢失）
        self._hotkey_health_timer = QTimer(self)
        self._hotkey_health_timer.timeout.connect(self._health_check_hotkey)
        self._hotkey_health_timer.start(300000)  # 5分钟

        # ========== 多窗口选中管理 ==========
        self._selected_windows: list = []  # 当前选中的 ToolPopupDialog 列表

        # ========== 排布模式 ==========
        # 0=网格(grid) / 1=竖列横排(horizontal) / 2=折叠(stack)
        # Ctrl+Shift+G 依次循环
        self._arrange_mode: int = 0
        self._ARRANGE_MODES = ("网格", "竖列", "折叠")

        # ========== Tab 管理器支持 ==========
        self._tab_manager_window = None  # Tab 模式开启时指向 TabManagerWindow

        logger.info("TrayManager 初始化完成")

    # ========== 托盘右键菜单动态重建 ==========

    def _rebuild_context_menu(self):
        """动态重建托盘右键菜单，为每个窗口创建独立的菜单项"""
        self._tray_menu.clear()

        # ── Tab 模式：简化菜单 ──
        if self._tab_manager_window is not None:
            tm_action = QAction("📑 Tab 管理器", self._tray_menu)
            tm_action.triggered.connect(
                lambda: (
                    (
                        self._tab_manager_window.show(),
                        self._tab_manager_window.activateWindow(),
                        self._tab_manager_window.raise_(),
                    )
                    if self._tab_manager_window
                    else None
                )
            )
            self._tray_menu.addAction(tm_action)
            self._tray_menu.addSeparator()
            quit_action = QAction("退出", self._tray_menu)
            quit_action.triggered.connect(self._quit_application)
            self._tray_menu.addAction(quit_action)
            return

        # ── 独立窗口模式分支已下线（M2a-A4）：Tab 模式固定启用后
        # `_tab_manager_window is not None` 恒为真，else 分支永不执行 ──
        # （原逻辑：过滤有效窗口、显示/隐藏全部、新建窗口菜单项）

    def _get_window_menu_title(self, window, index: int) -> str:
        """获取窗口在托盘菜单中显示的标题"""
        try:
            # 优先用 windowTitle（已由 _sync_dialog_title 同步为会话标题）
            title = window.windowTitle()
            if title and title != "飘狐":
                return f"  {title}"
        except RuntimeError:
            pass
        # 兜底：用窗口注册序号
        return f"  窗口 {index}"

    # ========== 多窗口选中管理 ==========

    def _select_window(self, window) -> None:
        """添加窗口到选中列表"""
        if window not in self._selected_windows:
            self._selected_windows.append(window)
            self._update_selection_visuals()

    def is_window_selected(self, window) -> bool:
        """检查窗口是否被选中"""
        return window in self._selected_windows

    def deselect_all(self) -> None:
        """清除所有窗口的选中状态"""
        if not self._selected_windows:
            return
        self._selected_windows.clear()
        self._update_selection_visuals()

    def _on_window_shift_clicked(self, window) -> None:
        """窗口 Shift+点击回调 - 切换选中状态"""
        if window in self._selected_windows:
            self._selected_windows.remove(window)
        else:
            self._selected_windows.append(window)
        self._update_selection_visuals()

    def _update_selection_visuals(self) -> None:
        """刷新所有窗口的选中标记"""
        for w in self._windows:
            try:
                selected = w in self._selected_windows
                if hasattr(w, "set_selection_indicator"):
                    w.set_selection_indicator(selected)
            except RuntimeError:
                pass

    def _handle_batch_move(self, source_window, delta) -> None:
        """批量移动：所有选中窗口同 delta 偏移

        Args:
            source_window: 发起移动的窗口（已移动完毕，跳过）
            delta: QPoint 偏移量
        """
        for w in self._selected_windows:
            if w is source_window:
                continue
            try:
                w.move(w.x() + delta.x(), w.y() + delta.y())
            except RuntimeError:
                pass

    _SNAP_THRESHOLD = 15  # 吸附阈值（像素）
    _TITLE_BAR_HEIGHT = 28  # 窗口标题栏高度（与 ToolWindowTitleBar.setFixedHeight(28) 一致）

    def _snap_position(self, moving_rect, exclude_window=None) -> tuple:
        """计算最近的对齐吸附位置

        Args:
            moving_rect: QRect 当前移动窗口的几何区域
            exclude_window: 排除的窗口（自身）

        Returns:
            (snapped_x, snapped_y, is_snapped_x, is_snapped_y)
            如果某方向未吸附，返回原值
        """
        x0, y0 = moving_rect.x(), moving_rect.y()
        w, h = moving_rect.width(), moving_rect.height()
        best_x, best_y = x0, y0
        snapped_x, snapped_y = False, False
        # 找最近吸附距离
        min_dist_x = self._SNAP_THRESHOLD + 1
        min_dist_y = self._SNAP_THRESHOLD + 1

        # 获取当前屏幕号（跳过不同屏幕的窗口）
        try:
            from PyQt5.QtWidgets import QDesktopWidget

            desktop = QDesktopWidget()
            current_screen_idx = desktop.screenNumber(exclude_window) if exclude_window else -1
        except Exception:
            current_screen_idx = -1

        for win in self._windows:
            if win is exclude_window:
                continue
            # 只有当选中的窗口在批量拖拽时，才跳过其他选中窗口
            # 非选中窗口拖拽时，选中窗口是有效吸附目标
            if exclude_window in self._selected_windows and win in self._selected_windows:
                continue
            try:
                # 跳过不可见或最小化的窗口
                if win.isHidden() or win.isMinimized():
                    continue
                # 跳过不同屏幕的窗口
                if current_screen_idx >= 0:
                    try:
                        win_screen = desktop.screenNumber(win)
                        # -1 表示窗口尚未映射到屏幕（新窗口初始化中），不跳过
                        if win_screen >= 0 and win_screen != current_screen_idx:
                            continue
                    except Exception:
                        pass

                r = win.geometry()
                candidates_x = []

                # 水平候选：左边缘、右边缘对齐
                candidates_x.append((r.x(), abs(x0 - r.x())))  # 移动左 → 目标左
                candidates_x.append((r.x() - w, abs(x0 + w - r.x())))  # 移动右 → 目标左
                candidates_x.append((r.x() + r.width(), abs(x0 - r.x() - r.width())))  # 移动左 → 目标右
                candidates_x.append((r.x() + r.width() - w, abs(x0 + w - r.x() - r.width())))  # 移动右 → 目标右

                # 垂直候选：上边缘、下边缘对齐
                candidates_y = []
                candidates_y.append((r.y(), abs(y0 - r.y())))
                candidates_y.append((r.y() - h, abs(y0 + h - r.y())))
                candidates_y.append((r.y() + r.height(), abs(y0 - r.y() - r.height())))
                candidates_y.append((r.y() + r.height() - h, abs(y0 + h - r.y() - r.height())))

                # 取最近的水平吸附
                for cand_x, dist in candidates_x:
                    if dist < min_dist_x:
                        min_dist_x = dist
                        best_x = cand_x
                        snapped_x = True

                # 取最近的垂直吸附
                for cand_y, dist in candidates_y:
                    if dist < min_dist_y:
                        min_dist_y = dist
                        best_y = cand_y
                        snapped_y = True

            except RuntimeError:
                pass

        # 阈值检查：如果最近距离超出阈值，不吸附
        if min_dist_x > self._SNAP_THRESHOLD:
            best_x = x0
            snapped_x = False
        if min_dist_y > self._SNAP_THRESHOLD:
            best_y = y0
            snapped_y = False

        return best_x, best_y, snapped_x, snapped_y

    def arrange_selected_windows_grid(self) -> None:
        """智能网格模式：右下锚定，自适应列数

        - 列数根据屏幕宽高比 + 窗口数计算(让网格尽量贴近屏幕比例)
        - 窗口目标尺寸独立从屏幕和列数推算,不读窗口当前尺寸,避免模式间互相影响
        - 第 1 个窗口始终在右下角,作为视觉重心
        """
        self._prune_dead_windows()  # 防御性:剔除已销毁引用,避免按旧数量排布
        if len(self._selected_windows) < 1:
            return

        available = self._get_screen_geometry()
        if available is None:
            return

        n = len(self._selected_windows)
        margin = 16  # 统一边距

        # === 智能列数 ===
        # 目标:让网格整体宽高比接近屏幕宽高比
        # cols/rows ≈ aspect → cols ≈ sqrt(n * aspect)
        avail_w = max(1, available.width())
        avail_h = max(1, available.height())
        aspect = avail_w / avail_h
        cols = max(1, round(math.sqrt(n * aspect)))
        if n >= 2:
            cols = max(2, min(n, cols))
        rows = math.ceil(n / cols)

        # === 单格可用区域 ===
        cell_w = (avail_w - margin * (cols + 1)) / cols
        cell_h = (avail_h - margin * (rows + 1)) / rows

        # === 独立计算目标尺寸(不读窗口当前尺寸) ===
        # 单格宽高比决定窗口宽高比
        cell_aspect = cell_w / max(1.0, cell_h)
        if cell_aspect >= 1.0:
            # 偏宽 → 窗口以宽度为基准
            win_w = cell_w * 0.95
            win_h = win_w / cell_aspect
        else:
            # 偏高 → 窗口以高度为基准
            win_h = cell_h * 0.95
            win_w = win_h * cell_aspect

        # 最小尺寸保护
        win_w = max(260, min(win_w, cell_w))
        win_h = max(200, min(win_h, cell_h))

        for i, w in enumerate(self._selected_windows):
            try:
                col = i % cols  # 0 = 最右列
                row = i // cols  # 0 = 最底行
                # 从右下角开始填充(右下为视觉重心)
                new_x = available.right() - margin - win_w - col * (win_w + margin)
                new_y = available.bottom() - margin - win_h - row * (win_h + margin)
                w.setGeometry(int(new_x), int(new_y), int(win_w), int(win_h))
            except RuntimeError:
                pass

    def arrange_selected_windows_horizontal(self) -> None:
        """竖列横排模式：右向左排列,右下为视觉重心

        N 个窗口从右向左并排排列(第 1 个在最右),每个窗口高度 ≈ 屏幕高,
        宽度按窗口数等分。窗口尺寸独立从屏幕推算。
        """
        self._prune_dead_windows()  # 防御性:剔除已销毁引用,避免按旧数量排布
        if len(self._selected_windows) < 1:
            return

        available = self._get_screen_geometry()
        if available is None:
            return

        n = len(self._selected_windows)
        margin = 12

        # === 独立计算目标尺寸 ===
        win_h = available.height() - margin * 2
        win_w = (available.width() - margin * 2) / max(1, n)
        # 最小宽度保护
        win_w = max(180, win_w)

        for i, w in enumerate(self._selected_windows):
            try:
                # 从右向左:第 1 个在最右(右下锚定)
                new_x = available.right() - margin - win_w - i * win_w
                new_y = available.top() + margin
                # 边界保护:不能超出左边界,允许溢出但不调整(用户能看到每个窗口的左边缘)
                if new_x < available.left() + margin:
                    new_x = available.left() + margin
                w.setGeometry(int(new_x), int(new_y), int(win_w), int(win_h))
            except RuntimeError:
                pass

    def arrange_selected_windows_stack(self) -> None:
        """折叠模式:自适应 cascade,右下锚定,层次清晰

        第 1 个窗口在屏幕右下角,后续窗口依次向左上偏移,形成经典 Windows
        cascade 视觉效果。两个关键优化:

        1. 窗口尺寸自适应:目标尺寸 = min(屏幕宽30%, 屏幕高55%),并保证
           所有窗口放下后仍不超出屏幕左/上边界
        2. 偏移量自适应:默认 dx=40, dy=32(略大于标题栏高度,层次更明显);
           窗口数多时自动缩小偏移,保证每个窗口的标题栏都能露出
        """
        self._prune_dead_windows()  # 防御性:剔除已销毁引用,避免按旧数量排布
        if len(self._selected_windows) < 1:
            return

        available = self._get_screen_geometry()
        if available is None:
            return

        n = len(self._selected_windows)
        margin = 20  # 略大的边距,让 cascade 更舒展
        avail_w = available.width()
        avail_h = available.height()

        # === 目标窗口尺寸(基于屏幕比例,不读窗口当前尺寸) ===
        # 目标:宽 = 屏幕30%, 高 = 屏幕55%(保持纵向略高的视觉比例)
        target_w = int(avail_w * 0.30)
        target_h = int(avail_h * 0.55)
        target_side = min(target_w, target_h)

        # === 自适应偏移量 ===
        # 目标偏移量略大于标题栏高度,层次更清晰
        target_dx = 40
        target_dy = 32
        # 最小偏移量(保证视觉错开)
        min_dx = 20
        min_dy = 15

        if n > 1:
            # 约束 1:总 x 偏移不能太大,否则最后一个窗口飞出屏幕
            max_dx_for_screen = max(min_dx, (avail_w - 2 * margin - target_side) // (n - 1))
            # 约束 2:总 y 偏移不能太大,否则最后一个窗口飞出屏幕
            max_dy_for_screen = max(min_dy, (avail_h - 2 * margin - target_side) // (n - 1))
            dx = min(target_dx, max_dx_for_screen)
            dy = min(target_dy, max_dy_for_screen)
        else:
            dx = target_dx
            dy = target_dy

        # === 窗口尺寸(根据最终 dx, dy 微调) ===
        # 如果偏移缩小了,窗口可以适当放大(因为有更多空间)
        win_w = target_side
        win_h = target_side
        # 适配屏幕
        if win_w > avail_w - 2 * margin:
            win_w = avail_w - 2 * margin
        if win_h > avail_h - 2 * margin:
            win_h = avail_h - 2 * margin
        # 保持接近正方形(以宽度为基准)
        if win_w < win_h:
            win_h = win_w
        # 最小保护
        win_w = max(180, int(win_w))
        win_h = max(140, int(win_h))

        # === Cascade 偏移 ===
        for i, w in enumerate(self._selected_windows):
            try:
                step = i
                new_x = available.right() - margin - win_w - step * dx
                new_y = available.bottom() - margin - win_h - step * dy
                # 边界保护(即使 dx/dy 已自适应,仍保留兜底)
                if new_x < available.left() + margin:
                    new_x = available.left() + margin
                if new_y < available.top() + margin:
                    new_y = available.top() + margin
                w.setGeometry(int(new_x), int(new_y), int(win_w), int(win_h))
            except RuntimeError:
                pass

    def _get_screen_geometry(self):
        """获取第一个选中窗口所在屏幕的可用区域,失败返回 None

        统一封装,供三种排布模式共享。
        """
        if not self._selected_windows:
            return None
        ref = self._selected_windows[0]
        try:
            screen = ref.screen()
            if not screen:
                return None
            return screen.availableGeometry()
        except RuntimeError:
            return None

    def cycle_arrange_mode(self) -> str:
        """循环切换排布模式（Ctrl+Shift+G）

        依次执行：网格 → 竖列 → 折叠 → 网格 …
        返回本次切换到的模式名称，便于调用方做提示。
        """
        # 三个具体排布方法入口已自带 _prune_dead_windows,此处直接分发即可
        if not self._selected_windows:
            return ""

        mode = self._arrange_mode
        if mode == 0:
            self.arrange_selected_windows_grid()
        elif mode == 1:
            self.arrange_selected_windows_horizontal()
        else:
            self.arrange_selected_windows_stack()

        # 切换到下一个模式（循环）
        self._arrange_mode = (self._arrange_mode + 1) % len(self._ARRANGE_MODES)
        return self._ARRANGE_MODES[mode]

    def _prune_dead_windows(self) -> None:
        """清理已销毁的窗口引用(C++ 对象已被 deleteLater 销毁)

        通过 try/except isVisible() 检查每个引用是否还有效,无效的从列表移除。
        作为 unregister_window 的兜底:即使某些路径未走 unregister_window,
        排布时也能自动剔除无效引用,避免按旧窗口数量排布。
        """
        alive = []
        for w in self._selected_windows:
            try:
                _ = w.isVisible()  # 已销毁对象会抛 RuntimeError
                alive.append(w)
            except Exception:
                continue
        if len(alive) != len(self._selected_windows):
            self._selected_windows = alive
            self._update_selection_visuals()

    def register_window(self, window) -> None:
        """注册一个窗口到托盘管理器"""
        if window not in self._windows:
            self._windows.append(window)
            logger.debug(f"窗口已注册到 TrayManager: {window.windowTitle()}")

    def unregister_window(self, window) -> None:
        """窗口销毁时注销"""
        if window in self._windows:
            self._windows.remove(window)
        # 同步从选中列表移除,避免关闭窗口后残留引用导致排布按旧窗口数量计算
        if window in self._selected_windows:
            self._selected_windows.remove(window)
            self._update_selection_visuals()
        logger.debug(f"窗口已从 TrayManager 注销: {window.windowTitle()}")

    def notify(self, title: str, message: str, window: QObject = None, tab_index: int = -1) -> None:
        """发送 Windows 通知

        Args:
            title: 通知标题
            message: 通知内容
            window: 触发通知的窗口对象，点击通知时会显示该窗口
            tab_index: Tab 模式下，关联的标签页索引（-1 表示不跳转）
        """
        if self._tray_icon.isVisible():
            # 生成唯一 ID 关联通知和窗口
            notification_id = str(uuid.uuid4())
            self._pending_notification[notification_id] = window or self._get_first_valid_window()
            # 保存到实例属性，供 messageClicked 信号处理器使用
            self._last_notification_window = self._pending_notification.get(notification_id)
            self._last_notification_tab_index = tab_index
            self._tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon(1), 4000)
            # 4秒后清理（与 showMessage 的显示时长一致）
            QTimer.singleShot(4500, lambda: self._pending_notification.pop(notification_id, None))

    def _get_first_valid_window(self):
        # D3: dead code — _windows 恒空（Tab 模式），保留作回退兜底
        """获取第一个有效的窗口"""
        for w in self._windows:
            if self._is_window_valid(w):
                return w
        return None

    def _show_window(self, window) -> None:
        # D3: dead code — _windows 恒空（Tab 模式），保留作回退兜底
        """显示指定的窗口（如果已隐藏则显示，如果最小化则还原）

        支持传入嵌入式Widget：自动获取其所属的顶层窗口再操作。
        """
        if window is None:
            logger.warning("[_show_window] 窗口为空")
            return

        # 如果传入的是嵌入式Widget（如OpenAIChatToolWindow），取其顶层窗口
        top_window = window.window() if hasattr(window, "window") and callable(window.window) else window
        if top_window is None:
            top_window = window

        try:
            # 普通最小化窗口仍然是 visible，必须先解除最小化；否则后续
            # activateWindow/raise_ 只会激活任务栏项，不会把窗口恢复到前台。
            if top_window.isMinimized():
                logger.info("[_show_window] 从最小化恢复窗口")
                top_window.showNormal()
            if top_window.isHidden():
                logger.info("[_show_window] 显示窗口")
                top_window.show()
            top_window.raise_()
            top_window.activateWindow()
        except RuntimeError as e:
            logger.error(f"[_show_window] 窗口操作失败: {e}")

    def _on_message_clicked(self) -> None:
        """处理托盘通知被点击的事件"""
        logger.debug("[_on_message_clicked] 通知被点击")
        # 显示最近一次通知关联的窗口
        window = getattr(self, "_last_notification_window", None)
        if window:
            self._show_window(window)
        else:
            # 没有记录时，显示任意一个窗口
            window = self._get_first_valid_window()
            if window:
                self._show_window(window)

        # Tab 模式：跳转到通知对应的标签页
        if self._tab_manager_window is not None and self._last_notification_tab_index >= 0:
            try:
                logger.debug(f"[_on_message_clicked] 跳转到 tab {self._last_notification_tab_index}")
                self._tab_manager_window._tab_panel.set_active_index(self._last_notification_tab_index)
            except Exception as e:
                logger.warning(f"[_on_message_clicked] 跳转 tab 失败: {e}")

    def _on_tray_activated(self, reason):
        """Windows 托盘图标点击处理"""
        # QSystemTrayIcon.Trigger = 单击, DoubleClick = 双击
        if reason == QSystemTrayIcon.Trigger:
            self._show_or_create_window()

    def _show_or_create_window(self) -> None:
        """显示所有窗口或创建新窗口"""
        logger.info(f"[_show_or_create] windows count: {len(self._windows)}")

        # Tab 模式：即使没有独立窗口也要恢复 TabManagerWindow
        if self._tab_manager_window is not None:
            try:
                # ★ 修复：先检查最小化状态。Qt5 在 Windows 上对最小化窗口
                # isVisible() == True，若仅走可见分支→activateWindow/raise_ 不取消
                # 最小化，导致"点任务栏图标无法从最小化恢复"的 bug。
                if self._tab_manager_window.isMinimized():
                    self._tab_manager_window.showNormal()
                elif not self._tab_manager_window.isVisible():
                    self._tab_manager_window.show()
                self._tab_manager_window.activateWindow()
                self._tab_manager_window.raise_()
                return
            except RuntimeError:
                # C++ 对象已销毁，重建引用
                self._tab_manager_window = None

        # 防御性：从单例恢复 _tab_manager_window 引用
        if self._tab_manager_window is None:
            try:
                from app.widgets.tab_manager_window import TabManagerWindow as _TMW

                if _TMW._instance is not None:
                    self._tab_manager_window = _TMW._instance
                    self._tab_manager_window.show()
                    self._tab_manager_window.activateWindow()
                    self._tab_manager_window.raise_()
                    return
            except Exception:
                self._tab_manager_window = None

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
            # 普通最小化窗口仍然可见，必须先解除最小化，再激活到前台。
            if w.isMinimized():
                w.showNormal()
            elif w.isHidden():
                w.show()
            w.raise_()
            w.activateWindow()
        except Exception as e:
            logger.error(f"[_show_or_create] 显示窗口失败: {e}")

    # ========== 全局热键支持（一键隐藏/显示所有窗口） ==========

    @staticmethod
    def _read_hotkey_from_command() -> str:
        """从 toggle-window 命令读取全局热键

        优先级：
        1. CommandManager 中已注册的 shortcut（支持用户插件覆盖）
        2. 系统插件 toggle-window.md 文件的 shortcut 字段（兜底）
        """
        try:
            # 优先从 CommandManager 读取（支持用户插件覆盖系统命令）
            from app.core.command_manager import CommandManager

            cmd_mgr = CommandManager.get_instance()
            toggle_cmd = cmd_mgr.get_command("toggle-window")
            if toggle_cmd and toggle_cmd.shortcut:
                return toggle_cmd.shortcut
        except Exception:
            pass

        # 兜底：直接从系统文件读取
        try:
            cmd_path = Path(__file__).parents[1] / "plugins" / "system" / "commands" / "toggle-window.md"
            if not cmd_path.exists():
                return ""
            content = cmd_path.read_text(encoding="utf-8")
            if not content.startswith("---"):
                return ""
            lines = content.splitlines()
            close_idx = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    close_idx = i
                    break
            if close_idx is None:
                return ""
            frontmatter = "\n".join(lines[1:close_idx])
            import yaml

            meta = yaml.safe_load(frontmatter)
            if not meta:
                return ""
            return (meta.get("shortcut", "") or "").strip()
        except Exception:
            return ""

    def _setup_global_hotkey(self, hotkey_str: str = None):
        """注册全局热键

        Windows: 使用原生 Win32 RegisterHotKey（官方应用级热键，睡眠/锁屏/UAC
          均不会静默失效，是根治 toggle-window 热键“用久了失效”的方案）。
        其它平台: 沿用 keyboard 库的 LL 钩子实现（保持原有行为）。
        快捷键来源：从 commands/toggle-window.md 的 shortcut 字段读取（支持热修改）。

        Args:
            hotkey_str: 热键字符串，如 'alt+z'。None 则从命令定义读取。
        """
        if hotkey_str is None:
            hotkey_str = self._read_hotkey_from_command()
        if not hotkey_str:
            hotkey_str = "alt+z"

        hotkey_str = hotkey_str.lower()

        if platform.system() == "Windows":
            self._win_register_hotkey(hotkey_str)
        else:
            self._kbd_register_hotkey(hotkey_str)

    def _release_global_hotkey(self):
        """释放已注册的全局热键（兼容 win / kbd 两种模式）"""
        if platform.system() == "Windows":
            self._win_unregister_hotkey()
        self._kbd_release()
        self._hotkey_mode = None
        self._registered_hotkey = None

    # ---------- Windows: 原生 RegisterHotKey 实现 ----------

    def _win_register_hotkey(self, hotkey_str: str):
        """用 Win32 RegisterHotKey 注册/重注册全局热键

        优先尝试 OS 原生注册（最稳健：睡眠/锁屏/UAC 均不会静默失效）。
        若该组合已被其它程序占用（RegisterHotKey 返回 0 + 错误码 1409），
        则【自动回退】到 keyboard 库的 LL 钩子，与占用方共存（行为同旧版），
        保证用户的热键始终可用，而非彻底失效。
        """
        try:
            modifiers, vk = self._parse_hotkey(hotkey_str)
        except ValueError as exc:
            logger.warning(f"[TrayManager] 热键字符串解析失败({hotkey_str}): {exc}")
            return

        self._win_unregister_hotkey()
        ok = _RegisterHotKey(None, _HOTKEY_ID, modifiers, vk)
        if ok:
            self._hotkey_id = _HOTKEY_ID
            self._registered_hotkey = hotkey_str
            self._hotkey_mode = "win"
            # 升级到原生热键成功 → 释放可能残留的 keyboard 兜底钩子
            self._kbd_release()
            self._hotkey_failed_once = False
            logger.info(f"[TrayManager] 原生全局热键已注册: {hotkey_str}")
            return

        # —— 注册失败：组合键被占用，回退到 keyboard LL 钩子 ——
        err = ctypes.GetLastError()
        logger.warning(
            f"[TrayManager] RegisterHotKey 注册失败({hotkey_str}): 错误码 {err}"
            f"（组合键已被其它程序占用，自动回退到 keyboard 钩子兼容模式）"
        )
        # 仅首次失败 / 更换组合时弹一次托盘提示，引导用户换键
        if not getattr(self, "_hotkey_failed_once", False) or self._hotkey_failed_hotkey != hotkey_str:
            self._hotkey_failed_once = True
            self._hotkey_failed_hotkey = hotkey_str
            try:
                self.notify(
                    "全局快捷键被占用",
                    f"「{hotkey_str}」已被其它程序占用（常见于 NVIDIA GeForce Experience 的 Alt+Z）。"
                    f"已自动回退到兼容模式，功能正常；如需彻底规避，可在 toggle-window 的 shortcut 换为 ctrl+alt+z。",
                )
            except Exception:
                pass
        self._kbd_register_hotkey(hotkey_str)

    def _win_unregister_hotkey(self):
        """注销已注册的原生热键（幂等安全）"""
        if getattr(self, "_hotkey_id", None) is not None:
            try:
                _UnregisterHotKey(None, self._hotkey_id)
            except Exception:
                pass
            self._hotkey_id = None

    # ---------- 兜底：keyboard 库 LL 钩子（组合键被占用/非 Windows 时使用） ----------

    def _kbd_register_hotkey(self, hotkey_str: str):
        """用 keyboard 库 LL 钩子注册全局热键（RegisterHotKey 的兼容兜底）

        与占用方（如 NVIDIA）共存，行为同旧版。仅在 RegisterHotKey 抢不到
        组合键时启用。自带幂等：相同组合已注册则直接跳过，避免重复回调导致
        一次按键触发多次切换。
        """
        # 幂等：同模式 + 同组合 + 句柄有效 → 跳过
        if (
            getattr(self, "_hotkey_mode", None) == "kbd"
            and getattr(self, "_registered_hotkey", None) == hotkey_str
            and getattr(self, "_hotkey_handle", None) is not None
        ):
            return
        self._kbd_release()
        try:
            handle = keyboard.add_hotkey(
                hotkey_str,
                self._hotkey_bridge.toggle_all_windows.emit,
                suppress=True,
            )
            self._hotkey_handle = handle
            self._registered_hotkey = hotkey_str
            self._hotkey_mode = "kbd"
            logger.info(f"[TrayManager] 兜底（keyboard 钩子）热键已注册: {hotkey_str}")
        except Exception as exc:
            logger.warning(f"[TrayManager] keyboard 兜底热键注册失败({hotkey_str}): {exc}")

    def _kbd_release(self):
        """释放 keyboard 库兜底热键（幂等安全）"""
        if getattr(self, "_hotkey_handle", None) is not None:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
            self._hotkey_handle = None
        if getattr(self, "_hotkey_mode", None) == "kbd":
            self._hotkey_mode = None

    # ---------- 热键字符串解析 ----------

    def _parse_hotkey(self, hotkey_str: str):
        """将 'alt+z' 之类字符串解析为 (modifiers, vk)

        modifiers: MOD_ALT/MOD_CONTROL/MOD_SHIFT/MOD_WIN 的组合（含 MOD_NOREPEAT）
        vk: 主键的虚拟键码
        """
        parts = [p.strip() for p in hotkey_str.lower().split("+") if p.strip()]
        modifiers = 0
        vk = None
        for p in parts:
            if p in ("alt", "menu"):
                modifiers |= MOD_ALT
            elif p in ("ctrl", "control"):
                modifiers |= MOD_CONTROL
            elif p == "shift":
                modifiers |= MOD_SHIFT
            elif p in ("win", "super", "meta"):
                modifiers |= MOD_WIN
            else:
                vk = self._key_to_vk(p)
        if vk is None:
            raise ValueError(f"缺少主键: {hotkey_str}")
        modifiers |= MOD_NOREPEAT
        return modifiers, vk

    def _key_to_vk(self, key: str):
        """将单个按键名转为虚拟键码（vk）"""
        named = {
            "backspace": 0x08,
            "tab": 0x09,
            "enter": 0x0D,
            "return": 0x0D,
            "shift": 0x10,
            "ctrl": 0x11,
            "control": 0x11,
            "alt": 0x12,
            "menu": 0x12,
            "pause": 0x13,
            "capslock": 0x14,
            "esc": 0x1B,
            "escape": 0x1B,
            "space": 0x20,
            "pgup": 0x21,
            "pgdn": 0x22,
            "end": 0x23,
            "home": 0x24,
            "left": 0x25,
            "up": 0x26,
            "right": 0x27,
            "down": 0x28,
            "insert": 0x2D,
            "delete": 0x2E,
            "del": 0x2E,
            "0": 0x30,
            "1": 0x31,
            "2": 0x32,
            "3": 0x33,
            "4": 0x34,
            "5": 0x35,
            "6": 0x36,
            "7": 0x37,
            "8": 0x38,
            "9": 0x39,
            "a": 0x41,
            "b": 0x42,
            "c": 0x43,
            "d": 0x44,
            "e": 0x45,
            "f": 0x46,
            "g": 0x47,
            "h": 0x48,
            "i": 0x49,
            "j": 0x4A,
            "k": 0x4B,
            "l": 0x4C,
            "m": 0x4D,
            "n": 0x4E,
            "o": 0x4F,
            "p": 0x50,
            "q": 0x51,
            "r": 0x52,
            "s": 0x53,
            "t": 0x54,
            "u": 0x55,
            "v": 0x56,
            "w": 0x57,
            "x": 0x58,
            "y": 0x59,
            "z": 0x5A,
            "numpad0": 0x60,
            "numpad1": 0x61,
            "numpad2": 0x62,
            "numpad3": 0x63,
            "numpad4": 0x64,
            "numpad5": 0x65,
            "numpad6": 0x66,
            "numpad7": 0x67,
            "numpad8": 0x68,
            "numpad9": 0x69,
            "f1": 0x70,
            "f2": 0x71,
            "f3": 0x72,
            "f4": 0x73,
            "f5": 0x74,
            "f6": 0x75,
            "f7": 0x76,
            "f8": 0x77,
            "f9": 0x78,
            "f10": 0x79,
            "f11": 0x7A,
            "f12": 0x7B,
            "numlock": 0x90,
            "scrolllock": 0x91,
            "semicolon": 0xBA,
            "equal": 0xBB,
            "comma": 0xBC,
            "minus": 0xBD,
            "period": 0xBE,
            "slash": 0xBF,
            "backquote": 0xC0,
            "lbracket": 0xDB,
            "backslash": 0xDC,
            "rbracket": 0xDD,
            "quote": 0xDE,
        }
        key = key.lower()
        if key in named:
            return named[key]
        if len(key) == 1:
            return _VkKeyScanW(key) & 0xFF
        if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
            return 0x70 + int(key[1:]) - 1
        raise ValueError(f"未知按键: {key}")

    # ---------- 原生事件过滤器安装 ----------

    def _install_hotkey_filter(self):
        """安装 WM_HOTKEY 原生事件过滤器（仅 Windows）

        必须在 QApplication 实例存在后调用。若此时尚未获得实例，则延后到
        下一个事件循环再试（保证 RegisterHotKey 所在的线程有消息循环）。
        """
        if self._hotkey_filter is not None:
            return
        try:
            app = QApplication.instance()
            if app is None:
                # 尚未就绪，下一轮事件循环再安装
                QTimer.singleShot(0, self._install_hotkey_filter)
                return
            self._hotkey_filter = _HotkeyNativeFilter(self._hotkey_bridge.toggle_all_windows.emit)
            app.installNativeEventFilter(self._hotkey_filter)
            logger.info("[TrayManager] WM_HOTKEY 原生过滤器已安装")
        except Exception as exc:
            logger.warning(f"[TrayManager] 安装 WM_HOTKEY 过滤器失败: {exc}")

    def _toggle_all_windows(self):
        """切换所有窗口的隐藏/显示状态

        任意窗口可见 → 全部隐藏
        全部已隐藏 → 全部显示并激活
        """
        # 防重复触发（全局热键 + QShortcut 兜底同时触发时）
        now = time.perf_counter()
        if now - self._last_toggle_time < _TRAY_TOGGLE_DEBOUNCE_MS / 1000.0:
            return
        self._last_toggle_time = now

        # ── 恢复可能丢失的 Tab 管理器引用（仅 Tab 模式下）──
        # _enable_mode 已确保引用注册，但以防 C++ 对象重建或异常导致引用丢失，
        # 此处安全兜底：仅在无独立窗口（Tab 模式特征）时恢复引用，不自动显示。
        if self._tab_manager_window is None and not self._windows:
            try:
                from app.widgets.tab_manager_window import TabManagerWindow as _TMW

                if _TMW._instance is not None:
                    self._tab_manager_window = _TMW._instance
            except Exception:
                self._tab_manager_window = None

        # ── Tab 模式分支 ──
        if self._tab_manager_window is not None:
            try:
                if self._tab_manager_window.isVisible():
                    self._tab_manager_window.hide()
                else:
                    self._tab_manager_window.show()
                    self._tab_manager_window.activateWindow()
                    self._tab_manager_window.raise_()
                    # 从最小化状态恢复
                    if self._tab_manager_window.isMinimized():
                        self._tab_manager_window.showNormal()
            except RuntimeError:
                self._tab_manager_window = None
                # C++ 对象已销毁，降级到独立窗口逻辑
            else:
                return  # Tab 模式正常完成，跳过独立窗口逻辑

        try:
            # 过滤有效窗口，同时剔除已销毁的 C++ 对象
            valid_windows = []
            for w in self._windows:
                try:
                    if w is None:
                        continue
                    # 验证 C++ 对象存活；已销毁对象会抛 RuntimeError
                    w.isVisible()
                    valid_windows.append(w)
                except Exception:
                    continue

            if not valid_windows:
                return

            # 检查是否有任何窗口可见
            any_visible = any(w.isVisible() for w in valid_windows)

            if any_visible:
                # 全部隐藏
                for w in valid_windows:
                    try:
                        w.hide()
                    except RuntimeError:
                        pass
            else:
                # 全部显示并激活最后一个窗口
                for w in valid_windows:
                    try:
                        w.show()
                        w.activateWindow()
                        if w.isMinimized():
                            w.showNormal()
                        w.raise_()
                    except RuntimeError:
                        pass
        except Exception as e:
            logger.error(f"[TrayManager] _toggle_all_windows 异常: {e}")

    def _ensure_listener_alive(self):
        """检测 keyboard 库监听线程是否存活，若死亡则尝试重启

        仅在「keyboard 兜底模式」(_hotkey_mode == 'kbd')下需要：
        RegisterHotKey 原生模式没有监听线程，无需此检查。
        """
        if getattr(self, "_hotkey_mode", None) != "kbd":
            return
        try:
            listener = keyboard._listener
            thread = getattr(listener, "listening_thread", None)
            if thread is not None and not thread.is_alive():
                logger.warning("[TrayManager] keyboard 监听线程已死亡，尝试重启...")
                listener.listening = False
                listener.init()
                listener.start_if_necessary()
                logger.info("[TrayManager] keyboard 监听线程重启完成")
        except Exception as exc:
            logger.debug(f"[TrayManager] 监听线程检查失败（非致命）: {exc}")

    def _health_check_hotkey(self):
        """定期检查并重建全局热键

        Windows(RegisterHotKey): 注销+重注册，幂等且能在任何极端场景下自愈。
          若组合键被占用则自动回退到 keyboard 兜底（见 _win_register_hotkey）；
          若后续组合键空闲（占用方关闭），此处会重新升级为原生热键。
        其它平台(keyboard LL 钩子): 确认监听线程存活（否则重启）后无竞态重建。
        """
        try:
            hotkey_str = self._read_hotkey_from_command()
            if not hotkey_str:
                hotkey_str = "alt+z"
            hotkey_str = hotkey_str.lower()

            if platform.system() == "Windows":
                # 幂等重注册：RegisterHotKey 失败会自动回退到 keyboard 兜底；
                # 组合键空闲后此处会重新升级为原生 RegisterHotKey。
                self._win_register_hotkey(hotkey_str)
                # keyboard 兜底模式下，确保监听线程存活（否则热键会静默失效）
                self._ensure_listener_alive()
                return

            # 非 Windows：沿用 keyboard 库的重建逻辑
            self._ensure_listener_alive()

            old_handle = self._hotkey_handle
            old_hotkey = self._registered_hotkey

            new_handle = None
            for attempt in range(3):
                try:
                    new_handle = keyboard.add_hotkey(
                        hotkey_str,
                        self._hotkey_bridge.toggle_all_windows.emit,
                        suppress=True,
                    )
                    break  # 成功
                except Exception as exc:
                    if attempt < 2:
                        logger.warning(f"[TrayManager] 热键注册失败（第{attempt + 1}次），1s后重试: {exc}")
                        import time as _time

                        _time.sleep(1)
                    else:
                        logger.warning(f"[TrayManager] 热键注册失败（已重试3次）: {exc}")

            if new_handle is not None:
                if old_handle is not None:
                    try:
                        keyboard.remove_hotkey(old_handle)
                    except Exception:
                        pass
                self._hotkey_handle = new_handle
                self._registered_hotkey = hotkey_str
                logger.debug(f"[TrayManager] 热键健康检查完成: {hotkey_str}")
            else:
                if self._hotkey_handle is None and old_handle is not None:
                    self._hotkey_handle = old_handle
                    self._registered_hotkey = old_hotkey
                    logger.debug("[TrayManager] 热键健康检查失败，保留旧热键")
        except Exception as exc:
            logger.debug(f"[TrayManager] 健康检查异常（非致命）: {exc}")

    def _quit_application(self) -> None:
        """退出应用：强制关闭所有窗口后退出"""
        # 停止定时器
        try:
            self._hotkey_health_timer.stop()
        except Exception:
            pass
        # 移除原生事件过滤器
        if self._hotkey_filter is not None:
            try:
                app = QApplication.instance()
                if app is not None:
                    app.removeNativeEventFilter(self._hotkey_filter)
            except Exception:
                pass
            self._hotkey_filter = None
        # 释放全局热键
        self._release_global_hotkey()
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
