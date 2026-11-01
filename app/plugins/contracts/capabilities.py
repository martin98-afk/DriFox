# -*- coding: utf-8 -*-
"""能力池契约：等级 / 能力定义 / 池上下文 / 池视图 Protocol。

运行时实现见 app/plugins/capabilities/。本文件只放类型，不 import 任何
运行时对象（避免 contracts ↔ capabilities 循环依赖）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Protocol, Tuple, runtime_checkable

# provider 签名：单参 (host) 或双参 (host, PoolContext)
CapabilityProvider = Callable[..., Any]


class CapabilityLevel(str, Enum):
    """审核用语义分级。运行时按插件声明裁剪，不按 level 裁剪。"""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class CapabilitySpec:
    """一条能力定义（catalog 的唯一数据单元）。"""

    id: str
    group: str
    level: CapabilityLevel
    summary: str
    surfaces: FrozenSet[str] = field(default_factory=lambda: frozenset({"ui"}))
    legacy_keys: Tuple[str, ...] = ()
    # pool 取值形态（单一真相源，供 AST 静态检查与调用方自查）：
    #   "factory" — pool 值是闭包/工厂，需再调用一次拿到对象再用（facade 模式，默认）
    #   "value"   — pool 值即终值（dict/宿主实例等），直接用；再调用 = 形态 bug
    # （#23：本次 'dict' object is not callable 的根因即调用方多调了一层）
    value_style: str = "factory"


@dataclass(frozen=True)
class PoolContext:
    """构建池时传给双参 provider 的上下文（插件身份 + 所在 surface）。"""

    plugin: str
    surface: str
    registry: Any


@runtime_checkable
class CapabilityPoolView(Protocol):
    """插件侧看到的能力池视图（dict 风格）。"""

    def __contains__(self, cap_id: object) -> bool: ...

    def __getitem__(self, cap_id: str) -> Any: ...

    def names(self) -> List[str]: ...

    def group(self, group: str) -> Dict[str, Any]: ...
