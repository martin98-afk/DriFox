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

import threading
import time

from loguru import logger
from PyQt5.QtCore import QObject, QTimer, pyqtSignal

_PUMP_IDLE_GAP_MS = 0  # idle 任务间 yield 事件循环的间隔（0 = 下一轮事件循环）
_PUMP_WAIT_MS = 5  # 无可执行任务（等 delay/约束）时的轮询间隔
class _Task:
    __slots__ = ("name", "fn", "priority", "delay_ms", "once", "barrier", "ran", "started", "async_run")

    def __init__(self, name, fn, priority, delay_ms, once, async_run=False):
        self.name = name
        self.fn = fn
        self.priority = priority
        self.delay_ms = delay_ms
        self.once = once
        self.barrier = False
        self.ran = False
        # started：异步任务已投递到后台线程但尚未回填 ran（防重复启动）
        self.started = False
        self.async_run = async_run


class DeferredTaskQueue(QObject):
    """per-window 延迟任务队列（host 销毁后由窗口侧调用 stop()）"""

    # 后台线程任务完成回填（跨线程 emit 自动排队到本对象线程 = 主线程）
    _async_task_done = pyqtSignal(str)

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
        self._paused = False
        self._async_task_done.connect(self._on_async_task_done)

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

    def register_async(self, name: str, fn, *, delay_ms: int = 0) -> "DeferredTaskQueue":
        """注册后台线程任务：fn 在守护线程执行，完成后回主线程标记 ran。

        用于「同步执行会冻结 UI、但必须在后续任务前完成」的预热（运行时组件
        warmup / 网络栈预热）。队列的保序约束与屏障语义照旧：before 未回填 ran
        时 after 不会执行，所以主线程只会等待，不会被任务体阻塞。

        fn 必须不碰 Qt widget（后台线程），且自身幂等。
        """
        if self._stopped:
            logger.warning(f"[DeferredTaskQueue] stop 后注册 {name}，忽略")
            return self
        if name in self._tasks and self._tasks[name].ran:
            logger.debug(f"[DeferredTaskQueue] 任务 {name} 已执行过，忽略重复注册")
            return self
        self._tasks[name] = _Task(name, fn, "idle", int(delay_ms), True, async_run=True)
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

    def pause(self, reason: str = "") -> None:
        """暂停调度（幂等）。

        用途：与另一段「会 import 插件模块」的主线程工作错峰。Python 的 import
        锁是进程级的，后台线程导入模块时主线程再导同一模块会整段阻塞（实测
        UI 插件装载被拖住 3.3s，且恢复时刻精确等于后台 warmup 结束）。
        已投递的异步任务无法撤回，但主线程不会再启新的。
        """
        if self._stopped or self._paused:
            return
        self._paused = True
        logger.debug(f"[DeferredTaskQueue] paused ({reason or 'manual'})")

    def resume(self, reason: str = "") -> None:
        """恢复调度（幂等；暂停期间未执行的任务照常按 delay/约束执行）"""
        if self._stopped or not self._paused:
            return
        self._paused = False
        logger.debug(f"[DeferredTaskQueue] resumed ({reason or 'manual'})")
        self._schedule_pump(0)

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
        if self._stopped or self._finished or self._pump_scheduled or self._paused:
            return
        self._pump_scheduled = True
        QTimer.singleShot(max(0, delay_ms), self._pump)

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self._start_monotonic) * 1000

    def _ready(self, task: _Task) -> bool:
        """任务是否满足执行条件（delay 到期 + 保序约束满足 + 屏障条件满足）"""
        if task.ran:
            return False
        if task.started:
            return False  # 异步任务在跑，等回填
        if self._elapsed_ms() < task.delay_ms:
            return False
        for before in self._constraints.get(task.name, ()):  # 保序
            bt = self._tasks.get(before)
            if bt is None:
                continue  # before 未注册/已取消：约束失效（对齐原 singleShot 独立语义）
            if not bt.ran:
                return False
        if task.name in self._barriers:
            # 屏障：等「非屏障」的 critical 全部完成。
            # 注意：屏障任务自身可能被注册为 critical（如 initialization_complete），
            # 此时不能把它自己算作阻塞源——否则 _ready 恒为 False，该任务永不可
            # 执行（自锁），并连带让 _all_critical_done 恒为 False、泵永不收口。
            if any(
                t.priority == "critical" and not t.ran and t.name not in self._barriers for t in self._tasks.values()
            ):
                return False
        return True

    def _next_runnable(self, want_priority: str):
        for task in self._tasks.values():
            if task.priority == want_priority and self._ready(task):
                return task
        return None

    def _all_critical_done(self) -> bool:
        """非屏障的 critical 是否全部完成。

        屏障任务自身不计入（否则屏障注册为 critical 时永远无法放行——自锁）。
        语义：屏障等待的是「工作项」，而不是其他屏障门。
        """
        return all(t.ran for t in self._tasks.values() if t.priority == "critical" and t.name not in self._barriers)

    def _pump(self):
        self._pump_scheduled = False
        if self._stopped or self._paused:
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
        if task.async_run:
            # 后台执行：主线程只投递，不等待；完成由 _async_task_done 回填
            task.started = True
            t = threading.Thread(target=self._async_runner, args=(task,), daemon=True, name=f"dtq-{task.name}")
            t.start()
            return
        task.ran = True
        try:
            task.fn()
        except Exception:  # noqa: BLE001
            logger.exception(f"[DeferredTaskQueue] 任务 {task.name} 执行异常（继续后续任务）")
        if task.once and task.name in self._barriers:
            self._barriers.discard(task.name)

    def _async_runner(self, task: _Task):
        """后台线程体：执行任务后经信号回主线程回填 ran"""
        t0 = time.perf_counter()
        try:
            task.fn()
        except Exception:  # noqa: BLE001
            logger.exception(f"[DeferredTaskQueue] 后台任务 {task.name} 执行异常（继续后续任务）")
        finally:
            logger.debug(f"[DeferredTaskQueue] 后台任务 {task.name} 耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
            self._async_task_done.emit(task.name)

    def _on_async_task_done(self, name: str):
        """主线程回填：标记完成并重新泵一轮（保序约束据此放行后续任务）"""
        if self._stopped:
            return
        task = self._tasks.get(name)
        if task is None:
            return
        task.ran = True
        if task.once and name in self._barriers:
            self._barriers.discard(name)
        if not self._paused:
            self._schedule_pump(0)
