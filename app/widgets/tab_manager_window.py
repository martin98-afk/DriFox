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
from PyQt5.QtCore import QEasingCurve, QEvent, Qt, QPoint, QTimer, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
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

# ── 对话区最大宽度（px）：窗口过宽时限制对话区宽度并居中，两侧留白 ──
# 避免聊天内容在超宽屏幕上被拉得难以阅读。窗口宽度不足该值时对话区自动占满。
_MAX_CHAT_WIDTH = 1000

# ── 侧边栏展开最小宽度（px，frame 宽，含 margins 12 + border 2）──
# 挤压折叠后点击展开时，若保存的宽度已被压到折叠阈值以下，展开不得窄于该值，
# 否则标题文字被压成窄条无法阅读。
_EXPANDED_MIN_FRAME_WIDTH = 200

# ── 挤压折叠后自动展开的窗口增长阈值（px）──
# 窗口 resize 挤压折叠后，需比折叠时窗口总宽再宽出该值才自动展开，
# 避免"折叠刚完成条件恰满足就弹回展开"的抖动（绝对条件在窗口 ~760 时
# 折叠即满足展开条件，导致折叠态无法保持）。overlay 卡片关闭属布局恢复
# （窗口总宽未变），不走增长条件。
_AUTO_EXPAND_GROWTH = 200

# ── nativeEvent 热路径缓存（模块级，进程内只算一次）──
# 拖拽窗口时每秒有上千条原生消息进入 nativeEvent，
# 这里预先缓存平台判定与 ctypes cast 函数，避免 per-message 开销。
_IS_WINDOWS = platform.system() == "Windows"
_MSG_CAST = None
if _IS_WINDOWS:
    try:
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        # Windows 原生消息结构（nativeEvent 热路径用）。
        # 原定义于 app/tool_popup.py（随 ToolPopupDialog 一并下线），
        # 迁移到本模块避免 tool_popup 模块残留依赖。
        class _WINDOWS_MSG(_ctypes.Structure):
            _fields_ = [
                ("hwnd", _wintypes.HWND),
                ("message", _wintypes.UINT),
                ("wParam", _wintypes.WPARAM),
                ("lParam", _wintypes.LPARAM),
                ("time", _wintypes.DWORD),
                ("pt", _wintypes.POINT),
            ]

        _MSG_STRUCT_PTR = _ctypes.POINTER(_WINDOWS_MSG)

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

    团队窗口特殊处理：Tab 图标已被团队模式隐藏（项目 icon 移到团队标题处），
    此处改为刷新团队框 header 的项目 icon；数据源必须为团队级 project
    （TeamManager.get_team_project，多个成员共享同一 header）——团队级
    尚未设置时回退本次传入的 project（即正在切换的目标项目，随后
    _broadcast_team_project 会写入团队级，保证一致）。
    """
    tm = TabManagerWindow.get_instance()
    if tm is None:
        return
    try:
        # 团队窗口：刷新团队框 header 的项目 icon（团队级数据）
        if 0 <= tab_idx < len(tm._windows):
            win = tm._windows[tab_idx]
            if getattr(win, "_team_agent_name", "") or "":
                team_id = tm._resolve_tab_team_id(win)
                if team_id:
                    initials, color = tm._team_project_icon_data(win, fallback=project)
                    tm._tab_panel.set_team_project(team_id, initials, color)
                    return

        from app.widgets.cards.settings.project_selector_card import (
            extract_project_initials,
            get_project_color,
        )

        initials = extract_project_initials(project)
        color_str = get_project_color(project, alpha=255)
        tm._tab_panel.update_tab_project(tab_idx, initials, color_str)
    except Exception:
        pass


def _get_window_session_title(win) -> str:
    """获取窗口当前会话标题（topic_summary or name），失败返回空串。

    用于团队窗口：Tab 标题保持会话标题，角色名只进胶囊。
    """
    try:
        sm = getattr(win, "session_manager", None)
        if sm is None:
            return ""
        session = sm.get_current_session()
        if session is None:
            return ""
        return session.topic_summary or session.name or ""
    except Exception:
        return ""


class TabManagerWindow(QWidget):
    """Tab 管理器宿主窗口（单例）"""

    _instance: Optional["TabManagerWindow"] = None
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
        # ── 侧边栏展开/折叠宽度动画 ──
        self._sidebar_anim: Optional[QVariantAnimation] = None
        # 动画方向（True=收起），供 valueChanged 里判断跨阈值时机
        self._sidebar_anim_collapsing: bool = False
        self._sidebar_anim_ui_switched: bool = False  # 动画中是否已跨阈值切换 UI

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

        # 全局卡片控制器：系统设置/服务商编辑/Hook/MCP 卡片挂载在 Tab 窗口层
        # （单例在此处初始化，确保全局容器已随 _setup_ui 创建完毕）
        from app.widgets.cards.global_card_controller import get_global_card_controller

        get_global_card_controller()

        # 刷新 Tab 面板内嵌的 UI 插件列表
        self._tab_panel.refresh_ui_plugins()
        # 注册到 TrayManager
        from app.tray_manager import TrayManager

        TrayManager.get_instance()._tab_manager_window = self

        # 注册主题刷新回调（虽主题切换路径不走 dispatch_refresh，
        # 但保持接口一致性便于将来扩展）
        theme_manager.register_refresh_target(self)

        # 初始加载全局背景图（延迟到首帧后，背景为纯装饰，不阻塞出现）
        QTimer.singleShot(0, self._apply_bg_from_theme)

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
        # 刷新四向全局卡片容器背景（主题色/边框随主题切换）
        for _c in (
            getattr(self, "_global_top_container", None),
            getattr(self, "_global_bottom_container", None),
            getattr(self, "_global_left_container", None),
            getattr(self, "_global_right_container", None),
        ):
            if _c is not None:
                try:
                    _c.refresh_style()
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

        # 刷新全局背景图
        self._apply_bg_from_theme()

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
                background: {Colors.CARD_BG.format(alpha=150)};
                border-radius: 8px;
            }}
            #tabFrame {{
                /* 左侧圆角矩形容器，与右侧 #chatFrame 对称，提升呼吸感 */
                background: {Colors.CARD_BG.format(alpha=150)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                margin: 4px 0 4px 4px;  /* 四边与窗口 4px 边距，右 0 让位 splitter handle */
            }}
            #tabManagerWindow {{
                background: {Colors.CONTENT_BG};
                border-radius: 8px;
            }}
            #tabManagerContent {{
                background: transparent;
                border-radius: 8px;
            }}
            #chatFrame {{
                background: {Colors.CARD_BG.format(alpha=150)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                margin: 4px 4px 4px 0;  /* 左 0 让位给 splitter handle（矩形边框保持全宽） */
            }}
            #contentArea {{
                background: transparent;
            }}
            #contentStack {{
                background: transparent;
            }}
            #globalOverlay {{
                background: {Colors.CARD_BG.format(alpha=246)};
                border-radius: 8px;
            }}
        """)
        # splitter handle 区域：融入窗口背景，让两侧 frame border 自然形成分隔线
        # 这样不会和 frame border 形成"双重线"叠加，保持 4px 拖拽热区
        if getattr(self, "_splitter", None) is not None:
            self._splitter.setStyleSheet("QSplitter::handle:horizontal { background: transparent; }")
        # ── 停靠区 splitter：handle 绘制可见分隔线（明确 UI 卡片与对话区边界）──
        # 6px 热区中画 2px 居中线（BORDER 色），hover 时变主题强调色提示可拖拽。
        # 停靠容器折叠时自身 hide()，对应 handle 由 Qt 自动隐藏，不留缝。
        if getattr(self, "_dock_splitter", None) is not None:
            self._dock_splitter.setStyleSheet(f"""
                #dockSplitter::handle:horizontal {{
                    background: transparent;
                    border-left: 2px solid {Colors.BORDER};
                    margin: 10px 2px;
                    border-radius: 1px;
                }}
                #dockSplitter::handle:horizontal:hover {{
                    border-left: 2px solid {Colors.BORDER_ACCENT};
                }}
            """)
        if getattr(self, "_vdock_splitter", None) is not None:
            self._vdock_splitter.setStyleSheet(f"""
                #vDockSplitter::handle:vertical {{
                    background: transparent;
                    border-top: 2px solid {Colors.BORDER};
                    margin: 2px 10px;
                    border-radius: 1px;
                }}
                #vDockSplitter::handle:vertical:hover {{
                    border-top: 2px solid {Colors.BORDER_ACCENT};
                }}
            """)

    def _apply_bg_from_theme(self):
        """从当前主题配置加载背景图片，作为 TabManagerWindow 全局背景

        背景为纯装饰：单例窗口全局一张，不随对话框各自加载。
        优化：缓存背景配置，同一背景（路径+透明度）不重复创建 QLabel。
        """
        try:
            bg_config = theme_manager.get_theme_background(theme_manager.get_current_theme_id())
            chat_list = bg_config.get("chat_list", {})
            if chat_list.get("enabled", True):
                image = chat_list.get("image", ":/icons/fox_bg.png")
                opacity = chat_list.get("opacity", 0.1)
            else:
                image = None
                opacity = 0.1

            # ── 缓存检查：同一背景配置跳过重建 ──
            from app.utils.theme_refresh import ThemeRefreshCoordinator

            bg_key = ThemeRefreshCoordinator.get_bg_cache_key(image, opacity)
            if (
                getattr(self, "_last_bg_key", None) == bg_key
                and hasattr(self, "_bg_label")
                and self._bg_label is not None
            ):
                return
            self._last_bg_key = bg_key

            if image:
                # 先清除旧背景
                if hasattr(self, "_bg_label") and self._bg_label is not None:
                    self._bg_label.deleteLater()
                    self._bg_label = None
                # 解析图片路径：主题文件夹内的相对路径基于主题目录
                import os as _os

                if not image.startswith(":") and not _os.path.isabs(image):
                    theme_dir = theme_manager.get_theme_dir(theme_manager.get_current_theme_id())
                    if theme_dir:
                        abs_path = str(theme_dir / image)
                        if _os.path.exists(abs_path):
                            image = abs_path
                self._bg_label = QLabel(self)
                self._bg_label.setPixmap(QPixmap(image))
                self._bg_label.setScaledContents(True)
                self._bg_opacity = QGraphicsOpacityEffect(self._bg_label)
                self._bg_opacity.setOpacity(opacity)
                self._bg_label.setGraphicsEffect(self._bg_opacity)
                self._bg_label.lower()
                self._bg_label.setAttribute(Qt.WA_TransparentForMouseEvents)
                self._bg_label.resize(self.size())
                self._bg_label.show()
            else:
                # 主题禁用背景图，清除旧背景
                if hasattr(self, "_bg_label") and self._bg_label is not None:
                    self._bg_label.deleteLater()
                    self._bg_label = None
        except Exception:
            pass

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
        # 最小宽度取收起状态的窄宽度(46px)，展开时由 splitter 控制实际宽度
        self._tab_panel.setMinimumWidth(self._tab_panel._collapsed_min_width)
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
        #   frame min = panel min(46) + margins(12) + border(2) = 60
        self._tab_frame.setMinimumWidth(60)
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

        # ── 全局卡片宿主容器（Tab 级系统卡片） ──
        # 系统配置 / 服务商编辑 / Hook 编辑 / MCP 编辑等全局卡片不再绑定
        # 单个对话窗口，统一挂在此容器（位于对话区上方，随卡片显隐展开/折叠）。
        # 对话级卡片（项目/会话/模型选择）仍留在各 OpenAIChatToolWindow 内部。
        from app.widgets.cards.card_container import CardContainer
        from app.widgets.cards.card_manager import (
            GLOBAL_WINDOW_ID,
            CardManager,
            ContainerType,
        )

        _card_mgr = CardManager.get_instance()
        _card_mgr.register_window(GLOBAL_WINDOW_ID)

        # 四向全局容器：上/下沿高度折叠，左/右沿宽度折叠（停靠区）
        self._global_top_container = CardContainer(ContainerType.TOP)
        self._global_top_container.setObjectName("globalCardContainer")
        # TOP 容器启用覆盖层模式：不再在 splitter 中展开，而是作为 QStackedWidget 覆盖层
        self._global_top_container.set_overlay_mode(True)
        self._global_bottom_container = CardContainer(ContainerType.BOTTOM)
        self._global_bottom_container.setObjectName("globalBottomContainer")
        self._global_left_container = CardContainer(ContainerType.LEFT)
        self._global_left_container.setObjectName("globalLeftContainer")
        self._global_right_container = CardContainer(ContainerType.RIGHT)
        self._global_right_container.setObjectName("globalRightContainer")
        for _c in (
            self._global_top_container,
            self._global_bottom_container,
            self._global_left_container,
            self._global_right_container,
        ):
            _c.bind_card_manager(_card_mgr, GLOBAL_WINDOW_ID)

        # 兼容别名：GlobalCardController 及旧代码通过 _global_card_container 访问 TOP 容器
        self._global_card_container = self._global_top_container
        # UIPluginRegistry 复用的鸭子类型属性（与 OpenAIChatToolWindow 对齐）
        self._card_manager = _card_mgr
        self._window_id = GLOBAL_WINDOW_ID
        self._top_card_container = self._global_top_container
        self._bottom_card_container = self._global_bottom_container
        self._left_card_container = self._global_left_container
        self._right_card_container = self._global_right_container

        # 标记 LEFT/RIGHT/BOTTOM 为共存容器：四向区域可同时存在、互不关闭。
        # 覆盖层（TOP）通过 QStackedWidget 仅替换对话区，不影响 LEFT/RIGHT/BOTTOM。
        _card_mgr.mark_coexist_containers(
            GLOBAL_WINDOW_ID,
            frozenset({ContainerType.LEFT, ContainerType.RIGHT, ContainerType.BOTTOM}),
        )

        # ── 停靠区双层 QSplitter：四向占比均可拖拽调整 ──
        # 结构：vDockSplitter(纵向)
        #         ├─ dockSplitter(横向)：左停靠区 | 内容区(含覆盖层) | 右停靠区
        #         └─ 下停靠区（bottom 容器）
        #
        # 内容区内嵌 QStackedWidget：
        #   Page 0: 对话区（_content_area）
        #   Page 1: 覆盖层（_global_overlay / _global_top_container）
        #   覆盖层仅替换对话区，LEFT/RIGHT/BOTTOM 不受影响、始终可见。
        #
        # CardContainer 停靠模式协议（enable_dock_mode）：
        #   展开动画结束 → 释放轴向 max、锁定最小尺寸，占比交给 splitter 拖拽；
        #   折叠 → 记忆占比、动画收 0 后 hide() 并显式归还空间给内容区；
        #   重开 → 恢复上次拖出的占比。
        from PyQt5.QtWidgets import QSplitter as _DockSplitter, QStackedWidget as _QStackedWidget

        # ── 覆盖层堆栈（QStackedWidget）：仅替换对话区，不覆盖 LEFT/RIGHT/BOTTOM ──
        # Page 0: 正常对话视图
        # Page 1: 系统卡片覆盖层（_global_top_container 内的全局卡片）
        self._content_stack = _QStackedWidget(self._chat_frame)
        self._content_stack.setObjectName("contentStack")
        self._content_stack.addWidget(self._content_area)  # index 0: 对话区

        # 覆盖层页面：包裹 _global_top_container，使其填满覆盖层空间
        self._global_overlay = QWidget(self._chat_frame)
        self._global_overlay.setObjectName("globalOverlay")
        _overlay_layout = QVBoxLayout(self._global_overlay)
        _overlay_layout.setContentsMargins(0, 0, 0, 0)
        _overlay_layout.setSpacing(0)
        _overlay_layout.addWidget(self._global_top_container)
        self._content_stack.addWidget(self._global_overlay)  # index 1: 覆盖层

        # 默认显示对话区
        self._content_stack.setCurrentIndex(0)

        # ── 对话内容限宽居中：_chat_frame 矩形边框保持全宽填满 splitter，
        #    仅内容区（_content_stack：消息列表+输入区+覆盖层）限宽居中。
        #    wrapper 作为 _dock_splitter 的中间窗格与左右 dock 容器并列：
        #    dock 卡片展开时从 wrapper 借空间、可占满边框全宽；
        #    wrapper 内部 content_stack 按 _MAX_CHAT_WIDTH 动态居中留白。 ──
        self._chat_wrapper = QWidget(self._chat_frame)
        self._chat_wrapper.setObjectName("chatWrapper")
        self._chat_wrapper_layout = QHBoxLayout(self._chat_wrapper)
        self._chat_wrapper_layout.setContentsMargins(0, 0, 0, 0)
        self._chat_wrapper_layout.setSpacing(0)
        self._chat_wrapper_layout.addWidget(self._content_stack, 1)
        self._chat_wrapper.installEventFilter(self)

        # dock_splitter: 左停靠区 | 内容区(wrapper) | 右停靠区
        self._dock_splitter = _DockSplitter(Qt.Horizontal, self._chat_frame)
        self._dock_splitter.setObjectName("dockSplitter")
        self._dock_splitter.addWidget(self._global_left_container)
        self._dock_splitter.addWidget(self._chat_wrapper)
        self._dock_splitter.addWidget(self._global_right_container)
        self._dock_splitter.setStretchFactor(0, 0)  # 左停靠区不随窗口拉伸
        self._dock_splitter.setStretchFactor(1, 1)  # 内容区(含覆盖层)吃掉多余空间
        self._dock_splitter.setStretchFactor(2, 0)  # 右停靠区不随窗口拉伸
        self._dock_splitter.setHandleWidth(6)
        # 折叠依赖轴向 max=0 约束而非用户拖拽收起，禁止拖拽塌陷
        self._dock_splitter.setChildrenCollapsible(False)

        self._vdock_splitter = _DockSplitter(Qt.Vertical, self._chat_frame)
        self._vdock_splitter.setObjectName("vDockSplitter")
        self._vdock_splitter.addWidget(self._dock_splitter)
        self._vdock_splitter.addWidget(self._global_bottom_container)
        self._vdock_splitter.setStretchFactor(0, 1)  # 内容区吃掉多余空间
        self._vdock_splitter.setStretchFactor(1, 0)  # 下停靠区不随窗口拉伸
        self._vdock_splitter.setHandleWidth(6)
        self._vdock_splitter.setChildrenCollapsible(False)
        chat_frame_layout.addWidget(self._vdock_splitter, 1)

        # 全局容器启用停靠模式
        self._global_left_container.enable_dock_mode(self._dock_splitter)
        self._global_right_container.enable_dock_mode(self._dock_splitter)
        # TOP 容器处于覆盖层模式，不启用 dock mode
        self._global_bottom_container.enable_dock_mode(self._vdock_splitter)

        # ── 覆盖层状态切换 ──
        self._global_top_container.overlayStateChanged.connect(self._on_overlay_state_changed)

        # 使用 QSplitter 让左侧面板可拖拽
        from PyQt5.QtWidgets import QSplitter

        self._splitter = QSplitter(Qt.Horizontal, content_widget)
        self._splitter.addWidget(self._tab_frame)
        # 矩形边框（_chat_frame）保持全宽填满 splitter 右侧；
        # 内部对话内容限宽居中逻辑在 chat_frame 内的 _chat_wrapper 中处理
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

        # 恢复侧边栏收起状态（必须在 splitter sizes 设置之后执行）
        QTimer.singleShot(0, self._restore_sidebar_collapsed)

        # 应用样式（使用 _apply_theme_stylesheet 以确保 objectName 选择器生效）
        self._apply_theme_stylesheet()

    def _setup_signals(self):
        self._tab_panel.tabSelected.connect(self._on_tab_selected)
        self._tab_panel.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_panel.tabBranchRequested.connect(self._on_tab_branch_requested)
        self._tab_panel.newTabRequested.connect(self._on_new_tab_requested)
        self._tab_panel.sidebarToggled.connect(self._on_sidebar_toggled)
        self._tab_panel.teamCloseRequested.connect(self._on_team_close_requested)
        self._tab_panel.teamAddMemberRequested.connect(self._on_team_add_member_requested)
        self._tab_panel.teamNewTaskRequested.connect(self._on_team_new_task_requested)
        # 用户手动拖拽 splitter 把手 → 折叠是用户主动，不标记挤压，
        # 关闭卡片/空间恢复时不得自动展开（尊重手动意图）
        if hasattr(self, "_splitter"):
            self._splitter.splitterMoved.connect(self._on_splitter_manually_moved)

    # ── 侧边栏收起/展开（宽度平滑动画） ──

    def _on_sidebar_toggled(self, collapsed: bool, animate: bool = True):
        """侧边栏收起/展开：通过 splitter 平滑动画调整面板宽度

        Args:
            collapsed: True=收起 False=展开
            animate: 是否走宽度动画（启动恢复配置时传 False 瞬时切换）
        """
        sizes = self._splitter.sizes()
        total_w = sum(sizes) if sizes else self.width()
        cur_w = sizes[0] if sizes else (self._tab_frame.width() if hasattr(self, "_tab_frame") else 250)

        if collapsed:
            # 记录挤压折叠基准窗口总宽：自动展开需窗口比此刻更宽（相对增长），
            # 避免"折叠瞬间绝对空间恰满足→立刻弹回"（见 _maybe_auto_expand_after_squeeze）
            self._squeeze_total_width = total_w
            # 收起前保存当前宽度（供展开时恢复）
            if sizes:
                cur_frame_w = sizes[0]
                # 挤压折叠场景：resizeEvent 自动折叠时宽度已被压到折叠阈值
                # （< _auto_collapse_width=100）以下，此刻保存的"当前宽度"只剩
                # 90px 左右，点击展开只能恢复窄条。此时改用常规展开宽度
                # （历史配置 tab_panel_width，默认 280）作为恢复目标，保证展开
                # 后宽度可读；用户手动折叠时宽度正常，照常保存实际宽度。
                if cur_frame_w < self._tab_panel._auto_collapse_width:
                    saved_w = Settings.get_instance().tab_panel_width.value or 280
                    cur_frame_w = max(saved_w, _EXPANDED_MIN_FRAME_WIDTH - 14) + 14
                self._saved_panel_frame_width = cur_frame_w
            # 使用 _tab_panel 的收起最小宽度
            target_w = self._tab_panel._collapsed_min_width + 14  # +12 margins +2 border
        else:
            # 展开：恢复折叠时保存的目标宽度（frame 宽度）。
            # 下限 _EXPANDED_MIN_FRAME_WIDTH：挤压折叠时保存的窄宽度已被折叠端
            # 修正为常规宽度，此处兜底保证展开后宽度不小于最小可读宽度。
            target_w = max(_EXPANDED_MIN_FRAME_WIDTH, getattr(self, "_saved_panel_frame_width", 250))
            # 拖拽把手拉开的场景（当前宽度已远超收起最小宽度，说明用户
            # 手动拖到位）：保持用户拖出的宽度，不覆盖为保存值。
            # 按钮点击展开时 cur_w == 收起宽度(60)，不满足此条件。
            if cur_w > self._tab_panel._collapsed_min_width + 14 + 10:
                target_w = cur_w
            elif cur_w >= target_w - 4:
                target_w = cur_w

        # 持久化收起状态
        Settings.get_instance().tab_panel_collapsed.value = collapsed

        if not animate:
            # 启动恢复等瞬时路径：直接 setSizes
            self._splitter.setSizes([target_w, max(0, total_w - target_w)])
            if not collapsed:
                new_sizes = self._splitter.sizes()
                if new_sizes:
                    Settings.get_instance().tab_panel_width.value = max(_EXPANDED_MIN_FRAME_WIDTH, new_sizes[0]) - 14
            return

        # 平滑动画过渡
        if abs(cur_w - target_w) < 3:
            # 距离过近（如拖拽已到位）：瞬时落位，省一次空动画
            self._splitter.setSizes([target_w, max(0, total_w - target_w)])
            if hasattr(self, "_tab_panel"):
                self._tab_panel.sync_collapsed_ui()
            if not collapsed:
                Settings.get_instance().tab_panel_width.value = max(_EXPANDED_MIN_FRAME_WIDTH, target_w) - 14
            return

        # 平滑动画过渡
        self._start_sidebar_anim(cur_w, target_w, collapsing=collapsed)

    def _start_sidebar_anim(self, start_w: int, end_w: int, collapsing: bool):
        """启动侧边栏宽度动画（200ms OutCubic）"""
        anim = self._sidebar_anim
        if anim is None:
            anim = QVariantAnimation(self)
            anim.setDuration(200)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.valueChanged.connect(self._on_sidebar_anim_value)
            anim.finished.connect(self._on_sidebar_anim_finished)
            self._sidebar_anim = anim
        # 动画期间抑制 TabPanel resizeEvent 自动展开（折叠途中宽度仍 > 阈值，
        # 若不抑制会在动画中途误触发"拖拽展开"打断动画）
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_animating(True)
        self._sidebar_anim_collapsing = collapsing
        self._sidebar_anim_ui_switched = False
        anim.stop()
        anim.setStartValue(float(start_w))
        anim.setEndValue(float(end_w))
        anim.start()

    def _on_sidebar_anim_value(self, value):
        """动画每帧：更新 splitter 宽度；跨阈值时切换 TabPanel 紧凑/展开 UI"""
        w = int(round(value))
        sizes = self._splitter.sizes()
        total_w = sum(sizes) if sizes else self.width()
        self._splitter.setSizes([w, max(0, total_w - w)])

        # 跨阈值切换 UI：收起时宽度 < 100 切紧凑；展开时宽度 >= 120 切完整。
        # 阈值取收起最小宽度(46)与展开最小宽度(120) 的中间值，避免展开时
        # 文字在极窄条中被挤压（先让宽度足够再显示文字）。
        if not self._sidebar_anim_ui_switched:
            if self._sidebar_anim_collapsing and w <= 100:
                self._sidebar_anim_ui_switched = True
                self._tab_panel.sync_collapsed_ui()
            elif not self._sidebar_anim_collapsing and w >= 120:
                self._sidebar_anim_ui_switched = True
                self._tab_panel.sync_collapsed_ui()

    def _on_sidebar_anim_finished(self):
        """动画结束：恢复自动展开能力 + 精确落位最终宽度 + 保存配置"""
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_animating(False)
        # 最终宽度精确落位（插值收尾可能差 1px）
        sizes = self._splitter.sizes()
        total_w = sum(sizes) if sizes else self.width()
        if self._sidebar_anim_collapsing:
            target_w = self._tab_panel._collapsed_min_width + 14
        else:
            target_w = getattr(self, "_saved_panel_frame_width", 250)
            target_w = max(_EXPANDED_MIN_FRAME_WIDTH, target_w)
        self._splitter.setSizes([target_w, max(0, total_w - target_w)])
        # UI 最终状态兜底（动画中途未跨阈值时强制同步，如目标宽度恰为阈值）
        if hasattr(self, "_tab_panel"):
            self._tab_panel.sync_collapsed_ui()
        # 展开时保存当前宽度（去除 frame 补偿）
        if not self._sidebar_anim_collapsing:
            new_sizes = self._splitter.sizes()
            if new_sizes:
                Settings.get_instance().tab_panel_width.value = max(_EXPANDED_MIN_FRAME_WIDTH, new_sizes[0]) - 14
        else:
            # 折叠动画结束：布局已稳定，若折叠是"挤压所致"（_collapsed_by_squeeze），
            # 补一次空间恢复检测——覆盖"折叠动画期间窗口已拉宽"的时序缺口
            # （resize 结束检测在动画中会被 _animating 跳过）。
            self._maybe_auto_expand_after_squeeze()

    def _maybe_auto_expand_after_squeeze(self, growth_required: bool = True):
        """挤压折叠后空间恢复：自动展开回常规宽度

        仅对"被外部挤压自动折叠"（_collapsed_by_squeeze=True）生效；
        用户手动折叠/拖拽折叠（标记已清除）不自动展开，尊重手动意图。

        触发点：窗口 resize 结束、overlay 卡片关闭、折叠动画结束。
        空间判定（两条件都满足才展开）：
        1. 相对增长（growth_required=True）：当前窗口总宽 ≥ 折叠时总宽 + 200。
           防止"折叠刚完成绝对条件恰满足就弹回"——窗口只缩窄到 900 左右折叠
           时，绝对空间（900-60 ≥ 展开宽+400）仍满足，若只看绝对条件会立刻
           弹回展开，折叠态无法保持。仅当窗口比折叠时明显更宽（有新增空间）
           才自动展开，语义即"再有剩余空间时自动展开"。
        2. 绝对下限：窗口总宽 - 折叠宽 ≥ 展开目标宽 + 聊天区最小可用宽(400)，
           展开后面板与聊天区都放得下。
        """
        if not hasattr(self, "_tab_panel"):
            return
        panel = self._tab_panel
        if not panel._collapsed or not panel._collapsed_by_squeeze:
            return
        if panel._animating:
            return  # 动画中，等下次触发
        total = sum(self._splitter.sizes()) if hasattr(self, "_splitter") else self.width()
        target_w = max(_EXPANDED_MIN_FRAME_WIDTH, getattr(self, "_saved_panel_frame_width", 250))
        chat_min = 400  # 聊天区最小可用宽度
        # 条件1：相对增长（仅窗口 resize 类触发需要；overlay 布局恢复传 False）
        if growth_required:
            base = getattr(self, "_squeeze_total_width", None)
            if base is not None and total < base + _AUTO_EXPAND_GROWTH:
                return  # 窗口未比折叠时更宽，不自动展开
        # 条件2：绝对下限
        if total - (self._tab_panel._collapsed_min_width + 14) < target_w + chat_min:
            return  # 空间不足，保持折叠
        # 空间足够：自动展开（先清标记防重入）
        panel._collapsed_by_squeeze = False
        panel.set_collapsed(False)
        self._saved_panel_frame_width = target_w
        self._on_sidebar_toggled(False)

    def _restore_sidebar_collapsed(self):
        """启动时根据配置恢复侧边栏收起状态"""
        collapsed = Settings.get_instance().tab_panel_collapsed.value
        if collapsed:
            self._on_sidebar_toggled(True, animate=False)
            # 让 TabPanel 内部状态同步（不重复发射信号）
            self._tab_panel.set_collapsed(True)
            return
        # 非折叠配置：显式恢复保存宽度。
        # 背景：_setup_ui 里 setSizes 在窗口未显示时调用，show 后首次 relayout
        # 按 stretch/sizeHint 重新分配，左面板会被压到最小宽度（< _auto_collapse_width，
        # 实测 46~60px），TabPanel.resizeEvent 误判为"用户拖窄"自动折叠；
        # 欢迎卡片懒渲染（QWebEngineView 创建）还会引发后续 relayout 再次压缩。
        # 因此在启动早期多轮补射恢复（时间递增，覆盖 2~3 次 relayout 窗口期，
        # 直到布局不再弹跳），期间均以保存宽度为准。
        if not hasattr(self, "_splitter"):
            return
        self._apply_restored_panel_width()
        for delay in (80, 200, 400, 700):
            QTimer.singleShot(delay, self._apply_restored_panel_width)

    def _apply_restored_panel_width(self):
        """按保存宽度恢复左面板宽度 + 解除启动误折叠（展开配置的启动兜底）"""
        if not hasattr(self, "_splitter") or self._splitter.count() == 0:
            return
        saved_w = Settings.get_instance().tab_panel_width.value or 280
        frame_w = max(_EXPANDED_MIN_FRAME_WIDTH, saved_w + 14)
        sizes = self._splitter.sizes()
        total = sum(sizes) if sizes else self.width()
        if total <= frame_w:
            return
        # 仅当前宽度明显小于保存宽度时才恢复（避免覆盖用户手动拖宽）
        if sizes and sizes[0] >= frame_w - 10:
            return
        frame_w = min(frame_w, total)
        self._splitter.setSizes([frame_w, max(0, total - frame_w)])
        # 启动时 TabPanel 可能已被 relayout 压窄误触发折叠（_collapsed=True），
        # 这里显式解除，并同步紧凑/展开 UI（不发射信号，避免与动画互打断）
        if self._tab_panel._collapsed:
            self._tab_panel.set_collapsed(False)
        self._tab_panel.sync_collapsed_ui()

    # ── 覆盖层状态切换 ──

    def _on_overlay_state_changed(self, has_visible: bool):
        """系统卡片覆盖层显隐切换：QStackedWidget 页面 0←→1

        当 _global_top_container 报告有可见卡片时，切换到覆盖层页面（index 1），
        隐藏对话区、仅显示系统卡片；全部卡片关闭后切回对话区（index 0）。

        卡片打开时覆盖层页面可能引发布局 relayout 挤压左面板（宽度被压到
        < _auto_collapse_width），触发 TabPanel 自动折叠（_collapsed_by_squeeze=True）。
        卡片关闭后空间恢复：若折叠确为挤压所致（非用户手动），且可用宽度足够，
        则自动展开回常规宽度，避免折叠态残留。
        """
        if has_visible:
            self._content_stack.setCurrentIndex(1)
        else:
            self._content_stack.setCurrentIndex(0)
            # 卡片关闭 → 布局恢复：延迟到下一事件循环（等 relayout 完成）再检测。
            # growth_required=False：overlay 挤压时窗口总宽未变，不适用相对增长
            # 条件（否则窗口没变宽永远不会自动展开），此处仅按绝对空间下限判断。
            QTimer.singleShot(0, lambda: self._maybe_auto_expand_after_squeeze(growth_required=False))

    def _on_splitter_manually_moved(self, pos: int, index: int):
        """用户手动拖拽 splitter 把手：清除挤压折叠标记

        挤压折叠标记（_collapsed_by_squeeze）仅用于"被外部 relayout 压窄
        自动折叠后自动展开"场景。用户手动拖动把手瘦身到折叠阈值以下，
        视为手动意图——不自动展开，避免关闭卡片/窗口拉宽时把用户拖窄的
        面板再撑开。
        """
        if index == 0 and hasattr(self, "_tab_panel"):
            self._tab_panel._collapsed_by_squeeze = False

    # ── 窗口管理 ──

    def add_window(self, window) -> int:
        """添加窗口到 Tab 管理器，返回索引"""
        existing = self._window_to_index.get(id(window), -1)
        if existing >= 0 and existing < len(self._windows):
            return existing

        self._windows.append(window)
        idx = len(self._windows) - 1  # 0-based
        self._window_to_index[id(window)] = idx

        # ── P0-2 性能优化：冻结更新，批量完成 addWidget + addTab + 激活 ──
        # setCurrentWidget 会触发新窗口 showEvent 与整棵子控件树的布局/绘制，
        # 冻结期间抑制逐控件绘制，恢复后一次性 polish + updateGeometry，
        # 把绘制/样式传播开销压缩到单次重绘。
        # 信号时序不变：set_active_index → tabSelected → setCurrentWidget 仍同步，
        # 恢复更新后才发射 tabCountChanged。
        stack = self._content_area
        panel = self._tab_panel
        stack.setUpdatesEnabled(False)
        panel.setUpdatesEnabled(False)
        window.setUpdatesEnabled(False)
        try:
            # 添加到 QStackedWidget（从索引 1 开始，0 是空状态页）
            stack.addWidget(window)

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

            tab_idx = panel.add_tab(
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
                # 团队模式：Tab 标题保持会话标题，角色名只进胶囊
                team_agent = getattr(_win, "_team_agent_name", "") or ""
                if team_agent:
                    # 团队窗口：保持会话标题（不采用窗口标题，避免被角色名覆盖）
                    t = (
                        _get_window_session_title(_win)
                        or _win.windowTitle()
                        or getattr(_win, "_current_project", None)
                        or "对话"
                    )
                    self._tab_panel.update_tab_title(cur_idx, t)
                    self._tab_panel.update_tab_capsule(cur_idx, team_agent)
                else:
                    # 非团队窗口：Tab 标题取窗口标题
                    t = _win.windowTitle() or getattr(_win, "_current_project", None) or "对话"
                    self._tab_panel.update_tab_title(cur_idx, t)
                    self._tab_panel.clear_tab_capsule(cur_idx)
                # 更新项目图标
                p = getattr(_win, "_current_project", None) or ""
                _update_tab_icon(cur_idx, p)
                # 如果该窗口是当前选中 Tab，同步宿主窗口标题
                if self._tab_panel.active_index == cur_idx:
                    self._sync_window_title()

            # ★ 泄漏修复（P0）：保存闭包引用到窗口属性，供 _close_window_at 显式断开。
            # 闭包通过 __defaults__（_win=window）持有窗口引用，C++ 对象销毁后
            # Qt 虽自动断开信号，但 PyQt 对 Python slot 的释放滞后，wrapper 残留
            # 导致窗口对象树无法回收（T5 诊断第⑦条引用链：信号表自环）。
            window._tab_title_changed_slot = _on_win_title_changed
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

            window._tab_ai_state_slot = _on_ai_state_changed
            window.ai_state_changed.connect(_on_ai_state_changed)

            # 立即触发一次初始图标更新 + 团队胶囊状态同步
            logger.info(f"[TabMode] 初始图标: project={project!r}, tab_idx={tab_idx}")
            _update_tab_icon(tab_idx, project)
            team_agent = getattr(window, "_team_agent_name", "") or ""
            if team_agent:
                panel.update_tab_capsule(tab_idx, team_agent)
            # 同步初始团队分组（窗口加入 Tab 时可能已是团队成员）
            # 分组 key 用团队运行标识（_team_run_id，方案 A：同一次 /team --load
            # 的所有成员共享同一 run_id），同团队多窗口圈进同一容器；同一模板
            # 多次加载产生不同 run_id，不再混组。老窗口无 _team_run_id 时回落
            # 团队名（_team_name，模板名）；非团队窗口传 "" 留在独立区。
            # 胶囊仍显示角色名（team_agent）。
            team_id = self._resolve_tab_team_id(window)
            panel.set_tab_team(tab_idx, team_id)
            # 同步团队框 header 名称（初次加入 Tab 时即同步）
            if team_id:
                panel.set_team_label(team_id, getattr(window, "_team_name", "") or "")
                # 团队模式：隐藏 Tab 项目 icon（项目 icon 移到团队标题处）
                panel.set_tab_team_mode(tab_idx, True)
                # 团队标题 icon（团队级 project；团队框此时已创建，重新走
                # _update_tab_icon 团队分支刷新 header）
                _update_tab_icon(tab_idx, project)
            else:
                # 非团队窗口：Tab 保持显示项目 icon（回归不变）
                panel.set_tab_team_mode(tab_idx, False)

            # 隐藏空状态页，切换到新窗口
            stack.widget(0).hide()
            panel.set_active_index(idx)
        finally:
            window.setUpdatesEnabled(True)
            panel.setUpdatesEnabled(True)
            stack.setUpdatesEnabled(True)
            # 恢复更新后一次性重算布局/绘制
            stack.updateGeometry()
            panel.updateGeometry()
            window.update()

        self.tabCountChanged.emit(len(self._windows))
        return idx

    def refresh_capsule_for_window(self, window):
        """主动刷新指定窗口的 Tab 胶囊（基于其 _team_agent_name）。

        用途：窗口加入/离开团队时立即同步胶囊状态，不依赖 windowTitleChanged
        信号触发（Qt 在标题未变时不发射该信号，导致新建空白窗口加入团队后胶囊不显示）。

        同时同步团队分组（set_tab_team）：胶囊与分组框共同表达团队归属，
        胶囊显示角色名，分组框圈出同团队多个窗口。

        同时同步团队框 header 名称（set_team_label）：窗口加入团队时把
        窗口的 _team_name 注入团队框 header，离开团队时清空（set_tab_team
        移除分组会自然触发 _maybe_remove_empty_group 清理）。
        """
        idx = self._window_to_index.get(id(window), -1)
        if idx < 0 or idx >= len(self._windows):
            return
        team_agent = getattr(window, "_team_agent_name", "") or ""
        if team_agent:
            self._tab_panel.update_tab_capsule(idx, team_agent)
        else:
            self._tab_panel.clear_tab_capsule(idx)
        # 同步团队分组：分组 key 用团队运行标识（_team_run_id），同一模板多次
        # 加载的多个团队（不同 run_id）不再混组；老窗口无 run_id 回落团队名。
        # 非团队窗口传 "" 留在独立区。胶囊仍显示角色名（team_agent）。
        team_id = self._resolve_tab_team_id(window)
        self._tab_panel.set_tab_team(idx, team_id)
        # 团队模式切换：团队窗口隐藏 Tab 项目 icon（移到团队标题处显示）；
        # 退出团队时恢复显示（检查点 2：清胶囊与恢复 icon 同时处理，勿漏）
        self._tab_panel.set_tab_team_mode(idx, bool(team_id))
        # 同步团队框 header 名称 + 项目 icon（数据源=团队级 project）
        if team_id:
            self._tab_panel.set_team_label(team_id, getattr(window, "_team_name", "") or "")
            project = getattr(window, "_current_project", "") or ""
            self._tab_panel.set_team_project(team_id, *self._team_project_icon_data(window, fallback=project))
        else:
            # 退出团队：恢复 Tab 项目 icon 并刷新内容（团队模式期间 _update_tab_icon
            # 团队分支只刷 header、直接 return，TabItem 自身 icon 数据停留在加入团队
            # 时的初值——review #11 问题 2：退出团队后 Tab 图标过时）
            project = getattr(window, "_current_project", "") or ""
            _update_tab_icon(idx, project)

    @staticmethod
    def _resolve_tab_team_id(window) -> str:
        """计算窗口的 Tab 团队分组 key

        方案 A：分组 key 优先用团队运行标识（_team_run_id）——同一次
        /team --load 的所有成员共享同一 run_id，同组；同一模板多次加载
        产生不同 run_id，不再混组。老窗口（无 _team_run_id）回落团队名
        （_team_name，模板名）兼容；非团队窗口返回 "" 留在独立区。
        """
        if not getattr(window, "_team_agent_name", ""):
            return ""
        run_id = getattr(window, "_team_run_id", "") or ""
        if run_id:
            return run_id
        return getattr(window, "_team_name", "") or "default"

    @staticmethod
    def _team_project_icon_data(window, fallback: str = "") -> tuple:
        """团队框 header 项目 icon 数据（缩写, 颜色）

        数据源**必须为团队级 project**（TeamManager.get_team_project）：
        多个成员窗口共享同一个团队框 header，读任一窗口自身项目会导致
        展示不一致（review 检查点 1）。

        团队级 project 为空（团队尚未统一设置项目）时：回退 fallback 参数
        （调用方传入的"正在切换的目标项目"——广播后团队级即写入，两者一致）；
        fallback 也为空则返回 ("", "")（header 不显示 icon）。
        """
        try:
            from app.core.team_manager import TeamManager

            project = TeamManager.get_instance().get_team_project()
            if not project:
                project = fallback
            if not project:
                return ("", "")
            from app.widgets.cards.settings.project_selector_card import (
                extract_project_initials,
                get_project_color,
            )

            return (extract_project_initials(project), get_project_color(project, alpha=255))
        except Exception:
            return ("", "")

    def remove_window(self, window):
        """从 Tab 管理器移除窗口（外部 API：按 window 对象定位）"""
        idx = self._window_to_index.get(id(window), -1)
        if idx < 0 or idx >= len(self._windows):
            return
        self._close_window_at(idx)

    def _close_window_at(self, idx: int):
        """按索引统一关闭窗口：从 _windows 弹出 + removeWidget + remove_tab + close

        被 3 处调用以消除重复清理逻辑：
        - remove_window（外部按 window 对象定位后调用）
        - _on_tab_close_requested（标签关闭按钮）
        - _on_team_close_requested（团队关闭按钮的窗口清理）

        索引越界防御：调用方应保证 idx 有效，此处仍做防御性 return。

        Args:
            idx: 窗口在 self._windows 中的索引
        """
        if not (0 <= idx < len(self._windows)):
            return
        window = self._windows[idx]
        # 先从列表中移除，避免后续操作访问到已销毁的窗口
        self._windows.pop(idx)
        self._window_to_index.pop(id(window), None)
        # 被移除窗口之后的所有窗口索引减 1
        for j in range(idx, len(self._windows)):
            self._window_to_index[id(self._windows[j])] = j
        # 从 QStackedWidget 移除（异常不影响清理流程）
        try:
            self._content_area.removeWidget(window)
        except Exception:
            pass
        # 从 Tab 面板移除
        try:
            self._tab_panel.remove_tab(idx)
        except Exception:
            pass
        # ★ 泄漏修复（P0）：显式断开 add_window 连接的信号闭包。
        # 闭包 __defaults__（_win=window）持有窗口引用，PyQt 对 C++ 对象销毁后
        # 自动断开的 Python slot 释放滞后，窗口 Python wrapper 残留导致整树泄漏。
        # 须在 close() 之前断开（此时 C++ 对象仍存活，disconnect 安全）。
        for _slot_attr, _signal in (
            ("_tab_title_changed_slot", "windowTitleChanged"),
            ("_tab_ai_state_slot", "ai_state_changed"),
        ):
            _slot = getattr(window, _slot_attr, None)
            if _slot is not None:
                try:
                    getattr(window, _signal).disconnect(_slot)
                except Exception:
                    pass
                try:
                    setattr(window, _slot_attr, None)
                except Exception:
                    pass
        # 调用窗口的关闭逻辑（自动保存会话）
        try:
            window.close()
        except Exception as e:
            logger.error(f"[TabManager] 关闭窗口失败: {e}")
        # ★ 内存泄漏修复（P0）：显式断开 Qt parent 链 + 排队删除。
        # QStackedWidget.removeWidget 不会解除 parent（窗口仍挂在
        # _content_area 下），close() 仅隐藏不销毁（无 WA_DeleteOnClose）；
        # 不 deleteLater 则 C++ 对象树存活至 Python GC 回收 wrapper 才析构，
        # 反复开关 tab 时窗口整树被多根持有（实测 offscreen 每 tab ~11.6MB）。
        # setParent(None) 断开对象树 + deleteLater 让 Qt 下一轮事件循环即回收。
        try:
            window.setParent(None)
            window.deleteLater()
        except Exception as e:
            logger.error(f"[TabManager] 释放窗口对象树失败: {e}")
        # 如果所有窗口都被移除，显示空状态页
        if not self._windows:
            try:
                self._content_area.widget(0).show()
            except Exception:
                pass
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
            # 团队窗口：宿主窗口标题保持会话标题（角色名只进胶囊），避免被角色名污染
            team_agent = getattr(win, "_team_agent_name", "") or ""
            if team_agent:
                t = _get_window_session_title(win) or win.windowTitle()
            else:
                t = win.windowTitle()
            if t:
                self.setWindowTitle(t)
                return
        self.setWindowTitle("飘狐-DriFox")

    def _on_tab_selected(self, index: int):
        if 0 <= index < len(self._windows):
            win = self._windows[index]
            self._content_area.setCurrentWidget(win)
            self.activeTabChanged.emit(index)
            # 切换 tab 时同步宿主窗口标题
            self._sync_window_title()
            # 补刷新延迟的主题变更（Tab 模式下主题刷新跳过非可见窗口）
            if getattr(win, "_theme_needs_refresh", False):
                try:
                    win._theme_needs_refresh = False
                    from app.main_widget import OpenAIChatToolWindow

                    # P5b（V2 方案 A）：按置位时记录的 scope 精确补刷——
                    # theme→只刷颜色、font_family→只刷字体、font_size→只刷字号、
                    # None→全量刷新。scope 取自 _theme_needs_refresh_scope
                    # （batched 路径记录 final_scope，dispatch_refresh 路径记录
                    # "theme"），补刷后清空避免残留。
                    scope = getattr(win, "_theme_needs_refresh_scope", None) or None
                    win._theme_needs_refresh_scope = None
                    win._apply_runtime_ui_settings(scope=scope, _skip_global=True)
                except Exception:
                    pass

    def _on_tab_close_requested(self, index: int):
        """标签关闭按钮回调：按索引关闭单个窗口"""
        self._close_window_at(index)

    def _on_team_close_requested(self, team_id: str):
        """团队关闭按钮回调：解散整个团队（匹配 team_id 的所有窗口都退出）

        复用 _handle_team_leave 的 7 步副作用逻辑（silent=True 避免重复弹 InfoBar）：
        1) 停 watcher 2) tm.leave_team 3) restore_user 工具权限
        4) 清 _team_* 标记 5) 刷新团队 UI 6) 同步活跃窗口
        7) 还原默认智能体身份

        🛡️ W3a（W2 联调审查问题 4）：降级路径（窗口无 _handle_team_leave）同样
        先停 watcher 再 leave_team（见循环内 else 分支），与主路径行为对齐，
        消除与 U3/F3 后台 rmtree 的竞态窗口。

        🛡️ U2 批量解散优化：循环内改走 _handle_team_leave 轻量路径
        （batch_disband=True，跳过每窗全局同步 + 欢迎卡片 QWebEngineView 重建），
        循环结束后统一执行 1 次 _sync_active_windows_to_team_manager——
        全局同步由原 2n 次降为 1 次；另以 begin_batch_remove/end_batch_remove
        包裹（U1 契约，未合入时 hasattr 降级）避免逐窗 O(N²) 布局重建。
        解散单个窗口（非本方法场景）行为与原先完全一致。

        索引处理：先收集匹配窗口的索引列表（降序排序），倒序遍历避免 pop 后索引漂移。

        Args:
            team_id: Tab 分组 key（优先 _team_run_id，回退 _team_name/"default"）
        """
        if not team_id:
            return

        # 收集匹配 team_id 的窗口索引（按 _resolve_tab_team_id 同样优先级）
        matching_indices = []
        for idx, win in enumerate(self._windows):
            try:
                if self._resolve_tab_team_id(win) == team_id:
                    matching_indices.append(idx)
            except Exception:
                continue

        if not matching_indices:
            return

        # 🛡️ U2 批量解散：调用 U1 的批量删除接口（契约：
        # begin_batch_remove/end_batch_remove 成对包裹，期间 remove_tab 跳过
        # _rebuild_team_layout，end 时统一重建一次，避免逐窗 O(N²) 布局重建）。
        # U1 尚未合入时 hasattr 守卫自动降级为原逐次重建行为，联调时统一验证。
        try:
            if hasattr(self._tab_panel, "begin_batch_remove"):
                self._tab_panel.begin_batch_remove()
        except Exception:
            pass

        try:
            # 倒序遍历：先关高索引再关低索引，避免 pop 后索引漂移
            for idx in sorted(matching_indices, reverse=True):
                if idx >= len(self._windows):
                    continue
                window = self._windows[idx]
                # 调用窗口的 _handle_team_leave（silent=True 不弹 InfoBar；
                # batch_disband=True 走轻量路径：跳过每窗全局同步与欢迎卡片
                # 重建，全局同步由循环结束后统一执行 1 次）；
                # 不存在时（老窗口/非主窗口）走最小可降级路径：仅调 tm.leave_team
                try:
                    if hasattr(window, "_handle_team_leave") and callable(window._handle_team_leave):
                        window._handle_team_leave(silent=True, batch_disband=True)
                    else:
                        from app.core.team_manager import TeamManager

                        # 🛡️ W3a（W2 联调审查问题 4）：降级路径（无 _handle_team_leave
                        # 的老窗口/非主窗口）在 leave_team 前必须先停 team watcher——
                        # 与主路径 _handle_team_leave（main_widget.py:4089 先
                        # _stop_team_watcher）行为对齐，消除与 U3/F3 后台 rmtree 的
                        # 竞态窗口（watcher 仍监视邮箱目录时被 rmtree 删除会触发
                        # FindNextChangeNotification 报错）。hasattr 守卫与现有
                        # 风格一致（main_widget.py:7486），无该方法时保持原行为。
                        stop_watcher = getattr(window, "_stop_team_watcher", None)
                        if callable(stop_watcher):
                            try:
                                stop_watcher()
                            except Exception:
                                pass
                        wid = getattr(window, "_window_id", "")
                        if wid:
                            # 🛡️ W3b-2：批量解散循环内挂起落盘（save_now=False），
                            # 由循环后 flush_pending_saves 统一写盘 1 次——
                            # 与主路径 _handle_team_leave 的批量语义对齐。
                            TeamManager.get_instance().leave_team(wid, save_now=False)
                except Exception as e:
                    logger.error(f"[TabManager] 退出团队失败: {e}")

                # 关闭窗口（统一走 _close_window_at）
                try:
                    self._close_window_at(idx)
                except Exception as e:
                    logger.error(f"[TabManager] 关闭团队窗口失败: {e}")
        finally:
            try:
                if hasattr(self._tab_panel, "end_batch_remove"):
                    self._tab_panel.end_batch_remove()
            except Exception:
                pass

        # 🛡️ W3b-2：批量解散循环后统一落盘挂起的团队数据（1 次写盘替代 N 次）。
        # 主路径 _handle_team_leave / 降级路径 leave_team 在批量模式下均传
        # save_now=False 挂起（team_manager._save_team_data 仅标记 pending），
        # 此处 flush 一次原子写盘。flush 是优化项，失败静默不破坏解散流程。
        try:
            from app.core.team_manager import TeamManager

            TeamManager.get_instance().flush_pending_saves()
        except Exception:
            pass

        # 🛡️ A1-4（P5b 收尾）+ F4 异步化：批量解散循环后统一处理 DeferredDelete
        # 事件，加速窗口对象树回收（_close_window_at 中对窗口调用的 deleteLater
        # 在事件循环空闲时才销毁）。
        # F4：同步排空会把整批窗口树析构拉回主线程阻塞解散路径（perf-analyzer
        # A2 实测 ~0.23s/3窗，占解散耗时 31%），故改为 QTimer.singleShot(0)
        # 异步后置——下个事件循环周期执行排空（≈立即，但不在当前调用栈内
        # 阻塞），保留"加速回收"意图，消除主路径同步等待析构。QTimer 已在
        # 文件头部导入，此处复用。lambda 经 try/except 包裹，不引用已销毁对象。
        try:
            from PyQt5.QtCore import QCoreApplication, QEvent

            QTimer.singleShot(0, lambda: QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete))
        except Exception:
            pass

        # 🛡️ U2 批量解散收尾：统一全局同步 1 次（替代原每成员 2 次：
        # _handle_team_leave 第 4 步 1 次 + 各窗口 closeEvent 1 次）。
        # 已关闭窗口已从 _instances 移除且 _is_destroyed=True，取任一存活
        # 窗口实例执行即可覆盖全量活跃集；无存活窗口时无需同步。
        try:
            from app.main_widget import OpenAIChatToolWindow

            for inst in list(OpenAIChatToolWindow._instances):
                if not getattr(inst, "_is_destroyed", False) and callable(
                    getattr(inst, "_sync_active_windows_to_team_manager", None)
                ):
                    inst._sync_active_windows_to_team_manager()
                    break
        except Exception as e:
            logger.error(f"[TabManager] 解散后同步活跃窗口失败: {e}")

    def _on_team_add_member_requested(self, team_id: str):
        """团队框"快速新建成员"按钮回调：为当前团队新建成员会话（可重复角色，F14）

        定位该 team_id 的任一窗口作为参照窗口（读团队上下文），
        委托其 _handle_team_add_member 执行交互与创建：
        - 菜单列出模板角色 ∪ 当前成员角色（全部可点、不去重）
        - 选择后创建该角色成员会话（并入当前 run_id）
        - 无模板且无成员时由 _handle_team_add_member 内弹 InfoBar 提示

        Args:
            team_id: Tab 分组 key（与 _resolve_tab_team_id 同优先级：
                _team_run_id 优先，回落 _team_name/"default"）
        """
        if not team_id:
            return

        # 收集匹配 team_id 的窗口（与 _resolve_tab_team_id 同优先级）
        ref_win = None
        for win in self._windows:
            try:
                if self._resolve_tab_team_id(win) == team_id:
                    ref_win = win
                    break
            except Exception:
                continue

        if ref_win is None:
            logger.warning(f"[TabManager] 新建成员：未找到 team_id={team_id} 的窗口")
            return

        # 委托窗口的交互方法（main_widget.OpenAIChatToolWindow._handle_team_add_member）
        handler = getattr(ref_win, "_handle_team_add_member", None)
        if handler is None or not callable(handler):
            logger.warning("[TabManager] 新建成员：窗口缺少 _handle_team_add_member 处理器")
            return
        try:
            handler()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[TabManager] 新建成员失败: {e}")

    def _on_team_new_task_requested(self, team_id: str):
        """团队框"新建任务"按钮回调：全员内部新建会话 + 生成新 run_id（F14）

        定位该 team_id 的任一窗口作为参照窗口（读团队上下文），
        委托其 _handle_team_new_task 执行：
        - 收集团队全部成员窗口（TeamManager.get_members）
        - 每个窗口内部新建会话（保存旧历史到旧 run_id）
        - start_team_run(force=True) 生成新 run_id 并更新所有成员窗口
        - 刷新 Tab 分组（窗口移入新 run_id 团队框）

        Args:
            team_id: Tab 分组 key（与 _resolve_tab_team_id 同优先级：
                _team_run_id 优先，回落 _team_name/"default"）
        """
        if not team_id:
            return

        # 收集匹配 team_id 的窗口（与 _resolve_tab_team_id 同优先级）
        ref_win = None
        for win in self._windows:
            try:
                if self._resolve_tab_team_id(win) == team_id:
                    ref_win = win
                    break
            except Exception:
                continue

        if ref_win is None:
            logger.warning(f"[TabManager] 新建任务：未找到 team_id={team_id} 的窗口")
            return

        # 委托窗口的交互方法（main_widget.OpenAIChatToolWindow._handle_team_new_task）
        handler = getattr(ref_win, "_handle_team_new_task", None)
        if handler is None or not callable(handler):
            logger.warning("[TabManager] 新建任务：窗口缺少 _handle_team_new_task 处理器")
            return
        try:
            handler()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[TabManager] 新建任务失败: {e}")

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

    # ── 几何持久化（简化版）──

    def _save_geometry(self):
        """防抖：记录位置/尺寸，等拖拽/缩放结束后 200ms 再写盘

        ★ 拖拽/缩放模态循环期间直接跳过：否则鼠标中途停顿 >200ms 时
        _do_save_geometry 会在拖拽帧里触发（遍历 splitter + 写配置项）
        → 偶发掉帧。循环结束后由 _on_window_drag_end / EXITSIZEMOVE
        分支统一补一次。
        """
        from app.utils.window_drag_state import any_window_dragging

        if any_window_dragging:
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
        # 收起状态下不覆盖已保存的正常宽度
        if hasattr(self, "_splitter") and hasattr(self, "_tab_panel"):
            if not self._tab_panel._collapsed:
                sizes = self._splitter.sizes()
                if sizes:
                    Settings.get_instance().tab_panel_width.value = max(_EXPANDED_MIN_FRAME_WIDTH, sizes[0]) - 14

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
        # 对话区限宽居中：首次显示同步 wrapper margins（Resize 事件链可能晚到）
        if hasattr(self, "_chat_wrapper"):
            try:
                self._sync_chat_wrapper_width()
            except Exception:
                pass
        # ★ T3 修复：窗口重新显示时补刷 UI 插件列表。
        # 根因：插件热加载发生在窗口隐藏期间时，刷新被可见性门控跳过
        # （main_widget 仅对可见的 TabManagerWindow 刷新），showEvent 无补刷
        # → 列表停留在旧状态（全关重开才恢复）。重新显示时补刷一次。
        if self._tab_panel is not None:
            self._tab_panel.refresh_ui_plugins()

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
                        from app.utils.window_drag_state import any_window_dragging
                        from app.utils.drag_stall_profiler import drag_profiler

                        any_window_dragging = False
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

        利用 any_window_dragging 全局标志（原 ToolPopupDialog._any_window_dragging
        类变量迁移而来，见 app/utils/window_drag_state.py），
        使 Tab 模式下的嵌入窗口（main_widget/bottom_input_area/card_container）
        跳过耗时的布局重算和高度调整，与多窗口模式享受同等的拖拽性能优化。

        ★ 不再 setUpdatesEnabled(False)：纯移动窗口时 DWM 直接在新位置合成，
        客户区根本不需要重绘，禁用绘制毫无收益；而重新启用时 Qt 会把整棵
        控件树标脏 → 松手瞬间全窗口（含所有 MessageCard）一次性全量重绘，
        这正是"松手卡一下"的根因。缩放路径仍由 resizeEvent 的 blocking
        机制独立管理（缩放确实需要冻结绘制）。
        """
        from app.utils.window_drag_state import any_window_dragging
        from app.utils.drag_stall_profiler import drag_profiler

        any_window_dragging = True
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_resizing(True)  # 暂停动画定时器
        # 诊断：拖拽期间抓取主线程阻塞现场（定位偶发卡顿元凶）
        drag_profiler.start()

    def _on_window_drag_end(self):
        """窗口拖拽结束：恢复动画定时器 + 解除节流 + 触发一次几何保存

        无 setUpdatesEnabled(True) → 无积压重绘炸弹，松手零代价。
        """
        from app.utils.window_drag_state import any_window_dragging
        from app.utils.drag_stall_profiler import drag_profiler

        any_window_dragging = False
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_resizing(False)  # 如有流式则恢复动画
        # 拖拽期间 moveEvent 跳过了几何保存防抖，此处统一补一次（200ms 后落盘）
        self._save_geometry()
        # 诊断：延迟 1.5s 停止采样，覆盖"松手瞬间卡顿"窗口期
        drag_profiler.stop_deferred()

    def changeEvent(self, event):
        """标准系统窗口自带最大化/还原处理，无需自定义逻辑"""
        super().changeEvent(event)

    def eventFilter(self, obj, event):
        """对话区 wrapper Resize → 动态调整左右 margins 实现限宽居中

        窗口超宽时：wrapper 宽度 > _MAX_CHAT_WIDTH，左右各留
        (wrapper_w - _MAX_CHAT_WIDTH)/2 的空白，chat_frame 精确居中；
        窗口不足该宽度时 margins 归零，chat_frame 占满 wrapper。
        """
        if obj is self._chat_wrapper and event.type() == QEvent.Resize:
            try:
                self._sync_chat_wrapper_width()
            except Exception:
                pass
        return super().eventFilter(obj, event)

    def _sync_chat_wrapper_width(self):
        """按当前 wrapper 宽度设置左右 margins，实现对话区限宽居中"""
        wrapper = self._chat_wrapper
        w = wrapper.width()
        if w <= 0:
            return
        pad = max(0, (w - _MAX_CHAT_WIDTH) // 2)
        layout = self._chat_wrapper_layout
        if layout.contentsMargins().left() != pad:
            layout.setContentsMargins(pad, 0, pad, 0)
            # 立即重算：resize 节流期间布局事件链可能延迟，主动失效+激活
            # 确保 chat_frame 几何同步到最新 margins（否则停留在旧宽度）
            layout.invalidate()
            layout.activate()

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
        # 窗口 resize 结束：若左面板因挤压已自动折叠且空间已恢复，自动展开
        self._maybe_auto_expand_after_squeeze()

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
            # 背景图尺寸跟随（轻量操作，不触发布局）
            if hasattr(self, "_bg_label") and self._bg_label is not None:
                self._bg_label.resize(self.size())
            return

        # ── 阶段一：首个 resize 事件，正常布局 + 初始化 blocking ──
        super().resizeEvent(event)
        # 背景图尺寸跟随（轻量操作，不触发布局）
        if hasattr(self, "_bg_label") and self._bg_label is not None:
            self._bg_label.resize(self.size())
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
        self.close()
