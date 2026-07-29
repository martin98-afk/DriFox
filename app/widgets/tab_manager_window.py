# -*- coding: utf-8 -*-
"""
TabManagerWindow — Tab 管理器宿主窗口（标准系统窗口）

左侧 TabPanel + 右侧 QStackedWidget 嵌入 OpenAIChatToolWindow 实例。
使用标准系统标题栏，Windows 自动提供 Aero Snap / 摇动 / 任务栏预览等。
"""

import json
import platform
from typing import Any, Dict, List, Optional

from PyQt5 import sip as _sip

from loguru import logger
from PyQt5.QtCore import Qt, QPoint, QTimer, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css, get_unified_font

# ── nativeEvent 热路径缓存（模块级，进程内只算一次）──
# 拖拽窗口时每秒有上千条原生消息进入 nativeEvent，
# 这里预先缓存平台判定与 ctypes cast 函数，避免 per-message 开销。
_IS_WINDOWS = platform.system() == "Windows"
_MSG_CAST = None
if _IS_WINDOWS:
    try:
        import ctypes as _ctypes

        from app.tool_popup import _WINDOWS_MSG as _WMSG

        _MSG_STRUCT_PTR = _ctypes.POINTER(_WMSG)

        def _MSG_CAST(addr, _cast=_ctypes.cast, _ptr=_MSG_STRUCT_PTR):  # noqa: E731
            return _cast(addr, _ptr)
    except Exception:
        _MSG_CAST = None


class EmptyStateWidget(QWidget):
    """空状态页 — 最后一个 Tab 关闭时显示"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(16)

        icon_label = QLabel("📑", self)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet("font-size: 48px; background: transparent;")
        layout.addWidget(icon_label)

        self._text_label = QLabel("没有打开的窗口", self)
        self._text_label.setAlignment(Qt.AlignCenter)
        self._text_label.setFont(get_unified_font(14))
        self._text_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(14)}"
        )
        layout.addWidget(self._text_label)

    def refresh_style(self):
        """主题/字体变更后刷新样式"""
        self._text_label.setFont(get_unified_font(14))
        self._text_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(14)}"
        )


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


def _apply_window_topmost(window):
    """应用窗口置顶配置（Settings.window_always_on_top）到指定窗口

    用于启动时和模式切换后，确保配置生效。
    """
    from app.utils.config import Settings as _Settings

    if _Settings.get_instance().window_always_on_top.value:
        flags = window.windowFlags()
        if not (flags & Qt.WindowStaysOnTopHint):
            flags |= Qt.WindowStaysOnTopHint
            was_visible = window.isVisible()
            window.setWindowFlags(flags)
            if was_visible:
                window.show()
                window.raise_()
                window.activateWindow()


def _update_tab_icon(tab_idx: int, project: str):
    """更新指定 Tab 的项目图标

    直接提取缩写+颜色，交给 _TabProjectIcon 用 QPainter 绘制圆角矩形+白字。
    Qt 自动处理 DPI，无需手动创建 QPixmap / round(ceil) 物理像素。
    """
    tm = TabManagerWindow.get_instance()
    if tm is None:
        return
    try:
        from app.widgets.cards.settings.project_selector_card import (
            extract_project_initials,
            get_project_color,
        )

        initials = extract_project_initials(project)
        color_str = get_project_color(project, alpha=255)
        tm._tab_panel.update_tab_project(tab_idx, initials, color_str)
    except Exception:
        pass


class TabManagerWindow(QWidget):
    """Tab 管理器宿主窗口（单例）"""

    _instance: Optional["TabManagerWindow"] = None
    _last_toggle_time: float = 0.0  # 上次模式切换时间戳（time.monotonic），防重入
    # 标题栏拖拽由 Windows 原生管理（HTCAPTION + WS_CAPTION），Python 不干预
    # Windows 原生移动/缩放模态循环消息：拖拽起止的权威信号，
    # 用于替代 moveEvent + 防抖定时器的"猜测式"拖拽检测
    _WM_ENTERSIZEMOVE = 0x0231
    _WM_EXITSIZEMOVE = 0x0232
    _WM_MOVING = 0x0216  # 仅"移动"触发；"缩放"发 WM_SIZING，二者互斥，可精确区分

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
        # ── 窗口拖拽检测：moveEvent 持续触发时视为拖拽中，100ms 无新事件视为结束 ──
        self._window_dragging_timer = QTimer(self)
        self._window_dragging_timer.setSingleShot(True)
        self._window_dragging_timer.setInterval(100)
        self._window_dragging_timer.timeout.connect(self._on_window_drag_end)
        # 编程式移动（如 _restore_geometry）期间跳过拖拽检测，避免误触
        self._suppress_drag_detection: bool = False
        # ── window → index O(1) 映射（替代 _windows.index() O(n) 查找） ──
        self._window_to_index: Dict[int, int] = {}
        # ── 上次图标缩放值，主题切换时用于判断是否需要重绘图标 ──
        self._last_icon_scale: int = -1

        self._geo_save_timer.timeout.connect(self._do_save_geometry)

        # ── Resize 动画节流：100ms 无 resize 事件后退出节流模式 ──
        self._resize_blocking: bool = False  # resize 期间跳过 super().resizeEvent，冻结全部布局
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self._on_resize_finished)

        self.setWindowTitle("飘狐-DriFox")
        self.setObjectName("tabManagerWindow")
        self.setMinimumSize(600, 450)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        # 标准系统窗口：原生标题栏 + Aero Snap + 任务栏预览 + 摇动等全部恢复
        self.setWindowFlags(Qt.Window)
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
        """主题切换时刷新配色

        注意：调用方（dispatch_refresh / _execute_batched_theme_refresh）
        已执行 Colors.refresh()，此处不再重复调用。
        """
        from app.utils.theme_refresh import ThemeRefreshCoordinator

        ThemeRefreshCoordinator.timer_start("tab_manager")

        # 重建样式表
        self._apply_theme_stylesheet()
        # 刷新空状态页（字体/颜色随主题或字号刷新）
        if hasattr(self, "_empty_state"):
            self._empty_state.refresh_style()
        # 刷新所有 Tab 项（标题颜色/字体/图标尺寸随主题或字号刷新）
        # refresh_style 内部已执行 repaint，此处不再重复
        try:
            self._tab_panel.refresh_style()
        except Exception:
            pass
        # 重画所有 tab 的项目图标：仅在 scale_icon_size 变化时才需重建
        # （纯主题色切换不影响图标，跳过可避免 QPainter 开销）
        from app.utils.design_tokens import scale_icon_size as _scale_size

        current_scale = _scale_size(20)
        if current_scale != self._last_icon_scale:
            self._last_icon_scale = current_scale
            for idx, win in enumerate(self._windows):
                try:
                    project = getattr(win, "_current_project", None) or ""
                    if project:
                        _update_tab_icon(idx, project)
                except Exception:
                    pass

        ThemeRefreshCoordinator.timer_end("tab_manager")

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
        直接给 #tabPanel 设 border 看不到；用 4px handle + BORDER 颜色
        在视觉上约 1px 可见（两侧被 BORDER 着色），保留拖拽热区但视觉
        上仍接近细边框的观感。
        """
        self.setStyleSheet(f"""
            #tabPanel {{
                background: {Colors.CARD_BG.format(alpha=240)};
                border-radius: 8px;
            }}
            #tabFrame {{
                /* 左侧圆角矩形容器，与右侧 #chatFrame 对称，提升呼吸感 */
                background: {Colors.CARD_BG.format(alpha=240)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                margin: 4px 0 4px 4px;  /* 四边与窗口 4px 边距，右 0 让位 splitter handle */
            }}
            #tabManagerWindow {{
                background: {Colors.CONTENT_BG};
                border-radius: 8px;
            }}
            #tabManagerContent {{
                background: {Colors.CONTENT_BG};
                border-radius: 8px;
            }}
            #chatFrame {{
                background: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                margin: 4px 4px 4px 0;  /* 左 0 让位给 splitter handle */
            }}
            #contentArea {{
                background: {Colors.CONTENT_BG};
            }}
        """)
        # splitter handle 区域：融入窗口背景，让两侧 frame border 自然形成分隔线
        # 这样不会和 frame border 形成"双重线"叠加，保持 4px 拖拽热区
        if getattr(self, "_splitter", None) is not None:
            self._splitter.setStyleSheet(f"""
                QSplitter::handle:horizontal {{
                    background: {Colors.CONTENT_BG};
                }}
            """)

    def _setup_ui(self):
        # ── 外层纵向布局：直接放内容区（标准系统窗口自带标题栏） ──
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

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

        # ── 左侧 Tab 区域圆角矩形包裹框架（与右侧 #chatFrame 对称，提升呼吸感） ──
        # 在 splitter 里直接给 #tabPanel 设 border 会被 splitter handle 子控件
        # 绘制顺序吞掉，所以再包一层 QFrame 用 objectName 给样式。
        self._tab_frame = QFrame(content_widget)
        self._tab_frame.setObjectName("tabFrame")
        # QSplitter 控制的 child 是 _tab_frame 而非 _tab_panel，因此
        # _tab_frame 必须显式设最大/最小宽度，否则 splitter 拖拽会绕过
        # _tab_panel 的约束：
        #   frame max = panel max(400) + margins(12) + border(2) = 414
        #   frame min = panel min(120) + margins(12) + border(2) = 134
        self._tab_frame.setMinimumWidth(134)
        self._tab_frame.setMaximumWidth(414)
        tab_frame_layout = QVBoxLayout(self._tab_frame)
        # 与 #chatFrame 内边距完全对齐：让两侧容器视觉一致
        tab_frame_layout.setContentsMargins(6, 6, 6, 6)
        tab_frame_layout.setSpacing(0)
        tab_frame_layout.addWidget(self._tab_panel)

        # 右侧内容区
        self._content_area = QStackedWidget(content_widget)
        self._content_area.setObjectName("contentArea")

        # 空状态页（索引 0）
        self._empty_state = EmptyStateWidget(content_widget)
        self._content_area.addWidget(self._empty_state)  # index 0

        # ── 右侧对话区域圆角矩形包裹框架 ──
        self._chat_frame = QFrame(content_widget)
        self._chat_frame.setObjectName("chatFrame")
        chat_frame_layout = QVBoxLayout(self._chat_frame)
        chat_frame_layout.setContentsMargins(6, 6, 6, 6)
        chat_frame_layout.setSpacing(0)
        chat_frame_layout.addWidget(self._content_area)

        # 使用 QSplitter 让左侧面板可拖拽
        from PyQt5.QtWidgets import QSplitter

        self._splitter = QSplitter(Qt.Horizontal, content_widget)
        self._splitter.addWidget(self._tab_frame)
        self._splitter.addWidget(self._chat_frame)
        self._splitter.setStretchFactor(0, 0)  # 左面板不拉伸
        self._splitter.setStretchFactor(1, 1)  # 右侧内容区拉伸
        # handle 宽 4px：足够宽的拖拽热区确保交互稳定（Qt 中 1px handle
        # 配合 QSplitter::handle 样式表在某些版本下命中区域会被覆盖，
        # 导致拖拽不可靠）；由 _apply_theme_stylesheet 给它上 BORDER 颜色，
        # 形成清晰的"左边框线"视觉效果。
        self._splitter.setHandleWidth(4)
        self._splitter.setChildrenCollapsible(False)
        content_layout.addWidget(self._splitter)
        content_widget.setLayout(content_layout)
        main_layout.addWidget(content_widget, 1)

        # 恢复面板宽度
        saved_w = Settings.get_instance().tab_panel_width.value
        if saved_w:
            # 补偿新增 #tabFrame 的 layout margins(12) + border(2) = 14px，
            # 保证 panel 的视觉宽度与改造前一致，避免用户配置缩水
            frame_w = saved_w + 14
            self._splitter.setSizes([frame_w, max(0, self.width() - frame_w)])

        # 应用样式（使用 _apply_theme_stylesheet 以确保 objectName 选择器生效）
        self._apply_theme_stylesheet()

    def _setup_signals(self):
        self._tab_panel.tabSelected.connect(self._on_tab_selected)
        self._tab_panel.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_panel.tabBranchRequested.connect(self._on_tab_branch_requested)
        self._tab_panel.newTabRequested.connect(self._on_new_tab_requested)

    # ── 窗口管理 ──

    def add_window(self, window) -> int:
        """添加窗口到 Tab 管理器，返回索引"""
        existing = self._window_to_index.get(id(window), -1)
        if existing >= 0 and existing < len(self._windows):
            return existing

        self._windows.append(window)
        idx = len(self._windows) - 1  # 0-based
        self._window_to_index[id(window)] = idx

        # 添加到 QStackedWidget（从索引 1 开始，0 是空状态页）
        self._content_area.addWidget(window)

        # 获取初始标题：优先用项目名，其次窗口标题，最后默认
        project = getattr(window, "_current_project", None) or ""
        title = window.windowTitle() or project or "新建会话"

        # 获取初始图标：提取项目缩写+颜色，交给 _TabProjectIcon 直接 QPainter 绘制
        tab_project_initials = ""
        tab_project_color = ""
        if project:
            try:
                from app.widgets.cards.settings.project_selector_card import (
                    extract_project_initials,
                    get_project_color,
                )

                tab_project_initials = extract_project_initials(project)
                tab_project_color = get_project_color(project, alpha=255)
            except Exception:
                pass

        tab_idx = self._tab_panel.add_tab(
            title, icon=None, project_initials=tab_project_initials, project_color=tab_project_color
        )

        # 统一回调：标题变更时同步更新 Tab 标题 + 项目图标 + 宿主窗口标题 + 团队胶囊
        # ★ 使用 _window_to_index O(1) 字典查找，替代 _windows.index() O(n)
        def _on_win_title_changed(_new_title, _win=window):
            if _sip.isdeleted(_win):
                return
            cur_idx = self._window_to_index.get(id(_win), -1)
            if cur_idx < 0 or cur_idx >= len(self._windows):
                return
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
            cur_idx = self._window_to_index.get(id(_win), -1)
            if cur_idx < 0 or cur_idx >= len(self._windows):
                return
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
        self._tab_panel.set_active_index(idx)

        self.tabCountChanged.emit(len(self._windows))
        return idx

    def remove_window(self, window):
        """从 Tab 管理器移除窗口"""
        idx = self._window_to_index.pop(id(window), -1)
        if idx < 0 or idx >= len(self._windows):
            return

        self._windows.pop(idx)
        # 被移除窗口之后的所有窗口索引减 1
        for j in range(idx, len(self._windows)):
            self._window_to_index[id(self._windows[j])] = j

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
        """将标题同步为当前窗口的会话标题（系统标题栏）"""
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

    def _on_tab_close_requested(self, index: int):
        if 0 <= index < len(self._windows):
            window = self._windows[index]
            # 先从列表中移除，避免后续操作访问到已销毁的窗口
            self._windows.pop(index)
            del self._window_to_index[id(window)]
            # 被移除窗口之后的所有窗口索引减 1
            for j in range(index, len(self._windows)):
                self._window_to_index[id(self._windows[j])] = j
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

    def _on_tab_branch_requested(self, index: int):
        """分支窗口 — 从指定标签页创建分支"""
        if 0 <= index < len(self._windows):
            window = self._windows[index]
            if hasattr(window, "_duplicate_window"):
                window._duplicate_window(branch=True)

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

        # ★ 确保 TrayManager 引用已注册
        # _disable_mode 会清空 tray_manager._tab_manager_window，而实例可能
        # 已存在（走 get_instance 不走 __init__），必须在此显式重新注册。
        tray_manager._tab_manager_window = tab_mgr

        # 清理旧的 Tab 面板和内容区（防止上次 Tab 模式残留）
        for i in range(tab_mgr._tab_panel.count - 1, -1, -1):
            tab_mgr._tab_panel.remove_tab(i)
        while tab_mgr._content_area.count() > 1:
            w = tab_mgr._content_area.widget(1)
            tab_mgr._content_area.removeWidget(w)
        tab_mgr._windows.clear()
        tab_mgr._window_to_index.clear()
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

            # 应用窗口置顶配置
            _apply_window_topmost(tab_mgr)

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
            # 先清空旧状态，再重新迁入所有窗口（add_window 会重建 _window_to_index）
            windows = list(tab_mgr._windows)
            tab_mgr._windows.clear()
            tab_mgr._window_to_index.clear()

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
                    # 应用窗口置顶配置
                    _apply_window_topmost(dialog)
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
        """防抖：记录位置/尺寸，等拖拽/缩放结束后 200ms 再写盘

        ★ 拖拽/缩放模态循环期间直接跳过：否则鼠标中途停顿 >200ms 时
        _do_save_geometry 会在拖拽帧里触发（遍历 splitter + 写配置项）
        → 偶发掉帧。循环结束后由 _on_window_drag_end / EXITSIZEMOVE
        分支统一补一次。
        """
        from app.tool_popup import ToolPopupDialog

        if ToolPopupDialog._any_window_dragging:
            return
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
        # 保存面板宽度（去除新增 #tabFrame 的 layout margins + border 14px）
        if hasattr(self, "_splitter"):
            sizes = self._splitter.sizes()
            if sizes:
                Settings.get_instance().tab_panel_width.value = max(120, sizes[0] - 14)

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
                self._suppress_drag_detection = True
                self.setGeometry(x, y, g["w"], g["h"])
                return
        except Exception:
            pass
        finally:
            self._suppress_drag_detection = False

        # 首次：居中
        self._suppress_drag_detection = True
        self.setGeometry(
            screen_rect.x() + (screen_rect.width() - w) // 2,
            screen_rect.y() + (screen_rect.height() - h) // 2,
            w,
            h,
        )
        self._suppress_drag_detection = False

    def showEvent(self, event):
        """每次显示时恢复位置（标准系统窗口自带阴影/边框/圆角）"""
        super().showEvent(event)
        self._restore_geometry()

    def moveEvent(self, event):
        super().moveEvent(event)
        # ── 窗口拖拽检测 ──
        # Windows：拖拽起止由系统原生消息 WM_ENTERSIZEMOVE / WM_EXITSIZEMOVE
        # 精确驱动（见 nativeEvent），不再用 moveEvent + 100ms 防抖定时器猜测。
        # 旧方案的缺陷：拖拽中途鼠标停顿 >100ms 会被误判为"拖拽结束"，
        # setUpdatesEnabled(True) 触发积压重绘轰炸主线程，手一动又重新禁用，
        # 造成周期性掉帧卡顿。
        # 非 Windows：无原生模态循环消息，保留防抖检测作为回退。
        if not _IS_WINDOWS:
            # _suppress_drag_detection 用于阻止编程式 setGeometry 误触
            if not self._suppress_drag_detection:
                if not self._window_dragging_timer.isActive():
                    self._on_window_drag_start()
                self._window_dragging_timer.start()  # 持续重置防抖
        # 几何保存防抖（拖拽期间由 _save_geometry 内部守卫跳过）
        self._save_geometry()

    def nativeEvent(self, eventType, message):
        """Windows 原生消息处理：精确捕获拖拽/缩放模态循环的起止

        WM_ENTERSIZEMOVE / WM_EXITSIZEMOVE 是 OS 对"用户正在拖动/缩放窗口"
        的权威信号——标题栏拖拽由系统原生管理（WS_CAPTION），Qt 收不到
        mousePress/Release，只能靠这两条消息准确判定拖拽区间。
        """
        # ★ 热路径：拖拽时每秒有上千条原生消息经过这里（WM_MOUSEMOVE /
        # WM_NCHITTEST 等），任何 per-call 开销都会被放大。
        # 判定/结构体/cast 全部使用模块级缓存（_IS_WINDOWS / _MSG_CAST），
        # 禁止在此函数内做 import 或 platform.system() 调用。
        if _IS_WINDOWS and _MSG_CAST is not None and eventType == "windows_generic_MSG":
            try:
                msg_id = _MSG_CAST(int(message))[0].message
                if msg_id == self._WM_ENTERSIZEMOVE:
                    self._window_dragging_timer.stop()  # 原生信号权威，停用防抖回退
                    self._on_window_drag_start()
                elif msg_id == self._WM_MOVING:
                    # ★ 纯移动：客户区内容不随位置变化，禁用整窗重绘。
                    # DWM 仅平移已有纹理即可，无需 app 重绘；这能消除拖拽时标题栏
                    # SVG 按钮（qfluentwidgets）每次 paintEvent 现场 new QSvgRenderer
                    # 解析 SVG 的昂贵同步开销（见 [DRAG-PROF] 热点 #2），拖拽丝滑。
                    # 仅对"移动"生效；"缩放"走 _resize_blocking 独立机制，互不干扰。
                    self.setUpdatesEnabled(False)
                elif msg_id == self._WM_EXITSIZEMOVE:
                    # 任何拖拽（移动/缩放）结束都恢复整窗重绘权。移动路径依赖此
                    # 恢复；缩放路径的子面板重绘由 _deferred_resize_complete 负责，
                    # 此处恢复顶层窗口不会与之冲突（顶层本就可重绘标题栏）。
                    self.setUpdatesEnabled(True)
                    if self._resize_blocking:
                        # 本次模态循环是"缩放"：布局/绘制恢复交给
                        # _on_resize_finished（防抖 100ms 后触发），
                        # 此处仅复位全局拖拽标志，避免双重恢复冲突
                        from app.tool_popup import ToolPopupDialog
                        from app.utils.drag_stall_profiler import drag_profiler

                        ToolPopupDialog._any_window_dragging = False
                        # 循环期间几何保存被守卫跳过，此处补一次防抖保存
                        self._save_geometry()
                        drag_profiler.stop_deferred()
                    else:
                        self._on_window_drag_end()
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    # ── 窗口拖拽节流 ──

    def _on_window_drag_start(self):
        """窗口拖拽开始：暂停动画定时器 + 通知各组件进入节流模式

        利用 ToolPopupDialog._any_window_dragging 全局标志（多窗口模式原有），
        使 Tab 模式下的嵌入窗口（main_widget/bottom_input_area/card_container）
        跳过耗时的布局重算和高度调整，与多窗口模式享受同等的拖拽性能优化。

        ★ 不再 setUpdatesEnabled(False)：纯移动窗口时 DWM 直接在新位置合成，
        客户区根本不需要重绘，禁用绘制毫无收益；而重新启用时 Qt 会把整棵
        控件树标脏 → 松手瞬间全窗口（含所有 MessageCard）一次性全量重绘，
        这正是"松手卡一下"的根因。缩放路径仍由 resizeEvent 的 blocking
        机制独立管理（缩放确实需要冻结绘制）。
        """
        from app.tool_popup import ToolPopupDialog
        from app.utils.drag_stall_profiler import drag_profiler

        ToolPopupDialog._any_window_dragging = True
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_resizing(True)  # 暂停动画定时器
        # 诊断：拖拽期间抓取主线程阻塞现场（定位偶发卡顿元凶）
        drag_profiler.start()

    def _on_window_drag_end(self):
        """窗口拖拽结束：恢复动画定时器 + 解除节流 + 触发一次几何保存

        无 setUpdatesEnabled(True) → 无积压重绘炸弹，松手零代价。
        """
        from app.tool_popup import ToolPopupDialog
        from app.utils.drag_stall_profiler import drag_profiler

        ToolPopupDialog._any_window_dragging = False
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_resizing(False)  # 如有流式则恢复动画
        # 拖拽期间 moveEvent 跳过了几何保存防抖，此处统一补一次（200ms 后落盘）
        self._save_geometry()
        # 诊断：延迟 1.5s 停止采样，覆盖"松手瞬间卡顿"窗口期
        drag_profiler.stop_deferred()

    def changeEvent(self, event):
        """标准系统窗口自带最大化/还原处理，无需自定义逻辑"""
        super().changeEvent(event)

    def _on_resize_finished(self):
        """resize 结束后恢复布局 + 绘制 + 强制收拢

        防抖：连续 resize 事件后 100ms 无新事件时触发：
        - 解除 blocking，允许布局事件正常传播
        - 恢复 TabPanel 动画
        - 将全量布局重算延迟到下一事件循环迭代，**不阻塞 UI 主线程**

        ★ blocking 期间用户可能拖拽了 splitter handle 调整了左右面板宽度，
        需先保存 splitter sizes，延迟恢复时重新应用。
        """
        self._resize_blocking = False
        # Phase 1（同步）：恢复标志/动画，保存 splitter sizes（轻量）
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_resizing(False)
            # 保持 updates 禁用，等 deferred 阶段与 relayout 一起恢复，
            # 避免旧 geometry 被 paint 出一帧视觉闪烁
        _saved_splitter_sizes = None
        if hasattr(self, "_splitter") and self._splitter.count() > 0:
            _saved_splitter_sizes = list(self._splitter.sizes())

        # Phase 2（异步）：启用绘制 + 全量 relayout → 延迟到下一事件循环迭代
        QTimer.singleShot(0, lambda: self._deferred_resize_complete(_saved_splitter_sizes))

    def _deferred_resize_complete(self, saved_splitter_sizes):
        """延迟执行的全量布局恢复 — 不阻塞 UI 主线程

        在 _on_resize_finished 的同步阶段之后，于下一事件循环迭代执行：
        - 启用子控件绘制（setUpdatesEnabled=True）
        - 强制完整 relayout（layout.invalidate + activate）
        - 恢复用户拖拽设定的 splitter 面板宽度
        - 触发重绘

        ★ 守卫：如果新的 resize 循环已在 deferred 前启动（_resize_blocking 为 True），
        则跳过本次 deferred 执行，避免干扰新 resize 循环的 blocking 机制。
        """
        if self._resize_blocking:
            # 新的 resize 循环已开始，放弃本次延迟恢复
            return
        # 启用子控件绘制
        if hasattr(self, "_tab_panel"):
            self._tab_panel.setUpdatesEnabled(True)
        if hasattr(self, "_content_area"):
            self._content_area.setUpdatesEnabled(True)

        # 强制完整 relayout：blocking 期间跳过了 super().resizeEvent，
        # 子控件 geometry 与窗口新尺寸已不同步。通过 invalidate + activate
        # 强制 Qt 从顶层布局开始重新计算整棵 widget 树。
        self._force_relayout()

        # 恢复 splitter sizes（阻止 stretch factor 重算覆盖用户拖拽尺寸）
        if saved_splitter_sizes is not None and hasattr(self, "_splitter"):
            try:
                cur = self._splitter.sizes()
                # 只在 total width 一致或接近时才恢复（防止窗口尺寸变化后越界）
                if cur and abs(sum(cur) - sum(saved_splitter_sizes)) < 20:
                    self._splitter.setSizes(saved_splitter_sizes)
            except Exception:
                pass

        # 安全守卫：检查左面板宽度是否异常（被压缩到 ≤最小宽度的 80%）
        if hasattr(self, "_splitter") and self._splitter.count() > 0:
            try:
                sizes = self._splitter.sizes()
                tab_min = self._tab_panel.minimumWidth() if hasattr(self, "_tab_panel") else 120
                if sizes and sizes[0] < tab_min * 0.8 and saved_splitter_sizes:
                    self._splitter.setSizes(saved_splitter_sizes)
            except Exception:
                pass

        # 触发重绘：relayout 更新了 geometry 但不会自动 paint
        if hasattr(self, "_tab_panel"):
            self._tab_panel.update()
        if hasattr(self, "_content_area"):
            self._content_area.update()

    def _force_relayout(self):
        """blocking 结束后强制完整布局收拢

        blocking 期间跳过了 super().resizeEvent()，子控件的 geometry
        与窗口新尺寸已不同步。通过 invalidate + activate 链使 Qt 从
        顶层布局开始重新计算整棵 widget 树的 geometry，触发完整的
        resizeEvent 传播链，一次性收拢到正确尺寸。
        """
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()

    def resizeEvent(self, event):
        """标准系统窗口自带 resize 处理 + resize 节流防抖

        resize 期间通过 _resize_blocking 标志做两阶段处理：

        阶段一（首个 resize 事件 → blocking 开启）：
        1. 正常调用 super().resizeEvent() 让布局引擎处理首帧
        2. 禁用左右两侧面板绘制（setUpdatesEnabled=False）
        3. 通知 TabPanel 暂停动画
        4. 开启 blocking 标志

        阶段二（后续连续 resize 事件 → blocking 活跃）：
        - 跳过 super().resizeEvent()，完全阻止布局引擎递归计算
          子控件 geometry（这是缩小窗口卡顿的根因）

        resize 停止 100ms 后 blocking 解除，_on_resize_finished 触发
        一次完整 relayout 收拢到正确尺寸。
        """
        if self._resize_blocking:
            # ── 阶段二：blocking 活跃，跳过布局传播 ──
            self._resize_timer.start()  # 重置防抖
            self._save_geometry()
            return

        # ── 阶段一：首个 resize 事件，正常布局 + 初始化 blocking ──
        super().resizeEvent(event)
        # 通知 TabPanel 进入 resize 节流模式
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_resizing(True)
        # 禁用内容区绘制
        if hasattr(self, "_content_area"):
            self._content_area.setUpdatesEnabled(False)
        # 禁用左侧 TabPanel 绘制
        if hasattr(self, "_tab_panel"):
            self._tab_panel.setUpdatesEnabled(False)
        # 开启 blocking：后续连续 resize 事件将跳过 super().resizeEvent()
        self._resize_blocking = True
        self._resize_timer.start()
        self._save_geometry()

    def closeEvent(self, event: QCloseEvent):
        """关闭 TabManagerWindow 时不销毁，仅隐藏到系统托盘

        对于标准系统窗口，必须 accept 事件让 Qt 正确更新内部状态；
        WA_DeleteOnClose=False 防止窗口被销毁，Qt 只会隐藏窗口。
        event.ignore() 在此场景下会导致 Qt 内部状态不一致，
        后续 show() 无法正常恢复窗口。
        """
        event.accept()

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
        self._window_to_index.clear()
        self._cached_dialogs.clear()
        self.close()
