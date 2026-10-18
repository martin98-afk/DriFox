# -*- coding: utf-8 -*-
"""能力池视图 —— 按插件声明裁剪的能力访问面。

设计要点：
- 惰性求值：provider 只在首次 ``pool[cap_id]`` 时执行并缓存（构建池不跑 40 个 provider）
- 首次触达记一行 [CapabilityAudit]（high 级用 WARNING），不刷屏
- 未授权抛 CapabilityDenied，消息里带可直接照做的修复指引
- provider 单参 (host) 或双参 (host, PoolContext)，按参数个数分派
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set

from loguru import logger

from app.plugins.capabilities import catalog
from app.plugins.contracts.capabilities import CapabilityLevel, PoolContext


def provider_arity(provider: Any) -> int:
    """provider 接受的定位参数个数（1 = (host)，2 = (host, ctx)）。

    registry 注册时会把结果缓存到 provider._cap_arity；未经过 registry 的
    provider（测试直构、手工创建）在此实时探测，保证两种路径行为一致。
    """
    cached = getattr(provider, "_cap_arity", None)
    if cached is not None:
        return int(cached)
    try:
        params = [
            p
            for p in inspect.signature(provider).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        arity = len(params)
    except (TypeError, ValueError):
        arity = 1
    try:
        provider._cap_arity = arity
    except (AttributeError, TypeError):
        pass
    return arity


class CapabilityDenied(PermissionError):
    """插件未获授权访问某能力。"""

    def __init__(self, plugin: str, cap_id: str, declared: Optional[Set[str]]):
        name = plugin or "(builtin)"
        if declared is None:
            hint = "该插件未声明 capabilities 清单，已按兼容期规则处理"
        else:
            hint = f'在 .drifox-plugin/plugin.json 的 capabilities 数组加入 "{cap_id}" 后重载插件'
        super().__init__(f"插件 {name} 未获授权能力 {cap_id}。{hint}")
        self.plugin = plugin
        self.cap_id = cap_id
        self.declared = declared


class CapabilityPool(Mapping):
    """插件能力池视图（dict 风格，只读）。"""

    def __init__(
        self,
        *,
        plugin: str,
        surface: str,
        host: Any,
        declared: Optional[Set[str]],
        providers: Dict[str, Any],
        ungated: Set[str],
        registry: Any = None,
    ) -> None:
        self._plugin = plugin or ""
        self._surface = surface
        self._host = host
        self._declared = declared
        self._providers = dict(providers)
        self._ungated = set(ungated)
        self._registry = registry
        self._values: Dict[str, Any] = {}
        self._audited: Set[str] = set()

    # ── Mapping 协议 ──

    def __contains__(self, cap_id: object) -> bool:
        return isinstance(cap_id, str) and cap_id in self._allowed()

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._allowed()))

    def __len__(self) -> int:
        return len(self._allowed())

    def __getitem__(self, cap_id: str) -> Any:
        if cap_id not in self._allowed():
            raise CapabilityDenied(self._plugin, cap_id, self._declared)
        if cap_id in self._values:
            return self._values[cap_id]
        self._audit(cap_id)
        provider = self._providers.get(cap_id)
        self._values[cap_id] = None if provider is None else self._call(provider)
        return self._values[cap_id]

    # ── 查询辅助 ──

    def names(self) -> List[str]:
        """已放行的能力 ID（审核用）。"""
        return sorted(self._allowed())

    def group(self, group: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for cap_id in self._allowed():
            spec = catalog.get(cap_id)
            if spec is not None and spec.group == group:
                out[cap_id] = self[cap_id]
        return out

    def declared(self) -> Optional[Set[str]]:
        return None if self._declared is None else set(self._declared)

    # ── 内部 ──

    def _allowed(self) -> Set[str]:
        return set(self._providers) | self._ungated

    def _call(self, provider: Any) -> Any:
        if provider_arity(provider) >= 2:
            return provider(self._host, PoolContext(self._plugin, self._surface, self._registry))
        return provider(self._host)

    def _audit(self, cap_id: str) -> None:
        if cap_id in self._audited:
            return
        self._audited.add(cap_id)
        spec = catalog.get(cap_id)
        level = spec.level.value if spec else "unknown"
        msg = f"[CapabilityAudit] plugin={self._plugin or '(builtin)'} cap={cap_id} level={level}"
        if spec is not None and spec.level is CapabilityLevel.HIGH:
            logger.warning(msg)
        else:
            logger.info(msg)
