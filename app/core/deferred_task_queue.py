# -*- coding: utf-8 -*-
"""DeferredTaskQueue — per-window 延迟任务队列（批4：事件循环削峰）。

把 OpenAIChatToolWindow / ChatBackend 分散在各处的 QTimer.singleShot 延迟
初始化统一收敛到单一队列调度：

- priority="critical"：关键路径（会话创建/引擎创建/workdir 同步等），依赖
  满足后**连续执行**（同一事件循环批次内背靠背完成，不给其他事件插队）。
- priority="idle"：非关键预热（模型配置/网络预热/命令注册等），每任务执行后
  **yield 一次事件循环**，避免连续忙段冻结 UI。
- add_order_constraint(before, after)：保序对。违序注册（after 先于 before
  注册）同样生效——约束独立于注册顺序。
- set_barrier(name)：屏障任务（如 initialization_complete 解除初始化保护），
  等 critical 全部完成 + idle 已获得一轮执行机会后才执行。
- stop()：窗口销毁时调用；未执行的 critical 丢弃并 warning。

调度为 QTimer.singleShot 自泵：start() 后单发泵循环推进，全部任务完成后
自动停止。纯 QtCore，可独立单测。
"""

import time

from loguru import logger
from PyQt5.QtCore import QObject, QTimer

_PUMP_IDLE_GAP_MS = 0  # idle 任务间 yield 事件循环的间隔（0 = 下一轮事件循环）
_PUMP_WAIT_MS = 5  # 无可执行任务（等 delay/约束）时的轮询间隔
class _Task:
    __slots__ = ("name", "fn", "priority", "delay_ms", "once", "barrier", "ran")

    def __init__(self, name, fn, priority, delay_ms, once):
        self.name = name
        self.fn = fn
        self.priority = priority
        self.delay_ms = delay_ms
        self.once = once
        self.barrier = False
        self.ran = False


class DeferredTaskQueue(QObject):
    """per-window 延迟任务队列（host 销毁后由窗口侧调用 stop()）"""

    def __init__(self):
        super().__init__()
        self._tasks: dict = {}  # name -> _Task（保注册序，dict 有序）
        self._constraints: dict = {}  # after_name -> set(before_names)
        self._barriers: set = set()
        self._started = False
        self._stopped = False
        self._finished = False
        self._start_monotonic = 0.0
        self._pump_scheduled = False

    # ── 注册接口 ──

    def register(self, name: str, fn, *, priority: str = "idle", delay_ms: int = 0, once: bool = True):
        """注册任务。同名重复注册覆盖（once 幂等）。

        Args:
            name: 任务名（约束/屏障引用键）
            fn: 任务体（无参调用）
            priority: "critical"（依赖满足后连续执行）或 "idle"（每任务 yield 一轮事件循环）
            delay_ms: 相对 start() 的最早执行延迟（对齐原 QTimer.singleShot 语义）
            once: 任务只执行一次
        """
        if self._stopped:
            logger.warning(f"[DeferredTaskQueue] stop 后注册 {name}，忽略")
            return self
        if name in self._tasks and self._tasks[name].ran:
            logger.debug(f"[DeferredTaskQueue] 任务 {name} 已执行过，忽略重复注册")
            return self
        self._tasks[name] = _Task(name, fn, priority, int(delay_ms), once)
        return self

    def add_order_constraint(self, before: str, after: str):
        """保序对：after 执行前 before 必须已完成。违序注册同样生效。"""
        self._constraints.setdefault(after, set()).add(before)
        return self

    def set_barrier(self, name: str):
        """标记屏障任务：等 critical 全完成 + idle 已有一轮机会后执行"""
        self._barriers.add(name)
        return self

    def cancel(self, name: str):
        """取消未执行任务"""
        self._tasks.pop(name, None)
        self._barriers.discard(name)
        return self

    # ── 生命周期 ──

    def start(self):
        """启动调度（幂等）。注册可在 start 后动态追加（运行中注册照常调度）。"""
        if self._started or self._stopped:
            return
        self._started = True
        self._start_monotonic = time.monotonic()
        self._schedule_pump(0)

    def stop(self, reason: str = ""):
        """停止调度。未执行的 critical 丢弃并 warning。"""
        if self._stopped:
            return
        self._stopped = True
        pending_critical = [
            t.name for t in self._tasks.values() if not t.ran and t.priority == "critical"
        ]
        if pending_critical:
            logger.warning(
                f"[DeferredTaskQueue] stop({reason or 'manual'})：未执行的 critical 任务丢弃 "
                f"{pending_critical}"
            )
        logger.debug(f"[DeferredTaskQueue] stopped ({reason or 'manual'})")

    # ── 调度内核 ──

    def _schedule_pump(self, delay_ms: int):
        if self._stopped or self._finished or self._pump_scheduled:
            return
        self._pump_scheduled = True
        QTimer.singleShot(max(0, delay_ms), self._pump)

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self._start_monotonic) * 1000

    def _ready(self, task: _Task) -> bool:
        """任务是否满足执行条件（delay 到期 + 保序约束满足 + 屏障条件满足）"""
        if task.ran:
            return False
        if self._elapsed_ms() < task.delay_ms:
            return False
        for before in self._constraints.get(task.name, ()):  # 保序
            bt = self._tasks.get(before)
            if bt is None:
                continue  # before 未注册/已取消：约束失效（对齐原 singleShot 独立语义）
            if not bt.ran:
                return False
        if task.name in self._barriers:
            # 屏障：critical 全部完成
            if any(t.priority == "critical" and not t.ran for t in self._tasks.values()):
                return False
        return True

    def _next_runnable(self, want_priority: str):
        for task in self._tasks.values():
            if task.priority == want_priority and self._ready(task):
                return task
        return None

    def _all_critical_done(self) -> bool:
        return all(t.ran for t in self._tasks.values() if t.priority == "critical")

    def _pump(self):
        self._pump_scheduled = False
        if self._stopped:
            return

        if not self._tasks:
            self._finished = True
            return

        # critical：连续执行（同一批次内背靠背完成）
        progressed = True
        while progressed:
            progressed = False
            task = self._next_runnable("critical")
            if task is not None:
                self._run_task(task)
                progressed = True
                if self._stopped:
                    return

        # 屏障判定：critical 全完成 + idle 已有一轮机会 → 屏障任务立即放行
        if self._all_critical_done():
            barrier_task = next(
                (t for t in self._tasks.values() if t.name in self._barriers and self._ready(t)),
                None,
            )
            if barrier_task is not None:
                self._run_task(barrier_task)
                if self._stopped:
                    return

        # idle：每任务 yield 一次事件循环
        idle_task = self._next_runnable("idle")
        if idle_task is not None:
            self._run_task(idle_task)
            if self._stopped:
                return
            self._schedule_pump(_PUMP_IDLE_GAP_MS)
            return

        # 无可执行任务：还有未完成任务 → 等待重试；全部完成 → 结束
        if any(not t.ran for t in self._tasks.values()):
            self._schedule_pump(_PUMP_WAIT_MS)
        else:
            self._finished = True

    def _run_task(self, task: _Task):
        task.ran = True
        try:
            task.fn()
        except Exception:  # noqa: BLE001
            logger.exception(f"[DeferredTaskQueue] 任务 {task.name} 执行异常（继续后续任务）")
        if task.once and task.name in self._barriers:
            self._barriers.discard(task.name)
