# -*- coding: utf-8 -*-
"""
TabManagerWindow — Tab 管理器宿主窗口（无边框现代化窗口）

左侧 TabPanel + 右侧 QStackedWidget 嵌入 OpenAIChatToolWindow 实例。
基于 qframelesswindow.FramelessWindow：自绘标题栏（CustomTitleBar），
Windows 原生保留 Aero Snap / 摇动 / 任务栏预览 / DWM 阴影。
"""

import platform
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

import shiboken6 as _sip
from loguru import logger
from app.core.infra import window_registry
from PySide6.QtCore import QEasingCurve, QEvent, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
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
from app.utils.design_tokens import Animations, Colors, font_size_css
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css, get_unified_font

# ── 对话区限宽档位（px）：窗口过宽时限制对话内容宽度并居中，两侧留白 ──
# 避免聊天内容在超宽屏幕上被拉得难以阅读。档位可在 设置-外观 切换。
# full 档返回 0 = 不限宽，对话区占满窗口。
_CHAT_WIDTH_PRESETS = {"narrow": 820, "standard": 1000, "wide": 1200, "full": 0}


def _current_chat_width() -> int:
    """当前对话区限宽（px）；0 = 不限宽"""
    try:
        key = Settings.get_instance().ui_chat_width.value
    except Exception:
        key = "standard"
    return _CHAT_WIDTH_PRESETS.get(key, _CHAT_WIDTH_PRESETS["standard"])


# ── 覆盖层「配置类」卡片最大宽度（px）──
# 比对话区稍宽，同样在超宽窗口下限宽居中，避免系统配置卡片无限拉伸。
# 仅作用于配置类卡片（系统设置/服务商/添加服务商/Hook/MCP）；差异对比、子智能体
# 会话等内容型卡片不限宽，铺满对话区。
# 不限宽档（full）下覆盖层同步铺满，否则跟随对话区宽度 +100。
def _current_overlay_width() -> int:
    chat_w = _current_chat_width()
    return chat_w + 100 if chat_w > 0 else 0


_CONFIG_REPLACE_CARDS = frozenset({"settings", "provider_edit", "provider_picker", "hook_edit", "mcp_edit"})

# ── 标题栏 tab：内置「聊天」常驻 tab + 已知「内置替换」全局卡片 ──
# full 容器卡片（UI 插件 full 卡 + 下列内置全局卡）打开时在标题栏 tab 区显示
# （非常驻可关闭，× 点击真实隐藏卡片）；「聊天」为常驻 tab，active 时表示对话视图。
CHAT_TAB_ID = "chat"
KNOWN_GLOBAL_REPLACE_CARDS = frozenset(
    {
        "settings",
        "provider_edit",
        "provider_picker",
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
    "provider_picker": "服务商",
    "hook_edit": "Hook 编辑",
    "mcp_edit": "MCP 编辑",
    "diff_viewer": "文件差异对比",
    "sub_agent_session": "子智能体会话",
    "file_undo": "文件撤销",
    "chart_viewer": "图表查看",
}

# ══════════════════════════════════════════════════════════════════════════
# 侧边栏宽度常量 —— ★ 单一真源（single source of truth）
#
# ★★ 坐标系纪律（此前踩过坑，务必看懂再改）：
#   - `_FRAME_*` 系列是 **frame 宽**（QSplitter 直接控制的窗格宽度，含 padding）；
#   - `TabPanel._auto_collapse_width` 等是 **content 宽**（TabPanel.width()
#     自身宽度 = frame 宽 − 14）。
#   两套坐标不能混用：早期把 frame 域的值直接塞进 TabPanel 的 content 域比较，
#   等价于把阈值整体抬高 14px，并让展开/折叠的滞回区被撑成 24px —— 表现为
#   "手动拉宽到 188px（内容区 174px，视觉上已完全够用）却仍是折叠态"。
#   因此 content 域常量在此显式定义为 frame 值减去 padding，禁止再手写换算。
# ══════════════════════════════════════════════════════════════════════════

# frame 宽 = 内容宽 + margins 12 + border 2
_FRAME_PADDING_X = 14

# ── 卡片态 frame↔槽位二次换算补偿（px，frame 域）──
# 卡片打开期 #tabFrame 内多出两处未计入 _FRAME_PADDING_X 的装饰：
#   #tabFrame QSS margin-left(4) + sidebarSplitter handle(4)。
# 实测几何方程（卡片态）：frame 宽 = TabPanel 宽 + 卡片槽位 + 22
# （= _FRAME_PADDING_X 14 + handle 4 + QSS margin 4）。
# 卡片打开编排若不补偿，卡片槽每次重开被净吞 8px，连续切页时
# 360→352→344 逐轮递减（表现为「切页往返后卡片越切越窄」）。
_CARD_SLOT_CHROME_EXTRA = 8

# ── 侧边栏折叠线（px，★ content 宽，= TabPanel.width() 口径）──
# 展开态拖窄到该宽度以下即视为不可用 → 触发自动折叠；同时作为挤压折叠后
# 展开的目标下限（不得窄于该值，否则标题文字被压成窄条）。
#
# ★★ 取值约束（三者必须满足，否则手感断裂）：
#   折叠线 < _DEFAULT_PANEL_WIDTH < 展开线
#   即"默认展开宽度"必须落在折叠线**之上**、展开线**之下或之上**，且
#   折叠线必须低于默认展开宽度。此前把折叠线取到 186 而默认展开宽度是 187，
#   再加 6px 滞回区 → 展开线 192 > 187，用户从折叠态拉到"正常宽度"187px
#   仍判定为折叠态（截图 bug：左栏文字齐全但 tab 还是图标胶囊）。
#   现取 150：content 150 时标题已明显被压缩（约 11 个中文字以内），
#   低于它折叠合理；而默认 187 距折叠线有 37px 余量，手感自然。
_EXPANDED_MIN_CONTENT_WIDTH = 150

# ── frame 域展开最小宽度（px，frame 宽）──
# 仅用于 QSplitter.setSizes / 窗格宽度计算（那些地方用的是 frame 宽）。
_EXPANDED_MIN_FRAME_WIDTH = _EXPANDED_MIN_CONTENT_WIDTH + _FRAME_PADDING_X

# ── 侧边栏固定默认宽度（px，content 宽）──
# 打开时固定默认宽度（不记忆），约原 280 的 2/3。
_DEFAULT_PANEL_WIDTH = 187

# ── 挤压折叠后自动展开的窗口增长阈值（px）──
# 窗口 resize 挤压折叠后，需比折叠时窗口总宽再宽出该值才自动展开，
# 避免"折叠刚完成条件恰满足就弹回展开"的抖动（绝对条件在窗口 ~760 时
# 折叠即满足展开条件，导致折叠态无法保持）。overlay 卡片关闭属布局恢复
# （窗口总宽未变），不走增长条件。
_AUTO_EXPAND_GROWTH = 80  # 自动展开的窗口增长门槛（原 200 过苛：挤压折叠后几乎永不恢复，保留滞后防"折叠完立刻弹回"）

# ── 聊天区最小可用宽度（px）──
# 判定"侧边栏是否真的被挤压"的下限：窗口总宽放得下
# 「常规展开宽度 + 该值」就不算挤压，侧边栏必须保持展开。
_MIN_CHAT_WIDTH = 400

# ── 侧边栏收起态宽度（px，content 宽；frame = 该值 + 14 = 60）──
# 仅容纳图标条的宽度（TabPanel._collapsed_min_width 同源引用）。
_COLLAPSED_PANEL_WIDTH = 46

# ── 侧边栏拖拽最大宽度（px，content 宽；frame = 该值 + 14 = 414）──
# TabPanel 内容区的拖拽上限（splitter 实际约束的是 #tabFrame）。
_TAB_PANEL_MAX_CONTENT_WIDTH = 400

# ── 折叠/展开滞回区宽度（px，★ content 宽）──
# 折叠线 = _EXPANDED_MIN_CONTENT_WIDTH；展开线 = 折叠线 + 本值。
# ★ 取值纪律：滞回区只用于吸收"拖拽途中宽度在阈值附近抖动"（手抖 1~2px、
#   Qt 量化误差），因此必须**明显小于**用户可感知的操作粒度。取 6px：
#   折叠 <186、展开 >=192。此前误用 frame 域的 10 并在错误的坐标系上叠加，
#   实际滞回区被撑到 24px content，用户拉宽 20px 仍等不到展开，手感即
#   "明显拉宽了却还是折叠态"。
_HYSTERESIS_WIDTH = 6

# ── left 卡片区最小可用宽度（px，frame 域）──
# 空间不足（工作台让位后仍放不下）时的卡片区保底：低于该值内容挤压不可读
# （2026-10 反馈：窄窗口开 UI 卡片被挤成竖排细条）。
_LEFT_CARD_MIN_VISIBLE_WIDTH = 240

# ── 聊天区绝对下限（px）──
# 「工作台让位后仍放不下卡片最小宽」的极端窄窗口下，聊天区最后的妥协线
# （低于 _MIN_CHAT_WIDTH 但保对话基本可读）。
_CHAT_ABSOLUTE_MIN_WIDTH = 300


def _tab_frame_max_expanded(card_w: int = 0) -> int:
    """#tabFrame 的最大宽度（frame 域）

    卡片区未展开（card_w=0）：静态上限 = TabPanel 最大内容宽 + padding。
    卡片区展开：上限额外叠加卡片宽，使拖主 splitter 时 frame 能被拖宽
    （增量全部落到卡片区，TabPanel 锁定收起宽，见 _on_left_card_area_toggled）。
    """
    return _TAB_PANEL_MAX_CONTENT_WIDTH + _FRAME_PADDING_X + max(0, card_w)


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
_dwmapi = None
_GWL_STYLE = -16
_SNAP_STYLES = 0
_SWP_FRAMECHANGED = 0
_HTCLIENT = 1
_HTLEFT = _HTRIGHT = _HTTOP = 0
_HTTOPLEFT = _HTTOPRIGHT = 0
_HTBOTTOM = _HTBOTTOMLEFT = _HTBOTTOMRIGHT = 0

# ── DWM 窗口外观（Win11 build 22000+）──
# DWMWA_WINDOW_CORNER_PREFERENCE(33)：窗口圆角偏好
# DWMWA_BORDER_COLOR(34)：窗口最外圈描边颜色
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMCP_ROUND = 2  # DWM_WINDOW_CORNER_PREFERENCE.DWMCP_ROUND
# ★ DWMWA_COLOR_NONE：让 DWM「跳过」描边绘制（不是画成透明色）。官方文档：
#   可得到"有圆角但无边框"的窗口。与之相对，DWMWA_COLOR_DEFAULT(0xFFFFFFFF)
#   是**恢复系统默认描边** —— 即那条会随激活状态变亮/变暗的 2px 白线，
#   所以千万不要用它来"复位"边框。
_DWMWA_COLOR_NONE = 0xFFFFFFFE

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

        # ── DWM 绑定（Win11 圆角 / 压边框描边，见 _apply_win11_dwm_chrome）──
        # ★ 必须显式声明 argtypes：不声明时 ctypes 默认把 Python int 转成 C int
        #   （32 位），64 位进程里 HWND 有被截断的风险。
        _dwmapi = _ctypes.windll.dwmapi
        _dwmapi.DwmSetWindowAttribute.argtypes = [
            _wintypes.HWND,
            _wintypes.DWORD,
            _ctypes.c_void_p,
            _wintypes.DWORD,
        ]
        _dwmapi.DwmSetWindowAttribute.restype = _ctypes.c_long

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
        _dwmapi = None


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

    def minimumSizeHint(self):  # noqa: N802
        # setMinimumWidth(0) 压不住 hint（QWidgetItem::minimumSize = hint.expandedTo(min)），
        # 文本宽 ~150px 会顶住整窗 resize 下限，必须从源头摘掉
        return QSize(0, 0)


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

        # 临时对话页：Tab 用固定隐身紫「临」图标（不用项目色），标题前缀由窗口标题携带
        # 字色固定白：隐身紫是语义常量色，白字随色定义处声明，不走亮度自适应
        # （#8B5CF6 lum 0.198 落在自适应黑字分支，紫底黑字与全站白字惯例冲突）
        if 0 <= tab_idx < len(tm._windows) and getattr(tm._windows[tab_idx], "_is_temp_scope", False):
            tm._tab_panel.update_tab_project(tab_idx, "临", "#8B5CF6", "#FFFFFF")
            return

        from app.utils.project_meta import (
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

    ── 嵌入侧边栏模式（set_partner_pane） ──
    当宿主把本 wrapper 挂进 #tabFrame 内部的 sidebarSplitter（左卡片贴边栏右侧）时，
    可指定伙伴窗格（多为 TabPanel）：卡片显隐时同步收紧/恢复伙伴宽度，实现
    「卡片打开 → tab 列表让位收窄；卡片关闭 → tab 列表恢复」。
    未指定伙伴窗格时行为与旧版完全一致（对话区左停靠区模式）。
    """

    # 卡片区展开/收起状态变化（True=有可见卡片展开，False=已收起）。
    # 宿主（TabManagerWindow）监听：驱动 #tabFrame 整体宽度加宽/缩回、
    # 对话区让位/回收、TabPanel 折叠态同步（防 resizeEvent 回路）。
    expandedChanged = Signal(bool)

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
        # ── 伙伴窗格联动（嵌入侧边栏模式；None = 旧行为）──
        self._partner_pane: Optional[QWidget] = None
        self._partner_splitter_index: int = -1
        self._partner_saved_size: int = 0  # 卡片打开前的伙伴宽（关闭时恢复）
        self._partner_collapsed_size: int = 0  # 卡片打开时伙伴收窄目标（0=用 min 宽）
        # 伙伴自动收窄开关产生的 setSizes 会被 splitterMoved 视为"用户拖拽"？
        # Qt 的编程式 setSizes 不发 splitterMoved（实测），无需额外守卫；
        # 但本 wrapper 自身 show/hide 会触发 _sync 重入，用标志位防环。
        self._syncing = False
        # 重入期又有显隐请求（置脏）→ 当前轮跑完后补跑一轮，保证状态收敛
        self._sync_dirty = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(primary)
        lay.addWidget(stack)
        primary.installEventFilter(self)
        stack.installEventFilter(self)
        self.hide()  # 默认收起

    def set_partner_pane(self, pane: QWidget, collapsed_size: int = 0) -> None:
        """指定伙伴窗格（嵌入侧边栏模式）：卡片打开时收窄它、关闭时恢复

        Args:
            pane: 伙伴窗格（同一 splitter 内的另一个直接子项，如 TabPanel/其 frame）
            collapsed_size: 卡片打开时伙伴收窄到的尺寸（px）；0 = 用其 minimumWidth()
        """
        self._partner_pane = pane
        self._partner_collapsed_size = int(collapsed_size)

    def attach_to_splitter(self, splitter: QWidget, index: int) -> None:
        """关联宿主 splitter：联动 setSizes 维持收起态"""
        self._splitter = splitter
        self._splitter_index = index
        if self._partner_pane is not None:
            try:
                self._partner_splitter_index = splitter.indexOf(self._partner_pane)
            except Exception:
                self._partner_splitter_index = -1
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
        # ★ 重入处理：_sync 内部会 setSizes → 触发新一轮 hide/show 事件 →
        # 经 singleShot 回到 _sync。若直接丢弃（旧实现直接 return），在该时序下
        # 卡片显隐的最终状态可能永远不被处理（实测关闭 left 卡片后 wrapper
        # 停在展开态、卡片槽位不归 0）。改为置脏标记：当前跑完后若期间又有
        # 请求，立即补跑一轮，保证最终状态一定收敛。
        if self._syncing:
            self._sync_dirty = True
            return
        self._syncing = True
        self._sync_dirty = False
        try:
            any_visible = self._child_visible_intent(self._primary) or self._child_visible_intent(self._stack)
            if self._splitter is None or self._splitter_index < 0:
                # 未关联 splitter 时只切自身 visibility（测试 / 单实例场景）
                self.setVisible(any_visible)
                return
            sizes = self._splitter.sizes()
            idx = self._splitter_index
            if any_visible:
                self._on_expand(sizes, idx)
            else:
                self._on_collapse(sizes, idx)
        finally:
            self._syncing = False
        # 期间又有显隐请求 → 立即补跑（最多收敛若干轮，状态稳定后自然停止）
        if self._sync_dirty:
            self._sync_dirty = False
            self._sync()

    def _on_expand(self, sizes: List[int], idx: int) -> None:
        """卡片可见：展开自身；伙伴窗格（若指定）同步收窄让位

        ★ 信号时序纪律：expandedChanged(True) 必须在改内部分配之前同步发出，
        宿主回调里先把 TabPanel 置入收起态（静默 set_collapsed，不 emit）——
        随后本方法把伙伴收窄到 46px 时，TabPanel.resizeEvent 因 _collapsed
        已为 True 而不再发 sidebarToggled(True)，阻断「伙伴收窄 → 宿主把
        frame 压到收起宽 → 卡片被挤没」的回路。
        """
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
                self.expandedChanged.emit(True)  # 宿主先备 TabPanel 收起态
                sizes[idx] = expand_w
                self._shrink_partner(sizes)
                sp = self._splitter
                if sp is not None:
                    sp.setSizes(sizes)
        else:
            # 已展开（互斥切卡）：槽位可能与新卡片记忆不符，交由容器
            # _apply_visible_card_dock_size 处理；此处只保证伙伴已收窄
            if self._partner_pane is not None and idx < len(sizes) and sizes[idx] > 0:
                before = list(sizes)
                self._shrink_partner(sizes)
                sp = self._splitter
                if sizes != before and sp is not None:
                    sp.setSizes(sizes)

    def _on_collapse(self, sizes: List[int], idx: int) -> None:
        """卡片全隐：收起自身；伙伴窗格（若指定）恢复打开前宽度

        ★ 信号时序同 _on_expand：expandedChanged(False) 先发，宿主在回调里
        把 TabPanel 置回展开态（静默），随后伙伴恢复宽度时不会误 emit。
        """
        if self.isHidden():
            return
        sp = self._splitter
        if idx < len(sizes) and sp is not None:
            self._expanded_width = sizes[idx] if sizes[idx] > 0 else self._expanded_width or self.DEFAULT_EXPANDED_WIDTH
            self.expandedChanged.emit(False)  # 宿主先备 TabPanel 展开态
            sizes[idx] = 0
            self._restore_partner(sizes)
            sp.setSizes(sizes)
        self.hide()

    def _shrink_partner(self, sizes: List[int]) -> None:
        """把伙伴窗格收窄到目标尺寸，差额转给本 wrapper 槽位"""
        pane_idx = self._partner_splitter_index
        if self._partner_pane is None or pane_idx < 0 or pane_idx >= len(sizes):
            return
        target = self._partner_collapsed_size
        if target <= 0:
            target = self._partner_pane.minimumWidth()
        cur = sizes[pane_idx]
        if cur <= target:
            return
        if self._partner_saved_size <= 0:
            # 首次收窄：记忆打开前宽度（供关闭恢复）
            self._partner_saved_size = cur
        delta = cur - target
        sizes[pane_idx] = target
        if self._splitter_index < len(sizes):
            sizes[self._splitter_index] += delta

    def _restore_partner(self, sizes: List[int]) -> None:
        """把伙伴窗格恢复到卡片打开前的宽度（从本 wrapper 槽位归还）"""
        pane_idx = self._partner_splitter_index
        if self._partner_pane is None or pane_idx < 0 or pane_idx >= len(sizes):
            return
        if self._partner_saved_size <= 0:
            return
        cur = sizes[pane_idx]
        if cur >= self._partner_saved_size:
            return
        delta = self._partner_saved_size - cur
        if self._splitter_index < len(sizes) and sizes[self._splitter_index] >= delta:
            sizes[self._splitter_index] -= delta
            sizes[pane_idx] = self._partner_saved_size
        self._partner_saved_size = 0


class _WorkbenchFrame(QFrame):
    """工作台外框：动画期间可临时把 minimumSizeHint 归零

    QSplitter 经 QWidgetItemV2::minimumSize() 取窗格下限：显式 minimum 为
    0 / 无效时会回落到 minimumSizeHint()，所以 setMinimumWidth(0) 压不住
    内容最小宽——面板内容（页签条 / 正文页 / 任务区）把窗格硬顶在 ~230px。收起动画后半程宽度
    被钳在 230px 完全静止（实测约占 160ms 动画中的 96ms），最后一帧才
    frame.hide() 瞬间消失 → 用户感知「中途卡顿一会，然后才收起」。

    动画期间覆写 minimumSizeHint 返回 (0,0)，从源头摘掉内容最小宽，
    窗格才能真正收到 0；动画结束立刻恢复，不影响拖拽下限。
    """

    # [T30] 主题切换时不可见即置脏（_apply_theme_stylesheet 跳过重刷），
    # 由 _refresh_theme_dirty_frames 在可见性恢复点补刷
    _theme_needs_refresh: bool = False

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._min_hint_disabled = False

    def set_min_hint_disabled(self, disabled: bool) -> None:
        """切换「最小尺寸提示归零」；幂等，切换后主动 updateGeometry 让 splitter 重算"""
        disabled = bool(disabled)
        if self._min_hint_disabled == disabled:
            return
        self._min_hint_disabled = disabled
        self.updateGeometry()

    def minimumSizeHint(self):
        if self._min_hint_disabled:
            return QSize(0, 0)
        return super().minimumSizeHint()


class _ContentStack(QWidget):
    """对话区覆盖层堆栈（叠放实现 + 方向性滑动切换）：覆盖层页首次 show 后永不 hide。

    为什么不用 QStackedWidget（2026-09-08 排查，见
    docs/superpowers/specs/2026-09-08-content-stack-overlay-switch-design.md）：
    QStackedWidget 切到覆盖层页 = 整棵覆盖层子树 hidden→visible 的 show 传播
    （polish + 逐控件 Show 事件），实测 270-335ms，每次从对话切走都重付。
    叠放实现下覆盖层页首次 show 后常驻 visible（被对话页遮住），切换只
    raise_/lower + hide/show 对话页（双向 <12ms）。

    QStackedLayout::minimumSize 取**所有页**的最大值：page0 聊天页的最小宽会
    透过 splitter 链顶住把手。本实现 minimumSizeHint 只取**当前顶层可见页**，
    语义更准；`set_min_hint_disabled` 归零逻辑保留。

    布局：绝对定位（无布局管理器）。页级 push 滑动要求动画期间两页同时可见
    且水平错位，QGridLayout 同格叠放会在每次布局激活时把子项几何拉回原位，
    故改手动 setGeometry：resizeEvent / addWidget / 动画收尾统一归位。
    """

    currentChanged = Signal(int)

    # 页级滑动时长：大范围整块位移取阶梯最高档（design_tokens）
    SLIDE_MS = Animations.SLOW_MS

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._min_hint_disabled = False
        self._pages: List[QWidget] = []
        self._current = -1
        # ── 页级滑动状态（push 切页 / over 覆盖层内整页滑入共用）──
        self._slide_anim: Optional[QVariantAnimation] = None
        self._slide_old: Optional[QWidget] = None  # push=旧页 / over=垫底页
        self._slide_new: Optional[QWidget] = None  # push=新页 / over=滑动顶层页
        self._slide_dir = 0  # +1 新页从右侧进入（视口右移）/ -1 从左侧进入
        self._slide_kind = ""  # "push" / "over" / "swap"，驱动 tick 位移与收尾
        self._swap_cb: Optional[Callable[[], None]] = None  # swap 段间内容切换回调

    # ── 几何管理（无布局，resizeEvent 全量同步）──

    def resizeEvent(self, event: QResizeEvent) -> None:
        """容器 resize → 全部页铺满新 rect。

        ★ 动画在途**不收尾**：切页后宿主的 _sync_chat_wrapper_width 会在同一
        事件循环内改 wrapper margins，本栈随之 resize。早期版本在此直接
        _finish_slide(interrupted=True)，等于第一帧就把动画杀掉（实测「动画
        没了」的根因）。现在改为：更新记录的目标几何，下一页 tick 按新宽度
        重新计算位移；动画自身节奏不变。
        """
        super().resizeEvent(event)
        r = self.rect()
        if self.slide_in_progress():
            # 动画在途：只同步几何基准，位移由 _on_slide_tick 用新宽度接着算
            self._sync_slide_baseline(r)
            return
        for w in self._pages:
            w.setGeometry(r)

    def _sync_slide_baseline(self, r) -> None:
        """动画在途同步两页几何到新 rect（不打断动画）

        _on_slide_tick 每帧都用 self.width() 重新算位移，所以把两页铺到新
        rect 后，下一帧自然按新宽度继续滑，节奏不中断。
        """
        for w in (self._slide_old, self._slide_new):
            if w is not None:
                w.setGeometry(r)

    # ── QStackedWidget 兼容 API ──

    def addWidget(self, w: QWidget) -> int:
        """加入堆栈。首页可见并置顶；后续页可见但在下层（被首页遮住）。"""
        idx = len(self._pages)
        self._pages.append(w)
        w.setParent(self)
        w.setGeometry(self.rect())
        if idx == 0:
            w.setVisible(True)
            self._current = 0
        else:
            w.setVisible(True)
            w.lower()  # 压到首页之下，不遮对话区
        return idx

    def widget(self, i: int) -> Optional[QWidget]:
        if 0 <= i < len(self._pages):
            return self._pages[i]
        return None

    def currentIndex(self) -> int:
        return self._current

    def currentWidget(self) -> Optional[QWidget]:
        return self.widget(self._current)

    def setCurrentIndex(self, i: int, direction: int = 0) -> None:
        """切换当前页（长条模型：页面按 tab 顺序左右平铺，切换 = 视口在长条上滑动）。

        direction：+1 视口右移（新页从右侧进入、旧页向左退出，如点对话右侧的
        卡片 tab）、-1 视口左移（新页从左侧进入，如回最左的「对话」）、0 直接
        切换（初始化等场景）。非 0 且系统动效开启时走双页 push 滑动；
        动画期间页状态（_current / currentChanged）立即落定，仅旧页 hide
        延后到滑动结束（时长 300ms，期间对话页参与布局无功能影响）。
        """
        if i == self._current or not (0 <= i < len(self._pages)):
            return
        new = self._pages[i]
        old = self._pages[self._current] if self._current >= 0 else None
        self._current = i
        if direction != 0 and Animations.motion_enabled() and old is not None and old is not new:
            self._start_slide(old, new, direction)
        elif i == 0:
            # 回对话：show 对话页（实测 ~10ms）+ raise 盖住覆盖层。
            # ⚠️ 覆盖层页**不 hide**——保活不变式，消灭下次切换的 show 传播。
            if old is not None:
                old.lower()
            new.show()
            new.raise_()
        else:
            # 去覆盖层：覆盖层 raise 到顶；对话页 hide（实测 ~11ms，
            # 省布局 + 杜绝焦点穿透到被遮的对话区）
            new.show()
            new.raise_()
            if old is not None:
                old.hide()
        self.currentChanged.emit(i)

    def setCurrentWidget(self, w: QWidget) -> None:
        i = self._pages.index(w) if w in self._pages else -1
        if i >= 0:
            self.setCurrentIndex(i)

    # ── 页级滑动（push 切页 / over 覆盖层内整页滑入）──

    def slide_in_progress(self) -> bool:
        """是否有滑动动画在途（供调用方避免叠加动画）"""
        return self._slide_anim is not None and self._slide_anim.state() == QVariantAnimation.Running

    def _start_slide(self, old: QWidget, new: QWidget, direction: int) -> None:
        """双页 push 滑动：长条模型下视口在两页间平移（旧页滑出 + 新页滑入等速）。

        重入：动画在途先快进到终态（_finish_slide 幂等），再起新动画——
        连点表现为上一次立即滑到头，紧接着滑本次。
        """
        if self.slide_in_progress():
            self._finish_slide(interrupted=True)
        self._slide_old = old
        self._slide_new = new
        self._slide_dir = 1 if direction > 0 else -1
        self._slide_kind = "push"
        r = self.rect()
        # 两页同时可见；新页置顶，起点在进场侧整宽外（+dir 侧）
        old.show()
        new.show()
        new.raise_()
        old.setGeometry(r)
        new.setGeometry(r.translated(r.width() * self._slide_dir, 0))
        self._start_anim()

    def slide_over(self, direction: int = 1) -> None:
        """覆盖层内容切换的整页滑入（页 index 不变，无 swap 回调的轻量路径）:
        当前顶层页从进场侧滑入归位，露出区透传 chatFrame 背景（不垫底）。

        ⚠️ 不垫底是刻意的：垫对话页会让切换期间对话页强制可见 300ms
        （新卡首建阻塞主线程时动画帧被饿），用户实测不可接受。

        用途：切对话标签页恢复卡片等无法提供 swap 时机的事件链。
        卡间互斥切换走 slide_swap（两段式，旧卡画面真实滑出）。
        """
        top = self.widget(self._current)
        if top is None or not Animations.motion_enabled():
            return
        if self.slide_in_progress():
            self._finish_slide(interrupted=True)
        self._slide_old = None
        self._slide_new = top
        self._slide_dir = 1 if direction > 0 else -1
        self._slide_kind = "over"
        r = self.rect()
        top.raise_()
        top.setGeometry(r.translated(r.width() * self._slide_dir, 0))
        self._start_anim()

    def slide_swap(self, direction: int, swap) -> None:
        """卡间互斥切换的两段式滑动（长条模型「视口滑过相邻卡片」）：

        段1：顶层页带旧卡画面向出场侧整体滑出（完全出画）；
        段间：页在画外时执行 swap()（hide 旧卡 + show 新卡，构建阻塞被画外掩蔽）；
        段2：页瞬移到进场侧后滑入归位。

        旧卡画面真实跟随滑动（区别于 slide_over 的「新卡滑入+背景露出」）。
        swap 在动画收尾/打断时保证被执行（resizeEvent 快进等路径统一走
        _finish_slide），杜绝「高亮已切、卡片未切」的状态漂移。
        """
        top = self.widget(self._current)
        if top is None or not callable(swap):
            swap()
            return
        if not Animations.motion_enabled():
            swap()
            return
        if self.slide_in_progress():
            self._finish_slide(interrupted=True)
        self._slide_old = None
        self._slide_new = top
        self._slide_dir = 1 if direction > 0 else -1
        self._slide_kind = "swap"
        self._swap_cb = swap
        top.raise_()
        self._start_anim()

    def _start_anim(self) -> None:
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(self.SLIDE_MS)
        anim.setEasingCurve(Animations.EASE_ENTER)
        anim.valueChanged.connect(self._on_slide_tick)
        anim.finished.connect(self._finish_slide)
        self._slide_anim = anim
        anim.start()

    def _on_slide_tick(self, t: float) -> None:
        """每帧位移（长条模型）：

        push：视口右移时旧页 0→-w（左出）、新页 +w→0（右进），反向对称；
        over：仅顶层页从进场侧 +w→0 滑入；
        swap 段1：顶层页 0→-d*w（旧画面向出场侧滑出，段2 由 _finish_slide 接力）。
        """
        old, new = self._slide_old, self._slide_new
        if new is None:
            return
        w = self.width()
        d = self._slide_dir
        if self._slide_kind == "swap":
            new.move(round(w * d * -t), 0)
            return
        new.move(round(w * d * (1.0 - t)), 0)
        if self._slide_kind == "push" and old is not None:
            old.move(round(w * d * -t), 0)

    def _finish_slide(self, interrupted: bool = False) -> None:
        """滑动收尾/切段（幂等，可重复调用）

        swap 段1 完成（自然结束）：画外执行 swap 内容切换，页瞬移到进场侧后
        接力段2 滑入；被打断快进（resize/新切换接管几何）时 interrupted=True：
        仍执行 swap（保高亮/卡片状态一致）但直接归位不接段2。其余路径统一
        归位：new（新页/顶层页）置顶归位，old 按 _current 决定 hide（_current≠0）
        或 lower 保活（=0）。
        """
        anim = self._slide_anim
        if anim is not None:
            try:
                anim.finished.disconnect(self._finish_slide)
                anim.valueChanged.disconnect(self._on_slide_tick)
            except (TypeError, RuntimeError):
                pass
            anim.stop()
        self._slide_anim = None
        old, new = self._slide_old, self._slide_new
        if self._slide_kind == "swap" and new is not None:
            # 段1 完成：画外切换内容（cb 内含打断守卫：意图已变则跳过切换）
            cb = self._swap_cb
            self._swap_cb = None
            self._slide_kind = "over"
            if cb is not None:
                cb()
            if interrupted:
                self._settle(new, None)
                return
            new.setGeometry(self.rect().translated(self.width() * self._slide_dir, 0))
            self._start_anim()
            return
        self._slide_kind = ""
        self._slide_old = None
        self._slide_new = None
        self._slide_dir = 0
        self._swap_cb = None
        self._settle(new, old)

    def _settle(self, new, old) -> None:
        """统一归位：new 置顶归位；old 按 _current 决定 hide 或 lower 保活"""
        if new is not None:
            new.setGeometry(self.rect())
            new.raise_()
        if old is None:
            return
        old.setGeometry(self.rect())
        if self._current == 0:
            # 回对话：旧覆盖层页保持 visible + lower（保活不变式，被对话页遮住）
            old.lower()
        else:
            # 去覆盖层：对话页 hide（省布局 + 杜绝焦点穿透）
            old.hide()

    def set_min_hint_disabled(self, disabled: bool) -> None:
        """切换「最小尺寸提示归零」；幂等，切换后主动 updateGeometry 让布局重算"""
        disabled = bool(disabled)
        if self._min_hint_disabled == disabled:
            return
        self._min_hint_disabled = disabled
        self.updateGeometry()

    def minimumSizeHint(self):
        if self._min_hint_disabled:
            return QSize(0, 0)
        w = self.currentWidget()
        return w.minimumSizeHint() if w is not None else QSize(0, 0)


class TabManagerWindow(FramelessWindow):
    """Tab 管理器宿主窗口（单例）"""

    _instance: Optional["TabManagerWindow"] = None
    # 批5 5b/5c：壳先行（首窗构造中）状态标记
    _first_window_ready: bool = False
    _pending_new_tab_request: bool = False
    # 标题栏拖拽由 Windows 原生管理（HTCAPTION + WS_CAPTION），Python 不干预
    # Windows 原生移动/缩放模态循环消息：拖拽起止的权威信号，
    # 用于替代 moveEvent + 防抖定时器的"猜测式"拖拽检测
    _WM_ENTERSIZEMOVE = 0x0231
    _WM_EXITSIZEMOVE = 0x0232
    _WM_MOVING = 0x0216  # 仅"移动"触发；"缩放"发 WM_SIZING，二者互斥，可精确区分
    _WM_NCHITTEST = 0x0084  # 边缘/角落 resize 热区判定（自建，见 _native_hit_test）
    # ── 非客户区（NC）消息：切窗白框闪的抑制点（见 nativeEvent 说明）──
    _WM_NCACTIVATE = 0x0086  # 窗口激活状态变化 → 系统按默认外观重画非客户区（白框闪的来源）
    _WM_NCPAINT = 0x0085  # 要求绘制非客户区（帧/标题栏）
    _WM_ERASEBKGND = 0x0014  # 用窗口类背景刷擦除客户区（默认刷 = 白）
    # 焦点消息：基类 qframelesswindow 在注册表 DWM\ColorPrevalence=1（"在标题栏和
    # 边框上显示强调色"）时，会借它们把边框染成强调色 / 复位为系统默认描边，
    # 故仍在此拦截（本机 ColorPrevalence=0，该分支不会触发）。
    _WM_SETFOCUS = 0x0007
    _WM_KILLFOCUS = 0x0008

    # resize 热区宽度（逻辑 px，按窗口 DPI 缩放）。系统默认无边框热区为 0，
    # 基类 qframelesswindow 固定 5px 不随 DPI 变化，高 DPI 下几乎抓不到。
    _RESIZE_BORDER = 6
    # 顶边热区高度：只在窗口最上沿开一条窄带，避免吞掉标题栏主体及其按钮
    _TOP_RESIZE_BAND = 5

    tabCountChanged = Signal(int)
    activeTabChanged = Signal(int)

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

        # 启动分段打点（DEBUG）：定位 __init__ 内部耗时段（TrayManager 单例等）
        import time as _sm_time

        _sm_t0 = _sm_time.perf_counter()

        self._windows: List = []  # List[OpenAIChatToolWindow]
        # 最近一次顶部 tab 激活变化推断出的滑动方向（+1/-1/0）：
        # 点击切换时按 old/new 在 titleBar 的位置差缓存，切页消费后清零；
        # 无事件路径（关卡补位、程序切换）消费时端点兜底（开卡 +1 / 回对话 -1）
        self._pending_slide_direction = 0
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

        # ── 侧边栏内 left 卡片区（嵌入 sidebarSplitter）──
        # 卡片打开前的 TabPanel frame 宽（卡片关闭时恢复）与「卡片占位期间」标记
        self._left_card_saved_panel_frame_w: int = 0
        # 卡片打开期拖主把手：起点 tab 宽（保持不动）/ 目标 frame 宽 / 本次拖拽脏标记
        self._left_card_tab_w_keep: int = 0
        self._left_card_frame_target_w: int = 0
        self._left_card_drag_dirty: bool = False
        # 卡片打开前 TabPanel 是否处于折叠态（关闭卡片时按其恢复，避免
        # 「折叠态 → 开插件 → 关插件」意外展开侧边栏）
        self._left_card_prev_collapsed: bool = False

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
        # tab_id → 上次挂载的 TitlebarTabInfo 对象：插件热重载后 registry 中是
        # 全新 info 对象，身份不同 → 摘除旧 tab 重建；否则 titleBar 上挂的还是
        # 旧模块的 on_click 闭包，插件代码改动不生效
        self._plugin_titlebar_tab_infos: dict = {}
        # 标题栏高亮重算合并标志（见 _schedule_replace_highlight）
        self._replace_highlight_pending = False
        # tab 点击重活队列：show/hide 卡片链路是主线程长任务（WebEngine 卡片
        # 首建 + 系统卡互斥批量显隐），同步执行会饿死滑动指示器的动画帧；
        # 点击后动画先跑，重活延到动画结束后逐项执行（项间让一帧）
        self._tab_switch_queue: list = []
        self._tab_switch_timer: Optional[QTimer] = None

        self._setup_ui()
        self._setup_signals()
        # 不在 __init__ 设位置，等第一次 showEvent 时再设

        # ── 应用级服务启动已挪至 main.py（壳显示后、首窗构造前）──
        # [PERF 2026-09-14] GatewayService/PluginHostService.ensure_started()
        # （插件发现 55 个 + tools/agents/hooks 注册，实测 ~1.1s）原先同步卡在
        # __init__ 里挡住壳窗口出现。挪出后本构造只负责 UI；时序约束不变：
        # 首个 ChatWindow 仍必须在 ensure_started 之后构造（由 main.py 保证），
        # 否则首窗 _load_all_ui_plugins 会因 pm 未就绪静默 return 且无人重试。
        _sm_t1 = _sm_time.perf_counter()
        logger.debug(f"[TabManager] __init__ UI 构建段耗时 {(_sm_t1 - _sm_t0) * 1000:.0f}ms")

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
        _sm_t2 = _sm_time.perf_counter()
        logger.debug(f"[TabManager] __init__ API 会话/全局卡段耗时 {(_sm_t2 - _sm_t1) * 1000:.0f}ms")
        from app.tray_manager import TrayManager

        TrayManager.get_instance()._tab_manager_window = self
        logger.debug(
            f"[TabManager] __init__ TrayManager 单例段耗时 {(_sm_time.perf_counter() - _sm_t2) * 1000:.0f}ms"
        )

        # 注册主题刷新回调（虽主题切换路径不走 dispatch_refresh，
        # 但保持接口一致性便于将来扩展）
        theme_manager.register_refresh_target(self)

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
        # 🆕 页签按对话窗口独立记忆：页签变化 → 写入当前活跃窗口；切回窗口时恢复
        self.workbench_panel.current_tab_changed.connect(self._remember_workbench_tab)
        # 页面请求切换工作目录 → 转发给当前活跃窗口（实例缓存/分支标签/团队广播）
        self.workbench_panel.workingDirChanged.connect(self._on_workbench_working_dir_changed)
        self.titleBar.workbench_toggle_requested.connect(self.toggle_workbench)
        # 主题 / 字号刷新注册
        theme_manager.register_refresh_target(self.workbench_panel)

        # 圆角矩形容器（与 #tabFrame / #chatFrame 同款），作为 splitter 第三窗格。
        # 直接给 #workbenchPanel 设 border 会被 splitter handle 绘制顺序吞掉，
        # 故同左侧一样再包一层 QFrame 用 objectName 上样式。
        self._workbench_frame = _WorkbenchFrame()
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

        # ── 侧边栏 hover 悬浮预览（见 sidebar_hover_preview）──
        # 标题栏 workbench 按钮 hover → 控制器 → 浮层 fade_in/out；
        # 浮层自身不发信号，由 eventFilter 转发 HoverEnter/Leave 给控制器。
        from app.widgets.sidebar_hover_preview import (
            HoverPreviewOverlay,
            HoverPreviewController,
        )
        from app.widgets.custom_title_bar import CustomTitleBar

        _tb_h = CustomTitleBar.MAC_HEIGHT if self.titleBar._is_mac else CustomTitleBar.HEIGHT
        # 路线 C（决策 D025）：浮层是 Qt.Tool 顶层 owned 窗口（独立 HWND，按需
        # show/hide），不占主窗口客户区 → 不破坏 frameless 边缘 resize；owned
        # z-order 恒在主窗口之上 → 盖得住 WebEngine。路线 A 的 WA_NativeWindow
        # 常驻子 HWND 已实测证伪（四边 resize 全废），勿回退。
        self._wb_overlay = HoverPreviewOverlay(self, side="right", titlebar_h=_tb_h)
        self._wb_overlay.installEventFilter(self)
        self._wb_preview_ctrl = HoverPreviewController(
            self._wb_overlay,
            can_preview=lambda: not self.is_workbench_visible(),
            on_enter=self._wb_preview_enter,
            on_leave=self._wb_preview_leave,
        )
        self.titleBar.workbench_hover_changed.connect(self._wb_preview_ctrl.on_button_hover)
        self._wb_suppress_memory = False
        self._wb_in_preview = False
        self._wb_promote_on_leave = False

        # 初始加载全局背景图（延迟到首帧后，背景为纯装饰，不阻塞出现）
        QTimer.singleShot(0, self._apply_bg_from_theme)

        # 顶栏内置「聊天」常驻 tab（full 卡片 tab 由显隐事件动态增删，
        # 插件常驻 tab 由 _sync_plugin_titlebar_tabs 挂载）
        self.titleBar.add_tab(CHAT_TAB_ID, "对话")
        self._sync_plugin_titlebar_tabs()

    def _on_titlebar_tab_clicked(self, tab_id: str):
        """顶栏 tab 点击：「聊天」→ 对话视图；full 卡片 tab → 切换/显示；
        插件常驻 tab 的展示由注册时的 on_click 回调处理，此处忽略。

        ★ 重活延后：show/hide 卡片链路是主线程长任务（WebEngine 卡片首次
        创建 + 系统卡互斥批量显隐），同步执行会饿死滑动指示器的动画帧
        （实测「点 tab 看不到滑动、直接跳变」，内容轻时偶现正常）。
        这里只入队，重活由 _drain_tab_switch_work 在动画结束后执行。
        连点不取消只顺延：toggle 语义依赖执行时的卡片状态，乱序取消会
        产生与点击序列相反的最终状态。

        无论走哪条分支，最后都调度一次高亮收敛（见 ``_sync_replace_highlight``）：
        插件常驻 tab 的 on_click 内部若把卡片 toggle 成隐藏，高亮必须回退，
        而这条路径此前完全没有同步点。
        """
        logger.debug(f"[TitleBar] tab clicked: {tab_id}")
        self._enqueue_tab_switch_work(tab_id)
        self._schedule_replace_highlight()

    def _enqueue_tab_switch_work(self, tab_id: str) -> None:
        """tab 切换重活入队，定时器对齐滑动动画结束（减少动态效果时立即执行）"""
        from app.widgets.custom_title_bar import TabIndicatorController

        self._tab_switch_queue.append(tab_id)
        if self._tab_switch_timer is None:
            self._tab_switch_timer = QTimer(self)
            self._tab_switch_timer.setSingleShot(True)
            self._tab_switch_timer.timeout.connect(self._drain_tab_switch_work)
        if not self._tab_switch_timer.isActive():
            delay = 0 if not Animations.motion_enabled() else TabIndicatorController.ANIM_MS + 30
            self._tab_switch_timer.start(delay)

    def _drain_tab_switch_work(self) -> None:
        """依次执行排队的 tab 切换重活；相邻两项之间让出一帧，避免同帧连发集中阻塞"""
        if not self._tab_switch_queue:
            return
        tab_id = self._tab_switch_queue.pop(0)
        if tab_id == CHAT_TAB_ID:
            self._show_conversation_view()
        elif tab_id not in self._plugin_titlebar_tab_ids:
            self._on_replace_tab_clicked(tab_id)
        if self._tab_switch_queue:
            QTimer.singleShot(0, self._drain_tab_switch_work)

    def _apply_win11_dwm_chrome(self):
        """Win11 DWM 窗口外观：圆角 + **不画**边框描边（Win10 及更早静默跳过）

        - ``DWMWA_WINDOW_CORNER_PREFERENCE(33) = DWMCP_ROUND(2)`` → 窗口圆角；
        - ``DWMWA_BORDER_COLOR(34) = DWMWA_COLOR_NONE(0xFFFFFFFE)`` → DWM 跳过
          最外圈那 2px 描边的绘制。

        ★ 为什么必须压掉描边（2026-09-12 像素级实测）：
        本窗口为了边缘 resize / Aero Snap 补回了 ``WS_THICKFRAME``，DWM 因此把
        它当"有框窗口"，会在窗口最外圈画一条 ``DWMWA_VISIBLE_FRAME_BORDER_
        THICKNESS`` 像素宽的描边，且颜色随**激活状态**变化（失焦时变亮）。因此
        把它设为 ``DWMWA_COLOR_NONE``，消除稳定态下这条"会随焦点变色的描边"。
        注意描边**由 DWM 画**，与自建的 ``_native_hit_test`` 热区（纯判定、
        不绘制任何像素）无关。

        ★ 本方法**不能**消除"切窗时闪一下白框"：实测（焦点切换后逐帧抓屏）
        过渡帧仍会在最外圈画出纯白 #FFFFFF（左/上 13px、右/下 1px），下一帧
        即消失 —— 那是一次由系统在激活过渡帧完成的重画，只能在消息层拒掉，
        见 ``TabManagerWindow.nativeEvent`` 对 WM_NCACTIVATE / WM_NCPAINT /
        WM_ERASEBKGND 的拦截。

        ★ 为什么不设成"和背景同色的描边"：``DWMWA_BORDER_COLOR`` 只接受
        COLORREF，无 alpha 通道，画不出半透明描边；而 ``DWMWA_COLOR_NONE`` 是
        **跳过描边绘制**，视觉上等价于把描边彻底去掉，官方说明其可保留圆角，
        且不影响 DWM 投影阴影（实测确认：描边消失、窗口外侧阴影像素不变）。

        幂等、极廉价（两条 DwmSetWindowAttribute）；hwnd 会因 setWindowFlags /
        跨屏 DPI 变化重建，重建后 DWM 属性随之丢失，故挂在
        ``_ensure_native_window_styles`` 里随每次 showEvent 一起重设。
        """
        if not _IS_WINDOWS or _dwmapi is None:
            return
        try:
            hwnd = int(self.winId())
            if hwnd == 0:
                return
            pref = _ctypes.c_int(_DWMCP_ROUND)
            _dwmapi.DwmSetWindowAttribute(hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, _ctypes.byref(pref), 4)
            border = _ctypes.c_uint32(_DWMWA_COLOR_NONE)
            _dwmapi.DwmSetWindowAttribute(hwnd, _DWMWA_BORDER_COLOR, _ctypes.byref(border), 4)
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

        # 刷新右侧工作台面板（页签条 / 任务区 / 内置页 / 插件页签 / 卡片 tab
        # 全在 workbench_panel.refresh_style 内逐页分发，独立于本窗口 QSS）
        # ★ GUI 手动切主题走 main_widget._execute_batched_theme_refresh（不走
        #   theme_manager.dispatch_refresh），注册在 _refresh_targets 里的
        #   workbench_panel 回调永远不会被触发，本方法才是 batched 路径触达
        #   TabManager 区域的唯一入口。此前漏刷 → 右栏插件页内容停留旧主题。
        #   置于方法前部 + 独立 try：后方 refresh 调用抛异常时不再腰斩本刷新
        #   （_empty_state/titleBar 等未受保护，任一异常都会中断整段方法）。
        #   dispatch 路径下 panel 另有独立注册，此处会重复刷新——低频云同步/
        #   主题热重载路径，refresh_style 幂等，双刷可接受。
        try:
            panel = getattr(self, "workbench_panel", None)
            if panel is not None:
                panel.refresh_style()
        except Exception:
            logger.exception("[TabManagerWindow] workbench panel theme refresh failed")

        # hover 预览浮层是独立顶层窗口，不继承本窗口 QSS，需单独刷新；
        # 预览中的 frame 带内联样式（脱离 QSS 作用域），同步刷新
        overlay = getattr(self, "_wb_overlay", None)
        if overlay is not None:
            overlay.refresh_style()
        if getattr(self, "_wb_in_preview", False):
            self._workbench_frame.setStyleSheet(self._wb_frame_inline_qss())

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
        # 刷新全局卡片容器背景（主题色/边框随主题切换）
        for _c in (
            getattr(self, "_global_top_container", None),
            getattr(self, "_global_bottom_container", None),
            getattr(self, "_global_left_container", None),
        ):
            if _c is not None:
                try:
                    _c.refresh_style()
                except Exception:
                    pass
        # 刷新全局卡片本体（系统设置/服务商/Hook/MCP 编辑等挂在本窗口层，
        # 不在 main_widget widget 树内，主题/字号刷新链扫不到，需显式触发）
        try:
            from app.widgets.cards.global_card_controller import get_global_card_controller

            _controller = get_global_card_controller()
            if _controller is not None:
                _controller.refresh_theme_styles()
        except Exception:
            logger.exception("[TabManagerWindow] global cards theme refresh failed")
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

        ★ 若当前正处 hover 预览态：点击应转为常驻展开（spec）——
        置 _wb_promote_on_leave 后再 on_clicked()，回调里走「常驻 True」分支；
        非预览态才走翻转。
        """
        ctrl = getattr(self, "_wb_preview_ctrl", None)
        if ctrl is not None and ctrl.is_previewing():
            # 标志交由 _wb_preview_leave 的 _done（slide_out 异步回调）读取并复位，
            # 此处不能提前复位：否则异步 _done 跑时读到 False → 误走收起而非转常驻。
            self._wb_promote_on_leave = True
            ctrl.on_clicked()  # 触发 _wb_preview_leave → slide_out → _done 里 promote
            return
        self.set_workbench_visible(not self.is_workbench_visible(), persist=True)

    def is_workbench_visible(self) -> bool:
        """工作台当前是否可见（动画期间返回目标状态，避免半途状态误判）"""
        if getattr(self, "_wb_visible_target", None) is not None:
            return bool(self._wb_visible_target)
        frame = getattr(self, "_workbench_frame", None)
        return bool(frame is not None and frame.isVisible())

    # ── 工作台显隐动画 ──

    def _apply_wb_width(self, w: float) -> None:
        """动画帧：把工作台窗格宽度推到指定值（从对话区借/还空间，总宽不变）

        几何用 _start_wb_anim 时的快照（总宽/左侧栏宽）计算三值，配合 round 取整：
        每帧读写 splitter 会引入量化漂移；round 比 int 截断帧间步进更均匀。"""
        frame = getattr(self, "_workbench_frame", None)
        if frame is None:
            return
        try:
            total = getattr(self, "_wb_anim_total", 0)
            left = getattr(self, "_wb_anim_left", 0)
            if total <= 0:
                sizes = self._splitter.sizes()
                if len(sizes) < 3:
                    return
                total = sum(sizes)
                left = sizes[0]
            wb = max(0, min(int(round(w)), frame.maximumWidth()))
            chat = max(0, total - left - wb)
            self._splitter.setSizes([left, chat, wb])
        except Exception:
            pass

    def _set_wb_width_now(self, w: int) -> None:
        """无动画直接落位工作台窗格宽度（per-tab 显隐瞬切路径专用）

        一次性 setSizes，无逐帧量化问题，直接读当前 sizes 即可。"""
        frame = getattr(self, "_workbench_frame", None)
        if frame is None:
            return
        try:
            sizes = self._splitter.sizes()
            if len(sizes) < 3:
                return
            wb = max(0, min(int(w), frame.maximumWidth()))
            total = sum(sizes)
            self._splitter.setSizes([sizes[0], max(0, total - sizes[0] - wb), wb])
            logger.debug(f"[TSW] wb瞬切 wb={wb} left={sizes[0]}->{self._splitter.sizes()[0]}")
        except Exception:
            pass

    def _start_wb_anim(self, start_w: int, end_w: int, on_finished=None) -> None:
        """启动工作台宽度动画（200ms OutCubic）；复用动画对象，stop 后重设起止值

        对齐侧栏 #31：动画期间不再冻结 _content_area 重绘（中间区域原样实时
        显示），改为抑制卡片 resize 预览模式，由 _on_wb_anim_finished 统一恢复。

        ★ 动画期间必须同时放开 panel 自身的最小宽度：frame.setMinimumWidth(0)
        管不住 minimumSizeHint——panel 的 setMinimumWidth(PANEL_WIDTH_MIN) 会把
        frame 实际下限顶在 334px，导致展开/收起动画前段被硬性钳住不动、尾段猛跳
        （用户感知的"动画卡顿"主因）。
        ★ 只放开 panel 显式最小宽还不够：面板内部内容（页签条 / 正文页 / 任务区）
        经由 minimumSizeHint 继续把窗格顶在 ~230px —— 收起动画后半程（实测
        160ms 中的 ~96ms）宽度卡在 230px 完全静止，最后一帧才 hide() 瞬间消失
        （"中途卡顿一会然后才收起"的真正根因）。故动画期间一并覆写
        frame.minimumSizeHint（见 _WorkbenchFrame）。"""
        self._set_windows_resize_preview_suppressed(True)
        panel = getattr(self, "workbench_panel", None)
        if panel is not None:
            panel.setMinimumWidth(0)
        frame = getattr(self, "_workbench_frame", None)
        # 帧内几何快照：动画期间总宽与左（侧栏）窗格不变，每帧直接用快照算三值。
        # 不再每帧读回 splitter.sizes() —— 读回会被 QSplitter 的整数分配量化，
        # 左侧栏与对话区窗格产生 ±1px 帧间漂移（感知为动画抖动）。
        try:
            sizes = self._splitter.sizes()
            self._wb_anim_total = sum(sizes)
            self._wb_anim_left = sizes[0] if len(sizes) >= 3 else 0
        except Exception:
            self._wb_anim_total = 0
            self._wb_anim_left = 0
        anim = self._wb_anim
        if anim is None:
            anim = QVariantAnimation(self)
            anim.valueChanged.connect(lambda v: self._apply_wb_width(v))
            anim.finished.connect(self._on_wb_anim_finished)
            self._wb_anim = anim
        # 中途反向重启时旧收尾回调直接丢弃，只执行最新一次的回调
        self._wb_anim_finished_cb = on_finished
        anim.stop()
        # ★ 系统「减少动态效果」：跳过插值直接落位，并走**同一条收尾路径**
        # （预览抑制、panel 最小宽度、frame 最小尺寸提示都靠它恢复），
        # 否则这几项开关会永久停在「动画中」状态。
        if not Animations.motion_enabled():
            self._apply_wb_width(float(end_w))
            self._on_wb_anim_finished()
            return
        # ★ 最小尺寸提示归零必须在 anim.stop() 之后：stop 不发射 finished，
        # 旧回调被丢弃，此处统一保证新动画从"无下限"状态起跑。
        if frame is not None:
            frame.set_min_hint_disabled(True)
        # ★ 曲线/时长按方向分离：Duration/曲线走全局 token —— 进入
        # ENTER_MS + EASE_ENTER（快速露出内容）；离场 EXIT_MS + EASE_EXIT。
        # 离场不能用 OutCubic：其尾段拖尾（后 12.5% 宽度要磨掉一半时间）
        # 观感为“收到一半顿一下才收完”（用户实测反馈）。
        collapsing = end_w < start_w
        anim.setDuration(Animations.EXIT_MS if collapsing else Animations.ENTER_MS)
        anim.setEasingCurve(QEasingCurve(Animations.EASE_EXIT if collapsing else Animations.EASE_ENTER))
        # ★ 设值期间屏蔽信号：QVariantAnimation 在 setEndValue 后会立刻以
        # 新终值补发一次 valueChanged，导致动画尚未 start 就先 _apply_wb_width(0)
        # 把窗格瞬间推到下限（收起起手闪一下）。起止值就绪后再解除屏蔽。
        anim.blockSignals(True)
        anim.setStartValue(float(start_w))
        anim.setEndValue(float(end_w))
        anim.blockSignals(False)
        anim.start()

    def _on_wb_anim_finished(self) -> None:
        """动画结束：执行收尾回调 + 恢复卡片 resize 预览能力

        若本回调又同步启动了新动画（反向重启），则跳过恢复（预览抑制与
        panel 最小宽度、frame 最小尺寸提示都交由其 finished 恢复），
        避免动画中途解除。"""
        cb = getattr(self, "_wb_anim_finished_cb", None)
        self._wb_anim_finished_cb = None
        if cb is not None:
            cb()
        if self._wb_anim is not None and self._wb_anim.state() == QVariantAnimation.Running:
            return
        # 收尾回调已把 frame/panel 的显式最小宽恢复（PANEL_WIDTH_MIN + 14），
        # 此处解除 minimumSizeHint 覆写，拖拽下限回到内容最小宽。
        frame = getattr(self, "_workbench_frame", None)
        if frame is not None:
            frame.set_min_hint_disabled(False)
        self._set_windows_resize_preview_suppressed(False)
        # [T30] 动画结束可见性恢复点：补刷隐藏期置脏的顶层容器
        self._refresh_theme_dirty_frames()

    # ── 右侧工作台 hover 悬浮预览：进入/退出回调 ──

    def _wb_frame_inline_qss(self) -> str:
        """预览期 #workbenchFrame 内联样式（与 _apply_theme_stylesheet 同款）

        frame 被摘到 hover 预览浮层后脱离本窗口 QSS 作用域，需内联补同款；
        主题切换时若预览中，也走本方法刷新。
        """
        return (
            "#workbenchFrame {"
            f" background: {Colors.CARD_BG.format(alpha=150)};"
            f" border: 1px solid {Colors.BORDER};"
            " border-radius: 8px;"
            " }"
        )

    def _wb_preview_enter(self) -> None:
        """控制器 on_enter：把工作台 frame 摘到浮层做纯几何滑入。

        ★ 全程不调 set_workbench_visible：预览是浮层盖在**已稳定**的对话区上，
        frame 关闭态在 splitter 里本就 0 宽、摘出后 chatFrame 仍占满不 resize，
        也就不切 chatFrame margin、不写 _workbench_frame_w → 消除消息卡闪 + 宽度重置。
        """
        from app.widgets.workbench_panel import PANEL_WIDTH_MIN

        frame = self._workbench_frame
        panel = self.workbench_panel
        self._wb_in_preview = True
        self._wb_visible_target = False  # 预览≠打开：is_workbench_visible() 保持 False，不污染
        w = int(getattr(self, "_workbench_frame_w", PANEL_WIDTH_MIN + 14))
        frame.setParent(self._wb_overlay)
        self._wb_overlay.set_content(frame)
        # ★ reparent 后 frame 脱离本窗口 QSS 作用域，#workbenchFrame 规则失效
        #   （QSS 继承基于父链）→ 补同款内联样式，否则预览态丢面板底/边框；
        #   回挂 splitter 时清掉（_wb_preview_leave），避免内联样式残留压住后续主题刷新
        frame.setStyleSheet(self._wb_frame_inline_qss())
        panel.set_panel_visible(True)
        panel.setMinimumWidth(0)  # 滑入期放开下限，避免 panel 最小宽顶住浮层展开
        panel.setUpdatesEnabled(False)  # 滑入期冻结内容重绘（对齐嵌入展开"先滑后显"观感）
        target = max(w, PANEL_WIDTH_MIN + 14)

        def _shown() -> None:
            panel.setUpdatesEnabled(True)
            panel.setMinimumWidth(PANEL_WIDTH_MIN)
            self.refresh_workbench()

        self._wb_overlay.slide_in(target, on_done=_shown)

    def _wb_preview_leave(self) -> None:
        """控制器 on_leave：浮层滑出后把 frame 以 0 宽 hide 回挂 splitter 第三窗格。

        非点击路径不复用 set_workbench_visible(False)——那会把 reparent 残值写进
        _workbench_frame_w（宽度重置）并切 chatFrame margin（消息闪）。回挂后 frame
        保持隐藏 0 宽，chatFrame 宽度自始至终不变。仅点击转常驻才走嵌入展开动画。
        """
        if not self._wb_in_preview:
            return
        self._wb_in_preview = False

        def _done() -> None:
            frame = self._workbench_frame
            panel = self.workbench_panel
            panel.set_panel_visible(False)
            self._wb_overlay.clear_content()
            # ★ 回挂前先隐藏：QSplitter 不给隐藏窗格分宽度，chatFrame 全程不被挤
            #   → 消除关闭瞬间的消息卡闪烁（原 bug：frame 仍 visible 时被塞回 splitter）
            frame.hide()
            self._wb_overlay.hide()
            frame.setParent(self._splitter)
            self._splitter.insertWidget(2, frame)
            # 回挂后重新继承本窗口 QSS：清内联样式，防残留压住 _apply_theme_stylesheet
            frame.setStyleSheet("")
            if self._wb_promote_on_leave:
                # 点击转常驻：显式走嵌入展开（动画 + chatFrame margin 切换 + 记忆落账 True）
                self._wb_promote_on_leave = False
                self.set_workbench_visible(True)
            else:
                self._wb_visible_target = None  # 回挂收起后回到"看 isVisible"，关闭态 False
            # [T30] 预览离开可见性恢复点：补刷隐藏期置脏的顶层容器
            self._refresh_theme_dirty_frames()

        self._wb_overlay.slide_out(on_done=_done)

    def _stop_wb_anim(self) -> None:
        anim = getattr(self, "_wb_anim", None)
        if anim is not None:
            anim.stop()  # stop 不发射 finished；动画对象保留供复用，
            # 挂起的收尾回调由下一次 _start_wb_anim 覆盖或 _on_wb_anim_finished 丢弃
        # stop 不触发 finished → 最小尺寸提示覆写必须在此兜底恢复，
        # 否则用户可把手柄拖到 0（拖拽下限被永久摘除）。
        frame = getattr(self, "_workbench_frame", None)
        if frame is not None:
            frame.set_min_hint_disabled(False)

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

    def set_workbench_visible(self, visible: bool, animate: bool = True, persist: bool = False) -> None:
        """显示/隐藏工作台（带 200ms 宽度展开/收拢动画；animate=False 瞬切）

        隐藏时记忆当前宽度，展开时恢复。动画期间重复触发会重启反向动画。
        显隐状态写入当前活跃对话窗口（per-tab 显隐记忆，切换窗口时由
        _on_tab_selected 按目标窗口记忆瞬切恢复）。

        Args:
            persist: True 时写入用户显隐记忆（规则 4）；仅用户手动开关路径传，
                per-tab 瞬切恢复/挤压协调/定向打开等程序化路径不写。
        """
        frame = getattr(self, "_workbench_frame", None)
        panel = getattr(self, "workbench_panel", None)
        if frame is None or panel is None:
            return
        visible = bool(visible)
        if persist:
            # 规则 4：用户手动开关记忆终态（置前于 early-return，保证每次手动点击都落账）
            from app.utils.config import Settings

            Settings.get_instance().ui_workbench_visible.value = visible
        # per-tab 显隐记忆：无论走哪条路径（含状态一致的 early-return）都以
        # 当前活跃窗口为准落账，切换标签页时按目标窗口记忆恢复
        # ★ 预览路径（_wb_suppress_memory=True）只复用落位/数据，不写记忆
        cur = self.get_current_window()
        if cur is not None and not getattr(self, "_wb_suppress_memory", False):
            cur._workbench_visible_memory = visible
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
            # [T30] 瞬切打开（animate=False：启动恢复 / 切窗 per-tab 恢复）不跑
            # 动画 → 没有动画结束补刷点，frame 若在隐藏期被主题刷新置脏，会带
            # 空 styleSheet 显示 → 无背景/边框/圆角。此处立即补刷；未置脏时
            # 本调用为 no-op，动画路径动画结束后另有一次（同串短路，零开销）。
            self._refresh_theme_dirty_frames()
            # ★ 不调 restore_last_tab：hide 期间 stack 当前页本就保持，
            #   重新 show 天然恢复用户上次选择的页签；首次默认第一个页签由
            #   面板 __init__ 保证。若在此恢复上次关闭时页签，会覆盖
            #   「隐藏状态下定向打开」刚设置的页签（如插件卡片 tab）。
            self._set_chat_frame_wb_hidden(False)
            # 动画期间放开最小宽度约束，QSplitter 才允许窗格拉到中间值
            frame.setMinimumWidth(0)
            target_w = max(0, min(getattr(self, "_workbench_frame_w", PANEL_WIDTH_DEFAULT + 14), frame.maximumWidth()))
            if animate:
                # ★ 动画期冻结工作台内容渲染：新暴露区域显示背景色（空面板展开），
                # 内部页（历史列表/树等大量子控件）不再逐帧重绘重排 —— 开关卡顿主因。
                # 完全展开后再恢复渲染并填充数据（用户认可的“先不渲染后显示”）。
                panel.setUpdatesEnabled(False)

                def _wb_expand_finished() -> None:
                    panel.setUpdatesEnabled(True)
                    frame.setMinimumWidth(PANEL_WIDTH_MIN + 14)
                    panel.setMinimumWidth(PANEL_WIDTH_MIN)
                    # 数据填充推迟一帧：refresh_workbench（页签 reconcile + 树/列表刷新）
                    # 同步跑在动画收尾帧内会让结尾肉眼可见地顿一下，先让末帧绘制完成
                    QTimer.singleShot(0, self.refresh_workbench)

                self._start_wb_anim(
                    start_w,
                    target_w,
                    # 数据填充（refresh_workbench 含页签 reconcile + 树/列表刷新）
                    # 延后到动画结束，避免与首帧布局争抢主线程
                    on_finished=_wb_expand_finished,
                )
            else:
                # 瞬切（per-tab 显隐恢复）：直接落位，不动画不延帧。
                # ★ 若此前动画被 stop 打断（finished 未跑），冻结标志残留，
                # 必须在此恢复，否则面板内容永不重绘（空白面板）
                panel.setUpdatesEnabled(True)
                frame.setMinimumWidth(PANEL_WIDTH_MIN + 14)
                panel.setMinimumWidth(PANEL_WIDTH_MIN)
                self._set_wb_width_now(target_w)
                self.refresh_workbench()
        else:
            if start_w > 0:
                self._workbench_frame_w = start_w
            frame.setMinimumWidth(0)
            if animate:
                # ★ 收起同样冻结内容渲染：面板显示最后一帧静态内容被逐渐裁剪，
                # 内部页不再逐帧重绘（与展开期冻结同源同收益）
                panel.setUpdatesEnabled(False)
                self._start_wb_anim(
                    start_w,
                    0,
                    # ★ 收尾顺序：先隐藏面板再恢复渲染标志——
                    # setUpdatesEnabled(True) 会调度一次全量重绘，若在面板仍可见时
                    # 恢复，收完瞬间会重绘一次大内容（感知为收尾顿一下）
                    on_finished=lambda: (
                        panel.set_panel_visible(False),
                        frame.hide(),
                        frame.setMinimumWidth(PANEL_WIDTH_MIN + 14),
                        panel.setMinimumWidth(PANEL_WIDTH_MIN),
                        self._set_chat_frame_wb_hidden(True),
                        panel.setUpdatesEnabled(True),
                    ),
                )
            else:
                # 瞬切收起：对齐动画收尾动作，一次性完成（含恢复冻结标志）
                panel.setUpdatesEnabled(True)
                panel.set_panel_visible(False)
                frame.hide()
                frame.setMinimumWidth(PANEL_WIDTH_MIN + 14)
                panel.setMinimumWidth(PANEL_WIDTH_MIN)
                self._set_chat_frame_wb_hidden(True)

    def _hide_workbench(self) -> None:
        """工作台关闭按钮：直接隐藏（实例保留，再次开启零重建）"""
        self.set_workbench_visible(False, persist=True)

    def _remember_workbench_tab(self, index: int) -> None:
        """用户主动切页回调：把页签记到当前活跃对话窗口（按窗口独立记忆）

        ★ 仅用户路径触发：current_tab_changed 已收窄为 set_current_tab(user=True)
        才发射（页签点击 / 定向入口 / 显式关闭），程序化切换（历史页换挂延续、
        切窗恢复 saved）不发射、不写记忆，避免后台切换覆盖活跃窗口的原记忆。
        无活跃窗口/面板未挂载时静默跳过（信号驱动，零轮询）。

        ★ 按 tab_id 记忆而非裸 index：各窗口的 tab 集合可能不同（卡片 tab
        per-tab 投影 / 历史页懒挂载 / 插件页），裸 index 在切窗恢复时会撞到
        别的页签或越界 → 「选中残留/丢失」根因。restore 侧按 id 重新定位。
        """
        win = self.get_current_window()
        if win is None:
            return
        try:
            panel = getattr(self, "workbench_panel", None)
            ids = panel._tab_ids if panel is not None else ()
            win._workbench_tab_memory = ids[index] if 0 <= index < len(ids) else None
        except Exception:
            pass

    def refresh_workbench(
        self,
        force: bool = False,
        force_plugin: str = "",
    ) -> None:
        """从当前活跃窗口拉取数据填充工作台（产物/项目记忆）

        数据源均为既有单一数据源：
        - 产物：backend.file_recorder 会话级文件写入记录
        - 项目：win._current_project + _current_workdir → MemoryCardContent

        任务（todowrite）已迁至消息卡片内嵌看板（见 app/widgets/inline_todo_panel.py），
        不再属于工作台数据。

        force=True：插件页强制重建（ui 热重载后签名未变但实现已变）。

        ★ force_plugin：只定向重建归属该插件的工作台页（热重载单个插件时避免
        连带销毁其余插件的页）。留空时沿用 force 的全量语义。判定无状态，
        由面板按「已挂载页记录的归属」就地比对。
        """
        panel = getattr(self, "workbench_panel", None)
        if panel is None or not panel.isVisible():
            return
        # 插件工作台页签 reconcile（内置产物/记忆 + 插件注册页）
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            panel.sync_plugin_pages(
                UIPluginRegistry.get_instance().get_workbench_tabs(),
                force=force,
                force_plugin=force_plugin,
            )
        except Exception:
            pass
        # 当前页数据刷新：页面自拉（插件页可选协议 refresh_data()）。
        # ★ 宿主不再为具体页面推送数据（原 update_project / update_artifacts），
        #   页面自己从 context / 活跃窗口取数，面板保持零页面语义。
        try:
            panel.refresh_current_page_data()
        except Exception:
            logger.exception("[Workbench] 当前页数据刷新失败")

    def _deferred_workbench_refresh(self, win, saved_tab, seq: int = 0) -> None:
        """切标签后的工作台数据刷新（延迟一帧执行，见 _on_tab_selected）

        含刷新后的页签恢复（refresh_workbench 可能因历史页保持/插件页
        reconcile 改变当前页，故恢复放在其之后）。仅处理窗口显式记忆过的
        页签；未记忆过的窗口保持现状不跳页。★ 记忆的是 tab_id（见
        _remember_workbench_tab）：按 id 在当前页签集合中重新定位，找不到
        （页签已卸载）则保持现状，不越界跳页也不把全部按钮高亮熄灭。
        ★ seq 过期作废：调度后用户又切了标签 → 本次是旧切换的遗留，
        执行会用旧窗口的页签记忆覆盖新活跃窗口状态（per-tab 串态根因）。
        """
        if seq and seq != getattr(self, "_wb_refresh_seq", 0):
            return  # 已有更新的切换调度，本次作废
        panel = getattr(self, "workbench_panel", None)
        if panel is None or not self.is_workbench_visible():
            return
        try:
            if win is None or win not in self._windows:
                return  # 延迟期间目标标签已被关闭
        except RuntimeError:
            return
        self.refresh_workbench()
        if saved_tab is None:
            return
        try:
            if isinstance(saved_tab, int):
                # 旧版按 index 记忆的兼容（本次修复前写入的存量值）
                saved_tab = panel._tab_ids[saved_tab] if 0 <= saved_tab < len(panel._tab_ids) else None
            if saved_tab is not None:
                idx = panel._tab_id_index(saved_tab)
                if idx is not None and panel.current_tab() != idx:
                    panel.set_current_tab(idx)
        except RuntimeError:
            pass

    def _on_workbench_working_dir_changed(self, file_path: str) -> None:
        """工作树页内工作目录变更 → 转发给当前活跃窗口

        右侧工作台是全局单例，workingDirChanged 来自共享的 MemoryCardContent；
        工作目录是窗口级状态（实例缓存/tool_executor/团队广播），由活跃窗口消费。
        """
        win = self.get_current_window()
        if win is not None and hasattr(win, "_on_working_dir_changed"):
            try:
                win._on_working_dir_changed(file_path)
            except Exception:
                logger.exception("[Workbench] 处理工作目录变更失败")

    def open_workbench_tab(self, page_id: str) -> bool:
        """展开工作台并定位到指定页签 id（插件工作台页命令的统一直达入口）

        与 ``open_workbench_history`` / ``open_workbench_memory`` 的差异：本方法
        不感知具体页语义（不触发任何懒构建），只做「展开 + 按 id 切页」，因此
        可被任意插件页命令（``register_workbench_tab`` 联动注册的 ``/{page_id}``）复用。

        Args:
            page_id: 工作台页签 id（如 "worktree-manager" / "history-manager" / 插件自定义 page_id）

        Returns:
            True 表示页签存在并已切换；False 表示面板不可用或当前无此页签
        """
        panel = getattr(self, "workbench_panel", None)
        if panel is None:
            return False
        # 展开工作台（不可见时 set_workbench_visible 内部会触发 refresh_workbench，
        # 其中含 sync_plugin_pages —— 保证插件页已 reconcile 后再按 id 定位）
        if not self.is_workbench_visible():
            self.set_workbench_visible(True)
        else:
            self.refresh_workbench()
        return panel.set_current_tab_by_id(page_id, user=True)

    def open_workbench_memory(self, sub_tab: str = "docs") -> None:
        """展开工作台并定位「工作树」页签（记忆功能已下线，保留兼容入口）

        统一的「一键直达」入口：新建项目后自动展开、工作树「管理工作树」
        按钮等场景都走这里。置顶记忆在助手中心管理，不再有记忆页。

        Args:
            sub_tab: 兼容旧签名；任何取值都落「工作树」页（关键文档+工作树）
        """
        panel = getattr(self, "workbench_panel", None)
        if panel is None:
            return
        # 1) 展开工作台（不可见时 set_workbench_visible 内部会触发 refresh_workbench）
        if not self.is_workbench_visible():
            self.set_workbench_visible(True)
        else:
            self.refresh_workbench()
        # 2) 定位「工作树」页
        panel.set_current_tab_by_id("worktree-manager", user=True)

    def open_workbench_history(self) -> None:
        """打开会话历史（已迁对话区左侧停靠区的常驻浮动卡）

        统一直达入口：底部工具栏历史按钮 / ``/history`` 命令都走这里。
        历史会话在 ``de49617c`` 从工作台页签迁为 ``plugins/history-manager``
        的 ``container="left"`` 浮动卡（``card_id="history-manager"``），工作台
        页签集合中已无 ``"history-manager"`` 页 —— 继续按 page_id 定位只会静默
        返回 False，表现为「历史会话点不开」，故改走浮动卡显示通道。

        卡片数据由 ``show_card()`` → ``HistoryPage.refresh()`` 自拉
        （``HistoryManager`` 单例），无需宿主补刷；再次调用为 toggle 语义。
        """
        win = self.get_current_window()
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            UIPluginRegistry.get_instance().toggle_floating_card("history-manager", main_widget=win)
        except Exception:
            logger.exception("[Workbench] 打开历史会话失败")

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
        """ThemeManager 统一刷新入口（dispatch_refresh 调用）"""
        self._on_theme_changed()

    def _reapply_splitter_handle_styles(self):
        """把 splitter handle 样式下沉到 QSplitterHandle 自身

        [PERF] 原始实现用 ``QSplitter.setStyleSheet("...::handle...")``，会让
        QStyleSheetStyle 对整个 splitter 子树重建 + repolish。本窗口三个
        splitter 子树规模为 431 / 1470 / 1462，实测单次分别 332 / 319 / 313ms，
        合计近 1 秒，而它们的样式规则只作用于 2px 的 handle 线条。

        改为逐 handle ``setStyleSheet`` + ``WA_StyledBackground``：
        作用域收窄到 handle 自身（无子控件），实测 0.3ms 总计。

        handle 数量随 addWidget 变化，故本方法幂等可重复调用；样式串按当前
        Colors 生成，主题切换时重新调用即可刷新颜色。
        """
        specs = (
            (
                getattr(self, "_splitter", None),
                # 隐藏的拖拽缝：透明底，与两侧 frame border 形成分隔线
                """
                QSplitterHandle {
                    background: transparent;
                }
                """,
            ),
            (
                getattr(self, "_dock_splitter", None),
                # 停靠区：6px 热区中画 2px 居中线，hover 变强调色提示可拖拽
                f"""
                QSplitterHandle {{
                    background: transparent;
                    border-left: 2px solid {Colors.BORDER};
                    margin: 10px 2px;
                    border-radius: 1px;
                }}
                QSplitterHandle:hover {{
                    border-left: 2px solid {Colors.BORDER_ACCENT};
                }}
                """,
            ),
            (
                getattr(self, "_sidebar_splitter", None),
                # 侧边栏内部分隔条（TabPanel ↔ left 卡片区）：4px 热区画 2px
                # 竖线，hover 变强调色提示可拖拽（卡片打开后此线可见、可拖动
                # 在两窗格间互借宽度）。不接线时 QSplitterHandle 默认透明，
                # 外观上「分割线消失」。
                # ★ 不留上下 margin（margin: 0）：卡片打开时卡片区与侧栏同高，
                #   分隔条应贯穿上下，与 #tabFrame 圆角内缘贴合；用 margin 收缩
                #   会在头尾留出空白段（用户反馈「没有完全连到上下」）。
                f"""
                QSplitterHandle {{
                    background: transparent;
                    border-left: 2px solid {Colors.BORDER};
                    margin: 0;
                    border-radius: 1px;
                }}
                QSplitterHandle:hover {{
                    border-left: 2px solid {Colors.BORDER_ACCENT};
                }}
                """,
            ),
            (
                getattr(self, "_chat_vsplitter", None),
                f"""
                QSplitterHandle {{
                    background: transparent;
                    border-top: 2px solid {Colors.BORDER};
                    margin: 2px 10px;
                    border-radius: 1px;
                }}
                QSplitterHandle:hover {{
                    border-top: 2px solid {Colors.BORDER_ACCENT};
                }}
                """,
            ),
        )
        for splitter, qss in specs:
            if splitter is None:
                continue
            try:
                for idx in range(splitter.count()):
                    handle = splitter.handle(idx)
                    if handle is None:
                        continue
                    # 不设该属性时 QSplitterHandle 的 paintEvent 不绘制 QSS 背景/边框
                    handle.setAttribute(Qt.WA_StyledBackground, True)
                    handle.setStyleSheet(qss)
            except RuntimeError:
                continue  # C++ 对象已销毁

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

        [PERF] 本串挂在窗口上，`setStyleSheet` 会 repolish 整窗 1500+ 子控件，
        实测 262~320ms。因此这里做**同串短路**：字号变化等场景会重复调用本方法
        而主题色未变，串相同时直接跳过（Qt 内部对同串同样全量 repolish，
        不会自身短路）。主题真变化时串必然不同，自然生效。
        另注：本串无法按 #objectName 拆到各子控件上——子控件子树重叠
        （chatFrame/chatManagerContent 各含 1400+，逐个设反而更慢，实测
        拆分合计 1245ms vs 整窗 320ms），且窗口自身设任意非空串都是全量 cost。

        [T30] 作用域拆分只拆**顶层三兄弟**（_tab_frame/_chat_frame/
        _workbench_frame）：它们互不重叠、各自子树规模可控；更深的子树
        重叠容器拆分实测反而更慢（1245ms 教训）。margin/radius 与 wbHidden
        margin 留在静态窗口串——它们与主题色无关且会被 wb 属性切换路径
        独立更新，拆走反而让该路径多一次容器串重刷。
        """
        # [T30] 窗口串缩为静态部分（几何/透明/圆角）；主题色值仅保留一处例外：
        # 窗口本体底色 #tabManagerWindow（无独立承载链，窗口串是唯一挂点），
        # 其余色值全部下沉到顶层三兄弟容器（_qss_tab_frame/_qss_chat_frame/
        # _qss_workbench_frame）的独立串上。字号变化/同主题场景串恒定 →
        # 同串短路直接跳过整窗 repolish（0ms）；真换主题时窗口串与三个可见
        # 容器各自重刷，隐藏者置脏由补刷点恢复。
        # 主题色值全部下沉到顶层三兄弟容器（_qss_tab_frame/_qss_chat_frame/
        # _qss_workbench_frame）的独立串上。字号变化/同主题场景串恒定 →
        # 同串短路直接跳过整窗 repolish（0ms）；真换主题时仅三个可见容器
        # 各自 repolish 自身子树，隐藏者置脏由补刷点恢复。
        # margin/radius 留在静态串的原因：wbHidden 属性切换只改 margin（
        # wb_hidden 路径独立成串重设窗口串即可，无需触碰色值容器串）。
        # 只拆**顶层三兄弟**的原因：子树重叠容器（chatFrame/chatManagerContent
        # 各含 1400+ 控件）逐个设反而更慢（拆分合计 1245ms vs 整窗 320ms，实测）。
        qss = f"""
            #tabManagerWindow {{
                /* [T30-P0fix] 窗口本体底色：无独立承载链，窗口串是唯一挂点。
                   f-string 动态色值——CONTENT_BG 变则串变，同串短路语义不受影响
                   （字号/同主题场景色值不变，仍走短路）。
                   ★ 顶层窗口不要设 border-radius：Qt 只会把"背景绘制"裁成圆角，
                   圆角外侧三角区不会被绘制，底层透出系统默认窗口色。窗口圆角
                   由 DWM 负责（_apply_win11_dwm_chrome）。 */
                background: {Colors.CONTENT_BG};
            }}
            #tabFrame {{
                /* 左侧圆角矩形容器，与右侧 #chatFrame 对称，提升呼吸感 */
                border-radius: 8px;
                margin: 4px 0 4px 4px;  /* 四边与窗口 4px 边距，右 0 让位 splitter handle */
            }}
            #chatFrame {{
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
            #tabManagerContent {{
                background: transparent;
                border-radius: 8px;
            }}
            #contentArea {{
                background: transparent;
            }}
            #contentStack {{
                background: transparent;
            }}
            #globalOverlay {{
                /* 覆盖层页面透明：面板底由内部 CardContainer 按卡片状态自绘
                   （transparentOverlay 卡片 → 容器透明，透出 #chatFrame 半透明
                   面板与对话区无缝衔接；普通卡片 → 容器自画 alpha=246 面板底）。 */
                background: transparent;
                border-radius: 8px;
            }}
        """
        if qss != self.styleSheet():
            self.setStyleSheet(qss)
        # [T30] 顶层三兄弟：可见者即时刷色值串并清脏；不可见者置脏跳过
        #（repolish 留到可见性恢复点 _refresh_theme_dirty_frames）
        # [T30-P0] getattr 防御：本方法在构造期（_setup_ui）即被调用，
        # 此时 frame 属性可能尚未创建——未创建属正常态，跳过即可，
        # 后续主题切换 / showEvent / 动画结束等补刷点会兜住。
        for frame, qss_method in (
            (getattr(self, "_tab_frame", None), self._qss_tab_frame),
            (getattr(self, "_chat_frame", None), self._qss_chat_frame),
            (getattr(self, "_workbench_frame", None), self._qss_workbench_frame),
        ):
            if frame is None:
                continue
            try:
                frame_qss = qss_method()
                if frame.isVisible():
                    if frame_qss != frame.styleSheet():
                        frame.setStyleSheet(frame_qss)
                    frame._theme_needs_refresh = False
                else:
                    frame._theme_needs_refresh = True
            except RuntimeError:
                continue  # C++ 对象已销毁
        # ── splitter handle 样式：下沉到 QSplitterHandle 自身（性能关键）──
        # [PERF] QSplitter.setStyleSheet 会让 QStyleSheetStyle 重建并 repolish
        # **整棵子树**（本窗口三个 splitter 分别挂 431/1470/1462 个子控件）。
        # 实测：设 splitter 322ms/个，下沉到 handle 自身 0.1ms/个；
        # 三者合计从 ~970ms 降到 ~0.3ms。整窗逐像素比对：
        # _dock_splitter / _chat_vsplitter 常态与 hover 均 0/768000 差异
        # （probe_h3_window_diff.py 结论）。
        #
        # 要点：
        # - QSplitterHandle 继承 QWidget 且不设 WA_StyledBackground，不设该属性
        #   时 QSS 背景/边框根本不画；
        # - 选择器必须保留 `QSplitterHandle` / `QSplitterHandle:hover`
        #   （裸属性写法与 `*:hover` 均无法触发 hover 态）；
        # - handle 由 splitter 在 addWidget / setOrientation 时按需创建，
        #   故需 _reapply_splitter_handle_styles 在布局变化后补刷。
        self._reapply_splitter_handle_styles()

    def _qss_tab_frame(self) -> str:
        """#tabPanel/#tabFrame 主题色值串（[T30] 静态串拆出的色值组）"""
        return f"""
            #tabPanel {{
                background: {Colors.CARD_BG.format(alpha=150)};
                border-radius: 8px;
            }}
            #tabFrame {{
                background: {Colors.CARD_BG.format(alpha=150)};
                border: 1px solid {Colors.BORDER};
            }}
        """

    def _qss_chat_frame(self) -> str:
        """#chatFrame 主题色值串"""
        return f"""
            #chatFrame {{
                background: {Colors.CARD_BG.format(alpha=150)};
                border: 1px solid {Colors.BORDER};
            }}
        """

    def _qss_workbench_frame(self) -> str:
        """#workbenchFrame 主题色值串（radius/margin 一并在此：静态串无此规则）"""
        return f"""
            #workbenchFrame {{
                background: {Colors.CARD_BG.format(alpha=150)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                margin: 4px 4px 4px 0;
            }}
        """

    def _refresh_theme_dirty_frames(self) -> None:
        """补刷隐藏期置脏的顶层三容器（可见性恢复点：动画结束/预览离开/showEvent）

        [T30] hasattr/RuntimeError 防御：容器可能已销毁（窗口关闭竞态）。
        [T30-fix] 不再以 _theme_needs_refresh 作补刷门控：样式可能被旁路清空
        （_wb_preview_leave 回挂时 setStyleSheet("") 不置脏），门控会漏刷 →
        面板裸奔无背景。改为可见者无条件「串不一致就刷」，比对开销可忽略；
        不可见者置脏留给 _apply_theme_stylesheet 快速跳过（setStyleSheet 全量
        repolish 贵，隐藏期白刷）。
        """
        # [T30-P0] getattr 防御：本方法在构造期（_setup_ui）即被调用，
        # 此时 frame 属性可能尚未创建——未创建属正常态，跳过即可，
        # 后续主题切换 / showEvent / 动画结束等补刷点会兜住。
        for frame, qss_method in (
            (getattr(self, "_tab_frame", None), self._qss_tab_frame),
            (getattr(self, "_chat_frame", None), self._qss_chat_frame),
            (getattr(self, "_workbench_frame", None), self._qss_workbench_frame),
        ):
            if frame is None:
                continue
            # 预览态：workbench frame 摘在浮层且带专用内联串（无 margin 版），
            # 不参与补刷，由 _wb_frame_inline_qss 负责（回挂后清串走正常补刷）
            if frame is getattr(self, "_workbench_frame", None) and getattr(self, "_wb_in_preview", False):
                continue
            try:
                if frame.isVisible():
                    frame_qss = qss_method()
                    if frame_qss != frame.styleSheet():
                        frame.setStyleSheet(frame_qss)
                    frame._theme_needs_refresh = False
                else:
                    frame._theme_needs_refresh = True
            except RuntimeError:
                continue  # C++ 对象已销毁

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
        # ★ 与 _tab_frame.setMaximumWidth 同源（此前硬编码 400，两处真源易漂移）
        self._tab_panel.setMaximumWidth(_TAB_PANEL_MAX_CONTENT_WIDTH)

        # ── 左侧 Tab 区域圆角矩形包裹框架（与右侧 #chatFrame 对称，提升呼吸感） ──
        # 在 splitter 里直接给 #tabPanel 设 border 会被 splitter handle 子控件
        # 绘制顺序吞掉，所以再包一层 QFrame 用 objectName 给样式。
        self._tab_frame = QFrame(content_widget)
        self._tab_frame.setObjectName("tabFrame")
        # QSplitter 控制的 child 是 _tab_frame 而非 _tab_panel，因此
        # _tab_frame 必须显式设最大/最小宽度，否则 splitter 拖拽会绕过
        # _tab_panel 的约束：
        #   frame min = 收起宽(46) + margins(12) + border(2) = 60
        #   frame max = panel max(400) + margins(12) + border(2) = 414
        self._tab_frame.setMinimumWidth(_COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X)
        self._tab_frame.setMaximumWidth(_TAB_PANEL_MAX_CONTENT_WIDTH + _FRAME_PADDING_X)
        tab_frame_layout = QVBoxLayout(self._tab_frame)
        # 与 #chatFrame 内边距完全对齐：让两侧容器视觉一致
        tab_frame_layout.setContentsMargins(6, 6, 6, 6)
        tab_frame_layout.setSpacing(0)

        # ── 侧边栏内部横向 splitter：TabPanel | left 卡片区 ──
        # left 卡片（container="left"）由「对话区左停靠区」迁到此处：嵌入
        # #tabFrame 内部右侧，视觉上像从侧边栏里长出来，与对话区彻底解耦。
        # 拖拽语义（产品确认）：
        # - 卡片打开：TabPanel 自动收起让位（宿主在 expandedChanged 里驱动），
        #   卡片区展开（默认 300 / 按插件 per-card 记忆）；
        # - 拖主 splitter（tabFrame ↔ chatFrame）：增量只加卡片区宽度，
        #   TabPanel 锁定收起宽不动（宿主 _on_splitter_manually_moved 改写）；
        # - 拖内部分隔条（TabPanel ↔ 卡片区）：两者互借宽度，frame 总宽不变、
        #   对话区不动，可把 tab 列表重新拉开；
        # - 卡片关闭：卡片区收 0 + hide，TabPanel 恢复打开前宽度，frame 缩回。
        from PySide6.QtWidgets import QSplitter as _SidebarSplitter

        self._sidebar_splitter = _SidebarSplitter(Qt.Horizontal, self._tab_frame)
        self._sidebar_splitter.setObjectName("sidebarSplitter")
        self._sidebar_splitter.addWidget(self._tab_panel)
        self._sidebar_splitter.setStretchFactor(0, 0)
        self._sidebar_splitter.setHandleWidth(4)
        self._sidebar_splitter.setChildrenCollapsible(False)
        tab_frame_layout.addWidget(self._sidebar_splitter)

        # 右侧内容区
        self._content_area = QStackedWidget(content_widget)
        self._content_area.setObjectName("contentArea")

        # 空状态页（索引 0）
        self._empty_state = EmptyStateWidget(content_widget)
        self._content_area.addWidget(self._empty_state)  # index 0

        # [L2] 多页 resize 编排：注册内容区 stack，使其接管 currentChanged ——
        # 页被激活时补跑该页此前被挂起的 resize 同步，消除切页空窗；
        # 后台页的恢复链则会被挂起，避免与前台页争抢主线程。
        from app.widgets.resize_orchestrator import ResizeOrchestrator

        ResizeOrchestrator.get_instance().register_stack(self._content_area)

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
        for _c in (
            self._global_top_container,
            self._global_bottom_container,
            self._global_left_container,
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

        # 标记 LEFT/BOTTOM 为共存容器：各向区域可同时存在、互不关闭。
        # 覆盖层（TOP）通过 QStackedWidget 仅替换对话区，不影响 LEFT/BOTTOM。
        _card_mgr.mark_coexist_containers(
            GLOBAL_WINDOW_ID,
            frozenset({ContainerType.LEFT, ContainerType.BOTTOM}),
        )

        # ── 停靠区双层 QSplitter：左/下占比均可拖拽调整 ──
        # 结构：chatVsplitter(纵向)
        #         ├─ dockSplitter(横向)：左停靠区 | 内容区(含覆盖层)
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
        from PySide6.QtWidgets import QSplitter as _DockSplitter

        # ── 覆盖层堆栈（QStackedWidget）：仅替换对话区，不覆盖 LEFT/RIGHT/BOTTOM ──
        # Page 0: 正常对话视图
        # Page 1: 系统卡片覆盖层（_global_top_container 内的全局卡片）
        self._content_stack = _ContentStack(self._chat_frame)
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
        #    wrapper 内部 content_stack 按当前限宽档位动态居中留白。 ──
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
        # wrapper 作为宿主 splitter 直接子项——dock mode 协议依赖 CardContainer
        # 为 splitter 直接子项，故经 wrapper 承载二者（_splitter_index 兼容之）。
        from app.widgets.cards.card_stack_container import CardStackContainer

        self._global_left_stack = CardStackContainer(self._tab_frame)
        self._global_left_stack.setObjectName("globalLeftStack")

        # 停靠区侧 wrapper：默认收起，子控件 visibility 联动 wrapper 与 splitter 大小
        # （避免无卡片时 splitter 均分空间显示空白 splitter handle）
        # ★ left 卡片宿主已从对话区 dock_splitter 迁到侧边栏内部 sidebarSplitter：
        #   卡片嵌在 #tabFrame 右侧、TabPanel 右侧，视觉上从侧边栏长出（产品确认形态）。
        #   伙伴窗格联动：卡片打开时 TabPanel 收到收起宽（_COLLAPSED_PANEL_WIDTH），
        #   卡片关闭时恢复打开前宽度。
        self._global_left_wrapper = _DockSideWrapper(
            self._global_left_container, self._global_left_stack, self._tab_frame
        )
        self._global_left_wrapper.setObjectName("globalLeftWrapper")
        self._global_left_stack.set_container_context(GLOBAL_WINDOW_ID, ContainerType.LEFT)
        self._global_left_container.set_stack_sibling(self._global_left_stack)

        self._sidebar_splitter.addWidget(self._global_left_wrapper)
        self._sidebar_splitter.setStretchFactor(1, 1)  # 卡片区吸收多余宽度
        self._sidebar_splitter.setStretchFactor(0, 0)  # TabPanel 不随宽度拉伸
        self._global_left_wrapper.set_partner_pane(self._tab_panel, collapsed_size=_COLLAPSED_PANEL_WIDTH)
        # wrapper 关联 splitter：联动 setSizes 维持收起态 + 伙伴窗格让位
        _sidebar_idx = self._sidebar_splitter.indexOf(self._global_left_wrapper)
        self._global_left_wrapper.attach_to_splitter(self._sidebar_splitter, _sidebar_idx)
        # 卡片显隐 → 宿主编排（tabFrame 加宽/缩回、对话区让位、TabPanel 收起态）
        self._global_left_wrapper.expandedChanged.connect(self._on_left_card_area_toggled)

        # dock_splitter：left 停靠区迁走后仅剩对话列一个窗格。
        # 保留该对象（外部按 objectName "dockSplitter" 探测 Tab 模式，见
        # main_widget.resizeEvent / test_card_container_priority），
        # 但不再承载左卡片区；handle 相应消失。
        self._dock_splitter.addWidget(self._chat_vsplitter)
        self._dock_splitter.setStretchFactor(0, 1)  # 对话列吃掉全部空间
        self._dock_splitter.setHandleWidth(6)
        # 折叠依赖轴向 max=0 约束而非用户拖拽收起，禁止拖拽塌陷
        self._dock_splitter.setChildrenCollapsible(False)
        # ── full 容器卡片标题栏 tab（替代原 ReplaceTabBar）──
        # full 卡片（UI 插件 full 卡 + 内置全局卡）打开时在标题栏 tab 区显示
        # （带 × 关闭钮，见 _on_card_visibility_changed）。这里仅订阅显隐事件。
        from app.core.infra.ui_event_bus import EV_CARD_VISIBILITY_CHANGED, UIEventBus

        # 订阅 full 卡片显隐事件，同步标题栏 tab open 集合与显隐
        UIEventBus.get_instance().subscribe(EV_CARD_VISIBILITY_CHANGED, self._on_card_visibility_changed)

        chat_frame_layout.addWidget(self._dock_splitter, 1)

        # 全局容器启用停靠模式
        self._global_left_container.enable_dock_mode(self._dock_splitter)
        # TOP 容器处于覆盖层模式，不启用 dock mode
        self._global_bottom_container.enable_dock_mode(self._chat_vsplitter)

        # ── 覆盖层状态切换 ──
        self._global_top_container.overlayStateChanged.connect(self._on_overlay_state_changed)

        # 使用 QSplitter 让左侧面板可拖拽
        from PySide6.QtWidgets import QSplitter

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
        # ★ 把手双击 = 折叠/展开侧边栏（与标题栏按钮同语义）
        #   QSplitter 自带 handle 是内部 QSplitterHandle，无法直接 connect，
        #   用事件过滤器在 handle 上捕获 MouseButtonDblClick（splitter 自己的
        #   mouseDoubleClickEvent 在 handle 命中时不会到达，故必须过滤 handle）。
        #   注册在 index 0 的 handle 上：那是左栏与聊天区之间的分隔条。
        _h = self._splitter.handle(1)
        if _h is not None:
            _h.installEventFilter(self)
            self._splitter_left_handle = _h
        content_layout.addWidget(self._splitter)
        content_widget.setLayout(content_layout)
        main_layout.addWidget(content_widget, 1)

        # 固定默认面板宽度（_DEFAULT_PANEL_WIDTH + 14px 补偿 #tabFrame margins/border），不记忆
        frame_w = _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X
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
        self._tab_panel.tabSwitchSessionRequested.connect(self._on_tab_switch_session_requested)
        self._tab_panel.newTabRequested.connect(self._on_new_tab_requested)
        self._tab_panel.newTempTabRequested.connect(lambda: self._on_new_tab_requested(temp_scope=True))
        self._tab_panel.sidebarToggled.connect(self._on_sidebar_toggled)
        self._tab_panel.teamCloseRequested.connect(self._on_team_close_requested)
        self._tab_panel.teamAddMemberRequested.connect(self._on_team_add_member_requested)
        self._tab_panel.teamNewTaskRequested.connect(self._on_team_new_task_requested)
        # 工作区树模式：在项目 + 工作树下新建对话页 / 打开历史会话
        self._tab_panel.newSessionInWorkspaceRequested.connect(self._on_new_session_in_workspace)
        self._tab_panel.openSessionRecordRequested.connect(self._on_open_session_record)
        # 工作区树「工作区」标题行 + → 新建项目（选文件夹流程）
        self._tab_panel.newProjectRequested.connect(self._on_new_project_requested)
        # Tab 增删后同步工作区树（会话归属/历史列表会变）
        self.tabCountChanged.connect(self._on_tab_count_changed_for_tree)
        # 用户手动拖拽 splitter 把手 → 折叠是用户主动，不标记挤压，
        # 关闭卡片/空间恢复时不得自动展开（尊重手动意图）
        if hasattr(self, "_splitter"):
            self._splitter.splitterMoved.connect(self._on_splitter_manually_moved)

        # macOS 软置顶：应用激活时补抬升（详见 _on_app_state_changed）
        if _IS_MAC:
            QApplication.instance().applicationStateChanged.connect(self._on_app_state_changed)

        # 对话区限宽档位变更（设置-外观）→ 立即重算 wrapper margins，
        # 无需等窗口 resize；同时通知已存在的消息卡按新宽度重新折行。
        try:
            Settings.get_instance().ui_chat_width.valueChanged.connect(self._on_chat_width_changed)
        except Exception:
            pass

    def _on_chat_width_changed(self, *_args):
        """限宽档位变化 → 重算对话区/覆盖层留白 + 消息卡宽度"""
        self._sync_chat_wrapper_width()
        # 强制所有窗口的消息卡按新容器宽度重算（full 档铺满 / 窄档收紧）。
        # 走既有的分帧宽度同步链，30 张卡不会冻结主线程。
        for win in self._windows:
            try:
                win._sync_all_cards_width()
            except Exception:
                pass

    # ── 侧边栏内 left 卡片区：宿主编排（frame 加宽 / TabPanel 折叠态 / 对话区让位） ──

    def _left_card_area_active(self) -> bool:
        """left 卡片区当前是否已展开（wrapper 有可见意图）"""
        wrapper = getattr(self, "_global_left_wrapper", None)
        if wrapper is None:
            return False
        return _DockSideWrapper._child_visible_intent(wrapper._primary) or _DockSideWrapper._child_visible_intent(
            wrapper._stack
        )

    def _left_card_frame_extra(self) -> int:
        """卡片区占用的 frame 额外宽度（0 = 未展开）

        取值口径与 wrapper 槽位一致（splitter sizes），无槽位时回落到记忆/默认宽。
        """
        wrapper = getattr(self, "_global_left_wrapper", None)
        sp = getattr(self, "_sidebar_splitter", None)
        if wrapper is None or sp is None:
            return 0
        try:
            sizes = sp.sizes()
            idx = sp.indexOf(wrapper)
            if 0 <= idx < len(sizes) and sizes[idx] > 0:
                return sizes[idx]
        except Exception:
            pass
        return getattr(wrapper, "_expanded_width", 0) or 0

    def _on_left_card_area_toggled(self, expanded: bool) -> None:
        """left 卡片区展开/收起 → 编排 #tabFrame 宽度 + TabPanel 折叠态

        产品语义（已确认）：
        - 卡片打开：TabPanel 让位收窄到收起宽（46px 图标条），卡片区展开，
          #tabFrame 整体加宽（TabPanel + 卡片），对话区被挤窄；
        - 卡片关闭：卡片区收 0，TabPanel 恢复打开前宽度，#tabFrame 缩回，
          对话区回收空间。

        关键时序：本回调由 _DockSideWrapper 在**改内部分配之前**同步调用，
        此处先把 TabPanel 的 _collapsed/_collapsed_by_squeeze 置好（静默，
        不发信号），随后伙伴窗格收窄时 TabPanel.resizeEvent 不会再 emit
        sidebarToggled → 阻断「收窄 → 宿主把 frame 压到 60px → 卡片被挤没」回路。
        """
        if not hasattr(self, "_tab_frame") or not hasattr(self, "_splitter"):
            return
        panel = getattr(self, "_tab_panel", None)
        if panel is None:
            return
        sizes = self._splitter.sizes()
        if not sizes:
            return
        tab_w = sizes[0]

        if expanded:
            # 1) 记忆 TabPanel 当前宽度（关闭时恢复），并按收起态静默落位。
            #    ★ 同时记忆打开前的折叠态：卡片打开前侧边栏本就是折叠的（用户
            #      此前手动收起），关闭卡片后必须回到折叠态，不能无条件展开
            #      （否则「折叠态 → 开插件 → 关插件」会意外变成展开态）。
            self._left_card_prev_collapsed = bool(panel._collapsed)
            if not panel._collapsed:
                self._left_card_saved_panel_frame_w = max(
                    tab_w, max(_EXPANDED_MIN_FRAME_WIDTH, _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X)
                )
            panel._collapsed = True
            panel._collapsed_by_squeeze = False  # 卡片让位属程序行为，非挤压
            panel._left_card_present = True  # 占位期禁用面板宽度自治（防坍缩）
            panel.sync_collapsed_ui()
            # 2) frame 目标宽 = TabPanel 收起宽 + 卡片区当前槽位（首帧 0 时用记忆/默认）
            #    ★ 槽位此刻是旧 frame 总宽下的钳制值（内层尚未加宽），不能直接
            #      采信：容器无记忆首开 target=_DOCK_DEFAULT_W(360)，若按钳后
            #      槽位(~300 兜底)编排 frame，容器意图永远落不了位（实测 360
            #      被钳到 292，表现为"默认宽度没变"）。取容器意图 _last_expand_target
            #      （与 wrapper._on_expand 落位同源）参与 max。
            card_w = self._left_card_frame_extra()
            if card_w <= 0:
                card_w = _DockSideWrapper.DEFAULT_EXPANDED_WIDTH
            _cont = getattr(self._global_left_wrapper, "_primary", None)
            _intent_w = int(getattr(_cont, "_last_expand_target", 0) or 0)
            card_w = max(card_w, _intent_w)
            # ★ 空间不足让位（规则 2 同源）：窗口放不下「TabPanel 收起宽 +
            #   卡片宽 + 聊天区最小宽」时，先瞬切收起工作台腾空间（打
            #   _wb_collapsed_by_squeeze 标记，卡片关闭空间回收后自动重开）。
            #   否则 _left_card_frame_target 把 frame 钳到收起宽附近，卡片区
            #   被挤成竖条（内容不可读）。
            try:
                _sizes_now = self._splitter.sizes()
                _total_now = sum(_sizes_now)
                _wb_now = _sizes_now[2] if len(_sizes_now) >= 3 else 0
                if _wb_now > 0 and _total_now - _wb_now - _MIN_CHAT_WIDTH < (
                    _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X + card_w
                ):
                    self._collapse_workbench_by_squeeze()
            except Exception:
                pass
            target = self._left_card_frame_target(card_w)
            # ★ 槽位→frame 换算补偿（+8）：_FRAME_PADDING_X(14) 只覆盖
            #   layout margins(12) + border(2)；卡片态下内层还有 QSS
            #   margin-left(4) 与 splitter handle(4) 未计——不补偿时卡片槽
            #   每次重开被压缩 8px（实测连续切页卡片槽 360→352→344 逐轮
            #   递减，表现为「切页往返后卡片宽度越来越窄」）。
            target += _CARD_SLOT_CHROME_EXTRA
            # 3) 放宽 frame max（允许加宽）后落位
            self._tab_frame.setMaximumWidth(max(_tab_frame_max_expanded(), target))
            if abs(tab_w - target) > 1:
                self._splitter.setSizes(self._splitter_sizes_with_left(target))
        else:
            # 1) TabPanel 恢复打开前的状态：折叠前是收起态就回收起态，
            #    展开态才恢复打开前的宽度（否则会意外把用户收起的侧边栏撑开）。
            #    ★ 例外：占位期用户拖内部分隔条把面板拉开了（宽度越过展开线、
            #      _collapsed 已被形态自治置 False），这是明确意图——此时不得
            #      再按打开前的折叠态收回去，改为按用户拖出的实际宽度落位。
            prev_collapsed = bool(getattr(self, "_left_card_prev_collapsed", False))
            self._left_card_prev_collapsed = False
            panel._left_card_present = False  # 卡片已退场：恢复面板宽度自治
            user_widened = not panel._collapsed and prev_collapsed
            panel._collapsed = prev_collapsed and not user_widened
            panel._collapsed_by_squeeze = False
            panel.sync_collapsed_ui()
            if panel._collapsed:
                target = panel._collapsed_min_width + _FRAME_PADDING_X
            elif user_widened:
                # 用户拖出的宽度即目标（下限保证不骑折叠线）
                _min_expand_w = max(_EXPANDED_MIN_FRAME_WIDTH, _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X)
                cur_panel_w = 0
                sp = getattr(self, "_sidebar_splitter", None)
                if sp is not None:
                    sizes_in = sp.sizes()
                    tab_idx = sp.indexOf(panel)
                    if 0 <= tab_idx < len(sizes_in):
                        cur_panel_w = sizes_in[tab_idx]
                target = max(_min_expand_w, cur_panel_w + _FRAME_PADDING_X)
                self._saved_panel_frame_width = target
            else:
                target = getattr(self, "_left_card_saved_panel_frame_w", 0) or max(
                    _EXPANDED_MIN_FRAME_WIDTH, _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X
                )
                # ★ 切页投影触发的卡片关闭：当前活跃页有独立宽度记忆
                #   （per-tab）时以它为准——_left_card_saved_panel_frame_w 是
                #   卡片打开那页的无卡宽，拿它落位别的页会把该页宽度打回
                #   打开页基准（实测：A 开卡（201）→ 切到记忆 500 的 B 页，
                #   卡片投影关闭用 201 覆盖 B 的 500）。
                try:
                    _cur_win = self.get_current_window()
                    _win_mem = getattr(_cur_win, "_tab_sidebar_frame_w", None) if _cur_win is not None else None
                    if _win_mem and int(_win_mem) > _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X:
                        target = max(_EXPANDED_MIN_FRAME_WIDTH, int(_win_mem))
                except Exception:
                    pass
            # 2) 收回 frame max（回到无卡片时的静态上限）后落位
            self._tab_frame.setMaximumWidth(_TAB_PANEL_MAX_CONTENT_WIDTH + _FRAME_PADDING_X)
            if abs(tab_w - target) > 1:
                self._splitter.setSizes(self._splitter_sizes_with_left(target))
            # 卡片关闭空间回收：卡片打开时挤压协调收的工作台，放得下则重开
            # （规则 2 反向；growth_required=False：布局性恢复，窗口总宽未变）
            if getattr(self, "_wb_collapsed_by_squeeze", False):
                self._maybe_restore_workbench_after_squeeze(growth_required=False)
        # 卡片区宽度变化会改 frame 上限，同步一次（拖拽增量全部给卡片）
        self._sync_tab_frame_cap()

    def _left_card_frame_target(self, card_w: int) -> int:
        """卡片打开时的 frame 目标宽：TabPanel 收起宽 + 卡片宽（含 padding）

        上限取「可见区能容纳」的最大值：窗口总宽 - 工作台宽 - 聊天区最小宽。
        ★ 工作台让位后仍放不下理想宽时，保卡片最小可用宽
        （_LEFT_CARD_MIN_VISIBLE_WIDTH，否则卡片区被钳到接近 0、内容挤成
        竖条不可读），聊天区允许暂时低于理想宽但不低于绝对下限。
        """
        base = _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X
        target = base + max(0, card_w)
        try:
            sizes = self._splitter.sizes()
            wb_w = sizes[2] if len(sizes) >= 3 else 0
            total = sum(sizes) if sizes else self.width()
            free = total - wb_w
            cap = max(base + _LEFT_CARD_MIN_VISIBLE_WIDTH, free - _MIN_CHAT_WIDTH)
            if free - _CHAT_ABSOLUTE_MIN_WIDTH < cap:
                cap = max(base, free - _CHAT_ABSOLUTE_MIN_WIDTH)
            target = min(target, cap)
        except Exception:
            pass
        return target

    def _sync_tab_frame_cap(self) -> None:
        """同步 #tabFrame 的 maximumWidth：卡片打开时放宽，关闭时回到静态上限

        拖主 splitter 时卡片区可被拖宽，frame 上限须同步放开，否则被钳住。
        上限 = TabPanel 最大内容宽 + 卡片当前槽位 + padding。
        """
        frame = getattr(self, "_tab_frame", None)
        if frame is None:
            return
        if not self._left_card_area_active():
            frame.setMaximumWidth(_TAB_PANEL_MAX_CONTENT_WIDTH + _FRAME_PADDING_X)
            return
        card_w = self._left_card_frame_extra() or _DockSideWrapper.DEFAULT_EXPANDED_WIDTH
        frame.setMaximumWidth(_tab_frame_max_expanded(card_w))

    # ── 侧边栏收起/展开（宽度平滑动画） ──

    def _on_sidebar_toggled(self, collapsed: bool, animate: bool = True):
        """侧边栏收起/展开：通过 splitter 平滑动画调整面板宽度

        Args:
            collapsed: True=收起 False=展开
            animate: 是否走宽度动画（启动恢复配置时传 False 瞬时切换）
        """
        # ── 卡片占位期：按钮语义 = 先收卡片，面板落到按钮请求的态 ──
        # 占位期 panel._collapsed 是编排状态（True=面板让位给卡片），与按钮
        # 语义（用户对面板形态的意图）混用同一状态位：点击翻转后发出的请求
        # 与用户意图相反，且照常走宽度路径只会把 frame 压小，卡片区因未
        # 隐藏被挤成细条残留（实测：折叠后卡片仍显示）。
        # 收卡片必须逐卡走 registry hide（卡片的 visible 状态在容器内部，
        # 直接 hide 容器会被 _do_expand 的 has_visible 判定 show 回来）；
        # 卡片全部隐藏后容器状态机自行折叠 → wrapper._sync 收卡片槽 →
        # expandedChanged(False) → _on_left_card_area_toggled(False) 按
        # _left_card_prev_collapsed 把面板直接落位到请求态。
        if self._left_card_area_active():
            # 占位期面板已是 46px 窄条（编排收窄），按钮图标不分形态，
            # 用户此时点击的唯一合理意图 = 收起一切：卡片隐藏 + 面板折叠。
            # ★ _toggle_sidebar 在 emit 前已把 _collapsed 翻转（编排 True→False）
            #   并落盘 False，不矫正的话卡片关闭回调的 user_widened 会误判为
            #   True（"用户拖开了面板"），把面板展开落位而非折叠。
            panel = getattr(self, "_tab_panel", None)
            if panel is not None:
                panel._collapsed = True
                panel._collapsed_by_squeeze = False
                from app.utils.config import Settings

                Settings.get_instance().ui_sidebar_collapsed.value = True
                panel._update_toggle_button(switch_ui=False)
            self._left_card_prev_collapsed = True
            try:
                from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

                reg = UIPluginRegistry.get_instance()
                for cid, info in list(reg.get_floating_cards().items()):
                    if getattr(info, "container", None) == "left":
                        try:
                            reg.hide_floating_card_globally(cid)
                        except Exception:
                            pass
            except Exception:
                pass
            return
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
                # 挤压折叠场景：resizeEvent 自动折叠时宽度已被压到折叠线
                # （panel._auto_collapse_width 是 **content 宽** → 换算到 frame 域）
                # 以下，此刻保存的"当前宽度"只剩窄条，点击展开只能恢复窄条。
                # 此时改用常规展开宽度（固定默认 _DEFAULT_PANEL_WIDTH）作为恢复
                # 目标，保证展开后可读；用户手动折叠时宽度正常，照常保存实际宽度。
                # ★ saved 单调不减：窄条折叠不得降级用户此前拖出的更大意图宽
                #   （实测 414 被压窄后折叠 → saved 被打成 201 → 切页恢复全失效）。
                #   prev_saved 已是 frame 域，保留时不得重复 +_FRAME_PADDING_X。
                if cur_frame_w < self._tab_panel._auto_collapse_width + _FRAME_PADDING_X:
                    _prev_saved = int(getattr(self, "_saved_panel_frame_width", 0) or 0)
                    if _prev_saved > cur_frame_w:
                        self._saved_panel_frame_width = _prev_saved
                    else:
                        self._saved_panel_frame_width = max(
                            _DEFAULT_PANEL_WIDTH, _EXPANDED_MIN_CONTENT_WIDTH
                        ) + _FRAME_PADDING_X
                else:
                    self._saved_panel_frame_width = cur_frame_w
                # 手动折叠（非挤压）：清当前活跃窗口的 per-tab 宽度记忆——
                # 用户显式收起是意图，切页不得按旧记忆展开回拖拽宽度
                if not self._tab_panel._collapsed_by_squeeze:
                    _cur_win = self.get_current_window()
                    if _cur_win is not None:
                        _cur_win._tab_sidebar_frame_w = None
                logger.debug(
                    f"[TSW] 折叠存意图 cur_frame_w={sizes[0]} saved->{self._saved_panel_frame_width}"
                )
            # 使用 _tab_panel 的收起最小宽度
            target_w = self._tab_panel._collapsed_min_width + _FRAME_PADDING_X
        else:
            # 展开：恢复折叠时保存的目标宽度（frame 宽度）。
            # ★★ 下限不能用 _EXPANDED_MIN_FRAME_WIDTH —— 它等于折叠线
            #   （150 content / 164 frame），展开后宽度恰好骑在折叠线上，
            #   动画结束瞬间 resizeEvent 判定 width() < 折叠线 即再次折叠
            #   → "点了展开又弹回收起"（实测：双击把手展开直接失败）。
            #   下限必须是**明显高于展开线**的可读宽度：取 _DEFAULT_PANEL_WIDTH
            #   （187 content，距折叠线 37px、距展开线 31px），保证展开后
            #   稳稳落在展开区内侧，且与"固定默认宽度"语义一致。
            _min_expand_w = max(_EXPANDED_MIN_FRAME_WIDTH, _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X)
            target_w = max(_min_expand_w, getattr(self, "_saved_panel_frame_width", _min_expand_w))
            # 拖拽把手拉开的场景（当前宽度已远超收起最小宽度，说明用户
            # 手动拖到位）：保持用户拖出的宽度，不覆盖为保存值。
            # 按钮点击展开时 cur_w == 收起宽度(60)，不满足此条件。
            if cur_w > self._tab_panel._collapsed_min_width + _FRAME_PADDING_X + _HYSTERESIS_WIDTH:
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
        """启动侧边栏宽度动画（时长/曲线走全局 token，按方向分离）"""
        # ── #31 中间对话区原样实时显示：不再冻结 _content_area 重绘 ──
        # 改为抑制卡片进入 resize 预览模式（WebView 保持可见），动画期
        # 每帧 restart 的宽度同步防抖在动画结束后一次执行。
        self._set_windows_resize_preview_suppressed(True)
        anim = self._sidebar_anim
        if anim is None:
            anim = QVariantAnimation(self)
            anim.valueChanged.connect(self._on_sidebar_anim_value)
            anim.finished.connect(self._on_sidebar_anim_finished)
            self._sidebar_anim = anim
        # 动画期间抑制 TabPanel resizeEvent 自动展开（折叠途中宽度仍 > 阈值，
        # 若不抑制会在动画中途误触发"拖拽展开"打断动画）
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_animating(True)
        self._sidebar_anim_collapsing = collapsing
        # ── 收起方向：整段动画不再中途切紧凑 ──
        # 旧实现在宽度掉到"收起宽+滞回"时就把整行切紧凑：外框放大 + margins
        # 归零 + 居中对齐一次生效，icon 中心从展开位(约 28)跳到紧凑位(23)，
        # 而宽度还在继续变小 → 视觉上 icon 先往右跑、再跟着宽度往左回。
        # 现在 `_sidebar_anim_ui_switched` 收起侧恒为 False（跨阈值不切），
        # 形态切换统一放到动画真正结束后（_on_sidebar_anim_finished）执行：
        # 此时宽度已落定，切换只剩一次、且不带位移方向反转。
        self._sidebar_anim_ui_switched = True if collapsing else False
        anim.stop()
        # ★ 系统「减少动态效果」：直接落位并走同一条收尾路径 —— 收拾动画
        # 结束标志 / 解除 TabPanel 抑制 / 恢复 resize 预览都靠它。
        if not Animations.motion_enabled():
            self._on_sidebar_anim_value(float(end_w))
            self._on_sidebar_anim_finished()
            return
        anim.setDuration(Animations.EXIT_MS if collapsing else Animations.ENTER_MS)
        anim.setEasingCurve(QEasingCurve(Animations.EASE_EXIT if collapsing else Animations.EASE_ENTER))
        anim.setStartValue(float(start_w))
        anim.setEndValue(float(end_w))
        anim.start()

    def _on_sidebar_anim_value(self, value):
        """动画每帧：更新 splitter 宽度；跨阈值时切换 TabPanel 紧凑/展开 UI"""
        w = int(round(value))
        # ★ 必须三值：缺工作台窗格会被 QSplitter 压到 0/最小宽度（见 helper 注释）
        self._splitter.setSizes(self._splitter_sizes_with_left(w))

        # 跨阈值切换 UI：展开途中宽度升到展开线（= 折叠线 + 滞回区）时切完整。
        # ★ 坐标系：本方法 w 是 **frame 宽**（直接喂给 setSizes），而
        #   panel._auto_expand_width 是 **content 宽** → 展开侧必须加 padding
        #   换算。展开侧与 resizeEvent 的展开线同源，避免"动画已切完整但
        #   resizeEvent 判定仍算折叠"的双标。
        #
        # ★★ 收起方向不再有跨阈值切换（_collapse_ui_w 已废弃保留注释说明）：
        #   动画途中切紧凑会让 icon 中心从展开位(约 28)跳到紧凑位(23)，而宽度
        #   仍在变小 → icon 先随切形态右移、再随宽度左移，出现"先右后左"的
        #   折返。收起形态一律等动画结束（_on_sidebar_anim_finished）执行一次。
        #   （曾把收起阈值写死 100：UI 在动画过大半时才切，收尾段闪一帧完整
        #   布局；直接前移到动画中段同样错，方向反了。）
        _expand_ui_w = self._tab_panel._auto_expand_width + _FRAME_PADDING_X
        if not self._sidebar_anim_ui_switched:
            # 仅展开方向保留跨阈值提前切（文字早于宽度就位，避免窄条挤字）；
            # 收起方向已置 switched=True，整段动画维持展开形态，切换留给收尾。
            if not self._sidebar_anim_collapsing and w >= _expand_ui_w:
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
                target_w = self._tab_panel._collapsed_min_width + _FRAME_PADDING_X
            else:
                # ★ 同 _on_sidebar_toggled：展开下限必须明显高于折叠线，
                #   否则落位宽度骑在折叠线上会被 resizeEvent 再次判折叠。
                _min_expand_w = max(_EXPANDED_MIN_FRAME_WIDTH, _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X)
                target_w = getattr(self, "_saved_panel_frame_width", _min_expand_w)
                target_w = max(_min_expand_w, target_w)
            self._splitter.setSizes(self._splitter_sizes_with_left(target_w))
            # UI 最终状态兜底（动画中途未跨阈值时强制同步，如目标宽度恰为阈值）
            if hasattr(self, "_tab_panel"):
                self._tab_panel.sync_collapsed_ui()
            # 折叠动画结束：布局已稳定，若折叠是"挤压所致"（_collapsed_by_squeeze），
            # 补一次空间恢复检测——覆盖"折叠动画期间窗口已拉宽"的时序缺口
            # （resize 结束检测在动画中会被 _animating 跳过）。
            if self._sidebar_anim_collapsing:
                self._maybe_auto_expand_after_squeeze()
            else:
                # ★ 展开动画结束：恢复内容区重绘（finally）会触发重排，chat 区
                #   sizeHint 恢复 → QSplitter 重排可能把左栏压穿折叠线，此时
                #   _animating 已复位，resizeEvent 误判"用户拖窄"而自动折叠
                #   （实测：拖宽 414 落定 1s 内被弹回图标条）。延迟两拍复检：
                #   压窄未折 → _evaluate_squeeze_collapse 恢复宽度；
                #   已挤压折叠 → _maybe_auto_expand 按意图宽展开（无窗口增长要求）。
                QTimer.singleShot(150, self._post_expand_squeeze_check)
                QTimer.singleShot(400, self._post_expand_squeeze_check)
        finally:
            # ── #31 动画结束恢复卡片 resize 预览能力 ──
            # 若本回调又同步启动了新动画（_maybe_auto_expand 走 _start_sidebar_anim
            # 重新抑制），则交由其 _on_sidebar_anim_finished 恢复，此处跳过。
            if not (self._sidebar_anim is not None and self._sidebar_anim.state() == QVariantAnimation.Running):
                self._set_windows_resize_preview_suppressed(False)

    def _post_expand_squeeze_check(self):
        """展开动画收尾后的重排挤压复检（见 _on_sidebar_anim_finished 展开分支）"""
        panel = getattr(self, "_tab_panel", None)
        if panel is None or getattr(panel, "_left_card_present", False) or panel._dragging_splitter:
            return
        if self._evaluate_squeeze_collapse():
            return  # 真挤压：已走折叠动画
        # 未折叠压窄（上一调用已恢复）或已挤压折叠：按意图宽展开（无增长要求）
        self._maybe_auto_expand_after_squeeze(growth_required=False)

    def _evaluate_squeeze_collapse(self) -> bool:
        """按稳定后的几何判定"侧边栏确实被挤压"→ 自动折叠（含双面板协调）

        背景：resize 周期内（尤其 _deferred_resize_complete 的 _force_relayout
        全量重算）左面板宽度会瞬时跌到折叠阈值以下。TabPanel.resizeEvent
        只看宽度，会把最大化/还原、覆盖层 relayout 这类几何瞬变误判为"用户
        把面板拖窄"而折叠；而折叠后窗口总宽往往不增反减，自动展开的相对增长
        条件（≥ 折叠时总宽 + _AUTO_EXPAND_GROWTH）永不满足 → 折叠态永久残留。
        因此 resize 周期内置 _auto_collapse_suppressed 抑制该判定，改在这里
        基于收拢后的真实几何决定。

        判定标准（"被挤压"）：左面板最终宽度低于折叠阈值，且窗口总宽已放不下
        「常规展开宽度 + 聊天区最小可用宽度」。空间其实够的（纯 relayout 瞬时
        压窄）保持展开，不折叠。

        规则 2（右先折）：窗口放不下「左栏展开 + 聊天区最小宽 + 工作台」时，
        先瞬切收起工作台让位；释放后放得下「左栏展开 + 聊天区最小宽」则保持
        左栏展开；仍放不下才折叠左栏。协调折叠的工作台打 _wb_collapsed_by_squeeze
        标记，空间恢复时自动重开（手动关闭永不重开）。

        Returns:
            True 表示本次触发了折叠（调用方应跳过随后的自动展开检测，
            否则刚折叠就可能被 _maybe_auto_expand_after_squeeze 弹回）。
        """
        panel = getattr(self, "_tab_panel", None)
        if panel is None or panel._collapsed or panel._animating:
            return False
        # ★ 卡片占位期：面板与卡片区共享 sidebarSplitter，宽度语义由卡片编排
        #   独占（拖内把手 = 两窗格互借，不代表窗口挤压）。此时若照常按宽度
        #   判定，会把用户拖窄的 tab 判成"被挤压"再弹回/折叠，破坏内拖手感。
        #   拖拽会话中同理（松手由 _on_splitter_idle 统一落定）。
        if getattr(panel, "_left_card_present", False) or getattr(panel, "_dragging_splitter", False):
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
        # ★ 坐标系：sizes[0] 是 **frame 宽**，panel._auto_collapse_width 是
        #   **content 宽**（= TabPanel.width()）→ 比较前必须先换算，否则折叠线
        #   被隐性抬高 14px。统一折算到 frame 域比较（本方法其余判定都在 frame 域）。
        collapse_frame_w = panel._auto_collapse_width + _FRAME_PADDING_X
        if total <= 0 or left >= collapse_frame_w:
            return False  # 宽度正常：没有挤压，无需折叠
        # 空间仍放得下"常规展开宽度 + 聊天区最小宽度" → 只是瞬时压窄，不折叠：
        # 把左面板恢复到常规展开宽度，避免停留在被压扁的窄条上。
        needed = max(_EXPANDED_MIN_FRAME_WIDTH, getattr(self, "_saved_panel_frame_width", _EXPANDED_MIN_FRAME_WIDTH))
        wb_w = sizes[2] if len(sizes) >= 3 else 0
        if total >= needed + _MIN_CHAT_WIDTH + wb_w:
            tab_frame = getattr(self, "_tab_frame", None)
            cap = tab_frame.maximumWidth() if tab_frame is not None else needed
            frame_w = max(0, min(needed, cap))
            if frame_w > 0 and frame_w != left:
                self._splitter.setSizes(self._splitter_sizes_with_left(frame_w))
                panel.sync_collapsed_ui()
            return False
        # 放不下全布局（含工作台）：先折工作台让位（规则 2，右先折）
        _wb_frame = getattr(self, "_workbench_frame", None)
        _wb_visible = _wb_frame is not None and _wb_frame.isVisible() and not _wb_frame.isHidden()
        wb_freed = 0
        if _wb_visible:
            self._collapse_workbench_by_squeeze()
            # ★ 工作台让位不一定把宽度让给中间区：不同实现下它极可能被
            #   游离窗格（尚未渲染的 39px）或相邻窗格吃掉，而 Qt 会把空出的
            #   宽度**补到最大窗格**（实测分配 [L=60, chat=304, WB=250]，
            #   WB 隐藏后 → [60, 554, 0]，左栏纹丝不动）。因此这里必须
            #   主动把"让位后可用总宽"重新喂给 QSplitter，否则紧随其后的
            #   重判会基于旧几何，把本可容纳左栏的情形误判为"确实被挤压"。
            if len(sizes) >= 3:
                wb_freed = sizes[2]
            elif getattr(self, "_saved_wb_frame_width", 0):
                wb_freed = int(self._saved_wb_frame_width)
        # 工作台让位后重判：放得下「左栏展开 + 聊天最小宽」→ 保持左栏展开
        if total + wb_freed >= needed + _MIN_CHAT_WIDTH:
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

    def _collapse_workbench_by_squeeze(self) -> None:
        """挤压协调第一步：瞬切收起工作台并打标记（animate=False 避开动画互打断）"""
        self._wb_collapsed_by_squeeze = True
        self.set_workbench_visible(False, animate=False)

    @staticmethod
    def _tab_switch_win_memory_baseline(prev_left_w: int, prev_collapsed: bool, win_mem) -> tuple | None:
        """per-tab 宽度记忆基准（全局折叠态语义）

        返回 (left_w, collapsed, snapshot) 或 None（无有效记忆，走 saved 基准）。
        展开态：应用记忆宽（每页记住自己拖出的宽度）；
        折叠态：保持折叠与折叠宽——记忆宽留给下次展开，切页不静默展开。
        """
        if win_mem and int(win_mem) > _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X:
            if prev_collapsed:
                return int(prev_left_w), True, ("win", int(win_mem))
            return int(win_mem), False, ("win", int(win_mem))
        return None

    def _tab_switch_baseline_width(self, cur_left_w: int, collapsed: bool) -> int:
        """切页保宽的恢复基准宽

        折叠态：取即时宽（折叠宽即意图）；展开态：取用户意图宽
        （_saved_panel_frame_width）——即时宽度可能已被其他标签页的工作台
        挤压污染，切回后空间释放，意图宽可达。saved 缺省（从未拖拽）时
        退化为即时宽。
        """
        if collapsed:
            return cur_left_w
        return max(cur_left_w, int(getattr(self, "_saved_panel_frame_width", 0) or 0))

    def _restore_tab_frame_after_tab_switch(self, prev_left_w: int, prev_collapsed: bool) -> None:
        """切页保宽恢复：切页后把左栏宽度/折叠态还原到切页前

        背景：工作台显隐按 per-tab 记忆在 _on_tab_selected 瞬切恢复，QSplitter
        重分配空间时窗口放不下会压窄左栏；TabPanel.resizeEvent 同步折叠
        （_collapsed_by_squeeze 置位）后，切回释放的空间全部归对话区，左栏
        宽度被永久污染——挤压自动展开要求窗口总宽增长 ≥_AUTO_EXPAND_GROWTH，
        布局性释放空间不触发。此处以「切页前后左栏不变」为原则还原。

        ★ 一致性守卫：prev 是切页瞬间的快照，若执行时（尤其动画 250ms 重试）
          saved 已被中间发生的新拖拽/挤压恢复/卡片编排改写，说明切页快照
          已过时，直接放弃（新意图优先，实测覆盖用户刚拖出的宽度）。
        """
        panel = getattr(self, "_tab_panel", None)
        if panel is None or prev_left_w <= 0:
            return
        # 解除切页期折叠抑制（_on_tab_selected 切页前置位，防瞬切挤压被
        # 误判为用户拖窄；先解除，后续分支不得提前 return 跳过）
        try:
            panel.set_auto_collapse_suppressed(False)
        except Exception:
            pass
        _snapshot = getattr(self, "_tab_switch_saved_snapshot", None)
        if _snapshot is not None:
            _kind, _val = _snapshot
            if _kind == "win":
                _win = self.get_current_window()
                _cur = getattr(_win, "_tab_sidebar_frame_w", None) if _win is not None else None
            else:
                _cur = getattr(self, "_saved_panel_frame_width", None)
            if _cur is not None and (_val is None or int(_cur) != int(_val)):
                logger.debug(
                    f"[TSW] restore 放弃 prev={prev_left_w} 基准已变 {_val}->{_cur} ({_kind})"
                )
                return  # 切页后有新的宽度编排（含 saved 从无到有）：快照过时，不覆盖
        # ★ 卡片态守卫：left 卡片显隐是 per-tab 的，切页投影会让卡片态翻转
        #   （如「A 开卡 → B 无卡 → 切回 A」）。目标页卡片活跃时宽度语义由
        #   卡片编排独占（重开卡片时编排自己会把 frame 落位到卡片态宽），
        #   restore 携的 prev 是旧页语义（含/不含卡片槽），覆盖即把卡片区
        #   挤成残条（实测：无卡宽 201 覆盖卡片态 412）。卡片态未变（卡片
        #   常驻，切页挤压仍需 restore 还原）或目标页无卡（宽度回归普通左栏
        #   语义，per-tab 记忆照常恢复）时放行。
        _snap_card = getattr(self, "_tab_switch_card_active_snapshot", None)
        if _snap_card is not None:
            _fn = getattr(self, "_left_card_area_active", None)
            if callable(_fn):
                try:
                    _card_now = bool(_fn())
                except Exception:
                    _card_now = None
                if _card_now is not None and _card_now != bool(_snap_card):
                    logger.debug(
                        f"[TSW] restore 放弃 prev={prev_left_w} 卡片态已变 {bool(_snap_card)}->{_card_now}"
                    )
                    return  # 卡片态翻转（开→关同样放弃）：宽度归卡片编排落位，
                    # 旧页基准含卡片槽总宽，迁到无卡页会把 TabPanel 拉到卡片态宽
                    # （实测：A 开卡 460 切到 B，restore 把无卡的 B 拉到 460，
                    # resizeEvent 自动展开 → 全局折叠态被翻转 → 来回切横跳）
        if getattr(panel, "_animating", False):
            # 切页时上一动作的宽度动画仍在跑：等动画结束再恢复（动画 200ms）
            logger.debug(f"[TSW] restore 延迟(动画中) prev={prev_left_w}")
            QTimer.singleShot(250, lambda: self._restore_tab_frame_after_tab_switch(prev_left_w, prev_collapsed))
            return
        # 卡片占位期：restore 仍执行——prev_left_w 是外层 frame 总宽（TabPanel+
        # 卡片），恢复它按切前比例还原内层分配，不破坏卡片编排；折叠态切前被
        # 编排锁定为折叠（prev_collapsed 一致），下方还原分支自然跳过。
        # （原实现一刀切跳过：用户环境 left 卡片常驻 → 切页保宽永不生效。）
        try:
            sizes = self._splitter.sizes()
        except Exception:
            return
        if not sizes:
            return
        if abs(sizes[0] - prev_left_w) <= 1 and bool(panel._collapsed) == prev_collapsed:
            return  # 未被挤压：零操作
        # 还原切前折叠态并清挤压标记（切页挤压痕迹一并抹掉，防后续
        # _maybe_auto_expand_after_squeeze 误触发自动展开）
        if bool(panel._collapsed) != prev_collapsed:
            panel._collapsed = prev_collapsed
            panel.sync_collapsed_ui()
        panel._collapsed_by_squeeze = False
        _before = sizes[0]
        self._splitter.setSizes(self._splitter_sizes_with_left(prev_left_w))
        _after = self._splitter.sizes()[0] if self._splitter.sizes() else -1
        logger.debug(
            f"[TSW] restore 执行 prev={prev_left_w} collapsed={prev_collapsed} {_before}->{_after}"
        )
        if not prev_collapsed:
            # 展开态恢复值即意图宽：回写全局 saved + 目标窗口 per-tab 记忆
            self._saved_panel_frame_width = prev_left_w
            _cur_win = self.get_current_window()
            if _cur_win is not None:
                _cur_win._tab_sidebar_frame_w = int(prev_left_w)

    def _write_tab_sidebar_memory(self, w: int) -> None:
        """把拖拽落定的宽度写进当前活跃对话窗口的 per-tab 宽度记忆"""
        win = self.get_current_window()
        if win is not None:
            win._tab_sidebar_frame_w = int(w)

    def _maybe_auto_expand_after_squeeze(self, growth_required: bool = True, _retried: bool = False):
        """挤压折叠后空间恢复：自动展开回常规宽度（含工作台反向恢复）

        规则 3 确认项：手动折叠（无挤压标记）永不自动展开——只有 _collapsed_by_squeeze
        标记的挤压折叠才参与自动恢复。原实现的 growth_required 豁免（手动折叠后
        拉宽窗口也展开）已按用户确认删除。

        触发点：窗口 resize 结束、overlay 卡片关闭、折叠动画结束。
        空间判定（两条件都满足才展开）：
        1. 相对增长（growth_required=True）：当前窗口总宽 ≥ 折叠时总宽 + 80。
           防止"折叠刚完成绝对条件恰满足就弹回"。仅当窗口比折叠时更宽
           （有新增空间）才自动展开，语义即"再有剩余空间时自动展开"。
        2. 绝对下限：窗口总宽 - 折叠宽 ≥ 展开目标宽 + 聊天区最小可用宽(400)，
           展开后面板与聊天区都放得下。

        规则 2 反向（先左后右）：左栏展开完成后检查协调折叠的工作台，
        空间再有富余则自动重开（_maybe_restore_workbench_after_squeeze）。

        动画时序兜底：若检测时折叠/展开动画仍在进行（_animating），延迟 250ms
        重试一次（动画 200ms 后必然结束），避免"用户快速开关卡片 → 折叠动画
        未结束 → 检查被跳过 → 折叠态残留"的恢复丢失。
        """
        if not hasattr(self, "_tab_panel"):
            return
        panel = self._tab_panel
        # ★ 卡片占位期：面板宽度语义由卡片编排独占（见 _evaluate_squeeze_collapse
        #   同款注释）。此时不得按"挤压恢复"逻辑自动展开面板——那会把用户拖出的
        #   内部分配覆盖掉。
        if getattr(panel, "_left_card_present", False):
            return
        if not panel._collapsed:
            # 左栏已展开：只查工作台的挤压恢复（规则 2 反向，先左后右由调用序保证）
            self._maybe_restore_workbench_after_squeeze(growth_required=growth_required)
            return
        # 规则 3 确认项：手动折叠永不自动展开，只有挤压标记的折叠才恢复
        if not panel._collapsed_by_squeeze:
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
        # ★ 展开目标下限须明显高于折叠线（同 _on_sidebar_toggled 注释）
        _min_expand_w = max(_EXPANDED_MIN_FRAME_WIDTH, _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X)
        target_w = max(_min_expand_w, getattr(self, "_saved_panel_frame_width", _min_expand_w))
        chat_min = _MIN_CHAT_WIDTH  # 聊天区最小可用宽度
        # 条件1：相对增长（仅窗口 resize 类触发需要；overlay 布局恢复传 False）
        if growth_required:
            base = getattr(self, "_squeeze_total_width", None)
            if base is not None and total < base + _AUTO_EXPAND_GROWTH:
                return  # 窗口未比折叠时更宽，不自动展开
        # 条件2：绝对下限
        if total - (self._tab_panel._collapsed_min_width + _FRAME_PADDING_X) < target_w + chat_min:
            return  # 空间不足，保持折叠
        # 空间足够：自动展开（先清标记防重入）
        panel._collapsed_by_squeeze = False
        panel.set_collapsed(False)
        self._saved_panel_frame_width = target_w
        self._on_sidebar_toggled(False)
        # 左栏刚展开完成：再查工作台是否也能恢复（规则 2 反向）
        self._maybe_restore_workbench_after_squeeze(growth_required=growth_required)

    def _maybe_restore_workbench_after_squeeze(self, growth_required: bool = True) -> None:
        """规则 2 反向：挤压折叠的工作台在空间恢复后自动重开

        前提：左栏已展开（"先左后右"顺序由 _maybe_auto_expand_after_squeeze
        的调用序保证）。判定（三者都满足才重开）：
        1. 仅 _wb_collapsed_by_squeeze=True（协调挤压折叠）——手动关闭永不重开；
        2. 相对增长（growth_required=True）：窗口比挤压时宽 _AUTO_EXPAND_GROWTH；
        3. 绝对下限：左栏收起宽 + 工作台最小宽 + 目标面板宽 + 聊天区最小宽都放得下。
        """
        if not getattr(self, "_wb_collapsed_by_squeeze", False):
            return
        frame = getattr(self, "_workbench_frame", None)
        if frame is None or getattr(self, "workbench_panel", None) is None:
            self._wb_collapsed_by_squeeze = False
            return
        panel = self._tab_panel
        if panel._animating:
            # 左栏动画中：延迟重试一次（对齐左栏恢复的时序兜底）
            QTimer.singleShot(
                250,
                lambda: self._maybe_restore_workbench_after_squeeze(growth_required=growth_required),
            )
            return
        from app.widgets.workbench_panel import PANEL_WIDTH_MIN

        try:
            total = sum(self._splitter.sizes())
        except Exception:
            return
        target_w = max(_EXPANDED_MIN_FRAME_WIDTH, getattr(self, "_saved_panel_frame_width", _EXPANDED_MIN_FRAME_WIDTH))
        wb_min = PANEL_WIDTH_MIN + 14
        if growth_required:
            base = getattr(self, "_squeeze_total_width", None)
            if base is not None and total < base + _AUTO_EXPAND_GROWTH:
                return  # 窗口未比挤压时更宽，不重开
        if total - (panel._collapsed_min_width + _FRAME_PADDING_X) - wb_min < target_w + _MIN_CHAT_WIDTH:
            return  # 空间不足，保持折叠
        self._wb_collapsed_by_squeeze = False
        self.set_workbench_visible(True)

    def _restore_sidebar_collapsed(self):
        """启动时按配置恢复侧边栏折叠态 + 面板宽度（规则 4：记住用户选择）

        窗口大小/位置仍固定默认（几何记忆已按需求移除，_do_save_geometry 维持空实现）。
        多轮补射（80/200/400/700ms）机制保留：对抗启动期多轮 relayout 弹跳，
        恢复目标从「固定展开」改为「存档折叠态」。工作台存档为开则启动瞬切打开。
        """
        if not hasattr(self, "_splitter"):
            return
        from app.utils.config import Settings

        # 实时读 cfg：补射窗口（80~700ms）内用户已折叠/展开的，跟随最新意图
        # 落位，不得用启动快照覆盖（旧实现读启动快照，启动后 0.7s 内的折叠
        # 被静默翻回，探针 WATCH 栈实锤）。
        self._apply_restored_panel_width()
        for delay in (80, 200, 400, 700):
            QTimer.singleShot(delay, self._apply_restored_panel_width)
        # 工作台：存档为开 → 启动瞬切打开（无动画，避免启动期叠加动画）
        if bool(Settings.get_instance().ui_workbench_visible.value):
            QTimer.singleShot(0, lambda: self.set_workbench_visible(True, animate=False))

    def _apply_restored_panel_width(self):
        """按存档折叠态恢复左面板宽度 + 解除启动误折叠（启动兜底）"""
        if not hasattr(self, "_splitter") or self._splitter.count() == 0:
            return
        from app.utils.config import Settings

        # ★ left 卡片区展开期间跳过：该状态下 TabPanel 收起、frame 加宽均为
        #   卡片编排的主动结果，启动补射（80/200/400/700ms）若照常按存档态
        #   落位会把卡片编排冲掉（表现为卡片打开后 TabPanel 又弹回展开态）。
        if self._left_card_area_active():
            return
        # 实时读 cfg：补射跟随用户最新折叠意图（启动后用户已操作的，
        # 按最新值落位；不用启动快照——它会把 0.7s 内的新操作静默翻回）
        saved_collapsed = bool(Settings.get_instance().ui_sidebar_collapsed.value)
        frame_w = (
            (self._tab_panel._collapsed_min_width + _FRAME_PADDING_X)
            if saved_collapsed
            else max(_EXPANDED_MIN_FRAME_WIDTH, _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X)
        )
        sizes = self._splitter.sizes()
        total = sum(sizes) if sizes else self.width()
        if total <= frame_w:
            return
        # 已在目标宽度附近则不重设（避免覆盖用户手动拖宽）
        if sizes and abs(sizes[0] - frame_w) <= 10:
            return
        frame_w = min(frame_w, total)
        self._splitter.setSizes([frame_w, max(0, total - frame_w)])
        # 对齐存档折叠态（set_collapsed 不发信号，避免与启动期动画互打断）
        if self._tab_panel._collapsed != saved_collapsed:
            self._tab_panel.set_collapsed(saved_collapsed)
        self._tab_panel.sync_collapsed_ui()

    # ── 覆盖层状态切换 ──

    def _consume_slide_direction(self, fallback: int) -> int:
        """取走缓存方向（消费即清零）：0 时用 fallback（开卡 +1 / 回对话 -1）

        fallback 对应 tab 栏静态布局的两个端点：对话 tab 恒在最左，
        无精确方向时回对话左滑、开卡右滑永远合理。
        """
        d = self._pending_slide_direction
        self._pending_slide_direction = 0
        return d if d != 0 else fallback

    def _on_overlay_state_changed(self, has_visible: bool):
        """系统卡片覆盖层显隐切换：QStackedWidget 页面 0←→1

        当 _global_top_container 报告有可见卡片时，切换到覆盖层页面（index 1），
        隐藏对话区、仅显示系统卡片；全部卡片关闭后切回对话区（index 0）。

        卡片打开时覆盖层页面可能引发布局 relayout 挤压左面板（宽度被压到
        < _auto_collapse_width），触发 TabPanel 自动折叠（_collapsed_by_squeeze=True）。
        卡片关闭后空间恢复：若折叠确为挤压所致（非用户手动），且可用宽度足够，
        则自动展开回常规宽度，避免折叠态残留。
        """
        # 覆盖层可见期间聊天页隐藏，其最小宽不应参与布局（否则顶住 splitter
        # 把手，覆盖层卡片无法被左右侧边栏挤压）。卡片自身下限已各自放开。
        self._content_stack.set_min_hint_disabled(has_visible)
        if has_visible:
            # [PERF] 承接覆盖层容器隐藏期主题刷新门控的补刷（见
            # CardContainer.refresh_style）。覆盖层走 QStackedWidget 切页，
            # 不触发 showEvent，故补刷挂在此处；★必须在 setCurrentIndex 之前
            # 完成，否则会先显示旧主题背景再刷（闪一帧）。
            _top_c = getattr(self, "_global_top_container", None)
            if _top_c is not None and getattr(_top_c, "_theme_needs_refresh", False):
                _top_c._theme_needs_refresh = False
                try:
                    _top_c._apply_background_style()
                except RuntimeError:
                    pass
            self._content_stack.setCurrentIndex(1, direction=self._consume_slide_direction(fallback=1))
        else:
            self._content_stack.setCurrentIndex(0, direction=self._consume_slide_direction(fallback=-1))
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
            # 覆盖层内互斥切换（页已在前台且无在途切换动画）：整页按 tab 位置差
            # 方向滑入（助手→左侧的轨迹 tab = 从左滑出），无事件时兜底右滑。
            # 首次从对话进覆盖层由 push 动画负责（在途检测跳过），
            # 事件先于切页到达时 currentIndex 仍为 0，同样自然跳过。
            if self._content_stack.currentIndex() == 1 and not self._content_stack.slide_in_progress():
                self._content_stack.slide_over(direction=self._consume_slide_direction(fallback=1))
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
            # 仅当关的是当前窗口的激活卡才补位激活剩余卡；后台 tab ×（对话视图
            # 或看其他卡时）不得触发自动切换（回归：胶囊从「对话」滑到最右）
            was_active_current = self._replace_active.get(self._current_window_id()) == card_id
            # 卡片是全局单例：从所有对话的 open 集合中移除（关闭后任何对话都不应再显示）
            owners = [wid for wid, od in self._replace_open.items() if card_id in od]
            for wid in owners:
                del self._replace_open[wid][card_id]
                if self._replace_active.get(wid) == card_id:
                    self._replace_active[wid] = None
            # tab 栏仅反映当前对话：仅当卡片归属当前对话时改 tab 栏
            if owners:
                # 常驻插件 tab（register_titlebar_tab，tab_id 与 full 卡共用）只清
                # open/active，不删 tab：它是插件固定入口，删了无人恢复（回归：
                # assistant_hub 点「新建人格」后标题栏「助手」tab 消失）
                if card_id not in self._plugin_titlebar_tab_ids:
                    # 关的是激活卡时不自动激活第一个：高亮由补位激活接管
                    self.titleBar.remove_tab(card_id, reactivate=not was_active_current)
                # 关掉的是当前激活卡 → 自动激活（互斥显示）最近一个；无剩余临时卡时内部回「对话」
                if was_active_current:
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
            self._cache_slide_direction(self._replace_active.get(cur_wid), CHAT_TAB_ID)
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

        # 乐观置活：不等 shown 事件，胶囊从被关 tab 直接滑向补位 tab（shown/显隐
        # 事件到达后同 target 幂等）；显示失败由 _schedule_replace_highlight 收敛
        # 兜底。不置活的话高亮悬空，等卡片创建完才从原处长滑过去。
        self._set_replace_active(nid)
        if nid in KNOWN_GLOBAL_REPLACE_CARDS:
            # 内置全局卡：toggle_floating_card 对其无效，走 CardManager 对称显示
            cm.show_card(nid, wid)
        else:
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
        cur_wid = self._current_window_id()
        # 长条模型方向：按 old/new 在 titleBar 的位置差缓存，页切换/nudge 消费
        self._cache_slide_direction(self._replace_active.get(cur_wid), card_id)
        self._replace_active[cur_wid] = card_id
        self.titleBar.set_active_tab(card_id or CHAT_TAB_ID)

    def _cache_slide_direction(self, old_tab_id, new_tab_id) -> None:
        """按 old/new 在 titleBar 的位置差缓存滑动方向（+1 右滑 / -1 左滑 / 0 未知）

        长条模型：页面按 tab 顺序平铺，切到右边 tab = 视口右移 = 新页从右进。
        收口在宿主自身激活方法（_set_replace_active/_show_conversation_view），
        不依赖 titleBar 信号链；old 已删（关卡补位）或相等时存 0，
        消费端（_consume_slide_direction）用端点兜底。
        """
        self._pending_slide_direction = self._direction_between(old_tab_id, new_tab_id)

    def _direction_between(self, old_tab_id, new_tab_id) -> int:
        """两 tab 的滑动方向（不落缓存）：new 在 old 右侧 +1 / 左侧 -1 / 未知 0"""
        if not old_tab_id or not new_tab_id or old_tab_id == new_tab_id:
            return 0
        tabs = list(getattr(self.titleBar, "_tabs", {}).keys())
        if old_tab_id in tabs and new_tab_id in tabs:
            return 1 if tabs.index(new_tab_id) > tabs.index(old_tab_id) else -1
        return 0

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

            # 卡间互斥切换（覆盖层前台 + 动效开启）：两段式滑动——旧卡画面
            # 整体滑出画外 → swap 闭包在画外执行互斥切换（新卡构建阻塞被掩蔽）
            # → 新卡画面从进场侧滑入。方向现场按 old/new 位置差算（此时
            # _set_replace_active 尚未执行，缓存不可用）。
            if (
                self._content_stack.currentIndex() == 1
                and not self._content_stack.slide_in_progress()
                and Animations.motion_enabled()
            ):
                direction = self._direction_between(self._replace_active.get(cur_wid), card_id) or 1
                self._pending_slide_direction = 0
                # 胶囊高亮与胶囊滑动立即跟随点击：卡片内容在画外切换（300ms 后才
                # 完成），但标题栏反馈必须即时，否则胶囊要等滑动结束才跳位，观感
                # 是「点了没反应，然后突然跳」。配合 _do_swap 里的打断守卫，
                # 期间若用户改点「对话」，此处的高亮会被随后的逻辑纠正。
                self._replace_active[cur_wid] = card_id
                self.titleBar.set_active_tab(card_id)

                def _do_swap(_cm=cm, _wid=wid, _cur=cur_wid, _cid=card_id):
                    # 打断守卫：段1 期间用户意图已变（如已点「对话」）则放弃切换，
                    # 状态以最新操作为准，杜绝「高亮已切、卡片被 cb 又开回」
                    if getattr(self.titleBar, "_active_id", None) != _cid:
                        return
                    for other in list(self._replace_open.get(_cur, {}).keys()):
                        if other != _cid:
                            try:
                                _cm.hide_card(other, _wid)
                            except Exception:
                                pass
                    try:
                        _cm.show_card(_cid, _wid)
                    except Exception:
                        pass
                    # 高亮已在点击时同步过（见上），这里只做幂等收敛
                    self._set_replace_active(_cid)

                self._content_stack.slide_swap(direction=direction, swap=_do_swap)
                return

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
        # 长条模型方向：回对话（tab 栏最左端），按当前激活卡位置差缓存
        self._cache_slide_direction(self._replace_active.get(cur_wid), CHAT_TAB_ID)
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
        # 仅当关的是当前窗口的激活卡才补位激活剩余卡；后台 tab ×（对话视图
        # 或看其他卡时）不得触发自动切换（回归：胶囊从「对话」滑到最右）
        was_active_current = self._replace_active.get(cur_wid) == card_id
        owners = [wid for wid, od in self._replace_open.items() if card_id in od]
        for wid in owners:
            del self._replace_open[wid][card_id]
            if self._replace_active.get(wid) == card_id:
                self._replace_active[wid] = None
        if owners:
            # 常驻插件 tab 只清 open/active 不删 tab（同 _on_replace_close_timeout）
            if card_id not in self._plugin_titlebar_tab_ids:
                # 关的是激活卡时不自动激活第一个：高亮由补位激活接管
                self.titleBar.remove_tab(card_id, reactivate=not was_active_current)

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

        if was_active_current:
            self._activate_remaining_replace_card()
        # 关掉的是当前高亮项时，_activate_remaining_replace_card 未必命中
        # （例如剩余项都是常驻插件 tab），补一次收敛兜底
        self._schedule_replace_highlight()

    def _toggle_sidebar_from_handle(self) -> None:
        """双击左栏把手：切换侧边栏收起/展开（与标题栏按钮同语义）

        与标题栏按钮共用 _toggle_sidebar 路径，保证行为完全一致：
        收起 → 动画收到 46px 图标条；展开 → 恢复 _saved_panel_frame_width。
        手动切换会清除挤压标记（尊重手动意图，不自动回弹）。
        """
        panel = getattr(self, "_tab_panel", None)
        if panel is None:
            return
        panel._collapsed_by_squeeze = False
        panel._toggle_sidebar()

    def _on_splitter_idle(self):
        """splitter 拖拽防抖超时：落定折叠态 + 恢复内容区绘制 + 解除节流

        ~120ms 无新 splitterMoved 即视为停手/松手，由 _splitter_idle_timer
        触发。

        ★★ 拖拽落定（抖动修复的收尾）：拖拽期 TabPanel 被置 _dragging_splitter
        抑制了 emit（否则逐帧跨阈值反复启停动画 = 抖动），因此折叠态与最终宽度
        必须在这里一次性落定：
          1. 解除 _dragging_splitter（后续 resizeEvent 恢复正常自治）；
          2. 按松手时的真实宽度决定目标态——低于折叠线则动画收到收起宽，
             否则动画恢复到当前拖出的宽度。用动画而非瞬切，保持与按钮切换
             一致的手感。
        合并恢复 _content_area.setUpdatesEnabled(True) 与
        _tab_panel.set_resizing(False)，复位拖拽首帧标记供下次拖拽重新冻结。
        """
        self._splitter_dragging = False
        panel = getattr(self, "_tab_panel", None)
        # ── 卡片打开期拖主把手：增量全部转入卡片区（TabPanel 锁定收起宽）──
        # 用户拖出的 frame 宽 pos 已在 _on_splitter_manually_moved 登记
        # （_left_card_drag_extra = 超出 TabPanel 收起宽的余量）；此处把
        # frame 收回到「TabPanel 收起宽 + 拖出余量」，新增余量即卡片槽位宽。
        if panel is not None and self._left_card_area_active() and getattr(self, "_left_card_drag_dirty", False):
            panel.set_dragging_splitter(False)
            self._left_card_drag_dirty = False
            sp = getattr(self, "_sidebar_splitter", None)
            wrapper = getattr(self, "_global_left_wrapper", None)
            if sp is not None and wrapper is not None:
                tab_keep = max(
                    self._tab_panel._collapsed_min_width,
                    min(getattr(self, "_left_card_tab_w_keep", _COLLAPSED_PANEL_WIDTH), _TAB_PANEL_MAX_CONTENT_WIDTH),
                )
                # ★ 折叠态按 tab_keep 实际宽度决定，不硬写 True：用户可能已在
                #   占位期拖内把手把 tab 拉宽（形态自治置 _collapsed=False），
                #   此处硬写会把用户拖出的展开态压回图标条。
                panel._collapsed = tab_keep < panel._auto_expand_width
                panel._collapsed_by_squeeze = False
                panel.sync_collapsed_ui()
                # frame 目标宽：不小于「tab 保留宽 + 卡片下限」，且不挤压聊天区
                frame_target = max(
                    tab_keep + _FRAME_PADDING_X + 40,
                    int(getattr(self, "_left_card_frame_target_w", 0)),
                )
                frame_target = self._left_card_frame_target(frame_target - tab_keep - _FRAME_PADDING_X) + (
                    tab_keep - _COLLAPSED_PANEL_WIDTH
                )
                card_w = max(0, frame_target - tab_keep - _FRAME_PADDING_X)
                self._tab_frame.setMaximumWidth(max(_tab_frame_max_expanded(card_w), frame_target))
                # ★ 显式锁死内部分配 [tab=保留宽, 卡片=card_w]：
                #   外层 frame 宽变化会被 QSplitter 按内层窗格**比例**重排,
                #   仅改外层会让 tab 被摊窄（实测 46→19），必须一次 setSizes 同步。
                idx = sp.indexOf(wrapper)
                tab_idx = sp.indexOf(panel)
                sizes = sp.sizes()
                if 0 <= idx < len(sizes) and 0 <= tab_idx < len(sizes):
                    new_sizes = list(sizes)
                    new_sizes[tab_idx] = tab_keep
                    new_sizes[idx] = card_w
                    sp.setSizes(new_sizes)
                if abs(self._splitter.sizes()[0] - frame_target) > 1:
                    self._splitter.setSizes(self._splitter_sizes_with_left(frame_target))
            self._finish_splitter_idle()
            return
        # ── 1+2. 拖拽落定：先解除抑制再判定，复用 _on_sidebar_toggled 的动画路径 ──
        if panel is not None and getattr(panel, "_dragging_splitter", False):
            panel.set_dragging_splitter(False)
            sizes = self._splitter.sizes()
            cur_frame_w = sizes[0] if sizes else 0
            # 以松手时的真实 frame 宽对照折叠线（content 口径 → frame 域换算）
            collapse_frame_w = panel._auto_collapse_width + _FRAME_PADDING_X
            if cur_frame_w < collapse_frame_w:
                # 拖到折叠线以下：落定收起态
                if not panel._collapsed:
                    panel._collapsed = True
                    panel._collapsed_by_squeeze = False  # 手动拖拽，尊重意图
                    panel._update_toggle_button(switch_ui=True)
                self._on_sidebar_toggled(True)
            else:
                # 拖到折叠线以上：落定展开态（宽度就位，动画把 UI 切回完整态）
                if panel._collapsed:
                    panel._collapsed = False
                    panel._update_toggle_button(switch_ui=True)
                    self._saved_panel_frame_width = cur_frame_w
                    self._write_tab_sidebar_memory(cur_frame_w)
                    self._on_sidebar_toggled(False)
                else:
                    # 本来就展开：记录用户拖出的意图宽（切页保宽恢复基准）
                    # + 同步 UI（拖拽期可能已按阈值切过紧凑态）
                    self._saved_panel_frame_width = cur_frame_w
                    self._write_tab_sidebar_memory(cur_frame_w)
                    logger.debug(f"[TSW] 拖拽落定(已展开) left={cur_frame_w} saved->{cur_frame_w}")
                    panel.sync_collapsed_ui()
        # 规则 4：拖拽把手属用户明确意图，松手落最终折叠态（值不变不写盘）
        from app.utils.config import Settings

        _cfg = Settings.get_instance()
        if panel is not None and _cfg.ui_sidebar_collapsed.value != panel._collapsed:
            _cfg.ui_sidebar_collapsed.value = panel._collapsed
        # ── #14 收尾：折叠/展开动画仍运行则跳过解冻，交 #4 动画 finally 统一恢复 ──
        # 防「拖拽跨折叠阈值触发动画 + 120ms idle timer 提前解冻」极端路径尾段
        # 额外 WebView 重绘。动画收尾由 _on_sidebar_anim_finished(try/finally) 恢复
        # _content_area(setUpdatesEnabled True) 与 _set_cards_resize_preview_mode(False)。
        # _splitter_dragging 已先行复位，后续 splitterMoved 可重新冻结下一拖拽会话。
        self._finish_splitter_idle()

    def _finish_splitter_idle(self):
        """splitter 拖拽会话收尾：动画运行则跳过，否则解冻绘制 + 复位节流"""
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
        """用户手动拖拽 splitter 把手：标记拖拽会话 + 清除挤压折叠标记

        挤压折叠标记（_collapsed_by_squeeze）仅用于"被外部 relayout 压窄
        自动折叠后自动展开"场景。用户手动拖动把手瘦身到折叠阈值以下，
        视为手动意图——不自动展开，避免关闭卡片/窗口拉宽时把用户拖窄的
        面板再撑开。

        ★★ 抖动修复：本方法只负责**开启拖拽会话**（置 panel._dragging_splitter），
        折叠/展开的落定全部交给 _on_splitter_idle（~120ms 无新事件 = 松手）。
        此前在拖拽过程中就按 pos 判定并 QTimer.singleShot(0, _on_sidebar_toggled)
        启动动画，而 splitterMoved 是逐帧触发的 → 拖拽区跨阈值时反复启停
        200ms 动画，动画与拖拽同时写 setSizes 互相打断 = 抖动。
        """
        if not hasattr(self, "_tab_panel") or not hasattr(self, "_splitter"):
            return
        # ★ 门控按「把手位置」判定，不硬编码 index：splitterMoved 上报的 index
        #   是**被移动把手右侧**窗格索引（实测 [tabFrame|chatFrame] 两窗格时
        #   左把手 index=1）。旧实现写 index != 0 直接 return，导致本条路径
        #   从未生效（拖拽会话不开启、侧栏抖动修复与本文件的卡片占位期改写全部
        #   落空）。改为：仅当该把手左邻是 tab_frame（即左栏与聊天区之间那条）
        #   才接管。
        if 0 <= index - 1 < self._splitter.count() and self._splitter.widget(index - 1) is not self._tab_frame:
            return
        panel = self._tab_panel
        # 手动拖拽：清除挤压折叠标记（无论方向，尊重手动意图）
        panel._collapsed_by_squeeze = False
        # ★ 卡片打开期的拖拽改写（产品语义：拖主把手增量只加卡片宽度，
        #   TabPanel 保持它当时的宽度不动）。此处只登记本次拖拽的起点与目标：
        #   - _left_card_tab_w_keep：拖拽起点 TabPanel 宽（用户此前拖内把手
        #     拉开的宽度要保留，不能硬编码收起宽——那会把用户拉开的 tab 收回）；
        #   - _left_card_frame_target_w：用户拖出的 frame 目标宽（= 把手位置）。
        #   真正的落定（改 outer/inner 分配）统一在 _on_splitter_idle 执行。
        if self._left_card_area_active():
            if not getattr(self, "_left_card_drag_dirty", False):
                sp = getattr(self, "_sidebar_splitter", None)
                if sp is not None:
                    sizes = sp.sizes()
                    tab_idx = sp.indexOf(panel)
                    self._left_card_tab_w_keep = sizes[tab_idx] if 0 <= tab_idx < len(sizes) else _COLLAPSED_PANEL_WIDTH
                else:
                    self._left_card_tab_w_keep = _COLLAPSED_PANEL_WIDTH
            self._left_card_drag_dirty = True
            self._left_card_frame_target_w = max(_COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X, int(pos))
        # ★ 开启拖拽会话：拖拽期 resizeEvent 只跟随 UI 形态，不 emit 折叠信号
        panel.set_dragging_splitter(True)
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
            tab_project_text_color = ""
            # 临时对话页：初始标题加「临时」前缀；图标用固定隐身紫「临」（不用项目色，字色固定白）
            if getattr(window, "_is_temp_scope", False):
                title = f"临时 · {title}"
                tab_project_initials = "临"
                tab_project_color = "#8B5CF6"
                tab_project_text_color = "#FFFFFF"
            elif project:
                try:
                    from app.utils.project_meta import (
                        extract_project_initials,
                        get_project_color,
                    )

                    tab_project_initials = extract_project_initials(project)
                    tab_project_color = get_project_color(project, alpha=255)
                except Exception:
                    pass

            tab_idx = panel.add_tab(
                title,
                icon=None,
                project_initials=tab_project_initials,
                project_color=tab_project_color,
                project_text_color=tab_project_text_color,
            )

            # 统一回调：标题变更时同步更新 Tab 标题 + 项目图标 + 宿主窗口标题 + 团队胶囊
            # ★ 使用 _window_to_index O(1) 字典查找，替代 _windows.index() O(n)
            def _on_win_title_changed(_new_title, _win=window):
                if _shiboken6.isValid(_win):
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
                if _shiboken6.isValid(_win):
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
            from app.core.team.team_manager import TeamManager

            tm = TeamManager.get_instance()
            run_id = TabManagerWindow._resolve_tab_team_id(window)
            team_name = getattr(window, "_team_name", "") or tm.DEFAULT_TEAM
            project = tm.get_project_for_run_id(run_id, team_name=team_name) if run_id else ""
            if not project:
                project = fallback
            if not project:
                return ("", "")
            from app.utils.project_meta import (
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
            # ★ 切页保宽：工作台显隐是 per-tab 记忆，切页瞬切会重分配 splitter
            #   空间，窗口放不下时左栏被压窄且 TabPanel 同步折叠；切回后释放的
            #   空间全归对话区，左栏宽度被永久污染（挤压自动展开要求窗口总宽
            #   增长 ≥_AUTO_EXPAND_GROWTH，布局性释放不触发）。记录切前左栏
            #   宽度/折叠态并抑制切页期折叠，切页完成后按「切页前后不变」恢复。
            _prev_left_w = 0
            _prev_collapsed = False
            try:
                _sizes = self._splitter.sizes()
                _prev_left_w = _sizes[0] if _sizes else 0
                _prev_collapsed = bool(self._tab_panel._collapsed)
                # ★ 挤压折叠非用户意图：切页时若左栏是被挤压收起的
                #   （_collapsed_by_squeeze），切前真实意图是展开态（意图宽即
                #   saved），切回后应恢复展开，而非停留在挤压折叠的图标条。
                if _prev_collapsed and getattr(self._tab_panel, "_collapsed_by_squeeze", False):
                    _prev_collapsed = False
                # ★ per-tab 宽度：目标窗口有独立宽度记忆时优先（每页记住自己
                #   拖出的宽度，互不串扰）；无记忆回落到全局意图宽逻辑。
                #   ★ 全局折叠态语义：折叠是窗口级状态，切页不得改变它——
                #   折叠态下即使目标页有宽度记忆也不得静默展开（旧实现强制
                #   _prev_collapsed=False，B 折叠后切回有记忆的 A 被静默拉开，
                #   随后 relayout 压窄触发 resizeEvent 折叠动画，来回切反复横跳）。
                _win_mem = None
                try:
                    _win_mem = getattr(self._windows[index], "_tab_sidebar_frame_w", None)
                except Exception:
                    pass
                _mem_base = self._tab_switch_win_memory_baseline(_prev_left_w, _prev_collapsed, _win_mem)
                if _mem_base is not None:
                    _prev_left_w, _prev_collapsed, self._tab_switch_saved_snapshot = _mem_base
                else:
                    # ★ 展开态以用户意图宽为恢复基准：即时宽度可能已被其他标签页
                    #   的工作台挤压污染（切回后空间释放，意图宽仍有效且可达）。
                    #   saved 缺省（从未拖拽）时退化为即时宽，行为不变。
                    _prev_left_w = self._tab_switch_baseline_width(_prev_left_w, _prev_collapsed)
                    # 快照切页时刻的意图宽：restore 执行时（含 250ms 动画重试）若
                    # 基准已变（中间发生了新拖拽/挤压恢复/卡片编排），放弃恢复
                    self._tab_switch_saved_snapshot = (
                        "saved",
                        getattr(self, "_saved_panel_frame_width", None),
                    )
                logger.debug(
                    f"[TSW] 切页 idx={index} raw={_sizes[0] if _sizes else 0} collapsed={_prev_collapsed} "
                    f"baseline={_prev_left_w} saved={self._tab_switch_saved_snapshot}"
                )
                self._tab_panel.set_auto_collapse_suppressed(True)
                # ★ 卡片态快照：记录切页瞬间（目标页投影前）left 卡片区显隐。
                #   restore 执行时与此刻不一致 → 宽度基准不可迁移（见守卫）。
                self._tab_switch_card_active_snapshot = bool(self._left_card_area_active())
            except Exception:
                pass
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
                from app.core.infra.ui_event_bus import EV_TAB_SWITCHED, UIEventBus

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
            # 切 tab 时工作台跟随激活窗：显隐/页签均按窗口独立记忆
            if getattr(self, "workbench_panel", None) is not None:
                # 🆕 先读目标窗口记忆再做任何刷新/恢复（先读后写，防程序化切页
                # 经 current_tab_changed 污染记忆——虽然发射已收窄到用户路径，
                # 仍按先读后写顺序双保险）
                saved_visible = getattr(win, "_workbench_visible_memory", None)
                saved_tab = getattr(win, "_workbench_tab_memory", None)
                # per-tab 显隐：目标窗口记忆与当前不同 → 瞬切（无动画，切窗要干脆）
                if saved_visible is not None and self.is_workbench_visible() != bool(saved_visible):
                    self.set_workbench_visible(bool(saved_visible), animate=False)
                if self.is_workbench_visible():
                    # 🚀 数据刷新延迟一帧：setCurrentWidget 先完成目标窗口渲染，
                    # 工作台全量刷新（产物/任务/工作树/历史换挂）在下一帧执行，
                    # 切标签的感知延迟不再叠加工作台刷新成本。★ 序号守卫：
                    # 快速连续切换时旧调度作废，防止旧窗口记忆覆盖新活跃窗口状态
                    self._wb_refresh_seq = getattr(self, "_wb_refresh_seq", 0) + 1
                    seq = self._wb_refresh_seq
                    QTimer.singleShot(
                        0,
                        lambda w=win, sv=saved_tab, s=seq: self._deferred_workbench_refresh(w, sv, s),
                    )
            # 切页完成：下一事件循环恢复切前左栏宽度并解除折叠抑制（等
            # QSplitter 重分配与工作台瞬切落位稳定后统一还原）
            if _prev_left_w > 0:
                QTimer.singleShot(
                    0,
                    lambda w=_prev_left_w, c=_prev_collapsed: self._restore_tab_frame_after_tab_switch(w, c),
                )

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
                        from app.core.team.team_manager import TeamManager

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
            from app.core.team.team_manager import TeamManager

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
            from PySide6.QtCore import QCoreApplication, QEvent

            QTimer.singleShot(0, lambda: QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete))
        except Exception:
            pass

        # 🛡️ U2 批量解散收尾：统一全局同步 1 次（替代原每成员 2 次：
        # _handle_team_leave 第 4 步 1 次 + 各窗口 closeEvent 1 次）。
        # 已关闭窗口已从 _instances 移除且 _is_destroyed=True，取任一存活
        # 窗口实例执行即可覆盖全量活跃集；无存活窗口时无需同步。
        try:
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
        import shiboken6 as sip

        try:
            if shiboken6.isValid(source_window) or shiboken6.isValid(getattr(source_window, "homepage", None)):
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
        branch_messages: "list | None" = None,
        branch_name: "str | None" = None,
        temp_scope: bool = False,
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
            branch_messages: 分支内容（消息卡片级分支用）。传入时以此为分支消息，
                不传则复制整个当前会话（tab 级分支的原行为）。
            branch_name: 分支会话标题；不传时由当前会话名 + " [分支]" 生成。
            temp_scope: 临时对话页（页内会话不落盘、不进历史，
                使用独立临时工作目录，关闭时删除）。
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
            # 临时对话页标记：必须在 showEvent 前设置（_sync_working_directory
            # 首次同步时读此标记决定 workdir 指向）
            new._is_temp_scope = temp_scope
            # 临时对话页：窗口内部「项目 avatar + 分支 chip」容器整体隐藏
            # （构造期已创建；首次 show 前置为不可见，后续
            # _refresh_project_branch_style 会保持隐藏）
            if temp_scope:
                container = getattr(new, "_project_branch_container", None)
                if container is not None:
                    container.setVisible(False)

            if branch:
                cur = getattr(source_window, "session_manager", None)
                cur_session = cur.get_current_session() if cur else None
                # 消息卡片级分支：优先用调用方传入的截断消息；不传则整会话复制（tab 级分支原行为）
                if cur_session is not None or branch_messages:
                    messages = list(branch_messages) if branch_messages is not None else list(cur_session.messages)
                    name = branch_name or ((cur_session.name or "对话") if cur_session is not None else "对话") + " [分支]"
                    new._branch_session_data = {
                        "messages": messages,
                        "name": name,
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
            # 临时对话页做分支无意义（不落盘）：入口已隐藏，此处兑底忽略
            if getattr(window, "_is_temp_scope", False):
                return
            if window is not None:
                self.spawn_tab(window, branch=True)

    def _on_tab_switch_session_requested(self, index: int):
        """标签页右键「切换会话」：跳转到该标签页并展开左侧历史会话卡

        两步语义（用户视角是一次点击）：
        1. **跳转标签页** —— ``_tab_panel.set_active_index(index)`` 同步发
           ``tabSelected`` → ``_on_tab_selected``（内容区切换 + per-tab 浮动卡
           投影 + 会话数据同步），无需额外切换动作。
        2. **打开历史会话** —— 历史卡是 ``container="left"`` 的全局浮动卡
           （``GLOBAL_WINDOW_ID``），走 ``toggle_floating_card``。★ 该入口是
           **toggle 语义**：卡片已可见时再调会把卡片关掉，故先用
           ``CardManager.is_card_visible`` 判定，仅在不可见时调用。
        3. 卡片按新活跃窗口重刷 —— ``HistoryService.refresh()`` 会经
           ``_locate_current_index()`` 重新定位「当前会话」，等价于跳到
           该标签页的会话位置（高亮 + 该分组渲染在列表最前）。
        """
        if not (0 <= index < len(self._windows)):
            return
        # 1) 跳转标签页（同步：set_active_index → tabSelected → _on_tab_selected）
        try:
            self._tab_panel.set_active_index(index)
        except Exception as e:
            logger.warning(f"[TabManagerWindow] 切换标签页失败: {e}")
        # 2) 展开左侧历史会话卡（非 toggle；不可见时才调用）
        registry = None
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            registry = UIPluginRegistry.get_instance()
        except Exception:
            registry = None
        if registry is None:
            return
        card_id = "history-manager"
        try:
            from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

            card_manager = getattr(self, "_card_manager", None)
            wid = getattr(self, "_window_id", None) or GLOBAL_WINDOW_ID
            visible = False
            if card_manager is not None:
                visible = card_manager.is_card_visible(card_id, wid)
            # 「工作台页签」形态：卡片已挂到右侧工作台 tab 时也算已打开
            if not visible and hasattr(self, "workbench_panel"):
                try:
                    visible = self.workbench_panel.has_card_tab(card_id)
                except Exception:
                    visible = False
            if not visible:
                registry.toggle_floating_card(card_id, main_widget=self.get_current_window())
        except Exception as e:
            logger.warning(f"[TabManagerWindow] 打开历史会话失败: {e}")
            return
        # 3) 按新活跃窗口重刷（当前会话高亮 / 分组定位跟随激活窗）
        service = registry.get_service("history")
        if service is not None:
            try:
                service.refresh()
            except Exception as e:
                logger.warning(f"[TabManagerWindow] 刷新历史会话卡失败: {e}")

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

    def _on_new_project_requested(self):
        """工作区树「工作区」标题行 + ：新建项目（选文件夹流程）

        复用当前窗口的 _on_project_open_folder_btn（文件夹选择器 → 弹建项目
        对话框 → 绑定根目录），与既有拖拽建项目同一流程、同一数据路径。
        """
        source = self.get_current_window()
        if source is None:
            return
        opener = getattr(source, "_on_project_open_folder_btn", None)
        if callable(opener):
            try:
                opener()
            except Exception as e:
                logger.warning(f"[TabManagerWindow] 新建项目失败: {e}")

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

    def refresh_workspace_tree(self, force: bool = False):
        """刷新左侧工作区树（历史会话增删改后调用）

        批2：默认 250ms 防抖合并（连续新建/关闭 tab、批量数据变更时整树
        重建收敛为一次）；force=True 跳过防抖立即执行。
        """
        panel = getattr(self, "_tab_panel", None)
        if panel is None or not hasattr(panel, "refresh_tree"):
            return
        if force:
            panel.refresh_tree()
            return
        timer = getattr(self, "_tree_refresh_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(250)
            timer.timeout.connect(self._flush_tree_refresh)
            self._tree_refresh_timer = timer
        timer.start()

    def _flush_tree_refresh(self):
        """树刷新防抖到期：真正执行 refresh_tree"""
        panel = getattr(self, "_tab_panel", None)
        if panel is not None and hasattr(panel, "refresh_tree"):
            panel.refresh_tree()

    def _on_tab_count_changed_for_tree(self, _count: int):
        """Tab 数量变化 → 刷新工作区树（批2：250ms 防抖合并连续新建/关闭）"""
        panel = getattr(self, "_tab_panel", None)
        if panel is not None and getattr(panel, "current_mode", lambda: "list")() == "tree":
            self.refresh_workspace_tree()

    def _on_new_tab_requested(self, temp_scope: bool = False):
        """新建窗口 — 走当前窗口的复制逻辑，复用后端状态

        Args:
            temp_scope: True 时新建临时对话页（页内会话不落盘、不进历史）。
        """
        # 批5 5b：壳期（首窗构造中）点击 + → 排队，ready 后由
        # _mark_first_window_ready 补执行恰一次
        if not getattr(self, "_first_window_ready", True):
            self._pending_new_tab_request = True
            return
        current = self.get_current_window()
        if current is not None:
            # 从当前窗口复制（保留后端上下文）并新开标签页
            self.spawn_tab(current, new_session=True, temp_scope=temp_scope)
        else:
            # 没有当前窗口时，走基础创建逻辑
            from app.main_widget import OpenAIChatToolWindow

            fake_page = self._create_fake_page()
            new_window = OpenAIChatToolWindow(fake_page)
            new_window._is_temp_scope = temp_scope
            self.add_window(new_window)
            if not self.isVisible():
                self.show()

    def _mark_first_window_ready(self):
        """批5 5b：首窗就绪标记；壳期排队的 + 请求在此补执行恰一次"""
        self._first_window_ready = True
        if getattr(self, "_pending_new_tab_request", False):
            self._pending_new_tab_request = False
            self._on_new_tab_requested()

    # ── 批5 5a：壳期占位（首窗构造期间覆盖显示）──

    def show_boot_placeholder(self):
        """显示「正在准备会话…」空壳占位（覆盖在内容区上，不动 Tab 栈索引）"""
        if getattr(self, "_boot_placeholder", None) is not None:
            return
        from qfluentwidgets import IndeterminateProgressRing

        from app.utils.design_tokens import Colors, font_size_css
        from app.utils.utils import get_font_family_css, get_unified_font

        ph = QWidget(self)
        ph.setObjectName("bootPlaceholder")
        lay = QVBoxLayout(ph)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(16)
        ring = IndeterminateProgressRing(ph)
        ring.setFixedSize(36, 36)
        lay.addWidget(ring, 0, Qt.AlignCenter)
        text = QLabel("正在准备会话…", ph)
        text.setAlignment(Qt.AlignCenter)
        text.setFont(get_unified_font(14))
        text.setStyleSheet(f"color: {Colors.TEXT_MUTED}; background: transparent; {font_size_css(14)}")
        lay.addWidget(text)
        ph.setStyleSheet("background: transparent;")
        self._boot_placeholder = ph
        ph.setGeometry(self._content_area.geometry())
        ph.raise_()
        ph.show()

    def remove_boot_placeholder(self):
        """首窗就绪后移除壳期占位"""
        ph = getattr(self, "_boot_placeholder", None)
        if ph is None:
            return
        self._boot_placeholder = None
        ph.deleteLater()

    # ── Tab 面板 UI 插件列表 ──

    #: 插件常驻 tab 容压上限：超过时前 LIMIT-1 个驻留，其余收进「更多」下拉
    _PLUGIN_TITLEBAR_TAB_LIMIT = 5
    #: 「更多」聚合 tab 的固定 ID（不与插件 tab_id 冲突的保留名）
    _MORE_TAB_ID = "__plugin_more__"

    def _sync_plugin_titlebar_tabs(self) -> None:
        """同步插件注册的常驻标题栏 tab（无 × 关闭钮；点击走插件 on_click 回调自展示）

        幂等：info 对象未变（未重载）的已挂载 tab 跳过；插件热重载后 registry
        中是全新 info → 摘旧挂新；已卸载插件的 tab 移除。注册表为空时仅做清理。

        容压：插件 tab 超过 ``_PLUGIN_TITLEBAR_TAB_LIMIT`` 时，前 LIMIT-1 个驻留，
        其余收进「更多」聚合下拉（防插件生态增长挤爆标题栏 tab 区）。
        """
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            infos = UIPluginRegistry.get_instance().get_titlebar_tabs()
        except Exception:
            infos = []
        # 容压分流：溢出集每次全量重建，「更多」tab 闭包经 self._more_tab_infos 取最新
        limit = self._PLUGIN_TITLEBAR_TAB_LIMIT
        overflow_infos = []
        if len(infos) > limit:
            overflow_infos = infos[limit - 1 :]
            infos = infos[: limit - 1]
        self._more_tab_infos = overflow_infos
        valid_ids = {info.tab_id for info in infos}
        # 移除已卸载/不再注册/被容压收起的常驻 tab
        for tab_id in list(self._plugin_titlebar_tab_ids):
            if tab_id not in valid_ids:
                self.titleBar.remove_tab(tab_id)
                self._plugin_titlebar_tab_ids.discard(tab_id)
                self._plugin_titlebar_tab_infos.pop(tab_id, None)
        for info in infos:
            mounted = self._plugin_titlebar_tab_infos.get(info.tab_id)
            if info.tab_id in self.titleBar._tabs:
                if mounted is info:
                    continue  # 未重载，幂等跳过
                # 插件热重载后 info 是全新对象（label/icon/on_click 均可能更新）
                # → 摘除旧 tab 再挂新，否则 titleBar 一直引用旧模块闭包
                self.titleBar.remove_tab(info.tab_id)
            self.titleBar.add_tab(
                info.tab_id,
                info.label,
                on_click=info.on_click,
                closable=False,
                icon_path=info.icon_path or "",
            )
            self._plugin_titlebar_tab_ids.add(info.tab_id)
            self._plugin_titlebar_tab_infos[info.tab_id] = info
        # 「更多」聚合 tab：溢出集非空挂载、为空移除；生命周期由本函数显式管理，
        # 不入 _plugin_titlebar_tab_ids（避免被上面的清理循环误删）
        more_id = self._MORE_TAB_ID
        if overflow_infos:
            if more_id not in self.titleBar._tabs:
                self.titleBar.add_tab(more_id, "更多", on_click=self._show_more_tabs_menu, closable=False)
        elif more_id in self.titleBar._tabs:
            self.titleBar.remove_tab(more_id)

    def _show_more_tabs_menu(self) -> None:
        """「更多」聚合下拉：列出被容压收起的插件 tab，点击等价点击原 tab"""
        infos = list(getattr(self, "_more_tab_infos", []))
        if not infos:
            return
        from PySide6.QtGui import QCursor

        try:
            from qfluentwidgets import Action, RoundMenu

            menu = RoundMenu(parent=self)
            for info in infos:
                cb = info.on_click
                if cb is None:
                    continue
                menu.addAction(Action(info.label, lambda _checked=False, f=cb: f()))
            menu.exec(QCursor.pos())
        except Exception:
            # qfluentwidgets 不可用时退化为原生 QMenu（测试环境/旧依赖兜底）
            from PySide6.QtWidgets import QMenu

            menu = QMenu(self)
            for info in infos:
                cb = info.on_click
                if cb is None:
                    continue
                act = menu.addAction(info.label)
                act.triggered.connect(lambda _checked=False, f=cb: f())
            menu.exec(QCursor.pos())

    def _update_shared_launcher(self) -> None:
        """兼容旧调用方（main_widget.py 热重载和模式切换）并刷新内嵌列表"""
        self._tab_panel.refresh_ui_plugins()
        self._sync_plugin_titlebar_tabs()
        # 工作区页面刷新（Phase G）：注册集变化后重建 sidebar 入口 + 销毁被卸载页面
        if hasattr(self, "_workspace_page_host"):
            self._workspace_page_host.refresh_pages()

    @staticmethod
    def _create_fake_page():
        """创建一个临时的 FakePage 用于窗口初始化（与 main.py 类似）"""
        from PySide6.QtWidgets import QWidget

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
        """固定默认窗口几何：1100x800，屏幕居中，确保不超出屏幕"""
        screen = QApplication.primaryScreen()
        screen_rect = screen.availableGeometry() if screen else None
        if not screen_rect:
            self.resize(1100, 800)
            return

        w, h = 1100, 800
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
        # 原生窗口能力补全（边缘 resize + Aero Snap）+ Win11 DWM 圆角 / 压掉
        # 边框描边。hwnd 会因 setWindowFlags / 跨屏 DPI 变化重建，DWM 属性随之
        # 丢失，故每次显示都重设一遍（内部幂等）。
        self._ensure_native_window_styles()
        # 标题栏宽度必须显式同步：构造期几何恢复走的是 resize 节流路径，
        # 基类那次 titleBar.resize() 会被跳过（详见 _sync_title_bar_width）。
        self._sync_title_bar_width()
        if not self._geometry_applied:
            self._geometry_applied = True
            self._restore_geometry()
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
        # macOS：close→show 循环后修复标题栏错位（见 _refresh_mac_fullsize_content）
        if _IS_MAC:
            self._refresh_mac_fullsize_content()
        # [T30] 窗口重新显示可见性恢复点：补刷隐藏期置脏的顶层容器
        self._refresh_theme_dirty_frames()

    def _refresh_mac_fullsize_content(self) -> None:
        """macOS：强制重算 NSWindow 的 fullSizeContentView 布局

        现象：窗口关闭（红绿灯，closeEvent accept → NSWindow orderOut，对象保留）
        后从 dock 唤起（show → orderFront），窗口整体退回原生样式：顶部出现
        原生标题栏行，自定义标题栏被压到第二行。

        根因：close→show 循环中 AppKit 会把 NSFullSizeContentViewWindowMask
        位清掉（或 Qt 重建 NSWindow），contentView 布局退回「标题栏之下」。
        库（MacFramelessWindow）只在 paintEvent/changeEvent 里用 ``styleMask |
        full`` 幂等置位——即使位从无到有加上，AppKit 也可能不再重算 contentView
        frame；且 qframelesswindow 缓存的 ``__nsWindow`` 若指向已重建前的旧
        窗口，库后续所有设置都会落空。

        修法：① ``updateFrameless()`` 让库重新绑定当前 NSWindow 并重置
        titlebar（幂等）；② 无条件确保 full 位戴上，并用「先摘后戴」制造一次
        真实变化，强制 AppKit 重算 contentView frame（对相同值 AppKit 是
        no-op，必须先摘后戴）。摘/戴在同一事件栈内完成，不经过绘制循环，无闪烁。
        """
        try:
            import Cocoa
            from qframelesswindow.utils.mac_utils import getNSWindow
        except ImportError:
            return

        def _apply():
            try:
                if not self.isVisible():
                    return
                # NSWindow 可能已被 Qt 重建：让库重新绑定当前窗口并重设
                # titlebar（含交通灯可见性，幂等，失败不阻断摘戴）
                try:
                    self.updateFrameless()
                except Exception:
                    pass
                ns_window = getNSWindow(int(self.winId()))
                mask = int(ns_window.styleMask())
                full = int(Cocoa.NSFullSizeContentViewWindowMask)
                # close→show 后 full 位可能已被 AppKit 清掉：无条件确保戴上；
                # 先摘后戴制造一次真实变化才能触发布局重算（相同值是 no-op）
                if mask & full:
                    ns_window.setStyleMask_(mask & ~full)
                ns_window.setStyleMask_(mask | full)
                # 与 MacFramelessWindow._hideSystemTitleBar 保持一致的重申（幂等）
                ns_window.setTitlebarAppearsTransparent_(True)
                ns_window.setTitleVisibility_(Cocoa.NSWindowTitleHidden)
                ns_window.setMovable_(False)
            except Exception:
                logger.warning("[mac] 刷新 fullSizeContentView 失败", exc_info=True)

        _apply()
        # orderFront 后 AppKit 可能还有一次异步布局，事件循环空转后再兜底
        QTimer.singleShot(0, _apply)
        QTimer.singleShot(100, _apply)

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
        # hover 浮层跟随（独立 Tool 顶层窗口，全局坐标手动同步）
        self._sync_wb_overlay_geometry()

    def _sync_wb_overlay_geometry(self) -> None:
        """主窗口 move/resize 后同步工作台 hover 浮层全局坐标（未装配/隐藏时跳过）

        浮层是独立 Tool 顶层窗口，不会随主窗口布局自动移动，须在宿主
        moveEvent/resizeEvent 里手动重定位（见 sidebar_hover_preview 跟随策略）。
        """
        ov = getattr(self, "_wb_overlay", None)
        if ov is not None:
            ov.sync_to_window()

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

        # Win11 DWM 圆角 + 压掉稳定态的边框描边（过渡帧那次重画由 nativeEvent
        # 拦截 WM_NCACTIVATE / WM_NCPAINT / WM_ERASEBKGND 负责，见方法文档）。
        # 放在这里而非构造期：hwnd 重建后 DWM 属性会丢，必须随每次显示重设。
        self._apply_win11_dwm_chrome()

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

        另在此拦截非客户区（NC）消息：WM_NCACTIVATE / WM_NCPAINT /
        WM_ERASEBKGND。窗口补回 WS_THICKFRAME 后系统会把它当"有框窗口"，
        在激活状态变化的那一帧按默认外观重画一次非客户区 —— 表现为切窗 /
        重聚焦时窗口最外圈闪一圈白框。本层直接声明"已处理"拒掉这次重画。

        WM_SETFOCUS / WM_KILLFOCUS 也在拦截之列：基类在注册表
        DWM\\ColorPrevalence=1 时会借它们改边框颜色（见分支内注释）。
        """
        # ★ 热路径：拖拽时每秒有上千条原生消息经过这里（WM_MOUSEMOVE /
        # WM_NCHITTEST 等），任何 per-call 开销都会被放大。
        # 判定/结构体/cast 全部使用模块级缓存（_IS_WINDOWS / _MSG_CAST），
        # 禁止在此函数内做 import 或 platform.system() 调用。
        if _IS_WINDOWS and _MSG_CAST is not None and eventType == "windows_generic_MSG":
            try:
                msg = _MSG_CAST(int(message))[0]
                msg_id = msg.message
                # ── 切窗白框闪的抑制点（2026-09-12 像素级实测）──
                # 实测：焦点切换后第一帧，窗口最外圈出现纯白 #FFFFFF ——
                # 左 / 上各 13px、右 / 下各 1px（合计 14px 一圈，本机 150% DPI），
                # 下一帧即消失。该圈由**系统**在激活过渡帧按默认窗口外观绘制，
                # 与本窗口自绘内容无关（自建的 `_native_hit_test` 只做命中判定，
                # 一个像素都不画）。DWMWA_BORDER_COLOR=NONE 只压掉了稳定态的
                # 描边，压不住过渡帧这一次重画，故从消息层直接拒掉：
                #   · WM_NCACTIVATE → TRUE：不触发默认的非客户区激活重画；
                #   · WM_NCPAINT    → 0：不绘制帧 / 标题栏；
                #   · WM_ERASEBKGND → 1：不让系统用窗口类默认刷（白）擦背景，
                #     客户区由 Qt 整块自绘，不需要系统代劳。
                # 三者都只作用于非客户区，不影响 Qt 的焦点链与客户区重绘。
                if msg_id == self._WM_NCACTIVATE:
                    return True, 1
                if msg_id == self._WM_NCPAINT:
                    return True, 0
                if msg_id == self._WM_ERASEBKGND:
                    return True, 1
                if msg_id == self._WM_SETFOCUS or msg_id == self._WM_KILLFOCUS:
                    # 基类 qframelesswindow 在注册表 DWM\ColorPrevalence=1
                    # （"在标题栏和窗口边框上显示强调色"）时，会把
                    # DWMWA_BORDER_COLOR 染成系统强调色、失焦时复位为
                    # DWMWA_COLOR_DEFAULT(0xFFFFFFFF) —— "默认"即那条随激活
                    # 状态忽明忽暗的描边。本窗口已把描边固定为 DWMWA_COLOR_NONE
                    # （见 _apply_win11_dwm_chrome），这里返回 (False, 0) 跳过的
                    # 只是基类的着色逻辑，Qt 的焦点消息处理完全不受影响。
                    # 注：本机 ColorPrevalence=0，基类该分支本就不会触发，
                    # 此处为跨机器防御（开启强调色边框的机器上才会生效）。
                    return False, 0
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
                        from app.utils import window_drag_state as _wds
                        from app.utils.drag_stall_profiler import drag_profiler

                        # ★ 必须写模块属性：from-import 后赋值只改局部名，
                        # 全局标志永远不变（拖拽节流失效的根因）
                        _wds.any_window_dragging = False
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
        from app.utils import window_drag_state as _wds
        from app.utils.drag_stall_profiler import drag_profiler

        # ★ 必须写模块属性：from-import 后赋值只改局部名，全局标志永远不变
        _wds.any_window_dragging = True
        if hasattr(self, "_tab_panel"):
            self._tab_panel.set_resizing(True)  # 暂停动画定时器
        # 诊断：拖拽期间抓取主线程阻塞现场（定位偶发卡顿元凶）
        drag_profiler.start()

    def _on_window_drag_end(self):
        """窗口拖拽结束：恢复动画定时器 + 解除节流 + 触发一次几何保存

        无 setUpdatesEnabled(True) → 无积压重绘炸弹，松手零代价。
        """
        from app.utils import window_drag_state as _wds
        from app.utils.drag_stall_profiler import drag_profiler

        # ★ 必须写模块属性：from-import 后赋值只改局部名，全局标志永远不变
        _wds.any_window_dragging = False
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
            # 最小化时 owned 浮层被系统连带隐藏：预览态需同步收场
            # （on_clicked 取消缓收并走非 promote 的 leave 路径）
            if self.isMinimized():
                ctrl = getattr(self, "_wb_preview_ctrl", None)
                if ctrl is not None and ctrl.is_previewing():
                    ctrl.on_clicked()

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

        窗口超宽时：wrapper 宽度 > 当前限宽档位，左右各留
        (wrapper_w - 档位宽)/2 的空白，chat_frame 精确居中；
        窗口不足该宽度（或档位为 full）时 margins 归零，chat_frame 占满 wrapper。
        """
        # ── 右侧工作台浮层 hover 转发 ──
        # 浮层自身 WA_Hover 触发 HoverEnter/Leave，但不开信号；
        # 这里透传给控制器，让它在「光标停在浮层」时取消缓收计时。
        ov = getattr(self, "_wb_overlay", None)
        ctrl = getattr(self, "_wb_preview_ctrl", None)
        if ov is not None and ctrl is not None and obj is ov:
            t = event.type()
            if t == QEvent.HoverEnter:
                ctrl.on_overlay_hover(True)
            elif t == QEvent.HoverLeave:
                ctrl.on_overlay_hover(False)
            # 不 return：让既有 super().eventFilter 继续处理（不影响）
        # ── 左栏 / 聊天区之间的 splitter 把手：双击折叠/展开侧边栏 ──
        # ★ 必须在 handle 上过滤：QSplitter 命中 handle 时 MouseButtonDblClick
        #   不会冒泡到 QSplitter 自身，装在 splitter 上收不到。
        if obj is getattr(self, "_splitter_left_handle", None) and event.type() == QEvent.MouseButtonDblClick:
            if event.button() == Qt.LeftButton:
                self._toggle_sidebar_from_handle()
                return True
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

        - 对话区（index 0）：按当前宽度档位限宽居中；full 档不限宽，margins 归零
        - 覆盖层（index 1）：配置类卡片（系统设置/服务商/Hook/MCP）按
          覆盖层宽度限宽居中；内容型卡片（diff_viewer、
          子智能体会话、full 浮动卡）铺满全宽
        """
        wrapper = self._chat_wrapper
        w = wrapper.width()
        if w <= 0:
            return
        if self._content_stack.currentIndex() == 1:
            overlay_w = _current_overlay_width()
            pad = max(0, (w - overlay_w) // 2) if overlay_w and self._overlay_should_limit_width() else 0
        else:
            chat_w = _current_chat_width()
            pad = max(0, (w - chat_w) // 2) if chat_w else 0
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
            self._sync_wb_overlay_geometry()
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
            self._sync_wb_overlay_geometry()
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
        self._sync_wb_overlay_geometry()

    def closeEvent(self, event: QCloseEvent):
        """关闭 TabManagerWindow 时不销毁，仅隐藏到系统托盘

        对于标准系统窗口，必须 accept 事件让 Qt 正确更新内部状态；
        WA_DeleteOnClose=False 防止窗口被销毁，Qt 只会隐藏窗口。
        event.ignore() 在此场景下会导致 Qt 内部状态不一致，
        后续 show() 无法正常恢复窗口。
        """
        # 批5 5c：壳期（首窗构造中）关闭 → 最小关闭路径（几何落盘 + accept），
        # 跳过依赖首窗的清理链，避免半构造状态异常
        if not getattr(self, "_first_window_ready", True):
            try:
                self._do_save_geometry()
            except Exception:
                pass
            event.accept()
            return
        event.accept()

    # ── 资源清理 ──

    def cleanup(self):
        """清理所有窗口和资源"""
        TabManagerWindow._instance = None
        # ★ 停止应用级服务（Gateway 平台 WebSocket 断连 + 插件 watcher 线程）
        try:
            from app.core.services.gateway_service import GatewayService
            from app.core.services.plugin_host_service import PluginHostService

            GatewayService.get_instance().stop()
            PluginHostService.get_instance().stop()
        except Exception:
            pass
        # ★ 关闭清理缺口（审计 #3 P1-1）：LSP 子进程 / gateway 事件循环 /
        #   LLM API 服务 / Gitee 同步线程原先全凭 daemon 兜底，退出瞬间端口与
        #   线程未有序关闭。四者均幂等，未启动时为 no-op。
        try:
            from app.core.lsp.lsp_manager import get_lsp_manager

            get_lsp_manager().shutdown()
        except Exception as e:
            logger.warning(f"[TabManager] cleanup LspManager.shutdown 失败: {e}")
        try:
            from app.gateway.manager import get_platform_manager

            mgr = get_platform_manager()
            if mgr is not None:
                mgr.shutdown()
        except Exception as e:
            logger.warning(f"[TabManager] cleanup PlatformManager.shutdown 失败: {e}")
        try:
            from app.gateway import stop_llm_api_service

            stop_llm_api_service()
        except Exception as e:
            logger.warning(f"[TabManager] cleanup stop_llm_api_service 失败: {e}")
        try:
            from app.core.sync.config_sync import ConfigSyncService

            ConfigSyncService.get_instance().disable()
        except Exception as e:
            logger.warning(f"[TabManager] cleanup ConfigSyncService.disable 失败: {e}")
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
