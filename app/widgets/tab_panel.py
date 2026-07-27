# -*- coding: utf-8 -*-
"""
TabPanel — Tab 管理器左侧面板

每个 Tab 项显示：Agent 图标 + 会话标题 + 关闭按钮。
支持拖拽排序、右键菜单、滚轮滚动。
"""

from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QIcon, QMouseEvent, QPainter, QPixmap
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
from loguru import logger
from qfluentwidgets import (
    FluentIcon as FIF,
    TransparentPushButton,
    TransparentToolButton,
    isDarkTheme,
)

from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style, scale_icon_size
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_font_family_css
from app.widgets.cards.settings.gitee_card import GiteeAccountRow
from app.widgets.elided_label import _ElidedLabel

# ── 模块级缓存：避免 paintEvent 中反复解析 rgba 字符串 ──
import re as _re
import math as _math
from PyQt5.QtGui import (
    QColor as _QColor,
    QLinearGradient as _QLinearGradient,
    QPainterPath as _QPainterPath,
    QPen as _QPen,
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

# 悬停渐层颜色常量（避免 paintEvent 中反复创建 QColor）
_HOVER_DARK_COLORS = (_QColor(99, 102, 241, 32), _QColor(139, 92, 246, 18))
_HOVER_LIGHT_COLORS = (_QColor(99, 102, 241, 22), _QColor(139, 92, 246, 12))

# 流光 shimmer 渐层颜色常量（避免 paintEvent 中反复创建 5 个 QColor）
_SHIMMER_COLORS = (
    _QColor(255, 255, 255, 0),
    _QColor(130, 200, 255, 55),
    _QColor(180, 220, 255, 100),
    _QColor(130, 200, 255, 55),
    _QColor(255, 255, 255, 0),
)

# 错误态红条颜色
_CACHED_ERROR_RED = _QColor(220, 50, 50)


class TabItem(QFrame):
    """单个 Tab 项的 UI 组件"""

    closeRequested = pyqtSignal()

    def __init__(self, title: str, icon=None, parent=None, panel=None):
        super().__init__(parent)
        self._title = title
        self._icon_pixmap = icon
        self._selected = False
        self._streaming = False
        self._stream_error = False
        self._question = False  # AI 提问等待用户回答（橙黄脉动）
        self._hovered = False  # 鼠标悬停态
        self._panel = panel  # TabPanel 引用，用于读取 _anim_phase
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

        # 图标
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(self._icon_size, self._icon_size)
        self._apply_icon_to_label()
        layout.addWidget(self._icon_label)

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

        # 关闭按钮（与主标题栏一致的 FluentIcon.CLOSE）
        self._close_btn = TransparentToolButton(self)
        self._close_btn.setIcon(FIF.CLOSE)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setVisible(False)
        self._close_btn.clicked.connect(self.closeRequested.emit)
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
            self._icon_label.setFixedSize(self._icon_size, self._icon_size)
            self._apply_icon_to_label()
        self._apply_title_style()
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

    def _apply_icon_to_label(self):
        """统一处理 _icon_pixmap 缩放并赋值给 _icon_label，消除三处重复代码"""
        pixmap = self._icon_pixmap
        if pixmap is None:
            self._icon_label.clear()
            return
        from PyQt5.QtGui import QPixmap as _QP

        if isinstance(pixmap, _QP):
            if pixmap.devicePixelRatio() > 1.0:
                # HiDPI 感知 pixmap，直接设避免二次缩放破坏清晰度
                self._icon_label.setPixmap(pixmap)
            else:
                self._icon_label.setPixmap(
                    pixmap.scaled(self._icon_size, self._icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
        else:
            try:
                p = pixmap.pixmap(self._icon_size, self._icon_size)
                if p:
                    self._icon_label.setPixmap(p)
            except Exception as exc:
                logger.debug(f"[TabItem] 无法获取图标 pixmap: {exc}")

    def set_icon(self, icon):
        self._icon_pixmap = icon
        self._apply_icon_to_label()

    def set_capsule(self, text: str, color: str = ""):
        """显示团队角色胶囊"""
        if not color:
            # 从 agent 名 hash 生成稳定色
            h = abs(hash(text)) % 360
            color = f"hsl({h}, 65%, 50%)"
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

    def clear_capsule(self):
        """隐藏团队角色胶囊"""
        self._capsule_label.setVisible(False)
        self._capsule_label.setText("")

    def enterEvent(self, event):
        self._close_btn.setVisible(True)
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        if not self._selected:
            self._close_btn.setVisible(False)
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

        # ── 左侧指示条通用绘制函数：沿圆角路径描边 3px，clip 到左侧 5px 显示 ──
        def _draw_left_indicator(painter_obj, color):
            """用 3px 粗笔沿 _round_rect 描边，clip 到左 5px，自然呈现贴合圆角的曲线"""
            painter_obj.save()
            painter_obj.setClipRect(0, 0, 5, h)
            pen = _QPen(color, 3)
            pen.setCapStyle(Qt.RoundCap)
            painter_obj.setPen(pen)
            painter_obj.setBrush(_QColor(0, 0, 0, 0))
            painter_obj.drawPath(_round_rect)
            painter_obj.restore()

        # ── 流式/错误状态 ──
        if self._streaming or self._stream_error:
            if self._stream_error:
                _draw_left_indicator(painter, _CACHED_ERROR_RED)
            else:
                # 左侧彩虹逐帧单色指示条（贴合圆角曲线）
                phase = self._panel._anim_phase if self._panel else 0
                idx = int((phase / 360) * _RAINBOW_N) % _RAINBOW_N
                _draw_left_indicator(painter, _RAINBOW_COLORS[idx])

                # ── 整条标签来回脉冲流光（约束在圆角路径内） ──
                # ★ resize 期间跳过昂贵渐层，仅保留左侧指示条
                if self._panel and self._panel._is_resizing:
                    pass  # 跳过 shimmer 渐层
                else:
                    # sin 映射：0→360 相位对应 -1→1→-1，产生来回扫动
                    sweep = _math.sin(_math.radians(phase))
                    sweep_t = (sweep + 1.0) / 2.0  # 0.0 ~ 1.0
                    # 光斑中心在标签上从 -20% 扫到 120%
                    shimmer_center = sweep_t * (w + 0.4 * w) - 0.2 * w

                    shimmer_grad = _QLinearGradient(shimmer_center - 80, 0, shimmer_center + 80, 0)
                    shimmer_grad.setColorAt(0.0, _SHIMMER_COLORS[0])
                    shimmer_grad.setColorAt(0.3, _SHIMMER_COLORS[1])
                    shimmer_grad.setColorAt(0.5, _SHIMMER_COLORS[2])
                    shimmer_grad.setColorAt(0.7, _SHIMMER_COLORS[3])
                    shimmer_grad.setColorAt(1.0, _SHIMMER_COLORS[4])
                    painter.save()
                    painter.setClipPath(_round_rect)
                    painter.fillRect(self.rect(), shimmer_grad)
                    painter.restore()
        elif self._question:
            # AI 提问等待回答：橙黄 #F59E0B 慢呼吸脉动（1.2s 一周期）
            phase = self._panel._question_phase if self._panel else 0
            # resize 期间跳过 sin 计算取固定亮度
            if self._panel and self._panel._is_resizing:
                alpha = 150
            else:
                # 50ms 帧速 +6°/帧 ≈ 1.2s 一周期；亮度在 ~80~220 间脉动
                alpha = int(150 + _math.sin(_math.radians(phase)) * 70)
            _draw_left_indicator(painter, _QColor(245, 158, 11, max(0, min(255, alpha))))
        elif self._selected:
            # 左侧选中指示条（贴合圆角曲线）
            _draw_left_indicator(painter, _CACHED_INFO)

        super().paintEvent(event)


class UIPluginRow(QFrame):
    """TabPanel 中的 UI 插件行，固定图标和文本的相对位置。"""

    clicked = pyqtSignal()

    def __init__(self, title: str, icon: Optional[QIcon] = None, parent=None, plugin_name: str = ""):
        super().__init__(parent)
        self._plugin_name = plugin_name  # 存储插件名，主题刷新时重新获取图标
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(scale_icon_size(16), scale_icon_size(16))
        self._title_label = QLabel(title, self)
        self._title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)
        layout.addWidget(self._icon_label)
        layout.addWidget(self._title_label, 1)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("uiPluginRow")
        self.set_icon(icon)
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
                from app.core.plugin_manager import PluginManager

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


class TabPanel(QWidget):
    """左侧 Tab 列表面板"""

    tabSelected = pyqtSignal(int)  # 选中 Tab 索引
    tabCloseRequested = pyqtSignal(int)  # 关闭 Tab 索引
    tabBranchRequested = pyqtSignal(int)  # 分支窗口 Tab 索引
    newTabRequested = pyqtSignal()  # 新建 Tab
    tabsReordered = pyqtSignal(list)  # 拖拽排序后新顺序（索引列表）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[TabItem] = []
        self._active_index: int = -1
        self._plugin_infos: list[tuple[str, str, str]] = []
        self._system_plugin_layout: Optional[QVBoxLayout] = None
        self._system_plugin_buttons: list[UIPluginRow] = []
        self._custom_plugin_layout: Optional[QVBoxLayout] = None
        self._custom_plugin_buttons: list[UIPluginRow] = []
        self._gitee_account_row: Optional[GiteeAccountRow] = None
        self._anim_phase: float = 0.0  # 彩虹动画相位
        self._question_phase: float = 0.0  # question 脉动相位（独立，避免与彩虹冲突）
        self._anim_timer: Optional[QTimer] = None  # 有 tab 流式/question 时启动
        self._streaming_count: int = 0  # 当前流式 tab 计数
        self._question_count: int = 0  # 当前 question 状态 tab 计数
        self._is_resizing: bool = False  # resize 活跃态，用于节流动画/绘制
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

        # ── 顶部：品牌区 ──
        self._brand_widget = QWidget(self)
        brand_layout = QVBoxLayout(self._brand_widget)
        brand_layout.setContentsMargins(10, 8, 10, 2)
        brand_layout.setSpacing(1)
        self._brand_title = QLabel("DriFox", self._brand_widget)
        self._brand_title.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; {font_size_css(15)}; font-weight: bold; background: transparent;"
        )
        self._brand_version = QLabel("v0.4.7", self._brand_widget)
        self._brand_version.setStyleSheet(f"color: {Colors.TEXT_MUTED}; background: transparent; {font_size_css(11)}")
        brand_layout.addWidget(self._brand_title)
        brand_layout.addWidget(self._brand_version)
        layout.addWidget(self._brand_widget)

        # ── 品牌区下分隔线 ──
        self._brand_separator = QFrame(self)
        self._brand_separator.setFrameShape(QFrame.HLine)
        self._brand_separator.setStyleSheet(self._SEPARATOR_STYLE)
        layout.addWidget(self._brand_separator)

        # ── 系统 UI 插件（常驻显示，无滚动） ──
        self._system_plugin_section = QWidget(self)
        system_plugin_layout = QVBoxLayout(self._system_plugin_section)
        system_plugin_layout.setContentsMargins(6, 0, 6, 4)
        system_plugin_layout.setSpacing(2)
        self._system_plugin_layout = system_plugin_layout
        self._system_plugin_section.setStyleSheet("background: transparent;")
        self._system_plugin_section.setVisible(False)
        layout.addWidget(self._system_plugin_section)

        # ── 分隔线：系统插件 ↔ 自定义插件 ──
        self._plugin_separator_1 = QFrame(self)
        self._plugin_separator_1.setFrameShape(QFrame.HLine)
        self._plugin_separator_1.setStyleSheet(self._SEPARATOR_STYLE)
        self._plugin_separator_1.setVisible(False)
        layout.addWidget(self._plugin_separator_1)

        # ── 自定义 UI 插件折叠区 ──
        self._custom_plugin_header = QWidget(self)
        self._custom_plugin_header.setCursor(Qt.PointingHandCursor)
        custom_header_layout = QHBoxLayout(self._custom_plugin_header)
        custom_header_layout.setContentsMargins(6, 4, 6, 4)
        custom_header_layout.setSpacing(4)
        self._custom_plugin_arrow = QLabel("▶", self._custom_plugin_header)
        self._custom_plugin_arrow.setFixedWidth(12)
        self._custom_plugin_arrow.setStyleSheet(f"color: {Colors.TEXT_MUTED}; background: transparent;")
        self._custom_plugin_label = QLabel("自定义插件", self._custom_plugin_header)
        self._custom_plugin_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(12)}"
        )
        custom_header_layout.addWidget(self._custom_plugin_arrow)
        custom_header_layout.addWidget(self._custom_plugin_label, 1)
        self._custom_plugin_header.setVisible(False)
        self._custom_plugin_header.mousePressEvent = lambda ev: self._on_custom_plugin_toggle()
        layout.addWidget(self._custom_plugin_header)

        # ── 自定义 UI 插件滚动区 ──
        self._custom_plugin_scroll = QScrollArea(self)
        self._custom_plugin_scroll.setWidgetResizable(True)
        self._custom_plugin_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._custom_plugin_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._custom_plugin_scroll.setFrameShape(QFrame.NoFrame)
        self._custom_plugin_scroll.setMaximumHeight(160)
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
        custom_plugin_layout.setContentsMargins(6, 0, 6, 4)
        custom_plugin_layout.setSpacing(2)
        self._custom_plugin_layout = custom_plugin_layout
        self._custom_plugin_section.setStyleSheet("background: transparent;")
        self._custom_plugin_scroll.setWidget(self._custom_plugin_section)
        self._custom_plugin_scroll.setVisible(False)
        layout.addWidget(self._custom_plugin_scroll)

        # ── 分隔线：UI 插件区域 ↔ 新建标签页 ──
        self._plugin_separator_2 = QFrame(self)
        self._plugin_separator_2.setFrameShape(QFrame.HLine)
        self._plugin_separator_2.setStyleSheet(self._SEPARATOR_STYLE)
        self._plugin_separator_2.setVisible(False)
        layout.addWidget(self._plugin_separator_2)

        # ── 顶部：新建按钮 ──
        top_bar = QWidget(self)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(6, 6, 6, 4)
        self._new_btn = TransparentPushButton(FIF.ADD, "新建标签页", top_bar)
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._new_btn.clicked.connect(self.newTabRequested.emit)
        top_layout.addWidget(self._new_btn)
        layout.addWidget(top_bar)

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
        self._list_layout.setContentsMargins(6, 0, 6, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.addStretch()
        self._scroll_area.setWidget(self._list_widget)
        layout.addWidget(self._scroll_area, 1)

        # ── 分隔线 ──
        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet(self._SEPARATOR_STYLE)
        layout.addWidget(separator)

        # ── 底部：设置按钮 ──
        bottom_bar = QWidget(self)
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(6, 4, 6, 6)

        self._settings_btn = TransparentPushButton(FIF.SETTING, "设置", bottom_bar)
        self._settings_btn.setCursor(Qt.PointingHandCursor)
        self._settings_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._settings_btn.clicked.connect(self._on_settings_clicked)
        bottom_layout.addWidget(self._settings_btn)

        layout.addWidget(bottom_bar)

        account_separator = QFrame(self)
        account_separator.setFrameShape(QFrame.HLine)
        account_separator.setStyleSheet(self._SEPARATOR_STYLE)
        layout.addWidget(account_separator)

        self._gitee_account_row = GiteeAccountRow(self)
        layout.addWidget(self._gitee_account_row)

    def _on_settings_clicked(self):
        """打开设置卡片"""
        from PyQt5.QtWidgets import QWidget

        # 沿父链向上找 OpenAIChatToolWindow
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, "_toggle_settings_card"):
                parent._toggle_settings_card()
                return
            parent = parent.parent()
        # 兜底：通过 TabManagerWindow 切换回独立模式
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm:
            current = tm.get_current_window()
            if current and hasattr(current, "_toggle_settings_card"):
                current._toggle_settings_card()

    def refresh_ui_plugins(self):
        """刷新 Tab 模式顶部的 UI 插件按钮列表

        系统 UI 插件（plugins/ 目录下自带）→ 常驻显示，无滚动。
        自定义 UI 插件（~/.drifox/plugins/ 用户安装）→ 默认折叠，展开后可滚轮滚动。
        """
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            cards = UIPluginRegistry.get_instance().get_floating_cards()
        except Exception:
            cards = {}

        from app.core.plugin_manager import PluginManager

        pm = PluginManager.get_instance()

        # 按 plugin_type 分组
        system_infos: list[tuple[str, str, str]] = []
        custom_infos: list[tuple[str, str, str]] = []
        for card_id, info in cards.items():
            try:
                title = (info.title or "").strip() or card_id
                plugin_info = pm.get_plugin(info.plugin_name)
                is_system = plugin_info.is_system if plugin_info else False
                entry = (card_id, title, info.plugin_name)
                if is_system:
                    system_infos.append(entry)
                else:
                    custom_infos.append(entry)
            except Exception:
                continue
        system_infos.sort(key=lambda item: item[1].lower())
        custom_infos.sort(key=lambda item: item[1].lower())
        self._plugin_infos = system_infos + custom_infos

        # ── 系统插件区 ──
        while self._system_plugin_layout.count() > 0:
            item = self._system_plugin_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._system_plugin_buttons = []
        for card_id, title, plugin_name in system_infos:
            row = UIPluginRow(
                title,
                self._get_plugin_icon(pm, plugin_name),
                self._system_plugin_section,
                plugin_name=plugin_name,
            )
            row.clicked.connect(lambda cid=card_id: self._on_ui_plugin_clicked(cid))
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
        for card_id, title, plugin_name in custom_infos:
            row = UIPluginRow(
                title,
                self._get_plugin_icon(pm, plugin_name),
                self._custom_plugin_section,
                plugin_name=plugin_name,
            )
            row.clicked.connect(lambda cid=card_id: self._on_ui_plugin_clicked(cid))
            self._custom_plugin_layout.addWidget(row)
            self._custom_plugin_buttons.append(row)
        has_custom = bool(custom_infos)
        self._custom_plugin_header.setVisible(has_custom)
        # 默认折叠
        self._custom_plugin_scroll.setVisible(False)
        self._custom_plugin_arrow.setText("▶")
        self._custom_plugin_label.setText(f"自定义插件 ({len(custom_infos)})")

        # ── 分隔符可见性 ──
        self._plugin_separator_1.setVisible(has_system and has_custom)
        self._plugin_separator_2.setVisible(has_system or has_custom)

        self._refresh_plugin_style()

    def _on_custom_plugin_toggle(self):
        """切换自定义插件折叠/展开状态"""
        expanded = not self._custom_plugin_scroll.isVisible()
        self._custom_plugin_scroll.setVisible(expanded)
        self._custom_plugin_arrow.setText("▼" if expanded else "▶")
        # 展开时刷新样式，确保折叠期间的主题变更被应用
        if expanded:
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
            from app.core.ui_plugin_registry import UIPluginRegistry

            UIPluginRegistry.get_instance().toggle_floating_card(card_id, main_widget=current_window)
        except Exception as e:
            logger.error(f"[TabPanel] UI 插件 {card_id} 打开失败：{e}")

    def _refresh_plugin_style(self):
        """刷新插件区域的主题和字号样式"""
        # 品牌区
        if hasattr(self, "_brand_title"):
            self._brand_title.setStyleSheet(
                f"color: {Colors.TEXT_PRIMARY}; {font_size_css(15)}; font-weight: bold; background: transparent;"
            )
        if hasattr(self, "_brand_version"):
            self._brand_version.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: transparent; {font_size_css(11)}"
            )
        # 系统插件
        for row in self._system_plugin_buttons:
            row.refresh_style()
        # 自定义插件（即使折叠也需刷新，否则展开后样式仍为旧主题）
        for row in self._custom_plugin_buttons:
            row.refresh_style()
        if hasattr(self, "_custom_plugin_arrow"):
            self._custom_plugin_arrow.setStyleSheet(f"color: {Colors.TEXT_MUTED}; background: transparent;")
        if hasattr(self, "_custom_plugin_label"):
            self._custom_plugin_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(12)}"
            )

    def add_tab(self, title: str, icon=None) -> int:
        """添加 Tab 项，返回其索引"""
        idx = len(self._items)
        item = TabItem(title, icon, self._list_widget, panel=self)

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

        # 在 stretch 之前插入
        self._list_layout.insertWidget(idx, item)
        self._items.append(item)

        # 如果这是第一个 Tab，自动选中
        if len(self._items) == 1:
            self.set_active_index(0)

        return idx

    def remove_tab(self, index: int):
        """移除指定索引的 Tab"""
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            # 如果该 tab 正在流式，递减计数
            if item._streaming:
                self._streaming_count = max(0, self._streaming_count - 1)
                if self._streaming_count == 0:
                    self._stop_anim_timer()
            self._list_layout.removeWidget(item)
            item.deleteLater()

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
            self._items[self._active_index].set_selected(False)
            self._items[self._active_index]._close_btn.setVisible(False)

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
        """更新 Tab 图标"""
        if 0 <= index < len(self._items):
            self._items[index].set_icon(icon)

    def update_tab_capsule(self, index: int, text: str):
        """显示团队角色胶囊"""
        if 0 <= index < len(self._items):
            self._items[index].set_capsule(text)

    def clear_tab_capsule(self, index: int):
        """隐藏团队角色胶囊"""
        if 0 <= index < len(self._items):
            self._items[index].clear_capsule()

    def update_tab_streaming(self, index: int, streaming: bool, error: bool = False):
        """更新 Tab 的流式/错误状态"""
        if 0 <= index < len(self._items):
            old = self._items[index]._streaming
            self._items[index].set_streaming(streaming, error)
            if streaming and not old:
                self._streaming_count += 1
                self._ensure_anim_timer()
            elif not streaming and old:
                self._streaming_count = max(0, self._streaming_count - 1)
                if self._streaming_count + self._question_count == 0:
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
            if self._streaming_count + self._question_count == 0:
                self._stop_anim_timer()

    def _ensure_anim_timer(self):
        """确保彩虹动画定时器已启动"""
        if self._anim_timer is None:
            from PyQt5.QtCore import QTimer

            self._anim_timer = QTimer(self)
            self._anim_timer.setInterval(50)  # 50ms ≈ 20fps
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
            if self._streaming_count + self._question_count > 0:
                self._ensure_anim_timer()

    def _on_anim_tick(self):
        """动画帧：推进相位 + 刷新所有流式 / question tab

        resize 期间动画定时器已完全暂停（见 set_resizing），
        此处不再需要 _is_resizing 判断。
        """
        self._anim_phase = (self._anim_phase + 12) % 360
        self._question_phase = (self._question_phase + 6) % 360  # 1.2s 一周期（慢呼吸）
        for item in self._items:
            if item._streaming or item._question:
                item.update()

    def refresh_style(self):
        """ThemeManager 统一刷新入口：主题/字体变更后调用

        注意：调用方（TabManagerWindow._on_theme_changed）已执行 Colors.refresh()，
        此处不再重复调用。
        """
        # 刷新模块级缓存颜色，避免 paintEvent 使用旧主题色值
        _invalidate_cached_colors()
        # 先全部调用 update()（异步，Qt 自动合并绘制事件），
        # 再对 panel 统一触发一次重绘，避免逐个 repaint() 同步卡顿
        for item in self._items:
            item.refresh_style()
        self.update()
        self._refresh_plugin_style()
        if self._gitee_account_row is not None:
            self._gitee_account_row.refresh_style()

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def active_index(self) -> int:
        return self._active_index

    def contextMenuEvent(self, event):
        """显示右键菜单"""
        if self._active_index < 0:
            return

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
            }}
            QMenu::item:selected {{
                background: {Colors.HOVER_BG};
            }}
        """)
        close_action = menu.addAction("关闭标签页")
        menu.addSeparator()
        branch_action = menu.addAction("分支窗口")

        action = menu.exec_(event.globalPos())
        if action == close_action:
            self.tabCloseRequested.emit(self._active_index)
        elif action == branch_action:
            self.tabBranchRequested.emit(self._active_index)
