# -*- coding: utf-8 -*-
"""插件注册表（M2）。

把「插件的登记 / 加载顺序 / 整体卸载」从调用方手里收走：

- ``registry.add(plugin, provides=("db",))`` 声明式登记（不加载）
- ``registry.load_all()`` 按服务依赖拓扑排序后依次加载，循环依赖直接报错
- ``registry.delete(name)`` 停用插件全部 fork；它提供的服务随之撤销，
   依赖者由 M1 的回滚机制自动停用

服务依赖的加载顺序来自 ``@inject`` 声明的 required 服务与 ``provides``
声明的对应关系；无人提供的服务视为外部已有（如根上下文手工提供），
不参与排序。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from zero.errors import CircularDependency, MissingDependency, PluginError, ZeroError
from zero.service import get_inject_spec

if TYPE_CHECKING:
    from zero.context import Context

logger = logging.getLogger(__name__)

__all__ = ["Registry"]

Plugin = Callable[..., Any]


class _DuplicatePlugin(ZeroError):
    """同名插件重复登记。"""


@dataclass
class _Entry:
    """一条插件登记。"""

    name: str
    plugin: Plugin
    config: Any = None
    provides: Tuple[str, ...] = ()
    forks: List["Context"] = field(default_factory=list)


class Registry:
    """插件注册表：登记 → 排序加载 → 整体卸载。"""

    def __init__(self, ctx: "Context") -> None:
        self._ctx = ctx
        self._entries: Dict[str, _Entry] = {}

    # ── 登记 ──────────────────────────────────────────────────────────
    def add(
        self,
        plugin: Optional[Plugin] = None,
        *,
        name: str = "",
        provides: Tuple[str, ...] = (),
        config: Any = None,
    ) -> Any:
        """登记插件（不加载）。支持装饰器用法：

        @root.registry.add(name="database", provides=("db",))
        def database(ctx):
            ctx.provide()(Database)
        """
        if plugin is None:

            def _deco(target: Plugin) -> Plugin:
                self._register(target, name, provides, config)
                return target

            return _deco
        self._register(plugin, name, provides, config)
        return plugin

    def _register(self, plugin: Plugin, name: str, provides: Tuple[str, ...], config: Any) -> None:
        resolved = name or getattr(plugin, "__name__", type(plugin).__name__)
        if resolved in self._entries:
            raise _DuplicatePlugin(f"插件 {resolved!r} 已登记")
        self._entries[resolved] = _Entry(name=resolved, plugin=plugin, config=config, provides=tuple(provides))

    # ── 查询 ──────────────────────────────────────────────────────────
    def names(self) -> List[str]:
        return sorted(self._entries)

    def get(self, name: str) -> Optional[_Entry]:
        return self._entries.get(name)

    def forks_of(self, name: str) -> List["Context"]:
        entry = self._entries.get(name)
        return list(entry.forks) if entry else []

    # ── 加载 ──────────────────────────────────────────────────────────
    def load_all(self) -> List["Context"]:
        """按依赖顺序加载全部已登记插件，返回本次新加载的 fork。

        单插件加载失败（如依赖未就绪）只停用该插件并记日志，不阻断其余插件。
        """
        order = self._topo_order()
        forks: List["Context"] = []
        for name in order:
            try:
                forks.append(self.load(name))
            except MissingDependency as e:
                logger.warning(f"[zero] 插件 {name!r} 依赖未就绪，停用: {e}")
            except PluginError as e:
                logger.error(f"[zero] 插件 {name!r} 加载失败，已回滚: {e}")
        return forks

    def load(self, name: str) -> "Context":
        """加载单个插件（不校验顺序，一般经 load_all 调用）。"""
        entry = self._entries[name]
        fork = self._ctx.use(entry.plugin, entry.config)
        entry.forks.append(fork)
        logger.debug(f"[zero] registry 加载插件 {name!r}")
        return fork

    # ── 卸载 ──────────────────────────────────────────────────────────
    def delete(self, name: str) -> int:
        """停用插件并移除登记。返回停用的 fork 数。

        可重用插件（多次 load）的所有 fork 一并停用；它提供的服务随 fork
        销毁而撤销，依赖者由 Context 的回滚机制自动停用。
        """
        entry = self._entries.pop(name, None)
        if entry is None:
            return 0
        stopped = 0
        for fork in list(entry.forks):
            if not fork.disposed:
                fork.dispose()
                stopped += 1
        entry.forks.clear()
        logger.debug(f"[zero] registry 卸载插件 {name!r}（{stopped} 个 fork）")
        return stopped

    # ── 排序 ──────────────────────────────────────────────────────────
    def _topo_order(self) -> List[str]:
        """按服务依赖推导加载顺序（Kahn 拓扑排序，稳定输出）。"""
        provides_index: Dict[str, List[str]] = {}
        for name, entry in self._entries.items():
            for service in entry.provides:
                provides_index.setdefault(service, []).append(name)

        pending: Dict[str, set] = {}
        for name, entry in self._entries.items():
            spec = get_inject_spec(entry.plugin)
            required = set(spec.required) if spec is not None else set()
            providers: set = set()
            for service in required:
                providers.update(provides_index.get(service, ()))
            providers.discard(name)  # 自产自销不构成自环
            pending[name] = providers

        order: List[str] = []
        resolved: set = set()
        while pending:
            ready = sorted(n for n, deps in pending.items() if not (deps - resolved))
            if not ready:
                raise CircularDependency(sorted(pending))
            for name in ready:
                order.append(name)
                resolved.add(name)
                pending.pop(name)
        return order
