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
