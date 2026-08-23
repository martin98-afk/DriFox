# -*- coding: utf-8 -*-
"""WorkspacePage 契约 — 页面级扩展（插件提供完整主页面）

三层灵活性模型顶层：
- 条目级（ui_slots）：往区域加条目
- 模块级（ui_module）：替换区域实现
- 页面级（本契约）：提供全新主页面（Tab 管理器内容区新页，非对话形态）

widget_class 约定：__init__(self, parent=None, context=None)；
context 为 UIPluginRegistry._build_ui_context 同款 dict（project_root/theme/services 等）。
"""

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class WorkspacePageInfo:
    """工作区页面注册信息

    Attributes:
        plugin_name: 所属插件名
        page_id: 页面唯一 ID（同时是 content_area 页 key 与命令名）
        title: 显示标题（tab_panel 入口行文本）
        icon_path: 深色主题图标路径（可选）
        icon_light_path: 浅色主题图标路径（可选，缺省回退 icon_path）
        widget_class: 页面 widget 类（构造 parent + context）
        order_hint: 排序权重（小者在前；系统保留 <100，插件默认 500）
        metadata: 附加元数据（如 hide_sidebar 控制是否进 tab_panel 入口列表）
    """

    plugin_name: str
    page_id: str
    title: str
    widget_class: Any
    icon_path: str = ""
    icon_light_path: str = ""
    order_hint: int = 500
    metadata: Dict[str, Any] = field(default_factory=dict)