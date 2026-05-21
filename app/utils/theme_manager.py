# -*- coding: utf-8 -*-
"""
主题管理器
- 扫描 .drifox/themes/*.yaml 加载主题
- 首次运行自动生成内置主题
- 提供主题查询接口
"""
import os
import json
import yaml
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# 内置主题默认数据（迁移自 design_tokens.py THEME_STYLE_OPTIONS + 新增字段）
_BUILTIN_THEMES = {
    "midnight": {
        "name": "深海蓝黑",
        "window": {
            "gradient_start": "rgba(10, 14, 22, 255)",
            "gradient_end": "rgba(15, 20, 30, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
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
            "realtime_border": "#4a90d9",
            "realtime_accent": "#7dd3fc",
            "realtime_accent_warm": "#fbbf24",
            "realtime_success": "#34d399",
            "realtime_error": "#f87171",
            "realtime_bg": "rgba(18, 28, 48, 242)",
            "realtime_text": "#f3f6fc",
            "realtime_text_secondary": "rgba(226, 235, 249, 0.7)",
            "realtime_tag_bg": "rgba(125, 211, 252, 0.15)",
            "realtime_tag_border": "rgba(125, 211, 252, 0.3)",
            "system_border": "#3d4a60",
            "system_accent": "#66c6ff",
            # === 新增 ===
            "send_btn_start": "#C9A85C",
            "send_btn_end": "#B8956A",
            "send_btn_hover_start": "#D4B878",
            "send_btn_hover_end": "#C9A060",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#6BA3FF",
            "timeline_node_visible": "#00FF7F",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#00FF7F",
            "ring_normal": "#5aa9ff",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(102, 198, 255, 0.15)",
            "branch_label_border": "rgba(102, 198, 255, 0.3)",
        },
    },
    "obsidian": {
        "name": "曜石紫",
        "window": {
            "gradient_start": "rgba(14, 11, 24, 255)",
            "gradient_end": "rgba(25, 15, 37, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
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
            "realtime_border": "#8b5cf6",
            "realtime_accent": "#c4b5fd",
            "realtime_accent_warm": "#fbbf24",
            "realtime_success": "#34d399",
            "realtime_error": "#f87171",
            "realtime_bg": "rgba(32, 22, 50, 242)",
            "realtime_text": "#f7f2ff",
            "realtime_text_secondary": "rgba(238, 228, 255, 0.7)",
            "realtime_tag_bg": "rgba(196, 181, 253, 0.15)",
            "realtime_tag_border": "rgba(196, 181, 253, 0.3)",
            "system_border": "#5a476f",
            "system_accent": "#b792ff",
            "send_btn_start": "#b792ff",
            "send_btn_end": "#8a6fd0",
            "send_btn_hover_start": "#c8a8ff",
            "send_btn_hover_end": "#9b7ee0",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#b792ff",
            "timeline_node_visible": "#c4b5fd",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#c4b5fd",
            "ring_normal": "#b792ff",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(183, 146, 255, 0.15)",
            "branch_label_border": "rgba(183, 146, 255, 0.3)",
        },
    },
    "forest": {
        "name": "松林暗绿",
        "window": {
            "gradient_start": "rgba(8, 19, 17, 255)",
            "gradient_end": "rgba(13, 29, 25, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
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
            "realtime_border": "#2dd4bf",
            "realtime_accent": "#5eead4",
            "realtime_accent_warm": "#fbbf24",
            "realtime_success": "#34d399",
            "realtime_error": "#f87171",
            "realtime_bg": "rgba(12, 38, 32, 242)",
            "realtime_text": "#effcf6",
            "realtime_text_secondary": "rgba(222, 246, 236, 0.7)",
            "realtime_tag_bg": "rgba(94, 234, 212, 0.15)",
            "realtime_tag_border": "rgba(94, 234, 212, 0.3)",
            "system_border": "#31594f",
            "system_accent": "#57d29a",
            "send_btn_start": "#57d29a",
            "send_btn_end": "#3da87a",
            "send_btn_hover_start": "#6ee8b0",
            "send_btn_hover_end": "#4db888",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#57d29a",
            "timeline_node_visible": "#5eead4",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#5eead4",
            "ring_normal": "#57d29a",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(87, 210, 154, 0.15)",
            "branch_label_border": "rgba(87, 210, 154, 0.3)",
        },
    },
    "graphite": {
        "name": "石墨铜",
        "window": {
            "gradient_start": "rgba(18, 18, 19, 255)",
            "gradient_end": "rgba(31, 29, 27, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
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
            "realtime_border": "#e09f5f",
            "realtime_accent": "#f0c080",
            "realtime_accent_warm": "#7fc7ff",
            "realtime_success": "#6ee7b7",
            "realtime_error": "#f87171",
            "realtime_bg": "rgba(40, 34, 28, 242)",
            "realtime_text": "#f7f4ef",
            "realtime_text_secondary": "rgba(241, 233, 222, 0.7)",
            "realtime_tag_bg": "rgba(240, 192, 128, 0.15)",
            "realtime_tag_border": "rgba(240, 192, 128, 0.3)",
            "system_border": "#5d554d",
            "system_accent": "#d69a5b",
            "send_btn_start": "#d69a5b",
            "send_btn_end": "#b88040",
            "send_btn_hover_start": "#e0b070",
            "send_btn_hover_end": "#c89050",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#d69a5b",
            "timeline_node_visible": "#f0c080",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#f0c080",
            "ring_normal": "#d69a5b",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(214, 154, 91, 0.15)",
            "branch_label_border": "rgba(214, 154, 91, 0.3)",
        },
    },
    "bordeaux": {
        "name": "暗酒红",
        "window": {
            "gradient_start": "rgba(26, 12, 12, 255)",
            "gradient_end": "rgba(45, 24, 21, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
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
            "send_btn_start": "#c0392b",
            "send_btn_end": "#a03020",
            "send_btn_hover_start": "#d05040",
            "send_btn_hover_end": "#b04030",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#e07060",
            "timeline_node_visible": "#e88a80",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#e88a80",
            "ring_normal": "#e07060",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(192, 57, 43, 0.15)",
            "branch_label_border": "rgba(192, 57, 43, 0.3)",
        },
    },
    "amber": {
        "name": "琥珀日落",
        "window": {
            "gradient_start": "rgba(20, 16, 8, 255)",
            "gradient_end": "rgba(45, 31, 13, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
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
            "send_btn_start": "#d4893b",
            "send_btn_end": "#b87530",
            "send_btn_hover_start": "#e09a45",
            "send_btn_hover_end": "#c88538",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#f0b34b",
            "timeline_node_visible": "#f0c070",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#f0c070",
            "ring_normal": "#f0b34b",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(212, 137, 59, 0.15)",
            "branch_label_border": "rgba(212, 137, 59, 0.3)",
        },
    },
    "ocean": {
        "name": "深海蓝",
        "window": {
            "gradient_start": "rgba(10, 14, 24, 255)",
            "gradient_end": "rgba(16, 26, 48, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
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
            "send_btn_start": "#3498db",
            "send_btn_end": "#2980b9",
            "send_btn_hover_start": "#5dade2",
            "send_btn_hover_end": "#3498db",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#5dade2",
            "timeline_node_visible": "#3498db",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#5dade2",
            "ring_normal": "#3498db",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(52, 152, 219, 0.15)",
            "branch_label_border": "rgba(52, 152, 219, 0.3)",
        },
    },
    "sakura": {
        "name": "樱花粉",
        "window": {
            "gradient_start": "rgba(30, 18, 24, 255)",
            "gradient_end": "rgba(45, 25, 35, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
            "card_bg": "rgba(55, 30, 42, 232)",
            "card_bg_solid": "rgba(55, 30, 42, 250)",
            "content_bg": "#3d202e",
            "border": "#6a3a50",
            "border_accent": "#f28b9e",
            "text_primary": "#fceff2",
            "text_secondary": "rgba(252, 239, 242, 0.72)",
            "text_muted": "#b898a0",
            "accent": "#e85a7a",
            "accent_warm": "#f0b070",
            "hover_bg": "rgba(232, 90, 122, 0.13)",
            "selected_bg": "rgba(232, 90, 122, 0.32)",
            "capsule_bg": "rgba(48, 25, 36, 185)",
            "capsule_border": "rgba(95, 55, 70, 205)",
            "user_card_bg": "rgba(65, 28, 48, 170)",
            "user_card_accent": "#f0a0b0",
            "user_card_text": "#fceff2",
            "user_card_muted": "#c0a0a8",
            "assistant_card_bg": "rgba(50, 35, 30, 170)",
            "assistant_card_accent": "#d08060",
            "assistant_card_text": "#f0e0d0",
            "assistant_card_muted": "#b0a090",
            "agent_btn_text": "#b898a0",
            "agent_btn_text_active": "#f28b9e",
            "agent_btn_bg_active": "rgba(242, 139, 158, 0.22)",
            "agent_btn_separator": "rgba(90, 55, 70, 150)",
            "input_bg_start": "rgba(34, 18, 26, 150)",
            "input_bg_end": "rgba(44, 24, 34, 150)",
            "input_focus_bg_start": "rgba(38, 20, 30, 220)",
            "input_focus_bg_end": "rgba(48, 26, 38, 220)",
            "input_text": "#f0e0e6",
            "input_focus_text": "#fceff2",
            "input_border": "#6a3a50",
            "input_focus_border": "#e85a7a",
            "input_placeholder": "rgba(240, 224, 230, 0.4)",
            "realtime_border": "#d06080",
            "realtime_accent": "#f090a8",
            "realtime_accent_warm": "#f0b070",
            "realtime_success": "#6aab80",
            "realtime_error": "#d05050",
            "realtime_bg": "rgba(42, 22, 30, 242)",
            "realtime_text": "#fceff2",
            "realtime_text_secondary": "rgba(252, 239, 242, 0.7)",
            "realtime_tag_bg": "rgba(240, 144, 168, 0.15)",
            "realtime_tag_border": "rgba(240, 144, 168, 0.3)",
            "system_border": "#6a3a50",
            "system_accent": "#e85a7a",
            "send_btn_start": "#e85a7a",
            "send_btn_end": "#c84060",
            "send_btn_hover_start": "#f07090",
            "send_btn_hover_end": "#d85070",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#f090a8",
            "timeline_node_visible": "#f28b9e",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#f28b9e",
            "ring_normal": "#f090a8",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(232, 90, 122, 0.15)",
            "branch_label_border": "rgba(232, 90, 122, 0.3)",
        },
    },
    "slate": {
        "name": "雾灰蓝",
        "window": {
            "gradient_start": "rgba(16, 18, 22, 255)",
            "gradient_end": "rgba(26, 28, 34, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
            "card_bg": "rgba(36, 38, 46, 232)",
            "card_bg_solid": "rgba(36, 38, 46, 250)",
            "content_bg": "#282a34",
            "border": "#484a58",
            "border_accent": "#8890b0",
            "text_primary": "#e8eaf0",
            "text_secondary": "rgba(232, 234, 240, 0.72)",
            "text_muted": "#9898a8",
            "accent": "#7a82a0",
            "accent_warm": "#c0a878",
            "hover_bg": "rgba(120, 130, 160, 0.13)",
            "selected_bg": "rgba(120, 130, 160, 0.32)",
            "capsule_bg": "rgba(38, 40, 48, 185)",
            "capsule_border": "rgba(72, 74, 88, 205)",
            "user_card_bg": "rgba(32, 36, 50, 170)",
            "user_card_accent": "#98a0c0",
            "user_card_text": "#e8eaf0",
            "user_card_muted": "#a8a8b8",
            "assistant_card_bg": "rgba(44, 40, 34, 170)",
            "assistant_card_accent": "#b8a078",
            "assistant_card_text": "#e8e0d0",
            "assistant_card_muted": "#a8a090",
            "agent_btn_text": "#9898a8",
            "agent_btn_text_active": "#8890b0",
            "agent_btn_bg_active": "rgba(136, 144, 176, 0.22)",
            "agent_btn_separator": "rgba(72, 74, 88, 150)",
            "input_bg_start": "rgba(24, 26, 32, 150)",
            "input_bg_end": "rgba(32, 34, 42, 150)",
            "input_focus_bg_start": "rgba(28, 30, 38, 220)",
            "input_focus_bg_end": "rgba(36, 38, 48, 220)",
            "input_text": "#d8dae6",
            "input_focus_text": "#e8eaf0",
            "input_border": "#484a58",
            "input_focus_border": "#7a82a0",
            "input_placeholder": "rgba(216, 218, 230, 0.4)",
            "realtime_border": "#7880a0",
            "realtime_accent": "#98a0c0",
            "realtime_accent_warm": "#c0a878",
            "realtime_success": "#78a888",
            "realtime_error": "#c06060",
            "realtime_bg": "rgba(28, 30, 38, 242)",
            "realtime_text": "#e8eaf0",
            "realtime_text_secondary": "rgba(232, 234, 240, 0.7)",
            "realtime_tag_bg": "rgba(152, 160, 192, 0.15)",
            "realtime_tag_border": "rgba(152, 160, 192, 0.3)",
            "system_border": "#484a58",
            "system_accent": "#7a82a0",
            "send_btn_start": "#7a82a0",
            "send_btn_end": "#606888",
            "send_btn_hover_start": "#8890b0",
            "send_btn_hover_end": "#707898",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#8890b0",
            "timeline_node_visible": "#98a0c0",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#98a0c0",
            "ring_normal": "#7a82a0",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(120, 130, 160, 0.15)",
            "branch_label_border": "rgba(120, 130, 160, 0.3)",
        },
    },
    "jade": {
        "name": "翡翠青",
        "window": {
            "gradient_start": "rgba(8, 20, 18, 255)",
            "gradient_end": "rgba(14, 30, 28, 255)",
        },
        "background": {
            "chat_list": {
                "image": ":/icons/fox_bg.png",
                "opacity": 0.1,
                "enabled": True,
            }
        },
        "colors": {
            "card_bg": "rgba(22, 42, 38, 232)",
            "card_bg_solid": "rgba(22, 42, 38, 250)",
            "content_bg": "#18322e",
            "border": "#2d5a52",
            "border_accent": "#58c9b8",
            "text_primary": "#e0f5f0",
            "text_secondary": "rgba(224, 245, 240, 0.74)",
            "text_muted": "#88a8a0",
            "accent": "#3aa89a",
            "accent_warm": "#d4b860",
            "hover_bg": "rgba(58, 168, 154, 0.12)",
            "selected_bg": "rgba(58, 168, 154, 0.32)",
            "capsule_bg": "rgba(18, 38, 34, 185)",
            "capsule_border": "rgba(45, 90, 82, 205)",
            "user_card_bg": "rgba(12, 48, 44, 170)",
            "user_card_accent": "#60d0c0",
            "user_card_text": "#e0f5f0",
            "user_card_muted": "#88b0a8",
            "assistant_card_bg": "rgba(40, 38, 22, 170)",
            "assistant_card_accent": "#c8a860",
            "assistant_card_text": "#f0eed8",
            "assistant_card_muted": "#a8a888",
            "agent_btn_text": "#88b0a8",
            "agent_btn_text_active": "#58c9b8",
            "agent_btn_bg_active": "rgba(88, 201, 184, 0.2)",
            "agent_btn_separator": "rgba(45, 90, 82, 150)",
            "input_bg_start": "rgba(12, 28, 26, 150)",
            "input_bg_end": "rgba(18, 36, 32, 150)",
            "input_focus_bg_start": "rgba(14, 32, 28, 220)",
            "input_focus_bg_end": "rgba(20, 40, 36, 220)",
            "input_text": "#d0e8e4",
            "input_focus_text": "#e0f5f0",
            "input_border": "#2d5a52",
            "input_focus_border": "#3aa89a",
            "input_placeholder": "rgba(208, 232, 228, 0.4)",
            "realtime_border": "#3aa89a",
            "realtime_accent": "#58c9b8",
            "realtime_accent_warm": "#d4b860",
            "realtime_success": "#2ecc71",
            "realtime_error": "#e74c3c",
            "realtime_bg": "rgba(14, 34, 30, 242)",
            "realtime_text": "#e0f5f0",
            "realtime_text_secondary": "rgba(224, 245, 240, 0.7)",
            "realtime_tag_bg": "rgba(88, 201, 184, 0.15)",
            "realtime_tag_border": "rgba(88, 201, 184, 0.3)",
            "system_border": "#2d5a52",
            "system_accent": "#3aa89a",
            "send_btn_start": "#3aa89a",
            "send_btn_end": "#2d8878",
            "send_btn_hover_start": "#58c9b8",
            "send_btn_hover_end": "#3aa89a",
            "timeline_node": "#5A5A5A",
            "timeline_node_hover": "#58c9b8",
            "timeline_node_visible": "#58c9b8",
            "timeline_node_selected": "#FFA500",
            "timeline_line": "#3A3A3A",
            "timeline_line_progress": "#58c9b8",
            "ring_normal": "#3aa89a",
            "ring_warning": "#f6c453",
            "ring_danger": "#ff6b6b",
            "ring_compacted": "#9b59b6",
            "branch_label_bg": "rgba(58, 168, 154, 0.15)",
            "branch_label_border": "rgba(58, 168, 154, 0.3)",
        },
    },
}


def _get_themes_dir() -> str:
    """获取主题目录路径 - 统一使用 get_app_data_dir"""
    from app.utils.utils import get_app_data_dir
    path = get_app_data_dir() / "themes"
    os.makedirs(str(path), exist_ok=True)
    return str(path)



class ThemeManager:
    """主题管理器 - 单例"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._themes: Dict[str, dict] = {}
        self._themes_dir = _get_themes_dir()
        self._ensure_themes_dir()
        self._ensure_builtin_themes()
        self._load_themes()

    def _ensure_themes_dir(self):
        """确保主题目录存在"""
        os.makedirs(self._themes_dir, exist_ok=True)

    def _ensure_builtin_themes(self):
        """首次运行生成内置主题"""
        existing = self._list_yaml_files()
        if existing:
            return  # 已有主题文件，不覆盖

        logger.info(f"[ThemeManager] 首次运行，生成内置主题到 {self._themes_dir}")
        for theme_id, theme_data in _BUILTIN_THEMES.items():
            self._write_theme_file(theme_id, theme_data)

    def _list_yaml_files(self) -> List[str]:
        """列出主题目录下所有 yaml 文件"""
        if not os.path.isdir(self._themes_dir):
            return []
        files = []
        for f in os.listdir(self._themes_dir):
            if f.endswith((".yaml", ".yml")):
                files.append(os.path.join(self._themes_dir, f))
        return sorted(files)

    def _write_theme_file(self, theme_id: str, theme_data: dict):
        """将主题写入 YAML 文件"""
        filepath = os.path.join(self._themes_dir, f"{theme_id}.yaml")
        # 构建 YAML 数据
        yaml_data = {
            "name": theme_data.get("name", theme_id),
            "id": theme_id,
            "window": theme_data.get("window", {}),
        }
        # background
        bg = theme_data.get("background", {})
        if bg:
            yaml_data["background"] = bg
        # colors
        colors = theme_data.get("colors", {})
        if colors:
            yaml_data["colors"] = colors

        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(yaml_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        logger.info(f"[ThemeManager] 已生成主题: {filepath}")

    def _load_themes(self):
        """加载所有主题文件"""
        self._themes = {}
        for filepath in self._list_yaml_files():
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not data or not data.get("id"):
                    continue
                theme_id = data["id"]
                self._themes[theme_id] = data
                logger.debug(f"[ThemeManager] 加载主题: {theme_id} from {filepath}")
            except Exception as e:
                logger.warning(f"[ThemeManager] 加载主题失败 {filepath}: {e}")

        if not self._themes:
            logger.warning("[ThemeManager] 没有加载到任何主题，使用内置默认")
            self._themes = _BUILTIN_THEMES

    def list_themes(self) -> Dict[str, str]:
        """列出所有可用主题 {id: name}"""
        return {tid: data.get("name", tid) for tid, data in self._themes.items()}

    def get_theme(self, theme_id: str) -> Optional[dict]:
        """获取指定主题数据"""
        return self._themes.get(theme_id)

    def get_theme_value(self, theme_id: str, key: str, default: str = None) -> str:
        """从主题的 colors 中获取颜色值"""
        theme = self._themes.get(theme_id)
        if not theme:
            return default
        colors = theme.get("colors", {})
        return colors.get(key, default)

    def get_theme_window(self, theme_id: str) -> dict:
        """获取窗口背景配置"""
        theme = self._themes.get(theme_id) or {}
        return theme.get("window", {})

    def get_theme_background(self, theme_id: str) -> dict:
        """获取背景图片配置"""
        theme = self._themes.get(theme_id) or {}
        return theme.get("background", {})

    def get_current_theme_id(self) -> str:
        """获取当前选中的主题 ID"""
        try:
            from app.utils.config import Settings
            return Settings.get_instance().ui_theme_style.value
        except Exception:
            return "midnight"

    def get_current_theme(self) -> dict:
        """获取当前主题的完整 colors 字典"""
        theme_id = self.get_current_theme_id()
        return self.get_theme(theme_id) or {}

    def get_current_colors(self) -> dict:
        """获取当前主题的 colors 部分"""
        theme = self.get_current_theme()
        return theme.get("colors", {})

    def reload(self):
        """重新加载所有主题"""
        self._load_themes()


# 全局单例
theme_manager = ThemeManager()
