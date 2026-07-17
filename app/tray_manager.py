# -*- coding: utf-8 -*-
"""
全局唯一托盘图标管理器（单例）
所有 ToolPopupDialog 共享同一个 QSystemTrayIcon，避免多个托盘图标。
"""

import math
import platform
import time
import uuid
from pathlib import Path

import keyboard
from loguru import logger
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QSystemTrayIcon


class _HotkeyBridge(QObject):
    """从 keyboard 库线程桥接到 Qt 主线程的信号桥"""

    toggle_all_windows = pyqtSignal()


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
        except RuntimeError, Exception:
            return False

    def __init__(self, parent=None):
        super().__init__(parent)
        if TrayManager._instance is not None:
            raise RuntimeError("TrayManager 是单例，请使用 get_instance() 获取")
        TrayManager._instance = self

        self._windows: list = []  # 已注册的 ToolPopupDialog 列表
        self._pending_notification: dict = {}  # 当前待处理的托盘通知 {notification_id: window}

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

        # 监听托盘消息点击（点击通知时显示对应窗口）
        self._tray_icon.messageClicked.connect(self._on_message_clicked)

        self._tray_icon.show()

        # ========== 全局热键 ==========
        self._hotkey_bridge = _HotkeyBridge()
        self._hotkey_bridge.toggle_all_windows.connect(self._toggle_all_windows)
        self._hotkey_handle = None
        self._registered_hotkey = None
        self._last_toggle_time = 0.0  # 防重复触发时间戳
        self._setup_global_hotkey()

        # 定时重建全局热键（应对 sleep/resume 等导致的钩子丢失）
        self._hotkey_health_timer = QTimer(self)
        self._hotkey_health_timer.timeout.connect(self._health_check_hotkey)
        self._hotkey_health_timer.start(60000)  # 1分钟

        # ========== 多窗口选中管理 ==========
        self._selected_windows: list = []  # 当前选中的 ToolPopupDialog 列表

        # ========== 排布模式 ==========
        # 0=网格(grid) / 1=竖列横排(horizontal) / 2=折叠(stack)
        # Ctrl+Shift+G 依次循环
        self._arrange_mode: int = 0
        self._ARRANGE_MODES = ("网格", "竖列", "折叠")

        logger.info("TrayManager 初始化完成")

    # ========== 多窗口选中管理 ==========

    def _select_window(self, window) -> None:
        """添加窗口到选中列表"""
        if window not in self._selected_windows:
            self._selected_windows.append(window)
            self._update_selection_visuals()

    def _deselect_window(self, window) -> None:
        """从选中列表移除窗口"""
        if window in self._selected_windows:
            self._selected_windows.remove(window)
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

        return x, y, snapped_x, snapped_y

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
            except RuntimeError, Exception:
                continue
        if len(alive) != len(self._selected_windows):
            self._selected_windows = alive
            self._update_selection_visuals()

    def get_arrange_mode(self) -> str:
        """获取当前排布模式名称（用于 UI 提示）"""
        if 0 <= self._arrange_mode < len(self._ARRANGE_MODES):
            return self._ARRANGE_MODES[self._arrange_mode]
        return self._ARRANGE_MODES[0]

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

    def notify(self, title: str, message: str, window: QObject = None) -> None:
        """发送 Windows 通知

        Args:
            title: 通知标题
            message: 通知内容
            window: 触发通知的窗口对象，点击通知时会显示该窗口
        """
        if self._tray_icon.isVisible():
            # 生成唯一 ID 关联通知和窗口
            notification_id = str(uuid.uuid4())
            self._pending_notification[notification_id] = window or self._get_first_valid_window()
            # 保存到实例属性，供 messageClicked 信号处理器使用
            self._last_notification_window = self._pending_notification.get(notification_id)
            self._tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon(1), 4000)
            # 4秒后清理（与 showMessage 的显示时长一致）
            QTimer.singleShot(4500, lambda: self._pending_notification.pop(notification_id, None))

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

    def _get_first_valid_window(self):
        """获取第一个有效的窗口"""
        for w in self._windows:
            if self._is_window_valid(w):
                return w
        return None

    def _show_window(self, window) -> None:
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
            if top_window.isHidden():
                logger.info("[_show_window] 显示窗口")
                top_window.show()
            if top_window.isMinimized():
                top_window.showNormal()
            top_window.activateWindow()
            top_window.raise_()
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

        使用 keyboard 库实现跨进程全局热键，即使窗口全部隐藏也能响应。
        热键回调通过 _HotkeyBridge 信号桥接到 Qt 主线程。
        快捷键来源：从 commands/toggle-window.md 的 shortcut 字段读取（支持热修改）。

        Args:
            hotkey_str: 热键字符串，如 "alt+z"。None 则从命令定义读取。
        """
        if hotkey_str is None:
            hotkey_str = self._read_hotkey_from_command()
        if not hotkey_str:
            hotkey_str = "alt+z"

        hotkey_str = hotkey_str.lower()
        if self._hotkey_handle is not None and hotkey_str == self._registered_hotkey:
            return  # 已注册相同热键，跳过

        try:
            new_handle = keyboard.add_hotkey(
                hotkey_str,
                self._hotkey_bridge.toggle_all_windows.emit,
                suppress=True,
            )
        except Exception as exc:
            logger.warning(f"[TrayManager] 全局热键 {hotkey_str} 注册失败: {exc}")
            return

        # 释放旧热键
        self._release_global_hotkey()

        self._hotkey_handle = new_handle
        self._registered_hotkey = hotkey_str
        logger.info(f"[TrayManager] 全局热键已注册: {hotkey_str}")

    def _release_global_hotkey(self):
        """释放已注册的全局热键"""
        if self._hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
            self._hotkey_handle = None
            self._registered_hotkey = None

    def _toggle_all_windows(self):
        """切换所有窗口的隐藏/显示状态

        任意窗口可见 → 全部隐藏
        全部已隐藏 → 全部显示并激活
        """
        # 防重复触发（全局热键 + QShortcut 兜底同时触发时）
        now = time.perf_counter()
        if now - self._last_toggle_time < 0.5:
            return
        self._last_toggle_time = now

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
                except RuntimeError, Exception:
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

        keyboard 库的 _KeyboardListener 使用 daemon 线程运行 Windows 消息泵。
        该线程若因异常退出（GetMessage 返回 -1、未捕获异常等），
        低层键盘钩子（SetWindowsHookEx）也随之失效，全局热键将永久停止响应。

        keyboard 库自身无线程健康检测机制，此处通过轮询 is_alive() 主动发现。
        """
        try:
            # 通过模块级 _listener 访问内部线程状态
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

        底层 keyboard 库的低级钩子可能因 sleep/resume、UAC 弹窗、线程崩溃等场景丢失。

        修复要点：
        1. 【线程存活检查】先确认 keyboard 监听线程活着，否则重启
        2. 【无竞态重建】先注册新热键，成功后再释放旧热键
           （原逻辑先释放旧热键，若注册失败则热键永久丢失直到下次健康检查）
        3. 【快速重试】首次失败后 1s 重试，最多 3 次
        """
        # 第一步：检查监听线程存活
        self._ensure_listener_alive()

        # 第二步：先注册新热键，成功后再切换（保护旧热键不被提前释放）
        old_handle = self._hotkey_handle
        old_hotkey = self._registered_hotkey

        hotkey_str = self._read_hotkey_from_command()
        if not hotkey_str:
            hotkey_str = "alt+z"
        hotkey_str = hotkey_str.lower()

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
            # 新热键注册成功 → 释放旧热键，切换到新热键
            if old_handle is not None:
                try:
                    keyboard.remove_hotkey(old_handle)
                except Exception:
                    pass
            self._hotkey_handle = new_handle
            self._registered_hotkey = hotkey_str
            logger.debug(f"[TrayManager] 热键健康检查完成: {hotkey_str}")
        else:
            # 注册失败，旧热键仍保留在工作状态
            if self._hotkey_handle is None and old_handle is not None:
                self._hotkey_handle = old_handle
                self._registered_hotkey = old_hotkey
                logger.debug("[TrayManager] 热键健康检查失败，保留旧热键")

    def _quit_application(self) -> None:
        """退出应用：强制关闭所有窗口后退出"""
        # 停止定时器
        try:
            self._hotkey_health_timer.stop()
        except Exception:
            pass
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
