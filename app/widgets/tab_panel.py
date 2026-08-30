# -*- coding: utf-8 -*-
"""
TabPanel — Tab 管理器左侧面板

每个 Tab 项显示：Agent 图标 + 会话标题 + 关闭按钮。
支持拖拽排序、右键菜单、滚轮滚动。
"""

import math as _math
import os

# ── 模块级缓存：避免 paintEvent 中反复解析 rgba 字符串 ──
import re as _re
from typing import List, Optional

from loguru import logger
from PyQt5.QtCore import QSize, Qt, QTimer, pyqtSignal, pyqtProperty, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QColor, QIcon, QMouseEvent, QPainter, QPixmap, QPen, QTransform
from PyQt5.QtGui import (
    QColor as _QColor,
)
from PyQt5.QtGui import (
    QLinearGradient as _QLinearGradient,
)
from PyQt5.QtGui import (
    QPainterPath as _QPainterPath,
)
from PyQt5.QtGui import (
    QPen as _QPen,
)
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)
from qfluentwidgets import (
    TransparentToolButton,
    isDarkTheme,
)

from app.utils.config import Settings
from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style, scale_font_size, scale_icon_size
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css, get_icon, get_unified_font
from app.widgets.cards.settings.gitee_card import GiteeAccountRow
from app.widgets.elided_label import _ElidedLabel
from app.widgets.panel_mode_popup import PanelModePopup
from app.widgets.workspace_tree import (
    KIND_PROJECT,
    KIND_SESSION,
    KIND_TEAM,
    KIND_WIDGET,
    KIND_WORKTREE,
    TreeNodeSpec,
    WorkspaceTree,
)


def _parse_rgba(rgba_str: str) -> _QColor:
    """将 'rgba(r,g,b,a)' 字符串解析为 QColor，缓存结果"""
    try:
        if rgba_str.startswith("rgba("):
            parts = rgba_str.strip("rgba() ").split(",")
            r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
            a_raw = parts[3].strip()
            a = int(float(a_raw) * 255) if float(a_raw) <= 1 else int(a_raw)
            return _QColor(r, g, b, a)
    except Exception:
        pass
    return _QColor(rgba_str)


# 预解析常用颜色（首次导入时计算一次，后续通过 _invalidate_cached_colors() 刷新）
_CACHED_SELECTED_BG = _parse_rgba(Colors.SELECTED_BG)
_CACHED_INFO = _parse_rgba(Colors.INFO)

# ── 对话页面板显示模式 ──
PANEL_MODE_LIST = "list"  # 列表模式（默认）：已打开对话页 + 团队框平铺
PANEL_MODE_TREE = "tree"  # 工作区树模式：项目 → 工作树 → 会话

# 树模式缩进（像素）：项目头 0 / 工作树头·团队头 8 / 已打开 Tab 14 / 会话行 42
# 四个数值是配套的，改一个必须同步另外三个：
#   已打开 Tab：内部左边距 8+14，项目图标 20px + 间距 6 → 文字起点 48
#   会话行    ：左边距 6+42 → 文字起点 48（会话行不带图标，见 workspace_tree）
_TREE_INDENT_PROJECT = 0
_TREE_INDENT_WORKTREE = 8
_TREE_INDENT_TAB = 14
_TREE_INDENT_SESSION = 42
# 单个工作树下最多渲染的历史会话条数（侧栏窄，防止一次性构建上百行）
_TREE_MAX_SESSIONS = 50

# 模式选择悬浮框的选项（(mode, 主标签, 说明)）
_PANEL_MODE_OPTIONS = (
    (PANEL_MODE_LIST, "列表模式", "按打开顺序平铺对话页"),
    (PANEL_MODE_TREE, "工作区树模式", "按项目 / 工作树归组"),
)


def _invalidate_cached_colors():
    """主题变更后重新解析缓存颜色，确保 paintEvent 使用最新色值"""
    global _CACHED_SELECTED_BG, _CACHED_INFO
    _CACHED_SELECTED_BG = _parse_rgba(Colors.SELECTED_BG)
    _CACHED_INFO = _parse_rgba(Colors.INFO)


# 彩虹动画颜色列表（模块级常量，避免每次 paint 创建 10 个 QColor）
_RAINBOW_COLORS = [
    _QColor("#60D4FF"),
    _QColor("#40C8FF"),
    _QColor("#4DA6FF"),
    _QColor("#8B7BFF"),
    _QColor("#C084FC"),
    _QColor("#F472B6"),
    _QColor("#FB7185"),
    _QColor("#F59E0B"),
    _QColor("#34D399"),
    _QColor("#22D3EE"),
]
_RAINBOW_N = len(_RAINBOW_COLORS)

# 团队 leader 角色名：团队框内默认置顶
_LEADER_AGENT = "leader"

# 悬停渐层颜色常量（避免 paintEvent 中反复创建 QColor）
_HOVER_DARK_COLORS = (_QColor(99, 102, 241, 32), _QColor(139, 92, 246, 18))
_HOVER_LIGHT_COLORS = (_QColor(99, 102, 241, 22), _QColor(139, 92, 246, 12))

# 错误态红条颜色
_CACHED_ERROR_RED = _QColor(220, 50, 50)

# 报错红色流光渐层颜色常量（避免 paintEvent 中反复创建 5 个 QColor）
_SHIMMER_ERROR_COLORS = (
    _QColor(255, 255, 255, 0),
    _QColor(220, 80, 60, 80),
    _QColor(235, 110, 80, 140),
    _QColor(220, 80, 60, 80),
    _QColor(255, 255, 255, 0),
)

# 提问橙黄流光渐层颜色常量（避免 paintEvent 中反复创建 5 个 QColor）
_SHIMMER_QUESTION_COLORS = (
    _QColor(255, 255, 255, 0),
    _QColor(245, 170, 60, 80),
    _QColor(250, 200, 110, 140),
    _QColor(245, 170, 60, 80),
    _QColor(255, 255, 255, 0),
)

# 彩虹流光渐层缓存：key = 彩虹索引 → 5 段渐层色元组（主体色随相位循环）
_SHIMMER_RAINBOW_CACHE: dict = {}


def _shimmer_rainbow_colors(idx: int):
    """按彩虹索引生成 5 段流光渐层（透明→彩虹色→亮白→彩虹色→透明），带缓存

    流式流光随 _anim_phase 推进切换彩虹色，形成"彩色循环"的来回流光。
    """
    cached = _SHIMMER_RAINBOW_CACHE.get(idx)
    if cached is not None:
        return cached
    base = _RAINBOW_COLORS[idx]
    r, g, b = base.red(), base.green(), base.blue()
    colors = (
        _QColor(255, 255, 255, 0),
        _QColor(r, g, b, 55),
        _QColor(min(255, r + 60), min(255, g + 60), min(255, b + 60), 100),
        _QColor(r, g, b, 55),
        _QColor(255, 255, 255, 0),
    )
    _SHIMMER_RAINBOW_CACHE[idx] = colors
    return colors


# ── 绘制原语（模块级）──────────────────────────────────────────────
# [PERF] 这两个函数原先定义在 paintEvent 内部，每帧都要新建 2 个闭包对象
# （函数 + cell）。标签动画以 15fps 持续运行，流式期间每个可见 streaming tab
# 每帧一次 paint —— 闭包与画笔的重复创建是纯浪费。提为模块级函数后，
# 画笔与渐变对象也得以复用（GUI 单线程，跨 paint 复用 QPen/QGradient 安全）。
_SHIMMER_STOPS = (0.0, 0.3, 0.5, 0.7, 1.0)
_INDICATOR_PEN = _QPen()
_INDICATOR_PEN.setWidth(3)
_INDICATOR_PEN.setCapStyle(Qt.RoundCap)
_SHIMMER_GRAD = _QLinearGradient(0, 0, 0, 0)


def _draw_left_indicator(painter, round_rect, h: int, color) -> None:
    """左侧指示条：用 3px 粗笔沿圆角路径描边，clip 到左 5px 显示。

    沿圆角路径描边自然呈现贴合圆角的曲线（与 hover 背景同一路径）。
    """
    painter.save()
    painter.setClipRect(0, 0, 5, h)
    _INDICATOR_PEN.setColor(color)
    painter.setPen(_INDICATOR_PEN)
    # ⚠️ 必须显式清掉画刷：QPainter.drawPath 同时具备「描边 + 填充」两种语义，
    # 若调用方此前做过 setBrush(...)，这里会把整个圆角矩形再填充一遍，
    # 覆盖刚画好的背景与流光。当前背景绘制走 fillPath（不修改 painter 的
    # brush 属性），此行是防御性保险，成本可忽略。
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(round_rect)
    painter.restore()


def _draw_shimmer(painter, round_rect, w: int, phase: float, colors, is_resizing: bool = False) -> None:
    """整条标签内部的来回脉冲流光。

    sin 相位 → 光斑从 -20% 扫到 120% 再折回：内部来回移动的流光脉冲。
    colors 为 5 段渐层色（透明→主体→透明）；流式传彩虹色循环，报错传红色渐层。

    [PERF] 原实现用 ``setClipPath + fillRect(整个标签矩形)``：设置路径裁剪会
    让 Qt 走非矩形裁剪路径（生成裁剪 mask），且填充区域是外接矩形而非圆角内部。
    改为直接 ``fillPath(round_rect)``，省掉 clip 的 save/restore 与多余填充面积。
    """
    if is_resizing:
        return  # resize 期间跳过昂贵渐层
    sweep = _math.sin(_math.radians(phase))
    sweep_t = (sweep + 1.0) / 2.0  # 0.0 ~ 1.0
    # 光斑中心在标签上从 -20% 扫到 120%
    shimmer_center = sweep_t * (w + 0.4 * w) - 0.2 * w
    _SHIMMER_GRAD.setStart(shimmer_center - 80, 0)
    _SHIMMER_GRAD.setFinalStop(shimmer_center + 80, 0)
    for stop, color in zip(_SHIMMER_STOPS, colors):
        _SHIMMER_GRAD.setColorAt(stop, color)
    painter.fillPath(round_rect, _SHIMMER_GRAD)


def _vertical_more_icon() -> QIcon:
    """竖向「⋯」图标：把主题感知的「更多」图标旋转 90°

    icons/ 里只有横向三点（更多.svg），竖向版本通过 pixmap 旋转得到。代价是
    图标被烘焙成位图、不再随主题自动变色 —— 因此 _refresh_top_bar_style()
    （主题/字号刷新路径）里必须重新生成并 setIcon。
    """
    from PyQt5.QtCore import QSize as _QSize

    base = get_icon("更多")
    icon = QIcon()
    transform = QTransform().rotate(90)
    for size in (16, 20, 24, 32):
        pixmap = base.pixmap(_QSize(size, size))
        if pixmap.isNull():
            continue
        icon.addPixmap(pixmap.transformed(transform, Qt.SmoothTransformation))
    return base if icon.isNull() else icon


class _TabProjectIcon(QWidget):
    """标签页项目图标 — 与 _SquareAvatar（project_selector_card.py）一致的 DPI 感知方案

    直接 paintEvent 绘制纯色圆角矩形 + 白色缩写字母，
    Qt 自动处理 devicePixelRatio，无需中间 QPixmap / 手动 round(ceil) 物理像素。
    """

    def __init__(self, parent=None, size: int = 20):
        super().__init__(parent)
        self._size = size
        self._initials = ""
        self._color = QColor(128, 128, 128)
        self._fallback_pixmap: Optional[QPixmap] = None
        self.setFixedSize(size, size)

    def set_project(self, initials: str, color_rgba: str):
        """设置项目头像：缩写 + 颜色（rgba 字符串如 'rgba(33,139,255,255)'）"""
        self._initials = initials if initials else "?"
        self._color = self._parse_rgba(color_rgba)
        self._fallback_pixmap = None  # 清除 pixmap fallback
        self.update()

    def set_fallback_pixmap(self, pixmap):
        """设置通用 pixmap 兜底（非项目头像时使用）"""
        self._fallback_pixmap = pixmap
        self._initials = ""  # 清除 project 模式
        self.update()

    @staticmethod
    def _parse_rgba(rgba_str: str) -> QColor:
        """解析 'rgba(r,g,b,a)' 字符串为 QColor，与 _SquareAvatar 一致"""
        if rgba_str.startswith("#"):
            return QColor(rgba_str)
        try:
            import re

            m = re.match(r"rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(\d+))?\s*\)", rgba_str)
            if m:
                r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
                a = int(m.group(4)) if m.group(4) else 255
                return QColor(r, g, b, a)
        except Exception:
            pass
        return QColor(128, 128, 128)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = self.rect()

        # ── 项目头像模式：直接画圆角矩形 + 白字 ──
        if self._initials:
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._color)
            painter.drawRoundedRect(rect, 5, 5)

            painter.setPen(Qt.white)
            font = get_unified_font()
            font.setPixelSize(scale_font_size(self._size * 14 // 24))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, self._initials)
            return

        # ── 兜底：画 QPixmap ──
        pix = self._fallback_pixmap
        if pix is not None:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            if isinstance(pix, QPixmap):
                # source 用 logical 坐标：Qt 期望的逻辑坐标系
                dpr = pix.devicePixelRatio()
                if dpr > 1.0:
                    lw = pix.width() / dpr
                    lh = pix.height() / dpr
                    painter.drawPixmap(QRectF(rect), pix, QRectF(0, 0, lw, lh))
                else:
                    scaled = pix.scaled(rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    x = (rect.width() - scaled.width()) / 2
                    y = (rect.height() - scaled.height()) / 2
                    painter.drawPixmap(int(x), int(y), scaled)
            elif isinstance(pix, QIcon):
                p = pix.pixmap(rect.size().toSize())
                if p and not p.isNull():
                    painter.drawPixmap(rect, p, p.rect())


class TabItem(QFrame):
    """单个 Tab 项的 UI 组件"""

    closeRequested = pyqtSignal()

    def __init__(
        self, title: str, icon=None, parent=None, panel=None, project_initials: str = "", project_color: str = ""
    ):
        super().__init__(parent)
        self._title = title
        self._project_initials = project_initials
        self._project_color = project_color
        self._selected = False
        self._streaming = False
        self._stream_error = False
        self._question = False  # AI 提问等待用户回答（橙黄脉动）
        self._hovered = False  # 鼠标悬停态
        self._team_mode = False  # 团队模式：隐藏项目 icon（项目 icon 移到团队标题处）
        self._compact = False  # 紧凑模式（侧边栏折叠态）：仅图标 + 状态指示条
        self._indent = 0  # 树模式缩进（左侧内边距增量，像素）
        # 树模式专属：已激活（该会话占用一个后端）标记 + 关闭按钮常显
        self._open_marker = False
        self._close_persistent = False
        self._capsule_color = ""  # 胶囊颜色（紧凑态首字符图标用同色）
        self._compact_saved = None  # 紧凑态恢复现场（展开时逐控件配对还原）
        self._panel = panel  # TabPanel 引用，用于读取 _anim_phase
        # ── 关闭按钮二次确认（内联确认，参照 worktree_section）──
        self._confirming_close = False  # 关闭确认态（首次点击进入，二次点击真正关闭）
        self._close_timer = QTimer(self)  # 确认超时自动取消
        self._close_timer.setSingleShot(True)
        self._close_timer.setInterval(3000)
        self._close_timer.timeout.connect(self._cancel_close_confirm)
        # ── paintEvent 缓存：当尺寸未变时复用 QPainterPath ──
        self._cached_rect_key = (-1, -1)
        self._cached_round_rect = None
        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)

        # 图标尺寸：跟随系统字体缩放
        self._icon_size = scale_icon_size(20)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(6)

        # 图标 — _TabProjectIcon 直接 QPainter 绘制圆角矩形+文字，与 _SquareAvatar 一致
        self._icon_widget = _TabProjectIcon(self, size=self._icon_size)
        self._icon_widget.setToolTip(self._title)
        self._apply_project_to_icon()
        layout.addWidget(self._icon_widget)

        # ── 已激活标记（树模式）：一个会话占用一个后端，需可辨识 + 可关闭 ──
        self._active_dot = QLabel(self)
        self._active_dot.setFixedSize(6, 6)
        self._active_dot.setVisible(False)
        self._active_dot.setToolTip("已激活：占用一个后端")
        layout.addWidget(self._active_dot)

        # ── 团队角色胶囊（默认隐藏）──
        self._capsule_label = QLabel(self)
        self._capsule_label.setVisible(False)
        self._capsule_label.setFixedHeight(20)
        self._capsule_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        layout.addWidget(self._capsule_label)

        # 标题（使用 _ElidedLabel 自动省略，保证关闭按钮始终可见）
        self._title_label = _ElidedLabel(self._title, self)
        self._apply_title_style()
        layout.addWidget(self._title_label, 1)

        # 关闭按钮（与主标题栏一致的 FluentIcon.CLOSE，支持内联二次确认）
        self._close_btn = TransparentToolButton(self)
        self._close_btn.setIcon(FIF.CLOSE)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setVisible(False)
        self._close_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self._close_btn_orig_ss = self._close_btn.styleSheet()  # 保存 qfluentwidgets 全局样式，确认态恢复时还原
        self._close_btn.clicked.connect(self._on_close_btn_clicked)
        layout.addWidget(self._close_btn)

    def _apply_title_style(self):
        """应用标题样式：使用系统字体 + 缩放字号 + 当前主题色

        注意：单纯用 setStyleSheet 设置 font-family 在某些 Qt 场景下会被父级
        stylesheet 覆盖。这里同时调用 setFont，setFont 优先级最高，确保生效。

        每次主题或字体设置变更后都需调用，确保颜色和字体一致。
        """
        from app.utils.utils import get_unified_font

        # 直接通过 setFont 强制应用字体（避开 stylesheet 继承坑）
        self._title_label.setFont(get_unified_font(13))
        # 颜色随主题刷新（字体同一行写也能 setFont，但颜色走 stylesheet 更稳）
        self._title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(13)}"
        )

    def refresh_style(self):
        """主题 / 字体变更后刷新样式，重新调整图标尺寸与文字字号/颜色"""
        # 重新读取缩放后的图标尺寸
        new_size = scale_icon_size(20)
        if new_size != self._icon_size:
            self._icon_size = new_size
            self._icon_widget.setFixedSize(self._icon_size, self._icon_size)
            self._apply_project_to_icon()
        self._apply_title_style()
        # 「已激活」圆点用 INFO 色（主题感知，浅色/深色都可见）
        self._active_dot.setStyleSheet(f"background: {Colors.INFO}; border-radius: 3px; border: none;")
        # 紧凑态团队模式：字号缩放后幂等重刷首字符图标（补充点 3）
        if self._compact and self._team_mode:
            self._apply_compact_icon()
        self.update()

    def set_selected(self, selected: bool):
        self._selected = selected
        self.update()

    def set_streaming(self, streaming: bool, error: bool = False):
        self._streaming = streaming
        self._stream_error = error
        self.update()

    def set_question(self, question: bool):
        """设置"提问等待回答"态：仅点亮橙色脉动指示条"""
        self._question = question
        self.update()

    def set_title(self, title: str):
        self._title = title
        self._title_label.setText(title)
        self._icon_widget.setToolTip(title)

    def _apply_project_to_icon(self):
        """将项目信息交给 _TabProjectIcon 绘制"""
        if self._project_initials and self._project_color:
            self._icon_widget.set_project(self._project_initials, self._project_color)

    def set_project(self, initials: str, color_rgba: str):
        """设置项目头像（缩写+颜色）"""
        self._project_initials = initials
        self._project_color = color_rgba
        self._apply_project_to_icon()
        # 紧凑态非团队：保持图标显示（项目 icon 为折叠态唯一标识）
        if self._compact and not self._team_mode:
            self._icon_widget.setVisible(True)

    def set_icon(self, icon):
        """设置通用 icon（QPixmap/QIcon 兜底）"""
        self._project_initials = ""
        self._project_color = ""
        self._icon_widget.set_fallback_pixmap(icon)

    def set_indent(self, px: int):
        """树模式缩进：只调左侧内边距，其余三边保持不变

        背景仍铺满整行（paintEvent 用 self.rect()），缩进只影响内容起点，
        与树里其它行（工作树头 / 会话行）共用一套像素体系。
        """
        px = max(0, int(px))
        if self._indent == px:
            return
        self._indent = px
        lay = self.layout()
        if lay is not None:
            lay.setContentsMargins(8 + px, 4, 4, 4)

    def set_open_marker(self, on: bool):
        """树模式「已激活」标记：该对话页已占用一个后端

        紧凑态（46px 窄条）不显示，退出紧凑时由 set_compact 按标记恢复。
        """
        on = bool(on)
        if self._open_marker == on:
            return
        self._open_marker = on
        if not self._compact:
            self._active_dot.setVisible(on)

    def set_close_persistent(self, on: bool):
        """树模式：关闭按钮常显（已激活会话占着后端，必须一眼看到可关）

        列表模式下恒为 False（保持 hover 才显的既有行为）。
        """
        on = bool(on)
        if self._close_persistent == on:
            return
        self._close_persistent = on
        if not self._compact:
            self._close_btn.setVisible(on)

    def set_capsule(self, text: str, color: str = ""):
        """显示团队角色胶囊"""
        if not color:
            # 从 agent 名 hash 生成稳定色
            h = abs(hash(text)) % 360
            color = f"hsl({h}, 65%, 50%)"
        self._capsule_color = color  # 紧凑态首字符图标用同色（矩阵 B4）
        self._capsule_label.setText(text)
        self._capsule_label.setStyleSheet(f"""
            QLabel {{
                background: {color};
                color: white;
                border-radius: 4px;
                padding: 1px 6px;
                font-size: 11px;
                font-weight: bold;
            }}
        """)
        self._capsule_label.setVisible(True)
        # 紧凑态：无条件隐藏胶囊（P1 修复——set_capsule 先于 set_team_mode(True)
        # 执行时 _team_mode 仍为 False，旧条件会漏隐藏导致折叠态显示胶囊文字）；
        # 团队模式再幂等重刷首字符图标，非团队模式不刷（无胶囊语义）。
        if self._compact:
            if self._team_mode:
                self._apply_compact_icon()
            self._capsule_label.setVisible(False)

    def set_team_mode(self, team_mode: bool):
        """设置团队模式：隐藏项目 icon（项目 icon 移到团队标题处显示）

        True：TabItem 只显示角色胶囊 + 标题 + 关闭按钮（项目 icon 隐藏）；
        False：恢复显示项目 icon（非团队模式回归不变）。
        与胶囊共存：团队模式下胶囊照常显示，二者互不干扰。
        紧凑态（折叠）：仅切换图标内容——团队模式切角色首字符、非团队切项目
        icon，不改变文字类控件可见性（矩阵 D3）。
        """
        if self._team_mode == team_mode:
            return
        self._team_mode = team_mode
        if self._compact:
            if team_mode:
                self._apply_compact_icon()
            else:
                self._apply_project_to_icon()
                self._icon_widget.setVisible(True)
        else:
            self._icon_widget.setVisible(not team_mode)
        self.update()

    def _apply_compact_icon(self):
        """紧凑态团队模式：用角色胶囊首字符 + 胶囊色绘制图标（折叠态成员可区分）

        胶囊为空（异常时序）时回退标题首字，避免裸 "?" 无法区分成员。
        """
        text = (self._capsule_label.text() or "").strip()
        if not text:
            text = (self._title or "").strip()
        ch = text[0] if text else "?"
        color = self._capsule_color or ""
        if not color and text:
            h = abs(hash(text)) % 360
            color = f"hsl({h}, 65%, 50%)"
        self._icon_widget.set_project(ch, color)
        self._icon_widget.setVisible(True)

    def set_compact(self, compact: bool):
        """切换紧凑模式（侧边栏折叠态）：仅保留图标 + 状态指示条

        compact=True：保存恢复现场（标题/胶囊/关闭/图标可见性 + margins），
            隐藏 title/capsule/close，margin 收紧为 (4,4,4,4)；团队模式用角色
            首字符图标（矩阵 B4），非团队保留项目 icon。
        compact=False：按保存的现场逐控件配对恢复（矩阵 D1 对称性），
            含团队模式 icon 恢复隐藏、margin 还原。
        幂等：相同状态重复调用直接返回。
        """
        if self._compact == compact:
            return
        self._compact = compact
        if compact:
            # 进入紧凑态：关闭确认态无意义（按钮被隐藏），直接取消恢复
            self._cancel_close_confirm()
            # 保存恢复现场（用 isHidden 逆：显式隐藏状态，与父链显示无关）
            self._compact_saved = {
                "icon_visible": not self._icon_widget.isHidden(),
                "title_visible": not self._title_label.isHidden(),
                "capsule_visible": not self._capsule_label.isHidden(),
                "close_visible": not self._close_btn.isHidden(),
                "dot_visible": not self._active_dot.isHidden(),
                "margins": self.layout().getContentsMargins(),
            }
            # 隐藏文字类控件，仅留图标
            self._title_label.setVisible(False)
            self._capsule_label.setVisible(False)
            self._close_btn.setVisible(False)
            self._active_dot.setVisible(False)
            self.layout().setContentsMargins(4, 4, 4, 4)
            if self._team_mode:
                self._apply_compact_icon()
            else:
                self._icon_widget.setVisible(True)
        else:
            saved = self._compact_saved or {}
            # 团队模式：icon 恢复隐藏；非团队：恢复原可见性
            if self._team_mode:
                self._icon_widget.setVisible(False)
            else:
                self._icon_widget.setVisible(bool(saved.get("icon_visible", True)))
            # 还原图标数据（compact 期间可能被 set_project/set_icon 更新）
            self._apply_project_to_icon()
            # 展开态按当前语义恢复（非折叠前现场）：折叠期间可能加入团队/设置胶囊，
            # 旧现场会漏显示——标题恒显、胶囊按团队模式+文本、close 恒隐（hover 再显）
            self._title_label.setVisible(True)
            has_capsule_text = bool((self._capsule_label.text() or "").strip())
            self._capsule_label.setVisible(self._team_mode and has_capsule_text)
            # 树模式语义优先于折叠前现场：常显关闭钮 / 已激活标记按当前标记恢复
            self._close_btn.setVisible(bool(self._close_persistent))
            self._active_dot.setVisible(bool(self._open_marker))
            if "margins" in saved:
                self.layout().setContentsMargins(*saved["margins"])
            self._compact_saved = None
        self.update()

    def clear_capsule(self):
        """隐藏团队角色胶囊"""
        self._capsule_label.setVisible(False)
        self._capsule_label.setText("")

    def _on_close_btn_clicked(self):
        """关闭按钮点击：内联二次确认（参照 worktree_section 删除按钮）。

        仅在存在"进行中状态"（流式对话 _streaming / 提问等待 _question）时启用
        二次确认，防止误触丢掉正在进行的对话；对话已结束（无状态）直接关闭不打扰。

        确认流程：首次点击 → 按钮变为红色"确认关闭"，3 秒内再次点击才真正
        emit closeRequested；移出 Tab / 3 秒超时自动取消。
        """
        # 对话已结束：直接关闭，不做二次确认
        if not self._streaming and not self._question:
            self.closeRequested.emit()
            return

        if not self._confirming_close:
            # 首次点击（对话进行中）：进入确认态
            self._confirming_close = True
            self._close_btn.setIcon(QIcon())  # 清除图标，文字占位
            self._close_btn.setText("确认关闭")
            self._close_btn.setFixedSize(64, 20)
            # ⚠️ 必须显式透明背景 + TextOnly：局部 stylesheet 会覆盖 qfluentwidgets
            # 全局样式（原背景半透明白），若只设 color 则 Qt 回退默认深色底 → 全黑；
            # 且 ToolButton 默认 IconOnly 不绘制 setText 文字。
            self._close_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
            self._close_btn.setStyleSheet(
                "QToolButton { background: transparent; border: none; "
                f"color: #f85149; font-weight: 600; {get_font_family_css()} {font_size_css(11)} "
                "}"
            )
            self._close_btn.setToolTip("正在对话，再次点击确认关闭，3秒后自动取消")
            self._close_timer.start()
        else:
            # 二次点击：确认关闭
            if self._close_timer.isActive():
                self._close_timer.stop()
            self._close_btn.setEnabled(False)  # 防重入：确认后禁用，避免连点
            self._confirming_close = False
            self.closeRequested.emit()

    def _cancel_close_confirm(self):
        """取消关闭确认态，恢复普通关闭按钮样式（移出按钮 / 超时触发）"""
        if not self._confirming_close and not self._close_btn.isEnabled():
            return
        self._confirming_close = False
        if self._close_timer.isActive():
            self._close_timer.stop()
        self._close_btn.setEnabled(True)
        self._close_btn.setIcon(FIF.CLOSE)
        self._close_btn.setText("")
        self._close_btn.setFixedSize(20, 20)
        # 还原 qfluentwidgets 全局样式（不能 setStyleSheet("")，否则连全局样式一起清掉）
        self._close_btn.setStyleSheet(getattr(self, "_close_btn_orig_ss", ""))
        self._close_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self._close_btn.setToolTip("")

    def enterEvent(self, event):
        # 紧凑态守卫：折叠态不弹关闭按钮，避免撑破小容器（矩阵 C3）
        if not self._compact:
            self._close_btn.setVisible(True)
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        # 移出 Tab 时若处于关闭确认态：取消确认（防止悬停残留误删）
        self._cancel_close_confirm()
        if not self._selected and not self._compact:
            # 树模式常显关闭钮：移出后仍保留（已激活会话占着后端）
            self._close_btn.setVisible(bool(self._close_persistent))
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()

        # ── 构建统一圆角路径（与 hover 一致，8px 圆角 + 2px 内边距） ──
        # ★ 缓存：尺寸未变时复用 QPainterPath（用 tuple key 避免 mutable 污染）
        _key = (w, h)
        if _key != self._cached_rect_key:
            self._cached_round_rect = _QPainterPath()
            self._cached_round_rect.addRoundedRect(2, 2, w - 4, h - 4, 8, 8)
            self._cached_rect_key = _key
        _round_rect = self._cached_round_rect

        # ── 选中背景（圆角矩形，视觉与 hover 统一） ──
        if self._selected:
            painter.fillPath(_round_rect, _CACHED_SELECTED_BG)

        # ── 悬停：圆角渐变背景（使用缓存颜色常量） ──
        if self._hovered and not self._selected:
            hover_grad = _QLinearGradient(0, 0, w, 0)
            if isDarkTheme():
                hover_grad.setColorAt(0.0, _HOVER_DARK_COLORS[0])
                hover_grad.setColorAt(1.0, _HOVER_DARK_COLORS[1])
            else:
                hover_grad.setColorAt(0.0, _HOVER_LIGHT_COLORS[0])
                hover_grad.setColorAt(1.0, _HOVER_LIGHT_COLORS[1])
            painter.fillPath(_round_rect, hover_grad)

        # ── 流式/错误状态 ──
        _is_resizing = bool(self._panel and self._panel._is_resizing)
        if self._streaming or self._stream_error:
            # 共用扫描相位：流式彩虹循环 / 报错红色循环
            phase = self._panel._anim_phase if self._panel else 0
            # 左侧指示条只在选中时显示；未选中的状态仅保留内部流光脉冲
            if self._stream_error:
                # 报错：内部红色流光脉冲（选中时叠加红色指示条）
                _err_color = _QColor(_CACHED_ERROR_RED)
                if self._selected:
                    _draw_left_indicator(painter, _round_rect, h, _err_color)
                _draw_shimmer(painter, _round_rect, w, phase, _SHIMMER_ERROR_COLORS, _is_resizing)
            else:
                # 流式：内部彩虹流光（选中时叠加彩色循环指示条，相位驱动颜色循环）
                idx = int((phase / 360) * _RAINBOW_N) % _RAINBOW_N
                if self._selected:
                    _draw_left_indicator(painter, _round_rect, h, _RAINBOW_COLORS[idx])
                _draw_shimmer(painter, _round_rect, w, phase, _shimmer_rainbow_colors(idx), _is_resizing)
        elif self._question:
            # AI 提问等待回答：橙黄 #F59E0B 慢呼吸脉动（1.2s 一周期）
            phase = self._panel._question_phase if self._panel else 0
            # 内部橙黄流光脉冲（选中时叠加橙黄指示条，与流式同款流光动效）
            if not self._selected:
                _draw_shimmer(painter, _round_rect, w, phase, _SHIMMER_QUESTION_COLORS, _is_resizing)
            else:
                # resize 期间跳过 sin 计算取固定亮度
                if _is_resizing:
                    alpha = 150
                else:
                    # 动画帧速 +8°/帧 ≈ 3s 一周期；亮度在 ~80~220 间脉动
                    alpha = int(150 + _math.sin(_math.radians(phase)) * 70)
                _question_color = _QColor(245, 158, 11)
                _question_color.setAlpha(max(0, min(255, alpha)))
                _draw_left_indicator(painter, _round_rect, h, _question_color)
                _draw_shimmer(painter, _round_rect, w, phase, _SHIMMER_QUESTION_COLORS, _is_resizing)
        elif self._selected:
            # 左侧选中指示条（贴合圆角曲线）
            _draw_left_indicator(painter, _round_rect, h, _CACHED_INFO)

        super().paintEvent(event)


# ── 插入方位菜单图标：2x2 方块，黑=卡片显示位置，白=空白区域 ──
# 深色主题下黑块融入菜单背景，故黑块带浅灰描边保证轮廓可辨；
# 白块带同色描边保证浅色主题下同样清晰。
_POSITION_CELLS = {
    # (col, row): 方块左上角 (x, y)，16x16 viewBox，每格 7x7、间距 1
    (0, 0): (1, 1),
    (1, 0): (8, 1),
    (0, 1): (1, 8),
    (1, 1): (8, 8),
}
_POSITION_BLACK = "#151515"
_POSITION_WHITE = "#f0f0f0"
_POSITION_STROKE = "#9a9a9a"
_POSITION_ICON_CACHE: dict = {}


def _make_position_icon(black_cells) -> QIcon:
    """生成 2x2 方块方位图标（黑=显示位置，白=空白），带缓存"""
    key = frozenset(black_cells)
    cached = _POSITION_ICON_CACHE.get(key)
    if cached is not None:
        return cached
    parts = []
    for (cx, cy), (x, y) in _POSITION_CELLS.items():
        fill = _POSITION_BLACK if (cx, cy) in key else _POSITION_WHITE
        parts.append(
            f'<rect x="{x}" y="{y}" width="7" height="7" rx="1.5" '
            f'fill="{fill}" stroke="{_POSITION_STROKE}" stroke-width="0.6"/>'
        )
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">{"".join(parts)}</svg>'
    # 渲染 32px 物理像素后由 QIcon 按需缩放，HiDPI 屏上保持清晰。
    # 注意：不能 setDevicePixelRatio —— QSvgRenderer 对带 DPR 的 QPixmap
    # 渲染异常（只渲染首个元素），实测 32px 无 DPR 正常。
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    renderer = QSvgRenderer(svg.encode("utf-8"))
    painter = QPainter(pm)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    icon = QIcon(pm)
    _POSITION_ICON_CACHE[key] = icon
    return icon


class _RotatableArrow(QWidget):
    """可旋转 chevron 折叠指示箭头：0° 指向右(折叠)，90° 指向下(展开)。

    用 QPainter 实时绘制（颜色取自当前主题 Colors.TEXT_MUTED），配合
    QPropertyAnimation 做 160ms 缓动旋转，替换原字符箭头 ▶/▼ 的生硬切换。
    """

    def __init__(self, parent=None, size: int = 14):
        super().__init__(parent)
        self._size = size
        self._angle = 0.0
        self.setFixedSize(size, size)
        self._anim: Optional[QPropertyAnimation] = None

    def _get_angle(self) -> float:
        return self._angle

    def _set_angle(self, a: float):
        self._angle = a
        self.update()

    angle = pyqtProperty(float, _get_angle, _set_angle)

    def set_expanded(self, expanded: bool, animate: bool = True):
        target = 90.0 if expanded else 0.0
        if not animate or self._angle == target:
            self._set_angle(target)
            return
        if self._anim is not None:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"angle")
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.setStartValue(self._angle)
        self._anim.setEndValue(target)
        self._anim.start()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(self._angle)
        pen = QPen(QColor(Colors.TEXT_MUTED))
        pen.setWidthF(1.5)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        half = self._size / 2
        dx, dy = half * 0.34, half * 0.46
        p.drawLine(int(-dx), int(-dy), 0, 0)
        p.drawLine(0, 0, int(-dx), int(dy))
        p.end()


class UIPluginRow(QFrame):
    """TabPanel 中的 UI 插件行，固定图标和文本的相对位置。"""

    clicked = pyqtSignal()
    positionRequested = pyqtSignal(str, str)  # (card_id, container) 右键选择插入方位

    def __init__(
        self,
        title: str,
        icon: Optional[QIcon] = None,
        parent=None,
        plugin_name: str = "",
        card_id: str = "",
        enable_position_menu: bool = True,
    ):
        super().__init__(parent)
        self._title = title  # 存储标题，图标 tooltip 使用（侧边栏收起时只剩图标）
        self._plugin_name = plugin_name  # 存储插件名，主题刷新时重新获取图标
        self._card_id = card_id  # 存储卡片 ID，右键菜单定位用
        self._enable_position_menu = enable_position_menu  # 独立 sidebar 项无卡片方位概念，禁用右键菜单
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(scale_icon_size(16), scale_icon_size(16))
        # 图标 tooltip：收起态只有图标时悬浮可见插件名（与 TabItem 一致）
        self._icon_label.setToolTip(title)
        # 使用与 TabItem 同款的 _ElidedLabel，收起时自动省略文本
        self._title_label = _ElidedLabel(title, self)
        self._compact = False  # 紧凑模式（侧边栏折叠态：只保留图标）
        self._compact_saved: Optional[dict] = None  # 紧凑态恢复现场

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)
        layout.addWidget(self._icon_label)
        layout.addWidget(self._title_label, 1)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("uiPluginRow")
        self.set_icon(icon)
        # 右键菜单：选择卡片插入方位（仅内存生效，不持久化）
        if enable_position_menu:
            self.setContextMenuPolicy(Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self._show_position_menu)
        # 初始应用字体和颜色，避免在 refresh_style() 被调用前显示默认 Qt 字体
        from app.utils.utils import get_unified_font

        self._title_label.setFont(get_unified_font(12))
        self._title_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}")
        self._icon_label.setStyleSheet("background: transparent;")

    def set_icon(self, icon: Optional[QIcon]):
        size = scale_icon_size(16)
        self._icon_label.setFixedSize(size, size)
        if icon is not None:
            self._icon_label.setPixmap(icon.pixmap(size, size))
        else:
            self._icon_label.clear()

    def set_compact(self, compact: bool):
        """切换紧凑模式（侧边栏折叠态）：仅保留图标

        compact=True：保存恢复现场（标题可见性 + margins），隐藏标题，
            边距收紧为 (4,4,4,4)（折叠态窄条下只显示 icon）；
        compact=False：按保存的现场恢复。
        幂等：相同状态重复调用直接返回。
        """
        if self._compact == compact:
            return
        self._compact = compact
        if compact:
            self._compact_saved = {
                "title_visible": not self._title_label.isHidden(),
                "margins": self.layout().getContentsMargins(),
            }
            self._title_label.setVisible(False)
            self.layout().setContentsMargins(4, 4, 4, 4)
        else:
            saved = self._compact_saved or {}
            self._title_label.setVisible(bool(saved.get("title_visible", True)))
            if "margins" in saved:
                self.layout().setContentsMargins(*saved["margins"])
            self._compact_saved = None
        self.update()

    def refresh_style(self):
        """刷新主题样式：字体 + 颜色 + 主题相关图标"""
        from app.utils.utils import get_unified_font

        # 应用系统字体（避免 stylesheet 继承问题）
        self._title_label.setFont(get_unified_font(12))
        self._title_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}")
        self._icon_label.setStyleSheet("background: transparent;")
        self.setStyleSheet(f"""
            #uiPluginRow {{
                background: transparent;
                border-radius: 5px;
            }}
            #uiPluginRow:hover {{
                background: {Colors.HOVER_BG};
            }}
        """)

        # 重新获取主题相关图标（浅/深色主题图标不同）
        if self._plugin_name:
            try:
                from app.plugins.managers.plugin_manager import PluginManager

                pm = PluginManager.get_instance()
                plugin = pm.get_plugin(self._plugin_name)
                icon_config = getattr(plugin, "icon_config", None) if plugin else None
                icon_path = icon_config.get("dark" if isDarkTheme() else "light") if icon_config else None
                if icon_path:
                    self.set_icon(QIcon(str(icon_path)))
            except Exception:
                pass

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def _build_position_menu(self) -> QMenu:
        """构建插入方位菜单：下/左/右/替换（「上」与 full 行为重复，不提供）"""
        menu = QMenu(self)
        # 样式与 TabPanel.contextMenuEvent 保持一致（运行时插值，跟随主题色）
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
                {get_font_family_css()} {font_size_css(13)}
            }}
            QMenu::item:selected {{
                background: {Colors.HOVER_BG};
            }}
        """)
        # 图标：2x2 方块，黑=卡片显示位置（下/左/右为半黑，替换=全黑）
        positions = (
            ("下", "bottom", {(0, 1), (1, 1)}),
            ("左", "left", {(0, 0), (0, 1)}),
            ("右", "right", {(1, 0), (1, 1)}),
            ("替换", "full", {(0, 0), (1, 0), (0, 1), (1, 1)}),
        )
        for label, container, black_cells in positions:
            action = menu.addAction(_make_position_icon(black_cells), label)
            action.triggered.connect(lambda checked=False, c=container: self.positionRequested.emit(self._card_id, c))
        return menu

    def _show_position_menu(self, pos):
        """显示插入方位菜单（仅内存生效，不持久化）"""
        self._build_position_menu().exec_(self.mapToGlobal(pos))


def _sort_plugin_entries(entries: list) -> list:
    """插件条目统一排序：priority 降序 → title 字母序兜底（稳定）

    regression：refresh_ui_plugins 曾直接 `sort(key=title.lower())`
    导致 SidebarItemInfo.priority 失效（注册优先级被字母序覆盖）。
    """
    return sorted(entries, key=lambda e: (-e[4], e[2].lower()))


class TabPanel(QWidget):
    """左侧 Tab 列表面板"""

    tabSelected = pyqtSignal(int)  # 选中 Tab 索引
    tabCloseRequested = pyqtSignal(int)  # 关闭 Tab 索引
    tabBranchRequested = pyqtSignal(int)  # 分支窗口 Tab 索引
    newTabRequested = pyqtSignal()  # 新建 Tab
    tabsReordered = pyqtSignal(list)  # 拖拽排序后新顺序（索引列表）
    sidebarToggled = pyqtSignal(bool)  # 侧边栏收起(true)/展开(false)
    teamCloseRequested = pyqtSignal(str)  # 关闭整个团队（传 team_id）
    teamAddMemberRequested = pyqtSignal(str)  # 团队框"快速新建成员"按钮（传 team_id，可重复角色）
    teamNewTaskRequested = pyqtSignal(str)  # 团队框"新建任务"按钮（传 team_id：全员新会话 + 新 run_id）
    newSessionInWorkspaceRequested = pyqtSignal(str, str)  # 工作区树：在 项目 + 工作树 下新建对话页
    openSessionRecordRequested = pyqtSignal(object)  # 工作区树：打开历史会话（轻量会话记录 dict）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[TabItem] = []
        self._active_index: int = -1
        # ── 团队分组：_items 保持扁平索引；_item_team 映射 _items[i] → team_id（"" 表示独立）
        #    _team_groups 缓存 team_id → QFrame 容器（避免反复创建）
        self._item_team: Dict[int, str] = {}
        self._team_groups: Dict[str, "QFrame"] = {}
        self._plugin_infos: list[tuple[str, str, str, str, int]] = []  # (kind, key, title, plugin_name, priority)
        self._system_plugin_layout: Optional[QVBoxLayout] = None
        self._system_plugin_buttons: list[UIPluginRow] = []
        self._custom_plugin_layout: Optional[QVBoxLayout] = None
        self._custom_plugin_buttons: list[UIPluginRow] = []
        self._custom_plugin_saved_state: Optional[dict] = None  # 折叠态保存卡片 scroll 现场
        self._gitee_account_row: Optional[GiteeAccountRow] = None
        self._anim_phase: float = 0.0  # 彩虹动画相位
        self._question_phase: float = 0.0  # question 脉动相位（独立，避免与彩虹冲突）
        self._anim_timer: Optional[QTimer] = None  # 有 tab 流式/question 时启动
        self._streaming_count: int = 0  # 当前流式 tab 计数
        self._error_count: int = 0  # 当前报错 tab 计数（报错红流同样需要动画驱动）
        self._question_count: int = 0  # 当前 question 状态 tab 计数
        self._is_resizing: bool = False  # resize 活跃态，用于节流动画/绘制
        self._collapsed: bool = False  # 侧边栏收起状态
        # 挤压折叠标记：非用户主动（窗口 resize / 覆盖层 relayout 压缩）导致
        # 自动折叠。用于空间恢复后管理器自动展开（区别于按钮手动折叠 / 手动
        # 拖拽把手折叠——手动折叠不自动展开）。
        self._collapsed_by_squeeze: bool = False
        self._collapsed_min_width: int = 46  # 收起时的最小宽度(仅容纳图标)
        self._auto_collapse_width: int = 100  # 展开态拖窄到该宽度(panel px)时自动折叠
        self._animating: bool = False  # 侧边栏宽度动画进行中（抑制 resizeEvent 自动展开/折叠）
        # 窗口 resize / relayout 过渡期抑制自动折叠：几何瞬变（_force_relayout
        # 重算、最大化/还原）会把左面板瞬时压到折叠阈值以下，若 resizeEvent
        # 据此折叠会误判为"用户拖窄"。由 TabManagerWindow 在 resize 周期开始
        # 时置 True、几何收拢后置 False，之后改由 _evaluate_squeeze_collapse
        # 按稳定后的最终宽度统一判定。
        self._auto_collapse_suppressed: bool = False
        # ── 对话页显示模式（列表 / 工作区树）──
        self._mode: str = PANEL_MODE_LIST
        self._mode_popup = None  # 模式选择悬浮框（Qt.Popup，二次点击收起）
        self._tree_scroll: Optional[QScrollArea] = None
        self._tree_widget: Optional[WorkspaceTree] = None
        self._tree_snapshot = None  # 树节点签名，用于跳过无变化的重建
        self._setup_ui()
        # 注册主题刷新回调：主题/字体变更后刷新所有 Tab 项样式
        theme_manager.register_refresh_target(self)

    _SEPARATOR_STYLE = f"""
        QFrame {{
            background: {Colors.DIVIDER_COLOR};
            border: none;
            min-height: 1px;
            max-height: 1px;
        }}
    """

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 顶部品牌区已移除 ──
        # 原「DriFox + 版本号 + 侧栏折叠按钮 + 分隔线」整块上移到窗口标题栏
        # （CustomTitleBar 左区）。TabPanel 首行直接是系统 UI 插件区，
        # 侧栏折叠改由标题栏按钮驱动（sidebar_toggle_requested → _toggle_sidebar）。

        # ── 系统 UI 插件（常驻显示，无滚动） ──
        self._system_plugin_section = QWidget(self)
        system_plugin_layout = QVBoxLayout(self._system_plugin_section)
        system_plugin_layout.setContentsMargins(6, 0, 6, 4)
        system_plugin_layout.setSpacing(2)
        self._system_plugin_layout = system_plugin_layout
        self._system_plugin_section.setStyleSheet("background: transparent;")
        self._system_plugin_section.setVisible(False)
        layout.addWidget(self._system_plugin_section)

        # ── 自定义 UI 插件：卡片式分组（折叠头 + 滚动列表，对齐团队分组框）──
        self._custom_plugin_card = QFrame(self)
        self._custom_plugin_card.setObjectName("customPluginCard")
        card_layout = QVBoxLayout(self._custom_plugin_card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        self._custom_plugin_card.setVisible(False)
        layout.addWidget(self._custom_plugin_card)

        # 折叠头（卡片内顶部，点击切换展开/折叠）
        self._custom_plugin_header = QWidget(self._custom_plugin_card)
        self._custom_plugin_header.setObjectName("customPluginHeader")
        self._custom_plugin_header.setCursor(Qt.PointingHandCursor)
        custom_header_layout = QHBoxLayout(self._custom_plugin_header)
        custom_header_layout.setContentsMargins(6, 5, 6, 5)
        custom_header_layout.setSpacing(6)
        self._custom_plugin_arrow = _RotatableArrow(self._custom_plugin_header, size=14)
        custom_header_layout.addWidget(self._custom_plugin_arrow)
        self._custom_plugin_label = QLabel("自定义插件", self._custom_plugin_header)
        self._custom_plugin_label.setObjectName("customPluginTitle")
        custom_header_layout.addWidget(self._custom_plugin_label, 1)
        self._custom_plugin_badge = QLabel("0", self._custom_plugin_header)
        self._custom_plugin_badge.setObjectName("customPluginBadge")
        custom_header_layout.addWidget(self._custom_plugin_badge)
        self._custom_plugin_header.mousePressEvent = lambda ev: self._on_custom_plugin_toggle()
        card_layout.addWidget(self._custom_plugin_header)

        # 滚动列表（卡片内，默认折叠）
        self._custom_plugin_scroll = QScrollArea(self._custom_plugin_card)
        self._custom_plugin_scroll.setWidgetResizable(True)
        self._custom_plugin_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._custom_plugin_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._custom_plugin_scroll.setFrameShape(QFrame.NoFrame)
        self._custom_plugin_scroll.setMaximumHeight(200)
        self._custom_plugin_scroll.setStyleSheet(
            f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            {get_unified_scrollbar_style(4)}
            """
        )
        self._custom_plugin_scroll.viewport().setStyleSheet("background: transparent;")
        self._custom_plugin_section = QWidget(self._custom_plugin_scroll)
        custom_plugin_layout = QVBoxLayout(self._custom_plugin_section)
        custom_plugin_layout.setContentsMargins(2, 2, 2, 2)
        custom_plugin_layout.setSpacing(2)
        self._custom_plugin_layout = custom_plugin_layout
        self._custom_plugin_section.setStyleSheet("background: transparent;")
        self._custom_plugin_scroll.setWidget(self._custom_plugin_section)
        self._custom_plugin_scroll.setVisible(False)
        card_layout.addWidget(self._custom_plugin_scroll)

        self._apply_custom_card_style()

        # ── 分隔线：UI 插件区域 ↔ 新建标签页 ──
        self._plugin_separator_2 = QFrame(self)
        self._plugin_separator_2.setFrameShape(QFrame.HLine)
        self._plugin_separator_2.setStyleSheet(
            f"""
            QFrame {{
                background: {Colors.DIVIDER_COLOR};
                border: none;
                min-height: 1px;
                max-height: 1px;
                margin-top: 4px;
                margin-bottom: 2px;
            }}
            """
        )
        self._plugin_separator_2.setVisible(False)
        layout.addWidget(self._plugin_separator_2)

        # ── 顶部：左「对话页」标题 + 右 分支/新建 纯图标按钮 ──
        # 布局：标题左对齐占满剩余空间（stretch=1），两个 24px 图标按钮靠右。
        # 收起态隐藏标题与分支按钮，只保留新建（46px 窄条容不下两个按钮）。
        self._top_bar = QWidget(self)
        top_layout = QHBoxLayout(self._top_bar)
        top_layout.setContentsMargins(8, 4, 4, 2)
        top_layout.setSpacing(0)

        self._sessions_label = QLabel("对话页", self._top_bar)
        self._sessions_label.setObjectName("sessionsLabel")
        top_layout.addWidget(self._sessions_label, 1)

        self._branch_btn = TransparentToolButton(self._top_bar)
        self._branch_btn.setIcon(get_icon("分支"))
        self._branch_btn.setIconSize(QSize(scale_icon_size(14), scale_icon_size(14)))
        self._branch_btn.setFixedSize(24, 24)
        self._branch_btn.setCursor(Qt.PointingHandCursor)
        self._branch_btn.setToolTip("从当前标签页分支")
        self._branch_btn.clicked.connect(self._on_branch_clicked)
        top_layout.addWidget(self._branch_btn)

        self._new_btn = TransparentToolButton(self._top_bar)
        self._new_btn.setIcon(FIF.ADD)
        self._new_btn.setIconSize(QSize(scale_icon_size(14), scale_icon_size(14)))
        self._new_btn.setFixedSize(24, 24)
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setToolTip("新建空白标签页")
        self._new_btn.clicked.connect(self.newTabRequested.emit)
        top_layout.addWidget(self._new_btn)

        # 模式切换：竖向「⋯」→ 悬浮框选择「列表模式 / 工作区树模式」
        self._mode_btn = TransparentToolButton(self._top_bar)
        self._mode_btn.setIcon(_vertical_more_icon())
        self._mode_btn.setIconSize(QSize(scale_icon_size(14), scale_icon_size(14)))
        self._mode_btn.setFixedSize(24, 24)
        self._mode_btn.setCursor(Qt.PointingHandCursor)
        self._mode_btn.setToolTip("切换对话页显示模式")
        self._mode_btn.clicked.connect(self._on_mode_btn_clicked)
        top_layout.addWidget(self._mode_btn)

        layout.addWidget(self._top_bar)

        # ── 中间：Tab 列表 ──
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setFrameShape(QFrame.NoFrame)
        # QScrollArea viewport 在 Windows 上默认可能为白色，需显式透明；
        # 滚动条样式与项目其他列表保持一致（get_unified_scrollbar_style）
        self._scroll_area.setStyleSheet(
            f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            {get_unified_scrollbar_style(6)}
            """
        )
        self._scroll_area.viewport().setStyleSheet("background: transparent;")

        self._list_widget = QWidget()
        self._list_widget.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(2, 0, 2, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch()
        self._scroll_area.setWidget(self._list_widget)
        layout.addWidget(self._scroll_area, 1)

        # ── 中间（树模式）：工作区树 项目 → 工作树 → 会话 ──
        # 与列表区并列，靠 setVisible 切换。TabItem / 团队框不需要手动搬运：
        # QLayout.addWidget 会自动 reparent，两个重建函数各摆各的容器。
        self._tree_scroll = QScrollArea(self)
        self._tree_scroll.setWidgetResizable(True)
        self._tree_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tree_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._tree_scroll.setFrameShape(QFrame.NoFrame)
        self._tree_scroll.setStyleSheet(
            f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            {get_unified_scrollbar_style(6)}
            """
        )
        self._tree_scroll.viewport().setStyleSheet("background: transparent;")
        self._tree_widget = WorkspaceTree(self._tree_scroll)
        self._tree_widget.setStyleSheet("background: transparent;")
        self._tree_widget.newSessionRequested.connect(self.newSessionInWorkspaceRequested)
        self._tree_widget.openSessionRequested.connect(self.openSessionRecordRequested)
        self._tree_widget.expansionChanged.connect(self._on_tree_expansion_changed)
        self._tree_scroll.setWidget(self._tree_widget)
        self._tree_scroll.setVisible(False)
        layout.addWidget(self._tree_scroll, 1)

        # ── 分隔线 ──
        self._separator = QFrame(self)
        self._separator.setFrameShape(QFrame.HLine)
        self._separator.setStyleSheet(self._SEPARATOR_STYLE)
        layout.addWidget(self._separator)

        # ── 底部：Gitee 账户（头像/名称右侧为 ⚙ 设置按钮） ──
        self._gitee_account_row = GiteeAccountRow(self)
        layout.addWidget(self._gitee_account_row)

        # 顶部行样式（标题颜色/字体、图标尺寸）首帧应用
        self._refresh_top_bar_style()

        # ── 恢复上次选择的显示模式与树折叠态 ──
        try:
            cfg = Settings.get_instance()
            saved_mode = cfg.tab_panel_mode.value
            saved_expansion = cfg.workspace_tree_expansion.value
        except Exception:
            saved_mode, saved_expansion = PANEL_MODE_LIST, {}
        self._mode = PANEL_MODE_TREE if saved_mode == PANEL_MODE_TREE else PANEL_MODE_LIST
        if self._tree_widget is not None and isinstance(saved_expansion, dict):
            self._tree_widget.set_expansion_state(saved_expansion)
        # ⚠️ 首帧只切可见性，内容延后一轮事件循环再构建：
        # TabPanel 是在 TabManagerWindow.__init__ 里 new 出来的，此刻宿主的
        # _tab_panel 还没赋值，而树要去问 host.get_current_window()（内部读
        # self._tab_panel.active_index）→ 直接 AttributeError 崩在启动路径上。
        self._apply_mode_visibility(rebuild=False)
        QTimer.singleShot(0, self._rebuild_layout)

    def resizeEvent(self, event):
        """手动拖拽 splitter 把手时自动折叠/展开

        展开态：拖拽把手把面板收窄到 _auto_collapse_width 以下 → 自动折叠
        为图标条（宽度由 TabManagerWindow 动画平滑收到收起宽度）。
        收起态：拖拽把手拉开超过阈值 → 自动切回展开态。
        宽度动画进行中（_animating=True）跳过：动画里宽度会经过
        阈值区间，若在此触发会与动画互相打断。

        注意：展开阈值与折叠阈值必须错开留滞回区（折叠 <100、展开 >=110），
        否则拖拽途中宽度在阈值附近抖动（如 99→101）会先折叠后展开，
        表现为"往里拉时又往外回弹"。滞回区（100~109）内保持当前状态不动。
        """
        super().resizeEvent(event)
        # 窗口 resize / relayout 过渡期：宽度是瞬时中间值，不代表用户意图，
        # 跳过自动折叠/展开。几何收拢完成后由 TabManagerWindow 按最终宽度
        # 统一判定（_evaluate_squeeze_collapse），避免最大化/还原等几何瞬变
        # 被误判成"用户把面板拖窄"。
        if self._auto_collapse_suppressed:
            return
        # 拖窄自动折叠（展开态 → 收起态）
        if not self._collapsed and not self._animating and self.width() < self._auto_collapse_width:
            self._collapsed = True
            # 宽度变化触发的折叠：可能是窗口 resize / 覆盖层 relayout 挤压（意外），
            # 也可能是用户拖拽把手主动收窄（有意）。无法从单次 resize 区分，
            # 统一标记为"宽度驱动折叠"，管理器在空间恢复时据此决定是否自动展开；
            # 手动拖拽把手会在 splitterMoved 中清除该标记（尊重手动意图）。
            self._collapsed_by_squeeze = True
            self._update_toggle_button()
            # 延迟发射信号，避免在 resize 链中直接嵌套 setSizes
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(0, lambda: self.sidebarToggled.emit(True))
            return
        # 拖宽自动展开（收起态 → 展开态）：阈值高于折叠阈值 10px 形成滞回区，
        # 折叠后拖拽抖动（宽度回到 100~109）不得再次展开，消除回弹。
        # 不区分折叠来源（手动/挤压）：任何收起态下拉宽超过滞回区即退出折叠，
        # 与窗口拉宽路径(_maybe_auto_expand_after_squeeze)行为一致——用户把
        # 面板/窗口拉宽即视为想要展开。手动拖把手拉开由 splitterMoved 显式处理。
        if self._collapsed and not self._animating and self.width() >= self._auto_collapse_width + 10:
            self._collapsed = False
            self._collapsed_by_squeeze = False
            self._update_toggle_button()
            # 延迟发射信号，避免在 resize 链中直接嵌套 setSizes
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(0, lambda: self.sidebarToggled.emit(False))

    def refresh_ui_plugins(self):
        """刷新 Tab 模式顶部的 UI 插件按钮列表

        数据源（Phase D）：
        1. 独立侧边栏项（UIPluginRegistry.get_sidebar_items()）——与 floating card 解耦
        2. 存量 floating card（container="left"）——兼容派生；若插件已注册
           独立 sidebar 项，则以 sidebar 为准（避免同一插件重复渲染两行）

        系统 UI 插件（plugins/ 目录下自带）→ 常驻显示，无滚动。
        自定义 UI 插件（~/.drifox/plugins/ 用户安装）→ 默认折叠，展开后可滚轮滚动。
        """
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            registry = UIPluginRegistry.get_instance()
            sidebar_items = registry.get_sidebar_items()
            cards = registry.get_floating_cards()
        except Exception:
            sidebar_items, cards = [], {}

        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()

        # 兼容映射：已注册独立 sidebar 项的插件 → 跳过其 container="left" 卡片派生
        sidebar_plugin_names = {info.plugin_name for info in sidebar_items}

        # 统一条目：(kind, key, title, plugin_name, priority)；kind ∈ {"sidebar", "card"}
        system_infos: list[tuple[str, str, str, str, int]] = []
        custom_infos: list[tuple[str, str, str, str, int]] = []
        for info in sidebar_items:
            entry = (
                "sidebar",
                info.item_id,
                (info.label or "").strip() or info.item_id,
                info.plugin_name,
                info.priority,
            )
            if info.group == "system":
                system_infos.append(entry)
            else:
                custom_infos.append(entry)
        for card_id, info in cards.items():
            # 兼容映射：插件已注册独立 sidebar 项 → 跳过其卡片派生（sidebar 优先）
            if info.plugin_name in sidebar_plugin_names:
                continue
            # 声明 hide_sidebar 的卡片不进侧边栏（如 autoloop 的 config/running
            # 仅经输入按钮/控制器弹出，避免侧边栏冗余条目）
            if info.metadata.get("hide_sidebar"):
                continue
            try:
                title = (info.title or "").strip() or card_id
                plugin_info = pm.get_plugin(info.plugin_name)
                is_system = plugin_info.is_system if plugin_info else False
                entry = ("card", card_id, title, info.plugin_name, 0)
                if is_system:
                    system_infos.append(entry)
                else:
                    custom_infos.append(entry)
            except Exception:
                continue
        # Phase E：按 priority 降序 → title 字母序兜底（regression: 旧 sort
        # 只看 title.lower() 抹平了 priority，导致高优先级插件被字母序压在后面）
        system_infos = _sort_plugin_entries(system_infos)
        custom_infos = _sort_plugin_entries(custom_infos)
        self._plugin_infos = system_infos + custom_infos

        # ── 系统插件区 ──
        while self._system_plugin_layout.count() > 0:
            item = self._system_plugin_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._system_plugin_buttons = []
        for kind, key, title, plugin_name, _priority in system_infos:
            # ★ T3 修复：单行构造异常不中断整体刷新（任一插件构造抛异常时
            # 跳过该插件，其余插件继续重建——否则一个"毒条目"导致整批
            # UI 插件按钮全部不显示）。
            try:
                row = UIPluginRow(
                    title,
                    self._get_plugin_icon(pm, plugin_name),
                    self._system_plugin_section,
                    plugin_name=plugin_name,
                    card_id=key,
                    enable_position_menu=(kind == "card"),
                )
            except Exception as e:
                logger.warning("[refresh_ui_plugins] 跳过异常插件 %s: %s", plugin_name, e)
                continue
            if kind == "sidebar":
                info = next((s for s in sidebar_items if s.item_id == key), None)
                row.clicked.connect(lambda cid=key, sid=info: self._on_sidebar_item_clicked(sid))
            else:
                row.clicked.connect(lambda cid=key: self._on_ui_plugin_clicked(cid))
                row.positionRequested.connect(self._on_ui_plugin_position_requested)
            self._system_plugin_layout.addWidget(row)
            self._system_plugin_buttons.append(row)
        has_system = bool(system_infos)
        self._system_plugin_section.setVisible(has_system)

        # ── 自定义插件区 ──
        while self._custom_plugin_layout.count() > 0:
            item = self._custom_plugin_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._custom_plugin_buttons = []
        for kind, key, title, plugin_name, _priority in custom_infos:
            # ★ T3 修复：单行构造异常不中断整体刷新（同系统插件区）。
            try:
                row = UIPluginRow(
                    title,
                    self._get_plugin_icon(pm, plugin_name),
                    self._custom_plugin_section,
                    plugin_name=plugin_name,
                    card_id=key,
                    enable_position_menu=(kind == "card"),
                )
            except Exception as e:
                logger.warning("[refresh_ui_plugins] 跳过异常插件 %s: %s", plugin_name, e)
                continue
            if kind == "sidebar":
                info = next((s for s in sidebar_items if s.item_id == key), None)
                row.clicked.connect(lambda cid=key, sid=info: self._on_sidebar_item_clicked(sid))
            else:
                row.clicked.connect(lambda cid=key: self._on_ui_plugin_clicked(cid))
                row.positionRequested.connect(self._on_ui_plugin_position_requested)
            self._custom_plugin_layout.addWidget(row)
            # 显式 show：清除 Qt 的 hidden 标志。折叠/展开态刷新时 scroll 被强制
            # 隐藏重建，未 show 的新行会被 QWidgetItem 视为空（sizeHint 贡献 0），
            # section.sizeHint 塌成 margins 高（4px），刷新末尾
            # _update_custom_plugin_scroll_height 读到 4 把列表高度锁死为"最矮"，
            # 需手动折叠/展开才能恢复。show() 仅清标志，父链隐藏时不会实际显示。
            row.show()
            self._custom_plugin_buttons.append(row)
        has_custom = bool(custom_infos)
        self._custom_plugin_card.setVisible(has_custom)
        # 重建前记录用户当前展开/折叠选择（展开态刷新后需恢复，见末尾）
        prev_custom_expanded = not self._custom_plugin_scroll.isHidden()
        # 默认折叠
        self._custom_plugin_scroll.setVisible(False)
        self._custom_plugin_arrow.set_expanded(False, animate=False)
        self._custom_plugin_label.setText("自定义插件")
        self._custom_plugin_badge.setText(str(len(custom_infos)))

        # ── 分隔符可见性 ──
        self._plugin_separator_2.setVisible(has_system or has_custom)

        self._refresh_plugin_style()

        # 重建后重新应用紧凑 UI：
        # - 折叠态：按 saved_state 应用（上面 scroll 被重置为默认折叠，新行未 compact）
        # - 展开态：无 saved_state 时 _update_toggle_button 保持当前状态不变；
        #   但上面已强制 setVisible(False)，需用重建前的用户选择恢复，
        #   避免手动展开的自定义插件列表在刷新（热更新/新增插件）后被强制折叠
        self._update_toggle_button()
        if not self._collapsed and getattr(self, "_custom_plugin_saved_state", None) is None:
            self._custom_plugin_scroll.setVisible(prev_custom_expanded)
            self._custom_plugin_arrow.set_expanded(prev_custom_expanded)
        # 行重建后按内容自适应高度（重载/安装/卸载插件后行数变化）
        self._update_custom_plugin_scroll_height()

    def _update_custom_plugin_scroll_height(self):
        """自定义插件列表高度自适应：未达上限时贴合内容，超出上限(200px)时封顶滚动

        QScrollArea 默认 sizeHint 不随内容变化，重建行后需手动按内容高度
        重设固定高度，否则插件重载/安装后出现内容变少仍占高、变多不扩展。
        """
        if not hasattr(self, "_custom_plugin_scroll"):
            return
        content_h = self._custom_plugin_section.sizeHint().height()
        self._custom_plugin_scroll.setFixedHeight(min(content_h, 200))

    def _on_sidebar_item_clicked(self, info):
        """独立侧边栏项点击：组上下文（当前窗口 + item_id）派发 info.on_click"""
        parent = self.parent()
        while parent is not None and not hasattr(parent, "get_current_window"):
            parent = parent.parent()
        if parent is None or info.on_click is None:
            logger.warning(f"[TabPanel] 侧边栏插件项 {info.item_id} 点击：无宿主窗口或未定义回调")
            return
        current_window = parent.get_current_window()
        if current_window is None:
            logger.warning(f"[TabPanel] 侧边栏插件项 {info.item_id} 点击：当前窗口为空")
            return
        try:
            context = {
                "item_id": info.item_id,
                "plugin_name": info.plugin_name,
                "window_id": getattr(current_window, "_window_id", None),
                "main_widget": current_window,
            }
            info.on_click(context)
        except Exception as e:
            logger.error(f"[TabPanel] 侧边栏插件项 {info.item_id} 回调失败：{e}")

    def _on_branch_clicked(self):
        """分支按钮点击：从当前活动 Tab 分支"""
        if 0 <= self._active_index < len(self._items):
            self.tabBranchRequested.emit(self._active_index)

    # ── 侧边栏收起/展开 ──

    def set_animating(self, animating: bool):
        """标记侧边栏宽度动画进行中：动画期间抑制 resizeEvent 自动展开

        由 TabManagerWindow 宽度动画开始/结束时调用。
        """
        self._animating = animating

    def set_auto_collapse_suppressed(self, suppressed: bool):
        """开关"resize/relayout 过渡期抑制自动折叠"

        由 TabManagerWindow 在窗口 resize 周期开始时置 True（几何尚未收拢，
        左面板宽度会瞬时跌到折叠阈值以下）、在几何收拢完成后置 False。
        抑制期内 resizeEvent 不自动折叠/展开，改由窗口在稳定几何上判定，
        否则最大化/还原这类几何瞬变会让侧边栏无故收起。
        """
        self._auto_collapse_suppressed = suppressed

    def sync_collapsed_ui(self):
        """按当前 _collapsed 状态同步紧凑/展开 UI（宽度动画跨阈值时调用）

        switch_ui=True：完整切换按钮图标 + 品牌区 + TabItem/团队紧凑态，
        供宽度动画在宽度跨过阈值时驱动，避免文字在窄条中被挤压。
        """
        self._update_toggle_button(switch_ui=True)

    def _toggle_sidebar(self):
        """切换侧边栏收起/展开状态

        仅翻转状态 + 更新按钮图标；紧凑/展开 UI 切换由宽度动画驱动
        （TabManagerWindow 在宽度跨过阈值时调用 _update_toggle_button），
        避免展开瞬间文字被挤在窄条里。
        """
        self._collapsed = not self._collapsed
        # 按钮手动折叠/展开：非挤压，清除挤压标记（避免空间恢复时误自动展开）
        self._collapsed_by_squeeze = False
        self._update_toggle_button(switch_ui=False)
        self.sidebarToggled.emit(self._collapsed)

    def set_collapsed(self, collapsed: bool):
        """外部设置侧边栏收起/展开状态（如启动时恢复配置，不发射信号）"""
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self._update_toggle_button()

    def _title_bar_toggle_btn(self):
        """窗口标题栏上的侧栏折叠按钮（品牌区上移后 TabPanel 不再自持）

        返回 None 表示无窗口宿主（单测 / TabPanel 独立构造）或标题栏尚未
        创建，调用方静默跳过即可。
        """
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is None:
                return None
            return getattr(getattr(tm, "titleBar", None), "_sidebar_btn", None)
        except Exception:
            return None

    def _update_toggle_button(self, switch_ui: bool = True):
        """更新收起/展开按钮的图标和提示，以及收起态下的可见元素

        Args:
            switch_ui: True 时同步切换紧凑/展开 UI（默认，按钮点击外的
                所有路径）；False 时仅更新按钮图标与 tooltip（按钮点击
                走宽度动画，UI 切换由动画跨阈值时驱动）。
        """
        # 侧栏折叠按钮已上移到窗口标题栏（CustomTitleBar 左区），此处仅同步其
        # tooltip 文案（图标由标题栏自身维护）；标题栏按钮缺失时静默跳过，
        # 保证 TabPanel 在独立测试/无窗口宿主场景下不报错。
        button = self._title_bar_toggle_btn()
        if button is not None:
            button.setIcon(get_icon("侧边栏"))
            button.setToolTip("展开侧边栏" if self._collapsed else "收起侧边栏")

        # switch_ui=False（按钮点击走宽度动画）时只更新按钮图标/tooltip，
        # 紧凑/展开 UI 由动画跨阈值时驱动，避免文字在窄条里被挤压。
        if not switch_ui:
            return

        if self._collapsed:
            # 收起时隐藏标题与分支/模式按钮，仅保留新建图标按钮（46px 窄条）
            self._sessions_label.setVisible(False)
            self._branch_btn.setVisible(False)
            self._new_btn.setVisible(True)
            if hasattr(self, "_mode_btn"):
                self._mode_btn.setVisible(False)
            # 收起时 Gitee 仅显示头像
            self._gitee_account_row.set_show_only_avatar(True)
        else:
            # 展开时恢复标题 + 分支/新建/模式图标按钮
            self._sessions_label.setVisible(True)
            self._branch_btn.setVisible(True)
            self._new_btn.setVisible(True)
            if hasattr(self, "_mode_btn"):
                self._mode_btn.setVisible(True)
            # 展开时恢复 Gitee 完整显示
            self._gitee_account_row.set_show_only_avatar(False)

        # ── 紧凑模式统一收口（矩阵 A2/A3）：折叠/展开影响所有 TabItem 与团队框 ──
        # 三入口（_toggle_sidebar / set_collapsed / resizeEvent 拖拽展开）最终都
        # 走到这里，确保不依赖 sidebarToggled 信号也能同步紧凑态。
        compact = self._collapsed
        for item in self._items:
            item.set_compact(compact)
        for grp in self._team_groups.values():
            self._apply_team_compact(grp, compact)

        # ── UI 插件区紧凑同步（矩阵 D5）：折叠时尊重折叠前状态 ──
        # 折叠态：header 简化为只显 arrow（label/badge 隐藏，46px 窄条容得下），
        #         scroll 保持折叠前可见性（不强制展开——用户手动展开过则保持
        #         展开，折叠过则保持折叠，由 saved_state 记录）；
        # 展开态：按 saved_state 恢复 label/badge/scroll 可见性。
        if hasattr(self, "_custom_plugin_card"):
            if compact:
                if getattr(self, "_custom_plugin_saved_state", None) is None:
                    self._custom_plugin_saved_state = {
                        "scroll_visible": not self._custom_plugin_scroll.isHidden(),
                        "label_visible": not self._custom_plugin_label.isHidden(),
                        "badge_visible": not self._custom_plugin_badge.isHidden(),
                    }
                # header 简化：只留 arrow（46px 窄条 label/badge 放不下）
                self._custom_plugin_label.setVisible(False)
                self._custom_plugin_badge.setVisible(False)
                # scroll 保持折叠前状态（不强制展开）
                self._custom_plugin_scroll.setVisible(bool(self._custom_plugin_saved_state["scroll_visible"]))
                self._custom_plugin_arrow.set_expanded(self._custom_plugin_scroll.isVisible())
                self._apply_custom_card_style(compact=True)
                self._update_custom_plugin_scroll_height()
            else:
                # 展开恢复：按 saved_state 恢复折叠前现场（saved_state 为 None 时
                # 保持当前 scroll 可见性——避免 refresh_ui_plugins 在展开态刷新
                # 后 scroll 被强制折叠）
                saved = getattr(self, "_custom_plugin_saved_state", None)
                if saved is not None:
                    self._custom_plugin_label.setVisible(bool(saved.get("label_visible", True)))
                    self._custom_plugin_badge.setVisible(bool(saved.get("badge_visible", True)))
                    self._custom_plugin_scroll.setVisible(bool(saved.get("scroll_visible", False)))
                    self._custom_plugin_arrow.set_expanded(self._custom_plugin_scroll.isVisible())
                    self._custom_plugin_saved_state = None
                    self._update_custom_plugin_scroll_height()
                self._apply_custom_card_style(compact=False)
        for row in self._custom_plugin_buttons:
            row.set_compact(compact)

        # 46px 窄条放不下工作区树 → 收起态统一降级为列表渲染，展开后恢复用户选择
        self._apply_mode_visibility()

    def _on_custom_plugin_toggle(self):
        """切换自定义插件折叠/展开状态"""
        # 用 isHidden()（显式隐藏状态）判断当前态：当前隐藏(True)→展开，
        # 当前显示(False)→折叠。isVisible() 依赖父链显示，折叠态/未显示窗口
        # 下恒 False 会导致 toggle 永远判为"展开"而无法折叠。
        expanded = self._custom_plugin_scroll.isHidden()
        self._custom_plugin_scroll.setVisible(expanded)
        self._custom_plugin_arrow.set_expanded(expanded)
        # 折叠态下手动切换 scroll：同步 saved_state，展开侧边栏时按最新
        # 状态恢复（避免回到"折叠前"旧状态造成体验割裂）
        if self._collapsed and getattr(self, "_custom_plugin_saved_state", None) is not None:
            self._custom_plugin_saved_state["scroll_visible"] = expanded
        # 展开时刷新样式，确保折叠期间的主题变更被应用；并按内容自适应高度
        if expanded:
            self._update_custom_plugin_scroll_height()
            for row in self._custom_plugin_buttons:
                row.refresh_style()

    @staticmethod
    def _get_plugin_icon(plugin_manager, plugin_name):
        """读取插件当前主题图标，读取失败时不影响列表显示"""
        try:
            plugin = plugin_manager.get_plugin(plugin_name)
            icon_config = getattr(plugin, "icon_config", None) if plugin else None
            icon_path = icon_config.get("dark" if isDarkTheme() else "light") if icon_config else None
            return QIcon(str(icon_path)) if icon_path else None
        except Exception:
            return None

    def _on_ui_plugin_clicked(self, card_id: str):
        """在当前活动 Tab 中打开 UI 插件卡片"""
        parent = self.parent()
        while parent is not None and not hasattr(parent, "get_current_window"):
            parent = parent.parent()
        if parent is None:
            logger.warning(f"[TabPanel] UI 插件 {card_id} 点击：无法找到 TabManagerWindow")
            return
        current_window = parent.get_current_window()
        if current_window is None:
            logger.warning(f"[TabPanel] UI 插件 {card_id} 点击：当前窗口为空")
            return
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            UIPluginRegistry.get_instance().toggle_floating_card(card_id, main_widget=current_window)
        except Exception as e:
            logger.error(f"[TabPanel] UI 插件 {card_id} 打开失败：{e}")

    def _on_ui_plugin_position_requested(self, card_id: str, container: str):
        """按指定方位移动 UI 插件卡片（仅内存生效，不持久化）"""
        parent = self.parent()
        while parent is not None and not hasattr(parent, "get_current_window"):
            parent = parent.parent()
        if parent is None:
            logger.warning(f"[TabPanel] UI 插件 {card_id} 定位：无法找到 TabManagerWindow")
            return
        current_window = parent.get_current_window()
        if current_window is None:
            logger.warning(f"[TabPanel] UI 插件 {card_id} 定位：当前窗口为空")
            return
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            registry = UIPluginRegistry.get_instance()
            if not registry.move_floating_card(card_id, container, main_widget=current_window):
                return
            # move_floating_card 仅在卡片原本可见时自动重建显示；原本隐藏时
            # 只更新方位注册，这里统一确保卡片显示（已可见则保持不动）
            host = parent  # TabManagerWindow（Tab 模式全局容器宿主）
            cm = getattr(host, "_card_manager", None)
            wid = getattr(host, "_window_id", None)
            if cm is not None and wid is not None and not cm.is_card_visible(card_id, wid):
                registry.toggle_floating_card(card_id, main_widget=current_window)
        except Exception as e:
            logger.error(f"[TabPanel] UI 插件 {card_id} 定位失败：{e}")

    def _refresh_plugin_style(self):
        """刷新插件区域的主题和字号样式"""
        # 品牌区（DriFox + 版本号 + 折叠按钮）已上移到窗口标题栏 CustomTitleBar，
        # 其样式由标题栏 refresh_style() 负责；此处不再处理。
        # 系统插件
        for row in self._system_plugin_buttons:
            row.refresh_style()
        # 自定义插件（即使折叠也需刷新，否则展开后样式仍为旧主题）
        for row in self._custom_plugin_buttons:
            row.refresh_style()
        # 自定义插件卡片 / 折叠头 / 标题 / 计数徽章（颜色随主题刷新）
        self._apply_custom_card_style(compact=self._collapsed)
        if hasattr(self, "_custom_plugin_arrow"):
            self._custom_plugin_arrow.update()
        # ── 顶部：「对话页」标题 + 分支/新建图标按钮随主题/字号刷新 ──
        self._refresh_top_bar_style()

    def _refresh_top_bar_style(self):
        """刷新顶部行样式：「对话页」标题（颜色/字体）+ 图标按钮（主题图标/图标尺寸）

        分支图标存在浅/深色两套资源，主题切换后需重新 setIcon，否则会沿用旧主题资源。
        """
        if hasattr(self, "_sessions_label") and self._sessions_label is not None:
            self._sessions_label.setFont(get_unified_font(12))
            self._sessions_label.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(12)}; font-weight: bold;"
            )
        if hasattr(self, "_branch_btn") and self._branch_btn is not None:
            self._branch_btn.setIcon(get_icon("分支"))
        # 竖向「⋯」是旋转位图（不随主题自动变色），主题/字号变更时必须重新生成
        if hasattr(self, "_mode_btn") and self._mode_btn is not None:
            self._mode_btn.setIcon(_vertical_more_icon())
        _icon_px = scale_icon_size(14)
        for _btn in (
            getattr(self, "_branch_btn", None),
            getattr(self, "_new_btn", None),
            getattr(self, "_mode_btn", None),
        ):
            if _btn is not None:
                _btn.setIconSize(QSize(_icon_px, _icon_px))

    def begin_batch_add(self):
        """开始批量添加 tab：期间 add_tab 跳过 _rebuild_team_layout，end_batch_add 统一重建。

        连续添加 N 个 Tab（如团队恢复/新建任务全员建会话）时，若每个 add_tab
        都全量重建团队布局，代价为 O(N²)。批量模式下只在外层 end_batch_add
        时重建一次，代价降为 O(N)。支持嵌套调用（内部计数），必须与
        end_batch_add 成对使用。
        """
        self._batch_add_depth = getattr(self, "_batch_add_depth", 0) + 1

    def end_batch_add(self):
        """结束批量添加 tab：统一重建一次视觉布局（仅在最外层结束时）。"""
        depth = getattr(self, "_batch_add_depth", 0)
        self._batch_add_depth = max(0, depth - 1)
        if self._batch_add_depth == 0:
            self._rebuild_layout()

    def begin_batch_remove(self):
        """开始批量删除 tab：期间 remove_tab 跳过 _rebuild_team_layout 与空组清理，
        end_batch_remove 统一重建。

        连续删除 N 个 Tab（如解散团队全员）时，若每个 remove_tab 都全量重建团队
        布局，代价为 O(N²)。批量模式下只在外层 end_batch_remove 时重建一次，
        代价降为 O(N)。支持嵌套调用（内部计数），必须与 end_batch_remove 成对使用。
        """
        self._batch_remove_depth = getattr(self, "_batch_remove_depth", 0) + 1

    def end_batch_remove(self):
        """结束批量删除 tab：统一清理批量期间可能变空的团队容器并重建一次布局
        （仅在最外层结束时）。"""
        depth = getattr(self, "_batch_remove_depth", 0)
        self._batch_remove_depth = max(0, depth - 1)
        if self._batch_remove_depth == 0:
            # 统一清理批量期间可能变空的团队容器（内部有成员判定，不空不删）
            for team_id in getattr(self, "_pending_empty_teams", ()):
                self._maybe_remove_empty_group(team_id)
            self._pending_empty_teams = set()
            self._rebuild_layout()

    def add_tab(self, title: str, icon=None, project_initials: str = "", project_color: str = "") -> int:
        """添加 Tab 项，返回其索引"""
        idx = len(self._items)
        item = TabItem(
            title, icon, self._list_widget, panel=self, project_initials=project_initials, project_color=project_color
        )

        # 连接信号 — ★ 使用动态索引查找，防止删除前序 tab 后索引漂移
        # 不能用 lambda 捕获 idx，否则删除 tab 0 后 tab 1 的 lambda 中 idx 仍为 1
        item.closeRequested.connect(
            lambda _item=item: self.tabCloseRequested.emit(self._items.index(_item) if _item in self._items else -1)
        )

        # 连接点击事件 — ★ 同样动态查找当前索引
        def _on_tab_click(ev, _item=item):
            if ev.button() == Qt.LeftButton:
                if _item in self._items:
                    self.set_active_index(self._items.index(_item))
                ev.accept()
            else:
                ev.ignore()

        item.mousePressEvent = _on_tab_click

        # 新 tab 不直接插入布局：扁平索引 insertWidget 在存在团队容器时会
        # 越界插到 stretch 之后（布局项数 = 独立 tab + 容器数 + 1(stretch)，
        # 而扁平 idx = 独立 + 团队 tab 数，团队容器含多 tab 时 idx ≥ 布局项数），
        # 破坏"stretch 恒在最末"假设。统一交给 _rebuild_team_layout 摆放。
        self._items.append(item)
        self._item_team[idx] = ""

        # 重建视觉布局：新 tab 作为独立项加入（置于团队框下方）。
        # _rebuild_team_layout 有快照保护，开销小。
        # 批量添加期间（begin_batch_add/end_batch_add 包围）跳过重建，
        # 由 end_batch_add 统一重建一次，避免连续 N 次添加触发 O(N²) 全量重建。
        if getattr(self, "_batch_add_depth", 0) == 0:
            self._rebuild_layout()

        # 折叠态新建 tab：立即紧凑（矩阵 D1/D2）
        if self._collapsed:
            item.set_compact(True)

        # 如果这是第一个 Tab，自动选中
        if len(self._items) == 1:
            self.set_active_index(0)

        return idx

    def set_tab_team(self, index: int, team_id: str):
        """设置指定索引 Tab 的团队归属。

        team_id 为空/None 时移回独立区；否则移入/创建对应 team 容器。
        _items 保持扁平索引；视觉布局由 _rebuild_team_layout 重建。
        """
        if not (0 <= index < len(self._items)):
            return
        team_id = team_id or ""
        old_team = self._item_team.get(index, "")
        if old_team == team_id:
            return  # 无变化
        self._item_team[index] = team_id
        # 若旧 team 已空，清理容器
        if old_team and not any(t == old_team for t in self._item_team.values()):
            self._maybe_remove_empty_group(old_team)
        # 重建视觉布局（team 容器置顶在上，独立区在下）
        self._rebuild_layout()

    def set_team_label(self, team_id: str, name: str):
        """设置指定 team 框 header 的团队名称

        Args:
            team_id: Tab 分组 key（与 set_tab_team 使用的 team_id 一致）
            name: 团队显示名称（空串兜底为"团队"）

        用法：TabManagerWindow 在 add_window / refresh_capsule_for_window 时
        调用，传入窗口的 _team_name，确保团队框 header 与实际团队名同步。
        """
        grp = self._team_groups.get(team_id)
        if grp is None:
            return  # 容器尚未创建（窗口未 join team / 未 set_tab_team）
        label = getattr(grp, "_team_name_label", None)
        if label is None:
            return
        # 空名兜底（与 _get_or_create_team_group 中占位文本一致）
        label.setText(name.strip() if name and name.strip() else "团队")

    def set_team_project(self, team_id: str, initials: str, color: str):
        """设置团队框 header 的项目 icon（缩写 + 颜色，复用 _TabProjectIcon）

        Args:
            team_id: Tab 分组 key（与 set_tab_team 使用的 team_id 一致）
            initials: 项目缩写（空串表示无团队级项目，隐藏 header icon）
            color: rgba 颜色字符串（如 'rgba(33,139,255,255)'）

        数据源必须是团队级 project（TeamManager.get_team_project）：多个成员
        窗口共享同一个团队框 header，读任一窗口自身项目会导致展示不一致。
        值相等时跳过（避免无效重绘）。
        """
        grp = self._team_groups.get(team_id)
        if grp is None:
            return  # 容器尚未创建
        icon = getattr(grp, "_team_icon", None)
        if icon is None:
            return
        if not initials:
            icon.setVisible(False)
            grp._team_icon_key = None
            # 折叠态清空项目：同步复位恢复现场，避免展开后复活旧项目 icon
            if getattr(grp, "_team_compact", False):
                grp._team_icon_orig_data = None
            return
        # 值相等跳过：同一 (initials, color) 不重复重绘（纯 key 缓存判断，
        # 不依赖 isVisible——父链未显示时 isVisible 恒 False 会误判）
        key = (initials, color)
        if grp._team_icon_key == key:
            return
        icon.set_project(initials, color)
        icon.setVisible(True)
        grp._team_icon_key = key

    def set_tab_team_mode(self, index: int, team_mode: bool):
        """设置指定 Tab 的团队模式（隐藏/显示项目 icon）"""
        if 0 <= index < len(self._items):
            self._items[index].set_team_mode(team_mode)

    def _get_or_create_team_group(self, team_id: str) -> "QFrame":
        """获取或创建 team 容器

        结构（自上而下嵌套）：
        - grp (QFrame) 的主布局 = outer (QVBoxLayout)
          - header (QWidget)：左侧团队名 QLabel + "新建任务"按钮 TransparentToolButton(get_icon("新会话"))
            + "快速新建成员"按钮 TransparentToolButton(get_icon("设置-subagent")) + 右侧关闭按钮 TransparentToolButton(FIF.CLOSE)
          - inner_widget (QWidget) 的布局 = inner_layout (QVBoxLayout)：成员 TabItem 列表

        header 默认隐藏 new_task/add/close 按钮，鼠标进入 header 区域时显示（参考 TabItem 实现）。
        new_task 按钮 click → teamNewTaskRequested(team_id)（全员新建会话 + 新 run_id）；
        add 按钮 click → teamAddMemberRequested(team_id)（快速新建成员，可重复角色）；
        close 按钮 click → teamCloseRequested(team_id)（含防御属性 WA_NoMousePropagation，
        避免鼠标事件冒泡到下层 TabItem 触发意外点击）。

        访问器：
        - grp.layout() = outer（grp 主布局）
        - grp._team_inner_layout = inner_layout（成员层，供 _rebuild_team_layout / 旧测试用）
        - grp._team_inner_widget = inner_widget
        - grp._team_header = header
        - grp._team_name_label / _team_new_task_btn / _team_add_btn / _team_close_btn = header 子控件
        """
        grp = self._team_groups.get(team_id)
        if grp is not None:
            return grp
        from PyQt5.QtWidgets import QVBoxLayout as _QVBL

        grp = QFrame(self._list_widget)
        grp.setObjectName("teamGroup")
        grp.setProperty("teamId", team_id)

        # ── outer：grp 主布局（嵌套结构：header + inner_widget） ──
        outer = _QVBL(grp)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(2)

        # ── header：团队名 + 关闭按钮 ──
        header = QWidget(grp)
        header.setObjectName("teamGroupHeader")
        header.setProperty("teamId", team_id)
        header.setAttribute(Qt.WA_Hover, True)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 2, 0, 4)
        header_layout.setSpacing(4)

        # 团队标题项目 icon（团队级统一项目，复用 _TabProjectIcon；默认隐藏，
        # 由 set_team_project 在团队级项目存在时显示）。多个成员窗口共享
        # 同一 header，数据源必须为团队级 project（TeamManager.get_team_project）。
        team_icon = _TabProjectIcon(header, size=16)
        team_icon.setVisible(False)
        header_layout.addWidget(team_icon)

        from app.widgets.elided_label import _ElidedLabel as _ELLabel

        name_label = _ELLabel("", header)
        name_label.setObjectName("teamGroupName")
        name_label.setText("团队")
        header_layout.addWidget(name_label, 1)

        # 新建任务按钮：hover 显示（与 add/close 联动），点击 → teamNewTaskRequested(team_id)
        # 🎨 图标：与主界面"新建对话"按钮一致的 新会话.svg（铅笔+加号，语义：全员新建会话）
        new_task_btn = TransparentToolButton(header)
        new_task_btn.setObjectName("teamGroupNewTaskBtn")
        new_task_btn.setIcon(get_icon("新会话"))
        new_task_btn.setFixedSize(20, 20)
        new_task_btn.setToolTip("新建任务：全员新建会话 + 生成新 run")
        new_task_btn.setVisible(False)
        new_task_btn.setAttribute(Qt.WA_NoMousePropagation, True)
        # clicked 信号会带 bool 参数（checked 状态），用 *args 忽略
        new_task_btn.clicked.connect(lambda *_args, _tid=team_id: self.teamNewTaskRequested.emit(_tid))
        header_layout.addWidget(new_task_btn)

        # 快速新建成员按钮：hover 显示（与 new_task/close 联动），点击 → teamAddMemberRequested(team_id)
        # 🎨 图标：与子智能体卡片 title 一致的 设置-subagent.svg
        add_btn = TransparentToolButton(header)
        add_btn.setObjectName("teamGroupAddBtn")
        add_btn.setIcon(get_icon("设置-subagent"))
        add_btn.setFixedSize(20, 20)
        add_btn.setToolTip("快速新建成员（可重复角色）")
        add_btn.setVisible(False)
        add_btn.setAttribute(Qt.WA_NoMousePropagation, True)
        # clicked 信号会带 bool 参数（checked 状态），用 *args 忽略
        add_btn.clicked.connect(lambda *_args, _tid=team_id: self.teamAddMemberRequested.emit(_tid))
        header_layout.addWidget(add_btn)

        close_btn = TransparentToolButton(header)
        close_btn.setObjectName("teamGroupCloseBtn")
        close_btn.setIcon(FIF.CLOSE)
        close_btn.setFixedSize(20, 20)
        close_btn.setToolTip("关闭团队")
        close_btn.setVisible(False)
        close_btn.setAttribute(Qt.WA_NoMousePropagation, True)
        # clicked 信号会带 bool 参数（checked 状态），用 *args 忽略
        close_btn.clicked.connect(lambda *_args, _tid=team_id: self.teamCloseRequested.emit(_tid))
        header_layout.addWidget(close_btn)
        outer.addWidget(header)

        # ── inner：成员列表（独立 widget + 独立布局，便于访问） ──
        inner_widget = QWidget(grp)
        inner_widget.setObjectName("teamGroupInner")
        inner_widget.setProperty("teamId", team_id)
        inner_layout = _QVBL(inner_widget)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(2)
        inner_widget.setLayout(inner_layout)
        outer.addWidget(inner_widget)

        grp.setLayout(outer)
        grp.layout().setProperty("teamId", team_id)

        # 访问器
        grp._team_header = header
        grp._team_name_label = name_label
        grp._team_close_btn = close_btn
        grp._team_new_task_btn = new_task_btn
        grp._team_add_btn = add_btn
        grp._team_icon = team_icon
        grp._team_icon_key = None  # 值相等跳过缓存（initials, color）
        grp._team_inner_widget = inner_widget
        grp._team_inner_layout = inner_layout

        # hover 控制 new_task/add/close 按钮可见性（紧凑态守卫：折叠态不弹按钮，矩阵 C3）
        def _enter(_e, _h=header, _btn=close_btn, _add=add_btn, _task=new_task_btn):
            if not getattr(grp, "_team_compact", False):
                _task.setVisible(True)
                _add.setVisible(True)
                _btn.setVisible(True)
                # 🛡️ 问题A 修复：三按钮由隐藏→显示时，若鼠标已停在某按钮上方，
                # Qt 不会为"显示后才在鼠标下"的控件补发 Enter → 该按钮的
                # _HoverTooltipFilter 收不到 Enter，tooltip 计时不启动（hover
                # 无提示）。手动查询鼠标所在控件，命中任一按钮则补发
                # QEnterEvent，让 tooltip 计时正常启动。
                from PyQt5.QtCore import QPointF as _QPointF
                from PyQt5.QtGui import QCursor as _QCursor, QEnterEvent as _QEnterEvent
                from PyQt5.QtWidgets import QApplication as _QApp

                _w = _QApp.widgetAt(_QCursor.pos())
                if _w in (_task, _add, _btn):
                    _lp = _w.mapFromGlobal(_QCursor.pos())
                    _gp = _QCursor.pos()
                    _QApp.sendEvent(_w, _QEnterEvent(_QPointF(_lp), _QPointF(_lp), _QPointF(_gp)))

        def _leave(_e, _h=header, _btn=close_btn, _add=add_btn, _task=new_task_btn):
            _btn.setVisible(False)
            _add.setVisible(False)
            _task.setVisible(False)

        header.enterEvent = _enter
        header.leaveEvent = _leave

        # 紧凑态字段（_apply_team_compact 使用）
        grp._team_compact = False
        grp._team_icon_orig_visible = False
        grp._team_icon_orig_data = None

        self._apply_team_group_style(grp)
        self._team_groups[team_id] = grp
        # 折叠态新建团队框：header 立即紧凑（规格书 2.2）
        if self._collapsed:
            self._apply_team_compact(grp, True)
        return grp

    def _apply_team_group_style(self, grp: "QFrame", bg_alpha: int = 40):
        """应用团队分组框样式：细边框 + 卡片背景 + 圆角，视觉清晰但不喧宾夺主

        同时刷新 header 子控件（团队名 QLabel + 关闭按钮）的样式，确保
        主题切换后 header 文字色与边框同步。

        Args:
            bg_alpha: 卡片背景透明度（0-255）。展开态默认 40（保持原视觉），
                折叠态由 _apply_team_compact 传入 70 增强窄条视觉边界（T4a 根因4），
                避免全局改色破坏展开态零回归（P2-1 方案 B）。
        """
        Colors.refresh()
        # 注意：Colors.HOVER_BG = "rgba(255, 255, 255, 0.08)" 不含 {alpha} 占位符，
        # .format(alpha=N) 是空操作（alpha 始终是字面 0.08），背景会过透明。
        # 用 CARD_BG（"rgba(33, 33, 38, {alpha})"，含占位符）正确代入 alpha。
        grp.setStyleSheet(f"""
            #teamGroup {{
                background: {Colors.CARD_BG.format(alpha=bg_alpha)};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                margin: 2px 0;
            }}
            #teamGroupHeader {{
                background: transparent;
                border: none;
            }}
            #teamGroupName {{
                color: {Colors.TEXT_PRIMARY};
                background: transparent;
                {get_font_family_css()} {font_size_css(12)}
                font-weight: bold;
                padding: 0px;
            }}
            #teamGroupNewTaskBtn {{
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 2px;
            }}
            #teamGroupNewTaskBtn:hover {{
                background: {Colors.HOVER_BG};
            }}
            #teamGroupAddBtn {{
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 2px;
            }}
            #teamGroupAddBtn:hover {{
                background: {Colors.HOVER_BG};
            }}
            #teamGroupCloseBtn {{
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 2px;
            }}
            #teamGroupCloseBtn:hover {{
                background: {Colors.HOVER_BG};
            }}
        """)
        # header 是独立子控件（在 grp 主布局外），需要单独刷新样式避免主题切换遗漏
        header = getattr(grp, "_team_header", None)
        if header is not None:
            header.setStyleSheet("""
                QWidget#teamGroupHeader {
                    background: transparent;
                    border: none;
                }
            """)

    def _apply_team_compact(self, grp: "QFrame", compact: bool):
        """团队框 header 紧凑模式（侧边栏折叠态）：仅保留团队 icon

        compact=True：
          - 隐藏团队名 label 与 new_task/add/close 按钮；
          - icon 无内容时用团队名首字 + 主题强调色绘制占位并显示（矩阵 C1）；
          - header 设置 tooltip=团队名（矩阵 C2）。
        compact=False：逐控件配对恢复——name 显示、new_task/add/close 恢复 hover 逻辑、
          icon 恢复原可见性与原数据（矩阵 D1 对称性）、tooltip 清空。
        幂等：相同状态重复调用直接返回。
        """
        header = getattr(grp, "_team_header", None)
        if header is None:
            return
        if getattr(grp, "_team_compact", False) == compact:
            return
        grp._team_compact = compact
        name_label = getattr(grp, "_team_name_label", None)
        close_btn = getattr(grp, "_team_close_btn", None)
        add_btn = getattr(grp, "_team_add_btn", None)
        new_task_btn = getattr(grp, "_team_new_task_btn", None)
        team_icon = getattr(grp, "_team_icon", None)
        team_name = (name_label.text() if name_label else "").strip() or "团队"

        if compact:
            # 记录恢复现场（icon 可见性 + 数据；用 isHidden 逆，与父链显示无关）
            grp._team_icon_orig_visible = not (team_icon and team_icon.isHidden())
            grp._team_icon_orig_data = None
            if team_icon is not None:
                grp._team_icon_orig_data = {
                    "initials": team_icon._initials,
                    "color": team_icon._color,
                    "fallback": team_icon._fallback_pixmap,
                }
                if getattr(grp, "_team_icon_key", None):
                    # 已有项目数据（set_team_project 写入过）→ 折叠态沿用项目
                    # icon，不替换为团队名首字母（折叠/展开 icon 统一为项目 icon）
                    team_icon.setVisible(True)
                else:
                    # 无项目数据 → 占位：团队名首字 + 主题强调色
                    ch = team_name[0] if team_name else "?"
                    team_icon.set_project(ch, Colors.INFO)
                    team_icon.setVisible(True)
            if name_label is not None:
                name_label.setVisible(False)
            if close_btn is not None:
                close_btn.setVisible(False)
            if add_btn is not None:
                add_btn.setVisible(False)
            if new_task_btn is not None:
                new_task_btn.setVisible(False)
            header.setToolTip(team_name)
            # 折叠态增强窄条视觉边界：背景加深（P2-1 方案 B，仅折叠态）
            self._apply_team_group_style(grp, bg_alpha=70)
        else:
            if name_label is not None:
                name_label.setVisible(True)
            if close_btn is not None:
                close_btn.setVisible(False)
            if add_btn is not None:
                add_btn.setVisible(False)
            if new_task_btn is not None:
                new_task_btn.setVisible(False)
            if team_icon is not None:
                # 折叠期间 set_team_project 写入/更新过项目数据（_team_icon_key
                # 非空）→ 以当前数据为准保留显示，不还原折叠前旧快照
                # （覆盖"折叠态创建/改项目 → 展开后项目 icon 消失"场景）
                if getattr(grp, "_team_icon_key", None):
                    team_icon.setVisible(True)
                    grp._team_icon_orig_data = None
                else:
                    # 折叠期间无项目数据 → 还原原数据与可见性（HexArgb 保留
                    # alpha，避免 rgba→hex 丢透明度）
                    data = getattr(grp, "_team_icon_orig_data", None) or {}
                    if data.get("fallback") is not None:
                        team_icon.set_fallback_pixmap(data["fallback"])
                        team_icon.setVisible(getattr(grp, "_team_icon_orig_visible", False))
                    elif data.get("initials"):
                        from PyQt5.QtGui import QColor as _QColor

                        team_icon.set_project(data["initials"], data["color"].name(_QColor.HexArgb))
                        team_icon.setVisible(getattr(grp, "_team_icon_orig_visible", False))
                    else:
                        # 折叠前无项目数据 → 恢复隐藏并清空占位符残留
                        team_icon._initials = ""
                        team_icon._fallback_pixmap = None
                        team_icon.setVisible(False)
            header.setToolTip("")
            # 展开态恢复原背景透明度（零回归）
            self._apply_team_group_style(grp, bg_alpha=40)
        header.update()

    def _maybe_remove_empty_group(self, team_id: str):
        """若指定 team 容器已无成员，从布局移除并 deleteLater

        新结构：grp 主布局 = 成员层（grp.layout() = inner_layout）。
        header 是 grp 的独立子控件（不在主布局中），容器 deleteLater 时随父子对象树回收。
        清空成员层残留 widget 时不动 header（header 在 _team_groups 中通过 grp 引用）。
        """
        grp = self._team_groups.get(team_id)
        if grp is None:
            return
        # 若还有成员，不删
        if any(t == team_id for t in self._item_team.values()):
            return
        # 防御：删除容器前把内部成员 widget 脱绑（parent 改回 _list_widget），
        # 避免容器 deleteLater 时连带销毁仍在 _items 中管理的 tab
        # （历史布局损坏可能在成员层内残留重复 widget）。
        inner_layout = getattr(grp, "_team_inner_layout", None)
        if inner_layout is None:
            # 兜底：兼容旧结构（grp.layout() 直接是成员层）
            inner_layout = grp.layout()
        while inner_layout is not None and inner_layout.count() > 0:
            child = inner_layout.takeAt(0)
            w = child.widget() if child is not None else None
            if w is not None:
                w.setParent(self._list_widget)
        self._list_layout.removeWidget(grp)
        # 🛡️ tooltip 兜底：先隐藏再 deleteLater。deleteLater 是延迟销毁（DeferredDelete
        # 事件循环空闲才执行），期间 close_btn 等子控件仍显示在屏幕上——若鼠标恰好
        # 停在其上方，tooltip 气泡不消失形成"残留"。主动 hide() 触发子控件
        # HideToParent（27）事件，_HoverTooltipFilter 随即收起 tooltip（B2 分支）。
        grp.hide()
        grp.deleteLater()
        self._team_groups.pop(team_id, None)

    def _rebuild_team_layout(self):
        """按当前 _item_team 重建视觉布局：

        顺序：所有 team 容器（置顶，按 _items 中首次出现顺序排列；同 team 的
        TabItem 在容器内按 _items 顺序排列）→ 所有独立 TabItem（无 team 归属）→
        末尾 stretch 始终在最末。

        实现：将所有现有 widget 从 _list_layout 移除，按规则重新 insert。
        末尾的 stretch 始终在最末。

        优化：当 _item_team 与上次构建快照一致且 _items 数量未变时直接 return，
        避免 add_tab/remove_tab 反复触发时的冗余重建。
        """
        # 快照对比：_item_team 内容 + _items 数量 + leader 成员签名 未变 → 跳过
        # （leader 签名含胶囊角色文本，update_tab_capsule 后置顶排序才能被感知）
        snapshot = (
            tuple(sorted(self._item_team.items())),
            len(self._items),
            tuple(i for i, item in enumerate(self._items) if item._capsule_label.text() == _LEADER_AGENT),
        )
        if getattr(self, "_layout_snapshot", None) == snapshot:
            return
        self._layout_snapshot = snapshot

        # 取出布局中的 stretch（QSpacerItem，widget() is None）：
        # 不假设"恒在最末"——历史 add_tab 用扁平索引 insertWidget 可能把新 tab
        # 插到 stretch 之后，破坏末尾假设。扫描全布局找到并 takeAt；
        # 找不到（历史损坏已丢失 stretch）则标记为 None，重建后 addStretch() 自愈。
        stretch_item = None
        for i in range(self._list_layout.count()):
            child = self._list_layout.itemAt(i)
            if child is not None and child.widget() is None:
                stretch_item = self._list_layout.takeAt(i)
                break

        # 清空布局（移除所有 widget 但不 delete；widget 仍由 _items 持有）。
        # 注意：真正的 stretch 已在上面取出，此处不再有任何 spacer 被静默丢弃。
        while self._list_layout.count() > 0:
            child = self._list_layout.takeAt(0)
            w = child.widget() if child is not None else None
            if w is not None:
                w.setParent(self._list_widget)  # 仅脱绑布局，不销毁

        # 收集每个 team 的成员（按 _items 顺序）
        team_order: List[str] = []  # 记录 team 容器插入顺序
        team_members: Dict[str, List[int]] = {}
        independent_indices: List[int] = []
        for i, item in enumerate(self._items):
            t = self._item_team.get(i, "")
            if t:
                if t not in team_members:
                    team_members[t] = []
                    team_order.append(t)
                team_members[t].append(i)
            else:
                independent_indices.append(i)

        # 1) 先放 team 容器（置顶，按首次出现顺序）
        for t in team_order:
            grp = self._get_or_create_team_group(t)
            # grp.layout() 是嵌套外层（header + inner_widget），成员层通过
            # _team_inner_layout 访问（向后兼容旧测试）。
            inner = getattr(grp, "_team_inner_layout", None)
            if inner is None:
                inner = grp.layout()
            # 清空成员层旧 widgets（header 在外层布局中，不在此层）
            while inner.count() > 0:
                inner.takeAt(0)
            # 成员 tab 加入成员层（header 已在 _get_or_create_team_group 中加入 outer）。
            # ⭐ leader 置顶：稳定排序——leader 排最前，其余成员保持 _items 原始顺序
            # （快速新建成员可追加任意角色，leader 加在中间也要自动置顶）。
            for i in sorted(
                team_members[t], key=lambda _i: 0 if self._items[_i]._capsule_label.text() == _LEADER_AGENT else 1
            ):
                inner.addWidget(self._items[i])
            self._list_layout.addWidget(grp)

        # 2) 再放独立 TabItem（无 team 归属，置于团队框下方）
        for i in independent_indices:
            self._list_layout.addWidget(self._items[i])

        # 3) 末尾 stretch：找到的真 stretch（QSpacerItem）放回末尾；
        #    找不到（历史损坏 stretch 已丢失）则 addStretch() 自愈重建。
        #    只有 stretch 会被 addItem，杜绝把 widget item 误当 stretch 重复入布局。
        if stretch_item is not None:
            self._list_layout.addItem(stretch_item)
        else:
            self._list_layout.addStretch()

        # ── 列表模式复位：清掉树模式留下的缩进 / 已激活标记 / 常显关闭钮 ──
        # 同时强制可见：树里被折叠节点跳过的成员会被 WorkspaceTree.rebuild
        # 隐藏（否则会以旧几何继续绘制形成残影），切回列表必须显式显示，
        # 否则表现为「列表空白」。
        for item in self._items:
            item.set_indent(0)
            item.set_open_marker(False)
            item.set_close_persistent(False)
            item.setVisible(True)
        for grp in self._team_groups.values():
            grp.setVisible(True)

        # 折叠态重建后统一应用紧凑（补充点 1：重建不得出现非紧凑新控件）
        if self._collapsed:
            for item in self._items:
                item.set_compact(True)
            for grp in self._team_groups.values():
                self._apply_team_compact(grp, True)

    # ── 显示模式：列表 / 工作区树 ───────────────────────────────────
    def current_mode(self) -> str:
        """当前选择的显示模式（"list" / "tree"）"""
        return self._mode

    def set_mode(self, mode: str, persist: bool = True):
        """切换对话页面板显示模式

        Args:
            mode: PANEL_MODE_LIST / PANEL_MODE_TREE；非法值回落列表模式
            persist: 是否写入 Settings（启动时恢复上次选择）
        """
        mode = PANEL_MODE_TREE if mode == PANEL_MODE_TREE else PANEL_MODE_LIST
        if persist and mode != self._mode:
            try:
                Settings.get_instance().tab_panel_mode.value = mode
            except Exception:
                pass
        self._mode = mode
        self._apply_mode_visibility()

    def _apply_mode_visibility(self, rebuild: bool = True):
        """按「当前模式 + 折叠态」决定两个滚动区的可见性并重建对应布局

        收起态（46px 窄条）放不下工作区树，统一降级为列表渲染；侧栏重新
        展开后自动回到用户选择的模式（_mode 本身不被改写）。

        Args:
            rebuild: 是否立即重建布局。构造期传 False —— 见 _setup_ui 末尾
                的延后构建说明（宿主 TabManagerWindow 可能还没初始化完）。
        """
        tree_active = self._mode == PANEL_MODE_TREE and not self._collapsed
        if self._tree_scroll is not None:
            self._tree_scroll.setVisible(tree_active)
        if self._scroll_area is not None:
            self._scroll_area.setVisible(not tree_active)
        if self._tree_widget is not None:
            self._tree_widget.set_compact(self._collapsed)
        # ⚠️ 切换容器后两侧布局都被搬空过，必须作废两边的快照缓存：
        # 否则 _rebuild_team_layout / _rebuild_tree_layout 会因签名未变直接
        # return —— 表现为「切回列表后空白」或「树模式专属状态没复位」。
        self._layout_snapshot = None
        self._tree_snapshot = None
        if rebuild:
            self._rebuild_layout()

    def _rebuild_layout(self):
        """布局重建分发：按当前生效模式选用列表/树实现

        所有原 _rebuild_team_layout 调用点都改走这里，列表模式下行为与
        改动前完全一致（既有的批量快照保护也照旧生效）。
        """
        if self._mode == PANEL_MODE_TREE and not self._collapsed:
            self._rebuild_tree_layout()
        else:
            self._rebuild_team_layout()

    def refresh_tree(self):
        """外部数据变更（新建/归档历史会话等）后强制刷新工作区树

        作废签名缓存，保证下一次重建一定真正执行。
        """
        self._tree_snapshot = None
        self._rebuild_layout()

    def _on_tree_expansion_changed(self, state):
        """树的折叠态变化 → 落 Settings（重启后保持展开现场）"""
        try:
            Settings.get_instance().workspace_tree_expansion.value = dict(state or {})
        except Exception:
            pass

    def _on_mode_btn_clicked(self):
        """竖向「⋯」按钮：弹出/收起模式选择悬浮框"""
        if self._mode_popup is not None:
            try:
                self._mode_popup.close()
            except Exception:
                pass
            self._mode_popup = None
            return
        popup = PanelModePopup(list(_PANEL_MODE_OPTIONS), self._mode, self)
        popup.modeSelected.connect(self.set_mode)
        popup.destroyed.connect(self._on_mode_popup_destroyed)
        self._mode_popup = popup
        popup.adjustSize()
        width = max(popup.sizeHint().width(), 196)
        height = popup.sizeHint().height()
        popup.setFixedSize(width, height)
        anchor = self._mode_btn.mapToGlobal(self._mode_btn.rect().bottomLeft())
        gx, gy = anchor.x(), anchor.y()
        host = self.window()
        if host is not None:
            frame = host.frameGeometry()
            if gx + width > frame.right():
                gx = max(frame.left() + 4, frame.right() - width - 8)
            if gy + height > frame.bottom():
                gy = max(frame.top() + 4, anchor.y() - height - self._mode_btn.height() - 4)
        popup.move(gx, gy)
        popup.show()

    def _on_mode_popup_destroyed(self, *_args):
        self._mode_popup = None

    def _active_list_container(self) -> QWidget:
        """当前生效的行容器（树模式 → 树 widget；否则列表 widget）

        右键菜单命中测试需要按屏幕坐标找子控件，必须先确定搜哪棵树。
        """
        if self._tree_widget is not None and self._mode == PANEL_MODE_TREE and not self._collapsed:
            return self._tree_widget
        return self._list_widget

    # ── 工作区树：数据编排 + 布局重建 ───────────────────────────────
    def _tab_owners(self, windows) -> List[tuple]:
        """按 _items 顺序收集每个已打开 Tab 的 (项目, 工作树, team_id)

        数据源是窗口实例（_current_project / _current_workdir），不额外缓存
        一份状态 —— 用户中途切项目/切工作树后，下次重建自然反映最新归属。
        """
        owners: List[tuple] = []
        for i in range(len(self._items)):
            win = windows[i] if i < len(windows) else None
            project = (getattr(win, "_current_project", "") or "").strip() or "默认项目"
            try:
                workdir_map = getattr(win, "_current_workdir", {}) or {}
                worktree = workdir_map.get(project, "") or ""
            except Exception:
                worktree = ""
            owners.append((project, worktree, self._item_team.get(i, "")))
        return owners

    @staticmethod
    def _open_session_ids(windows) -> set:
        """已打开窗口当前的 session_id 集合（历史列表里剔除，避免与 Tab 行重复）"""
        ids = set()
        for win in windows:
            try:
                sm = getattr(win, "session_manager", None)
                sess = sm.get_current_session() if sm is not None else None
                sid = getattr(sess, "session_id", "") if sess is not None else ""
                if sid:
                    ids.add(str(sid))
            except Exception:
                continue
        return ids

    def _collect_tree_specs(self) -> List[TreeNodeSpec]:
        """编排工作区树节点：项目 →（团队框）→ 工作树 →（已打开 Tab / 历史会话）

        节点必须按「父在前、子在后且缩进递增」输出；WorkspaceTree 遇到折叠的
        父节点会整体跳过其后续更深层节点（懒构建，不创建 widget）。
        """
        specs: List[TreeNodeSpec] = []
        if self._tree_widget is None:
            return specs

        # ⚠️ 宿主可能处于「半成品」状态：TabPanel 是在 TabManagerWindow.__init__
        # 里创建的，此刻 TabManagerWindow._tab_panel 尚未赋值，而
        # get_current_window() 会去读它 → AttributeError。整体兜住，
        # 取不到就按「无历史数据」渲染（只剩已打开 Tab 的骨架）。
        try:
            host = self._resolve_tab_host()
            windows = list(getattr(host, "_windows", []) or [])
            current = host.get_current_window() if hasattr(host, "get_current_window") else None
            source = current if current is not None else (windows[-1] if windows else None)
            backend = getattr(source, "backend", None)
            history = getattr(backend, "history_manager", None)
            memory = getattr(backend, "memory_manager", None)
        except Exception:
            host, windows, current, source = None, [], None, None
            backend = history = memory = None

        open_sids = self._open_session_ids(windows)
        owners = self._tab_owners(windows)

        # ── 项目集合：历史项目 + 已打开 Tab 的项目，当前项目置顶 ──
        try:
            projects = [str(p) for p in (history.get_projects() if history is not None else [])]
        except Exception:
            projects = []
        for proj, _wt, _team in owners:
            if proj and proj not in projects:
                projects.append(proj)
        if not projects:
            projects = ["默认项目"]
        cur_project = (getattr(current, "_current_project", "") or "").strip()
        if not cur_project and owners:
            cur_project = owners[0][0]
        if cur_project in projects:
            projects.remove(cur_project)
            projects.insert(0, cur_project)
        else:
            projects.sort()

        # ── 历史会话：团队按 run_id 聚合，其余按 项目+工作树 归组 ──
        try:
            sessions = list(history.get_history_list(merge_team=True) if history is not None else [])
        except Exception:
            sessions = []
        team_by_project: dict = {}
        plain_by_project: dict = {}
        for rec in sessions:
            proj = (rec.get("project") or "").strip() or "默认项目"
            if rec.get("team_merged"):
                team_by_project.setdefault(proj, []).append(rec)
                continue
            if str(rec.get("session_id") or "") in open_sids:
                continue  # 已作为 Tab 行出现，历史列表里不再重复
            plain_by_project.setdefault(proj, []).append(rec)

        for project in projects:
            plain = plain_by_project.get(project, [])
            opened_here = [o for o in owners if o[0] == project]
            has_open = bool(opened_here)
            # 含已激活对话页的工作树默认展开：占着后端的会话不能被折叠藏起来
            open_worktrees = {wt for p, wt, tid in owners if p == project and not tid}
            specs.append(
                TreeNodeSpec(
                    key=f"project:{project}",
                    kind=KIND_PROJECT,
                    title=project,
                    indent=_TREE_INDENT_PROJECT,
                    icon="folder",
                    count=len(plain) + len(opened_here),
                    active_count=len(opened_here),
                    project=project,
                    worktree="",
                    tooltip=f"项目：{project}\n历史会话 {len(plain)} · 已激活 {len(opened_here)}",
                    bold=True,
                    expanded_by_default=(project == cur_project or has_open),
                )
            )

            # 团队：先已打开的团队框，再历史团队条目（成员行可单独打开）
            for team_id, grp in self._team_groups.items():
                if self._team_project(team_id, owners) != project:
                    continue
                specs.append(
                    TreeNodeSpec(
                        key=f"teamwidget:{team_id}",
                        kind=KIND_WIDGET,
                        title="",
                        indent=_TREE_INDENT_WORKTREE,
                        widget=grp,
                    )
                )
            for entry in team_by_project.get(project, []):
                run_id = str(entry.get("team_run_id") or "")
                label = (entry.get("team_name") or "").strip() or "团队"
                specs.append(
                    TreeNodeSpec(
                        key=f"team:{run_id}",
                        kind=KIND_TEAM,
                        title=label,
                        indent=_TREE_INDENT_WORKTREE,
                        icon="团队",
                        count=int(entry.get("member_count") or 0),
                        project=project,
                        worktree="",
                        tooltip=f"团队：{label}",
                    )
                )
                for member in list(entry.get("members") or [])[:_TREE_MAX_SESSIONS]:
                    mid = str(member.get("session_id") or "")
                    if not mid or mid in open_sids:
                        continue
                    title = (
                        (member.get("agent_name") or "").strip()
                        or (member.get("title") or "").strip()
                        or "成员会话"
                    )
                    specs.append(
                        TreeNodeSpec(
                            key=f"session:{mid}",
                            kind=KIND_SESSION,
                            title=title,
                            indent=_TREE_INDENT_SESSION,
                            project=project,
                            worktree=str(member.get("worktree_path") or ""),
                            record=member,
                            tooltip=f"{label} · {title}",
                        )
                    )

            # 工作树：主仓库() + 会话记录里的 + 已登记的 git worktree + 已打开 Tab 的
            worktrees = [""]
            seen = {""}
            for rec in plain:
                wt = (rec.get("worktree_path") or "").strip()
                if wt not in seen:
                    seen.add(wt)
                    worktrees.append(wt)
            try:
                if memory is not None:
                    for doc in memory.get_key_documents(project) or []:
                        if str(doc.get("added_by") or "") != "git_worktree":
                            continue
                        path = (doc.get("file_path") or "").strip()
                        if path and path not in seen:
                            seen.add(path)
                            worktrees.append(path)
            except Exception:
                pass
            for proj, wt, _team in owners:
                if proj == project and wt not in seen:
                    seen.add(wt)
                    worktrees.append(wt)

            for wt in worktrees:
                label = "主仓库" if not wt else (os.path.basename(wt.rstrip("/\\")) or wt)
                children = [r for r in plain if (r.get("worktree_path") or "").strip() == wt]
                specs.append(
                    TreeNodeSpec(
                        key=f"worktree:{project}|{wt}",
                        kind=KIND_WORKTREE,
                        title=label,
                        indent=_TREE_INDENT_WORKTREE,
                        icon="根目录" if not wt else "分支",
                        count=len(children),
                        # 该工作树下已激活的对话页（团队会话已在团队框里，不计入）
                        active_count=len([o for o in opened_here if o[1] == wt and not o[2]]),
                        project=project,
                        worktree=wt,
                        tooltip=wt or f"{project} · 主仓库",
                        expanded_by_default=((project == cur_project and not wt) or wt in open_worktrees),
                    )
                )
                # 已打开的对话页（非团队，团队已单独成框）
                for i, (proj, wt_i, team_id) in enumerate(owners):
                    if proj != project or team_id or wt_i != wt:
                        continue
                    self._items[i].set_indent(_TREE_INDENT_TAB)
                    specs.append(
                        TreeNodeSpec(
                            key=f"tab:{i}",
                            kind=KIND_WIDGET,
                            title="",
                            indent=_TREE_INDENT_TAB,
                            widget=self._items[i],
                        )
                    )
                # 历史会话
                for rec in children[:_TREE_MAX_SESSIONS]:
                    sid = str(rec.get("session_id") or "")
                    if not sid:
                        continue
                    title = (rec.get("title") or "").strip() or "未命名会话"
                    specs.append(
                        TreeNodeSpec(
                            key=f"session:{sid}",
                            kind=KIND_SESSION,
                            title=title,
                            indent=_TREE_INDENT_SESSION,
                            project=project,
                            worktree=wt,
                            record=rec,
                            tooltip=title,
                        )
                    )
        return specs

    @staticmethod
    def _team_project(team_id: str, owners) -> str:
        """团队框归属的项目（取第一个成员 Tab 的项目）"""
        for proj, _wt, tid in owners:
            if tid == team_id:
                return proj
        return ""

    def _rebuild_tree_layout(self):
        """树模式布局重建：把已打开的 Tab / 团队框 / 历史会话挂进 项目→工作树 树里

        与 _rebuild_team_layout 平级：两者都只读 _items / _item_team，只是决定
        widget 摆进哪个容器。QLayout.addWidget 会自动 reparent，切模式无需搬运。
        """
        if self._tree_widget is None:
            return
        specs = self._collect_tree_specs()
        # 签名快照：节点 key + 标题 + 计数，未变则跳过重建
        snapshot = tuple((s.key, s.title, s.count) for s in specs)
        if getattr(self, "_tree_snapshot", None) == snapshot:
            return
        self._tree_snapshot = snapshot
        self._tree_widget.rebuild(specs)
        # 复用到树里的 TabItem / 团队框必须跟随折叠态
        compact = self._collapsed
        for item in self._items:
            # 已激活的对话页（一个会话一个后端）在树里要可辨识 + 可关闭
            item.set_open_marker(not compact)
            item.set_close_persistent(not compact)
            item.set_compact(compact)
        for grp in self._team_groups.values():
            grp.setVisible(True)
            self._apply_team_compact(grp, compact)

    def remove_tab(self, index: int):
        """移除指定索引的 Tab"""
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            # 如果该 tab 正在流式/报错，递减计数
            if item._streaming:
                self._streaming_count = max(0, self._streaming_count - 1)
            if item._stream_error:
                self._error_count = max(0, self._error_count - 1)
            if self._streaming_count + self._question_count + self._error_count == 0:
                self._stop_anim_timer()
            self._list_layout.removeWidget(item)
            if self._tree_widget is not None:
                self._tree_widget.detach_widget(item)
            item.deleteLater()

            # 清理 team 映射：弹出 index，重建后续索引（仅删除非末尾项时需要，
            # 末尾项 pop 后键 0..n-2 已连续，无需 O(n) 全量重建）
            old_team = self._item_team.pop(index, "")
            if index < len(self._item_team):
                new_mapping: Dict[int, str] = {}
                for i, t in self._item_team.items():
                    new_mapping[i - 1 if i > index else i] = t
                self._item_team = new_mapping

            # 批量删除模式（begin_batch_remove/end_batch_remove 包围）：空组清理
            # 与视觉布局重建延迟到 end_batch_remove 统一执行，避免连续删除 N 个
            # tab 触发 O(N²) 全量重建（与 begin_batch_add 对称）。
            if getattr(self, "_batch_remove_depth", 0) > 0:
                # 团队成员 tab 不在 _list_layout（在 team 容器内层），此处显式
                # 脱绑，否则 deleteLater 后内层布局仍持有 widget 引用（悬空）。
                if old_team:
                    grp = self._team_groups.get(old_team)
                    if grp is not None:
                        inner = getattr(grp, "_team_inner_layout", None)
                        if inner is None:
                            inner = grp.layout()
                        inner.removeWidget(item)
                    # 记录可能变空的 team，end 时统一判定清理
                    if not hasattr(self, "_pending_empty_teams"):
                        self._pending_empty_teams = set()
                    self._pending_empty_teams.add(old_team)
            else:
                # 若被移除 tab 所在 team 已空，移除 group 容器
                if old_team:
                    self._maybe_remove_empty_group(old_team)
                # 重建视觉布局：removeWidget 仅脱绑 widget，不重新排序，
                # 删除前部独立 tab 后剩余独立 tab 会停留在 team 容器之后。
                self._rebuild_layout()

            # 更新选中态
            if self._active_index == index:
                # 切换到相邻 Tab
                new_idx = min(index, len(self._items) - 1) if self._items else -1
                self.set_active_index(new_idx)
            elif self._active_index > index:
                self._active_index -= 1

    def set_active_index(self, index: int):
        """设置选中 Tab"""
        # 取消旧的选中态
        if 0 <= self._active_index < len(self._items):
            old_item = self._items[self._active_index]
            old_item.set_selected(False)
            old_item._cancel_close_confirm()  # 切换选中时取消旧 tab 的关闭确认态
            old_item._close_btn.setVisible(False)

        self._active_index = index

        # 设置新的选中态
        if 0 <= index < len(self._items):
            self._items[index].set_selected(True)
            self.tabSelected.emit(index)

    def update_tab_title(self, index: int, title: str):
        """更新 Tab 标题"""
        if 0 <= index < len(self._items):
            self._items[index].set_title(title)

    def update_tab_icon(self, index: int, icon):
        """更新 Tab 图标（QPixmap/QIcon 兜底）"""
        if 0 <= index < len(self._items):
            self._items[index].set_icon(icon)

    def update_tab_project(self, index: int, initials: str, color_rgba: str):
        """更新 Tab 的项目头像（缩写+颜色，直接 QPainter 绘制）"""
        if 0 <= index < len(self._items):
            self._items[index].set_project(initials, color_rgba)

    def update_tab_capsule(self, index: int, text: str):
        """显示团队角色胶囊"""
        if 0 <= index < len(self._items):
            item = self._items[index]
            old = item._capsule_label.text()
            item.set_capsule(text)
            # ⭐ leader 状态变化（补设/移除胶囊）→ 触发团队内重排置顶。
            # 非 leader 变化的更新被 _layout_snapshot 快照拦截，开销可忽略。
            if (old == _LEADER_AGENT) != (text == _LEADER_AGENT):
                self._rebuild_layout()

    def clear_tab_capsule(self, index: int):
        """隐藏团队角色胶囊"""
        if 0 <= index < len(self._items):
            item = self._items[index]
            item.clear_capsule()
            # ⭐ 角色胶囊被移除（leader 退出团队）→ 重排（与 update_tab_capsule 对称）
            self._rebuild_layout()

    def update_tab_streaming(self, index: int, streaming: bool, error: bool = False):
        """更新 Tab 的流式/错误状态"""
        if 0 <= index < len(self._items):
            item = self._items[index]
            old_streaming = item._streaming
            old_error = item._stream_error
            item.set_streaming(streaming, error)
            if streaming and not old_streaming:
                self._streaming_count += 1
                self._ensure_anim_timer()
            elif not streaming and old_streaming:
                self._streaming_count = max(0, self._streaming_count - 1)
            if error and not old_error:
                # 报错同样驱动动画（红色流光脉冲）
                self._error_count += 1
                self._ensure_anim_timer()
            elif not error and old_error:
                self._error_count = max(0, self._error_count - 1)
            if self._streaming_count + self._question_count + self._error_count == 0:
                self._stop_anim_timer()

    def update_tab_question(self, index: int, question: bool):
        """更新 Tab 的 question 状态（AI 提问等待用户回答）"""
        if not (0 <= index < len(self._items)):
            return
        old = self._items[index]._question
        if old == question:
            return
        self._items[index].set_question(question)
        if question:
            self._question_count += 1
            self._ensure_anim_timer()
        else:
            self._question_count = max(0, self._question_count - 1)
            if self._streaming_count + self._question_count + self._error_count == 0:
                self._stop_anim_timer()

    def _ensure_anim_timer(self):
        """确保彩虹动画定时器已启动"""
        if self._anim_timer is None:
            from PyQt5.QtCore import QTimer

            # [PERF] 66ms ≈ 15fps（原 50ms/20fps）。流光是慢速扫动的渐层，
            # 15fps 与 20fps 视觉无差异，但绘制开销降低 25%。相位增量同步放大
            # （12→16、6→8）以保持角速度与动画周期完全不变。
            self._anim_timer = QTimer(self)
            self._anim_timer.setInterval(66)
            self._anim_timer.timeout.connect(self._on_anim_tick)
        if not self._anim_timer.isActive():
            self._anim_timer.start()

    def _stop_anim_timer(self):
        if self._anim_timer and self._anim_timer.isActive():
            self._anim_timer.stop()
        self._anim_phase = 0.0

    def set_resizing(self, active: bool):
        """设置 resize 活跃状态

        resize 期间完全暂停动画定时器（避免无意义相位计算），
        resize 结束后如有流式 / question tab 则恢复。
        """
        self._is_resizing = active
        if active:
            if self._anim_timer and self._anim_timer.isActive():
                self._anim_timer.stop()
        else:
            if self._streaming_count + self._question_count + self._error_count > 0:
                self._ensure_anim_timer()

    def _on_anim_tick(self):
        """动画帧：推进相位 + 刷新所有流式 / question tab

        resize 期间动画定时器已完全暂停（见 set_resizing），
        此处不再需要 _is_resizing 判断。
        """
        # 相位增量按 66ms 帧长换算（保持角速度与 50ms/12° 完全一致）
        self._anim_phase = (self._anim_phase + 16) % 360
        self._question_phase = (self._question_phase + 8) % 360  # ≈3s 一周期（慢呼吸）
        for item in self._items:
            # 跳过不可见标签（多标签横向滚动时大部分 item 已滚出视口）：
            # update() 对不可见控件只是排队一次无效重绘。
            if item.isVisible() and (item._streaming or item._stream_error or item._question):
                item.update()

    def _reapply_scroll_styles(self):
        """重新应用侧边两个滚动区的滚动条 QSS（含滚动条颜色跟随主题）。

        滚动条颜色来自 Colors.SCROLLBAR_HANDLE_BG（主题源属性，Colors.refresh()
        已随主题更新）；滚动区 QSS 在 __init__ 时一次性烘焙，若主题切换后不
        重建就会停留在旧主题的滚动条颜色。此方法在 refresh_style 中调用，
        与 model_selector_card / project_selector_card 的统一模式保持一致。
        """
        # 自定义 UI 插件滚动区
        if hasattr(self, "_custom_plugin_scroll"):
            self._custom_plugin_scroll.setStyleSheet(
                f"""
                QScrollArea {{
                    background: transparent;
                    border: none;
                }}
                QScrollArea > QWidget > QWidget {{
                    background: transparent;
                }}
                {get_unified_scrollbar_style(4)}
                """
            )
        # 侧边 Tab 列表滚动区
        if hasattr(self, "_scroll_area"):
            self._scroll_area.setStyleSheet(
                f"""
                QScrollArea {{
                    background: transparent;
                    border: none;
                }}
                QScrollArea > QWidget > QWidget {{
                    background: transparent;
                }}
                {get_unified_scrollbar_style(6)}
                """
            )
            # 强制滚动条重新应用样式（水平 + 垂直，确保主题切换后颜色即时生效）
            for sb in (self._scroll_area.verticalScrollBar(), self._scroll_area.horizontalScrollBar()):
                if sb is not None:
                    sb_style = sb.style()
                    if sb_style is not None:
                        sb_style.unpolish(sb)
                        sb_style.polish(sb)

        if getattr(self, "_tree_scroll", None) is not None:
            self._tree_scroll.setStyleSheet(
                f"""
                QScrollArea {{
                    background: transparent;
                    border: none;
                }}
                QScrollArea > QWidget > QWidget {{
                    background: transparent;
                }}
                {get_unified_scrollbar_style(6)}
                """
            )
            for sb in (self._tree_scroll.verticalScrollBar(), self._tree_scroll.horizontalScrollBar()):
                if sb is not None:
                    sb_style = sb.style()
                    if sb_style is not None:
                        sb_style.unpolish(sb)
                        sb_style.polish(sb)

    def refresh_style(self):
        """ThemeManager 统一刷新入口：主题/字体变更后调用

        注意：调用方（TabManagerWindow._on_theme_changed）已执行 Colors.refresh()，
        此处不再重复调用。
        """
        # 刷新模块级缓存颜色，避免 paintEvent 使用旧主题色值
        _invalidate_cached_colors()
        # 重新应用侧边两个滚动区的滚动条 QSS（滚动条颜色随主题）
        self._reapply_scroll_styles()
        # 先全部调用 update()（异步，Qt 自动合并绘制事件），
        # 再对 panel 统一触发一次重绘，避免逐个 repaint() 同步卡顿
        for item in self._items:
            item.refresh_style()
        # 同步刷新团队分组框样式（边框/背景色随主题）
        for grp in self._team_groups.values():
            # 折叠态保持加深背景（bg_alpha=70），避免主题刷新把 alpha 重置回 40
            self._apply_team_group_style(grp, bg_alpha=70 if getattr(grp, "_team_compact", False) else 40)
        self.update()
        # 工作区树（即使当前不可见也要刷新，否则切过去还是旧主题）
        if self._tree_widget is not None:
            self._tree_widget.refresh_style()
        self._refresh_plugin_style()
        if self._gitee_account_row is not None:
            self._gitee_account_row.refresh_style()

    def _apply_custom_card_style(self, compact: bool = False):
        """应用自定义插件卡片分组样式（卡片背景 + 细边框 + 圆角，对齐团队分组框）。

        颜色取自主题 Colors，主题切换时由 _refresh_plugin_style 重新调用。
        compact=True：折叠态紧凑样式——margin 收紧，窄条下只容纳 icon 行。
        """
        if not hasattr(self, "_custom_plugin_card"):
            return
        Colors.refresh()
        margin = "3px 4px" if compact else "5px 8px"
        self._custom_plugin_card.setStyleSheet(f"""
            #customPluginCard {{
                background: {Colors.CARD_BG.format(alpha=40)};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                margin: {margin};
            }}
            #customPluginHeader {{
                background: transparent;
                border: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
            }}
            #customPluginHeader:hover {{
                background: {Colors.HOVER_BG};
            }}
            #customPluginTitle {{
                color: {Colors.TEXT_PRIMARY};
                background: transparent;
                {get_font_family_css()} {font_size_css(12)}
                font-weight: bold;
                padding: 0px;
            }}
            #customPluginBadge {{
                color: {Colors.TEXT_MUTED};
                background: {Colors.HOVER_BG};
                border-radius: 8px;
                padding: 1px 7px;
                {get_font_family_css()} {font_size_css(10)}
                min-width: 14px;
            }}
        """)

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def active_index(self) -> int:
        return self._active_index

    def _resolve_tab_host(self):
        """沿父链查找 TabManagerWindow（window_id 来源）"""
        parent = self.parent()
        while parent is not None and not hasattr(parent, "get_current_window"):
            parent = parent.parent()
        return parent

    def _inject_plugin_tab_actions(self, menu: QMenu, context: dict):
        """注入 tab 右键菜单插件项（Phase D，target="tab"）

        与消息卡片注入同语义：action_func 返回 False → 关闭菜单。
        """
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            actions = UIPluginRegistry.get_instance().get_context_actions("tab")
        except Exception:
            return
        for info in actions:
            try:
                if info.separator_before:
                    menu.addSeparator()
                action = menu.addAction(info.label)
                enabled = True
                if info.enabled_func is not None:
                    try:
                        enabled = bool(info.enabled_func(context))
                    except Exception:
                        enabled = True
                action.setEnabled(enabled)
                action.triggered.connect(lambda checked=False, i=info: self._run_plugin_tab_action(i, context))
            except Exception as e:
                logger.warning(f"[TabPanel] 插件菜单项 {info.action_id} 注入失败：{e}")

    def _run_plugin_tab_action(self, info, context: dict):
        """执行插件菜单项：action_func(context)；返回 False → 关闭菜单（保持现有语义）"""
        try:
            close_menu = info.action_func(context) is False
        except Exception as e:
            logger.error(f"[TabPanel] 插件菜单项 {info.action_id} 执行失败：{e}")
            close_menu = True
        if close_menu:
            try:
                menu = self._current_context_menu
                if menu is not None:
                    menu.close()
            except Exception:
                pass

    def contextMenuEvent(self, event):
        """显示右键菜单

        右键点击了哪个标签页就操作哪个标签页；
        如果没有点击到任何标签页（如点击空白区域），则操作当前选中的标签页。
        """
        # ── 确定右键点击对应的标签页索引 ──
        clicked_index = self._active_index  # 默认回退到当前选中
        container = self._active_list_container()
        list_pos = container.mapFromGlobal(event.globalPos())
        child = container.childAt(list_pos)
        while child is not None and child is not container:
            if isinstance(child, TabItem):
                if child in self._items:
                    clicked_index = self._items.index(child)
                break
            child = child.parentWidget()

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
                {get_font_family_css()} {font_size_css(13)}
            }}
            QMenu::item:selected {{
                background: {Colors.HOVER_BG};
            }}
        """)
        new_action = menu.addAction("新建标签页")
        if clicked_index >= 0:
            branch_action = menu.addAction("分支标签页")
        menu.addSeparator()
        if clicked_index >= 0:
            close_action = menu.addAction("关闭标签页")

        # Phase D：插件右键菜单项（target="tab"）
        context = {
            "tab_index": clicked_index,
            "window_id": getattr(self._resolve_tab_host(), "_window_id", None),
        }
        self._current_context_menu = menu  # 供 action_func 返回 False 时关闭菜单
        self._inject_plugin_tab_actions(menu, context)

        try:
            action = menu.exec_(event.globalPos())
        finally:
            self._current_context_menu = None
        if action == new_action:
            self.newTabRequested.emit()
        elif clicked_index >= 0:
            if action == close_action:
                self.tabCloseRequested.emit(clicked_index)
            elif action == branch_action:
                self.tabBranchRequested.emit(clicked_index)
