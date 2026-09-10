# -*- coding: utf-8 -*-
"""zero —— Python 版可逆插件内核（对标 cordis）。

设计原点：所有对上下文的变更都归结为 ``ctx.effect()`` 一个原语，
因此「通过上下文做的任何操作都自动可追踪、可恢复」是结构事实而非约定。

分层：
    EffectScope  副作用栈（唯一原语，逆序回滚）
    Context      依赖容器 + 副作用边界 + 插件挂载点
    EventEmitter emit / waterfall / parallel / serial

当前阶段：M0（effect 原语 + Context + 生命周期）。
"""

from zero.context import (
    EV_DISPOSE,
    EV_FORK,
    EV_READY,
    Context,
    create_context,
)
from zero.effect import EffectScope, current_scope
from zero.errors import (
    CircularDependency,
    DisposedError,
    MissingDependency,
    PluginError,
    ZeroError,
)
from zero.events import EventEmitter
from zero.registry import Registry
from zero.service import InjectSpec, Service, get_inject_spec, inject

__version__ = "0.1.0"

__all__ = [
    "EV_DISPOSE",
    "EV_FORK",
    "EV_READY",
    "Context",
    "CircularDependency",
    "DisposedError",
    "EffectScope",
    "EventEmitter",
    "InjectSpec",
    "MissingDependency",
    "PluginError",
    "Registry",
    "Service",
    "ZeroError",
    "__version__",
    "create_context",
    "current_scope",
    "get_inject_spec",
    "inject",
]
