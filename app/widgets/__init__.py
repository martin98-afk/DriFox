# -*- coding: utf-8 -*-
"""
llm_chatter widgets - 大模型对话框 UI 组件

[PERF] PEP 562 懒加载：本包原顶层 from-import 全家桶（settings 各卡 +
message_card + engines + provider 插件扫描，合计 ~1s）。任何
`from app.widgets.xxx import ...` 都会先执行本 __init__，导致轻模块也被
迫拉起重链。改为 __getattr__ 按需导入，行为等价（同一模块/符号对象）。
"""

import typing as _typing

_LAZY_IMPORTS: dict[str, tuple[str, str | None]] = {
    # 核心卡片（已迁移到 cards/settings/）
    "BaseSettingsCard":   ("app.widgets.cards.settings.base_settings_card", "BaseSettingsCard"),
    "HistoryCard":        ("app.widgets.cards.settings.history_card", "HistoryCard"),
    "get_message_preview": ("app.widgets.cards.settings.history_card", "get_message_preview"),
    "LLMSettingsCard":    ("app.widgets.cards.settings.llm_settings_card", "LLMSettingsCard"),
    "MemoryCardContent":  ("app.widgets.cards.settings.memory_card", "MemoryCardContent"),
    "ModelConfigCard":    ("app.widgets.cards.settings.model_config_card", "ModelConfigCard"),
    "ProviderEditCard":   ("app.widgets.cards.settings.provider_edit_card", "ProviderEditCard"),
    # 输入区
    "AttachmentChip":     ("app.widgets.bottom_input_area", "AttachmentChip"),
    "SendableTextEdit":   ("app.widgets.bottom_input_area", "SendableTextEdit"),
    # 悬浮组件
    "QuestionFloatingWidget": ("app.widgets.cards.floating.question_floating_widget", "QuestionFloatingWidget"),
    # 对话组件
    "CodingPlanRing":     ("app.widgets.coding_plan_ring", "CodingPlanRing"),
    "ContextUsageRing":   ("app.widgets.context_usage_ring", "ContextUsageRing"),
    "ConversationNodePreview": ("app.widgets.conversation_node_preview", "ConversationNodePreview"),
    "MessageCard":        ("app.widgets.message_card", "MessageCard"),
    "create_welcome_card": ("app.widgets.message_card", "create_welcome_card"),
}


def __getattr__(name: str) -> _typing.Any:
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attr_name = _LAZY_IMPORTS[name]
    import importlib as _importlib

    module = _importlib.import_module(module_path)
    value = module if attr_name is None else getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY_IMPORTS)))


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
    "QuestionFloatingWidget",
    "MemoryCardContent",
    "ProviderEditCard",
    "AttachmentChip",
    "SendableTextEdit",
    "CodingPlanRing",
    "ContextUsageRing",
    "ConversationNodePreview",
]
