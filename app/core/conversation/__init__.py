# app/core/conversation/__init__.py
"""Conversation 模块 - 对话基础设施

注意：通过 __getattr__ 实现懒加载，避免导入 conversation 时触发全量加载。
"""

import typing as _typing

_LAZY_IMPORTS: dict[str, tuple[str, str | None]] = {
    "PermissionStrategy": ("app.core.conversation.config", "PermissionStrategy"),
    "ConversationConfig": ("app.core.conversation.config", "ConversationConfig"),
    "filter_interactive_tools": ("app.core.conversation.config", "filter_interactive_tools"),
    "INTERACTIVE_ONLY_TOOLS": ("app.core.conversation.config", "INTERACTIVE_ONLY_TOOLS"),
    "PermissionCache": ("app.core.conversation.config", "PermissionCache"),
    "ConversationCore": ("app.core.conversation.core", "ConversationCore"),
    "ConversationExecutor": ("app.core.conversation.executor", "ConversationExecutor"),
    "BaseConversationAdapter": ("app.core.conversation.adapters", "BaseConversationAdapter"),
    "UIConversationAdapter": ("app.core.conversation.adapters", "UIConversationAdapter"),
    "GatewayConversationAdapter": ("app.core.conversation.adapters", "GatewayConversationAdapter"),
}

__all__ = list(_LAZY_IMPORTS.keys())


def __getattr__(name: str) -> _typing.Any:
    """PEP 562 懒加载：访问时才导入对应子模块"""
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_path, attr_name = _LAZY_IMPORTS[name]
    import importlib as _importlib

    module = _importlib.import_module(module_path)

    if attr_name is None:
        value = module
    else:
        value = getattr(module, attr_name)

    globals()[name] = value
    return value
