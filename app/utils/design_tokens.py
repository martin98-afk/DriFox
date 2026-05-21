# -*- coding: utf-8 -*-
"""
统一的设计系统 - Design Tokens 和样式常量
所有 UI 组件应引用此模块以保持视觉一致性
"""

from PyQt5.QtCore import QSize


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

THEME_STYLE_OPTIONS = {
    "midnight": {
        "label": "深海蓝黑",
        "window_start": "rgba(10, 14, 22, 255)",
        "window_end": "rgba(15, 20, 30, 255)",
        "card_bg": "rgba(22, 30, 45, 230)",
        "card_bg_solid": "rgba(22, 30, 45, 250)",
        "content_bg": "#1d2533",
        "border": "#3d4a60",
        "border_accent": "#66c6ff",
        "text_primary": "#f3f6fc",
        "text_secondary": "rgba(226, 235, 249, 0.72)",
        "text_muted": "#8b98ad",
        "accent": "#66c6ff",
        "accent_warm": "#f59e0b",
        "hover_bg": "rgba(102, 198, 255, 0.12)",
        "selected_bg": "rgba(102, 198, 255, 0.32)",
        "capsule_bg": "rgba(27, 35, 50, 180)",
        "capsule_border": "rgba(43, 56, 80, 200)",
        # === 组件级颜色 ===
        "user_card_bg": "rgba(27, 42, 67, 150)",
        "user_card_accent": "#9FC3FF",
        "user_card_text": "#F4F7FD",
        "user_card_muted": "#B4C2D9",
        "assistant_card_bg": "rgba(45, 30, 20, 150)",
        "assistant_card_accent": "#D35400",
        "assistant_card_text": "#FFD4B8",
        "assistant_card_muted": "#8FA4C2",
        "agent_btn_text": "#8FA4C2",
        "agent_btn_text_active": "#C9A85C",
        "agent_btn_bg_active": "rgba(201, 168, 92, 0.2)",
        "agent_btn_separator": "rgba(60, 75, 95, 150)",
        "input_bg_start": "rgba(18, 24, 34, 150)",
        "input_bg_end": "rgba(24, 31, 45, 150)",
        "input_focus_bg_start": "rgba(22, 29, 41, 220)",
        "input_focus_bg_end": "rgba(28, 36, 50, 220)",
        "input_text": "#F2F6FF",
        "input_focus_text": "#FFFFFF",
        "input_border": "#2B3850",
        "input_focus_border": "#C9A85C",
        "input_placeholder": "rgba(242, 246, 255, 0.4)",
        # === 实时卡片色（对话类：todo/tool/question/sub_agent）===
        "realtime_border": "#4a90d9",       # 活泼海蓝边框
        "realtime_accent": "#7dd3fc",       # 明亮天蓝强调
        "realtime_accent_warm": "#fbbf24",  # 暖金执行色（工具运行中）
        "realtime_success": "#34d399",      # 成功绿
        "realtime_error": "#f87171",        # 失败红
        "realtime_bg": "rgba(18, 28, 48, 242)",  # 深蓝底
        "realtime_text": "#f3f6fc",
        "realtime_text_secondary": "rgba(226, 235, 249, 0.7)",
        "realtime_tag_bg": "rgba(125, 211, 252, 0.15)",  # 标签背景
        "realtime_tag_border": "rgba(125, 211, 252, 0.3)",
        # === 系统卡片色 ===
        "system_border": "#3d4a60",         # 沉稳灰蓝边框
        "system_accent": "#66c6ff",         # 系统主强调
    },
    "obsidian": {
        "label": "曜石紫",
        "window_start": "rgba(14, 11, 24, 255)",
        "window_end": "rgba(25, 15, 37, 255)",
        "card_bg": "rgba(37, 27, 53, 232)",
        "card_bg_solid": "rgba(37, 27, 53, 250)",
        "content_bg": "#2b2139",
        "border": "#5a476f",
        "border_accent": "#b792ff",
        "text_primary": "#f7f2ff",
        "text_secondary": "rgba(238, 228, 255, 0.74)",
        "text_muted": "#a99ab9",
        "accent": "#b792ff",
        "accent_warm": "#ffb86b",
        "hover_bg": "rgba(183, 146, 255, 0.13)",
        "selected_bg": "rgba(183, 146, 255, 0.34)",
        "capsule_bg": "rgba(38, 27, 55, 185)",
        "capsule_border": "rgba(88, 68, 110, 205)",
        # === 组件级颜色 ===
        "user_card_bg": "rgba(40, 22, 68, 170)",
        "user_card_accent": "#c9a8ff",
        "user_card_text": "#f7f2ff",
        "user_card_muted": "#a99ab9",
        "assistant_card_bg": "rgba(55, 28, 22, 170)",
        "assistant_card_accent": "#ff9a5c",
        "assistant_card_text": "#ffe6d4",
        "assistant_card_muted": "#b89888",
        "agent_btn_text": "#a99ab9",
        "agent_btn_text_active": "#d4b8ff",
        "agent_btn_bg_active": "rgba(183, 146, 255, 0.22)",
        "agent_btn_separator": "rgba(90, 71, 111, 150)",
        "input_bg_start": "rgba(28, 18, 42, 150)",
        "input_bg_end": "rgba(38, 24, 55, 150)",
        "input_focus_bg_start": "rgba(35, 22, 50, 220)",
        "input_focus_bg_end": "rgba(45, 28, 60, 220)",
        "input_text": "#f0e6ff",
        "input_focus_text": "#f7f2ff",
        "input_border": "#5a476f",
        "input_focus_border": "#b792ff",
        "input_placeholder": "rgba(240, 230, 255, 0.4)",
        # === 实时卡片色（对话类：todo/tool/question/sub_agent）===
        "realtime_border": "#8b5cf6",       # 活泼紫边框
        "realtime_accent": "#c4b5fd",       # 淡紫强调
        "realtime_accent_warm": "#fbbf24",  # 暖金执行色
        "realtime_success": "#34d399",
        "realtime_error": "#f87171",
        "realtime_bg": "rgba(32, 22, 50, 242)",
        "realtime_text": "#f7f2ff",
        "realtime_text_secondary": "rgba(238, 228, 255, 0.7)",
        "realtime_tag_bg": "rgba(196, 181, 253, 0.15)",
        "realtime_tag_border": "rgba(196, 181, 253, 0.3)",
        # === 系统卡片色 ===
        "system_border": "#5a476f",
        "system_accent": "#b792ff",
    },
    "forest": {
        "label": "松林暗绿",
        "window_start": "rgba(8, 19, 17, 255)",
        "window_end": "rgba(13, 29, 25, 255)",
        "card_bg": "rgba(18, 42, 36, 232)",
        "card_bg_solid": "rgba(18, 42, 36, 250)",
        "content_bg": "#18362f",
        "border": "#31594f",
        "border_accent": "#57d29a",
        "text_primary": "#effcf6",
        "text_secondary": "rgba(222, 246, 236, 0.74)",
        "text_muted": "#8eb0a5",
        "accent": "#57d29a",
        "accent_warm": "#d6b45d",
        "hover_bg": "rgba(87, 210, 154, 0.12)",
        "selected_bg": "rgba(87, 210, 154, 0.32)",
        "capsule_bg": "rgba(20, 43, 38, 185)",
        "capsule_border": "rgba(51, 92, 82, 205)",
        # === 组件级颜色 ===
        "user_card_bg": "rgba(12, 55, 48, 170)",
        "user_card_accent": "#5ee8ab",
        "user_card_text": "#effcf6",
        "user_card_muted": "#8eb0a5",
        "assistant_card_bg": "rgba(55, 42, 18, 170)",
        "assistant_card_accent": "#e8a94d",
        "assistant_card_text": "#f5e8d0",
        "assistant_card_muted": "#a89878",
        "agent_btn_text": "#8eb0a5",
        "agent_btn_text_active": "#57d29a",
        "agent_btn_bg_active": "rgba(87, 210, 154, 0.2)",
        "agent_btn_separator": "rgba(49, 89, 79, 150)",
        "input_bg_start": "rgba(12, 32, 26, 150)",
        "input_bg_end": "rgba(18, 42, 35, 150)",
        "input_focus_bg_start": "rgba(15, 35, 28, 220)",
        "input_focus_bg_end": "rgba(22, 48, 38, 220)",
        "input_text": "#daf0e8",
        "input_focus_text": "#effcf6",
        "input_border": "#31594f",
        "input_focus_border": "#57d29a",
        "input_placeholder": "rgba(218, 240, 232, 0.4)",
        # === 实时卡片色（对话类：todo/tool/question/sub_agent）===
        "realtime_border": "#2dd4bf",       # 活泼翠绿边框
        "realtime_accent": "#5eead4",       # 明亮薄荷强调
        "realtime_accent_warm": "#fbbf24",  # 暖金执行色
        "realtime_success": "#34d399",
        "realtime_error": "#f87171",
        "realtime_bg": "rgba(12, 38, 32, 242)",
        "realtime_text": "#effcf6",
        "realtime_text_secondary": "rgba(222, 246, 236, 0.7)",
        "realtime_tag_bg": "rgba(94, 234, 212, 0.15)",
        "realtime_tag_border": "rgba(94, 234, 212, 0.3)",
        # === 系统卡片色 ===
        "system_border": "#31594f",
        "system_accent": "#57d29a",
    },
    "graphite": {
        "label": "石墨铜",
        "window_start": "rgba(18, 18, 19, 255)",
        "window_end": "rgba(31, 29, 27, 255)",
        "card_bg": "rgba(43, 40, 37, 232)",
        "card_bg_solid": "rgba(43, 40, 37, 250)",
        "content_bg": "#302d2a",
        "border": "#5d554d",
        "border_accent": "#d69a5b",
        "text_primary": "#f7f4ef",
        "text_secondary": "rgba(241, 233, 222, 0.74)",
        "text_muted": "#a99f93",
        "accent": "#d69a5b",
        "accent_warm": "#7fc7ff",
        "hover_bg": "rgba(214, 154, 91, 0.13)",
        "selected_bg": "rgba(214, 154, 91, 0.32)",
        "capsule_bg": "rgba(48, 44, 40, 185)",
        "capsule_border": "rgba(93, 80, 68, 205)",
        # === 组件级颜色 ===
        "user_card_bg": "rgba(35, 38, 55, 170)",
        "user_card_accent": "#8cb4e0",
        "user_card_text": "#eef2fa",
        "user_card_muted": "#99a3b5",
        "assistant_card_bg": "rgba(55, 38, 22, 170)",
        "assistant_card_accent": "#e0a050",
        "assistant_card_text": "#f5ead8",
        "assistant_card_muted": "#b0a090",
        "agent_btn_text": "#a99f93",
        "agent_btn_text_active": "#d69a5b",
        "agent_btn_bg_active": "rgba(214, 154, 91, 0.2)",
        "agent_btn_separator": "rgba(93, 85, 77, 150)",
        "input_bg_start": "rgba(28, 26, 24, 150)",
        "input_bg_end": "rgba(38, 34, 30, 150)",
        "input_focus_bg_start": "rgba(35, 30, 26, 220)",
        "input_focus_bg_end": "rgba(45, 38, 32, 220)",
        "input_text": "#ede8e0",
        "input_focus_text": "#f7f4ef",
        "input_border": "#5d554d",
        "input_focus_border": "#d69a5b",
        "input_placeholder": "rgba(237, 232, 224, 0.4)",
        # === 实时卡片色（对话类：todo/tool/question/sub_agent）===
        "realtime_border": "#e09f5f",       # 活泼铜橙边框
        "realtime_accent": "#f0c080",       # 明亮暖铜强调
        "realtime_accent_warm": "#7fc7ff",  # 冷蓝执行色（对比暖主色）
        "realtime_success": "#6ee7b7",
        "realtime_error": "#f87171",
        "realtime_bg": "rgba(40, 34, 28, 242)",
        "realtime_text": "#f7f4ef",
        "realtime_text_secondary": "rgba(241, 233, 222, 0.7)",
        "realtime_tag_bg": "rgba(240, 192, 128, 0.15)",
        "realtime_tag_border": "rgba(240, 192, 128, 0.3)",
        # === 系统卡片色 ===
        "system_border": "#5d554d",
        "system_accent": "#d69a5b",
    },
    "bordeaux": {
        "label": "暗酒红",
        "window_start": "rgba(26, 12, 12, 255)",
        "window_end": "rgba(45, 24, 21, 255)",
        "card_bg": "rgba(55, 30, 28, 232)",
        "card_bg_solid": "rgba(55, 30, 28, 250)",
        "content_bg": "#2d1815",
        "border": "#5a3530",
        "border_accent": "#e74c3c",
        "text_primary": "#f0e0da",
        "text_secondary": "rgba(240, 224, 218, 0.72)",
        "text_muted": "#b08070",
        "accent": "#c0392b",
        "accent_warm": "#f0a030",
        "hover_bg": "rgba(192, 57, 43, 0.13)",
        "selected_bg": "rgba(192, 57, 43, 0.32)",
        "capsule_bg": "rgba(48, 25, 22, 185)",
        "capsule_border": "rgba(90, 50, 45, 205)",
        "user_card_bg": "rgba(65, 30, 25, 170)",
        "user_card_accent": "#e88a80",
        "user_card_text": "#f5ece8",
        "user_card_muted": "#c0a098",
        "assistant_card_bg": "rgba(55, 35, 20, 170)",
        "assistant_card_accent": "#d06830",
        "assistant_card_text": "#f0dcc8",
        "assistant_card_muted": "#b8a088",
        "agent_btn_text": "#b08070",
        "agent_btn_text_active": "#e74c3c",
        "agent_btn_bg_active": "rgba(231, 76, 60, 0.22)",
        "agent_btn_separator": "rgba(90, 53, 48, 150)",
        "input_bg_start": "rgba(30, 16, 14, 150)",
        "input_bg_end": "rgba(40, 22, 18, 150)",
        "input_focus_bg_start": "rgba(38, 20, 16, 220)",
        "input_focus_bg_end": "rgba(48, 26, 20, 220)",
        "input_text": "#e8d4ce",
        "input_focus_text": "#f0e0da",
        "input_border": "#5a3530",
        "input_focus_border": "#c0392b",
        "input_placeholder": "rgba(232, 212, 206, 0.4)",
        "realtime_border": "#d05040",
        "realtime_accent": "#e07060",
        "realtime_accent_warm": "#f0a030",
        "realtime_success": "#6aab70",
        "realtime_error": "#d04030",
        "realtime_bg": "rgba(42, 22, 18, 242)",
        "realtime_text": "#f0e0da",
        "realtime_text_secondary": "rgba(240, 224, 218, 0.7)",
        "realtime_tag_bg": "rgba(224, 112, 96, 0.15)",
        "realtime_tag_border": "rgba(224, 112, 96, 0.3)",
        "system_border": "#5a3530",
        "system_accent": "#c0392b",
    },
    "amber": {
        "label": "琥珀日落",
        "window_start": "rgba(20, 16, 8, 255)",
        "window_end": "rgba(45, 31, 13, 255)",
        "card_bg": "rgba(55, 38, 18, 232)",
        "card_bg_solid": "rgba(55, 38, 18, 250)",
        "content_bg": "#3d2912",
        "border": "#6a4e30",
        "border_accent": "#f0b34b",
        "text_primary": "#f5edd8",
        "text_secondary": "rgba(245, 237, 216, 0.72)",
        "text_muted": "#b89868",
        "accent": "#d4893b",
        "accent_warm": "#7fc7ff",
        "hover_bg": "rgba(212, 137, 59, 0.15)",
        "selected_bg": "rgba(212, 137, 59, 0.34)",
        "capsule_bg": "rgba(48, 32, 14, 185)",
        "capsule_border": "rgba(95, 72, 45, 205)",
        "user_card_bg": "rgba(55, 35, 15, 170)",
        "user_card_accent": "#f0c070",
        "user_card_text": "#f5edd8",
        "user_card_muted": "#c0a878",
        "assistant_card_bg": "rgba(45, 38, 20, 170)",
        "assistant_card_accent": "#d08040",
        "assistant_card_text": "#f0dcc0",
        "assistant_card_muted": "#b0a080",
        "agent_btn_text": "#b89868",
        "agent_btn_text_active": "#f0b34b",
        "agent_btn_bg_active": "rgba(240, 179, 75, 0.22)",
        "agent_btn_separator": "rgba(90, 72, 48, 150)",
        "input_bg_start": "rgba(30, 20, 10, 150)",
        "input_bg_end": "rgba(40, 28, 14, 150)",
        "input_focus_bg_start": "rgba(38, 24, 12, 220)",
        "input_focus_bg_end": "rgba(48, 30, 16, 220)",
        "input_text": "#e8dcce",
        "input_focus_text": "#f5edd8",
        "input_border": "#6a4e30",
        "input_focus_border": "#d4893b",
        "input_placeholder": "rgba(232, 220, 206, 0.4)",
        "realtime_border": "#e09a45",
        "realtime_accent": "#f0c070",
        "realtime_accent_warm": "#7fc7ff",
        "realtime_success": "#6aaa70",
        "realtime_error": "#d05030",
        "realtime_bg": "rgba(42, 28, 12, 242)",
        "realtime_text": "#f5edd8",
        "realtime_text_secondary": "rgba(245, 237, 216, 0.7)",
        "realtime_tag_bg": "rgba(240, 192, 112, 0.15)",
        "realtime_tag_border": "rgba(240, 192, 112, 0.3)",
        "system_border": "#6a4e30",
        "system_accent": "#d4893b",
    },
    "ocean": {
        "label": "深海蓝",
        "window_start": "rgba(10, 14, 24, 255)",
        "window_end": "rgba(16, 26, 48, 255)",
        "card_bg": "rgba(24, 40, 65, 232)",
        "card_bg_solid": "rgba(24, 40, 65, 250)",
        "content_bg": "#182842",
        "border": "#3a5880",
        "border_accent": "#5dade2",
        "text_primary": "#e8f0fc",
        "text_secondary": "rgba(232, 240, 252, 0.72)",
        "text_muted": "#8898b0",
        "accent": "#3498db",
        "accent_warm": "#f59e0b",
        "hover_bg": "rgba(52, 152, 219, 0.13)",
        "selected_bg": "rgba(52, 152, 219, 0.32)",
        "capsule_bg": "rgba(20, 34, 58, 185)",
        "capsule_border": "rgba(52, 78, 118, 205)",
        "user_card_bg": "rgba(18, 48, 80, 170)",
        "user_card_accent": "#7fc0f0",
        "user_card_text": "#e8f0fc",
        "user_card_muted": "#98a8c0",
        "assistant_card_bg": "rgba(45, 30, 20, 170)",
        "assistant_card_accent": "#d08040",
        "assistant_card_text": "#f5e8d8",
        "assistant_card_muted": "#b0a090",
        "agent_btn_text": "#8898b0",
        "agent_btn_text_active": "#5dade2",
        "agent_btn_bg_active": "rgba(93, 173, 226, 0.22)",
        "agent_btn_separator": "rgba(58, 88, 128, 150)",
        "input_bg_start": "rgba(14, 22, 36, 150)",
        "input_bg_end": "rgba(20, 30, 50, 150)",
        "input_focus_bg_start": "rgba(18, 28, 44, 220)",
        "input_focus_bg_end": "rgba(24, 36, 56, 220)",
        "input_text": "#d8e4f2",
        "input_focus_text": "#e8f0fc",
        "input_border": "#3a5880",
        "input_focus_border": "#3498db",
        "input_placeholder": "rgba(216, 228, 242, 0.4)",
        "realtime_border": "#2980b9",
        "realtime_accent": "#5dade2",
        "realtime_accent_warm": "#fbbf24",
        "realtime_success": "#2ecc71",
        "realtime_error": "#e74c3c",
        "realtime_bg": "rgba(18, 30, 52, 242)",
        "realtime_text": "#e8f0fc",
        "realtime_text_secondary": "rgba(232, 240, 252, 0.7)",
        "realtime_tag_bg": "rgba(93, 173, 226, 0.15)",
        "realtime_tag_border": "rgba(93, 173, 226, 0.3)",
        "system_border": "#3a5880",
        "system_accent": "#3498db",
    },
    "sakura": {
        "label": "樱花粉",
        "window_start": "rgba(20, 14, 20, 255)",
        "window_end": "rgba(45, 24, 37, 255)",
        "card_bg": "rgba(60, 32, 50, 232)",
        "card_bg_solid": "rgba(60, 32, 50, 250)",
        "content_bg": "#3a2030",
        "border": "#604858",
        "border_accent": "#e8a0c8",
        "text_primary": "#f5eaf0",
        "text_secondary": "rgba(245, 234, 240, 0.72)",
        "text_muted": "#b090a0",
        "accent": "#d473b4",
        "accent_warm": "#f0b34b",
        "hover_bg": "rgba(212, 115, 180, 0.13)",
        "selected_bg": "rgba(212, 115, 180, 0.32)",
        "capsule_bg": "rgba(48, 26, 40, 185)",
        "capsule_border": "rgba(90, 68, 82, 205)",
        "user_card_bg": "rgba(60, 32, 55, 170)",
        "user_card_accent": "#e0b0d0",
        "user_card_text": "#f5eaf0",
        "user_card_muted": "#c0a0b0",
        "assistant_card_bg": "rgba(50, 32, 22, 170)",
        "assistant_card_accent": "#d08050",
        "assistant_card_text": "#f0dcc8",
        "assistant_card_muted": "#b0a090",
        "agent_btn_text": "#b090a0",
        "agent_btn_text_active": "#e8a0c8",
        "agent_btn_bg_active": "rgba(232, 160, 200, 0.22)",
        "agent_btn_separator": "rgba(90, 72, 82, 150)",
        "input_bg_start": "rgba(30, 16, 26, 150)",
        "input_bg_end": "rgba(40, 24, 34, 150)",
        "input_focus_bg_start": "rgba(38, 20, 30, 220)",
        "input_focus_bg_end": "rgba(48, 26, 38, 220)",
        "input_text": "#e8d8e2",
        "input_focus_text": "#f5eaf0",
        "input_border": "#604858",
        "input_focus_border": "#d473b4",
        "input_placeholder": "rgba(232, 216, 226, 0.4)",
        "realtime_border": "#d080b8",
        "realtime_accent": "#e0a0c8",
        "realtime_accent_warm": "#f0b34b",
        "realtime_success": "#6aab70",
        "realtime_error": "#d05050",
        "realtime_bg": "rgba(42, 24, 34, 242)",
        "realtime_text": "#f5eaf0",
        "realtime_text_secondary": "rgba(245, 234, 240, 0.7)",
        "realtime_tag_bg": "rgba(224, 160, 200, 0.15)",
        "realtime_tag_border": "rgba(224, 160, 200, 0.3)",
        "system_border": "#604858",
        "system_accent": "#d473b4",
    },
    "slate": {
        "label": "石板灰",
        "window_start": "rgba(14, 14, 16, 255)",
        "window_end": "rgba(28, 28, 34, 255)",
        "card_bg": "rgba(42, 42, 50, 232)",
        "card_bg_solid": "rgba(42, 42, 50, 250)",
        "content_bg": "#2c2c32",
        "border": "#505058",
        "border_accent": "#bdc3c7",
        "text_primary": "#eceff1",
        "text_secondary": "rgba(236, 239, 241, 0.72)",
        "text_muted": "#9ea4a8",
        "accent": "#7f8c8d",
        "accent_warm": "#95a5a6",
        "hover_bg": "rgba(127, 140, 141, 0.13)",
        "selected_bg": "rgba(127, 140, 141, 0.32)",
        "capsule_bg": "rgba(36, 36, 42, 185)",
        "capsule_border": "rgba(72, 72, 80, 205)",
        "user_card_bg": "rgba(36, 40, 52, 170)",
        "user_card_accent": "#a0b0c0",
        "user_card_text": "#eceff1",
        "user_card_muted": "#a0a8b0",
        "assistant_card_bg": "rgba(44, 38, 34, 170)",
        "assistant_card_accent": "#b0a090",
        "assistant_card_text": "#e8e4e0",
        "assistant_card_muted": "#a09890",
        "agent_btn_text": "#9ea4a8",
        "agent_btn_text_active": "#bdc3c7",
        "agent_btn_bg_active": "rgba(189, 195, 199, 0.22)",
        "agent_btn_separator": "rgba(72, 72, 80, 150)",
        "input_bg_start": "rgba(20, 20, 24, 150)",
        "input_bg_end": "rgba(28, 28, 34, 150)",
        "input_focus_bg_start": "rgba(24, 24, 30, 220)",
        "input_focus_bg_end": "rgba(34, 34, 40, 220)",
        "input_text": "#d8dcde",
        "input_focus_text": "#eceff1",
        "input_border": "#505058",
        "input_focus_border": "#7f8c8d",
        "input_placeholder": "rgba(216, 220, 222, 0.4)",
        "realtime_border": "#95a5a6",
        "realtime_accent": "#b0b8bc",
        "realtime_accent_warm": "#95a5a6",
        "realtime_success": "#6aaa70",
        "realtime_error": "#c05050",
        "realtime_bg": "rgba(36, 36, 40, 242)",
        "realtime_text": "#eceff1",
        "realtime_text_secondary": "rgba(236, 239, 241, 0.7)",
        "realtime_tag_bg": "rgba(176, 184, 188, 0.15)",
        "realtime_tag_border": "rgba(176, 184, 188, 0.3)",
        "system_border": "#505058",
        "system_accent": "#7f8c8d",
    },
    "jade": {
        "label": "翡翠绿",
        "window_start": "rgba(10, 24, 18, 255)",
        "window_end": "rgba(15, 40, 30, 255)",
        "card_bg": "rgba(22, 50, 38, 232)",
        "card_bg_solid": "rgba(22, 50, 38, 250)",
        "content_bg": "#183a2a",
        "border": "#2a5a48",
        "border_accent": "#48c9b0",
        "text_primary": "#e0f2ec",
        "text_secondary": "rgba(224, 242, 236, 0.72)",
        "text_muted": "#80a898",
        "accent": "#1abc9c",
        "accent_warm": "#f0b34b",
        "hover_bg": "rgba(26, 188, 156, 0.13)",
        "selected_bg": "rgba(26, 188, 156, 0.32)",
        "capsule_bg": "rgba(18, 42, 32, 185)",
        "capsule_border": "rgba(42, 85, 68, 205)",
        "user_card_bg": "rgba(14, 52, 42, 170)",
        "user_card_accent": "#5ee0c0",
        "user_card_text": "#e0f2ec",
        "user_card_muted": "#90b8a8",
        "assistant_card_bg": "rgba(42, 38, 18, 170)",
        "assistant_card_accent": "#d8a050",
        "assistant_card_text": "#f0e8d0",
        "assistant_card_muted": "#b0a880",
        "agent_btn_text": "#80a898",
        "agent_btn_text_active": "#48c9b0",
        "agent_btn_bg_active": "rgba(72, 201, 176, 0.22)",
        "agent_btn_separator": "rgba(42, 90, 72, 150)",
        "input_bg_start": "rgba(12, 32, 24, 150)",
        "input_bg_end": "rgba(18, 44, 32, 150)",
        "input_focus_bg_start": "rgba(16, 38, 28, 220)",
        "input_focus_bg_end": "rgba(22, 50, 38, 220)",
        "input_text": "#d0e8e0",
        "input_focus_text": "#e0f2ec",
        "input_border": "#2a5a48",
        "input_focus_border": "#1abc9c",
        "input_placeholder": "rgba(208, 232, 224, 0.4)",
        "realtime_border": "#2dd4a0",
        "realtime_accent": "#5eead4",
        "realtime_accent_warm": "#fbbf24",
        "realtime_success": "#34d399",
        "realtime_error": "#f87171",
        "realtime_bg": "rgba(14, 38, 28, 242)",
        "realtime_text": "#e0f2ec",
        "realtime_text_secondary": "rgba(224, 242, 236, 0.7)",
        "realtime_tag_bg": "rgba(94, 234, 212, 0.15)",
        "realtime_tag_border": "rgba(94, 234, 212, 0.3)",
        "system_border": "#2a5a48",
        "system_accent": "#1abc9c",
    },
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


def get_theme_style_key() -> str:
    try:
        from app.utils.config import Settings
        key = Settings.get_instance().ui_theme_style.value
    except Exception:
        key = "midnight"
    return key if key in THEME_STYLE_OPTIONS else "midnight"


def current_theme() -> dict:
    return THEME_STYLE_OPTIONS[get_theme_style_key()]


def get_window_style() -> str:
    theme = current_theme()
    return f"""
    OpenAIChatToolWindow {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
            stop:0 {theme["window_start"]},
            stop:1 {theme["window_end"]});
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


# ============ 圆角系统 ============
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
