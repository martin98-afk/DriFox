# -*- coding: utf-8 -*-
"""
TabManagerWindow — Tab 管理器宿主窗口

左侧 TabPanel + 右侧 QStackedWidget 嵌入 OpenAIChatToolWindow 实例。
支持模式切换（独立 ↔ 嵌入），窗口状态通过 setParent 迁移完全保留。
"""

import json
from typing import Any, Dict, List, Optional

from PyQt5 import sip as _sip

from loguru import logger
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css
from app.utils.theme_manager import theme_manager


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
    # ── [DEBUG] 拖拽期间 paintEvent 计数 ──
    _paint_count_during_drag: int = 0

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
        self.setMinimumSize(500, 400)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        # 窗口标志：支持最大化 + 独立任务栏按钮 + 置顶
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )

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

    def _setup_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── 左侧 Tab 面板（可拖拽调整宽度）──
        from app.widgets.tab_panel import TabPanel

        self._tab_panel = TabPanel(self)
        self._tab_panel.setObjectName("tabPanel")
        self._tab_panel.setMinimumWidth(120)
        self._tab_panel.setMaximumWidth(400)

        # ── 右侧内容区 ──
        self._content_area = QStackedWidget(self)
        self._content_area.setObjectName("contentArea")

        # 空状态页（索引 0）
        self._empty_state = EmptyStateWidget(self)
        self._empty_state.newTabRequested.connect(self._on_new_tab_requested)
        self._content_area.addWidget(self._empty_state)  # index 0

        # 使用 QSplitter 让左侧面板可拖拽
        from PyQt5.QtWidgets import QSplitter

        self._splitter = QSplitter(Qt.Horizontal, self)
        self._splitter.addWidget(self._tab_panel)
        self._splitter.addWidget(self._content_area)
        self._splitter.setStretchFactor(0, 0)  # 左面板不拉伸
        self._splitter.setStretchFactor(1, 1)  # 右侧内容区拉伸
        # handle 宽设为 1 并由 _apply_theme_stylesheet 给它上 BORDER 颜色，
        # 形成清晰的"左边框线"；QSplitter 自身的子控件绘制顺序会吞掉普通
        # widget 的 border-right，所以用 handle 区域显示更可靠。
        self._splitter.setHandleWidth(1)
        self._splitter.setChildrenCollapsible(False)
        main_layout.addWidget(self._splitter)

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
        """将 TabManagerWindow 标题同步为当前窗口的会话标题"""
        win = self.get_current_window()
        if win:
            t = win.windowTitle()
            if t:
                self.setWindowTitle(t)
                return
        self.setWindowTitle("飘狐-DriFox")

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

        # ── [DEBUG] 拖拽性能诊断 ──
        samples = self.__class__._drag_perf_samples
        in_handler = self.__class__._drag_in_handler
        if len(samples) >= 5:
            intervals = [(samples[i] - samples[i - 1]) * 1000 for i in range(1, len(samples))]
            avg = sum(intervals) / len(intervals)
            mx = max(intervals)
            gt_16 = sum(1 for iv in intervals if iv > 16)
            gt_33 = sum(1 for iv in intervals if iv > 33)
            gt_50 = sum(1 for iv in intervals if iv > 50)
            paints = self.__class__._paint_count_during_drag
            # moveEvent 自身耗时（应远小于 avg，否则 Qt 内部有问题）
            handler_avg = sum(in_handler) / len(in_handler) if in_handler else 0
            handler_max = max(in_handler) if in_handler else 0
            logger.info(
                f"[TabDrag] frames={len(samples)} "
                f"avg={avg:.1f}ms max={mx:.1f}ms "
                f">16:{gt_16} >33:{gt_33} >50:{gt_50} "
                f"paints={paints} "
                f"inHandler={handler_avg:.1f}/{handler_max:.1f}ms"
            )
        # ── 拖拽结束：清理诊断状态 ──
        self.__class__._is_dragging = False
        self.__class__._drag_perf_samples.clear()
        self.__class__._drag_in_handler.clear()
        self.__class__._paint_count_during_drag = 0

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
        """每次显示时恢复位置"""
        super().showEvent(event)
        self._restore_geometry()

    # ── [DEBUG] 拖拽性能诊断 ──
    # 每次 moveEvent 记录时间戳；拖拽停止后由 _do_save_geometry 输出摘要
    _drag_perf_samples: List[float] = []
    # moveEvent 函数体内耗时（用于判断 Qt 事件循环是否慢）
    _drag_in_handler: List[float] = []
    # 是否正在拖拽（moveEvent 在短时间内连续触发即视为拖拽中）
    _is_dragging: bool = False
    _last_move_ts: Optional[float] = None

    def moveEvent(self, event):
        import time as _time

        t0 = _time.monotonic()
        super().moveEvent(event)
        t1 = _time.monotonic()
        self._save_geometry()
        self.__class__._drag_perf_samples.append(t1)
        self.__class__._drag_in_handler.append((t1 - t0) * 1000)  # ms
        # ── 拖拽状态机 ──
        last = self.__class__._last_move_ts
        if last is None or (t1 - last) > 0.15:
            # 第一次或距上次 > 150ms → 视为新拖拽开始
            self.__class__._is_dragging = True
            self.__class__._paint_count_during_drag = 0

        self.__class__._last_move_ts = t1

    def paintEvent(self, event):
        if self.__class__._is_dragging:
            self.__class__._paint_count_during_drag += 1
        super().paintEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
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
