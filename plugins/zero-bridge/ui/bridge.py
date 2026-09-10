# -*- coding: utf-8 -*-
"""ZeroBridge —— 宿主无关的桥接核心。

把任意宿主的事件流与状态快照接入 zero 上下文树，宿主只需满足两个契约：

- 事件源：``events() -> list[str]``、``subscribe(name, cb) -> unsub``
- 状态源：``state_provider() -> dict[str, Any]``（值可为任意对象，弱引用喂入）

zero 插件从 ctx 里读 ``host.<key>`` 拿宿主状态，订阅 ``host.<event>`` 事件；
多窗口场景用 ``window(window_id)`` 取域上下文（isolate 语义）。
本模块不 import 任何宿主模块，DriFox 特定接胶水见同目录 ``__init__.py``。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from zero import Context, create_context

logger = logging.getLogger(__name__)

__all__ = ["ZeroBridge", "EventSource"]

EventSource = Any  # 鸭子类型：events() / subscribe(name, cb) -> unsub


class ZeroBridge:
    """宿主事件与状态 → zero 上下文树的桥。"""

    def __init__(self, name: str = "host") -> None:
        self.root: Context = create_context(name)
        self._windows: Dict[str, Context] = {}
        self._unsubs: List[Callable[[], None]] = []
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def start(
        self,
        event_source: Optional[EventSource] = None,
        state_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> "ZeroBridge":
        """启动桥接：喂入状态快照、订阅事件流（幂等）。"""
        if self._started:
            return self

        if state_provider is not None:
            for key, value in (state_provider() or {}).items():
                # 宿主对象（窗口 / widget）用弱引用，宿主回收后条目自动失效
                self.root.set(f"host.{key}", value, weak=True)

        if event_source is not None:
            for event in event_source.events():
                unsub = event_source.subscribe(event, self._forward(event))
                self._unsubs.append(unsub)
                self.root.add_disposer(unsub)

        self._started = True
        logger.debug(f"[zero-bridge] 已启动（{len(self._unsubs)} 个事件通道）")
        return self

    def window(self, window_id: str) -> Context:
        """取窗口域上下文（首次访问时创建，域内可挂窗口级插件）。"""
        win = self._windows.get(window_id)
        if win is None or win.disposed:
            win = self.root.isolate("session", window_id)
            self._windows[window_id] = win
        return win

    def close_window(self, window_id: str) -> None:
        """关闭窗口域（宿主窗口销毁时调用）。"""
        win = self._windows.pop(window_id, None)
        if win is not None and not win.disposed:
            win.dispose()

    def window_ids(self) -> List[str]:
        return [wid for wid, ctx in self._windows.items() if not ctx.disposed]

    def stop(self) -> None:
        """停桥：销毁整棵上下文树（幂等）。"""
        self._windows.clear()
        self.root.dispose()
        self._unsubs.clear()
        self._started = False

    def _forward(self, event: str) -> Callable[..., None]:
        def _cb(*args: Any) -> None:
            # DriFox 的 UIEventBus 回调签名是 (payload_dict)，zero 事件原样透传
            self.root.emit(f"host.{event}", *args)

        return _cb
