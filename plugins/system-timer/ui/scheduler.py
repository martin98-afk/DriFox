# -*- coding: utf-8 -*-
"""system-timer 调度器：主线程 QTimer tick + threading.Lock 保护的内存任务表。

线程纪律：
- QTimer 只能在主线程创建/销毁（start/stop 只由 register_ui/unload_ui 调用）；
- 任务表操作（add_task/list_tasks/cancel）线程安全，工具 impl（worker 线程）直调；
- tick 的到期触发在主线程执行（QTimer timeout 信号），经目标窗口能力池
  session.send 纯入队（消费由主程序 drain 完成，本模块不触碰 MainWidget）；
- 到期判定统一用 time.monotonic()（防系统时间跳变），tick(now) 允许注入时间供测试。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from loguru import logger

from PyQt5.QtCore import QTimer

_TICK_MS = 1000


class _WindowHost:
    """能力池 host 替身：session.send 入队只需要 window_id。

    tool_core._session_control(host) 的解析顺序是 backend._window_id →
    host._window_id；本替身无 backend 属性，window_id 精确落到目标窗口
    （多窗口不串）。
    """

    def __init__(self, window_id: str):
        self._window_id = window_id


class TimerScheduler:
    """内存级定时任务调度器（模块级单例，见 get_scheduler/create_scheduler）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._timer: Optional[QTimer] = None

    # ── 生命周期（仅主线程） ──

    def start(self) -> None:
        """创建并启动 1s tick 定时器（幂等）。"""
        if self._timer is not None:
            return
        self._timer = QTimer()
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()
        logger.info("[system-timer] 调度器已启动（tick=1000ms）")

    def stop(self) -> None:
        """停定时器并清空任务表（幂等）。"""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        with self._lock:
            self._tasks.clear()

    # ── 任务表（线程安全，worker 线程可直调） ──

    def add_task(
        self,
        session_id: str,
        prompt: str,
        delay_seconds: Optional[int] = None,
        interval_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """登记任务并返回快照。一次性（delay_seconds）与循环（interval_minutes）
        二选一，合法性由调用方（工具层）校验。"""
        now = time.monotonic()
        if interval_minutes is not None and interval_minutes > 0:
            task: Dict[str, Any] = {
                "id": uuid.uuid4().hex[:8],
                "session_id": session_id,
                "prompt": prompt,
                "type": "loop",
                "interval_seconds": float(interval_minutes) * 60.0,
                "fire_at": now + float(interval_minutes) * 60.0,
            }
        else:
            task = {
                "id": uuid.uuid4().hex[:8],
                "session_id": session_id,
                "prompt": prompt,
                "type": "once",
                "fire_at": now + float(delay_seconds or 0),
            }
        with self._lock:
            self._tasks[task["id"]] = task
        return self._snapshot(task, now)

    def list_tasks(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [self._snapshot(task, now) for task in self._tasks.values()]

    def cancel(self, task_id: str = "", all_tasks: bool = False, session_id: str = "") -> int:
        """取消任务，返回取消数。

        会话归属约束：all_tasks=True 只清 session_id 匹配的任务，session_id
        为空时拒绝（返回 0）—— 跨会话取消不生效，防误清其他会话的定时任务。
        单任务取消同样校验归属（task_id 属于其他会话时视同不存在）。
        """
        with self._lock:
            if all_tasks:
                if not session_id:
                    return 0
                ids = [tid for tid, task in self._tasks.items() if task["session_id"] == session_id]
                for tid in ids:
                    self._tasks.pop(tid, None)
                return len(ids)
            task = self._tasks.get(task_id)
            if task is None:
                return 0
            if session_id and task["session_id"] != session_id:
                return 0  # 跨会话取消不生效
            self._tasks.pop(task_id, None)
            return 1

    # ── 到期触发（主线程） ──

    def _on_tick(self) -> None:
        self.tick()

    def tick(self, now: Optional[float] = None) -> None:
        """推进一轮到期判定（now 可注入供测试；None 取 time.monotonic()）。

        锁内只做到期筛选与「触发中」标记（防同 tick 重复触发），实际发送
        在锁外串行执行：发送可能走主程序调用，持锁会阻塞 worker 的表操作。
        """
        if now is None:
            now = time.monotonic()
        due: List[Dict[str, Any]] = []
        with self._lock:
            for task in self._tasks.values():
                if task["fire_at"] <= now and not task.get("firing"):
                    task["firing"] = True
                    due.append(dict(task))
        due.sort(key=lambda item: item["fire_at"])  # 同 tick 多任务按到期先后触发
        for snapshot in due:
            try:
                self._fire(snapshot)
            except Exception as e:  # noqa: BLE001 - 单任务异常不拖垮整个 tick
                # QTimer 槽内未捕获异常默认 abort 进程（PyQt5 ≥5.5），必须就地吞掉。
                # firing 复位：_fire 在锁外抛异常时任务滞留「触发中」，后续 tick
                # 会因 firing=True 静默跳过（单任务永久停摆），必须就地复位。
                with self._lock:
                    task = self._tasks.get(snapshot["id"])  # due 里的快照必有 id
                    if task is not None:
                        task["firing"] = False
                logger.warning(f"[system-timer] 任务 {snapshot.get('id')} 触发异常（已跳过）: {e}")

    def _fire(self, snapshot: Dict[str, Any]) -> None:
        """触发一个到期任务：定位窗口 → session.send 入队 → 出表/顺延。

        未送达（窗口匹配不到 / 入队被拒）：一次性任务按错过（missed）终结
        出表；循环任务跳过本轮、从现在起算下一轮。
        """
        task_id = snapshot["id"]
        delivered = False
        window_id = self._find_window(snapshot["session_id"])
        if window_id is None:
            logger.warning(
                f"[system-timer] 任务 {task_id} 未找到目标窗口"
                f"（session={snapshot['session_id'][:8]}，会话可能已切走/关闭）"
            )
        else:
            delivered = self._send(f"⏰ 定时任务触发：{snapshot['prompt']}", window_id)
            if not delivered:
                logger.warning(f"[system-timer] 任务 {task_id} 触发消息入队被拒（窗口忙/正在关闭）")

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return  # 触发期间被 cancel：无事可做
            task["firing"] = False
            if not delivered:
                if task["type"] == "once":
                    # missed 终结：直接出表（无持久化，标记无处可落，靠日志留痕）
                    logger.warning(f"[system-timer] 一次性任务 {task_id} 按错过（missed）终结")
                    self._tasks.pop(task_id, None)
                else:
                    task["fire_at"] = time.monotonic() + task["interval_seconds"]
                return
            if task["type"] == "once":
                self._tasks.pop(task_id, None)
            else:
                task["fire_at"] += task["interval_seconds"]

    def _find_window(self, session_id: str) -> Optional[str]:
        """按 session_id 反查窗口 id（遍历 UIPluginRegistry 的窗口主部件表）。"""
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            registry = UIPluginRegistry.get_instance()
            if registry is None:
                return None
            for window_id, main_widget in list(getattr(registry, "_window_main_widgets", {}).items()):
                try:
                    session = main_widget.session_manager.get_current_session()
                except Exception:  # noqa: BLE001 - 单窗口异常不影响其余窗口匹配
                    continue
                if session is not None and getattr(session, "session_id", "") == session_id:
                    return str(window_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-timer] 窗口定位失败: {e}")
        return None

    def _send(self, text: str, window_id: str) -> bool:
        """经目标窗口 tool surface 能力池 session.send 入队（纯入队，立即返回）。"""
        try:
            from app.plugins.capabilities.bootstrap import ensure_registered
            from app.plugins.capabilities.registry import CapabilityRegistry

            ensure_registered("tool")
            pool = CapabilityRegistry.get_instance().build_pool(
                surface="tool", host=_WindowHost(window_id), plugin="system-timer"
            )
            if "session.send" not in pool:
                logger.warning("[system-timer] session.send 能力未放行（manifest 未声明？）")
                return False
            # session.send 的 provider 返回裸 send 可调用（tool_core._session_send
            # 直接返回 _session_control(host)["send"]），不是 dict —— 先例见
            # system-handoff/hooks/auto_compact_trigger.py:72
            send = pool["session.send"]
            if not callable(send):
                logger.warning("[system-timer] session.send 能力形状异常（非可调用）")
                return False
            return bool(send(text))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-timer] 触发消息发送失败: {e}")
            return False

    # ── 内部 ──

    @staticmethod
    def _snapshot(task: Dict[str, Any], now: float) -> Dict[str, Any]:
        return {
            "id": task["id"],
            "session_id": task["session_id"],
            "prompt": task["prompt"],
            "type": task["type"],
            "next_fire_seconds": round(max(0.0, task["fire_at"] - now), 1),
        }


# ── 模块级单例（ui/__init__.py 管生命周期；tools 侧经 sys.modules 解析） ──

_scheduler: Optional[TimerScheduler] = None


def get_scheduler() -> Optional[TimerScheduler]:
    """取当前调度器单例（未启动返回 None）。"""
    return _scheduler


def create_scheduler() -> TimerScheduler:
    """创建调度器单例（幂等；start 仍需主线程显式调用）。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TimerScheduler()
    return _scheduler


def destroy_scheduler() -> None:
    """停表并置空单例（unload_ui 调用；防热重载双调度器并存）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
    _scheduler = None
