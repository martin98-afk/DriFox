# -*- coding: utf-8 -*-
"""能力注册表 —— provider 注册 + 插件声明解析 + 能力池构建 + 订阅跟踪。

约定：
- 注册表**不持有窗口引用**；host 由 build_pool 传入，池随 ctx 生命周期回收
- 同一能力可分别注册 ui / tool 两个 surface 的实现（键为 (cap_id, surface)）
- provider 签名 (host) 或 (host, PoolContext)；参数个数探测统一在 pool.provider_arity
"""

from __future__ import annotations

import os
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from loguru import logger

from app.plugins.capabilities import catalog
from app.plugins.capabilities.pool import CapabilityPool
from app.plugins.contracts.capabilities import CapabilityProvider

_UNSET = object()
_CACHE_MAX = 512


class _Entry:
    __slots__ = ("provider", "owner", "experimental")

    def __init__(self, provider: CapabilityProvider, owner: str, experimental: bool):
        self.provider = provider
        self.owner = owner
        self.experimental = experimental


class CapabilityRegistry:
    """能力注册表单例。"""

    _instance: Optional["CapabilityRegistry"] = None

    # 无权限的元能力：不参与声明裁剪
    UNGATED: Tuple[str, ...] = ("state.kv", "plugin.log")

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str], _Entry] = {}
        # 旧键具名变体：(legacy_key, surface) -> 有该旧键专属实现的 provider。
        # 一个能力（授权单位）可对应多个语义不同的旧键（API 单位）：
        # 例如 agent.read 同时映射 get_agent_manager（取对象）与 get_agent_prompt
        # （按名取字符串），两者投影若取同一值会直接报错。
        self._legacy_entries: Dict[Tuple[str, str], CapabilityProvider] = {}
        self._declared_cache: Dict[str, tuple] = {}
        self._plugin_subs: Dict[str, List[Tuple[Any, Any]]] = {}
        # host 无关的瞬时池缓存：(plugin, surface, declared 签名) -> pool。
        # 工具/hook 类调用点每次现建池，若不缓存则重建的池 _audited 恒空
        # → 审计日志每次调用都刷一行（实测 tool.read 满屏）。
        self._hostless_pools: Dict[tuple, CapabilityPool] = {}
        # 全局审计去重：(plugin, surface, cap_id) -> 已记过。
        # 池实例可以有多个（每 ctx 一个），"某插件用过某能力"全局只需记一次。
        self._audited: Set[Tuple[str, str, str]] = set()

    # ── 单例 ──

    @classmethod
    def get_instance(cls) -> "CapabilityRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    # ── 注册 ──

    @staticmethod
    def _arity(provider: Any) -> int:
        """委托 pool.provider_arity（唯一探测实现，避免两处漂移）。"""
        from app.plugins.capabilities.pool import provider_arity

        return provider_arity(provider)

    def register(
        self,
        cap_id: str,
        provider: CapabilityProvider,
        *,
        surface: str = "ui",
        owner: str = "core",
        experimental: bool = False,
        legacy_key: str = "",
    ) -> None:
        """注册一个能力的 provider 实现。

        未在 catalog 登记的 ID 必须显式 experimental=True，否则抛 KeyError。

        legacy_key: 非空时注册为该旧服务键的**专属**实现（pool.legacy() 取用），
        不影响 pool[cap_id] 的主实现。用于一个能力对应多个语义不同的旧键的场景。
        """
        spec = catalog.get(cap_id)
        if spec is None:
            if not experimental:
                raise KeyError(f"能力 {cap_id} 未在 catalog 登记；确需注册请传 experimental=True")
            logger.warning(f"[CapabilityExperimental] 注册未登记能力 cap={cap_id} owner={owner}")
        self._arity(provider)
        if legacy_key:
            self._legacy_entries[(legacy_key, surface)] = provider
            return
        self._entries[(cap_id, surface)] = _Entry(provider, owner, experimental)

    def unregister(self, cap_id: str, *, surface: Optional[str] = None) -> None:
        if surface is None:
            for key in [k for k in self._entries if k[0] == cap_id]:
                self._entries.pop(key, None)
            for key in [k for k in self._legacy_entries if k[1] == cap_id]:
                self._legacy_entries.pop(key, None)
            return
        self._entries.pop((cap_id, surface), None)
        for key in [k for k in self._legacy_entries if k[1] == cap_id]:
            self._legacy_entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()
        self._legacy_entries.clear()
        self._declared_cache.clear()
        self._plugin_subs.clear()

    def registered(self, surface: Optional[str] = None) -> List[str]:
        ids = {k[0] for k in self._entries if surface is None or k[1] == surface}
        return sorted(ids)

    def has(self, cap_id: str, surface: str) -> bool:
        return (cap_id, surface) in self._entries

    # ── 池构建 ──

    def build_pool(
        self,
        *,
        surface: str,
        host: Any,
        plugin: str = "",
        declared: Any = _UNSET,
    ) -> CapabilityPool:
        """构建某插件在某 surface 的能力池视图。

        declared 传 _UNSET（缺省）时按插件 manifest 解析；
        显式传 None = 兼容期全量放行；传集合 = 按集合裁剪。
        """
        decl = self.resolve_declared(plugin) if declared is _UNSET else declared

        # host 无关的调用（tool/hook/context tier：host=None 且声明显式给出）
        # 走缓存：避免每次调用重建池导致审计日志刷屏。
        if host is None and declared is not _UNSET:
            sig = (
                plugin,
                surface,
                None if decl is None else tuple(sorted(decl)),
            )
            cached = self._hostless_pools.get(sig)
            if cached is not None:
                return cached

        pool = self._make_pool(surface, host, plugin, decl)
        if host is None and declared is not _UNSET:
            if len(self._hostless_pools) >= _CACHE_MAX:
                self._hostless_pools.clear()
            self._hostless_pools[sig] = pool
        return pool

    def _make_pool(
        self, surface: str, host: Any, plugin: str, decl: Optional[FrozenSet[str]]
    ) -> CapabilityPool:
        allowed, ungated = self._select(surface, decl)
        providers = {cid: self._entries[(cid, surface)].provider for cid in (allowed | ungated)}
        legacy = {
            key[0]: provider
            for key, provider in self._legacy_entries.items()
            if key[1] == surface
        }
        return CapabilityPool(
            plugin=plugin,
            surface=surface,
            host=host,
            declared=None if decl is None else set(decl),
            providers=providers,
            ungated=ungated,
            legacy_providers=legacy,
            registry=self,
        )

    def _select(
        self, surface: str, declared: Optional[FrozenSet[str]]
    ) -> Tuple[Set[str], Set[str]]:
        ungated = {cid for cid in self.UNGATED if (cid, surface) in self._entries}
        if declared is None:
            allowed = {cid for (cid, s) in self._entries if s == surface}
        else:
            allowed = {cid for (cid, s) in self._entries if s == surface and cid in declared}
        return allowed - ungated, ungated

    # ── 声明解析 ──

    def resolve_declared(self, plugin: str) -> Optional[FrozenSet[str]]:
        """读插件 manifest 的 capabilities 声明。

        返回 None = 兼容期（未声明且 api_version < 2）→ 全量放行
        返回集合 = 按清单裁剪（空集即只剩 UNGATED）

        PluginManager 查询异常时沿用上次缓存；无缓存则按最严口径（空清单）
        处理——管理器抖动不该把已声明收紧的插件放大成全量权限。
        """
        if not plugin:
            return None
        info = None
        query_failed = False
        try:
            from app.plugins.managers.plugin_manager import PluginManager

            info = PluginManager.get_instance().get_plugin(plugin)
        except Exception as e:
            query_failed = True
            logger.warning(f"[CapabilityAudit] 查询插件 {plugin} 失败: {e}")
        manifest = dict(getattr(info, "manifest", None) or {}) if info is not None else {}
        path = getattr(info, "path", "") if info is not None else ""
        try:
            mtime = os.stat(path).st_mtime if path else 0.0
        except OSError:
            mtime = 0.0

        cached = self._declared_cache.get(plugin)
        if query_failed:
            return cached[1] if cached is not None else frozenset()

        raw = manifest.get("capabilities")
        # 缓存签名必须含声明内容：插件路径可能为空（测试替身 / 无固定目录来源），
        # 只靠 (path, mtime) 会在这种情况下永久命中过期结果。
        signature = (str(path), mtime, repr(raw), manifest.get("api_version"))
        if cached is not None and cached[0] == signature:
            return cached[1]

        if isinstance(raw, list):
            resolved: Optional[FrozenSet[str]] = catalog.expand([str(x) for x in raw])
        elif int(manifest.get("api_version") or 1) >= 2:
            resolved = frozenset()
            logger.warning(
                f"[CapabilityAudit] plugin={plugin} api_version>=2 但未写 capabilities，按空清单处理"
            )
        else:
            resolved = None
            logger.warning(f"[CapabilityAudit] plugin={plugin} 未声明能力清单，按旧语义全量注入")
        if len(self._declared_cache) >= _CACHE_MAX:
            self._declared_cache.clear()
        self._declared_cache[plugin] = (signature, resolved)
        return resolved

    # ── 订阅跟踪（stream.subscribe 等需要显式释放的连接） ──

    def track_subscription(self, plugin: str, signal: Any, slot: Any) -> None:
        self._plugin_subs.setdefault(plugin or "", []).append((signal, slot))

    def release_plugin(self, plugin: str) -> int:
        """断开该插件登记的全部订阅，返回成功断开数。卸载插件时调用。"""
        pairs = self._plugin_subs.pop(plugin or "", [])
        n = 0
        for signal, slot in pairs:
            try:
                signal.disconnect(slot)
                n += 1
            except (TypeError, RuntimeError):
                pass
        return n
