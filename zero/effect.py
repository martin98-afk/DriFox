# -*- coding: utf-8 -*-
"""可逆副作用原语（zero 的唯一变更入口）。

cordis 的结构事实：所有对上下文的变更最终都归结为 ``ctx.effect`` 这一个
原语——提供服务、注册监听、挂载插件都是它的特例。zero 沿用该结构：

- ``EffectScope.effect(callback)`` 执行变更，callback 返回 disposer 则登记
- ``EffectScope.add(disposer)`` 直接登记撤销函数
- ``EffectScope.dispose()`` 逆序执行全部 disposer，子作用域先销毁

异常隔离：某个 disposer 抛异常不会阻断其余清理（热重载场景下这点很关键，
一次失败不应让整个插件树的资源泄漏）。

隐式捕获：通过 ``contextvars`` 记录当前作用域，嵌套调用中未显式传 ctx 的
代码也能把副作用登记到正确的作用域。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Callable, List, Optional

from zero.errors import DisposedError

logger = logging.getLogger(__name__)

Disposer = Callable[[], None]

_current_scope: ContextVar[Optional["EffectScope"]] = ContextVar("zero_current_scope", default=None)


def current_scope() -> Optional["EffectScope"]:
    """取当前活跃作用域（供隐式捕获）。"""
    return _current_scope.get()


class EffectScope:
    """副作用作用域：登记与逆序回滚。"""

    def __init__(self, name: str = "", parent: Optional["EffectScope"] = None) -> None:
        self.name = name
        self.parent = parent
        self._disposers: List[Disposer] = []
        self._children: List["EffectScope"] = []
        self._disposed = False

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def pending(self) -> int:
        """待回滚的副作用数量（诊断用）。"""
        return len(self._disposers)

    def add(self, disposer: Disposer) -> None:
        """登记一个撤销函数（销毁时逆序调用）。"""
        if self._disposed:
            raise DisposedError(f"作用域 {self.name!r} 已销毁，无法登记副作用")
        if not callable(disposer):
            raise TypeError(f"disposer 必须可调用，收到 {type(disposer).__name__}")
        self._disposers.append(disposer)

    def effect(self, callback: Callable[[], Any]) -> Any:
        """执行一个可逆副作用。

        callback 在当前作用域绑定的上下文中执行，返回值若为可调用对象则
        登记为 disposer。返回 callback 的返回值。
        """
        if self._disposed:
            raise DisposedError(f"作用域 {self.name!r} 已销毁，无法执行 effect")

        token = _current_scope.set(self)
        try:
            disposer = callback()
        finally:
            _current_scope.reset(token)

        if callable(disposer):
            self.add(disposer)
        return disposer

    def child(self, name: str = "") -> "EffectScope":
        """创建子作用域（父销毁时子先销毁）。"""
        if self._disposed:
            raise DisposedError(f"作用域 {self.name!r} 已销毁，无法创建子作用域")
        scope = EffectScope(name=name, parent=self)
        self._children.append(scope)
        return scope

    def dispose(self) -> None:
        """逆序回滚全部副作用（幂等）。"""
        if self._disposed:
            return
        self._disposed = True

        # 生命周期顺序：子作用域先销毁，再逆序回滚自身副作用
        children = list(reversed(self._children))
        self._children.clear()
        for child in children:
            child.dispose()

        while self._disposers:
            disposer = self._disposers.pop()
            try:
                disposer()
            except Exception as e:
                logger.error(f"[zero] 副作用回滚失败（scope={self.name}）: {e}")

        self._children.clear()

    def __enter__(self) -> "EffectScope":
        self._enter_token = _current_scope.set(self)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        _current_scope.reset(self._enter_token)

    def __repr__(self) -> str:
        state = "disposed" if self._disposed else f"{self.pending} effects"
        return f"<EffectScope {self.name!r} {state}>"
