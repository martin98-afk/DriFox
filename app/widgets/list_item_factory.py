# -*- coding: utf-8 -*-
"""
ListItem 工厂函数 - 使用组合模式生成通用列表项组件

解决列表项组件高度重复的问题：
- 各类自定义 Item（SkillItem、HookItem、ProviderItem 等）高度相似
- 统一高度、样式、hover、按钮位置等模式

使用工厂函数而非继承，保持灵活性。
"""

from typing import Callable, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    SwitchButton,
    ToolButton,
)

from app.utils.design_tokens import (
    Sizes,
    SwitchStyles,
    font_size_css,
)
from app.utils.utils import get_font_family_css

# ========== 常量 ==========
ITEM_HEIGHT = 53
DEFAULT_LEFT_MARGIN = 16
DEFAULT_RIGHT_MARGIN = 16


# ========== 通用样式工具 ==========

def _get_elided_text(text: str, max_width: int, font_size: int = 12) -> str:
    """
    获取省略后的文本（如果太长）
    
    Args:
        text: 原始文本
        max_width: 最大像素宽度
        font_size: 字体大小（用于估算）
    
    Returns:
        省略后的文本
    """
    if not text:
        return ""
    # 估算：每个中文字符约2个字节宽度，英文约0.5倍
    # 简化：假设平均字符宽度约等于 font_size * 0.6
    avg_char_width = font_size * 0.6
    max_chars = int(max_width / avg_char_width)

    if len(text) <= max_chars:
        return text

    # 留出省略号位置
    return text[:max(1, max_chars - 3)] + "..."


# ========== 基础列表项工厂 ==========

def create_base_item(
    parent: QWidget,
    height: int = ITEM_HEIGHT,
    left_margin: int = DEFAULT_LEFT_MARGIN,
    right_margin: int = DEFAULT_RIGHT_MARGIN,
) -> tuple:
    """
    创建基础列表项的骨架（Widget + Layout）
    
    Args:
        parent: 父组件
        height: 固定高度
        left_margin: 左侧边距
        right_margin: 右侧边距
    
    Returns:
        (widget, layout) 元组
    """
    widget = QWidget(parent)
    widget.setFixedHeight(height)
    widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    widget.setStyleSheet("background-color: transparent;")

    layout = QHBoxLayout(widget)
    layout.setContentsMargins(left_margin, 0, right_margin, 0)
    layout.setAlignment(Qt.AlignVCenter)

    return widget, layout


# ========== 文本标签项工厂 ==========

def create_text_item(
    parent: QWidget,
    text: str,
    on_remove: Optional[Callable] = None,
    left_margin: int = DEFAULT_LEFT_MARGIN,
    right_margin: int = DEFAULT_RIGHT_MARGIN,
    remove_button_visible: bool = True,
    elide: bool = True,
    max_text_width: int = 400,
) -> QWidget:
    """
    创建简单的文本列表项（带可选的删除按钮）
    
    Args:
        parent: 父组件
        text: 显示文本
        on_remove: 删除回调
        left_margin: 左侧边距
        right_margin: 右侧边距
        remove_button_visible: 是否显示删除按钮
        elide: 是否在文本过长时省略
        max_text_width: 文本最大宽度（像素）
    
    Returns:
        QWidget 实例
    
    Example:
        item = create_text_item(
            parent=self.view,
            text="My Item",
            on_remove=lambda: print("removed"),
        )
    """
    widget, layout = create_base_item(parent, ITEM_HEIGHT, left_margin, right_margin)

    # 文本标签
    display_text = text
    if elide:
        display_text = _get_elided_text(text, max_text_width)

    label = QLabel(display_text, widget)
    label.setObjectName("itemLabel")
    if text != display_text:
        label.setToolTip(text)  # 鼠标悬停显示完整文本
    label.setStyleSheet(f"{font_size_css(12)} {get_font_family_css()}")
    layout.addWidget(label, 0, Qt.AlignLeft)

    # 添加 stretch 使内容靠左
    layout.addSpacing(16)
    layout.addStretch(1)

    # 删除按钮
    if remove_button_visible and on_remove:
        remove_btn = ToolButton(FluentIcon.CLOSE, widget)
        remove_btn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        remove_btn.setIconSize(Sizes.TOOL_ICON_SZ)
        remove_btn.clicked.connect(lambda: on_remove(widget))
        layout.addWidget(remove_btn, 0, Qt.AlignRight)

    return widget


# ========== 双标签项工厂 ==========

def create_dual_label_item(
    parent: QWidget,
    primary_text: str,
    secondary_text: str,
    on_toggle: Optional[Callable[[bool], None]] = None,
    primary_width: int = 140,
    on_remove: Optional[Callable] = None,
    left_margin: int = DEFAULT_LEFT_MARGIN,
    right_margin: int = DEFAULT_RIGHT_MARGIN,
    has_switch: bool = True,
    primary_elide: bool = True,
    secondary_elide: bool = True,
) -> QWidget:
    """
    创建双标签列表项（主标签 + 次标签 + 可选开关）

    替代: SkillItem, HookItem

    Args:
        parent: 父组件
        primary_text: 主标签文本（通常在左侧加粗）
        secondary_text: 次标签文本（通常在右侧灰色）
        on_toggle: 开关切换回调 (checked: bool)
        primary_width: 主标签固定宽度
        on_remove: 删除回调
        left_margin: 左侧边距
        right_margin: 右侧边距
        has_switch: 是否显示开关按钮
        primary_elide: 主标签是否省略
        secondary_elide: 次标签是否省略
    
    Returns:
        QWidget 实例
    
    Example:
        item = create_dual_label_item(
            parent=self.view,
            primary_text="my-skill",
            secondary_text="A useful skill for...",
            on_toggle=lambda checked: print(f"toggled: {checked}"),
        )
    """
    widget, layout = create_base_item(parent, ITEM_HEIGHT, left_margin, right_margin)

    # 主标签
    display_primary = primary_text
    if primary_elide:
        display_primary = _get_elided_text(primary_text, primary_width)

    primary_label = QLabel(display_primary, widget)
    primary_label.setFixedWidth(primary_width)
    primary_label.setObjectName("primaryLabel")
    if primary_text != display_primary:
        primary_label.setToolTip(primary_text)
    primary_label.setStyleSheet(
        f"font-weight: bold; {font_size_css(12)}; {get_font_family_css()}"
    )
    layout.addWidget(primary_label, 0, Qt.AlignLeft)

    # 次标签
    display_secondary = secondary_text
    if secondary_elide:
        # 次标签可以更宽一些
        parent_width = parent.width() if parent and hasattr(parent, 'width') else 500
        secondary_width = parent_width - primary_width - left_margin - right_margin - 80
        display_secondary = _get_elided_text(secondary_text, secondary_width)

    secondary_label = QLabel(display_secondary, widget)
    secondary_label.setObjectName("secondaryLabel")
    secondary_label.setStyleSheet(f"color: #888888; {font_size_css(12)}; {get_font_family_css()}")
    secondary_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    if secondary_text != display_secondary:
        secondary_label.setToolTip(secondary_text)
    layout.addWidget(secondary_label, 1, Qt.AlignLeft)

    # 开关按钮或删除按钮
    if has_switch and on_toggle:
        switch = SwitchButton(widget)
        SwitchStyles.configure(switch)
        switch.checkedChanged.connect(on_toggle)
        layout.addWidget(switch, 0, Qt.AlignRight)
    elif on_remove:
        remove_btn = ToolButton(FluentIcon.CLOSE, widget)
        remove_btn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        remove_btn.setIconSize(Sizes.TOOL_ICON_SZ)
        remove_btn.clicked.connect(lambda: on_remove(widget))
        layout.addWidget(remove_btn, 0, Qt.AlignRight)

    return widget


# ========== 带图标的列表项工厂 ==========

def create_icon_text_item(
    parent: QWidget,
    icon: QWidget,  # 图标组件（QLabel 或其他）
    text: str,
    subtitle: str = "",
    on_click: Optional[Callable] = None,
    on_remove: Optional[Callable] = None,
    left_margin: int = DEFAULT_LEFT_MARGIN,
    right_margin: int = DEFAULT_RIGHT_MARGIN,
) -> QWidget:
    """
    创建带图标的列表项
    
    替代: ProviderItem, ModelItem, ProjectItem
    
    Args:
        parent: 父组件
        icon: 图标组件
        text: 主文本
        subtitle: 副文本（可选）
        on_click: 点击回调
        on_remove: 删除回调
        left_margin: 左侧边距
        right_margin: 右侧边距
    
    Returns:
        QWidget 实例
    """
    widget, layout = create_base_item(parent, ITEM_HEIGHT, left_margin, right_margin)

    # 图标
    if icon:
        layout.addWidget(icon, 0, Qt.AlignLeft)
        layout.addSpacing(12)

    # 文本区域
    text_layout = QVBoxLayout()
    text_layout.setSpacing(2)
    text_layout.setContentsMargins(0, 0, 0, 0)

    main_label = QLabel(text, widget)
    main_label.setStyleSheet(font_size_css(13))
    text_layout.addWidget(main_label)

    if subtitle:
        sub_label = QLabel(subtitle, widget)
        sub_label.setStyleSheet(f"color: #888888; {font_size_css(11)}")
        text_layout.addWidget(sub_label)

    layout.addLayout(text_layout, 1)

    # 删除按钮
    if on_remove:
        layout.addSpacing(8)
        remove_btn = ToolButton(FluentIcon.CLOSE, widget)
        remove_btn.setFixedSize(Sizes.TOOL_BUTTON_SZ)
        remove_btn.setIconSize(Sizes.TOOL_ICON_SZ)
        remove_btn.clicked.connect(lambda: on_remove(widget))
        layout.addWidget(remove_btn, 0, Qt.AlignRight)

    # 点击事件
    if on_click:
        widget.mousePressEvent = lambda e: on_click()

    return widget


# ========== 工厂函数导出 ==========

__all__ = [
    "ITEM_HEIGHT",
    "DEFAULT_LEFT_MARGIN",
    "DEFAULT_RIGHT_MARGIN",
    "create_base_item",
    "create_text_item",
    "create_dual_label_item",
    "create_icon_text_item",
    "_get_elided_text",
]
