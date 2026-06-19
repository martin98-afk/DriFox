# -*- coding: utf-8 -*-
"""
对话引擎模块 — 所有对话引擎的统一入口

注意：通过 __getattr__ 实现懒加载，避免导入 engines 时触发全量引擎加载。
"""

import typing as _typing

_LAZY_IMPORTS: dict[str, tuple[str, str | None]] = {
    "BaseEngine":               ("app.core.engines.base", "BaseEngine"),
    "UIEngine":                 ("app.core.engines.ui", "UIEngine"),
    "ChatEngine":               ("app.core.engines.ui", "ChatEngine"),
    "GatewayEngine":            ("app.core.engines.gateway", "GatewayEngine"),
    "AutoLoopEngine":           ("app.core.engines.auto_loop", "AutoLoopEngine"),
    "LoopState":                ("app.core.engines.auto_loop", "LoopState"),
    "AutoLoopConfig":           ("app.core.engines.auto_loop", "AutoLoopConfig"),
    "AutoLoopPromptComposer":   ("app.core.engines.auto_loop", "AutoLoopPromptComposer"),
}

__all__ = list(_LAZY_IMPORTS.keys())


def __getattr__(name: str) -> _typing.Any:
    """PEP 562 懒加载：访问时才导入对应子模块"""
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_path, attr_name = _LAZY_IMPORTS[name]
    import importlib as _importlib

    module = _importlib.import_module(module_path)
    value = getattr(module, attr_name) if attr_name else module

    # 缓存到模块命名空间
    globals()[name] = value
    return value
