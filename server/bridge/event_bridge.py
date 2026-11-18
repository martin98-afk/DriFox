"""Qt→Web 事件桥。

架构（docs/tauri/server-arch.md §二 / T13 c 表）：
反射枚举 ChatBackend 全部 pyqtSignal 逐一 connect 到统一分发器——
新增信号自动覆盖，无白名单；对外事件名经 EVENT_MAP 映射（闭集语义，
未映射的 QObject 内置信号丢弃不广播）。
事件方向：Qt 信号（任意线程 emit）→ dispatch（QueuedConnection 回
QCoreApplication 主线程）→ loop.call_soon_threadsafe 投递 asyncio 队列
→ WS 广播。

sid 约定（T13 c 表）：
- session 域事件（session.created/changed）以信封 sid 承载 session_id，
  payload 不重复携带
- stream/message 域事件 sid=当前会话（dispatch 时取 get_current_session）
- executor.ready 全局事件 sid="" 至多一次
"""
import json
import logging
from typing import Any

from PyQt5.QtCore import QObject, pyqtBoundSignal

logger = logging.getLogger("drifox.server.bridge")


def _jsonable(v: Any, depth: int = 0) -> Any:
    """参数 JSON 安全化：不可序列化的对象转 repr 截断（架构文档 §二）。"""
    if depth > 4:
        return repr(v)[:200]
    if isinstance(v, dict):
        return {str(k): _jsonable(val, depth + 1) for k, val in list(v.items())[:64]}
    if isinstance(v, (list, tuple)):
        return [_jsonable(val, depth + 1) for val in v[:128]]
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return repr(v)[:200]


def _sid_of_current(backend) -> str:
    """当前会话 sid；引擎未就绪/无会话时空串。仅 Qt 主线程调用。"""
    try:
        s = backend.get_current_session()
        return s.session_id if s else ""
    except Exception:
        return ""


def _map_stream_started(args, sid):
    return {}, sid

def _map_content_delta(args, sid):
    return {"text": args[0]}, sid

def _map_finished(args, sid):
    return {"final_text": args[0]}, sid

def _map_session(args, sid):
    # session_created/changed(str session_id)：sid 承载，payload 不重复
    return {}, str(args[0])

def _map_message(args, sid):
    # message_received(dict)：payload 原样，sid 取 payload 内标注或当前会话
    p = args[0] if isinstance(args[0], dict) else {"raw": _jsonable(args[0])}
    return p, str(p.get("session_id") or sid)

def _map_session_deleted(args, sid):
    # session_deleted(int index)：index 已删除无法回查 id，全局 sid=""
    return {"index": int(args[0])}, ""

def _map_conversation_error(args, sid):
    # conversation.error{error:object}（T13 c 表：与 error{message} 并存）
    err = _jsonable(args[0])
    esid = err.get("session_id") if isinstance(err, dict) else None
    return {"error": err}, (str(esid) if esid else sid)

def _map_error(args, sid):
    return {"message": str(args[0])}, sid

def _map_tool_started(args, sid):
    return {"tool_call_id": args[0], "tool_name": args[1], "arguments": _jsonable(args[2])}, sid

def _map_tool_result(args, sid):
    return {"tool_call_id": args[0], "tool_name": args[1], "result": _jsonable(args[3])}, sid

def _map_permission(args, sid):
    return {"tool_call_id": args[0], "tool_name": args[1], "arguments": _jsonable(args[2])}, sid

def _map_context(args, sid):
    return {"token_count": int(args[0]), "limit": int(args[1])}, sid

def _map_hook_status(args, sid):
    return {"event": str(args[0]), "status": str(args[1]), "is_start": bool(args[2])}, sid

# Qt 信号名 → (对外事件名, payload 整形)。
# 未列入的信号（含 QObject 内置 destroyed/objectNameChanged）不广播。
EVENT_MAP = {
    "stream_started": ("stream.started", _map_stream_started),
    "stream_chunk": ("stream.content_delta", _map_content_delta),
    "stream_finished": ("stream.finished", _map_finished),
    "reasoning_content": ("stream.reasoning_delta", _map_content_delta),
    "message_received": ("message.received", _map_message),
    "messages_updated": ("messages.updated", lambda a, s: ({}, s)),
    "session_created": ("session.created", _map_session),
    "session_changed": ("session.changed", _map_session),
    "session_deleted": ("session.deleted", _map_session_deleted),
    "conversation_error": ("conversation.error", _map_conversation_error),
    "error_occurred": ("error", _map_error),
    "tool_executor_ready": ("executor.ready", lambda a, s: ({}, "")),
    "tool_call_started": ("tool.started", _map_tool_started),
    "tool_result_received": ("tool.result", _map_tool_result),
    # permission_requested 不在 EVENT_MAP：backend.py:780-793 实证该 Qt 信号
    # 无 emit 点，下行唯一通道是 set_callback("permission_approval_requested")
    # （register_engine_callbacks）。未来若补 emit 点，双通道会结构性双发。
    "context_updated": ("context.updated", _map_context),
    "hook_status_changed": ("hook.status", _map_hook_status),
}


class EventBridge(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop = None            # asyncio event loop（api 线程）
        self._asyncio_queue = None   # loop 内 asyncio.Queue
        self._backend = None
        self.connected_signals: list[str] = []
        self._executor_ready_sent = False

    def attach(self, backend, loop, asyncio_queue) -> None:
        """反射枚举 backend 全部 pyqtSignal 并 connect（实例级访问得 bound signal）。"""
        self._loop = loop
        self._asyncio_queue = asyncio_queue
        self._backend = backend
        for name in dir(type(backend)):
            if name.startswith("_"):
                continue
            sig = getattr(backend, name, None)
            if not isinstance(sig, pyqtBoundSignal):
                continue
            # 默认参固化信号名，避免闭包晚绑定
            sig.connect(lambda *args, _name=name: self.dispatch(_name, args))
            self.connected_signals.append(name)
        logger.info("事件桥已接通 %d 个信号: %s", len(self.connected_signals),
                    ",".join(self.connected_signals))

    def dispatch(self, name: str, args: tuple) -> None:
        """统一分发器（Qt 主线程执行）：映射→sid→入队。"""
        entry = EVENT_MAP.get(name)
        if entry is None:
            return  # 未映射信号（内置/未知）不广播，保持事件清单闭集
        event_name, mapper = entry
        if name == "tool_executor_ready":
            if self._executor_ready_sent:
                return  # 全局事件至多一次（T13 c 表）
            self._executor_ready_sent = True
        sid = _sid_of_current(self._backend) if self._backend else ""
        try:
            payload, sid = mapper(args, sid)
        except Exception:
            logger.exception("事件整形失败: %s %r", name, args)
            return
        self._push({"type": event_name, "payload": payload, "sid": sid})

    def emit_custom(self, name: str, payload: Any, sid: str = None) -> None:
        """命令回执/引擎回调等非信号事件复用同一出口。

        sid=None 时自动取当前会话（worker 线程触发也安全：仅读 Python 属性）。
        """
        if sid is None and self._backend is not None:
            sid = _sid_of_current(self._backend)
        self._push({"type": name, "payload": _jsonable(payload), "sid": sid or ""})

    def register_engine_callbacks(self, backend) -> None:
        """注册引擎回调（question/permission/retry 下行，M2b）。

        backend.permission_requested 等 Qt 信号有定义但无 emit 点
        （backend.py:780-793 注释：实时事件走回调字典链路），故与主程序
        main_widget 同法走 set_callback。ChatBackend.set_callback 在引擎
        未就绪时暂存 _pending_engine_callbacks（#8r 修正），注册时序安全。
        回调在引擎 worker 线程触发 → emit_custom 内 call_soon_threadsafe 线程安全。
        """
        self._backend = backend

        def _cb(event_name, payload_fn):
            def callback(*args):
                sid = _sid_of_current(backend)
                self.emit_custom(event_name, payload_fn(args), sid=sid)
            return callback

        backend.set_callback(
            "permission_approval_requested",
            _cb("permission.requested", lambda a: {
                "tool_call_id": a[0], "tool_name": a[1], "arguments": _jsonable(a[2]),
            }),
        )
        backend.set_callback(
            "question_asked",
            _cb("question.asked", lambda a: {
                "id": a[0], "questions": _jsonable(a[1]), "extra": _jsonable(a[2]) if len(a) > 2 else {},
            }),
        )
        backend.set_callback(
            "retry_status",
            _cb("retry.status", lambda a: {
                "error_type": str(a[0]), "attempt": int(a[1]),
                "max_retries": int(a[2]), "wait_time": float(a[3]),
            }),
        )
        backend.set_callback(
            "retry_resolved",
            _cb("retry.resolved", lambda a: {}),
        )
        logger.info("引擎回调已注册: permission/question/retry 下行走 set_callback 链路")

    def _push(self, event: dict) -> None:
        if self._loop is None or self._asyncio_queue is None:
            return
        self._loop.call_soon_threadsafe(self._asyncio_queue.put_nowait, event)
