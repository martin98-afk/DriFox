# -*- coding: utf-8 -*-
"""
Workers 模块 - 包含各种执行器和任务类

[PERF] PEP 562 懒加载：chat_worker 顶层拉 openai 全家（~500ms）、shell_task 拉
app.tools（~700ms），若在包 __init__ 顶层 from-import，则导入 workers 下任何
子模块都会连带加载全部兄弟模块。改为 __getattr__ 按需导入后：
- `from app.core.workers.chat_worker import X` 只加载 chat_worker 本身
- `from app.core.workers import X` 首次访问时才加载对应子模块
行为与原顶层 from-import 完全等价（同一模块对象、同一符号）。
"""

import typing as _typing

# 懒加载映射表：{属性名 -> (模块路径, [导出符号])}
_LAZY_IMPORTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "error_handler": ("app.core.workers.error_handler", ()),
    "cache_tracker": (
        "app.core.workers.cache_tracker",
        ("AggregatedCacheStats", "CacheHitRateTracker", "CacheStats"),
    ),
    "chat_worker": ("app.core.workers.chat_worker", ("OpenAIChatWorker",)),
    "shell_task": ("app.core.workers.shell_task", ("ShellExecutionTask",)),
    "subagent_worker": ("app.core.workers.subagent_worker", ("SubAgentExecutor", "SubAgentManager")),
    "topic_summary": ("app.core.workers.topic_summary", ("TopicSummaryTask",)),
}

# 符号级映射：{导出符号 -> (模块路径, 符号名)}，供 from-import 击穿时解析
_SYMBOL_IMPORTS: dict[str, tuple[str, str]] = {}
for _mod, _names in _LAZY_IMPORTS.values():
    for _n in _names:
        _SYMBOL_IMPORTS[_n] = (_mod, _n)


def __getattr__(name: str) -> _typing.Any:
    # 子模块直取（import app.core.workers.chat_worker / from ... import chat_worker）
    if name in _LAZY_IMPORTS:
        import importlib as _importlib

        module = _importlib.import_module(_LAZY_IMPORTS[name][0])
        globals()[name] = module
        return module
    # 符号直取（from app.core.workers import OpenAIChatWorker 等）
    if name in _SYMBOL_IMPORTS:
        module_path, attr = _SYMBOL_IMPORTS[name]
        import importlib as _importlib

        value = getattr(_importlib.import_module(module_path), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_SYMBOL_IMPORTS) + list(_LAZY_IMPORTS)))


__all__ = [
    # Workers
    "OpenAIChatWorker",
    "SubAgentExecutor",
    "SubAgentManager",
    # Tasks
    "TopicSummaryTask",
    "ShellExecutionTask",
    # Cache Tracker
    "CacheHitRateTracker",
    "CacheStats",
    "AggregatedCacheStats",
    # Error Handler
    "error_handler",
]
