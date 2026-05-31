# -*- coding: utf-8 -*-
"""
统一的设计系统 - Design Tokens 和样式常量
所有 UI 组件应引用此模块以保持视觉一致性

主题完全从 app/themes/ 目录读取，不硬编码主题数据
"""

from PyQt5.QtCore import QSize

from app.utils.theme_manager import theme_manager


def _get_global_font() -> str:
    """获取全局字体名称，用于样式表"""
    try:
        from app.utils.config import Settings
        return Settings.get_instance().llm_font_family.value
    except Exception:
        try:
            return Settings.get_instance().canvas_font_selected.value
        except Exception:
            return "Segoe UI"


FONT_SIZE_OPTIONS = {
    "small": {"label": "小", "delta": -1, "base": 13},
    "medium": {"label": "中", "delta": 0, "base": 14},
    "large": {"label": "大", "delta": 2, "base": 16},
    "superlarge": {"label": "超大", "delta": 4, "base": 18},
}


def get_ui_font_size_key() -> str:
    try:
        from app.utils.config import Settings
        key = Settings.get_instance().ui_font_size.value
    except Exception:
        key = "medium"
    return key if key in FONT_SIZE_OPTIONS else "medium"


def get_ui_font_size() -> int:
    """获取当前配置的基础字体大小（未缩放）"""
    return FONT_SIZE_OPTIONS[get_ui_font_size_key()]["base"]


def scale_font_size(size: int) -> int:
    return max(8, int(size) + FONT_SIZE_OPTIONS[get_ui_font_size_key()]["delta"])


def font_size_css(size: int) -> str:
    return f"font-size: {scale_font_size(size)}px;"


def apply_font_size_to_widget(widget, base_size: int = 14):
    """递归设置 widget 及其所有子控件的字体像素大小
    
    用于解决 qfluentwidgets 组件字体不随配置变化的问题。
    qfluentwidgets 的 QSS 使用硬编码字体大小（如 font: 14px），
    setFont() 无法覆盖，必须通过 stylesheet 强制覆盖。
    
    Args:
        widget: 要设置字体的 widget
        base_size: 基础字体大小（会经过 scale_font_size 缩放）
    """
    from PyQt5.QtWidgets import QWidget
    scaled = scale_font_size(base_size)
    content_scaled = scale_font_size(11)
    font_family = _get_global_font()
    
    for child in widget.findChildren(QWidget):
        child_font = child.font()
        child_font.setPixelSize(scaled)
        child_font.setFamily(font_family)
        child.setFont(child_font)
    
    # qfluentwidgets SettingCard / ExpandSettingCard 的 titleLabel / contentLabel
    # 使用硬编码 QSS（font: 14px / font: 11px），setFont 无法覆盖，必须用 stylesheet 强制
    from qfluentwidgets.components.settings.setting_card import SettingCard
    from qfluentwidgets.components.settings.expand_setting_card import ExpandSettingCard
    from qfluentwidgets.components.widgets.switch_button import SwitchButton
    
    for card in widget.findChildren(SettingCard):
        card.titleLabel.setStyleSheet(
            f"QLabel {{ font-size: {scaled}px; font-family: '{font_family}'; }}"
        )
        card.contentLabel.setStyleSheet(
            f"QLabel#contentLabel {{ font-size: {content_scaled}px; font-family: '{font_family}'; }}"
        )
    
    for card in widget.findChildren(ExpandSettingCard):
        # ExpandSettingCard 内部的 HeaderSettingCard 继承 SettingCard，已在上面处理
        # 但其 titleLabel objectName 是 "titleLabel"，需要额外用 objectName 选择器覆盖
        if hasattr(card, 'card') and hasattr(card.card, 'titleLabel'):
            card.card.titleLabel.setStyleSheet(
                f"QLabel#titleLabel {{ font-size: {scaled}px; font-family: '{font_family}'; }}"
            )
        if hasattr(card, 'card') and hasattr(card.card, 'contentLabel'):
            card.card.contentLabel.setStyleSheet(
                f"QLabel#contentLabel {{ font-size: {content_scaled}px; font-family: '{font_family}'; }}"
            )
    
    # SwitchButton 内部 QLabel 也硬编码了 font: 14px
    for switch in widget.findChildren(SwitchButton):
        switch.setStyleSheet(
            f"SwitchButton>QLabel {{ font-size: {scaled}px; font-family: '{font_family}'; }}"
        )


def current_theme() -> dict:
    """获取当前主题的扁平 colors 字典"""
    return theme_manager.get_current_colors()


def get_window_style() -> str:
    """获取窗口渐变背景样式"""
    window = theme_manager.get_theme_window(theme_manager.get_current_theme_id())
    return f"""
    #OpenAIChatToolWindow {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 {window.get('gradient_start', 'rgba(10, 14, 22, 255)')},
            stop:1 {window.get('gradient_end', 'rgba(15, 20, 30, 255)')});
    }}
    """


def get_capsule_style() -> str:
    """获取胶囊样式"""
    theme = current_theme()
    return f"""
        background: {theme["capsule_bg"]};
        border: 1px solid {theme["capsule_border"]};
        border-radius: 12px;
    """


# ============ 颜色系统 ============
class Colors:
    """颜色 Token - 动态从 ThemeManager 读取"""
    
    # 默认值（fallback，用于主题未加载时）
    CARD_BG = "rgba(33, 33, 38, {alpha})"
    CARD_BG_SOLID = "rgba(33, 33, 38, 250)"
    CONTENT_BG = "#2a2a2e"
    BORDER = "#3d3d3d"
    BORDER_ACCENT = "#f59e0b"
    TEXT_PRIMARY = "#ffffff"
    TEXT_SECONDARY = "rgba(255, 255, 255, 0.5)"
    TEXT_SECONDARY_HOVER = "rgba(255, 255, 255, 0.8)"
    TEXT_ACCENT = "#f59e0b"
    TEXT_MUTED = "#888888"
    TAB_ACTIVE_BG = "rgba(102, 198, 255, 0.3)"
    TAB_INACTIVE = "rgba(255, 255, 255, 0.5)"
    TAB_HOVER_BG = "rgba(255, 255, 255, 0.1)"
    HOVER_BG = "rgba(255, 255, 255, 0.08)"
    SELECTED_BG = "rgba(102, 198, 255, 0.35)"
    
    # 组件级颜色
    USER_CARD_BG = "rgba(27, 42, 67, 150)"
    USER_CARD_ACCENT = "#9FC3FF"
    USER_CARD_TEXT = "#F4F7FD"
    USER_CARD_MUTED = "#B4C2D9"
    ASSISTANT_CARD_BG = "rgba(45, 30, 20, 150)"
    ASSISTANT_CARD_ACCENT = "#D35400"
    ASSISTANT_CARD_TEXT = "#FFD4B8"
    ASSISTANT_CARD_MUTED = "#8FA4C2"
    AGENT_BTN_TEXT = "#8FA4C2"
    AGENT_BTN_TEXT_ACTIVE = "#C9A85C"
    AGENT_BTN_BG_ACTIVE = "rgba(201, 168, 92, 0.2)"
    AGENT_BTN_SEPARATOR = "rgba(60, 75, 95, 150)"
    INPUT_BG_START = "rgba(18, 24, 34, 150)"
    INPUT_BG_END = "rgba(24, 31, 45, 150)"
    INPUT_FOCUS_BG_START = "rgba(22, 29, 41, 220)"
    INPUT_FOCUS_BG_END = "rgba(28, 36, 50, 220)"
    INPUT_TEXT = "#F2F6FF"
    INPUT_FOCUS_TEXT = "#FFFFFF"
    INPUT_BORDER = "#2B3850"
    INPUT_FOCUS_BORDER = "#C9A85C"
    INPUT_PLACEHOLDER = "rgba(242, 246, 255, 0.4)"
    
    # 实时卡片色
    REALTIME_BORDER = "#4a90d9"
    REALTIME_ACCENT = "#7dd3fc"
    REALTIME_ACCENT_WARM = "#fbbf24"
    REALTIME_SUCCESS = "#34d399"
    REALTIME_ERROR = "#f87171"
    REALTIME_BG = "rgba(18, 28, 48, 242)"
    REALTIME_TEXT = "#f3f6fc"
    REALTIME_TEXT_SECONDARY = "rgba(226, 235, 249, 0.7)"
    REALTIME_TAG_BG = "rgba(125, 211, 252, 0.15)"
    REALTIME_TAG_BORDER = "rgba(125, 211, 252, 0.3)"
    
    # 系统卡片色
    SYSTEM_BORDER = "#3d4a60"
    SYSTEM_ACCENT = "#66c6ff"
    
    # 发送按钮
    SEND_BTN_START = "#C9A85C"
    SEND_BTN_END = "#B8956A"
    SEND_BTN_HOVER_START = "#D4B878"
    SEND_BTN_HOVER_END = "#C9A060"
    SEND_BTN_RADIUS = 17  # 按钮圆角半径
    
    # 时间线
    TIMELINE_NODE = "#5A5A5A"
    TIMELINE_NODE_HOVER = "#6BA3FF"
    TIMELINE_NODE_VISIBLE = "#00FF7F"
    TIMELINE_NODE_SELECTED = "#FFA500"
    TIMELINE_LINE = "#3A3A3A"
    TIMELINE_LINE_PROGRESS = "#00FF7F"
    
    # 上下文圆环
    RING_NORMAL = "#5aa9ff"
    RING_WARNING = "#f6c453"
    RING_DANGER = "#ff6b6b"
    RING_COMPACTED = "#9b59b6"
    
    # 分支标签
    BRANCH_LABEL_BG = "rgba(102, 198, 255, 0.15)"
    BRANCH_LABEL_BORDER = "rgba(102, 198, 255, 0.3)"
    
    # 窗口淡背景色
    WINDOW_BG = "rgba(102, 198, 255, 0.04)"

    # ── 全局 UI 基底 ──────────────────────────────────
    TOOLBAR_BG = "rgba(255, 255, 255, 0.05)"
    DIVIDER_COLOR = "rgba(255, 255, 255, 0.06)"
    HOVER_BG_STRONG = "rgba(255, 255, 255, 0.10)"
    SCROLLBAR_HANDLE_BG = "rgba(255, 255, 255, 0.20)"
    SCROLLBAR_HANDLE_HOVER_BG = "rgba(255, 255, 255, 0.30)"
    CARD_PLACEHOLDER_TEXT = "#8FA4C2"

    # ── 卡片级语义色 ──────────────────────────────────
    BUTTON_TEXT_ON_ACCENT = "#1A1F2B"
    STATUS_INFO = "#7FDBFF"
    STATUS_DANGER_BG = "rgba(255, 80, 80, 0.8)"
    STATUS_ARCHIVE_BG = "rgba(139, 92, 246, 0.8)"
    CARD_BG_DIM = "rgba(255, 255, 255, 0.04)"
    ARCHIVED_CARD_BG = "rgba(255, 180, 100, 0.08)"
    ARCHIVED_CARD_BORDER = "rgba(255, 150, 80, 0.2)"

    # ── 语法高亮色 ────────────────────────────────────
    SYNTAX_STEP = "#4EC9B0"
    SYNTAX_TOOL = "#DCDCAA"
    SYNTAX_SUCCESS = "#6A9955"
    SYNTAX_ERROR = "#F14C4C"
    SYNTAX_RESULT = "#CE9178"

    # ── 标签色 ────────────────────────────────────────
    TAG_ACCENT = "#66c6ff"
    TAG_ACCENT_TEXT = "#aae0ff"
    TAG_PURPLE = "#b388ff"
    TAG_PURPLE_TEXT = "#d1b3ff"
    TAG_ORANGE = "#ffb366"
    TAG_ORANGE_TEXT = "#ffc999"

    # accent_warm 的 Colors 映射（主题已有该值，但 Colors 未暴露）
    ACCENT_WARM = "#f59e0b"

    # 语义色
    SUCCESS = "#22c55e"
    WARNING = "#f59e0b"
    ERROR = "#ef4444"
    INFO = "#3b82f6"

    @classmethod
    def refresh(cls) -> None:
        """从 ThemeManager 同步当前主题颜色到类属性"""
        theme = current_theme()
        if not theme:
            return
        
        cls.CARD_BG = (
            theme["card_bg"].rsplit(",", 1)[0] + ", {alpha})"
            if theme["card_bg"].startswith("rgba(")
            else theme["card_bg"]
        )
        cls.CARD_BG_SOLID = theme.get("card_bg_solid", cls.CARD_BG_SOLID)
        cls.CONTENT_BG = theme.get("content_bg", cls.CONTENT_BG)
        cls.BORDER = theme.get("border", cls.BORDER)
        cls.BORDER_ACCENT = theme.get("border_accent", cls.BORDER_ACCENT)
        cls.TEXT_PRIMARY = theme.get("text_primary", cls.TEXT_PRIMARY)
        cls.TEXT_SECONDARY = theme.get("text_secondary", cls.TEXT_SECONDARY)
        cls.TEXT_SECONDARY_HOVER = theme.get("text_primary", cls.TEXT_PRIMARY)
        cls.TEXT_ACCENT = theme.get("accent", cls.TEXT_ACCENT)
        cls.TEXT_MUTED = theme.get("text_muted", cls.TEXT_MUTED)
        cls.TAB_ACTIVE_BG = theme.get("selected_bg", cls.TAB_ACTIVE_BG)
        cls.TAB_HOVER_BG = theme.get("hover_bg", cls.TAB_HOVER_BG)
        cls.HOVER_BG = theme.get("hover_bg", cls.HOVER_BG)
        cls.SELECTED_BG = theme.get("selected_bg", cls.SELECTED_BG)
        
        # 组件级颜色
        cls.USER_CARD_BG = theme.get("user_card_bg", cls.USER_CARD_BG)
        cls.USER_CARD_ACCENT = theme.get("user_card_accent", cls.USER_CARD_ACCENT)
        cls.USER_CARD_TEXT = theme.get("user_card_text", cls.USER_CARD_TEXT)
        cls.USER_CARD_MUTED = theme.get("user_card_muted", cls.USER_CARD_MUTED)
        cls.ASSISTANT_CARD_BG = theme.get("assistant_card_bg", cls.ASSISTANT_CARD_BG)
        cls.ASSISTANT_CARD_ACCENT = theme.get("assistant_card_accent", cls.ASSISTANT_CARD_ACCENT)
        cls.ASSISTANT_CARD_TEXT = theme.get("assistant_card_text", cls.ASSISTANT_CARD_TEXT)
        cls.ASSISTANT_CARD_MUTED = theme.get("assistant_card_muted", cls.ASSISTANT_CARD_MUTED)
        cls.AGENT_BTN_TEXT = theme.get("agent_btn_text", cls.AGENT_BTN_TEXT)
        cls.AGENT_BTN_TEXT_ACTIVE = theme.get("agent_btn_text_active", cls.AGENT_BTN_TEXT_ACTIVE)
        cls.AGENT_BTN_BG_ACTIVE = theme.get("agent_btn_bg_active", cls.AGENT_BTN_BG_ACTIVE)
        cls.AGENT_BTN_SEPARATOR = theme.get("agent_btn_separator", cls.AGENT_BTN_SEPARATOR)
        cls.INPUT_BG_START = theme.get("input_bg_start", cls.INPUT_BG_START)
        cls.INPUT_BG_END = theme.get("input_bg_end", cls.INPUT_BG_END)
        cls.INPUT_FOCUS_BG_START = theme.get("input_focus_bg_start", cls.INPUT_FOCUS_BG_START)
        cls.INPUT_FOCUS_BG_END = theme.get("input_focus_bg_end", cls.INPUT_FOCUS_BG_END)
        cls.INPUT_TEXT = theme.get("input_text", cls.INPUT_TEXT)
        cls.INPUT_FOCUS_TEXT = theme.get("input_focus_text", cls.INPUT_FOCUS_TEXT)
        cls.INPUT_BORDER = theme.get("input_border", cls.INPUT_BORDER)
        cls.INPUT_FOCUS_BORDER = theme.get("input_focus_border", cls.INPUT_FOCUS_BORDER)
        cls.INPUT_PLACEHOLDER = theme.get("input_placeholder", cls.INPUT_PLACEHOLDER)
        cls.CAPSULE_BG = theme.get("capsule_bg", "rgba(27, 35, 50, 180)")
        cls.CAPSULE_BORDER = theme.get("capsule_border", "rgba(43, 56, 80, 200)")
        
        # 实时卡片色
        cls.REALTIME_BORDER = theme.get("realtime_border", cls.REALTIME_BORDER)
        cls.REALTIME_ACCENT = theme.get("realtime_accent", cls.REALTIME_ACCENT)
        cls.REALTIME_ACCENT_WARM = theme.get("realtime_accent_warm", cls.REALTIME_ACCENT_WARM)
        cls.REALTIME_SUCCESS = theme.get("realtime_success", cls.REALTIME_SUCCESS)
        cls.REALTIME_ERROR = theme.get("realtime_error", cls.REALTIME_ERROR)
        cls.REALTIME_BG = theme.get("realtime_bg", cls.REALTIME_BG)
        cls.REALTIME_TEXT = theme.get("realtime_text", cls.REALTIME_TEXT)
        cls.REALTIME_TEXT_SECONDARY = theme.get("realtime_text_secondary", cls.REALTIME_TEXT_SECONDARY)
        cls.REALTIME_TAG_BG = theme.get("realtime_tag_bg", cls.REALTIME_TAG_BG)
        cls.REALTIME_TAG_BORDER = theme.get("realtime_tag_border", cls.REALTIME_TAG_BORDER)
        
        # 系统卡片色
        cls.SYSTEM_BORDER = theme.get("system_border", cls.SYSTEM_BORDER)
        cls.SYSTEM_ACCENT = theme.get("system_accent", cls.SYSTEM_ACCENT)
        
        # 发送按钮
        cls.SEND_BTN_START = theme.get("send_btn_start", cls.SEND_BTN_START)
        cls.SEND_BTN_END = theme.get("send_btn_end", cls.SEND_BTN_END)
        cls.SEND_BTN_HOVER_START = theme.get("send_btn_hover_start", cls.SEND_BTN_HOVER_START)
        cls.SEND_BTN_HOVER_END = theme.get("send_btn_hover_end", cls.SEND_BTN_HOVER_END)
        cls.SEND_BTN_RADIUS = theme.get("send_btn_radius", cls.SEND_BTN_RADIUS)
        
        # 时间线
        cls.TIMELINE_NODE = theme.get("timeline_node", cls.TIMELINE_NODE)
        cls.TIMELINE_NODE_HOVER = theme.get("timeline_node_hover", cls.TIMELINE_NODE_HOVER)
        cls.TIMELINE_NODE_VISIBLE = theme.get("timeline_node_visible", cls.TIMELINE_NODE_VISIBLE)
        cls.TIMELINE_NODE_SELECTED = theme.get("timeline_node_selected", cls.TIMELINE_NODE_SELECTED)
        cls.TIMELINE_LINE = theme.get("timeline_line", cls.TIMELINE_LINE)
        cls.TIMELINE_LINE_PROGRESS = theme.get("timeline_line_progress", cls.TIMELINE_LINE_PROGRESS)
        
        # 上下文圆环
        cls.RING_NORMAL = theme.get("ring_normal", cls.RING_NORMAL)
        cls.RING_WARNING = theme.get("ring_warning", cls.RING_WARNING)
        cls.RING_DANGER = theme.get("ring_danger", cls.RING_DANGER)
        cls.RING_COMPACTED = theme.get("ring_compacted", cls.RING_COMPACTED)
        
        # 分支标签
        cls.BRANCH_LABEL_BG = theme.get("branch_label_bg", cls.BRANCH_LABEL_BG)
        cls.BRANCH_LABEL_BORDER = theme.get("branch_label_border", cls.BRANCH_LABEL_BORDER)
        cls.WINDOW_BG = theme.get("window_bg", cls.WINDOW_BG)

        cls.ACCENT_WARM = theme.get("accent_warm", cls.ACCENT_WARM)

        # 全局 UI 基底
        cls.TOOLBAR_BG = theme.get("toolbar_bg", cls.TOOLBAR_BG)
        cls.DIVIDER_COLOR = theme.get("divider_color", cls.DIVIDER_COLOR)
        cls.HOVER_BG_STRONG = theme.get("hover_bg_strong", cls.HOVER_BG_STRONG)
        cls.SCROLLBAR_HANDLE_BG = theme.get("scrollbar_handle_bg", cls.SCROLLBAR_HANDLE_BG)
        cls.SCROLLBAR_HANDLE_HOVER_BG = theme.get("scrollbar_handle_hover_bg", cls.SCROLLBAR_HANDLE_HOVER_BG)
        cls.CARD_PLACEHOLDER_TEXT = theme.get("card_placeholder_text", cls.CARD_PLACEHOLDER_TEXT)

        # 卡片级语义色
        cls.BUTTON_TEXT_ON_ACCENT = theme.get("button_text_on_accent", cls.BUTTON_TEXT_ON_ACCENT)
        cls.STATUS_INFO = theme.get("status_info", cls.STATUS_INFO)
        cls.STATUS_DANGER_BG = theme.get("status_danger_bg", cls.STATUS_DANGER_BG)
        cls.STATUS_ARCHIVE_BG = theme.get("status_archive_bg", cls.STATUS_ARCHIVE_BG)
        cls.CARD_BG_DIM = theme.get("card_bg_dim", cls.CARD_BG_DIM)
        cls.ARCHIVED_CARD_BG = theme.get("archived_card_bg", cls.ARCHIVED_CARD_BG)
        cls.ARCHIVED_CARD_BORDER = theme.get("archived_card_border", cls.ARCHIVED_CARD_BORDER)

        # 语法高亮色
        cls.SYNTAX_STEP = theme.get("syntax_step", cls.SYNTAX_STEP)
        cls.SYNTAX_TOOL = theme.get("syntax_tool", cls.SYNTAX_TOOL)
        cls.SYNTAX_SUCCESS = theme.get("syntax_success", cls.SYNTAX_SUCCESS)
        cls.SYNTAX_ERROR = theme.get("syntax_error", cls.SYNTAX_ERROR)
        cls.SYNTAX_RESULT = theme.get("syntax_result", cls.SYNTAX_RESULT)

        # 标签色
        cls.TAG_ACCENT = theme.get("tag_accent", cls.TAG_ACCENT)
        cls.TAG_ACCENT_TEXT = theme.get("tag_accent_text", cls.TAG_ACCENT_TEXT)
        cls.TAG_PURPLE = theme.get("tag_purple", cls.TAG_PURPLE)
        cls.TAG_PURPLE_TEXT = theme.get("tag_purple_text", cls.TAG_PURPLE_TEXT)
        cls.TAG_ORANGE = theme.get("tag_orange", cls.TAG_ORANGE)
        cls.TAG_ORANGE_TEXT = theme.get("tag_orange_text", cls.TAG_ORANGE_TEXT)


# 初始化 Colors
Colors.refresh()


class BorderRadius:
    """圆角 Token"""
    SM = "4px"   # 小标签、小按钮
    MD = "8px"   # 卡片、输入框
    LG = "18px"  # 搜索框、输入区域


# ============ 动效系统 ============
class Animations:
    """动画时间与缓动 Token — 克制使用，仅关键处动效"""
    FAST_MS = 150       # 按钮按下/释放
    NORMAL_MS = 200     # 卡片淡入、过渡
    SLOW_MS = 300       # 展开/折叠

    # 缓动曲线
    EASE_OUT = "QEasingCurve::OutCubic"
    EASE_IN_OUT = "QEasingCurve::InOutQuad"

    # 位移量
    FADE_SLIDE_Y = 8   # 淡入上滑像素数


# ============ 阴影系统 ============
class Shadows:
    """阴影 Token — 通过 QGraphicsDropShadowEffect 实现"""
    # 标准卡片阴影
    CARD = {
        "blur_radius": 12,
        "offset_x": 0,
        "offset_y": 4,
        "color": "rgba(0, 0, 0, 0.25)",
    }
    # 浮动卡片阴影（更明显）
    FLOATING = {
        "blur_radius": 20,
        "offset_x": 0,
        "offset_y": 8,
        "color": "rgba(0, 0, 0, 0.35)",
    }
    # 聚焦发光
    GLOW = {
        "blur_radius": 15,
        "offset_x": 0,
        "offset_y": 0,
        "color": "rgba(201, 168, 92, 0.3)",
    }


class BorderRadius:
    """圆角 Token"""
    SM = "4px"   # 小标签、小按钮
    MD = "8px"   # 卡片、输入框
    LG = "18px"  # 搜索框、输入区域


# ============ 间距系统 ============
class Spacing:
    """间距 Token（单位：px）"""
    XS = 4
    SM = 8
    MD = 12
    LG = 16
    XL = 20
    XXL = 24


# ============ 字体系统 ============
class FontSizes:
    """字体大小 Token"""
    XS = "10px"
    SM = "11px"   # 正文、标签
    MD = "12px"   # 标题
    LG = "14px"   # 大标题


class FontWeights:
    """字重 Token"""
    NORMAL = ""
    BOLD = "bold"


# ============ 组件尺寸 ============
class Sizes:
    """组件尺寸 Token"""
    ICON_SM = QSize(12, 12)
    ICON_MD = QSize(16, 16)
    ICON_LG = QSize(20, 20)

    BUTTON_H_SM = 29  # 小按钮高度
    BUTTON_H_MD = 36  # 中按钮高度

    CARD_MIN_H = 53   # 列表项最小高度

    # ToolButton 统一规格
    TOOL_BUTTON_SZ = QSize(28, 28)
    TOOL_ICON_SZ = QSize(14, 14)

    # SwitchButton 统一规格
    SWITCH_WIDTH = 50


# ============ CSS 模板 ============
class CardStyles:
    """卡片样式模板"""
    
    @staticmethod
    def card(alpha: int = 250) -> str:
        """标准卡片样式"""
        Colors.refresh()
        return f"""
            CardWidget, SimpleCardWidget {{
                background-color: {Colors.CARD_BG.format(alpha=alpha)};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """
    
    @staticmethod
    def card_content() -> str:
        """卡片内容区样式"""
        Colors.refresh()
        return f"""
            background-color: {Colors.CONTENT_BG};
            border-radius: 6px;
        """
    
    @staticmethod
    def scroll_area() -> str:
        """滚动区域样式 — 超薄、半透明、精致"""
        Colors.refresh()
        return f"""
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {Colors.SCROLLBAR_HANDLE_BG};
                border-radius: 3px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {Colors.SCROLLBAR_HANDLE_HOVER_BG};
                width: 8px;
            }}
            QScrollBar::handle:vertical:pressed {{
                background: {Colors.SCROLLBAR_HANDLE_HOVER_BG};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """
    
    @staticmethod
    def edit_card_style() -> str:
        """统一表单输入框样式（供 mcp/hook/provider_edit/gateway 等设置卡片复用）"""
        Colors.refresh()
        from app.utils.utils import get_font_family_css
        from app.utils.design_tokens import font_size_css
        return f"""
        QWidget {{
            background: transparent;
        }}
        QLineEdit {{
            background-color: {Colors.CONTENT_BG};
            color: {Colors.TEXT_PRIMARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 4px;
            padding: 4px 8px;
            {get_font_family_css()}
            {font_size_css(12)}
        }}
        QLineEdit:focus {{
            border-color: {Colors.INPUT_FOCUS_BORDER};
        }}
        QLineEdit::placeholder {{
            color: {Colors.INPUT_PLACEHOLDER};
        }}
        QPlainTextEdit {{
            background-color: {Colors.CONTENT_BG};
            color: {Colors.TEXT_PRIMARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 4px;
            padding: 4px 8px;
            {get_font_family_css()}
            {font_size_css(12)}
        }}
        QPlainTextEdit:focus {{
            border-color: {Colors.INPUT_FOCUS_BORDER};
        }}
        """
    
    @staticmethod
    def title_icon(emoji: str = "⚙️") -> str:
        """标题图标样式（返回 emoji）"""
        return emoji
    
    @staticmethod
    def title_label() -> str:
        """标题文字样式"""
        Colors.refresh()
        return f"color: {Colors.TEXT_ACCENT};"
    
    @staticmethod
    def close_button() -> str:
        """关闭按钮样式"""
        return "color: #888888; cursor: pointer; padding: 4px;"


class TabStyles:
    """标签样式模板"""
    
    @staticmethod
    def active() -> str:
        Colors.refresh()
        return f"""
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {font_size_css(11)}
                font-weight: bold;
                padding: 3px 8px;
                border-radius: 4px;
                background-color: {Colors.TAB_ACTIVE_BG};
                font-family: '{_get_global_font()}';
            }}
        """

    @staticmethod
    def inactive() -> str:
        Colors.refresh()
        return f"""
            QLabel {{
                color: {Colors.TEXT_SECONDARY};
                {font_size_css(11)}
                padding: 3px 8px;
                border-radius: 4px;
                cursor: pointer;
                font-family: '{_get_global_font()}';
            }}
            QLabel:hover {{
                color: {Colors.TEXT_PRIMARY};
                background-color: {Colors.TAB_HOVER_BG};
            }}
        """


class ItemStyles:
    """列表项样式模板"""
    
    @staticmethod
    def radio_button() -> str:
        """单选按钮样式"""
        return """
            QRadioButton::indicator {
                width: 16px;
                height: 16px;
                border-radius: 8px;
                border: 2px solid #8e8e8e;
                background-color: transparent;
            }
            QRadioButton::indicator:checked {
                border: 2px solid #0078d4;
                background-color: #0078d4;
            }
        """
    
    @staticmethod
    def tag() -> str:
        """标签样式"""
        return """
            color: #fff; 
            font-weight: bold; 
            background-color: rgba(102, 198, 255, 0.35); 
            border-radius: 4px; 
            padding: 2px 8px;
        """


class ButtonStyles:
    """按钮统一样式模板"""

    @staticmethod
    def tool_button() -> str:
        """ToolButton 透明背景样式"""
        return "background-color: transparent; border-radius: 4px;"

    @staticmethod
    def primary_action() -> str:
        """主操作按钮样式（用于 ManualUpdateCard 等）"""
        return f"""
            PrimaryPushButton {{
                background-color: #0078d4;
                color: #ffffff;
                border: none;
                border-radius: 5px;
                padding: 5px 16px;
                {font_size_css(13)}
                font-weight: bold;
            }}
            PrimaryPushButton:hover {{
                background-color: {Colors.BORDER_ACCENT};
            }}
            PrimaryPushButton:pressed {{
                background-color: {Colors.SELECTED_BG};
            }}
            PrimaryPushButton:disabled {{
                background-color: #444;
                color: #888;
            }}
        """


class SwitchStyles:
    """开关统一样式模板"""

    @staticmethod
    def configure(switch) -> None:
        """统一配置 SwitchButton：无文字标签 + 固定宽度"""
        switch.setOnText("")
        switch.setOffText("")
        switch.setFixedWidth(Sizes.SWITCH_WIDTH)


class ComboBoxStyles:
    """下拉框统一样式模板"""

    @staticmethod
    def dark_combo() -> str:
        """深色主题下拉框样式"""
        return f"""
            QComboBox {{
                color: {Colors.TEXT_PRIMARY};
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 5px;
                padding: 5px 12px 5px 10px;
                min-height: 28px;
                {font_size_css(12)}
            }}
            QComboBox:hover {{
                border: 1px solid {Colors.TEXT_ACCENT};
                background-color: {Colors.HOVER_BG};
            }}
            QComboBox:focus {{
                border: 1px solid {Colors.TEXT_ACCENT};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
                subcontrol-origin: padding;
                subcontrol-position: right center;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #888888;
                width: 0px;
                height: 0px;
                margin-right: 4px;
            }}
            QComboBox::down-arrow:hover {{
                border-top-color: {Colors.TEXT_ACCENT};
            }}
        """

    @staticmethod
    def dark_combo_dropdown() -> str:
        """深色主题下拉框弹出列表样式"""
        return f"""
            QAbstractItemView {{
                color: {Colors.TEXT_PRIMARY};
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px;
                outline: none;
                show-decoration-selected: 1;
            }}
            QAbstractItemView::item {{
                padding: 6px 14px 6px 12px;
                min-height: 36px;
                border-radius: 3px;
            }}
            QAbstractItemView::item:hover {{
                background-color: {Colors.HOVER_BG};
            }}
            QAbstractItemView::item:selected {{
                background-color: {Colors.TEXT_ACCENT};
                color: white;
            }}
            QScrollBar:vertical {{
                background: {Colors.CONTENT_BG};
                border: none;
                width: 14px;
                margin: 4px 2px 4px 2px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """


# ============ 便捷函数 ============
def get_card_style(alpha: int = 250) -> str:
    """获取卡片样式字符串"""
    return CardStyles.card(alpha)


def get_scroll_style() -> str:
    """获取滚动区域样式字符串"""
    return CardStyles.scroll_area()


def get_content_bg_style() -> str:
    """获取内容区背景样式"""
    return f"""
        background-color: {Colors.CONTENT_BG};
        border-radius: 6px;
    """


def fade_in_widget(widget, duration: int = Animations.NORMAL_MS):
    """为 widget 添加淡入动画（透明度 0→1），简洁克制"""
    from PyQt5.QtWidgets import QGraphicsOpacityEffect
    from PyQt5.QtCore import QPropertyAnimation

    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.start()
    # 保持引用防止被回收
    widget._fade_anim = anim


def apply_card_shadow(widget, shadow_type: str = "card"):
    """为 widget 添加预设阴影效果
    
    Args:
        widget: 目标控件
        shadow_type: "card" | "floating" | "glow"
    """
    from PyQt5.QtWidgets import QGraphicsDropShadowEffect
    from PyQt5.QtGui import QColor

    config = getattr(Shadows, shadow_type.upper(), Shadows.CARD)
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(config["blur_radius"])
    effect.setOffset(config["offset_x"], config["offset_y"])
    effect.setColor(QColor(config["color"]))
    widget.setGraphicsEffect(effect)


# 从 utils 导入字体家族 CSS 函数供复用
from app.utils.utils import get_font_family_css