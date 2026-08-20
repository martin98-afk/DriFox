"""设置类卡片模块 - History、Memory、ModelConfig 等配置类卡片

[PERF] PEP 562 懒加载：原顶层全家桶导致任何 settings 卡 import 都拉全部兄弟
（mcp_setting_card → mcp ~750ms、llm_settings_card → gateway、auto_loop_card
→ engines+provider 扫描）。改为 __getattr__ 按需导入，行为等价。
"""

import typing as _typing

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AutoLoopConfigCard":       ("app.widgets.cards.settings.auto_loop_card", "AutoLoopConfigCard"),
    "AutoLoopRunningCard":      ("app.widgets.cards.settings.auto_loop_card", "AutoLoopRunningCard"),
    "BaseSettingsCard":         ("app.widgets.cards.settings.base_settings_card", "BaseSettingsCard"),
    "GiteeCard":                ("app.widgets.cards.settings.gitee_card", "GiteeCard"),
    "HistoryCard":              ("app.widgets.cards.settings.history_card", "HistoryCard"),
    "HookEditCard":             ("app.widgets.cards.settings.hook_setting_card", "HookEditCard"),
    "HookListSettingCard":      ("app.widgets.cards.settings.hook_setting_card", "HookListSettingCard"),
    "SkillListSettingCard":     ("app.widgets.cards.settings.list_setting_card", "SkillListSettingCard"),
    "LLMSettingsCard":          ("app.widgets.cards.settings.llm_settings_card", "LLMSettingsCard"),
    "MCPEditCard":              ("app.widgets.cards.settings.mcp_setting_card", "MCPEditCard"),
    "MemoryCardContent":        ("app.widgets.cards.settings.memory_card", "MemoryCardContent"),
    "TAB_PROJECT_NOTES":        ("app.widgets.cards.settings.memory_card", "TAB_PROJECT_NOTES"),
    "ModelConfigCard":          ("app.widgets.cards.settings.model_config_card", "ModelConfigCard"),
    "ProjectSelectorCardContent": ("app.widgets.cards.settings.project_selector_card", "ProjectSelectorCardContent"),
    "ProviderEditCard":         ("app.widgets.cards.settings.provider_edit_card", "ProviderEditCard"),
    "ProviderListSettingCard":  ("app.widgets.cards.settings.provider_setting_card", "ProviderListSettingCard"),
    "SystemCardFrame":          ("app.widgets.cards.settings.system_card_frame", "SystemCardFrame"),
}


def __getattr__(name: str) -> _typing.Any:
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attr = _LAZY_IMPORTS[name]
    import importlib as _importlib

    value = getattr(_importlib.import_module(module_path), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY_IMPORTS)))


__all__ = [
    "BaseSettingsCard",
    "HistoryCard",
    "MemoryCardContent",
    "TAB_PROJECT_NOTES",
    "ModelConfigCard",
    "AutoLoopConfigCard",
    "AutoLoopRunningCard",
    "LLMSettingsCard",
    "MCPEditCard",
    "HookEditCard",
    "HookListSettingCard",
    "SkillListSettingCard",
    "ProviderListSettingCard",
    "ProviderEditCard",
    "ProjectSelectorCardContent",
    "GiteeCard",
    "SystemCardFrame",
]
