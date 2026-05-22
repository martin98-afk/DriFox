# -*- coding: utf-8 -*-
"""
统一的设计系统 - Design Tokens 和样式常量
所有 UI 组件应引用此模块以保持视觉一致性
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
}

THEME_STYLE_OPTIONS = {}  # 在模块底部动态构建


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



def _build_theme_options() -> dict:
    """从 ThemeManager 构建 THEME_STYLE_OPTIONS 兼容格式"""
    result = {}
    for tid, name in theme_manager.list_themes().items():
        theme = theme_manager.get_theme(tid)
        if not theme:
            continue
        colors = theme.get("colors", {})
        window = theme.get("window", {})
        entry = {
            "label": name,
            "window_start": window.get("gradient_start", "rgba(10, 14, 22, 255)"),
            "window_end": window.get("gradient_end", "rgba(15, 20, 30, 255)"),
        }
        for k, v in colors.items():
            entry[k] = v
        result[tid] = entry
    return result

def get_theme_style_key() -> str:
    try:
        from app.utils.config import Settings
        key = Settings.get_instance().ui_theme_style.value
    except Exception:
        key = "midnight"
    return key if key in THEME_STYLE_OPTIONS else "midnight"


def current_theme() -> dict:
    """获取当前主题的扁平 colors 字典，始终从 ThemeManager 实时读取"""
    from app.utils.theme_manager import theme_manager
    colors = theme_manager.get_current_colors()
    if colors:
        return colors
    # fallback：使用 THEME_STYLE_OPTIONS
    key = get_theme_style_key()
    if key in THEME_STYLE_OPTIONS:
        return THEME_STYLE_OPTIONS[key]
    return THEME_STYLE_OPTIONS.get("midnight", {})


def get_window_style() -> str:
    from app.utils.theme_manager import theme_manager
    window = theme_manager.get_theme_window(theme_manager.get_current_theme_id())
    return f"""
    #OpenAIChatToolWindow {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 {window.get('gradient_start', 'rgba(10, 14, 22, 255)')},
            stop:1 {window.get('gradient_end', 'rgba(15, 20, 30, 255)')});
    }}
    """


def get_capsule_style() -> str:
    theme = current_theme()
    return f"""
        background: {theme["capsule_bg"]};
        border: 1px solid {theme["capsule_border"]};
        border-radius: 12px;
    """


# ============ 颜色系统 ============
class Colors:
    """颜色 Token"""
    # 主背景
    CARD_BG = "rgba(33, 33, 38, {alpha})"  # 卡片背景，alpha 可配置
    CARD_BG_SOLID = "rgba(33, 33, 38, 250)"  # 固定透明度版本
    
    # 内容区背景
    CONTENT_BG = "#2a2a2e"
    
    # 边框
    BORDER = "#3d3d3d"
    BORDER_ACCENT = "#f59e0b"  # 强调边框（如工具折叠框）
    
    # 文字颜色
    TEXT_PRIMARY = "#ffffff"
    TEXT_SECONDARY = "rgba(255, 255, 255, 0.5)"
    TEXT_SECONDARY_HOVER = "rgba(255, 255, 255, 0.8)"
    TEXT_ACCENT = "#f59e0b"  # 标题强调色
    TEXT_MUTED = "#888888"
    
    # 标签颜色
    TAB_ACTIVE_BG = "rgba(102, 198, 255, 0.3)"
    TAB_INACTIVE = "rgba(255, 255, 255, 0.5)"
    TAB_HOVER_BG = "rgba(255, 255, 255, 0.1)"
    
    # 交互状态
    HOVER_BG = "rgba(255, 255, 255, 0.08)"
    SELECTED_BG = "rgba(102, 198, 255, 0.35)"
    
    # === 组件级颜色 ===
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

    # === 实时卡片色（对话类：todo/tool/question/sub_agent）===
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

    # === 系统卡片色 ===
    SYSTEM_BORDER = "#3d4a60"
    SYSTEM_ACCENT = "#66c6ff"


    # === 新增主题属性 ===
    SEND_BTN_START = "#C9A85C"
    SEND_BTN_END = "#B8956A"
    SEND_BTN_HOVER_START = "#D4B878"
    SEND_BTN_HOVER_END = "#C9A060"

    TIMELINE_NODE = "#5A5A5A"
    TIMELINE_NODE_HOVER = "#6BA3FF"
    TIMELINE_NODE_VISIBLE = "#00FF7F"
    TIMELINE_NODE_SELECTED = "#FFA500"
    TIMELINE_LINE = "#3A3A3A"
    TIMELINE_LINE_PROGRESS = "#00FF7F"

    RING_NORMAL = "#5aa9ff"
    RING_WARNING = "#f6c453"
    RING_DANGER = "#ff6b6b"
    RING_COMPACTED = "#9b59b6"

    BRANCH_LABEL_BG = "rgba(102, 198, 255, 0.15)"
    BRANCH_LABEL_BORDER = "rgba(102, 198, 255, 0.3)"

    # 窗口淡背景色
    WINDOW_BG = "rgba(102, 198, 255, 0.04)"

    # 语义色
    SUCCESS = "#22c55e"
    WARNING = "#f59e0b"
    ERROR = "#ef4444"
    INFO = "#3b82f6"

    @classmethod
    def refresh(cls) -> None:
        theme = current_theme()
        cls.CARD_BG = (
            theme["card_bg"].rsplit(",", 1)[0] + ", {alpha})"
            if theme["card_bg"].startswith("rgba(")
            else theme["card_bg"]
        )
        cls.CARD_BG_SOLID = theme["card_bg_solid"]
        cls.CONTENT_BG = theme["content_bg"]
        cls.BORDER = theme["border"]
        cls.BORDER_ACCENT = theme["border_accent"]
        cls.TEXT_PRIMARY = theme["text_primary"]
        cls.TEXT_SECONDARY = theme["text_secondary"]
        cls.TEXT_SECONDARY_HOVER = theme["text_primary"]
        cls.TEXT_ACCENT = theme["accent"]
        cls.TEXT_MUTED = theme["text_muted"]
        cls.TAB_ACTIVE_BG = theme["selected_bg"]
        cls.TAB_HOVER_BG = theme["hover_bg"]
        cls.HOVER_BG = theme["hover_bg"]
        cls.SELECTED_BG = theme["selected_bg"]
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
        # 新增主题属性
        cls.SEND_BTN_START = theme.get("send_btn_start", cls.SEND_BTN_START)
        cls.SEND_BTN_END = theme.get("send_btn_end", cls.SEND_BTN_END)
        cls.SEND_BTN_HOVER_START = theme.get("send_btn_hover_start", cls.SEND_BTN_HOVER_START)
        cls.SEND_BTN_HOVER_END = theme.get("send_btn_hover_end", cls.SEND_BTN_HOVER_END)

        cls.TIMELINE_NODE = theme.get("timeline_node", cls.TIMELINE_NODE)
        cls.TIMELINE_NODE_HOVER = theme.get("timeline_node_hover", cls.TIMELINE_NODE_HOVER)
        cls.TIMELINE_NODE_VISIBLE = theme.get("timeline_node_visible", cls.TIMELINE_NODE_VISIBLE)
        cls.TIMELINE_NODE_SELECTED = theme.get("timeline_node_selected", cls.TIMELINE_NODE_SELECTED)
        cls.TIMELINE_LINE = theme.get("timeline_line", cls.TIMELINE_LINE)
        cls.TIMELINE_LINE_PROGRESS = theme.get("timeline_line_progress", cls.TIMELINE_LINE_PROGRESS)

        cls.RING_NORMAL = theme.get("ring_normal", cls.RING_NORMAL)
        cls.RING_WARNING = theme.get("ring_warning", cls.RING_WARNING)
        cls.RING_DANGER = theme.get("ring_danger", cls.RING_DANGER)
        cls.RING_COMPACTED = theme.get("ring_compacted", cls.RING_COMPACTED)

        cls.BRANCH_LABEL_BG = theme.get("branch_label_bg", cls.BRANCH_LABEL_BG)
        cls.BRANCH_LABEL_BORDER = theme.get("branch_label_border", cls.BRANCH_LABEL_BORDER)
        cls.WINDOW_BG = theme.get("window_bg", cls.WINDOW_BG)



# 确保 THEME_STYLE_OPTIONS 被构建
THEME_STYLE_OPTIONS = _build_theme_options()
Colors.refresh()


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
        """滚动区域样式"""
        return """
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollArea > QWidget > QWidget {
                background: transparent;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.2);
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.3);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
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


# 从 utils 导入字体家族 CSS 函数供复用
from app.utils.utils import get_font_family_css
