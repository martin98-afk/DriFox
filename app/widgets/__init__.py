# -*- coding: utf-8 -*-
"""
llm_chatter widgets - 大模型对话框 UI 组件
"""

# 核心卡片（已迁移到 cards/settings/）
from app.widgets.cards.settings.base_settings_card import BaseSettingsCard
from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard
from app.widgets.cards.settings.history_card import HistoryCard, get_message_preview
from app.widgets.cards.settings.model_config_card import ModelConfigCard
from app.widgets.cards.settings.memory_card import MemoryCardContent
from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

# 悬浮组件（已迁移到 cards/floating/）
from app.widgets.cards.floating.tool_floating_widget import ToolFloatingWidget
from app.widgets.cards.floating.sub_agent_floating_widget import SubAgentFloatingWidget
from app.widgets.cards.floating.todo_floating_widget import TodoFloatingWidget
from app.widgets.cards.floating.question_floating_widget import QuestionFloatingWidget

# 对话组件
from app.widgets.message_card import MessageCard, create_welcome_card
from app.widgets.bottom_input_area import SendableTextEdit
from app.widgets.context_usage_ring import ContextUsageRing
from app.widgets.conversation_node_preview import ConversationNodePreview

# 对话框
from app.widgets.file_undo_dialog import FileUndoPreviewDialog

# Gateway
from app.widgets.cards.settings.gateway_setting_card import GatewaySettingCard

__all__ = [
    # 核心卡片
    "BaseSettingsCard",
    "LLMSettingsCard",
    "HistoryCard",
    "get_message_preview",
    "MessageCard",
    "create_welcome_card",
    "ModelConfigCard",
    # 悬浮组件
    "ToolFloatingWidget",
    "SubAgentFloatingWidget",
    "TodoFloatingWidget",
    "QuestionFloatingWidget",
    # 对话组件
    "SendableTextEdit",
    "ContextUsageRing",
    "ConversationNodePreview",
    "MemoryCardContent",
    # 对话框
    "FileUndoPreviewDialog",
    # Gateway
    "GatewaySettingsWidget",
    "WeComSettingsCard",
    "DingTalkSettingsCard",
]