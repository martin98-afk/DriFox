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

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

_TICK_MS = 1000

# 可订阅的对话流事件（与 ChatBackend._TRACE_SIGNAL_ARITY 的真实转发集对齐）。
# 注意 message_received 是死信号（全仓无 emit 点），不能作为事件源。
EVENT_NAMES = (
    "stream_finished",
    "stream_started",
    "messages_updated",
    "error_occurred",
    "session_changed",
)


class _WindowEventBridge(QObject):
    """单窗口事件桥：把 backend 信号搬回主线程后再转给调度器。

    backend 的 stream_finished 等在 worker 线程 emit；若直接 connect 到普通
    callable，Qt 会在发射线程就地执行，等于在 worker 线程摸任务表和窗口。
    桥是主线程 QObject，AutoConnection 自动排队回主线程，槽内再 emit 内部
    信号给调度器（同线程直连）。
    """

    event = pyqtSignal(str, str, str, dict)  # event_name, window_id, session_id, payload

    def __init__(self, window_id: str, widget: Any):
        super().__init__()
        self._window_id = str(window_id)
        self._widget = widget

    def _current_session_id(self) -> str:
        try:
            session = self._widget.session_manager.get_current_session()
            return str(getattr(session, "session_id", "") or "")
        except Exception:  # noqa: BLE001 - 窗口销毁中，视为空会话
            return ""

    def on_stream_finished(self, response: Any = "") -> None:
        self.event.emit(
            "stream_finished", self._window_id, self._current_session_id(), {"response": str(response or "")}
        )

    def on_stream_started(self) -> None:
        self.event.emit("stream_started", self._window_id, self._current_session_id(), {})

    def on_messages_updated(self) -> None:
        self.event.emit("messages_updated", self._window_id, self._current_session_id(), {})

    def on_error(self, error: Any = "") -> None:
        self.event.emit(
            "error_occurred", self._window_id, self._current_session_id(), {"error": str(error or "")}
        )

    def on_session_changed(self, session_id: Any = "") -> None:
        sid = str(session_id or "")
        self.event.emit("session_changed", self._window_id, sid, {"session_id": sid})


# backend 信号名 → 桥槽方法名
_SIGNAL_HANDLERS = {
    "stream_finished": "on_stream_finished",
    "stream_started": "on_stream_started",
    "messages_updated": "on_messages_updated",
    "error_occurred": "on_error",
    "session_changed": "on_session_changed",
}


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
        # window_id -> 事件桥；_connections 记录 (signal, handler) 供 stop 精确断开
        self._bridges: Dict[str, _WindowEventBridge] = {}
        self._connections: List[tuple] = []

    # ── 生命周期（仅主线程） ──

    def start(self) -> None:
        """创建并启动 1s tick 定时器（幂等）。"""
        if self._timer is not None:
            return
        self._timer = QTimer()
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()
        # 立即补一轮订阅（热加载时窗口已在，等 1s tick 会漏首条事件）
        self._ensure_subscriptions()
        logger.info("[system-timer] 调度器已启动（tick=1000ms）")

    def stop(self) -> None:
        """停定时器、断开事件桥并清空任务表（幂等）。"""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        for sig, handler in self._connections:
            try:
                sig.disconnect(handler)
            except Exception:  # noqa: BLE001 - 窗口已销毁时 Qt 可能已自动断连
                pass
        self._connections.clear()
        for bridge in self._bridges.values():
            try:
                bridge.event.disconnect()
            except Exception:  # noqa: BLE001
                pass
            bridge.deleteLater()
        self._bridges.clear()
        with self._lock:
            self._tasks.clear()

    # ── 任务表（线程安全，worker 线程可直调） ──

    def add_task(
        self,
        session_id: str,
        prompt: str,
        delay_seconds: Optional[int] = None,
        interval_minutes: Optional[int] = None,
        mode: str = "queue",
    ) -> Dict[str, Any]:
        """登记任务并返回快照。一次性（delay_seconds）与循环（interval_minutes）
        二选一，合法性由调用方（工具层）校验。mode 为触发方式（queue/skip/hook），
        到期时透传给 session.send。"""
        now = time.monotonic()
        if interval_minutes is not None and interval_minutes > 0:
            task: Dict[str, Any] = {
                "id": uuid.uuid4().hex[:8],
                "session_id": session_id,
                "prompt": prompt,
                "type": "loop",
                "mode": mode,
                "interval_seconds": float(interval_minutes) * 60.0,
                "fire_at": now + float(interval_minutes) * 60.0,
            }
        else:
            task = {
                "id": uuid.uuid4().hex[:8],
                "session_id": session_id,
                "prompt": prompt,
                "type": "once",
                "mode": mode,
                "fire_at": now + float(delay_seconds or 0),
            }
        task["trigger"] = "time"
        with self._lock:
            self._tasks[task["id"]] = task
        return self._snapshot(task, now)

    def add_event_task(
        self,
        session_id: str,
        prompt: str,
        event: str,
        watch_kind: str,
        watch_value: str,
        watch_label: str = "",
        mode: str = "queue",
        repeat: bool = False,
        cooldown_seconds: float = 5.0,
        max_fires: int = 100,
    ) -> Dict[str, Any]:
        """登记事件任务并返回快照。watch 已在工具层解析为 kind/value。

        repeat=False 时首命中即出表；repeat=True 受 cooldown 与 max_fires
        约束（防 messages_updated 这类连发事件打出任务风暴）。
        """
        task: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:8],
            "session_id": session_id,
            "prompt": prompt,
            "type": "event",
            "trigger": "event",
            "event": event,
            "watch_kind": watch_kind,
            "watch_value": watch_value,
            "watch_label": watch_label,
            "mode": mode,
            "repeat": bool(repeat),
            "cooldown_seconds": max(0.0, float(cooldown_seconds or 0)),
            "max_fires": max(1, int(max_fires or 1)),
            "fires": 0,
            "last_fire_at": 0.0,
        }
        with self._lock:
            self._tasks[task["id"]] = task
        return self._snapshot(task, time.monotonic())

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
        if self._timer is not None:
            # 每秒顺带补订阅新窗口（窗口可能在调度器启动后才创建）。
            # QTimer 槽内未捕获异常默认 abort 进程（PyQt5 ≥5.5），这里必须吞掉：
            # 窗口销毁竞态随时可能让 _bridges/widgets 的访问抛 RuntimeError。
            try:
                self._ensure_subscriptions()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[system-timer] 事件订阅扫描异常（本轮跳过）: {e}")
        due: List[Dict[str, Any]] = []
        with self._lock:
            for task in self._tasks.values():
                if task.get("trigger", "time") != "time":
                    continue  # 事件任务由 _on_event 驱动，不参与到期判定
                if task.get("fire_at", float("inf")) <= now and not task.get("firing"):
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
            delivered = self._send(
                f"⏰ 定时任务触发：{snapshot['prompt']}", window_id, mode=snapshot.get("mode", "queue")
            )
            if not delivered:
                logger.warning(f"[system-timer] 任务 {task_id} 触发消息发送被拒（窗口忙/正在关闭）")

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

    # ── 事件触发 ──

    def resolve_watch(self, watch: Any, session_id: str) -> tuple:
        """把 watch 写法解析为 (kind, value, label, error)。

        kind: session / window / all。agent 在创建时一次性解析成 window，
        避免每条事件都读团队成员表。
        """
        text = str(watch or "").strip()
        if not text:
            return "session", session_id, "当前会话", ""
        if text == "*":
            return "all", "", "所有窗口", ""
        if text.startswith("session:"):
            value = text[len("session:") :].strip()
            if not value:
                return "", "", "", "watch=session: 需要提供会话 ID"
            return "session", value, f"会话 {value[:8]}", ""
        if text.startswith("window:"):
            value = text[len("window:") :].strip()
            if not value:
                return "", "", "", "watch=window: 需要提供窗口 ID"
            return "window", value, f"窗口 {value}", ""
        if text.startswith("agent:"):
            ident = text[len("agent:") :].strip()
            if not ident:
                return "", "", "", "watch=agent: 需要提供角色名（可写 agent@window_id）"
            window_id = self._resolve_agent_window(ident)
            if not window_id:
                return "", "", "", f"未找到团队成员 {ident}（团队未组建或成员已退出）"
            label = f"成员 {ident.split('@', 1)[0]}"
            return "window", window_id, label, ""
        return "", "", "", f"无法解析 watch：{text}，支持 session:/window:/agent:/* 或留空"

    @staticmethod
    def _resolve_agent_window(ident: str) -> str:
        """团队成员标识（角色名或角色名@window_id）→ 窗口 id。"""
        try:
            from app.core.team.team_manager import TeamManager

            tm = TeamManager.get_instance()
            member = tm.find_member(ident) if tm is not None else None
            return str((member or {}).get("window_id", "") or "")
        except Exception as e:  # noqa: BLE001 - 团队未启用时按解析失败处理
            logger.warning(f"[system-timer] 成员窗口解析失败 {ident}: {e}")
            return ""

    def _ensure_subscriptions(self) -> None:
        """给尚未订阅的存活窗口补建事件桥（tick 每秒调一次）。"""
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            registry = UIPluginRegistry.get_instance()
            if registry is None:
                return
            widgets = dict(getattr(registry, "_window_main_widgets", {}) or {})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[system-timer] 窗口表不可用，跳过事件订阅: {e}")
            return
        # 已关闭窗口的桥就地释放（异常退出/强制关窗时 Qt 未必及时回收）
        for window_id in list(self._bridges):
            if window_id in widgets:
                continue
            bridge = self._bridges.pop(window_id, None)
            if bridge is not None:
                try:
                    bridge.event.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                bridge.deleteLater()
        for window_id, widget in widgets.items():
            window_id = str(window_id)
            if window_id in self._bridges:
                continue
            backend = getattr(widget, "backend", None)
            if backend is None:
                continue
            bridge = _WindowEventBridge(window_id, widget)
            connected = 0
            for event_name, handler_name in _SIGNAL_HANDLERS.items():
                sig = getattr(backend, event_name, None)
                handler = getattr(bridge, handler_name, None)
                if sig is None or not hasattr(sig, "connect") or handler is None:
                    continue
                try:
                    sig.connect(handler)
                    self._connections.append((sig, handler))
                    connected += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[system-timer] 订阅 {event_name} 失败（{window_id}）: {e}")
            if not connected:
                bridge.deleteLater()
                continue
            bridge.event.connect(self._on_event)
            self._bridges[window_id] = bridge
            logger.debug(f"[system-timer] 已订阅窗口 {window_id} 的 {connected} 个对话流事件")

    def _on_event(self, event_name: str, window_id: str, session_id: str, payload: Optional[Dict[str, Any]]) -> None:
        """主线程事件入口：筛出匹配的重复任务并触发（锁内只标记 firing）。"""
        payload = dict(payload or {})
        now = time.monotonic()
        due: List[Dict[str, Any]] = []
        with self._lock:
            for task in self._tasks.values():
                if task.get("trigger") != "event" or task.get("event") != event_name:
                    continue
                if task.get("firing") or not self._watch_matches(task, window_id, session_id):
                    continue
                last = float(task.get("last_fire_at") or 0.0)
                if last and now - last < float(task.get("cooldown_seconds") or 0.0):
                    continue
                task["firing"] = True
                due.append(dict(task))
        for snapshot in due:
            try:
                self._fire_event(snapshot, payload, now)
            except Exception as e:  # noqa: BLE001 - 单任务异常不拖垮事件分发
                with self._lock:
                    task = self._tasks.get(snapshot["id"])
                    if task is not None:
                        task["firing"] = False
                logger.warning(f"[system-timer] 事件任务 {snapshot.get('id')} 触发异常（已跳过）: {e}")

    @staticmethod
    def _watch_matches(task: Dict[str, Any], window_id: str, session_id: str) -> bool:
        """按创建时解析的 watch 判定该事件是否命中任务。"""
        kind = task.get("watch_kind") or "session"
        value = str(task.get("watch_value") or "")
        if kind == "all":
            return True
        if kind == "window":
            return value == window_id
        return value == session_id

    def _fire_event(self, snapshot: Dict[str, Any], payload: Dict[str, Any], now: float) -> None:
        """触发一个命中的事件任务：渲染模板 → session.send 入队 → 计数/出表。"""
        task_id = snapshot["id"]
        delivered = False
        window_id = self._find_window(snapshot["session_id"])
        if window_id is None:
            logger.warning(
                f"[system-timer] 事件任务 {task_id} 未找到目标窗口"
                f"（session={snapshot['session_id'][:8]}，会话可能已切走/关闭）"
            )
        else:
            text = self._render_event_message(snapshot, payload)
            delivered = self._send(text, window_id, mode=snapshot.get("mode", "queue"))
            if not delivered:
                logger.warning(f"[system-timer] 事件任务 {task_id} 触发消息发送被拒（窗口忙/正在关闭）")

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return  # 触发期间被 cancel
            task["firing"] = False
            if not delivered:
                if not task.get("repeat"):
                    logger.warning(f"[system-timer] 一次性事件任务 {task_id} 按错过（missed）终结")
                    self._tasks.pop(task_id, None)
                return
            task["fires"] = int(task.get("fires") or 0) + 1
            task["last_fire_at"] = now
            if not task.get("repeat"):
                self._tasks.pop(task_id, None)
            elif task["fires"] >= int(task.get("max_fires") or 1):
                logger.info(f"[system-timer] 事件任务 {task_id} 达到 max_fires 上限，自动移除")
                self._tasks.pop(task_id, None)

    @staticmethod
    def _render_event_message(snapshot: Dict[str, Any], payload: Dict[str, Any]) -> str:
        """渲染事件触发消息：支持 {event} / {watch_label} / {response} 占位。"""
        text = str(snapshot.get("prompt") or "")
        mapping = {
            "{event}": str(snapshot.get("event") or ""),
            "{watch_label}": str(snapshot.get("watch_label") or "当前会话"),
            "{response}": str(payload.get("response") or "")[:500],
        }
        for key, value in mapping.items():
            text = text.replace(key, value)
        label = snapshot.get("watch_label") or "当前会话"
        return f"🔔 事件触发（{label} · {snapshot.get('event')}）：{text}"

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

    def _send(self, text: str, window_id: str, mode: str = "queue") -> bool:
        """经目标窗口 tool surface 能力池 session.send 入队（mode 透传触发方式）。"""
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
            return bool(send(text, mode=mode))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[system-timer] 触发消息发送失败: {e}")
            return False

    # ── 内部 ──

    @staticmethod
    def _snapshot(task: Dict[str, Any], now: float) -> Dict[str, Any]:
        snap = {
            "id": task["id"],
            "session_id": task["session_id"],
            "prompt": task["prompt"],
            "type": task["type"],
            "trigger": task.get("trigger", "time"),
            "mode": task.get("mode", "queue"),
        }
        if task.get("trigger") == "event":
            snap.update(
                {
                    "event": task.get("event", ""),
                    "watch": task.get("watch_label", ""),
                    "repeat": bool(task.get("repeat")),
                    "fires": int(task.get("fires") or 0),
                }
            )
        else:
            snap["next_fire_seconds"] = round(max(0.0, task["fire_at"] - now), 1)
        return snap


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
