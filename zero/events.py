# -*- coding: utf-8 -*-
"""类型化事件分发（四种语义）。

===========  =======================================  ==================
语义          行为                                      适用
===========  =======================================  ==================
emit         通知，不等返回值                            观察与广播
waterfall    链式改写，next(v) 交给下游，不调即短路        策略拦截与改写
parallel     并发 await 全部监听器                       可并行的副作用
serial       按序 await 监听器                          有顺序依赖的处理
===========  =======================================  ==================

监听器异常一律隔离：记日志后继续，不让一个坏监听器打断整条链。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

Listener = Callable[..., Any]
Disposer = Callable[[], None]
Next = Callable[..., None]


class EventEmitter:
    """同步 / 异步双模事件分发器。"""

    def __init__(self) -> None:
        self._listeners: Dict[str, List[Listener]] = {}

    def on(self, name: str, listener: Listener) -> Disposer:
        """订阅事件，返回退订函数（可直接交给 effect 登记）。"""
        self._listeners.setdefault(name, []).append(listener)

        def _off() -> None:
            bucket = self._listeners.get(name)
            if bucket and listener in bucket:
                bucket.remove(listener)

        return _off

    def listener_count(self, name: str) -> int:
        return len(self._listeners.get(name, []))

    def clear(self) -> None:
        self._listeners.clear()

    # ── 同步 ──────────────────────────────────────────────────────────
    def emit(self, name: str, *args: Any) -> None:
        """广播通知：不等待返回值，异常隔离。"""
        for listener in list(self._listeners.get(name, [])):
            try:
                listener(*args)
            except Exception as e:
                logger.error(f"[zero] emit '{name}' 监听器异常: {e}")

    def waterfall(self, name: str, value: Any, *args: Any) -> Any:
        """链式改写：监听器签名 ``(value, *args, next)``。

        调用 ``next(new_value)`` 把新值交给下游；不调用则短路，返回当前值。
        """
        listeners = list(self._listeners.get(name, []))
        state = {"value": value, "index": 0}

        def _next(new_value: Any = None) -> None:
            if new_value is not None:
                state["value"] = new_value
            if state["index"] >= len(listeners):
                return
            listener = listeners[state["index"]]
            state["index"] += 1
            try:
                listener(state["value"], *args, _next)
            except Exception as e:
                logger.error(f"[zero] waterfall '{name}' 监听器异常: {e}")
                _next()  # 异常不打断链路，原值继续下传

        _next()
        return state["value"]

    # ── 异步 ──────────────────────────────────────────────────────────
    async def parallel(self, name: str, *args: Any) -> List[Any]:
        """并发执行全部监听器（同步监听器结果原样收集）。"""
        listeners = list(self._listeners.get(name, []))
        if not listeners:
            return []
        gathered = await asyncio.gather(*[_invoke(l, *args) for l in listeners], return_exceptions=True)
        results = []
        for item in gathered:
            if isinstance(item, BaseException):
                logger.error(f"[zero] parallel '{name}' 监听器异常: {item}")
                continue
            results.append(item)
        return results

    async def serial(self, name: str, *args: Any) -> List[Any]:
        """按序执行监听器，前一个完成后再跑下一个。"""
        results: List[Any] = []
        for listener in list(self._listeners.get(name, [])):
            try:
                results.append(await _invoke(listener, *args))
            except Exception as e:
                logger.error(f"[zero] serial '{name}' 监听器异常: {e}")
        return results


async def _invoke(listener: Listener, *args: Any) -> Any:
    """调用监听器，协程则 await，同步则原样返回。"""
    result = listener(*args)
    if inspect.isawaitable(result):
        return await result
    return result
