# -*- coding: utf-8 -*-
"""Context —— zero 的核心容器（对应 cordis 的 Context）。

Context 同时承担三种身份：
1. **服务 / 依赖容器**：``set`` 提供、``get`` 沿上下文树向上解析
2. **副作用边界**：所有变更登记在本上下文的作用域，``dispose`` 逆序回滚
3. **插件挂载点**：``use`` 派生子上下文加载插件，子上下文销毁即插件卸载

插件作者只需记住一条铁律：**凡是 zero 不管的资源（连接、文件、线程、
Qt 对象），都包进 ``ctx.effect()`` 返回 disposer。**

生命周期事件：``fork``（派生时）→ ``ready``（加载完成）→ ``dispose``（销毁前）。
"""

from __future__ import annotations

import inspect
import logging
import weakref
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from zero.effect import EffectScope
from zero.errors import DisposedError, MissingDependency, PluginError
from zero.events import EventEmitter
from zero.graph import DependencyGraph, build_graph
from zero.service import InjectSpec, Service, get_inject_spec, resolve_name

if TYPE_CHECKING:
    from zero.registry import Registry

logger = logging.getLogger(__name__)

# ── 生命周期事件名 ──
EV_FORK = "fork"
EV_READY = "ready"
EV_DISPOSE = "dispose"

Plugin = Callable[..., Any]

# 插件通过属性声明依赖（M1 生效，M0 先留常量避免字符串散落）
INJECT_ATTR = "__zero_inject__"


class Context:
    """上下文：依赖容器 + 副作用边界 + 插件挂载点。"""

    def __init__(
        self,
        name: str = "root",
        parent: Optional["Context"] = None,
        scope: Optional[EffectScope] = None,
    ) -> None:
        self.name = name
        self.parent = parent
        self._scope = scope or EffectScope(name=name)
        # 事件总线为应用级共享（对齐 cordis/Koishi）：任何 ctx.emit 全局可见，
        # ctx.on 只是生命周期绑定的订阅点（随上下文销毁自动退订）。
        self._events = parent._events if parent is not None else EventEmitter()
        self._children: List["Context"] = []
        # (realm, key) → 值栈（多次 set 逆序恢复）
        self._values: Dict[Tuple[str, str], List[Any]] = {}
        self._isolate: Dict[str, str] = dict(parent._isolate) if parent else {}
        # 插件重载所需的原始句柄（服务变更时回滚重加载）
        self._plugin: Optional[Plugin] = None
        self._config: Any = None
        # 本上下文提供的服务 (realm, key) → 实例（销毁时触发依赖者回滚）
        self._provided: Dict[Tuple[str, str], Any] = {}
        # 本上下文对应插件的依赖声明（依赖图导出用）
        self._inject_spec: Optional[InjectSpec] = None
        # 依赖反向索引（仅根上下文持有）：(realm, 服务名) → 依赖它的上下文
        self._dependents: Dict[Tuple[str, str], List["Context"]] = {}
        self._restarting = False
        self._registry: Optional["Registry"] = None
        self._disposed = False

    # ── 属性 ──────────────────────────────────────────────────────────
    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def scope(self) -> EffectScope:
        return self._scope

    @property
    def children(self) -> List["Context"]:
        return list(self._children)

    @property
    def root(self) -> "Context":
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    @property
    def registry(self) -> "Registry":
        """插件注册表（懒创建，挂在获得它的上下文上）。"""
        if self._registry is None:
            from zero.registry import Registry

            self._registry = Registry(self)
        return self._registry

    @property
    def path(self) -> str:
        """上下文树路径（诊断用），如 root/pluginA/pluginB。"""
        parts = []
        node: Optional[Context] = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return "/".join(reversed(parts))

    # ── 副作用原语 ────────────────────────────────────────────────────
    def effect(self, callback: Callable[[], Any]) -> Any:
        """登记一个可逆副作用（zero 的唯一变更入口）。"""
        self._assert_alive()
        return self._scope.effect(callback)

    def add_disposer(self, disposer: Callable[[], None]) -> None:
        """直接登记撤销函数（用于外部资源的手动接管）。"""
        self._assert_alive()
        self._scope.add(disposer)

    # ── 依赖 / 服务 ───────────────────────────────────────────────────
    def set(self, key: str, value: Any, *, weak: bool = False) -> Any:
        """提供一个依赖 / 服务（销毁时自动恢复旧值）。

        weak=True：存弱引用（QObject / 窗口实例等重对象），对象被回收后
        该条目自动失效（解析时视为不存在）。对象不支持弱引用时降级强引用。
        """
        self._assert_alive()
        if weak:
            with suppress(TypeError):
                value = weakref.ref(value)
        return self._set_in(key, value, self._realm_for(key))

    def _set_in(self, key: str, value: Any, realm: str) -> Any:
        """set 的核心：写入指定 realm 槽位并触发该槽位的依赖者回滚。"""
        slot = (realm, key)
        stack = self._values.setdefault(slot, [])

        def _provide() -> Callable[[], None]:
            stack.append(value)

            def _undo() -> None:
                if stack:
                    stack.pop()
                if not stack:
                    self._values.pop(slot, None)

            return _undo

        self._scope.effect(_provide)
        # 依赖变更 → 依赖它的插件回滚重启（cordis 的响应式余效应）
        self._restart_dependents(realm, key)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        """解析依赖：域优先、全局兜底的两级查找。

        1. 发起方的域槽 ``(realm, key)`` 沿父链查找（realm 由 ``isolate`` 决定，
           后代继承）
        2. 整条链无域槽命中 → 回落全局槽 ``("", key)`` 沿父链查找
        """
        realm = self._realm_for(key)
        if realm:
            node: Optional[Context] = self
            while node is not None:
                value = _resolve_stack(node._values.get((realm, key)))
                if value is not _MISSING:
                    return value
                node = node.parent
        node = self
        while node is not None:
            value = _resolve_stack(node._values.get(("", key)))
            if value is not _MISSING:
                return value
            node = node.parent
        return default

    def has(self, key: str) -> bool:
        """依赖是否可解析（不看默认值，避免与 None 值混淆）。"""
        sentinel = object()
        return self.get(key, sentinel) is not sentinel

    def require(self, key: str) -> Any:
        """取依赖，缺失即抛 MissingDependency（插件内部用）。"""
        sentinel = object()
        value = self.get(key, sentinel)
        if value is sentinel:
            raise MissingDependency(self.path, [key])
        return value

    def isolate(self, key: str, realm: str) -> "Context":
        """派生子上下文并改变 key 的解析域（空间可组合）。

        同一 key 在不同 realm 下可解析到不同实现，用于多窗口 / 多会话隔离。
        """
        child = self.fork(f"isolate:{key}={realm}")
        child._isolate = {**self._isolate, key: realm}
        return child

    # ── 插件 ──────────────────────────────────────────────────────────
    def fork(self, name: str = "") -> "Context":
        """派生子上下文（父销毁时子先销毁）。"""
        self._assert_alive()
        child = Context(name=name or f"fork{len(self._children)}", parent=self)
        self._children.append(child)
        self._scope.add(child.dispose)  # 父销毁带着子一起回滚
        return child

    def provide(self, name: str = "", impl: Any = None) -> Any:
        """注册带生命周期的服务（``set`` + ``start`` / ``stop`` 托管）。

        两种用法：

            ctx.provide("db", Database(ctx))          # 直接注册实例
            ctx.provide()(Database)                   # 装饰器，自动实例化 Service 子类
        """

        def _register(instance: Any) -> Any:
            key = name or resolve_name(instance, "service")
            # 服务默认全局（注册到根）；若提供者处于某 isolate 域内，
            # 则服务落在该域（多窗口各一份），出域不可见。
            # 撤销 disposer 登记在提供者作用域：提供方销毁 → 服务撤销 → 依赖者停用。
            realm = self._realm_for(key)
            host = self.root
            slot = (realm, key)
            old = host._provided.get(slot)
            if old is not None and old is not instance:
                _call_lifecycle(old, "stop")
            host._set_in(key, instance, realm)
            host._provided[slot] = instance
            _call_lifecycle(instance, "start")
            self.add_disposer(lambda: host._stop_if_current(slot, instance))
            return instance

        if impl is None:
            instance_holder: List[Any] = []

            def _deco(target: Any) -> Any:
                created = target(self) if isinstance(target, type) and issubclass(target, Service) else target
                instance_holder.append(created)
                return _register(created)

            # 支持 @ctx.provide("name") 与 @ctx.provide() 两种写法
            if callable(name):
                target, name = name, ""
                return _deco(target)
            return _deco

        return _register(impl)

    def use(self, plugin: Plugin, config: Any = None) -> "Context":
        """挂载插件：在子上下文中执行，失败则整棵子树回滚。

        插件签名 ``(ctx)`` 或 ``(ctx, config)`` 均可。带 ``@inject`` 声明时：
        required 依赖缺失则**不加载**（压根不进插件体，因此不留半截副作用），
        抛 MissingDependency；optional 缺失照常加载。
        """
        self._assert_alive()
        name = _plugin_name(plugin)
        child = self.fork(name)
        child._plugin = plugin
        child._config = config
        child._inject_spec = get_inject_spec(plugin)
        try:
            child.emit(EV_FORK, child)
            self._check_dependencies(child, plugin, name)
            _invoke_plugin(plugin, child, config)
        except BaseException as e:
            child.dispose()
            if isinstance(e, (PluginError, MissingDependency)):
                raise
            logger.error(f"[zero] 插件 {name!r} 加载失败，已回滚全部副作用: {e!r}")
            raise PluginError(name, e) from e

        child._mark_ready()
        return child

    def _mark_ready(self) -> None:
        self.emit(EV_READY, self)

    # ── 事件 ──────────────────────────────────────────────────────────
    def on(self, name: str, listener: Callable[..., Any]) -> Callable[[], None]:
        """订阅事件（自动登记撤销，随上下文销毁退订）。"""
        self._assert_alive()
        off = self._events.on(name, listener)
        self._scope.add(off)
        return off

    def emit(self, name: str, *args: Any) -> None:
        """广播事件（应用级共享总线：所有上下文可见，与挂载位置无关）。"""
        self._events.emit(name, *args)

    def waterfall(self, name: str, value: Any, *args: Any) -> Any:
        return self._events.waterfall(name, value, *args)

    async def parallel(self, name: str, *args: Any) -> List[Any]:
        return await self._events.parallel(name, *args)

    async def serial(self, name: str, *args: Any) -> List[Any]:
        return await self._events.serial(name, *args)

    def graph(self) -> "DependencyGraph":
        """导出以本上下文为根的依赖图（挂载 / 提供 / 依赖三类关系）。"""
        return build_graph(self)

    def to_mermaid(self) -> str:
        """依赖图的 mermaid 文本（诊断 / 文档用）。"""
        return self.graph().to_mermaid()

    def _stop_if_current(self, slot: Tuple[str, str], instance: Any) -> None:
        """服务撤销：stop 实例、弹出值栈、触发同域依赖者回滚。"""
        if self._provided.get(slot) is not instance:
            return
        realm, key = slot
        self._provided.pop(slot, None)
        _call_lifecycle(instance, "stop")

        stack = self._values.get(slot)
        if stack:
            stack.pop()
            if not stack:
                self._values.pop(slot, None)

        self.root._restart_dependents(realm, key)

    def _check_dependencies(self, child: "Context", plugin: Plugin, name: str) -> None:
        """按 @inject 声明校验依赖并登记反向索引。"""
        spec = get_inject_spec(plugin)
        if spec is None:
            return
        missing = [key for key in spec.required if not child.has(key)]
        if missing:
            raise MissingDependency(name, missing)

        root = child.root
        for key in spec.all_keys:
            # 依赖者登记到它实际解析的 realm 槽位，服务变更只影响同域插件
            root._dependents.setdefault((child._realm_for(key), key), []).append(child)

    def _restart_dependents(self, realm: str, key: str) -> None:
        """服务变更 / 消失：同域依赖它的插件回滚重启（重启失败即停用）。"""
        root = self.root
        if root._restarting:  # 防重入：重启过程中插件自己又 set 了服务
            return
        forks = list(root._dependents.get((realm, key), []))
        if not forks:
            return

        root._restarting = True
        try:
            for fork in forks:
                if fork.disposed or fork._plugin is None or fork.parent is None:
                    continue
                plugin, config, parent = fork._plugin, fork._config, fork.parent
                logger.debug(f"[zero] 依赖 {key!r}（realm={realm or 'global'}）变更，重载插件 {fork.name!r}")
                fork.dispose()
                try:
                    parent.use(plugin, config)
                except MissingDependency as e:
                    logger.warning(f"[zero] 依赖 {key!r} 不可用，插件 {e.plugin!r} 停用")
        finally:
            root._restarting = False

    # ── 销毁 ──────────────────────────────────────────────────────────
    def dispose(self) -> None:
        """销毁上下文：先 emit dispose，再逆序回滚全部副作用（幂等）。"""
        if self._disposed:
            return
        self._disposed = True

        try:
            self._events.emit(EV_DISPOSE, self)
        except Exception as e:  # dispose 监听器异常不影响清理
            logger.error(f"[zero] dispose 事件异常（{self.path}）: {e}")

        for child in reversed(list(self._children)):
            child.dispose()
        self._children.clear()

        # 摘除依赖反向索引，再让自己提供的服务触发依赖者回滚
        root = self.root
        for forks in root._dependents.values():
            if self in forks:
                forks.remove(self)

        # 注意：_provided 必须在 scope.dispose() 之后清空——
        # 服务的 stop 由 disposer 判定「仍是当前服务」后才执行
        provided_slots = list(self._provided)

        self._scope.dispose()
        self._values.clear()
        # 注意：_events 是应用级共享总线，绝不能 clear（会清掉其他插件的订阅）；
        # 本上下文的订阅已由 scope 回滚逐个退订
        self._provided.clear()

        for slot in provided_slots:
            root._restart_dependents(slot[0], slot[1])

        if self.parent is not None and self in self.parent._children:
            self.parent._children.remove(self)

    # ── 内部 ──────────────────────────────────────────────────────────
    def _assert_alive(self) -> None:
        if self._disposed:
            raise DisposedError(f"上下文 {self.path!r} 已销毁")

    def _realm_for(self, key: str) -> str:
        return self._isolate.get(key, "")

    def __repr__(self) -> str:
        state = "disposed" if self._disposed else f"{self._scope.pending} effects"
        return f"<Context {self.path} {state}>"


def _plugin_name(plugin: Plugin) -> str:
    return getattr(plugin, "__name__", type(plugin).__name__)


_MISSING = object()


def _resolve_stack(stack: Optional[List[Any]]) -> Any:
    """取值栈顶并解引用；weak 条目已回收时返回 _MISSING。"""
    if not stack:
        return _MISSING
    raw = stack[-1]
    if isinstance(raw, weakref.ref):
        alive = raw()
        if alive is None:
            return _MISSING
        return alive
    return raw


def _call_lifecycle(obj: Any, method: str) -> None:
    """调用服务的 start / stop，异常只记日志（不让清理链断裂）。"""
    fn = getattr(obj, method, None)
    if not callable(fn):
        return
    try:
        fn()
    except Exception as e:
        logger.error(f"[zero] 服务 {obj!r} 的 {method}() 异常: {e}")


def _invoke_plugin(plugin: Plugin, ctx: Context, config: Any) -> Any:
    """按插件签名决定传几个参数。"""
    takes_config = True
    # 用 suppress 而非 except (TypeError, ValueError)：项目 ruff 目标 py314，
    # 会把无括号多异常改写成 PEP 758 语法，在 3.12 环境下是语法错误
    with suppress(TypeError, ValueError):  # 内置对象 / 不可解析签名
        params = inspect.signature(plugin).parameters
        positional = [p for p in params.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        takes_config = len(positional) >= 2
    return plugin(ctx, config) if takes_config else plugin(ctx)


def create_context(name: str = "root") -> Context:
    """创建根上下文。"""
    return Context(name=name)
