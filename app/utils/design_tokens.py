# -*- coding: utf-8 -*-
"""
统一的设计系统 - Design Tokens 和样式常量
所有 UI 组件应引用此模块以保持视觉一致性

主题完全从 app/themes/ 目录读取，不硬编码主题数据
"""

from PyQt5.QtCore import QSize


# ─── 字体/字号缓存 ──────────────────────────────────────────
# 渲染热路径中 scale_font_size 每帧调用 15-50 次，
# get_font_family_css / _get_global_font 同样高频。
# 字体设置仅在用户更改配置时变化，缓存后在设置变更时失效即可。
_cached_font_family: str | None = None
_cached_font_size_key: str | None = None
_cached_font_size_delta: int = 0
_cached_font_size_base: int = 14


def invalidate_font_cache() -> None:
    """字体/字号缓存失效（在设置变更回调中调用）"""
    global _cached_font_family, _cached_font_size_key, _cached_font_size_delta, _cached_font_size_base
    _cached_font_family = None
    _cached_font_size_key = None
    _cached_font_size_delta = 0
    _cached_font_size_base = 14


def _get_global_font() -> str:
    """获取全局字体名称，用于样式表（缓存）"""
    global _cached_font_family
    if _cached_font_family is not None:
        return _cached_font_family
    try:
        from app.utils.config import Settings

        _cached_font_family = Settings.get_instance().llm_font_family.value
    except Exception:
        try:
            _cached_font_family = Settings.get_instance().canvas_font_selected.value
        except Exception:
            _cached_font_family = "Segoe UI"
    return _cached_font_family


def _get_font_family_css() -> str:
    """懒导入 get_font_family_css，避免 app.utils.utils 模块级加载（含 pypinyin 等重型包）"""
    from app.utils.utils import get_font_family_css

    return get_font_family_css()


# 界面字号档位：delta 键（-5px..+10px，步进 1），base 恒 14，实际字号 = 14 + delta
FONT_SIZE_OPTIONS = {str(d): {"delta": d, "base": 14} for d in range(-5, 11)}

# 旧档位键（small/medium/large/superlarge）→ delta 键迁移映射
_LEGACY_FONT_SIZE_KEYS = {"small": "-1", "medium": "0", "large": "2", "superlarge": "4"}

# 默认档位（对应旧 large：14+2=16px）
_DEFAULT_FONT_SIZE_KEY = "2"


def get_ui_font_size_key() -> str:
    global _cached_font_size_key, _cached_font_size_delta, _cached_font_size_base
    if _cached_font_size_key is not None:
        return _cached_font_size_key
    try:
        from app.utils.config import Settings

        key = Settings.get_instance().ui_font_size.value
    except Exception:
        key = _DEFAULT_FONT_SIZE_KEY
    # 旧配置值迁移兜底（validator correct 已处理，此处双保险）
    key = _LEGACY_FONT_SIZE_KEYS.get(key, key)
    if key not in FONT_SIZE_OPTIONS:
        key = _DEFAULT_FONT_SIZE_KEY
    _cached_font_size_key = key
    _cached_font_size_delta = FONT_SIZE_OPTIONS[key]["delta"]
    _cached_font_size_base = FONT_SIZE_OPTIONS[key]["base"]
    return key


def get_ui_font_size() -> int:
    """获取当前配置的基础字体大小（未缩放）"""
    get_ui_font_size_key()  # 确保缓存已填充
    return _cached_font_size_base


def scale_font_size(size: int) -> int:
    # 确保缓存已填充（首次调用时），后续直接用缓存值
    if _cached_font_size_key is None:
        get_ui_font_size_key()
    return max(8, int(size) + _cached_font_size_delta)


def scale_icon_size(size: int) -> int:
    """图标大小随系统字体缩放

    使用与字体相同的 delta 缩放量，保证图标与文字比例协调。
    """
    if _cached_font_size_key is None:
        get_ui_font_size_key()
    return max(8, int(size) + _cached_font_size_delta)


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

    # ── 单次遍历，同时完成：setFont + 按类型分类 ──
    # 替代之前 4 次独立 findChildren（QWidget / SettingCard / ExpandSettingCard / SwitchButton）
    from qfluentwidgets.components.settings.setting_card import SettingCard
    from qfluentwidgets.components.settings.expand_setting_card import ExpandSettingCard
    from qfluentwidgets.components.widgets.switch_button import SwitchButton

    setting_cards = []
    switches = []

    for child in widget.findChildren(QWidget):
        # setFont 覆盖递归字体
        # [PERF] 仅当像素大小/字族与目标不一致时才 setFont：
        # 全树可达数千控件（长会话消息卡），无变化时 setFont 仍触发
        # Qt 内部样式重算（实测 5786 次 ≈ 200ms），跳过可大幅提速。
        cf = child.font()
        if cf.pixelSize() == scaled and cf.family() == font_family:
            pass
        else:
            cf.setPixelSize(scaled)
            cf.setFamily(font_family)
            child.setFont(cf)

        # 分类：后续 stylesheet 覆盖仅对特定类型执行
        if isinstance(child, SettingCard):
            setting_cards.append(child)
        if isinstance(child, SwitchButton):
            switches.append(child)

    # ── SettingCard / ExpandSettingCard ──
    # ExpandSettingCard 继承 SettingCard，已被 setting_cards 包含
    for card in setting_cards:
        card.titleLabel.setStyleSheet(f"QLabel {{ font-size: {scaled}px; font-family: '{font_family}'; }}")
        card.contentLabel.setStyleSheet(
            f"QLabel#contentLabel {{ font-size: {content_scaled}px; font-family: '{font_family}'; }}"
        )

        # ExpandSettingCard 内部的 HeaderSettingCard 需额外覆盖
        if isinstance(card, ExpandSettingCard):
            if hasattr(card, "card") and hasattr(card.card, "titleLabel"):
                card.card.titleLabel.setStyleSheet(
                    f"QLabel#titleLabel {{ font-size: {scaled}px; font-family: '{font_family}'; }}"
                )
            if hasattr(card, "card") and hasattr(card.card, "contentLabel"):
                card.card.contentLabel.setStyleSheet(
                    f"QLabel#contentLabel {{ font-size: {content_scaled}px; font-family: '{font_family}'; }}"
                )

    # ── SwitchButton ──
    for switch in switches:
        switch.setStyleSheet(f"SwitchButton>QLabel {{ font-size: {scaled}px; font-family: '{font_family}'; }}")


def current_theme() -> dict:
    """获取当前主题的扁平 colors 字典"""
    from app.utils.theme_manager import theme_manager

    return theme_manager.get_current_colors()


def get_window_style() -> str:
    """获取窗口渐变背景样式"""
    from app.utils.theme_manager import theme_manager

    window = theme_manager.get_theme_window(theme_manager.get_current_theme_id())
    return f"""
    #OpenAIChatToolWindow {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 {window.get("gradient_start", "rgba(10, 14, 22, 255)")},
            stop:1 {window.get("gradient_end", "rgba(15, 20, 30, 255)")});
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


# ============ 发光预设（glow presets）============
# 每个预设一组 token，**只控制发光强度（ambient/primary/unfocused）**，
# **不控制颜色** —— 颜色由主题 yaml 自己的 `input_focus_border` 决定。
# 主题 yaml 中通过 `input_glow_preset: "breath"` 切换；不写则用类级默认值（保留原观感）。
#
# 设计原则：
# - subtle  : 淡光，alpha 最低 + blur 最小，金属边缘的微弱反射
# - breath  : 聚焦光 + 失焦态微光，焦点切换如"由弱到强"，奢华感来自持续呼吸
# - platinum: 冷白金，去除暖色印象，最现代
# - ember   : 四档中最亮（强度高），给习惯高调观感的用户
#
# 字段顺序：先填 INPUT_GLOW_AMBIENT_* 后填 UNFOCUSED_* —— 前者控制聚焦态光效强度，
# 后者决定失焦态是否保留微光。颜色一律跟随主题的 `input_focus_border`。
GLOW_PRESETS = {
    "subtle": {
        "input_glow_primary_alpha": 0,
        "input_glow_primary_blur": 0,
        "input_glow_ambient_alpha": 35,
        "input_glow_ambient_blur": 18,
        "input_glow_unfocused_ambient_alpha": 0,
        "input_glow_unfocused_ambient_blur": 0,
    },
    "breath": {
        "input_glow_primary_alpha": 0,
        "input_glow_primary_blur": 0,
        "input_glow_ambient_alpha": 65,
        "input_glow_ambient_blur": 30,
        "input_glow_unfocused_ambient_alpha": 38,
        "input_glow_unfocused_ambient_blur": 30,
    },
    "platinum": {
        "input_glow_primary_alpha": 0,
        "input_glow_primary_blur": 0,
        "input_glow_ambient_alpha": 55,
        "input_glow_ambient_blur": 26,
        "input_glow_unfocused_ambient_alpha": 0,
        "input_glow_unfocused_ambient_blur": 0,
    },
    "ember": {
        "input_glow_primary_alpha": 0,
        "input_glow_primary_blur": 0,
        "input_glow_ambient_alpha": 70,
        "input_glow_ambient_blur": 30,
        "input_glow_unfocused_ambient_alpha": 18,
        "input_glow_unfocused_ambient_blur": 20,
    },
}


# ============ 颜色系统 ============
class Colors:
    """颜色 Token - 动态从 ThemeManager 读取"""

    # 缓存：记录上一次实际刷新的主题条目，避免 100+ 次冗余调用
    # 同一个主题文件加载期内，Colors.refresh() 只执行一次有效刷新
    _cached_theme_items: Optional[frozenset] = None

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

    # 聚焦发光 halo cascade — 各主题可单独微调 alpha / blur，
    # 实现"主光 → 环境光晕"的个性化光效。
    # 默认值取自 Shadows.GLOW_PRIMARY / GLOW_AMBIENT。
    GLOW_PRIMARY_ALPHA = 195
    GLOW_PRIMARY_BLUR = 26
    GLOW_AMBIENT_ALPHA = 110
    GLOW_AMBIENT_BLUR = 36

    # 输入卡双层 halo：主光（紧致） + 环境光（弥散）
    # 输入卡自带主光 + wrapper 带环境光，叠加形成"核心亮→柔光晕开"层次
    INPUT_GLOW_PRIMARY_BLUR = 18
    INPUT_GLOW_PRIMARY_ALPHA = 220
    INPUT_GLOW_AMBIENT_BLUR = 42
    INPUT_GLOW_AMBIENT_ALPHA = 80

    # 失焦态发光（默认 0 = 失焦完全关闭；glow preset 如 breath 会改写）
    # 失焦态保留微光能营造"持续呼吸"的奢华感，焦点切换不再是硬开关
    INPUT_GLOW_UNFOCUSED_AMBIENT_BLUR = 0
    INPUT_GLOW_UNFOCUSED_AMBIENT_ALPHA = 0

    # 底部工具栏条（与输入卡片解耦的第二张卡，独立 token 以便主题分别调控）
    TOOLBAR_STRIP_BG = "rgba(24, 31, 45, 150)"
    TOOLBAR_STRIP_BORDER = "#2B3850"

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
    STATUS_DANGER_BG_DARK = "#8B4A4A"  # 暗红（工具按钮用）
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
    TAG_GREEN = "#34d399"
    TAG_GREEN_TEXT = "#6ee7b7"

    # accent_warm 的 Colors 映射（主题已有该值，但 Colors 未暴露）
    ACCENT_WARM = "#f59e0b"

    # 语义色
    SUCCESS = "#22c55e"
    SUCCESS_DARK = "#3D7A5A"  # 暗绿（工具按钮用）
    WARNING = "#f59e0b"
    ERROR = "#ef4444"
    INFO = "#3b82f6"

    # 以下两个 attr 在 refresh() 中通过 theme.get() 设置，但类定义中缺少默认值
    CAPSULE_BG = "rgba(27, 35, 50, 180)"
    CAPSULE_BORDER = "rgba(43, 56, 80, 200)"

    # ── 颜色映射表 ──────────────────────────────────────
    # 约定：Colors 属性名 = YAML key 的 UPPER_CASE（下划线分割一致）
    # 以下列出非标准映射（YAML key → 不同的 Colors 属性名）。
    # 标准 1:1 映射由 refresh() 自动派生，无需在此声明。
    _COLOR_ALIASES = {
        "TEXT_ACCENT": "accent",  # YAML "accent" → Colors.TEXT_ACCENT
        "TAB_ACTIVE_BG": "selected_bg",  # YAML "selected_bg" → Colors.TAB_ACTIVE_BG
        "TAB_HOVER_BG": "hover_bg",  # YAML "hover_bg" → Colors.TAB_HOVER_BG
        "TEXT_SECONDARY_HOVER": "text_primary",  # YAML "text_primary" → Colors.TEXT_SECONDARY_HOVER
    }

    # Colors 属性名白名单 — 仅有这些属性经由主题 YAML 填充。
    # 不在白名单内的属性（如 SUCCESS, WARNING, TAB_INACTIVE 等）始终保持类级默认值。
    _THEME_SOURCED_ATTRS = None  # 懒加载，见 _get_theme_sourced_attrs()

    # 主题 YAML 中不作为颜色值的顶层 key（跳过）
    _SKIP_YAML_KEYS = frozenset({"name", "id", "window", "background", "input_glow_preset"})

    @classmethod
    def _get_theme_sourced_attrs(cls) -> frozenset:
        """获取所有应该由主题 YAML 填充的 Colors 属性名"""
        if cls._THEME_SOURCED_ATTRS is not None:
            return cls._THEME_SOURCED_ATTRS

        # 别名映射中的 attr
        aliased = set(cls._COLOR_ALIASES.keys())
        # 1:1 映射：从类属性中筛选出命名符合约定且不在跳过列表中的
        direct = set()
        for attr_name in dir(cls):
            if attr_name.startswith("_"):
                continue
            if attr_name.isupper() and attr_name not in aliased:
                yaml_key = attr_name.lower()
                if yaml_key not in cls._SKIP_YAML_KEYS:
                    direct.add(attr_name)
        # 排除非颜色值的类属性
        EXCLUDE = {"SUCCESS", "WARNING", "ERROR", "INFO", "TAB_INACTIVE"}
        cls._THEME_SOURCED_ATTRS = frozenset((direct | aliased) - EXCLUDE)
        return cls._THEME_SOURCED_ATTRS

    @classmethod
    def refresh(cls) -> None:
        """从 ThemeManager 同步当前主题颜色到类属性

        幂等缓存：同一份 theme dict 仅执行一次有效刷新，
        后续冗余调用（widget 初始化时普遍模式）直接跳过。
        """
        theme = current_theme()
        if not theme:
            return

        # 幂等检查：主题 dict 未变化时跳过刷新
        current_items = frozenset(theme.items())
        if cls._cached_theme_items == current_items:
            return
        cls._cached_theme_items = current_items

        # 1. 特殊处理：CARD_BG 需要 {alpha} 模板
        if "card_bg" in theme:
            cls.CARD_BG = (
                theme["card_bg"].rsplit(",", 1)[0] + ", {alpha})"
                if theme["card_bg"].startswith("rgba(")
                else theme["card_bg"]
            )

        # 2. 1:1 映射：yaml_key → Colors.UPPER(yaml_key)
        sourced = cls._get_theme_sourced_attrs()
        for yaml_key, val in theme.items():
            if yaml_key in cls._SKIP_YAML_KEYS:
                continue
            if yaml_key == "card_bg":
                continue  # 已在上方处理
            attr = yaml_key.upper()
            if attr in sourced:
                setattr(cls, attr, val)

        # 3. 别名映射：yaml_key → 非标准 Colors 属性名
        for attr, yaml_key in cls._COLOR_ALIASES.items():
            if yaml_key in theme:
                setattr(cls, attr, theme[yaml_key])

        # 4. 发光预设（最后执行，会覆盖已设置的单个发光 token）
        preset_name = theme.get("input_glow_preset")
        if preset_name:
            if not cls.apply_glow_preset(preset_name):
                import warnings

                warnings.warn(
                    f"[design_tokens] Unknown input_glow_preset: {preset_name!r} "
                    f"(valid: {sorted(GLOW_PRESETS.keys())}); falling back to class defaults."
                )

        # 5. 刷新全局 tooltip 样式（跟随主题）
        _apply_tooltip_style()

    @classmethod
    def apply_glow_preset(cls, preset_name: str) -> bool:
        """应用发光预设：**只覆盖发光强度 token，不覆盖 INPUT_FOCUS_BORDER**

        INPUT_FOCUS_BORDER（颜色）由主题 yaml 自己负责，预设不介入。
        这样主题可以自由组合"颜色 + 强度"，例如辐射绿 fallback + ember 强度。

        Args:
            preset_name: GLOW_PRESETS 的 key（subtle / breath / platinum / ember）

        Returns:
            True 应用成功；False 预设名无效（调用方应降级到类级默认值）
        """
        preset = GLOW_PRESETS.get(preset_name)
        if preset is None:
            return False
        cls.INPUT_GLOW_PRIMARY_ALPHA = preset.get("input_glow_primary_alpha", cls.INPUT_GLOW_PRIMARY_ALPHA)
        cls.INPUT_GLOW_PRIMARY_BLUR = preset.get("input_glow_primary_blur", cls.INPUT_GLOW_PRIMARY_BLUR)
        cls.INPUT_GLOW_AMBIENT_ALPHA = preset.get("input_glow_ambient_alpha", cls.INPUT_GLOW_AMBIENT_ALPHA)
        cls.INPUT_GLOW_AMBIENT_BLUR = preset.get("input_glow_ambient_blur", cls.INPUT_GLOW_AMBIENT_BLUR)
        cls.INPUT_GLOW_UNFOCUSED_AMBIENT_ALPHA = preset.get(
            "input_glow_unfocused_ambient_alpha",
            cls.INPUT_GLOW_UNFOCUSED_AMBIENT_ALPHA,
        )
        cls.INPUT_GLOW_UNFOCUSED_AMBIENT_BLUR = preset.get(
            "input_glow_unfocused_ambient_blur",
            cls.INPUT_GLOW_UNFOCUSED_AMBIENT_BLUR,
        )
        return True


# Colors.refresh() 已延迟到首次调用时执行（ThemeManager 懒加载），
# 在 setup_ui 等入口点由显式 Colors.refresh() 调用触发。
# 移除模块级调用以加速 import 阶段。
# Colors 类默认值（暗色主题）在 theme 加载前即可安全使用。


class BorderRadius:
    """圆角 Token"""

    SM = "4px"  # 小标签、小按钮
    MD = "8px"  # 卡片、输入框
    LG = "18px"  # 搜索框、输入区域


# ============ 动效系统 ============
class Animations:
    """动画时间与缓动 Token — 克制使用，仅关键处动效"""

    FAST_MS = 150  # 按钮按下/释放
    NORMAL_MS = 200  # 卡片淡入、过渡
    SLOW_MS = 300  # 展开/折叠

    # 缓动曲线
    EASE_OUT = "QEasingCurve::OutCubic"
    EASE_IN_OUT = "QEasingCurve::InOutQuad"

    # 位移量
    FADE_SLIDE_Y = 8  # 淡入上滑像素数


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
    # ===== 聚焦发光（halo cascade — 主光 + 环境光晕双层 token）=====
    # 焦点态时输入卡 + 工具栏一起发光，构成"发光胶囊"。
    # 两者同色系（取自 Colors.INPUT_FOCUS_BORDER，主题感知），
    # 通过 alpha / blur 的差异营造"主光 → 回声"的层次：
    # 上紧下散、上亮下柔，不抢戏也不脱节。
    #
    # 注意：GLOW_* 系列不携带 color 字段 — 颜色由调用方从
    # Colors.INPUT_FOCUS_BORDER 读取，alpha 由 token 显式声明。
    # 这样主题切换时颜色自动跟随，无需维护两套 rgba 字面量。

    # 聚焦主光源 — 输入卡等"活动"控件的辉光
    # alpha 较高、blur 紧凑 → 收紧、聚焦，是胶囊的"光源"
    GLOW_PRIMARY = {
        "blur_radius": 26,
        "offset_x": 0,
        "offset_y": 0,
        "alpha": 195,
    }
    # 聚焦环境光晕 — 工具栏等"次级"控件的余光
    # alpha 较主光源低 ~44%、blur 较主光源宽 ~38%
    # → 弥散、柔和，像主光"洒"过来的余晖
    GLOW_AMBIENT = {
        "blur_radius": 36,
        "offset_x": 0,
        "offset_y": 0,
        "alpha": 110,
    }
    # 兼容旧名（历史别名，等价于 GLOW_PRIMARY）
    GLOW = GLOW_PRIMARY

    # 输入卡双层 halo 专用
    INPUT_GLOW_PRIMARY = {
        "blur_radius": 18,
        "offset_x": 0,
        "offset_y": 0,
        "alpha": 220,
    }
    INPUT_GLOW_AMBIENT = {
        "blur_radius": 42,
        "offset_x": 0,
        "offset_y": 0,
        "alpha": 80,
    }


class BorderRadius:
    """圆角 Token"""

    SM = "4px"  # 小标签、小按钮
    MD = "8px"  # 卡片、输入框
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
    SM = "11px"  # 正文、标签
    MD = "12px"  # 标题
    LG = "14px"  # 大标题


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

    CARD_MIN_H = 53  # 列表项最小高度

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
            {_get_font_family_css()}
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
            {_get_font_family_css()}
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

    @staticmethod
    def apply(combo_box, include_dropdown: bool = True) -> None:
        """一站式下拉框样式应用（统一入口）

        自动挂载主题感知样式 + 下拉列表样式，避免各组件重复实现。
        Args:
            combo_box: 目标 QComboBox 控件
            include_dropdown: 是否同时设置弹出列表样式（默认 True）
        """
        Colors.refresh()
        combo_box.setStyleSheet(ComboBoxStyles.dark_combo())
        if include_dropdown and hasattr(combo_box, "view") and combo_box.view() is not None:
            combo_box.view().setStyleSheet(ComboBoxStyles.dark_combo_dropdown())


# ============ 便捷函数 ============
def get_card_style(alpha: int = 250) -> str:
    """获取卡片样式字符串"""
    return CardStyles.card(alpha)


def get_scroll_style() -> str:
    """获取滚动区域样式字符串"""
    return CardStyles.scroll_area()


def get_unified_scrollbar_style(width: int = 6) -> str:
    """全局统一的滚动条样式 — 现代简约、薄而精致、主题感知

    供各 widget 复用，消除 12+ 处的重复定义。
    Args:
        width: 滚动条宽度（像素），默认 6，有效范围 4-20。
    """
    # 限制宽度范围，防止异常输入产生退化样式
    width = max(4, min(int(width), 20))
    Colors.refresh()
    return f"""
        QScrollBar:vertical {{
            background: transparent;
            width: {width}px;
            margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background: {Colors.SCROLLBAR_HANDLE_BG};
            border-radius: {width // 2}px;
            min-height: 30px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {Colors.SCROLLBAR_HANDLE_HOVER_BG};
            width: {width + 2}px;
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
        QScrollBar:horizontal {{
            background: transparent;
            height: {width}px;
            margin: 0;
        }}
        QScrollBar::handle:horizontal {{
            background: {Colors.SCROLLBAR_HANDLE_BG};
            border-radius: {width // 2}px;
            min-width: 30px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background: {Colors.SCROLLBAR_HANDLE_HOVER_BG};
            height: {width + 2}px;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0;
        }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
            background: none;
        }}
    """


def get_content_bg_style() -> str:
    """获取内容区背景样式"""
    return f"""
        background-color: {Colors.CONTENT_BG};
        border-radius: 6px;
    """


def fade_in_widget(widget, duration: int = Animations.NORMAL_MS):
    """为 widget 添加淡入动画（透明度 0→1），简洁克制"""
    from PyQt5.QtCore import QPropertyAnimation
    from PyQt5.QtWidgets import QGraphicsOpacityEffect

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
        shadow_type: "card" | "floating" | "glow" | "glow_primary" | "glow_ambient"
            - "card"/"floating": 静态 drop shadow（深色 + offset）
            - "glow*": 聚焦发光 halo，颜色取自 Colors.INPUT_FOCUS_BORDER（主题感知），
              alpha / blur_radius 来自对应 token
    """
    from PyQt5.QtGui import QColor
    from PyQt5.QtWidgets import QGraphicsDropShadowEffect

    config = getattr(Shadows, shadow_type.upper(), Shadows.CARD)
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(config["blur_radius"])
    effect.setOffset(config["offset_x"], config["offset_y"])

    if shadow_type.lower().startswith("glow"):
        # GLOW_* 系列：颜色跟随主题，alpha 来自 token
        Colors.refresh()
        glow = QColor(Colors.INPUT_FOCUS_BORDER)
        glow.setAlpha(config.get("alpha", 170))
        effect.setColor(glow)
    else:
        # CARD / FLOATING：颜色直接来自 token 的 color 字段
        effect.setColor(QColor(config["color"]))
    widget.setGraphicsEffect(effect)


# ── 全局 tooltip 样式（跟随主题） ──────────────────────


def _rgba_to_qcolor(value: str) -> "QColor":
    """将 rgba(r,g,b,a) / rgb(r,g,b) / #rrggbb 字符串转为 QColor。

    QColor 构造函数不认 CSS rgba()/rgb() 写法，主题 YAML 中大量使用
    rgba(r,g,b,a)，直接丢给 QColor 会退化为黑色。此处手动解析。
    """
    from PyQt5.QtGui import QColor

    s = str(value or "").strip()
    try:
        if s.startswith("#"):
            return QColor(s)
        elif s.lower().startswith(("rgba(", "rgb(")):
            inner = s[s.index("(") + 1 : s.rindex(")")]
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 3:
                r, g, b = (int(round(float(parts[i]))) for i in range(3))
                a = 255
                if len(parts) >= 4:
                    av = float(parts[3])
                    a = int(round(av * 255)) if av <= 1 else int(round(av))
                return QColor(
                    max(0, min(255, r)),
                    max(0, min(255, g)),
                    max(0, min(255, b)),
                    max(0, min(255, a)),
                )
        return QColor(s)
    except Exception:
        return QColor(33, 33, 38, 246)


# 模块级状态：供 monkey-patched qfluentwidgets ToolTip.showEvent 读取当前主题色
_tooltip_theme: dict = {}
_tooltip_qf_patched: bool = False


def _apply_tooltip_style() -> None:
    """用当前主题色设置全局 tooltip 样式

    统一规范：
    - 背景：主题 card_bg_solid（实底卡片色，亮/暗自适应）
    - 文字：主题 text_primary，字体 12px，字体家族跟随全局设置
    - 边框：主题 divider_color 细线 + 6px 圆角，悬浮感克制
    - 内边距：4px 8px，文字不贴边

    覆盖两种 tooltip 系统：
    1) 原生 QToolTip（widget.setToolTip() → 无 ToolTipFilter）
       → QToolTip.setPalette() 设色 + QToolTip.setFont() 设字体。
         故意不设 qapp.setStyleSheet()：stylesheet 会将 Qt 拖入 "stylesheet mode"，
         导致 palette 被 QStyleSheetStyle 忽略，亮/暗主题下颜色错乱。
    2) qfluentwidgets 自定义 ToolTip（经 ToolTipFilter → ToolTip widget）
       → monkey-patch ToolTip.showEvent，每次显示前用 self.setStyleSheet()
         完全替换 FluentStyleSheet.TOOL_TIP

    以下特殊 tooltip 不受此全局样式影响（各自独立绘制）：
    - ContextBreakdownTooltip（上下文占比条）
    - CodingPlanTooltip（套餐用量）
    - 命令卡片悬浮描述气泡（_DescTooltipBubble）
    """
    try:
        from PyQt5.QtWidgets import QApplication

        qapp = QApplication.instance()
        if qapp is None:
            return
    except Exception:
        return

    try:
        from loguru import logger as _log

        theme = current_theme()
        bg = theme.get("card_bg_solid", "rgba(30, 30, 32, 240)")
        tc = theme.get("text_primary", "#ffffff")
        border_c = theme.get("divider_color", "rgba(128,128,128,0.15)")
        ff = _get_global_font()
        fs = scale_font_size(11)

        _log.debug(f"[tooltip] apply: bg={bg[:40]} tc={tc} fs={fs} ff={ff}")

        # ── 1) 更新模块级主题字典（供 monkey-patch 读取） ──
        global _tooltip_theme
        _tooltip_theme = {
            "bg": bg,
            "tc": tc,
            "border_c": border_c,
            "ff": ff,
            "fs": fs,
        }

        # ── 2) 原生 QToolTip —— 用主题色样式化（备选兜底）──
        # 此处用主题色配置 QToolTip palette 和 font，
        # 确保直接调用 QToolTip.showText() 的组件（如图表）显示正确主题色，
        # 也作为任何未安装自绘 tooltip 的 widget 的兜底样式。
        from PyQt5.QtWidgets import QToolTip as _QToolTip
        from PyQt5.QtGui import QPalette as _QPalette, QFont as _QFont

        _pal = _QPalette()
        _pal.setColor(_QPalette.ToolTipBase, _rgba_to_qcolor(bg))
        _pal.setColor(_QPalette.ToolTipText, _rgba_to_qcolor(tc))
        _QToolTip.setPalette(_pal)
        _QToolTip.setFont(_QFont(ff, fs))

        # ── 3) qfluentwidgets ToolTip：monkey-patch showEvent ──
        _ensure_qfluentwidgets_tooltip_patch()
    except Exception:
        pass


def _ensure_qfluentwidgets_tooltip_patch() -> None:
    """对 qfluentwidgets ToolTip.showEvent 做一次性 monkey-patch。

    qfluentwidgets 的 ToolTip 在 __init__ 中通过 FluentStyleSheet.TOOL_TIP.apply()
    给自己设置了 widget 级样式表（ToolTip / ToolTip>#container / QLabel），
    导致 qapp.setStyleSheet() 对它无效。

    此处 monkey-patch showEvent，每次显示前直接替换 ToolTip 自身的整个样式表
    （self.setStyleSheet），用 DriFox 主题色覆盖 FluentStyleSheet 的默认值。
    样式结构保持与 FluentStyleSheet 一致（ToolTip / ToolTip>#container / QLabel），
    仅颜色/字号/圆角替换为主题感知值。
    """
    global _tooltip_qf_patched
    if _tooltip_qf_patched:
        return
    _tooltip_qf_patched = True

    try:
        from qfluentwidgets.components.widgets.tool_tip import ToolTip

        _original_show = ToolTip.showEvent

        def _patched_show_event(self, event):
            tt = _tooltip_theme  # 模块级 dict，主题切换时由 _apply_tooltip_style 更新
            if tt:
                # 完全替换 ToolTip 的样式表（覆盖 FluentStyleSheet.TOOL_TIP）
                self.setStyleSheet(f"""
                    ToolTip {{ border-radius: 6px; }}
                    ToolTip>#container {{
                        background-color: {tt["bg"]};
                        border: 1px solid {tt["border_c"]};
                        border-radius: 6px;
                    }}
                    ToolTip>#container[transparent=true] {{
                        background-color: transparent;
                    }}
                    QLabel {{
                        color: {tt["tc"]};
                        background-color: transparent;
                        font-size: {tt["fs"]}px;
                        font-family: '{tt["ff"]}';
                        border: none;
                    }}
                """)
            _original_show(self, event)

        ToolTip.showEvent = _patched_show_event
    except Exception:
        pass


# 从 utils 导入字体家族 CSS 函数供复用
