# -*- coding: utf-8 -*-
"""上下文策略注册表 — tier 与预算解析器按槽位注册，供 ContextPipeline 消费。

规则：
- register_tier：同 order 后注册者覆盖先注册者（热更新换新版自然生效）；
  不同 order 按序插入。内置 tier 由 plugins/system-context 系统插件提供。
- 零注册时 resolve_chain 返回空链 → 语义「不做任何上下文管理」= 全量发送，
  不抛错（与 StorageRegistry 的 noop 兜底不同，此处不改写入语义，仅打 warning）。
- 热重载经 unregister_source("plugin:<name>") 精准清理（watcher 分派）。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.plugins.contracts.context_policy import ContextBudgetResolver, ContextTier


class ContextPolicyRegistry:
    def __init__(self) -> None:
        self._tiers: Dict[str, Tuple[ContextTier, str]] = {}  # id -> (tier, source)
        self._resolvers: Dict[str, Tuple[ContextBudgetResolver, str]] = {}
        self._active_resolver: str = "builtin"
        self._lock = threading.Lock()

    def register(self, item: Any, source: str = "") -> None:
        """loader 代理统一入口：按形状分派到 register_tier / register_budget_resolver。

        runtime_component_loader 的 _RegistryProxy.register 只会调 registry.register，
        故此处提供兜底分派；插件代码也可直接调 register_tier / register_budget_resolver。
        """
        if hasattr(item, "order"):
            self.register_tier(item, source)
        elif hasattr(item, "resolve"):
            self.register_budget_resolver(item, source)
        else:
            logger.warning(f"[ContextPolicyRegistry] 未知组件形状，忽略: {type(item).__name__}")

    def register_tier(self, tier: ContextTier, source: str = "") -> None:
        with self._lock:
            prev = self._tiers.get(tier.id)
            self._tiers[tier.id] = (tier, source)
        if prev is not None and prev[1] != source:
            logger.info(
                f"[ContextPolicyRegistry] tier '{tier.id}' 由 {prev[1] or '内置'} 切换为 {source or '内置'}"
            )

    def register_budget_resolver(self, resolver: ContextBudgetResolver, source: str = "") -> None:
        with self._lock:
            self._resolvers[resolver.id] = (resolver, source)

    def unregister_source(self, source: str) -> None:
        with self._lock:
            for k in [k for k, (_t, s) in self._tiers.items() if s == source]:
                del self._tiers[k]
            for k in [k for k, (_r, s) in self._resolvers.items() if s == source]:
                del self._resolvers[k]

    def resolve_chain(self, stage: str) -> List[ContextTier]:
        """按 order 升序返回该 stage 生效的 tier 链。"""
        with self._lock:
            items = list(self._tiers.values())
        chain = [t for (t, _s) in items if stage in (getattr(t, "stages", None) or frozenset())]
        return sorted(chain, key=lambda t: getattr(t, "order", 0))

    def get_budget_resolver(self) -> Optional[ContextBudgetResolver]:
        with self._lock:
            item = self._resolvers.get(self._active_resolver)
            if item is None and self._resolvers:
                item = next(iter(self._resolvers.values()))
        return item[0] if item else None

    def set_active_resolver(self, resolver_id: str) -> bool:
        with self._lock:
            if resolver_id in self._resolvers:
                self._active_resolver = resolver_id
                return True
            return False

    def tiers(self) -> Dict[str, ContextTier]:
        with self._lock:
            return {k: v[0] for k, v in self._tiers.items()}

    @staticmethod
    def get_instance() -> "ContextPolicyRegistry":
        global _instance
        if _instance is not None:
            return _instance
        with _instance_lock:
            if _instance is None:
                _instance = ContextPolicyRegistry()
            return _instance


_instance: Optional[ContextPolicyRegistry] = None
_instance_lock = threading.Lock()
