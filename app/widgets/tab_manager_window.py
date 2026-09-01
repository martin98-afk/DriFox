# -*- coding: utf-8 -*-
"""
TabManagerWindow — Tab 管理器宿主窗口（无边框现代化窗口）

左侧 TabPanel + 右侧 QStackedWidget 嵌入 OpenAIChatToolWindow 实例。
基于 qframelesswindow.FramelessWindow：自绘标题栏（CustomTitleBar），
Windows 原生保留 Aero Snap / 摇动 / 任务栏预览 / DWM 阴影。
"""

import platform
import sys
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

from PyQt5 import sip as _sip

from loguru import logger
from app.core import window_registry
from PyQt5.QtCore import QEasingCurve, QEvent, Qt, QPoint, QTimer, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from qframelesswindow import FramelessWindow

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css, get_unified_font

# ── 对话区最大宽度（px）：窗口过宽时限制对话区宽度并居中，两侧留白 ──
# 避免聊天内容在超宽屏幕上被拉得难以阅读。窗口宽度不足该值时对话区自动占满。
_MAX_CHAT_WIDTH = 1000

# ── 覆盖层「配置类」卡片最大宽度（px）──
# 比对话区稍宽，同样在超宽窗口下限宽居中，避免系统配置卡片无限拉伸。
# 仅作用于配置类卡片（系统设置/服务商/Hook/MCP）；差异对比、子智能体
# 会话等内容型卡片不限宽，铺满对话区。
_MAX_OVERLAY_WIDTH = _MAX_CHAT_WIDTH + 100
_CONFIG_REPLACE_CARDS = frozenset({"settings", "provider_edit", "hook_edit", "mcp_edit"})

# ── 标题栏 tab：内置「聊天」常驻 tab + 已知「内置替换」全局卡片 ──
# full 容器卡片（UI 插件 full 卡 + 下列内置全局卡）打开时在标题栏 tab 区显示
# （非常驻可关闭，× 点击真实隐藏卡片）；「聊天」为常驻 tab，active 时表示对话视图。
CHAT_TAB_ID = "chat"
KNOWN_GLOBAL_REPLACE_CARDS = frozenset(
    {
        "settings",
        "provider_edit",
        "hook_edit",
        "mcp_edit",
        "diff_viewer",
        "sub_agent_session",
        "file_undo",
        "chart_viewer",
    }
)
GLOBAL_REPLACE_TITLES = {
    "settings": "系统设置",
    "provider_edit": "服务商编辑",
    "hook_edit": "Hook 编辑",
    "mcp_edit": "MCP 编辑",
    "diff_viewer": "文件差异对比",
    "sub_agent_session": "子智能体会话",
    "file_undo": "文件撤销",
    "chart_viewer": "图表查看",
}

# ── 侧边栏展开最小宽度（px，frame 宽，含 margins 12 + border 2）──

# ── 侧边栏展开最小宽度（px，frame 宽，含 margins 12 + border 2）──
# 挤压折叠后点击展开时，若保存的宽度已被压到折叠阈值以下，展开不得窄于该值，
# 否则标题文字被压成窄条无法阅读。
_EXPANDED_MIN_FRAME_WIDTH = 200

# ── 侧边栏固定默认宽度（px，内容宽）──
# 打开时固定默认宽度（不记忆），约原 280 的 2/3。
_DEFAULT_PANEL_WIDTH = 187

# ── 挤压折叠后自动展开的窗口增长阈值（px）──
# 窗口 resize 挤压折叠后，需比折叠时窗口总宽再宽出该值才自动展开，
# 避免"折叠刚完成条件恰满足就弹回展开"的抖动（绝对条件在窗口 ~760 时
# 折叠即满足展开条件，导致折叠态无法保持）。overlay 卡片关闭属布局恢复
# （窗口总宽未变），不走增长条件。
_AUTO_EXPAND_GROWTH = 200

# ── 聊天区最小可用宽度（px）──
# 判定"侧边栏是否真的被挤压"的下限：窗口总宽放得下
# 「常规展开宽度 + 该值」就不算挤压，侧边栏必须保持展开。
_MIN_CHAT_WIDTH = 400

# ── nativeEvent 热路径缓存（模块级，进程内只算一次）──
# 拖拽窗口时每秒有上千条原生消息进入 nativeEvent，
# 这里预先缓存平台判定与 ctypes cast 函数，避免 per-message 开销。
_IS_WINDOWS = platform.system() == "Windows"
_IS_MAC = platform.system() == "Darwin"

# ── Win32 绑定默认值 ──
# 非 Windows 平台（或绑定失败）保持占位值，调用方一律用 `_IS_WINDOWS and
# _user32 is not None` 守卫，避免 NameError 打断窗口构造。
_MSG_CAST = None
_user32 = None
_GWL_STYLE = -16
_SNAP_STYLES = 0
_SWP_FRAMECHANGED = 0
_HTCLIENT = 1
_HTLEFT = _HTRIGHT = _HTTOP = 0
_HTTOPLEFT = _HTTOPRIGHT = 0
_HTBOTTOM = _HTBOTTOMLEFT = _HTBOTTOMRIGHT = 0

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

        # ── Win32 函数绑定（WM_NCHITTEST 边缘判定用，进程内绑定一次）──
        _user32 = _ctypes.windll.user32
        _user32.ScreenToClient.argtypes = [_wintypes.HWND, _ctypes.POINTER(_wintypes.POINT)]
        _user32.ScreenToClient.restype = _ctypes.c_bool
        _user32.GetClientRect.argtypes = [_wintypes.HWND, _ctypes.POINTER(_wintypes.RECT)]
        _user32.GetClientRect.restype = _ctypes.c_bool
        _user32.IsZoomed.argtypes = [_wintypes.HWND]
        _user32.IsZoomed.restype = _ctypes.c_bool

        # 补回窗口样式位需要 GWL_STYLE / SetWindowPos
        _user32.GetWindowLongPtrW.argtypes = [_wintypes.HWND, _ctypes.c_int]
        _user32.GetWindowLongPtrW.restype = _ctypes.c_ssize_t
        _user32.SetWindowLongPtrW.argtypes = [_wintypes.HWND, _ctypes.c_int, _ctypes.c_ssize_t]
        _user32.SetWindowLongPtrW.restype = _ctypes.c_ssize_t
        _user32.SetWindowPos.argtypes = [
            _wintypes.HWND,
            _wintypes.HWND,
            _ctypes.c_int,
            _ctypes.c_int,
            _ctypes.c_int,
            _ctypes.c_int,
            _wintypes.UINT,
        ]
        _user32.SetWindowPos.restype = _ctypes.c_bool

        # GetWindowLongPtr 索引
        _GWL_STYLE = -16

        # 补回的窗口样式位（Qt 的 FramelessWindowHint 会连带清掉这些，
        # 详见 TabManagerWindow._ensure_native_window_styles 的说明）
        _WS_MINIMIZEBOX = 0x00020000
        _WS_MAXIMIZEBOX = 0x00010000
        _WS_SYSMENU = 0x00080000
        _WS_THICKFRAME = 0x00040000
        _SNAP_STYLES = _WS_THICKFRAME | _WS_SYSMENU | _WS_MINIMIZEBOX | _WS_MAXIMIZEBOX

        # SetWindowPos flags：NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED
        _SWP_FRAMECHANGED = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020

        # WM_NCHITTEST 返回值（边缘/角落 resize）
        _HTCLIENT = 1
        _HTLEFT = 10
        _HTRIGHT = 11
        _HTTOP = 12
        _HTTOPLEFT = 13
        _HTTOPRIGHT = 14
        _HTBOTTOM = 15
        _HTBOTTOMLEFT = 16
        _HTBOTTOMRIGHT = 17
    except Exception:
        _MSG_CAST = None
        _user32 = None


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

    - Windows/Linux：WindowStaysOnTopHint（WS_EX_TOPMOST，与最小化兼容）
    - macOS：软置顶——不加 hint。Qt 会把 StaysOnTopHint 顶层窗口提到
      NSStatusWindowLevel(8)，而 macOS WindowServer 对非 normal 层级窗口
      丢弃标题栏最小化点击（黄按钮无反应，系统层限制无法绕过）。
      改为仅抬升窗口；持续置顶由 TabManagerWindow 监听应用激活补抬升。
    """
    from app.utils.config import Settings as _Settings

    enabled = _Settings.get_instance().window_always_on_top.value

    def _strip_topmost_hint():
        """摘除 WindowStaysOnTopHint（配置关闭或历史残留时）"""
        flags = window.windowFlags()
        if flags & Qt.WindowStaysOnTopHint:
            flags &= ~Qt.WindowStaysOnTopHint
            was_visible = window.isVisible()
            window.setWindowFlags(flags)
            if was_visible:
                window.show()
                window.raise_()
                window.activateWindow()

    if _IS_MAC:
        # 摘除历史残留（旧版本/置顶期间启动的进程），恢复最小化能力
        _strip_topmost_hint()
        if enabled and not window.isMinimized():
            window.raise_()
        return

    if enabled:
        flags = window.windowFlags()
        if not (flags & Qt.WindowStaysOnTopHint):
            flags |= Qt.WindowStaysOnTopHint
            was_visible = window.isVisible()
            window.setWindowFlags(flags)
            if was_visible:
                window.show()
                window.raise_()
                window.activateWindow()
    else:
        _strip_topmost_hint()


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


class _DockSideWrapper(QWidget):
    """LEFT/RIGHT 停靠区侧 wrapper：子控件 visibility 联动 wrapper 与 splitter 大小

    默认收起（hide + splitter 分配 0 空间）；任一子 show 时恢复记忆展开宽度；
    全 hide 时记忆当前宽度并把 splitter 大小压回 0。解决 T3 接线后无卡片时
    splitter 默认均分空间导致空白 handle 显示的视觉 bug。
    """

    DEFAULT_EXPANDED_WIDTH = 300  # 首展开无记忆时的默认宽度

    @staticmethod
    def _child_visible_intent(w: QWidget) -> bool:
        """子控件「显式可见意图」：被 show 过即算，不受祖先链遮蔽

        不能用 isVisible()：wrapper 默认 hide 时子控件 isVisible() 恒为
        False（祖先链中断），_sync 会永远走收起分支 → wrapper 死锁无法
        展开，左右侧浮动卡片不显示。也不能用 not isHidden()：从未
        show/hide 过的子控件 isHidden() 同样为 False，会把初始收起态
        误判为展开。WA_WState_ExplicitShowHide 区分「从未操作」与
        「显式 show/hide」，WA_WState_Hidden 即 isHidden() 的底层状态位。
        """
        if not w.testAttribute(Qt.WA_WState_ExplicitShowHide):  # pyright: ignore[reportAttributeAccessIssue]
            return False  # 从未显式 show/hide → 视为收起（初始态）
        return not w.testAttribute(Qt.WA_WState_Hidden)  # pyright: ignore[reportAttributeAccessIssue]

    def __init__(self, primary: QWidget, stack: QWidget, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._primary = primary
        self._stack = stack
        self._splitter: Optional[QWidget] = None  # QSplitter（类型注解避免循环引用）
        self._splitter_index: int = -1
        self._expanded_width: int = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(primary)
        lay.addWidget(stack)
        primary.installEventFilter(self)
        stack.installEventFilter(self)
        self.hide()  # 默认收起

    def attach_to_splitter(self, splitter: QWidget, index: int) -> None:
        """关联宿主 splitter：联动 setSizes 维持收起态"""
        self._splitter = splitter
        self._splitter_index = index
        # 立即同步：默认两子均 hide → 立即压回 splitter 0 大小，
        # 避免 addWidget 后 splitter 默认均分空间导致空白 handle 显示
        self._sync()

    def eventFilter(self, obj, ev):
        # Show/Hide（17/18）仅在父级可见时发出；父级（wrapper 自身）hidden 时
        # 子控件显式 show() 只发 ShowToParent/HideToParent（26/27）——必须同时
        # 监听，否则 wrapper 收起态下子显示状态变化收不到通知，_sync 永不触发，
        # 左右侧浮动卡片无法展开（ isVisible()/事件双死锁）。
        ev_types = (
            QEvent.Show,  # pyright: ignore[reportAttributeAccessIssue]
            QEvent.Hide,  # pyright: ignore[reportAttributeAccessIssue]
            QEvent.ShowToParent,  # pyright: ignore[reportAttributeAccessIssue]
            QEvent.HideToParent,  # pyright: ignore[reportAttributeAccessIssue]
        )
        if ev.type() in ev_types:
            # singleShot 0 延迟到事件循环下轮，避免 setSizes 与正在进行的
            # 容器动画/尺寸变更相互覆盖（动画中 show/hide 频繁触发）
            QTimer.singleShot(0, self._sync)
        return super().eventFilter(obj, ev)

    def _sync(self) -> None:
        any_visible = self._child_visible_intent(self._primary) or self._child_visible_intent(self._stack)
        if self._splitter is None or self._splitter_index < 0:
            # 未关联 splitter 时只切自身 visibility（测试 / 单实例场景）
            self.setVisible(any_visible)
            return
        sizes = self._splitter.sizes()
        idx = self._splitter_index
        if any_visible:
            if self.isHidden():
                self.show()
                # 预置展开宽度（仅槽位为 0 的全收起重开）：
                # - primary（CardContainer）有可见卡片：用容器 dock 协议已算好
                #   的展开目标（_last_expand_target，按插件独立记忆），wrapper
                #   单值记忆会覆盖 per-card 记忆造成二次弹跳；splitter 对
                #   hidden 子项的 setSizes 无效，容器展开时的预置可能被
                #   wrapper 随后 show 冲掉，此处 show 后统一归位。
                # - 仅 stack 侧可见：用 wrapper 自身记忆/默认宽。
                primary_active = self._child_visible_intent(self._primary)
                if primary_active:
                    target = getattr(self._primary, "_last_expand_target", 0)
                    expand_w = target if target > 0 else (self._expanded_width or self.DEFAULT_EXPANDED_WIDTH)
                else:
                    if self._expanded_width == 0:
                        self._expanded_width = self.DEFAULT_EXPANDED_WIDTH
                    expand_w = self._expanded_width
                if idx < len(sizes) and sizes[idx] == 0:
                    sizes[idx] = expand_w
                    self._splitter.setSizes(sizes)
        else:
            if not self.isHidden():
                if idx < len(sizes):
                    self._expanded_width = (
                        sizes[idx] if sizes[idx] > 0 else self._expanded_width or self.DEFAULT_EXPANDED_WIDTH
                    )
                    sizes[idx] = 0
                    self._splitter.setSizes(sizes)
                self.hide()


class TabManagerWindow(FramelessWindow):
    """Tab 管理器宿主窗口（单例）"""

    _instance: Optional["TabManagerWindow"] = None
    # 标题栏拖拽由 Windows 原生管理（HTCAPTION + WS_CAPTION），Python 不干预
    # Windows 原生移动/缩放模态循环消息：拖拽起止的权威信号，
    # 用于替代 moveEvent + 防抖定时器的"猜测式"拖拽检测
    _WM_ENTERSIZEMOVE = 0x0231
    _WM_EXITSIZEMOVE = 0x0232
    _WM_MOVING = 0x0216  # 仅"移动"触发；"缩放"发 WM_SIZING，二者互斥，可精确区分
    _WM_NCHITTEST = 0x0084  # 边缘/角落 resize 热区判定（自建，见 _native_hit_test）

    # resize 热区宽度（逻辑 px，按窗口 DPI 缩放）。系统默认无边框热区为 0，
    # 基类 qframelesswindow 固定 5px 不随 DPI 变化，高 DPI 下几乎抓不到。
    _RESIZE_BORDER = 6
    # 顶边热区高度：只在窗口最上沿开一条窄带，避免吞掉标题栏主体及其按钮
    _TOP_RESIZE_BAND = 5

    tabCountChanged = pyqtSignal(int)
    activeTabChanged = pyqtSignal(int)
    # 聚合 AI 状态 → 全局桌宠（仅当前激活窗的状态被转发，避免多窗串扰）
    active_ai_state_changed = pyqtSignal(str)

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
        # 首次显示已应用默认几何（固定默认大小/位置，运行中不重置）
        self._geometry_applied: bool = False
        # ── 上次图标缩放值，主题切换时用于判断是否需要重绘图标 ──
        self._last_icon_scale: int = -1
        # ── 侧边栏展开/折叠宽度动画 ──
        self._sidebar_anim: Optional[QVariantAnimation] = None
        # 动画方向（True=收起），供 valueChanged 里判断跨阈值时机
        self._sidebar_anim_collapsing: bool = False
        self._sidebar_anim_ui_switched: bool = False  # 动画中是否已跨阈值切换 UI
        # ── 工作台显隐宽度动画（复用对象 + 挂起的收尾回调） ──
        self._wb_anim: Optional[QVariantAnimation] = None
        self._wb_anim_finished_cb: Optional[Callable[[], None]] = None

        self._geo_save_timer.timeout.connect(self._do_save_geometry)

        # ── Resize 动画节流：100ms 无 resize 事件后退出节流模式 ──
        self._resize_blocking: bool = False  # resize 期间跳过 super().resizeEvent，冻结全部布局
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self._on_resize_finished)

        # ── splitter 拖拽/动画防抖：松手后 ~120ms 恢复 _content_area 绘制 ──
        # 超时回调合并 _content_area.setUpdatesEnabled(True) + _tab_panel.set_resizing(False)。
        self._splitter_idle_timer = QTimer(self)
        self._splitter_idle_timer.setSingleShot(True)
        self._splitter_idle_timer.setInterval(120)
        self._splitter_idle_timer.timeout.connect(self._on_splitter_idle)
        # 拖拽首帧冻结标记：本次拖拽会话仅冻结一次，松手(device idle)后复位
        self._splitter_dragging: bool = False

        self.setWindowTitle("飘狐-DriFox")
        self.setObjectName("tabManagerWindow")
        self.setMinimumSize(600, 450)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        # 注：macOS 置顶与最小化互斥（WindowStaysOnTopHint → NSStatusWindowLevel
        # 会丢标题栏最小化点击），置顶统一走 _apply_window_topmost 的软置顶分支
        # 无边框窗口：显式 Qt.Window 顶级窗口标志（WS_OVERLAPPEDWINDOW 样式基础）——
        # 若缺省（Qt.Widget → WS_POPUP），系统级 Aero Snap（拖到屏幕边缘填充）与
        # NCHITTEST 边缘 resize 全部失效；随后 updateFrameless() 重新叠加
        # FramelessWindowHint + DWM 阴影/窗口动画
        self.setWindowFlags(Qt.Window)
        self.updateFrameless()

        from app.widgets.custom_title_bar import CustomTitleBar

        self.setTitleBar(CustomTitleBar(self))
        # 顶栏接线：侧栏开关 → 复用 TabPanel 折叠链（sidebarToggled → 宽度动画）
        self.titleBar.sidebar_toggle_requested.connect(lambda: self._tab_panel._toggle_sidebar())
        # 顶栏 tab 切换：「聊天」→ 对话视图；full 卡片 tab → 切换显示
        self.titleBar.tab_clicked.connect(self._on_titlebar_tab_clicked)
        # 顶栏 tab × 关闭（仅 full 卡片 tab 有）：真实隐藏卡片并从 open 集合移除
        self.titleBar.tab_close_clicked.connect(self._on_replace_tab_close_clicked)
        self.setWindowIcon(QIcon(":/icons/drifox.ico"))

        # 确保 Colors 已刷新（主题色初始化）
        Colors.refresh()

        # 替换类型(full 容器)卡片标题栏 tab 状态
        # full 卡片 tab 状态按对话标签页隔离：{window_id: {card_id: title}}
        self._replace_open: "Dict[str, OrderedDict[str, str]]" = {}
        self._replace_active: "Dict[str, Optional[str]]" = {}
        self._replace_timers: Dict[str, "QTimer"] = {}
        self._suppress_replace_close: bool = False
        # 插件注册的常驻标题栏 tab id 集合（插件卸载/刷新时移除用）
        self._plugin_titlebar_tab_ids: set = set()
        # 标题栏高亮重算合并标志（见 _schedule_replace_highlight）
        self._replace_highlight_pending = False

        self._setup_ui()
        self._setup_signals()
        # 不在 __init__ 设位置，等第一次 showEvent 时再设

        # ── 应用级服务（本类是应用生命周期容器，故在此创建/启停） ──
        # 一个应用一个实例：与任何 ChatWindow/tab 生命周期解耦。
        from app.core.gateway_service import GatewayService
        from app.core.plugin_host_service import PluginHostService

        GatewayService.get_instance().ensure_started()
        PluginHostService.get_instance().ensure_started()

        # app 级 API 会话处理器：始终路由到当前活跃 tab
        # 修复多 tab 下后建窗口静默覆盖先建窗口的问题（之前由 MainWidget
        # 在 _init_llm_api_service 注册 handler，绑死后建窗口的 widget；现改为
        # 每次调用实时查 _tab_panel.active_index 对应的活跃 ChatWindow）
        from app.gateway.local_service.session_handler import APISessionHandler
        from app.gateway import LLMAPIService

        def _active_main_widget():
            idx = self._tab_panel.active_index if self._tab_panel is not None else -1
            if 0 <= idx < len(self._windows):
                w = self._windows[idx]
                if w is not None and not getattr(w, "_is_destroyed", False):
                    return w
            return None

        self._api_session_handler = APISessionHandler(_active_main_widget)
        LLMAPIService.set_session_handler(self._api_session_handler)

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

        # ── 全局桌宠（单例浮层，下沉自 MainWidget）──
        # 多 tab 下唯一常驻：不随窗口销毁，切 tab 平滑跟随激活窗 send_btn。
        # pet_enabled=False 时 hide 而非 destroy（保留实例，便于实时开关）。
        from app.widgets.pixel_pet import PixelPetWidget

        self.pixel_pet = PixelPetWidget(self)
        if Settings.get_instance().pet_enabled.value:
            self.pixel_pet.show()
            self.pixel_pet.raise_()
        else:
            self.pixel_pet.hide()
        Settings.get_instance().pet_enabled.valueChanged.connect(self._on_pet_enabled_changed)
        # 主题 / 字号刷新（取代 main 的 pet 级连接）：仅在此处完成一次。
        # 注意：_pixel_pet 自身的 __init__ 已调用 _connect_config_signals()，
        # 此处【不可】重复调用，否则 pet_size 信号会被连两次。
        theme_manager.register_refresh_target(self.pixel_pet)
        # AI 状态：pet 只连一次聚合信号（切 tab 由聚合信号改发当前窗状态）
        self.active_ai_state_changed.connect(self.pixel_pet._on_ai_state_changed)
        # 初始定位到右下角（首个窗口加入 + 激活后会精确对齐 send_btn）
        self.pixel_pet.resize_handle(self.width(), self.height())

        # ── 右侧工作台面板（**嵌入式**：对话区右侧第三窗格，与左侧 TabPanel 对称）──
        # 放弃悬浮：QWebEngineView 是原生 HWND，悬浮面板必然与之争 z-order
        # （child 盖不住 / WA_NativeWindow 吞主窗口边缘 resize / Tool 窗口跟随延迟）。
        # 嵌入式与对话区并列不重叠，从根上无遮挡，也不需要任何 HWND 技巧。
        from app.widgets.workbench_panel import (
            PANEL_WIDTH_DEFAULT,
            PANEL_WIDTH_MAX,
            PANEL_WIDTH_MIN,
            WorkbenchPanel,
        )

        self.workbench_panel = WorkbenchPanel(self)
        self.workbench_panel.hide()
        self.workbench_panel.close_requested.connect(self._hide_workbench)
        self.workbench_panel.refresh_requested.connect(self.refresh_workbench)
        self.workbench_panel.diff_requested.connect(self._open_workbench_diff)
        # 切到「历史会话」页时刷新当前活跃窗口的历史列表（面板隐藏期间 isVisible 跳过的补刷）
        self.workbench_panel.history_tab_shown.connect(self._on_workbench_history_shown)
        # 🆕 页签按对话窗口独立记忆：页签变化 → 写入当前活跃窗口；切回窗口时恢复
        self.workbench_panel.current_tab_changed.connect(self._remember_workbench_tab)
        self.titleBar.workbench_toggle_requested.connect(self.toggle_workbench)
        # 主题 / 字号刷新：与桌宠同路径注册
        theme_manager.register_refresh_target(self.workbench_panel)

        # 圆角矩形容器（与 #tabFrame / #chatFrame 同款），作为 splitter 第三窗格。
        # 直接给 #workbenchPanel 设 border 会被 splitter handle 绘制顺序吞掉，
        # 故同左侧一样再包一层 QFrame 用 objectName 上样式。
        self._workbench_frame = QFrame()
        self._workbench_frame.setObjectName("workbenchFrame")
        # QSplitter 直接子项是 frame，宽约束必须设在 frame 上：
        #   min = panel min(320) + margins(12) + border(2) = 334
        #   max = panel max(820) + margins(12) + border(2) = 834
        self._workbench_frame.setMinimumWidth(PANEL_WIDTH_MIN + 14)
        self._workbench_frame.setMaximumWidth(PANEL_WIDTH_MAX + 14)
        _wb_frame_layout = QVBoxLayout(self._workbench_frame)
        # 与 #chatFrame / #tabFrame 内边距完全对齐，视觉一致
        _wb_frame_layout.setContentsMargins(6, 6, 6, 6)
        _wb_frame_layout.setSpacing(0)
        _wb_frame_layout.addWidget(self.workbench_panel)
        self._workbench_frame.hide()  # 默认隐藏（标题栏「右侧边栏」按钮 toggle）
        # 挂到 splitter：不拉伸（与左侧 tabFrame 同策略），宽度由 handle 拖拽
        self._splitter.addWidget(self._workbench_frame)
        self._splitter.setStretchFactor(2, 0)
        # 初始宽度（隐藏态不占空间，展开时生效）
        self._workbench_frame_w = PANEL_WIDTH_DEFAULT + 14

        # 初始加载全局背景图（延迟到首帧后，背景为纯装饰，不阻塞出现）
        QTimer.singleShot(0, self._apply_bg_from_theme)

        # 顶栏内置「聊天」常驻 tab（full 卡片 tab 由显隐事件动态增删，
        # 插件常驻 tab 由 _sync_plugin_titlebar_tabs 挂载）
        self.titleBar.add_tab(CHAT_TAB_ID, "对话")
        self._sync_plugin_titlebar_tabs()

    def _on_titlebar_tab_clicked(self, tab_id: str):
        """顶栏 tab 点击：「聊天」→ 对话视图；full 卡片 tab → 切换/显示；
        插件常驻 tab 的展示由注册时的 on_click 回调处理，此处忽略。

        无论走哪条分支，最后都调度一次高亮收敛（见 ``_sync_replace_highlight``）：
        插件常驻 tab 的 on_click 内部若把卡片 toggle 成隐藏，高亮必须回退，
        而这条路径此前完全没有同步点。
        """
        logger.debug(f"[TitleBar] tab clicked: {tab_id}")
        if tab_id == CHAT_TAB_ID:
            self._show_conversation_view()
        elif tab_id not in self._plugin_titlebar_tab_ids:
            self._on_replace_tab_clicked(tab_id)
        self._schedule_replace_highlight()

    def _apply_win11_round_corner(self):
        """Win11 DWM 圆角；Win10 及更早静默跳过

        DWMWA_WINDOW_CORNER_PREFERENCE(33) = DWMCP_ROUND(2)，
        调用失败（Win10 无此属性）不影响窗口功能。
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            pref = ctypes.c_int(2)  # DWMCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        except Exception:
            pass

    def _on_theme_changed(self):
        """主题切换时刷新配色

        注意：调用方（dispatch_refresh / _execute_batched_theme_refresh）
        已执行 Colors.refresh()，此处不再重复调用。
        """
        from app.utils.theme_refresh import ThemeRefreshCoordinator

        ThemeRefreshCoordinator.timer_start("tab_manager")

        # 重建样式表
        self._apply_theme_stylesheet()

        # 刷新自绘顶栏（tab 胶囊/按钮样式随主题；Colors 已由调用方 refresh）
        if hasattr(self.titleBar, "refresh_style"):
            self.titleBar.refresh_style()
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

        # 刷新标题栏（含 full 卡片 tab 与插件常驻 tab 样式）
        try:
            self.titleBar.refresh_style()
        except Exception:
            pass

        ThemeRefreshCoordinator.timer_end("tab_manager")

    def _on_pet_enabled_changed(self, enabled: bool) -> None:
        """桌宠显示开关实时响应（全局桌宠由 TabManager 统一管理）"""
        pet = getattr(self, "pixel_pet", None)
        if pet is None:
            return
        if enabled:
            pet.show()
            pet.raise_()
            # 重新对齐当前激活窗 send_btn
            pet.reposition_to_active_window()
        else:
            # hide 而非 destroy：保留实例，便于再次开启无需重建
            pet.hide()

    # ── 右侧工作台（嵌入式：toggle 显隐 / 数据刷新） ──

    def _splitter_sizes_with_left(self, left_w: int) -> List[int]:
        """按目标左侧宽度生成完整 splitter sizes（含工作台窗格）

        ★ setSizes 传值少于窗格数时，QSplitter 会把缺省窗格压到 0（实测：
        三窗格 [200,300,400] → setSizes([60,840]) → [59,819,0]）。三窗格布局
        下折叠左侧若只传两值，右侧工作台宽度会被清掉/回退到最小宽度。
        这里显式保留工作台当前宽度，chat 吃剩余空间。"""
        try:
            sizes = self._splitter.sizes()
        except Exception:
            sizes = []
        total = sum(sizes) if sizes else self.width()
        if len(sizes) >= 3:
            wb = sizes[2]
            return [left_w, max(0, total - left_w - wb), wb]
        return [left_w, max(0, total - left_w)]

    def _set_windows_resize_preview_suppressed(self, suppressed: bool) -> None:
        """动画期抑制所有窗口卡片进入 resize 预览模式（WebView 保持可见）

        MainWidget.resizeEvent 默认会 _set_cards_resize_preview_mode(True)
        把 WebView 藏成静态预览——侧栏/工作台动画每帧 resize 对话区，
        若不抑制，动画全程中间区域都是被隐藏的状态。动画结束后解除，
        宽度同步由既有的防抖定时器一次完成。"""
        if not hasattr(self, "_content_area"):
            return
        for i in range(self._content_area.count()):
            w = self._content_area.widget(i)
            if w is not None:
                w._suppress_resize_preview = suppressed

    def toggle_workbench(self) -> None:
        """标题栏「右侧边栏」按钮回调：直接显示/隐藏工作台（无折叠动画）

        工作台是 splitter 第三窗格，hide/show 后 QSplitter 自动把空间
        归还/分配给对话区，无需手动 setSizes。
        """
        self.set_workbench_visible(not self.is_workbench_visible())

    def is_workbench_visible(self) -> bool:
        """工作台当前是否可见（动画期间返回目标状态，避免半途状态误判）"""
        if getattr(self, "_wb_visible_target", None) is not None:
            return bool(self._wb_visible_target)
        frame = getattr(self, "_workbench_frame", None)
        return bool(frame is not None and frame.isVisible())

    # ── 工作台显隐动画 ──

    def _apply_wb_width(self, w: int) -> None:
        """动画帧：把工作台窗格宽度推到指定值（从对话区借/还空间，总宽不变）"""
        frame = getattr(self, "_workbench_frame", None)
        if frame is None:
            return
        try:
            sizes = self._splitter.sizes()
            if len(sizes) < 3:
                return
            wb = max(0, min(int(w), frame.maximumWidth()))
            total = sum(sizes)
            chat = max(0, total - sizes[0] - wb)
            self._splitter.setSizes([sizes[0], chat, wb])
        except Exception:
            pass

    def _start_wb_anim(self, start_w: int, end_w: int, on_finished=None) -> None:
        """启动工作台宽度动画（200ms OutCubic）；复用动画对象，stop 后重设起止值

        对齐侧栏 #31：动画期间不再冻结 _content_area 重绘（中间区域原样实时
        显示），改为抑制卡片 resize 预览模式，由 _on_wb_anim_finished 统一恢复。

        ★ 动画期间必须同时放开 panel 自身的最小宽度：frame.setMinimumWidth(0)
        管不住 minimumSizeHint——panel 的 setMinimumWidth(PANEL_WIDTH_MIN) 会把
        frame 实际下限顶在 334px，导致展开/收起动画前段被硬性钳住不动、尾段猛跳
        （用户感知的"动画卡顿"主因）。"""
        self._set_windows_resize_preview_suppressed(True)
        panel = getattr(self, "workbench_panel", None)
        if panel is not None:
            panel.setMinimumWidth(0)
        anim = self._wb_anim
        if anim is None:
            anim = QVariantAnimation(self)
            anim.setDuration(200)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.valueChanged.connect(lambda v: self._apply_wb_width(int(v)))
            anim.finished.connect(self._on_wb_anim_finished)
            self._wb_anim = anim
        # 中途反向重启时旧收尾回调直接丢弃，只执行最新一次的回调
        self._wb_anim_finished_cb = on_finished
        anim.stop()
        anim.setStartValue(float(start_w))
        anim.setEndValue(float(end_w))
        anim.start()

    def _on_wb_anim_finished(self) -> None:
        """动画结束：执行收尾回调 + 恢复卡片 resize 预览能力

        若本回调又同步启动了新动画（反向重启），则跳过恢复（预览抑制与
        panel 最小宽度都交由其 finished 恢复），避免动画中途解除。"""
        cb = getattr(self, "_wb_anim_finished_cb", None)
        self._wb_anim_finished_cb = None
        if cb is not None:
            cb()
        if self._wb_anim is not None and self._wb_anim.state() == QVariantAnimation.Running:
            return
        self._set_windows_resize_preview_suppressed(False)

    def _stop_wb_anim(self) -> None:
        anim = getattr(self, "_wb_anim", None)
        if anim is not None:
            anim.stop()  # stop 不发射 finished；动画对象保留供复用，
            # 挂起的收尾回调由下一次 _start_wb_anim 覆盖或 _on_wb_anim_finished 丢弃

    def _set_chat_frame_wb_hidden(self, hidden: bool) -> None:
        """工作台隐藏后给对话区右侧补 4px 窗口边距（否则圆角矩形贴死窗口边）

        显示时 chatFrame 右侧靠 splitter handle 与 workbenchFrame 隔开（margin 右 0）;
        隐藏后没有 handle 兑底，需切到 margin 右 4px。用 QSS 属性选择器切换。
        """
        cf = getattr(self, "_chat_frame", None)
        if cf is None or cf.property("wbHidden") == hidden:
            return
        cf.setProperty("wbHidden", hidden)
        st = cf.style()
        st.unpolish(cf)
        st.polish(cf)

    def set_workbench_visible(self, visible: bool) -> None:
        """显示/隐藏工作台（带 200ms 宽度展开/收拢动画）

        隐藏时记忆当前宽度，展开时恢复。动画期间重复触发会重启反向动画。
        """
        frame = getattr(self, "_workbench_frame", None)
        panel = getattr(self, "workbench_panel", None)
        if frame is None or panel is None:
            return
        visible = bool(visible)
        if visible == getattr(self, "_wb_visible_target", frame.isVisible()):
            # 状态一致时仍要确保 panel 同步（panel 曾被显式 hide，
            # frame.show() 不会连带显示它）
            panel.set_panel_visible(visible)
            return
        from app.widgets.workbench_panel import PANEL_WIDTH_DEFAULT, PANEL_WIDTH_MIN

        self._wb_visible_target = visible
        self._stop_wb_anim()
        try:
            sizes = self._splitter.sizes()
            start_w = sizes[2] if len(sizes) >= 3 else 0
        except Exception:
            start_w = 0
        if visible:
            frame.show()
            # ★ 必须显式 show panel：panel 构造后调过 hide()，
            #   Qt 中被显式 hide 的子 widget 不会因 parent.show() 自动恢复。
            panel.set_panel_visible(True)
            # ★ 不调 restore_last_tab：hide 期间 stack 当前页本就保持，
            #   重新 show 天然恢复用户上次选择的页签；首次默认第一个页签由
            #   面板 __init__ 保证。若在此恢复上次关闭时页签，会覆盖
            #   「隐藏状态下定向打开」刚设置的页签（如插件卡片 tab）。
            self._set_chat_frame_wb_hidden(False)
            # 动画期间放开最小宽度约束，QSplitter 才允许窗格拉到中间值
            frame.setMinimumWidth(0)
            target_w = max(0, min(getattr(self, "_workbench_frame_w", PANEL_WIDTH_DEFAULT + 14), frame.maximumWidth()))
            self._start_wb_anim(
                start_w,
                target_w,
                # 数据填充（refresh_workbench 含页签 reconcile + 树/列表刷新）
                # 延后到动画结束，避免与首帧布局争抢主线程
                on_finished=lambda: (
                    frame.setMinimumWidth(PANEL_WIDTH_MIN + 14),
                    panel.setMinimumWidth(PANEL_WIDTH_MIN),
                    self.refresh_workbench(),
                ),
            )
        else:
            if start_w > 0:
                self._workbench_frame_w = start_w
            frame.setMinimumWidth(0)
            self._start_wb_anim(
                start_w,
                0,
                on_finished=lambda: (
                    panel.set_panel_visible(False),
                    frame.hide(),
                    frame.setMinimumWidth(PANEL_WIDTH_MIN + 14),
                    panel.setMinimumWidth(PANEL_WIDTH_MIN),
                    self._set_chat_frame_wb_hidden(True),
                ),
            )

    def _hide_workbench(self) -> None:
        """工作台关闭按钮：直接隐藏（实例保留，再次开启零重建）"""
        self.set_workbench_visible(False)

    def _remember_workbench_tab(self, index: int) -> None:
        """页签变化回调：把当前页签记到当前活跃对话窗口（按窗口独立记忆）

        工作台是宿主级单例，页签状态原先全局共享——标签页 A 停在「记忆」页，
        切到标签页 B 也跟着停在「记忆」页。现在每次页签变化都写入活跃窗口的
        ``_workbench_tab_memory``，切换对话窗口时由 _on_tab_changed 恢复该窗口
        自己的记忆。无活跃窗口/面板未挂载时静默跳过（信号驱动，零轮询）。
        """
        win = self.get_current_window()
        if win is None:
            return
        try:
            win._workbench_tab_memory = int(index)
        except Exception:
            pass

    def refresh_workbench(self) -> None:
        """从当前活跃窗口拉取数据填充工作台（产物/任务/项目记忆）

        数据源均为既有单一数据源：
        - 产物：backend.file_recorder 会话级文件写入记录
        - 任务：窗口 _latest_todos（todowrite 结果联动缓存），缺失回退 tool_executor
        - 项目：win._current_project + _current_workdir → MemoryCardContent
        """
        panel = getattr(self, "workbench_panel", None)
        if panel is None or not panel.isVisible():
            return
        # 插件工作台页签 reconcile（内置产物/记忆 + 插件注册页）
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            panel.sync_plugin_pages(UIPluginRegistry.get_instance().get_workbench_tabs())
        except Exception:
            pass
        win = self.get_current_window()
        backend = getattr(win, "backend", None) if win is not None else None
        project = getattr(win, "_current_project", "") or ""
        # 历史会话页：跟随当前活跃窗口换挂（与其他页同一投影语义；未构建时保持现状）
        panel.attach_history_page(getattr(win, "_history_card", None) if win is not None else None)
        if backend is not None:
            # 记忆页懒构建（backend 未就绪时保持"未就绪"提示）
            panel.ensure_memory(backend.memory_manager)
            try:
                workdir = (getattr(win, "_current_workdir", None) or {}).get(project)
            except Exception:
                workdir = None
            panel.update_project(project, workdir)
            # 产物：本会话文件写入记录（会话未落库时跳过）
            session_id = getattr(win, "_current_session_id", None)
            ops: list = []
            try:
                if backend.file_recorder is not None and session_id:
                    ops = backend.file_recorder.get_all_operations_for_session(session_id)
            except Exception:
                ops = []
            panel.update_artifacts(ops)
        # 任务：优先窗口缓存（todowrite 结果联动），缺失回退 tool_executor 实时读
        todos = getattr(win, "_latest_todos", None)
        if not todos and backend is not None and getattr(backend, "_tool_executor", None) is not None:
            try:
                todos = backend._tool_executor.get_todos()
            except Exception:
                todos = []
        panel.update_todos(todos or [])

    def open_workbench_memory(self, sub_tab: str = "docs") -> None:
        """展开工作台并定位（记忆功能已迁移到工作台）

        统一的「一键直达」入口：新建项目后自动展开关键文档、工作树「管理工作树」
        按钮等场景都走这里，不再打开旧的独立记忆卡片。

        Args:
            sub_tab: docs=工作树页（关键文档+工作树，一级页签）/
                     entries|notes=记忆页对应子页签
        """
        panel = getattr(self, "workbench_panel", None)
        if panel is None:
            return
        # 1) 展开工作台（不可见时 set_workbench_visible 内部会触发 refresh_workbench）
        if not self.is_workbench_visible():
            self.set_workbench_visible(True)
        else:
            self.refresh_workbench()
        # 2) 按子页签路由：docs 已升为一级「工作树」页签，其余落记忆页
        if sub_tab == "docs":
            panel.set_current_tab(panel.TAB_WORKTREE)
            return
        panel.set_current_tab(panel.TAB_MEMORY)
        # 3) 切到指定子页签（内容未挂载时 MemoryPage 会记为 pending，挂载后补切）
        try:
            panel.memory_page.switch_sub_tab(sub_tab)
        except Exception as exc:
            logger.warning(f"[Workbench] 切换记忆子页签失败: {exc}")

    def open_workbench_history(self) -> None:
        """展开工作台并定位「历史会话」页（历史会话已从对话区底部卡片迁移至此）

        统一直达入口：底部工具栏历史按钮 / /history 命令都走这里。历史页内容
        是当前活跃窗口的历史卡片（懒创建，首次进入时构建并挂载）；切页后的
        数据刷新由 history_tab_shown → _on_workbench_history_shown 驱动。
        """
        panel = getattr(self, "workbench_panel", None)
        if panel is None:
            return
        # 确保当前活跃窗口的历史卡片已构建并挂载（幂等；未挂载时页签不出现）
        win = self.get_current_window()
        if win is not None and hasattr(win, "_ensure_history_card"):
            try:
                win._ensure_history_card()
            except Exception:
                logger.exception("[Workbench] 构建历史会话卡片失败")
        card = getattr(win, "_history_card", None) if win is not None else None
        if card is not None:
            panel.attach_history_page(card)
        # 展开工作台（不可见时 set_workbench_visible 内部会触发 refresh_workbench）
        if not self.is_workbench_visible():
            self.set_workbench_visible(True)
        else:
            self.refresh_workbench()
        panel.set_current_tab(panel.TAB_HISTORY)

    def _on_workbench_history_shown(self) -> None:
        """「历史会话」页显示 → 刷新当前活跃窗口的历史列表数据"""
        win = self.get_current_window()
        if win is not None and hasattr(win, "_refresh_history_toggle_panel"):
            try:
                win._refresh_history_toggle_panel()
            except Exception:
                pass

    # ── 工作台差异入口（替代标题栏 diff_btn） ──

    def _open_workbench_diff(self, file_paths: Optional[List[str]]) -> None:
        """工作台产物页的差异请求：调 controller.show_diff_viewer（保持工作台显示）

        file_paths 为 None 表示「查看所有产物差异」，由宿主从当前 backend 重新拉取 ops。
        """
        panel = getattr(self, "workbench_panel", None)
        if panel is None:
            return
        win = self.get_current_window()
        backend = getattr(win, "backend", None) if win is not None else None
        session_id = getattr(win, "_current_session_id", None)
        if backend is None or not session_id:
            self._show_diff_toast("当前没有活动会话")
            return
        try:
            from app.utils.file_operation_recorder import FileOperationRecorder
            from app.utils.diff_viewer import DiffHtmlGenerator

            file_recorder = FileOperationRecorder(backend.session_store)
            operations = file_recorder.get_all_operations_for_session(session_id)
        except Exception as e:
            logger.warning(f"[WorkbenchDiff] 数据准备失败: {e}")
            operations = []

        # 决定最终要 diff 的文件路径列表
        target_paths: List[str] = []
        if file_paths:
            # 单条目入口：仅取有效存在的
            target_paths = [p for p in file_paths if p]
        if not target_paths:
            # 「所有产物差异」入口：从 ops 拿文件路径去重
            target_paths = list({op.get("file_path") for op in operations if op.get("file_path")})
        if not target_paths:
            self._show_diff_toast("本次会话没有可对比的产物文件")
            return

        # 生成 diff html
        try:
            diff_text = DiffHtmlGenerator.get_diff_for_files(target_paths, session_id)
            html = DiffHtmlGenerator.generate_html_report(diff_text or "", session_id)
        except Exception as e:
            logger.warning(f"[WorkbenchDiff] 生成 diff html 失败: {e}")
            self._show_diff_toast("生成差异失败")
            return

        # 调 controller 显示差异卡片（覆盖对话区域）
        try:
            from app.widgets.cards.global_card_controller import get_global_card_controller

            controller = get_global_card_controller()
            if controller is None:
                self._show_diff_toast("卡片控制器未就绪")
                return
            controller.show_diff_viewer(html, f"产物差异（{len(target_paths)} 个文件）")
        except Exception as e:
            logger.warning(f"[WorkbenchDiff] show_diff_viewer 失败: {e}")
            self._show_diff_toast("打开差异卡片失败")
            return

    def _show_diff_toast(self, msg: str) -> None:
        """统一的轻量提示（InfoBar 顶层）"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition

            InfoBar.warning(
                "提示",
                msg,
                parent=self,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2500,
            )
        except Exception:
            logger.warning(f"[WorkbenchDiff] {msg}")

    def refresh_theme(self):
        """ThemeManager 统一刷新入口（dispatch_refresh 调用）

        全局桌宠配色随主题刷新交由 theme_manager 统一派发：
        pet 已通过 register_refresh_target 注册，dispatch_refresh 会直接
        调用 pet.refresh_theme() → refresh_pet() 重载 spritesheet，无需此处
        再显式调用（避免重复刷新）。
        """
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
                /* ★ 顶层窗口不要设 border-radius：Qt 只会把"背景绘制"裁成圆角，
                   圆角外侧的三角区不会被绘制，底层透出系统默认窗口色 —— 表现为
                   窗口四角隐约有一圈"系统窗口"的白边，resize 重绘时尤其明显。
                   窗口圆角由 DWM 负责（_apply_win11_round_corner / 补回的
                   WS_THICKFRAME），Qt 侧保持矩形即可。 */
            }}
            #tabManagerContent {{
                background: transparent;
                border-radius: 8px;
            }}
            #chatFrame {{
                background: {Colors.CARD_BG.format(alpha=150)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                /* 左右 margin 均 0，让位给 splitter handle：三窗格布局下
                   中间窗格两侧各有一个 4px handle，若此处保留右 4px margin，
                   右间距会变成 8px 而左间距只有 4px，两侧不对称。 */
                margin: 4px 0 4px 0;
            }}
            /* 工作台隐藏后右侧 handle 消失，需补回窗口右边距，
               否则对话区圆角矩形贴死窗口边框 */
            #chatFrame[wbHidden="true"] {{
                margin: 4px 4px 4px 0;
            }}
            #workbenchFrame {{
                /* 右侧工作台圆角矩形容器，与 #tabFrame / #chatFrame 同款 */
                background: {Colors.CARD_BG.format(alpha=150)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                margin: 4px 4px 4px 0;  /* 右 4px 为窗口右边距，左 0 让位 handle */
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
        if getattr(self, "_chat_vsplitter", None) is not None:
            self._chat_vsplitter.setStyleSheet(f"""
                #chatVsplitter::handle:vertical {{
                    background: transparent;
                    border-top: 2px solid {Colors.BORDER};
                    margin: 2px 10px;
                    border-radius: 1px;
                }}
                #chatVsplitter::handle:vertical:hover {{
                    border-top: 2px solid {Colors.BORDER_ACCENT};
                }}
            """)

    def _apply_bg_from_theme(self):
        """主题切换入口：刷新 4 个区域背景（window/sidebar/chat_area/scene）

        新 schema（yaml 的 `backgrounds:` 块）由 get_theme_backgrounds() 暴露；
        旧字段 `background.chat_list` 通过 PR1 自动映射到 `backgrounds.sidebar`，
        保持现有 17 套内置主题工作不变。

        scene 撑满整个右侧 _chat_frame 圆角矩形（含 tab bar / 对话区 / 输入框 /
        LEFT/RIGHT/BOTTOM 停靠区 / UI 插件槽位等），是 aurora 等主题的图片层；
        chat_area 是 _chat_frame 纯色底（向下兼容旧主题）。
        """
        try:
            bgs = theme_manager.get_theme_backgrounds(theme_manager.get_current_theme_id())
            self._apply_area_bg("window", self, bgs["window"])
            self._apply_area_bg("sidebar", getattr(self, "_tab_frame", None), bgs["sidebar"])
            self._apply_area_bg("chat_area", getattr(self, "_chat_frame", None), bgs["chat_area"])
            self._apply_scene_layer(bgs["scene"])
        except Exception as e:
            logger.warning(f"[TabManagerWindow] 应用主题背景失败: {e}")

    def _apply_scene_layer(self, scene_cfg):
        """应用 scene 配置到 _scene_layer（撑满整个右侧 _chat_frame 圆角矩形）

        Args:
            scene_cfg: get_theme_backgrounds()["scene"] 返回的 dict 或 None
        """
        if not hasattr(self, "_scene_layer") or self._scene_layer is None:
            return

        # image 解析：复用 _resolve_theme_image（已在文件内）
        try:
            self._scene_layer.apply_config(scene_cfg, self._resolve_theme_image)
        except Exception as e:
            logger.warning(f"[TabManagerWindow] 应用 scene 背景失败: {e}")

    def _apply_area_bg(self, area, parent_widget, bg_cfg):
        """统一区域背景加载器（window/sidebar/chat_area 共用）

        Args:
            area: "window"/"sidebar"/"chat_area"（用作属性名前缀）
            parent_widget: 背景挂载的目标 widget（None 时跳过）
            bg_cfg: get_theme_backgrounds() 返回的单个区域配置（None 或 dict）
        """
        if parent_widget is None:
            return

        label_attr = f"_{area}_bg_label"
        opacity_attr = f"_{area}_bg_opacity"
        key_attr = f"_last_{area}_bg_key"

        bg_cfg = bg_cfg or {}
        image = bg_cfg.get("image")
        opacity = bg_cfg.get("opacity", 1.0)
        color = bg_cfg.get("color")

        # 缓存键：image + opacity + color 共同决定唯一性
        bg_key = f"{(image or '__none__')}:{opacity:.3f}:{(color or '__none__')}"
        if getattr(self, key_attr, None) == bg_key and getattr(self, label_attr, None) is not None:
            return
        setattr(self, key_attr, bg_key)

        # 清除旧 label
        old = getattr(self, label_attr, None)
        if old is not None:
            try:
                old.deleteLater()
            except Exception:
                pass
            setattr(self, label_attr, None)

        if not bg_cfg.get("enabled", True):
            return

        label = _AutoGeometryLabel(parent_widget)
        label.setAttribute(Qt.WA_TransparentForMouseEvents)

        # 1. 图片
        if image:
            resolved = self._resolve_theme_image(image)
            pix = QPixmap(resolved)
            if not pix.isNull():
                label.setPixmap(pix)
                label.setScaledContents(True)

        # 2. 透明度
        if opacity < 1.0:
            effect = QGraphicsOpacityEffect(label)
            effect.setOpacity(opacity)
            label.setGraphicsEffect(effect)
            setattr(self, opacity_attr, effect)

        # 3. 颜色（背景底色，独立于图片）
        if color:
            label.setStyleSheet(f"background-color: {color};")

        # Z 序：放到 parent 最底层
        label.lower()
        # 尺寸：撑满 parent
        label.setGeometry(parent_widget.rect())
        label.show()

        setattr(self, label_attr, label)

        # 兼容别名：window 区域的 label 同时赋值给 _bg_label（保留旧契约，
        # 不破坏 tests/widgets/test_tab_manager_resize_throttle.py 的 mock）
        if area == "window":
            self._bg_label = label
            self._bg_opacity = getattr(self, opacity_attr, None)

    @staticmethod
    def _resolve_theme_image(image: str) -> str:
        """解析图片路径：主题文件夹内相对路径基于主题目录；:/icons/... 走 qrc

        Args:
            image: 用户在 yaml 中写的图片路径

        Returns:
            解析后的绝对路径或 qrc 路径
        """
        import os as _os

        if not image or image.startswith(":") or _os.path.isabs(image):
            return image
        theme_dir = theme_manager.get_theme_dir(theme_manager.get_current_theme_id())
        if theme_dir:
            abs_path = str(theme_dir / image)
            if _os.path.exists(abs_path):
                return abs_path
        return image  # fallback 原值

    def _resize_bg_labels(self):
        """同步 3 个区域背景 label 的尺寸跟随 parent（resize 阶段一/阶段二都用）"""
        for attr in ("_bg_label", "_sidebar_bg_label", "_chat_area_bg_label"):
            lbl = getattr(self, attr, None)
            if lbl is None:
                continue
            try:
                parent = lbl.parent()
                if parent is not None:
                    lbl.resize(parent.size())
            except Exception:
                pass

    def _setup_ui(self):
        # ── 外层纵向布局：直接放内容区（顶部让位自绘无边框标题栏） ──
        from app.widgets.custom_title_bar import CustomTitleBar

        main_layout = QVBoxLayout(self)
        # 顶部让位自绘无边框标题栏；mac 上标题栏只有 28（与系统交通灯配套）
        main_layout.setContentsMargins(0, self.titleBar.height(), 0, 0)
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
        # 工作台默认隐藏：对话区先按“右侧无边距”以外的隐藏态样式渲染，
        # 首次展开时由 set_workbench_visible 切回三窗格 margin
        self._chat_frame.setProperty("wbHidden", True)
        chat_frame_layout = QVBoxLayout(self._chat_frame)
        chat_frame_layout.setContentsMargins(6, 6, 6, 6)
        chat_frame_layout.setSpacing(0)

        # ── PR3：场景背景层（scene_layer）撑满整个右侧圆角矩形 ──
        # scene_layer 作为 _chat_frame 子 widget，绝对定位铺满 _chat_frame.rect()，
        # 覆盖整个右侧区域（含对话区 / LEFT/RIGHT/BOTTOM 停靠区 / UI 插件槽位等所有 _chat_frame 内的内容）。
        # 这是 aurora 等主题的 scene 图片/纯色底的真实挂载点。
        # 注意：scene_layer 在 _chat_frame 内部的其它 layout 之前创建，
        # 后续 .lower() 确保它在所有 UI 控件之下。
        from app.widgets.scene_layer import SceneLayer

        self._scene_layer = SceneLayer(self._chat_frame)
        self._scene_layer.lower()
        try:
            self._chat_frame.installEventFilter(self._scene_layer)
        except Exception:
            pass

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
        # 结构：chatVsplitter(纵向)
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

        # 中间对话列：纵向 splitter 包 [对话区(wrapper) | 下停靠区]，
        # 使下插槽与对话区域同列，不再横跨左侧/右侧停靠区下方。
        self._chat_vsplitter = _DockSplitter(Qt.Vertical, self._chat_frame)
        self._chat_vsplitter.setObjectName("chatVsplitter")
        self._chat_vsplitter.addWidget(self._chat_wrapper)
        self._chat_vsplitter.addWidget(self._global_bottom_container)
        self._chat_vsplitter.setStretchFactor(0, 1)  # 对话区吃掉多余高度
        self._chat_vsplitter.setStretchFactor(1, 0)  # 下停靠区不随窗口拉伸
        self._chat_vsplitter.setHandleWidth(6)
        # 折叠依赖轴向 max=0 约束而非用户拖拽收起，禁止拖拽塌陷
        self._chat_vsplitter.setChildrenCollapsible(False)

        # 堆叠卡容器（Phase G）：与单卡 CardContainer 并行挂于同侧停靠区下方。
        # LEFT/RIGHT 侧用 wrapper（QVBoxLayout）包裹 [单卡容器, 堆叠容器]，
        # wrapper 作为 dock_splitter 直接子项——dock mode 协议依赖 CardContainer
        # 为 splitter 直接子项，故经 wrapper 承载二者（_splitter_index 兼容之）。
        from app.widgets.cards.card_stack_container import CardStackContainer

        self._global_left_stack = CardStackContainer(self._chat_frame)
        self._global_left_stack.setObjectName("globalLeftStack")
        self._global_right_stack = CardStackContainer(self._chat_frame)
        self._global_right_stack.setObjectName("globalRightStack")

        # 停靠区侧 wrapper：默认收起，子控件 visibility 联动 wrapper 与 splitter 大小
        # （避免无卡片时 splitter 均分空间显示空白 splitter handle）
        self._global_left_wrapper = _DockSideWrapper(
            self._global_left_container, self._global_left_stack, self._chat_frame
        )
        self._global_left_wrapper.setObjectName("globalLeftWrapper")
        self._global_left_stack.set_container_context(GLOBAL_WINDOW_ID, ContainerType.LEFT)
        self._global_left_container.set_stack_sibling(self._global_left_stack)

        self._global_right_wrapper = _DockSideWrapper(
            self._global_right_container, self._global_right_stack, self._chat_frame
        )
        self._global_right_wrapper.setObjectName("globalRightWrapper")
        self._global_right_stack.set_container_context(GLOBAL_WINDOW_ID, ContainerType.RIGHT)
        self._global_right_container.set_stack_sibling(self._global_right_stack)

        self._dock_splitter.addWidget(self._global_left_wrapper)
        self._dock_splitter.addWidget(self._chat_vsplitter)
        self._dock_splitter.addWidget(self._global_right_wrapper)
        self._dock_splitter.setStretchFactor(0, 0)  # 左停靠区不随窗口拉伸
        self._dock_splitter.setStretchFactor(1, 1)  # 内容区(含覆盖层)吃掉多余空间
        self._dock_splitter.setStretchFactor(2, 0)  # 右停靠区不随窗口拉伸
        self._dock_splitter.setHandleWidth(6)
        # 折叠依赖轴向 max=0 约束而非用户拖拽收起，禁止拖拽塌陷
        self._dock_splitter.setChildrenCollapsible(False)
        # wrapper 关联 splitter：联动 setSizes 维持收起态
        self._global_left_wrapper.attach_to_splitter(self._dock_splitter, 0)
        self._global_right_wrapper.attach_to_splitter(self._dock_splitter, 2)
        # ── full 容器卡片标题栏 tab（替代原 ReplaceTabBar）──
        # full 卡片（UI 插件 full 卡 + 内置全局卡）打开时在标题栏 tab 区显示
        # （带 × 关闭钮，见 _on_card_visibility_changed）。这里仅订阅显隐事件。
        from app.core.ui_event_bus import EV_CARD_VISIBILITY_CHANGED, UIEventBus

        # 订阅 full 卡片显隐事件，同步标题栏 tab open 集合与显隐
        UIEventBus.get_instance().subscribe(EV_CARD_VISIBILITY_CHANGED, self._on_card_visibility_changed)

        chat_frame_layout.addWidget(self._dock_splitter, 1)

        # 全局容器启用停靠模式
        self._global_left_container.enable_dock_mode(self._dock_splitter)
        self._global_right_container.enable_dock_mode(self._dock_splitter)
        # TOP 容器处于覆盖层模式，不启用 dock mode
        self._global_bottom_container.enable_dock_mode(self._chat_vsplitter)

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

        # 固定默认面板宽度（_DEFAULT_PANEL_WIDTH + 14px 补偿 #tabFrame margins/border），不记忆
        frame_w = _DEFAULT_PANEL_WIDTH + 14
        self._splitter.setSizes([frame_w, max(0, self.width() - frame_w)])

        # 恢复侧边栏收起状态（必须在 splitter sizes 设置之后执行）
        QTimer.singleShot(0, self._restore_sidebar_collapsed)

        # 应用样式（使用 _apply_theme_stylesheet 以确保 objectName 选择器生效）
        self._apply_theme_stylesheet()

        # 工作区页面宿主（Phase G）：插件 register_workspace_page 注册的主页面
        from app.widgets.workspace_page_host import WorkspacePageHost

        self._workspace_page_host = WorkspacePageHost()
        self._workspace_page_host.attach_to(self)

    def _setup_signals(self):
        self._tab_panel.tabSelected.connect(self._on_tab_selected)
        self.activeTabChanged.connect(self._on_active_tab_changed)
        self._tab_panel.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_panel.tabBranchRequested.connect(self._on_tab_branch_requested)
        self._tab_panel.newTabRequested.connect(self._on_new_tab_requested)
        self._tab_panel.sidebarToggled.connect(self._on_sidebar_toggled)
        self._tab_panel.teamCloseRequested.connect(self._on_team_close_requested)
        self._tab_panel.teamAddMemberRequested.connect(self._on_team_add_member_requested)
        self._tab_panel.teamNewTaskRequested.connect(self._on_team_new_task_requested)
        # 工作区树模式：在项目 + 工作树下新建对话页 / 打开历史会话
        self._tab_panel.newSessionInWorkspaceRequested.connect(self._on_new_session_in_workspace)
        self._tab_panel.openSessionRecordRequested.connect(self._on_open_session_record)
        # Tab 增删后同步工作区树（会话归属/历史列表会变）
        self.tabCountChanged.connect(self._on_tab_count_changed_for_tree)
        # 用户手动拖拽 splitter 把手 → 折叠是用户主动，不标记挤压，
        # 关闭卡片/空间恢复时不得自动展开（尊重手动意图）
        if hasattr(self, "_splitter"):
            self._splitter.splitterMoved.connect(self._on_splitter_manually_moved)

        # macOS 软置顶：应用激活时补抬升（详见 _on_app_state_changed）
        if _IS_MAC:
            QApplication.instance().applicationStateChanged.connect(self._on_app_state_changed)

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
                # （固定默认 _DEFAULT_PANEL_WIDTH）作为恢复目标，保证展开后宽度可读；
                # 用户手动折叠时宽度正常，照常保存实际宽度。
                if cur_frame_w < self._tab_panel._auto_collapse_width:
                    saved_w = _DEFAULT_PANEL_WIDTH
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

        # 持久化收起状态已移除：不做记忆，侧边栏收起/展开只在本次会话内生效
        if not animate:
            # 启动恢复等瞬时路径：直接 setSizes
            self._splitter.setSizes(self._splitter_sizes_with_left(target_w))
            return

        # 平滑动画过渡
        if abs(cur_w - target_w) < 3:
            # 距离过近（如拖拽已到位）：瞬时落位，省一次空动画
            self._splitter.setSizes(self._splitter_sizes_with_left(target_w))
            if hasattr(self, "_tab_panel"):
                self._tab_panel.sync_collapsed_ui()
            return

        # 平滑动画过渡
        self._start_sidebar_anim(cur_w, target_w, collapsing=collapsed)

    def _start_sidebar_anim(self, start_w: int, end_w: int, collapsing: bool):
        """启动侧边栏宽度动画（200ms OutCubic）"""
        # ── #31 中间对话区原样实时显示：不再冻结 _content_area 重绘 ──
        # 改为抑制卡片进入 resize 预览模式（WebView 保持可见），动画期
        # 每帧 restart 的宽度同步防抖在动画结束后一次执行。
        self._set_windows_resize_preview_suppressed(True)
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
        # ★ 必须三值：缺工作台窗格会被 QSplitter 压到 0/最小宽度（见 helper 注释）
        self._splitter.setSizes(self._splitter_sizes_with_left(w))

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
        try:
            if hasattr(self, "_tab_panel"):
                self._tab_panel.set_animating(False)
            # 最终宽度精确落位（插值收尾可能差 1px）
            # ★ 三值 setSizes：保留工作台宽度，否则折叠左侧会连带清掉右侧
            if self._sidebar_anim_collapsing:
                target_w = self._tab_panel._collapsed_min_width + 14
            else:
                target_w = getattr(self, "_saved_panel_frame_width", 250)
                target_w = max(_EXPANDED_MIN_FRAME_WIDTH, target_w)
            self._splitter.setSizes(self._splitter_sizes_with_left(target_w))
            # UI 最终状态兜底（动画中途未跨阈值时强制同步，如目标宽度恰为阈值）
            if hasattr(self, "_tab_panel"):
                self._tab_panel.sync_collapsed_ui()
            # 折叠动画结束：布局已稳定，若折叠是"挤压所致"（_collapsed_by_squeeze），
            # 补一次空间恢复检测——覆盖"折叠动画期间窗口已拉宽"的时序缺口
            # （resize 结束检测在动画中会被 _animating 跳过）。
            if self._sidebar_anim_collapsing:
                self._maybe_auto_expand_after_squeeze()
        finally:
            # ── #31 动画结束恢复卡片 resize 预览能力 ──
            # 若本回调又同步启动了新动画（_maybe_auto_expand 走 _start_sidebar_anim
            # 重新抑制），则交由其 _on_sidebar_anim_finished 恢复，此处跳过。
            if not (self._sidebar_anim is not None and self._sidebar_anim.state() == QVariantAnimation.Running):
                self._set_windows_resize_preview_suppressed(False)

    def _evaluate_squeeze_collapse(self) -> bool:
        """按稳定后的几何判定"侧边栏确实被挤压"→ 自动折叠

        背景：resize 周期内（尤其 _deferred_resize_complete 的 _force_relayout
        全量重算）左面板宽度会瞬时跌到折叠阈值（100px）以下。TabPanel.resizeEvent
        只看宽度，会把最大化/还原、覆盖层 relayout 这类几何瞬变误判为"用户
        把面板拖窄"而折叠；而折叠后窗口总宽往往不增反减，自动展开的相对增长
        条件（≥ 折叠时总宽 + _AUTO_EXPAND_GROWTH）永不满足 → 折叠态永久残留。
        因此 resize 周期内置 _auto_collapse_suppressed 抑制该判定，改在这里
        基于收拢后的真实几何决定。

        判定标准（"被挤压"）：左面板最终宽度低于折叠阈值，且窗口总宽已放不下
        「常规展开宽度 + 聊天区最小可用宽度」。空间其实够的（纯 relayout 瞬时
        压窄）保持展开，不折叠。

        Returns:
            True 表示本次触发了折叠（调用方应跳过随后的自动展开检测，
            否则刚折叠就可能被 _maybe_auto_expand_after_squeeze 弹回）。
        """
        panel = getattr(self, "_tab_panel", None)
        if panel is None or panel._collapsed or panel._animating:
            return False
        if not hasattr(self, "_splitter") or self._splitter.count() == 0:
            return False
        try:
            sizes = self._splitter.sizes()
        except Exception:
            return False
        if not sizes:
            return False
        total = sum(sizes)
        left = sizes[0]
        if total <= 0 or left >= panel._auto_collapse_width:
            return False  # 宽度正常：没有挤压，无需折叠
        # 空间仍放得下"常规展开宽度 + 聊天区最小宽度" → 只是瞬时压窄，不折叠：
        # 把左面板恢复到常规展开宽度，避免停留在被压扁的窄条上。
        needed = max(_EXPANDED_MIN_FRAME_WIDTH, getattr(self, "_saved_panel_frame_width", 250))
        if total >= needed + _MIN_CHAT_WIDTH:
            tab_frame = getattr(self, "_tab_frame", None)
            cap = tab_frame.maximumWidth() if tab_frame is not None else needed
            frame_w = max(0, min(needed, cap))
            if frame_w > 0 and frame_w != left:
                self._splitter.setSizes(self._splitter_sizes_with_left(frame_w))
                panel.sync_collapsed_ui()
            return False
        # 确实被挤压：交接给折叠动画（与 TabPanel.resizeEvent 折叠路径一致）
        panel._collapsed = True
        panel._collapsed_by_squeeze = True
        panel._update_toggle_button()
        QTimer.singleShot(0, lambda: self._on_sidebar_toggled(True))
        return True

    def _maybe_auto_expand_after_squeeze(self, growth_required: bool = True, _retried: bool = False):
        """挤压折叠后空间恢复：自动展开回常规宽度

        窗口主动拉宽（growth_required=True）不受挤压标记限制：用户拉宽窗口
        即视为想要展开，点击折叠按钮/拖窄把手手动折叠后拉宽也退出折叠。
        仅 relayout/关闭卡片恢复（growth_required=False）要求挤压标记，
        避免把用户手动折叠的面板被动撑开（尊重手动意图）。

        触发点：窗口 resize 结束、overlay 卡片关闭、折叠动画结束。
        空间判定（两条件都满足才展开）：
        1. 相对增长（growth_required=True）：当前窗口总宽 ≥ 折叠时总宽 + 200。
           防止"折叠刚完成绝对条件恰满足就弹回"——窗口只缩窄到 900 左右折叠
           时，绝对空间（900-60 ≥ 展开宽+400）仍满足，若只看绝对条件会立刻
           弹回展开，折叠态无法保持。仅当窗口比折叠时明显更宽（有新增空间）
           才自动展开，语义即"再有剩余空间时自动展开"。
        2. 绝对下限：窗口总宽 - 折叠宽 ≥ 展开目标宽 + 聊天区最小可用宽(400)，
           展开后面板与聊天区都放得下。

        动画时序兜底：若检测时折叠/展开动画仍在进行（_animating），延迟 250ms
        重试一次（动画 200ms 后必然结束），避免"用户快速开关卡片 → 折叠动画
        未结束 → 检查被跳过 → 折叠态残留"的恢复丢失。
        """
        if not hasattr(self, "_tab_panel"):
            return
        panel = self._tab_panel
        if not panel._collapsed:
            return
        # 窗口主动拉宽(growth_required=True)不受 _collapsed_by_squeeze 限制：
        # 手动折叠(点按钮/拖窄把手)后用户拉宽窗口也应退出折叠。仅 relayout/
        # 关闭卡片恢复(growth_required=False)要求挤压标记，避免被动撑开。
        if not growth_required and not panel._collapsed_by_squeeze:
            return
        if panel._animating:
            # 动画中：延迟重试一次（等动画结束，覆盖快速开关卡片的时序缺口）
            if not _retried:
                QTimer.singleShot(
                    250,
                    lambda: self._maybe_auto_expand_after_squeeze(growth_required=growth_required, _retried=True),
                )
            return
        total = sum(self._splitter.sizes()) if hasattr(self, "_splitter") else self.width()
        target_w = max(_EXPANDED_MIN_FRAME_WIDTH, getattr(self, "_saved_panel_frame_width", 250))
        chat_min = _MIN_CHAT_WIDTH  # 聊天区最小可用宽度
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
        """启动时固定侧边栏为展开态 + 默认宽度（不恢复配置记忆）"""
        if not hasattr(self, "_splitter"):
            return
        # 始终展开 + 默认宽度。背景：_setup_ui 里 setSizes 在窗口未显示时调用，
        # show 后首次 relayout 按 stretch/sizeHint 重新分配，左面板会被压到
        # 最小宽度（< _auto_collapse_width，实测 46~60px），TabPanel.resizeEvent
        # 误判为"用户拖窄"自动折叠；欢迎卡片懒渲染（QWebEngineView 创建）还会
        # 引发后续 relayout 再次压缩。因此在启动早期多轮补射恢复（时间递增，
        # 覆盖 2~3 次 relayout 窗口期，直到布局不再弹跳），期间均以默认宽度为准。
        self._apply_restored_panel_width()
        for delay in (80, 200, 400, 700):
            QTimer.singleShot(delay, self._apply_restored_panel_width)

    def _apply_restored_panel_width(self):
        """按默认宽度恢复左面板宽度 + 解除启动误折叠（启动兜底）"""
        if not hasattr(self, "_splitter") or self._splitter.count() == 0:
            return
        saved_w = _DEFAULT_PANEL_WIDTH
        frame_w = max(_EXPANDED_MIN_FRAME_WIDTH, saved_w + 14)
        sizes = self._splitter.sizes()
        total = sum(sizes) if sizes else self.width()
        if total <= frame_w:
            return
        # 仅当前宽度明显小于默认宽度时才恢复（避免覆盖用户手动拖宽）
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
        # 覆盖层切换会改变 wrapper 限宽策略（page=1 取消限宽，page=0 恢复限宽），
        # wrapper resize 事件不会因此重发，立即同步一次让 margins 即时生效。
        QTimer.singleShot(0, self._sync_chat_wrapper_width)

    # ── 替换类型(full 容器)卡片顶部居中切换栏 ──

    def _on_card_visibility_changed(self, payload: Dict[str, Any]) -> None:
        """同步 replace 卡片（UI 插件 full 卡 + 内置全局卡）显隐到标题栏 tab 区

        仅处理覆盖对话区的 replace 卡片：container=="full" 浮动卡，或已知全局卡
        （settings/diff_viewer/sub_agent_session 等）；其余卡片忽略。
        shown → 加入 open 集合并高亮（标题栏 tab 带 × 关闭钮）；hidden → 120ms 去抖区分
        「互斥切换」与「用户关闭」：窗口内仍有其他 replace 卡片可见则为切换（保留），否则移除。
        """
        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

        card_id = payload.get("card_id")
        visible = payload.get("visible", False)
        if not card_id or card_id == CHAT_TAB_ID:
            return
        # 仅处理全局对话区（GLOBAL_WINDOW_ID）作用域的 replace 卡片
        if payload.get("window_id") and payload.get("window_id") != GLOBAL_WINDOW_ID:
            return

        # 解析 replace 卡片与标题：full 浮动卡优先取注册标题，否则取已知全局卡标题
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        info = UIPluginRegistry.get_instance().get_floating_cards().get(card_id)
        if info is not None:
            if info.container != "full":
                return
            title = info.title
        elif card_id in KNOWN_GLOBAL_REPLACE_CARDS:
            title = GLOBAL_REPLACE_TITLES.get(card_id, card_id)
        else:
            return

        cur_wid = self._current_window_id()
        if visible:
            # 取消该卡片的关闭计时（若是互斥切换后的恢复）
            self._cancel_replace_timer(card_id)
            # 卡片按「事件到达时的当前活跃对话」归属到对应窗口的 open 集合
            open_dict = self._replace_open.setdefault(cur_wid, OrderedDict())
            if card_id not in open_dict:
                open_dict[card_id] = title
                self.titleBar.add_tab(card_id, title, closable=True)
            self._set_replace_active(card_id)
        else:
            if getattr(self, "_suppress_replace_close", False):
                # 点「聊天」主动隐藏 replace 卡片：保留 open（标题栏 tab 常驻聊天项），仅取消其关闭计时
                self._cancel_replace_timer(card_id)
            else:
                # 120ms 去抖：紧接的 shown(其他) 会保留本卡片；无则视为关闭
                self._schedule_replace_close(card_id)
        # 覆盖层互斥切换（如 settings ↔ diff_viewer）stack 页不变、wrapper 不 resize，
        # 限宽策略随可见卡片类型变化，主动刷新一次 pad
        if self._content_stack.currentIndex() == 1:
            QTimer.singleShot(0, self._sync_chat_wrapper_width)
        # 卡片被隐藏后高亮若还停在它身上，原本要等 120ms 去抖走完才纠正；
        # 这里额外插一次 0ms 收敛，让高亮即时跟随真实可见性。
        self._schedule_replace_highlight()

    def _schedule_replace_close(self, card_id: str) -> None:
        """启动/重置某卡片的关闭去抖计时器（区分切换与关闭）"""
        timer = self._replace_timers.get(card_id)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._on_replace_close_timeout(card_id))
            self._replace_timers[card_id] = timer
        timer.start(120)

    def _cancel_replace_timer(self, card_id: str) -> None:
        timer = self._replace_timers.pop(card_id, None)
        if timer is not None:
            timer.stop()

    def _on_replace_close_timeout(self, card_id: str) -> None:
        self._replace_timers.pop(card_id, None)
        if not self._has_other_visible_full(card_id):
            # 卡片是全局单例：从所有对话的 open 集合中移除（关闭后任何对话都不应再显示）
            owners = [wid for wid, od in self._replace_open.items() if card_id in od]
            for wid in owners:
                del self._replace_open[wid][card_id]
                if self._replace_active.get(wid) == card_id:
                    self._replace_active[wid] = None
            # tab 栏仅反映当前对话：仅当卡片归属当前对话时改 tab 栏
            if owners:
                self.titleBar.remove_tab(card_id)
                # 关掉当前卡片且 open 仍有其他 full 卡片 → 自动激活（互斥显示）最近一个
                self._activate_remaining_replace_card()

    def _activate_remaining_replace_card(self) -> None:
        """当前对话 open 集合非空时，自动激活（互斥显示）最近一个 full 卡片，避免关掉
        当前后剩余卡片停在隐藏态（无论关闭来自 tab × 还是卡片自身按钮）。

        🛡️ 常驻 titlebar tab（插件 register_titlebar_tab 注册，如轨迹卡：常驻 tab
        与 full 浮动卡共用同一 card_id）不属于「临时可关闭 tab」：从剩余集合中
        排除，不自动激活——关闭最后一个临时 tab 后回到聊天，而不是自动弹出
        常驻插件的页面（用户需要时手动点击其常驻 tab）。
        """
        cur_wid = self._current_window_id()
        remaining = [cid for cid in self._replace_open.get(cur_wid, {}) if cid not in self._plugin_titlebar_tab_ids]
        if not remaining:
            # 只剩常驻插件的 full 卡（或全部关闭）：高亮/状态回聊天。
            # remove_tab 移除激活 tab 时已把高亮设为剩余第一个（聊天），此处
            # 同步 _replace_active 语义并幂等设高亮（防御 tab 注册顺序变化）。
            self._replace_active[cur_wid] = CHAT_TAB_ID
            self.titleBar.set_active_tab(CHAT_TAB_ID)
            return
        nid = remaining[-1]
        from app.widgets.cards.card_manager import CardManager, GLOBAL_WINDOW_ID

        cm = CardManager.get_instance()
        wid = getattr(self, "_window_id", None) or GLOBAL_WINDOW_ID
        if cm is not None and cm.is_card_visible(nid, wid):
            self._set_replace_active(nid)
            return
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        UIPluginRegistry.get_instance().toggle_floating_card(nid)

    def _has_other_visible_full(self, exclude_id: str) -> bool:
        """窗口内是否有其他 replace 卡片当前可见（用于区分切换/关闭）"""
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
        from app.widgets.cards.card_manager import CardManager, GLOBAL_WINDOW_ID

        reg = UIPluginRegistry.get_instance()
        cm = CardManager.get_instance()
        wid = getattr(self, "_window_id", None) or GLOBAL_WINDOW_ID
        # 其他可见的 full 浮动卡片
        for cid, info in reg.get_floating_cards().items():
            if cid == exclude_id or info.container != "full":
                continue
            if cm.is_card_visible(cid, wid):
                return True
        # 其他可见的内置全局替换卡片
        for cid in KNOWN_GLOBAL_REPLACE_CARDS:
            if cid == exclude_id:
                continue
            if cm.is_card_visible(cid, wid):
                return True
        return False

    def _set_replace_active(self, card_id: str) -> None:
        self._replace_active[self._current_window_id()] = card_id
        self.titleBar.set_active_tab(card_id or CHAT_TAB_ID)

    def _is_replace_visible(self, card_id: str) -> bool:
        """replace 卡片在当前窗口是否**真实**可见（权威判定）

        走 CardManager 的 ``visible_cards`` 快照而非任何本地缓存标志：
        ``show_card`` / ``hide_card`` 都是在发布显隐事件**之前**更新它，
        所以在事件回调里读到的就是最新值。
        """
        try:
            from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID, CardManager

            cm = CardManager.get_instance()
            if cm is None:
                return False
            wid = getattr(self, "_window_id", None) or GLOBAL_WINDOW_ID
            return bool(cm.is_card_visible(card_id, wid))
        except Exception:
            return False

    def _sync_replace_highlight(self) -> None:
        """按卡片真实可见状态纠正标题栏高亮（自愈收敛点）

        ★ 背景：高亮原本由 6 处命令式写入（显隐事件 / tab 点击 / 关闭去抖 /
        切标签页 / tab × / 插件常驻 tab）各自维护，任何一处时序错位都会留下
        「高亮停在已经关掉的 tab 上」。这里改成从事后可见性反推并纠正。

        只做**降级**：高亮所指的 tab 已被移除、或对应卡片已不可见时，退回
        当前仍可见的最后一个，都没有则回「对话」。

        刻意不做升级——显示卡片必经显隐事件，那里已经同步置活；若在此处
        反向提升，``_show_conversation_view`` 里被静默吞掉的 hide_card 失败
        会把用户刚点下的「对话」又顶回卡片。
        """
        cur_wid = self._current_window_id()
        open_dict = self._replace_open.get(cur_wid, {})
        active = self._replace_active.get(cur_wid)
        if not active or active == CHAT_TAB_ID:
            return
        tabs = getattr(self.titleBar, "_tabs", {})
        if active in open_dict and active in tabs and self._is_replace_visible(active):
            return
        visibles = [cid for cid in open_dict if cid in tabs and self._is_replace_visible(cid)]
        target = visibles[-1] if visibles else CHAT_TAB_ID
        self._replace_active[cur_wid] = target
        self.titleBar.set_active_tab(target)

    def _schedule_replace_highlight(self) -> None:
        """合并同一事件循环内的高亮重算请求（延迟一拍等卡片状态落定）"""
        if self._replace_highlight_pending:
            return
        self._replace_highlight_pending = True
        QTimer.singleShot(0, self._run_replace_highlight)

    def _run_replace_highlight(self) -> None:
        self._replace_highlight_pending = False
        self._sync_replace_highlight()

    def _current_window_id(self) -> str:
        """当前活跃对话标签页的 window_id（用于隔离 replace tab 栏状态）"""
        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

        win = self.get_current_window()
        wid = getattr(win, "_window_id", None)
        return wid or GLOBAL_WINDOW_ID

    def _refresh_titlebar_cards(self) -> None:
        """按当前活跃对话标签页重建标题栏 full 卡片 tab（打开列表 + 高亮独立）

        全量同步：移除不在 open 集合的卡片 tab（保留「聊天」与插件常驻 tab），
        补齐缺失项，最后同步高亮。"""
        cur_wid = self._current_window_id()
        open_dict = self._replace_open.get(cur_wid, {})
        active = self._replace_active.get(cur_wid)
        for cid in list(self.titleBar._tabs.keys()):
            if cid != CHAT_TAB_ID and cid not in self._plugin_titlebar_tab_ids and cid not in open_dict:
                self.titleBar.remove_tab(cid)
        for cid, title in open_dict.items():
            if cid not in self.titleBar._tabs:
                self.titleBar.add_tab(cid, title, closable=True)
        self.titleBar.set_active_tab(active or CHAT_TAB_ID)

    def _on_active_tab_changed(self, index: int) -> None:
        """切换对话标签页 → 同步覆盖层卡片显隐 + 重建标题栏 full 卡片 tab"""
        self._sync_overlay_cards_to_active_window()
        self._refresh_titlebar_cards()

    def _sync_overlay_cards_to_active_window(self) -> None:
        """覆盖层全局卡按目标对话标签页恢复显隐

        全局 replace 卡片是单例（对话间共享显示权），切换标签页时按目标对话的
        open/active 状态恢复：active 卡显示、其余可见卡隐藏；目标对话停留在
        「聊天」视图或无打开卡片时隐藏全部。隐藏走 _suppress_replace_close
        保护，open 集合保留（标题栏 tab 仍可点回）。
        """
        from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID, CardManager

        cm = CardManager.get_instance()
        if cm is None:
            return
        wid = getattr(self, "_window_id", None) or GLOBAL_WINDOW_ID
        cur_wid = self._current_window_id()
        open_ids = self._replace_open.get(cur_wid, {})
        active = self._replace_active.get(cur_wid)
        if active == CHAT_TAB_ID:
            active = None
        if active is not None and active not in open_ids:
            active = None

        # 收集所有候选 replace 卡（内置全局卡 + full 浮动卡）
        candidates = set(KNOWN_GLOBAL_REPLACE_CARDS)
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            for cid, info in UIPluginRegistry.get_instance().get_floating_cards().items():
                if info.container == "full":
                    candidates.add(cid)
        except Exception:
            pass

        self._suppress_replace_close = True
        try:
            # 切换投影保护：此期间 hide/show 是「per-tab 状态投影」（临时隐藏，
            # open 集合保留），registry 的 on_card_hidden 回调必须跳过 per-tab
            # 可见集合清除——否则卡片从集合丢失后，sync_floating_cards_to_tab
            # 会因 want=False & now=True 误执行 hide_card，触发 120ms 关闭去抖
            # 判定，导致 full 卡片切走再切回时被误关。
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            with UIPluginRegistry.get_instance().tab_sync_guard():
                for cid in candidates:
                    try:
                        if not cm.is_card_visible(cid, wid):
                            continue
                    except Exception:
                        continue
                    if cid != active:
                        try:
                            cm.hide_card(cid, wid)
                        except Exception:
                            pass
                if active is not None:
                    try:
                        cm.show_card(active, wid)
                    except Exception:
                        pass
        finally:
            self._suppress_replace_close = False

    def _on_replace_tab_clicked(self, card_id: str) -> None:
        """点 full 卡片 tab → 切换/显示对应卡片；点当前 active 不动作"""
        from app.widgets.cards.card_manager import CardManager, GLOBAL_WINDOW_ID

        if card_id == self._replace_active.get(self._current_window_id()):
            return
        if card_id in KNOWN_GLOBAL_REPLACE_CARDS:
            # 内置全局卡：裸 show_card 不自动互斥，需先隐藏其他 replace 卡再恢复显示（内容保留）
            cm = CardManager.get_instance()
            wid = getattr(self, "_window_id", None) or GLOBAL_WINDOW_ID
            cur_wid = self._current_window_id()
            for other in list(self._replace_open.get(cur_wid, {}).keys()):
                if other != card_id:
                    try:
                        cm.hide_card(other, wid)
                    except Exception:
                        pass
            try:
                cm.show_card(card_id, wid)
            except Exception:
                pass
            self._set_replace_active(card_id)
        else:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            UIPluginRegistry.get_instance().toggle_floating_card(card_id)
            # ★ toggle 是「取反」语义：卡片原本可见时这次调用是把它藏起来。
            # 早期版本在这里无条件置活，于是出现「tab 亮着、卡片其实已关」。
            # 显示分支会经显隐事件走到 _set_replace_active，这里只补一次收敛。
            self._schedule_replace_highlight()

    def _show_conversation_view(self) -> None:
        """点「聊天」→ 隐藏所有 replace 卡片回到对话区，但保留 open（标题栏 tab 常驻聊天项，可随时切回）"""
        from app.widgets.cards.card_manager import CardManager, GLOBAL_WINDOW_ID

        cm = CardManager.get_instance()
        wid = getattr(self, "_window_id", None) or GLOBAL_WINDOW_ID
        # 抑制 hide_card 触发的 visible=False 事件（对话视图下隐藏 replace 卡片是预期行为，open 保留）
        self._suppress_replace_close = True
        cur_wid = self._current_window_id()
        try:
            for cid in list(self._replace_open.get(cur_wid, {}).keys()):
                try:
                    cm.hide_card(cid, wid)
                except Exception:
                    pass
        finally:
            self._suppress_replace_close = False
        self._replace_active[cur_wid] = CHAT_TAB_ID
        self.titleBar.set_active_tab(CHAT_TAB_ID)

    def _on_replace_tab_close_clicked(self, card_id: str) -> None:
        """点 tab × → 真正关闭对应卡片并从 open 移除 tab 项（同 close_replace_card）"""
        self.close_replace_card(card_id)

    def close_replace_card(self, card_id: str) -> None:
        """真正关闭一张 replace 卡片并从 open 移除 tab 项（公共关闭入口）

        tab × 与「卡片内部关闭按钮」共用：卡片内部关闭（SystemCardFrame.closed）同样
        意味着用户关闭该卡片，必须移除 tab；若仅依赖 hide 事件的 120ms 去抖，
        会因其他 replace 卡片（如 settings）可见而被误判为互斥切换导致 tab 残留。

        内置全局卡（settings/diff_viewer 等）经 CardManager.hide_card(GLOBAL_WINDOW_ID)
        真正隐藏；full 浮动卡经 registry.hide_floating_card_globally 隐藏（该 API 仅对
        注册在 UI 插件注册表的浮动卡有效，对内置全局卡返回 False 不生效）。
        """
        from app.widgets.cards.card_manager import CardManager, GLOBAL_WINDOW_ID

        # 卡片是全局单例：从所有对话的 open 集合中移除并清 active（避免切回其他对话仍显示已关卡片）
        cur_wid = self._current_window_id()
        owners = [wid for wid, od in self._replace_open.items() if card_id in od]
        for wid in owners:
            del self._replace_open[wid][card_id]
            if self._replace_active.get(wid) == card_id:
                self._replace_active[wid] = None
        if owners:
            self.titleBar.remove_tab(card_id)

        # 真正隐藏卡片本身（避免「tab 消失但卡片仍显示」）
        if card_id in KNOWN_GLOBAL_REPLACE_CARDS:
            # 内置全局卡：经 CardManager 隐藏（hide_floating_card_globally 对非浮动卡无效）
            try:
                CardManager.get_instance().hide_card(card_id, GLOBAL_WINDOW_ID)
            except Exception:
                pass
        else:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            try:
                UIPluginRegistry.get_instance().hide_floating_card_globally(card_id)
            except Exception:
                pass

        if self._replace_open.get(cur_wid):
            self._activate_remaining_replace_card()
        # 关掉的是当前高亮项时，_activate_remaining_replace_card 未必命中
        # （例如剩余项都是常驻插件 tab），补一次收敛兜底
        self._schedule_replace_highlight()

    def _on_splitter_idle(self):
        """splitter 拖拽防抖超时：松手后恢复内容区绘制 + 解除 TabPanel 节流

        ~120ms 无新 splitterMoved 即视为停手/松手，由 _splitter_idle_timer
        触发。合并恢复 _content_area.setUpdatesEnabled(True) 与
        _tab_panel.set_resizing(False)，复位拖拽首帧标记供下次拖拽重新冻结。
        """
        self._splitter_dragging = False
        # ── #14 收尾：折叠/展开动画仍运行则跳过解冻，交 #4 动画 finally 统一恢复 ──
        # 防「拖拽跨折叠阈值触发动画 + 120ms idle timer 提前解冻」极端路径尾段
        # 额外 WebView 重绘。动画收尾由 _on_sidebar_anim_finished(try/finally) 恢复
        # _content_area(setUpdatesEnabled True) 与 _set_cards_resize_preview_mode(False)。
        # _splitter_dragging 已先行复位，后续 splitterMoved 可重新冻结下一拖拽会话。
        if self._sidebar_anim is not None and self._sidebar_anim.state() == QVariantAnimation.Running:
            return
        if hasattr(self, "_content_area"):
            self._content_area.setUpdatesEnabled(True)
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_resizing(False)
        # ── #10 对齐恢复：与解冻同步显式恢复 WebView 预览（幂等） ──
        # 冻结期 MainWidget.resizeEvent 已自动 _set_cards_resize_preview_mode(True)
        # 隐藏 WebView，此处与 setUpdatesEnabled(True) 同步恢复，消除潜在空窗。
        mw = self._content_area.currentWidget() if hasattr(self, "_content_area") else None
        if mw is not None and hasattr(mw, "_set_cards_resize_preview_mode"):
            mw._set_cards_resize_preview_mode(False)

    def _on_splitter_manually_moved(self, pos: int, index: int):
        """用户手动拖拽 splitter 把手：清除挤压折叠标记 + 手动拖宽显式展开

        挤压折叠标记（_collapsed_by_squeeze）仅用于"被外部 relayout 压窄
        自动折叠后自动展开"场景。用户手动拖动把手瘦身到折叠阈值以下，
        视为手动意图——不自动展开，避免关闭卡片/窗口拉宽时把用户拖窄的
        面板再撑开。

        手动拖宽超过展开阈值时显式展开：TabPanel.resizeEvent 的拖宽展开
        已加挤压标记守卫（仅被动挤压折叠允许布局恢复自动展开），手动拖
        把手拉开必须在此处理——splitterMoved 晚于 resizeEvent 触发，此时
        面板宽度已就位，直接按拖到的宽度展开，无时序竞态。
        """
        if index != 0 or not hasattr(self, "_tab_panel"):
            return
        panel = self._tab_panel
        # 手动拖拽：清除挤压折叠标记（无论方向，尊重手动意图）
        panel._collapsed_by_squeeze = False
        # ── P0 性能优化 OPT-2：拖拽首帧冻结 _content_area 重绘，防抖 timer 松手恢复 ──
        # 仅首帧冻结一次（_splitter_dragging 未置位时）；_tab_panel 全程保持启用，
        # 侧栏 UI 切换/展开逻辑仍须响应。每次 splitterMoved 重置 ~120ms 防抖 timer，
        # 松手后由 _on_splitter_idle 统一恢复绘制，抑制每帧全量重绘/reflow 致卡顿。
        if not self._splitter_dragging:
            self._splitter_dragging = True
            if hasattr(self, "_content_area"):
                self._content_area.setUpdatesEnabled(False)
        if hasattr(self, "_splitter_idle_timer"):
            self._splitter_idle_timer.start()
        # 手动拖宽超过展开阈值（滞回区 110+）：显式展开
        if panel._collapsed and not panel._animating and pos >= panel._auto_collapse_width + 10:
            panel._collapsed = False
            panel._update_toggle_button()
            # 延迟发射，避免在拖拽链中直接嵌套 setSizes
            QTimer.singleShot(0, lambda: self._on_sidebar_toggled(False))

    # ── 窗口管理 ──

    def begin_suppress_add_activate(self):
        """批量添加窗口期间抑制 add_window 自动激活新 tab（保持当前焦点）。

        配合 MainWidget._spawn_team_members(keep_current_active=True)：
        工作室等管理入口批量建成员时不打断用户当前所在 tab。
        支持嵌套（内部计数），必须与 end_suppress_add_activate 成对使用。
        """
        self._suppress_add_activate = getattr(self, "_suppress_add_activate", 0) + 1

    def end_suppress_add_activate(self):
        """结束抑制（仅在最外层计数归零后恢复 add_window 自动激活）"""
        depth = getattr(self, "_suppress_add_activate", 0)
        self._suppress_add_activate = max(0, depth - 1)

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

            # 监听 AI 状态变化（流式/错误/提问 → Tab 边框指示 + 全局桌宠）
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
                # 聚合 AI 状态 → 全局桌宠：仅当前激活窗的状态被转发，
                # 避免多 tab 下旧窗状态串扰（pet 只连一次 active_ai_state_changed）。
                if _win is self.get_current_window():
                    self.active_ai_state_changed.emit(state)

            window._tab_ai_state_slot = _on_ai_state_changed
            window.ai_state_changed.connect(_on_ai_state_changed)

            # ★ destroyed 兜底：窗体被 Qt 直接销毁（绕过 _close_window_at，
            # 如 cleanup() 的 w.close() 路径）时由 _on_window_destroyed 断开
            # 上述闭包连接，打破 __defaults__ 对 window 的引用环，防止整树泄漏。
            # _close_window_at 已覆盖常规关闭路径（close 前显式断开）。
            window.destroyed.connect(self._on_window_destroyed)

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

            # 隐藏空状态页；激活新窗口——抑制模式下（批量建成员保持焦点）
            # 且已有激活内容窗口时跳过，避免逐窗跳 tab
            stack.widget(0).hide()
            if getattr(self, "_suppress_add_activate", 0) <= 0 or self._tab_panel.active_index < 0:
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

        数据源**必须为按 run_id 粒度的团队级 project**（TeamManager.
        get_project_for_run_id）：tab_panel 团队框以 run_id（uuid）分组，
        用旧 get_team_project(team_name=DEFAULT_TEAM) 会读到所有团队共享
        的 DEFAULT_TEAM.project，导致多团队并存时 ``后建团队切项目 → 之前
        团队框 header icon 被覆盖`` 的 bug（#5a-fix Plan C）。

        团队级 project 为空（团队尚未统一设置项目）时：回退 fallback 参数
        （调用方传入的"正在切换的目标项目"——广播后团队级即写入，两者一致）；
        fallback 也为空则返回 ("", "")（header 不显示 icon）。
        """
        try:
            from app.core.team_manager import TeamManager

            tm = TeamManager.get_instance()
            run_id = TabManagerWindow._resolve_tab_team_id(window)
            team_name = getattr(window, "_team_name", "") or tm.DEFAULT_TEAM
            project = tm.get_project_for_run_id(run_id, team_name=team_name) if run_id else ""
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

    def _disconnect_window_signals(self, window):
        """断开 add_window 时为窗口挂接的信号闭包。

        add_window 连接了两路窗口级信号：
        - windowTitleChanged → _tab_title_changed_slot（_on_win_title_changed）
        - ai_state_changed   → _tab_ai_state_slot（_on_ai_state_changed）
        闭包 __defaults__（_win=window）持有窗口引用，PyQt 对 C++ 对象销毁后
        自动断开的 Python slot 释放滞后，窗口 Python wrapper 残留导致整树泄漏
        （T5 诊断第⑦条引用链：信号表自环）。须在窗口 C++ 对象存活时 disconnect，
        并置 None 打破引用环。destroyed 后 Qt 会自动断开，此处额外置 None 兜底。
        """
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

    def _on_window_destroyed(self, window=None):
        """destroyed 兜底：窗体被 Qt 直接销毁（绕过 _close_window_at，如
        cleanup() 的 w.close() 路径）时断开上述闭包连接，防止整树泄漏。
        window 为 destroyed 信号传入的发送者（C++ 对象正在销毁，disconnect
        可能已无意义，置 None 仍可打破引用环）。
        """
        if window is None:
            return
        self._disconnect_window_signals(window)

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
        # ★ 泄漏修复（P0）：断开 add_window 连接的信号闭包（逻辑见
        # _disconnect_window_signals；destroyed 兜底见 _on_window_destroyed）。
        # 须在 close() 之前断开（此时 C++ 对象仍存活，disconnect 安全）。
        self._disconnect_window_signals(window)
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

    def _build_ui_context(self) -> "Dict[str, Any]":
        """委托当前活跃聊天窗口构建 UI 上下文（window._build_ui_context 约定）。

        WorkspacePageHost / 内容渲染等路径以 ``window._build_ui_context()`` 取上下文；
        TabManagerWindow 本身不含项目/会话状态，转发给当前 OpenAIChatToolWindow。
        无活跃窗口或委托失败时返回空 dict，避免阻断页面装配。
        """
        from loguru import logger

        win = self.get_current_window()
        if win is not None and hasattr(win, "_build_ui_context"):
            try:
                return win._build_ui_context()
            except Exception as e:
                logger.warning(f"[TabManagerWindow] _build_ui_context 委托失败: {e}")
        return {}

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
            # UI 插件浮动卡片按标签页投影显隐（per-tab 隔离）：
            # 卡片单实例挂全局容器，这里按目标标签页的可见记录 show/hide
            try:
                _wid = getattr(win, "_window_id", None)
                if _wid:
                    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

                    UIPluginRegistry.get_instance().sync_floating_cards_to_tab(_wid)
            except Exception:
                pass
            # Phase E：发布 Tab 切换事件
            try:
                from app.core.ui_event_bus import EV_TAB_SWITCHED, UIEventBus

                UIEventBus.get_instance().publish(
                    EV_TAB_SWITCHED,
                    tab_index=index,
                    window_id=_wid,
                )
            except Exception:
                pass
            # 切换 tab 时同步宿主窗口标题
            self._sync_window_title()
            # 🆕 会话数据即时同步：切回窗口时历史卡片/欢迎卡片可能已过期
            # （切走期间其他窗口对话/删除/重命名会话 → 本窗口 isVisible=False
            # 期间 refresh_history_card_if_visible 被跳过，且欢迎卡片缓存未失效）。
            # 仅刷新本窗口（broadcast=False），避免切换动作广播全窗口风暴。
            try:
                if hasattr(win, "_notify_history_data_changed"):
                    win._notify_history_data_changed(broadcast=False)
            except Exception:
                pass
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
            # 切换 tab 时同步全局桌宠：① 平滑重定位到激活窗 send_btn；
            # ② 改发当前窗 AI 状态（旧窗状态不串扰，全局 pet 跟随激活窗）。
            if getattr(self, "pixel_pet", None) is not None:
                self.pixel_pet.reposition_to_active_window()
                try:
                    self.pixel_pet._on_ai_state_changed(getattr(win, "_ai_state", "idle"))
                except Exception:
                    pass
            # 切 tab 时工作台跟随激活窗：重定位 + 刷新产物/任务/项目数据
            if getattr(self, "workbench_panel", None) is not None and self.is_workbench_visible():
                self.refresh_workbench()
                # 🆕 恢复该窗口上次停留的工作台页签（refresh_workbench 可能因
                # 历史页保持/插件页 reconcile 改变当前页，故恢复放在其之后）。
                # 仅处理窗口显式记忆过的页签；未记忆过的窗口保持现状不跳页。
                saved = getattr(win, "_workbench_tab_memory", None)
                if saved is not None:
                    try:
                        if self.workbench_panel.current_tab() != saved:
                            self.workbench_panel.set_current_tab(saved)
                    except RuntimeError:
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

            for inst in list(window_registry.alive_window_instances()):
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

    def _build_new_window(self, source_window, project=None):
        """创建并配置一个新窗口实例（复制源窗口的项目/模型上下文）。

        提取自 MainWidget._duplicate_window 的创建逻辑，供 spawn_tab 统一编排。
        返回尚未加入 Tab 管理器的窗口实例；source 无效时返回 None。
        """
        from PyQt5 import sip

        try:
            if sip.isdeleted(source_window) or sip.isdeleted(getattr(source_window, "homepage", None)):
                return None
        except Exception:
            return None
        if getattr(source_window, "homepage", None) is None:
            return None

        from app.main_widget import OpenAIChatToolWindow

        new_instance = OpenAIChatToolWindow(source_window.homepage, source_window=source_window)

        # ── 多窗口隔离：把源窗口的项目上下文原样复制给新窗口 ──
        proj = project or getattr(source_window, "_current_project", None) or "默认项目"
        new_instance._current_project = proj
        if getattr(new_instance, "backend", None):
            new_instance.backend._current_project = proj
        if getattr(new_instance, "backend", None) and getattr(new_instance.backend, "tool_executor", None):
            try:
                new_instance.backend.tool_executor.set_current_project(proj)
            except Exception:
                pass
        if hasattr(new_instance, "_project_label"):
            new_instance._project_label.setText(proj)
        if hasattr(new_instance, "_refresh_project_branch_style"):
            new_instance._refresh_project_branch_style()
        if hasattr(new_instance, "_copy_branch_from"):
            new_instance._copy_branch_from(source_window)
        elif hasattr(new_instance, "_update_branch"):
            new_instance._update_branch()

        # 复制模型选择（确保两个实例都已初始化 UI）
        try:
            src_provider = getattr(source_window, "_current_provider_name", None)
            if src_provider:
                new_instance._current_provider_name = src_provider
                new_instance._current_model_name = getattr(source_window, "_current_model_name", None)
                new_instance._user_manually_selected_model = getattr(
                    source_window, "_user_manually_selected_model", False
                )
                new_instance._valid_configs = dict(getattr(source_window, "_valid_configs", {}) or {})
                new_instance._update_model_selector_btn()
        except Exception:
            pass

        # 跳过历史会话恢复，由 spawn_tab 按内容加载意图决定
        new_instance._skip_restore_history = True
        return new_instance

    def spawn_tab(
        self,
        source_window,
        *,
        new_session: bool = False,
        session_record: "dict | None" = None,
        project: "str | None" = None,
        branch: bool = False,
    ) -> "OpenAIChatToolWindow | None":
        """统一创建新标签页并加载内容。

        把「新标签页 / 分支标签页」的创建与内容加载逻辑收敛到 TabManagerWindow 管理。
        调用方（MainWidget 各入口）需自行判断当前标签页是否流式，并仅在需要时调用本方法，
        故本方法不处理停流逻辑。

        Args:
            source_window: 源窗口（复制项目/模型上下文的来源）
            new_session: 新 tab 新建会话（可配合 project 指定项目上下文）
            session_record: 直接加载的历史会话记录（注入 showEvent，跳过空会话）
            project: 目标项目上下文（覆盖源窗口当前项目）
            branch: 复制源窗口当前会话消息到新 tab
        Returns:
            新窗口实例；失败返回 None（调用方应降级为原行为）
        """
        if source_window is None:
            return None
        from loguru import logger

        try:
            new = self._build_new_window(source_window, project=project)
            if new is None:
                return None

            if branch:
                cur = getattr(source_window, "session_manager", None)
                cur_session = cur.get_current_session() if cur else None
                if cur_session is not None:
                    new._branch_session_data = {
                        "messages": list(cur_session.messages),
                        "name": (cur_session.name or "对话") + " [分支]",
                        "project": project or getattr(source_window, "_current_project", None) or "",
                    }
                new._skip_restore_history = True
                # 🛡️ 分支窗口继承源窗口工具权限（对齐原 _duplicate_window 的 branch 分支）
                try:
                    if hasattr(source_window, "_tool_permission_controller") and hasattr(
                        new, "_tool_permission_controller"
                    ):
                        new._tool_permission_controller.copy_state_from(source_window._tool_permission_controller)
                except Exception:
                    pass
            elif session_record is not None:
                new._target_session_record = session_record
                new._skip_restore_history = True
            # new_session / 其他：showEvent 默认建空新会话（_current_project 已设）

            if not self.isVisible():
                self.show()
            self.add_window(new)
            idx = self._window_to_index.get(id(new), -1)
            if idx >= 0:
                self._tab_panel.set_active_index(idx)
            return new
        except Exception as e:
            logger.warning(f"[TabManagerWindow] spawn_tab 失败: {e}")
            return None

    def _on_tab_branch_requested(self, index: int):
        """分支窗口 — 从指定标签页创建分支"""
        if 0 <= index < len(self._windows):
            window = self._windows[index]
            if window is not None:
                self.spawn_tab(window, branch=True)

    def _on_new_session_in_workspace(self, project: str, worktree_path: str):
        """工作区树：在指定项目 + 工作树下新建对话页

        spawn_tab(project=...) 只切窗口的 _current_project，工作树由窗口的
        _current_workdir[project] 单独维护，必须显式切（_switch_to_worktree 幂等）。
        """
        from loguru import logger

        source = self.get_current_window()
        if source is None:
            self._on_new_tab_requested()
            return
        project = (project or "").strip() or getattr(source, "_current_project", "") or "默认项目"
        new_window = self.spawn_tab(source, new_session=True, project=project)
        if new_window is None:
            return
        worktree_path = (worktree_path or "").strip()
        if worktree_path and hasattr(new_window, "_switch_to_worktree"):
            try:
                new_window._switch_to_worktree(worktree_path)
            except Exception as e:
                logger.warning(f"[TabManagerWindow] 切换到工作树失败: {e}")

    def _on_open_session_record(self, record):
        """工作区树：打开一条历史会话（新开标签页，不动当前页）

        session_record 只需含 session_id —— _load_session_from_record 会按 id
        重新拉全量消息，并自动同步项目 / 工作树。
        """
        from loguru import logger

        record = record if isinstance(record, dict) else {}
        if not str(record.get("session_id") or ""):
            return
        source = self.get_current_window()
        if source is None:
            return
        project = (record.get("project") or "").strip() or None
        try:
            self.spawn_tab(source, session_record=record, project=project)
        except Exception as e:
            logger.warning(f"[TabManagerWindow] 打开历史会话失败: {e}")

    def refresh_workspace_tree(self):
        """刷新左侧工作区树（历史会话增删改后调用）"""
        panel = getattr(self, "_tab_panel", None)
        if panel is not None and hasattr(panel, "refresh_tree"):
            panel.refresh_tree()

    def _on_tab_count_changed_for_tree(self, _count: int):
        """Tab 数量变化 → 刷新工作区树（历史会话与已打开 Tab 会互相挤位）"""
        panel = getattr(self, "_tab_panel", None)
        if panel is not None and getattr(panel, "current_mode", lambda: "list")() == "tree":
            self.refresh_workspace_tree()

    def _on_new_tab_requested(self):
        """新建窗口 — 走当前窗口的复制逻辑，复用后端状态"""
        current = self.get_current_window()
        if current is not None:
            # 从当前窗口复制（保留后端上下文）并新开标签页
            self.spawn_tab(current, new_session=True)
        else:
            # 没有当前窗口时，走基础创建逻辑
            from app.main_widget import OpenAIChatToolWindow

            fake_page = self._create_fake_page()
            new_window = OpenAIChatToolWindow(fake_page)
            self.add_window(new_window)
            if not self.isVisible():
                self.show()

    # ── Tab 面板 UI 插件列表 ──

    def _sync_plugin_titlebar_tabs(self) -> None:
        """同步插件注册的常驻标题栏 tab（无 × 关闭钮；点击走插件 on_click 回调自展示）

        幂等：已挂载的跳过；已卸载插件的 tab 移除。注册表为空时仅做清理。
        """
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            infos = UIPluginRegistry.get_instance().get_titlebar_tabs()
        except Exception:
            infos = []
        valid_ids = {info.tab_id for info in infos}
        # 移除已卸载/不再注册的常驻 tab
        for tab_id in list(self._plugin_titlebar_tab_ids):
            if tab_id not in valid_ids:
                self.titleBar.remove_tab(tab_id)
                self._plugin_titlebar_tab_ids.discard(tab_id)
        for info in infos:
            if info.tab_id in self.titleBar._tabs:
                continue
            self.titleBar.add_tab(
                info.tab_id,
                info.label,
                on_click=info.on_click,
                closable=False,
                icon_path=info.icon_path or "",
            )
            self._plugin_titlebar_tab_ids.add(info.tab_id)

    def _update_shared_launcher(self) -> None:
        """兼容旧调用方（main_widget.py 热重载和模式切换）并刷新内嵌列表"""
        self._tab_panel.refresh_ui_plugins()
        self._sync_plugin_titlebar_tabs()
        # 工作区页面刷新（Phase G）：注册集变化后重建 sidebar 入口 + 销毁被卸载页面
        if hasattr(self, "_workspace_page_host"):
            self._workspace_page_host.refresh_pages()

    def _show_shared_launcher(self) -> None:
        """兼容模式切换调用：刷新始终显示在 TabPanel 中的插件列表"""
        self._tab_panel.refresh_ui_plugins()
        self._sync_plugin_titlebar_tabs()
        if hasattr(self, "_workspace_page_host"):
            self._workspace_page_host.refresh_pages()

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
        """几何保存防抖（现为 no-op 链路）

        原实现：拖拽/缩放结束后 200ms 将窗口几何写入配置（记忆功能）。
        已按需求移除记忆，保留此调用点 + timer 空转，避免改动
        moveEvent/resizeEvent 等大量调用点。
        """
        # FramelessWindow.__init__ 内部 resize(500,500)/winId 创建会同步触发
        # resize/move 事件，早于本类 __init__ 创建 _geo_save_timer；此时跳过
        if not hasattr(self, "_geo_save_timer"):
            return
        from app.utils.window_drag_state import any_window_dragging

        if any_window_dragging:
            return
        self._geo_save_timer.start()  # 连续调用时不断重置计时器

    def _do_save_geometry(self):
        """固定默认几何策略：不再把窗口位置/大小/面板宽度写入配置。

        用户要求打开时固定默认大小、位置与 panel 宽度（屏幕居中），
        不做任何记忆。保留空实现以维持 moveEvent/resizeEvent 等
        调用点的防抖链路不变（timer 照常触发，只是不再写盘）。
        """

    def _restore_geometry(self):
        """固定默认窗口几何：960x720，屏幕居中，确保不超出屏幕"""
        screen = QApplication.primaryScreen()
        screen_rect = screen.availableGeometry() if screen else None
        if not screen_rect:
            self.resize(960, 740)
            return

        w, h = 960, 740
        self._suppress_drag_detection = True
        self.setGeometry(
            screen_rect.x() + (screen_rect.width() - w) // 2,
            screen_rect.y() + (screen_rect.height() - h) // 2,
            w,
            h,
        )
        self._suppress_drag_detection = False

    def showEvent(self, event):
        """仅首次显示时固定默认几何（标准系统窗口自带阴影/边框/圆角）

        运行中用户可自由拖动/缩放窗口，不再重置；重启后恢复默认居中。
        """
        super().showEvent(event)
        # 原生窗口能力补全（边缘 resize + Aero Snap）并压掉 DWM 白边。
        # hwnd 会因 setWindowFlags / 跨屏 DPI 变化重建，故每次显示都校验。
        self._ensure_native_window_styles()
        # 标题栏宽度必须显式同步：构造期几何恢复走的是 resize 节流路径，
        # 基类那次 titleBar.resize() 会被跳过（详见 _sync_title_bar_width）。
        self._sync_title_bar_width()
        if not self._geometry_applied:
            self._geometry_applied = True
            self._restore_geometry()
            # Win11 DWM 圆角（winId 此刻已有效；仅首次）
            self._apply_win11_round_corner()
        # 几何恢复可能改变窗口宽度，再同步一次标题栏
        self._sync_title_bar_width()
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
        # 插件常驻标题栏 tab 同步补刷（同 refresh_ui_plugins 的可见性门控根因）
        try:
            self._sync_plugin_titlebar_tabs()
        except Exception:
            pass

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
            # _suppress_drag_detection 用于阻止编程式 setGeometry 误触。
            # FramelessWindow.__init__ 内部 resize/winId 会同步触发 move 事件，
            # 早于本类 __init__ 创建 _suppress_drag_detection/_window_dragging_timer，
            # 此时跳过（与 _save_geometry 的 hasattr 守卫同理）
            if hasattr(self, "_suppress_drag_detection") and not self._suppress_drag_detection:
                if not self._window_dragging_timer.isActive():
                    self._on_window_drag_start()
                self._window_dragging_timer.start()  # 持续重置防抖
        # 几何保存防抖（拖拽期间由 _save_geometry 内部守卫跳过）
        self._save_geometry()

    # ── 原生窗口能力：边缘/角落 resize + Aero Snap 分屏 ──

    def _sync_title_bar_width(self) -> None:
        """把标题栏宽度同步到当前窗口宽度

        ★ 必须显式同步：``WindowsFramelessWindow.resizeEvent`` 负责把标题栏
        resize 到窗口宽度，而本类的 resize 节流（``_resize_blocking`` 阶段二）
        会跳过 ``super().resizeEvent()``，标题栏宽度因此停留在进入节流前的旧
        值 —— 表现为"最小化/最大化/关闭按钮不在最右边、跑到了屏幕中间"，
        直到下一次非节流路径的 resize（例如最大化）才被纠正。
        """
        tb = getattr(self, "titleBar", None)
        if tb is None:
            return
        try:
            if tb.width() != self.width():
                tb.resize(self.width(), tb.height())
        except Exception:
            pass

    def _ensure_native_window_styles(self) -> None:
        """补全无边框窗口丢失的原生能力（边缘 resize + Aero Snap）

        Qt 设置 ``Qt.FramelessWindowHint`` 时，Windows 端会一并清掉
        WS_THICKFRAME / WS_SYSMENU / WS_MINIMIZEBOX / WS_MAXIMIZEBOX。系统据此
        判定窗口"不可调整大小"，于是**边缘与角落的 resize 完全失效**，拖到屏幕
        边缘也不触发 Aero Snap / Win11 分屏布局。本方法把这些位补回来。

        ★ 不要再用 ``DWMWA_NCRENDERING_POLICY = DWMNCRP_DISABLED`` 去压边框。
        关闭 DWM 的非客户区渲染后，系统会退回**传统 GDI 路径**绘制非客户区，
        表现为 resize 时隐约闪出原生窗口边框（比不关时更明显）。正确做法是保留
        DWM 渲染，靠 ``DwmExtendFrameIntoClientArea(-1)``（qframelesswindow 的
        ``addShadowEffect``）把窗口框架吃进客户区——框架落在客户区里，DWM 就没有
        独立的位置可以画边框，阴影也还在。

        所以这里只做两件事：补样式位 + 确保框架扩展已生效。幂等。
        """
        if not _IS_WINDOWS or _user32 is None or not _SNAP_STYLES:
            return
        try:
            hwnd = _wintypes.HWND(int(self.winId()))

            style = _user32.GetWindowLongPtrW(hwnd, _GWL_STYLE)
            if style & _SNAP_STYLES != _SNAP_STYLES:
                _user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, style | _SNAP_STYLES)
                # ★ 只在样式位真的变了才 SetWindowPos(FRAMECHANGED)：该方法会
                # 强制系统重算整个非客户区并重绘窗口，无条件调用（例如每次
                # showEvent）会让窗口肉眼可见地"闪一下系统边框"。
                # 不带 ACTIVATE，避免抢焦点引发重入。
                _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_FRAMECHANGED)

            # 补回样式位后重新扩展框架（hwnd 重建 / DWM 状态变化都可能让它失效）
            if self.windowEffect is not None:
                self.windowEffect.addShadowEffect(self.winId())
        except Exception:
            pass

    def _system_buttons_left(self) -> int:
        """标题栏三个系统按钮的左边界 x（顶边热区需让开这段范围）"""
        btn = getattr(getattr(self, "titleBar", None), "minBtn", None)
        if btn is None or btn.isHidden():
            return self.width()
        return max(0, btn.x())

    def _native_hit_test(self, hwnd: int, lparam: int):
        """WM_NCHITTEST 自建判定：四边 + 四角的 resize 热区

        ★ 为何不直接复用基类实现：基类用 ``win32api.GetCursorPos()`` 取坐标，
        拖到屏幕边缘时游标位置与消息自带的 lParam 不同步；且热区固定 5px 不随
        DPI 缩放，高 DPI 屏上几乎抓不到。这里改用消息自带的 lParam（光标屏幕
        坐标）换算，热区按窗口 DPI 放大。

        顶边只在最上沿 ``_TOP_RESIZE_BAND`` px 内生效，并让开标题栏右侧系统
        按钮的横向范围——否则返回 HTTOP/HTTOPRIGHT 会让按钮收不到点击。

        返回 None 表示"本层不判定"，交由基类与 Qt 继续处理。
        """
        if not _IS_WINDOWS or _user32 is None:
            return None
        try:
            if _user32.IsZoomed(_wintypes.HWND(hwnd)):
                return None  # 最大化 / 全屏态没有可拉伸的边缘
            pt = _wintypes.POINT(
                _ctypes.c_int16(lparam & 0xFFFF).value,
                _ctypes.c_int16((lparam >> 16) & 0xFFFF).value,
            )
            _user32.ScreenToClient(_wintypes.HWND(hwnd), _ctypes.byref(pt))
            rect = _wintypes.RECT()
            _user32.GetClientRect(_wintypes.HWND(hwnd), _ctypes.byref(rect))
            w, h = rect.right, rect.bottom
            if w <= 0 or h <= 0:
                return None

            scale = max(1.0, self.logicalDpiX() / 96.0)
            side = max(3, int(round(self._RESIZE_BORDER * scale)))
            top_band = max(2, int(round(self._TOP_RESIZE_BAND * scale)))

            x, y = pt.x, pt.y
            # 标题栏系统按钮所在的一小条顶部区域：明确判为客户区，
            # 既让按钮可点击，也阻止基类按自己的固定 5px 规则再判成边缘。
            if y < max(side, top_band) and x >= self._system_buttons_left():
                return _HTCLIENT

            left = x < side
            right = x >= w - side
            top = y < top_band
            bottom = y >= h - side

            if left and top:
                return _HTTOPLEFT
            if right and top:
                return _HTTOPRIGHT
            if left and bottom:
                return _HTBOTTOMLEFT
            if right and bottom:
                return _HTBOTTOMRIGHT
            if top:
                return _HTTOP
            if bottom:
                return _HTBOTTOM
            if left:
                return _HTLEFT
            if right:
                return _HTRIGHT
        except Exception:
            return None
        return None

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
                msg = _MSG_CAST(int(message))[0]
                msg_id = msg.message
                if msg_id == self._WM_ENTERSIZEMOVE:
                    self._window_dragging_timer.stop()  # 原生信号权威，停用防抖回退
                    self._on_window_drag_start()
                elif msg_id == self._WM_NCHITTEST:
                    # 边缘/角落 resize：先于基类判定（基类热区固定 5px 且不
                    # 处理标题栏系统按钮让位）。返回 None 时落回 super() 链。
                    hit = self._native_hit_test(int(msg.hwnd), int(msg.lParam))
                    if hit is not None:
                        return True, hit
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
        """窗口状态变化：标准系统窗口的最大化/还原由 OS 处理，无需自定义逻辑"""
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            # 最大化 / 还原 / 全屏由系统直接改窗口几何，Qt 不一定走完整的
            # resizeEvent 链（节流阶段二会跳过基类同步），这里补一次，
            # 保证标题栏宽度始终等于窗口宽度（系统按钮不会跑到中间）。
            self._sync_title_bar_width()
            # 几何瞬变保护：状态切换会连带改标题栏高度/窗口边距并触发额外
            # relayout（左面板被瞬时压窄）。抑制侧边栏自动折叠，避免把
            # 最大化/还原误判为"用户拖窄"。resize 周期结束会提前解除，
            # 这里的定时器只是兜底。
            self._begin_window_state_guard()

    def _begin_window_state_guard(self):
        """窗口状态切换（最大化/还原/全屏）期间抑制侧边栏自动折叠"""
        panel = getattr(self, "_tab_panel", None)
        if panel is None:
            return
        panel.set_auto_collapse_suppressed(True)
        guard = getattr(self, "_window_state_guard_timer", None)
        if guard is None:
            guard = QTimer(self)
            guard.setSingleShot(True)
            guard.timeout.connect(self._end_window_state_guard)
            self._window_state_guard_timer = guard
        guard.start(600)

    def _end_window_state_guard(self):
        """兜底解除状态切换抑制（resize 周期正常结束会先于此解除）"""
        # resize 周期仍在进行：抑制交由 _deferred_resize_complete 统一解除，
        # 此处不提前解除，否则后续 relayout 瞬变会重新误折叠。
        if getattr(self, "_resize_blocking", False):
            return
        panel = getattr(self, "_tab_panel", None)
        if panel is not None:
            panel.set_auto_collapse_suppressed(False)

    def _on_app_state_changed(self, state):
        """macOS 软置顶：应用激活时若开启置顶配置则抬升主窗口

        替代 WindowStaysOnTopHint（mac 上该 hint 会把窗口提到
        NSStatusWindowLevel，导致标题栏最小化点击被系统丢弃）。
        """
        from app.utils.config import Settings as _Settings

        if state != Qt.ApplicationActive:
            return
        if not _Settings.get_instance().window_always_on_top.value:
            return
        if not self.isMinimized():
            self.raise_()

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
        elif obj is self._chat_wrapper and event.type() == QEvent.Wheel:
            # 限宽居中的左右留白区域没有子控件接收滚轮事件，
            # 转发给当前内容区（对话列表/覆盖层）的滚动区域。
            try:
                if self._forward_wheel_to_scroll_area(event):
                    event.accept()
                    return True
            except Exception:
                pass
        return super().eventFilter(obj, event)

    def _forward_wheel_to_scroll_area(self, event) -> bool:
        """把 wrapper 留白区的滚轮事件转发给当前可见内容区的滚动区域

        优先转发当前窗口的 chat_scroll_area（带平滑滚动）；覆盖层模式
        或找不到时，回退到当前内容下第一个可见滚动区域。
        """
        # 优先：当前窗口的对话滚动区域
        win = self.get_current_window()
        if win is not None:
            area = getattr(win, "chat_scroll_area", None)
            if area is not None and area.isVisible():
                area.wheelEvent(event)
                return True
        # 回退：当前内容（对话区/覆盖层）下第一个可见滚动区域
        root = self._content_stack.currentWidget()
        if root is None:
            return False
        for child in root.findChildren(QAbstractScrollArea):
            if child.isVisible():
                child.wheelEvent(event)
                return True
        return False

    def _sync_chat_wrapper_width(self):
        """按当前 wrapper 宽度设置左右 margins，实现限宽居中

        - 对话区（index 0）：按 _MAX_CHAT_WIDTH 限宽居中
        - 覆盖层（index 1）：配置类卡片（系统设置/服务商/Hook/MCP）按
          _MAX_OVERLAY_WIDTH 限宽居中；内容型卡片（diff_viewer、
          子智能体会话、full 浮动卡）铺满全宽
        """
        wrapper = self._chat_wrapper
        w = wrapper.width()
        if w <= 0:
            return
        if self._content_stack.currentIndex() == 1:
            pad = max(0, (w - _MAX_OVERLAY_WIDTH) // 2) if self._overlay_should_limit_width() else 0
        else:
            pad = max(0, (w - _MAX_CHAT_WIDTH) // 2)
        layout = self._chat_wrapper_layout
        if layout.contentsMargins().left() != pad:
            layout.setContentsMargins(pad, 0, pad, 0)
            # 立即重算：resize 节流期间布局事件链可能延迟，主动失效+激活
            # 确保 chat_frame 几何同步到最新 margins（否则停留在旧宽度）
            layout.invalidate()
            layout.activate()

    def _overlay_should_limit_width(self) -> bool:
        """覆盖层当前可见卡片是否为配置类（需限宽居中）

        系统设置/服务商编辑/Hook/MCP 等配置卡限宽；其余（diff_viewer、
        sub_agent_session、full 浮动卡）铺满。覆盖层卡片互斥，取可见者判断。
        """
        container = getattr(self, "_global_top_container", None)
        if container is None:
            return False
        for cid, card in getattr(container, "_cards", {}).items():
            if cid in _CONFIG_REPLACE_CARDS and not card.isHidden():
                return True
        return False

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
        # ── P1 防竞态：若 splitter 拖拽防抖 timer 仍活跃，跳过本次 _content_area 释放 ──
        # 避免与 OPT-2 的 _on_splitter_idle 双重释放竞态/闪烁（拖拽松手由 timer 统一恢复）。
        if hasattr(self, "_content_area") and not (
            hasattr(self, "_splitter_idle_timer") and self._splitter_idle_timer.isActive()
        ):
            self._content_area.setUpdatesEnabled(True)

        # 强制完整 relayout：blocking 期间跳过了 super().resizeEvent，
        # 子控件 geometry 与窗口新尺寸已不同步。通过 invalidate + activate
        # 强制 Qt 从顶层布局开始重新计算整棵 widget 树。
        self._force_relayout()

        # 标题栏宽度收拢：节流期间的多次 resize 只保证最后一次与窗口同宽，
        # 这里再补一次，确保任何退出路径下系统按钮都贴在最右侧。
        self._sync_title_bar_width()

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
        # 全局桌宠跟随窗体缩放：resize 结束后精确重定位到激活窗 send_btn
        if getattr(self, "pixel_pet", None) is not None:
            self.pixel_pet.reposition_to_active_window()

        # ── 几何已收拢：解除抑制 + 按最终宽度判定"是否真被挤压" ──
        # 整个 resize 周期（含 _force_relayout 瞬变）内 TabPanel 不自动折叠，
        # 这里基于稳定后的真实几何决定，避免最大化/还原这类几何瞬变把
        # 侧边栏误折叠（折叠后窗口总宽不增反减，自动展开条件永不满足）。
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_auto_collapse_suppressed(False)
        if not self._evaluate_squeeze_collapse():
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
        if not hasattr(self, "_resize_blocking"):
            # FramelessWindow.__init__ 内部 resize(500,500) 同步触发的首个 Resize：
            # 本类节流状态尚未初始化，直接走基类布局（含顶栏宽度同步）并返回
            super().resizeEvent(event)
            return

        if self._resize_blocking:
            # ── 阶段二：blocking 活跃，跳过布局传播 ──
            self._resize_timer.start()  # 重置防抖
            self._save_geometry()
            # 标题栏宽度同步：基类是在 super().resizeEvent() 里做的，
            # 阶段二跳过了 super，必须手动补，否则系统按钮停在旧位置。
            self._sync_title_bar_width()
            # 背景图尺寸跟随（轻量操作，不触发布局）
            self._resize_bg_labels()
            return

        # ── 阶段一：首个 resize 事件，正常布局 + 初始化 blocking ──
        # ★ 先抑制再布局：super().resizeEvent() 会把新几何一次性传播到子控件，
        # 左面板宽度在此期间是瞬时中间值（relayout 重算会先压到最小宽度），
        # 必须在传播前就抑制，否则会被误判为"用户拖窄"而自动折叠。
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_auto_collapse_suppressed(True)
        super().resizeEvent(event)
        # 背景图尺寸跟随（轻量操作，不触发布局）
        self._resize_bg_labels()
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
        # ★ 停止应用级服务（Gateway 平台 WebSocket 断连 + 插件 watcher 线程）
        try:
            from app.core.gateway_service import GatewayService
            from app.core.plugin_host_service import PluginHostService

            GatewayService.get_instance().stop()
            PluginHostService.get_instance().stop()
        except Exception:
            pass
        # ★ 清理全局桌宠（停止所有定时器），先于窗口关闭避免悬空引用
        if getattr(self, "pixel_pet", None) is not None:
            try:
                self.pixel_pet.cleanup()
            except Exception:
                pass
        for w in list(self._windows):
            try:
                w.close()
            except Exception:
                pass
        self._windows.clear()
        self._window_to_index.clear()
        self.close()


class _AutoGeometryLabel(QLabel):
    """背景 label：监听 parent 的 resize，自动同步 geometry

    用于 QSplitter 拖动 / parent 自身 resize 等场景。
    注：TabManagerWindow 整体 resize 走 _resize_bg_labels() 手动调用，
    此处只补齐 splitter 拖动（不触发顶层 resize）的同步路径。
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        try:
            parent.installEventFilter(self)
        except Exception:
            pass

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Resize and watched is self.parent():
            self.setGeometry(self.parent().rect())
        return super().eventFilter(watched, event)
