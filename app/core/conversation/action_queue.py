# -*- coding: utf-8 -*-
"""窗口级动作队列：插件调用会话控制能力只入队，本轮结束后由主线程按序执行。

为什么需要排队：`update_handoff` 在 worker 线程里执行，调用「中断会话」停的
正是跑着这个工具的 worker，同步执行必然死锁（等自己返回）。入队 + 本轮结束
drain 是唯一安全时序。

窗口隔离：队列按 window_id 分开。拿不到 window_id 一律拒绝入队——写操作
回退到「活跃窗口」会在多窗口同时交接时串窗（A 的交接切走 B 的会话）。
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# 同一队列中最多保留一次的动作（重复登记无意义且会重复停流/重复建会话）
_DEDUP = frozenset({"interrupt", "new"})


class WindowActionQueue:
    """按 window_id 隔离的动作队列"""

    def __init__(self):
        self._queues: Dict[str, Deque[dict]] = defaultdict(deque)
        self._draining: Dict[str, bool] = {}

    def enqueue(self, window_id: str, action: str, payload: Optional[dict] = None) -> bool:
        """入队，返回是否被接受

        线程安全：deque.append 在 GIL 下原子；不触碰任何 UI 对象。
        """
        wid = str(window_id or "").strip()
        if not wid:
            logger.warning("[ActionQueue] 拒绝入队：window_id 为空")
            return False
        if self._draining.get(wid):
            logger.info("[ActionQueue] 拒绝入队：窗口 %s 正在执行动作", wid)
            return False
        queue = self._queues[wid]
        if action in _DEDUP and any(item["action"] == action for item in queue):
            return False
        queue.append({"action": action, "payload": dict(payload or {})})
        return True

    def peek(self, window_id: str) -> List[dict]:
        """查看待执行动作（只读副本）"""
        return list(self._queues.get(str(window_id or ""), ()))

    def clear(self, window_id: str) -> None:
        self._queues.pop(str(window_id or ""), None)

    def drain(self, window_id: str, handlers: Dict[str, Callable[[dict], None]]) -> int:
        """按登记顺序执行并清空，返回成功执行的动作数

        单个动作失败即中止后续并清空队列：交接链路是强顺序（停流→新建→发消息），
        中途失败继续执行会得到「没切会话却发了消息」这类错位状态。
        """
        wid = str(window_id or "").strip()
        queue = self._queues.get(wid)
        if not queue:
            return 0
        self._draining[wid] = True
        done = 0
        try:
            while queue:
                item = queue.popleft()
                action, payload = item["action"], item["payload"]
                handler = handlers.get(action)
                if handler is None:
                    logger.warning("[ActionQueue] 无处理器，中止：%s", action)
                    queue.clear()
                    break
                try:
                    handler(payload)
                    done += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("[ActionQueue] 动作 %s 执行失败，中止后续：%s", action, e)
                    queue.clear()
                    break
        finally:
            self._draining[wid] = False
        return done


_QUEUE: Optional[WindowActionQueue] = None


def get_action_queue() -> WindowActionQueue:
    """进程内单例（队列自身按 window_id 隔离）"""
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = WindowActionQueue()
    return _QUEUE
