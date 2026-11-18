"""FastAPI 装配：REST /api/* + WS /ws。

运行于 uvicorn asyncio 工作线程。与 Qt 主线程仅通过两条 queue 转发：
- 事件：bridge → asyncio_queue（call_soon_threadsafe 投递）→ drain task 广播
- 命令/调用：REST/WS → command_queue（带 reply Future）→ Qt 主线程 QTimer
  泵消费执行 backend 方法 → Future 结果回传（qt_call）

WS 信封（架构文档 §三）：{v:1,type:"event|client_cmd|ack|error",name,seq,ts,sid,payload}
连接建立首推 hello{server_version,active_sessions}（信封 seq 全局单调递增，
payload 不再携带 seq——review 🟡2：双 seq 错位会诱发前端误判跳变）。
"""
import asyncio
import collections
import os
import concurrent.futures
import itertools
import logging
import queue
import time
from dataclasses import dataclass, field

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logger = logging.getLogger("drifox.server.api")

_CALL_TIMEOUT = 60.0  # backend 调用兜底超时（含引擎异步启动窗口）


@dataclass
class ServerContext:
    """api 层与 Qt 侧共享的运行时句柄（只读引用 + 线程安全队列）。"""

    version: str
    pid: int
    port: int
    command_queue: "queue.Queue"  # 命令：api 线程 → Qt 主线程泵
    on_loop_ready: object = field(default=None)  # startup 完成回调
    loop: object = field(default=None)  # startup 后由本模块注入
    asyncio_queue: object = field(default=None)  # startup 后创建，bridge 目标
    bridge: object = field(default=None)  # EventBridge，attach 后注入
    backend: object = field(default=None)  # ChatBackend，初始化成功后注入
    auth_token: str = ""  # 🟡6 WS 握手鉴权 token（stdout 握手下发）


class SessionCreateReq(BaseModel):
    title: str = ""
    project: str = ""
    agent: str = ""


class SessionPatchReq(BaseModel):
    title: str = ""
    agent: str = ""


class ModelConfig(BaseModel):
    model_config = {"extra": "allow"}
    provider: str = ""
    model: str = ""


class SendCmd(BaseModel):
    session_id: str = ""
    content: str
    agent: str = ""


class StopCmd(BaseModel):
    session_id: str = ""


class PermissionDecision(BaseModel):
    decision: str  # approve | deny（T13 b 表枚举）
    auto_allow: str = ""  # tool | round | session（作用域映射）
    session_allow: bool = False
    reason: str = ""


class QuestionAnswer(BaseModel):
    id: str = ""
    answers: list[str] = []


async def qt_call(ctx: ServerContext, name: str, args: dict | None = None):
    """跨线程调用 Qt 主线程 backend 方法：入命令队列 + Future 回传。"""
    fut = concurrent.futures.Future()
    ctx.command_queue.put(
        {"kind": "call", "name": name, "args": args or {}, "reply": fut}
    )
    loop = asyncio.get_running_loop()
    aio_fut = asyncio.wrap_future(fut)
    return await asyncio.wait_for(aio_fut, timeout=_CALL_TIMEOUT)


def create_api(ctx: ServerContext) -> FastAPI:
    app = FastAPI(title="DriFox Server")

    # 🟡2 REST 全端点 token 校验（X-Drifox-Token，与 WS 同 token）；
    # DRIFOX_REST_AUTH=0 降级跳过（壳侧同 uid 信任场景），默认开启
    rest_auth_enabled = os.environ.get("DRIFOX_REST_AUTH", "1") != "0"

    @app.middleware("http")
    async def _rest_token_guard(request, call_next):
        if rest_auth_enabled and ctx.auth_token:
            path = request.url.path
            if path.startswith("/api") and path != "/api/health":
                if request.headers.get("x-drifox-token") != ctx.auth_token:
                    from fastapi.responses import JSONResponse
                    return JSONResponse({"detail": "invalid token"}, status_code=401)
        return await call_next(request)
    # 🔴1：per-client 发送队列统一串行——重放帧先灌队列消费序，重放期间
    # drain 产生的新事件只能排在重放之后，交接窗不丢不重
    clients: dict = {}  # ws -> asyncio.Queue（待发信封）
    # 断线重连补偿（架构文档 §三）：全局事件环形缓冲，seq 全局单调故共享安全
    buf_size = int(os.environ.get("DRIFOX_EVENT_BUFFER", "2000"))
    event_buffer: collections.deque = collections.deque(maxlen=max(2, buf_size))
    seq_counter = itertools.count(1)
    seq_state = {"last": 0}  # 当前已分配最大 seq（itertools.count 无法回读当前值）

    def next_seq() -> int:
        seq_state["last"] = next(seq_counter)
        return seq_state["last"]

    def envelope(name: str, payload, *, etype: str = "event", sid: str = "") -> dict:
        return {
            "v": 1,
            "type": etype,
            "name": name,
            "seq": next_seq(),
            "ts": time.time(),
            "sid": sid,
            "payload": payload,
        }

    def active_sessions() -> int:
        sm = getattr(ctx.backend, "session_manager", None)
        try:
            return len(sm.get_all_sessions())
        except Exception:
            return 0

    # ---------- REST ----------

    @app.get("/api/health")
    async def health():
        return {
            "drifox_server": {
                "status": "ok",
                "version": ctx.version,
                "pid": ctx.pid,
                "port": ctx.port,
                "backend_ready": bool(
                    getattr(ctx.backend, "_initialized", False)
                ),
                "bridge_signals": len(getattr(ctx.bridge, "connected_signals", [])),
            }
        }

    @app.get("/api/sessions")
    async def list_sessions(project: str = "", limit: int = 50, offset: int = 0):
        data = await qt_call(ctx, "list_sessions", {"project": project})
        items = data["items"]
        return {"items": items[offset : offset + limit], "total": len(items)}

    @app.post("/api/sessions")
    async def create_session(req: SessionCreateReq):
        return await qt_call(
            ctx,
            "create_session",
            {"title": req.title, "project": req.project, "agent": req.agent},
        )

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        return await qt_call(ctx, "get_session", {"session_id": session_id})

    @app.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: str, req: SessionPatchReq):
        return await qt_call(
            ctx, "patch_session", {"session_id": session_id, "title": req.title}
        )

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        return await qt_call(ctx, "delete_session", {"session_id": session_id})

    @app.get("/api/projects")
    async def list_projects():
        return await qt_call(ctx, "list_projects", {})

    @app.post("/api/permissions/{tool_call_id}")
    async def decide_permission(tool_call_id: str, req: PermissionDecision):
        if req.decision not in ("approve", "deny"):
            return {"ok": False, "error": f"decision 枚举非法: {req.decision}"}
        return await qt_call(ctx, "permission_decide", {
            "tool_call_id": tool_call_id,
            "decision": req.decision,
            "auto_allow": req.auto_allow,
            "session_allow": req.session_allow,
            "reason": req.reason,
        })

    @app.post("/api/questions/{question_id}/answer")
    async def answer_question(question_id: str, req: QuestionAnswer):
        return await qt_call(ctx, "question_answer", {
            "question_id": question_id or req.id,
            "answers": req.answers,
        })

    # ---------- M3a：配置 / 插件管理 / UI 槽清单 ----------

    @app.get("/api/config/model")
    async def get_config_model():
        return await qt_call(ctx, "get_config_model", {})

    @app.put("/api/config/model")
    async def put_config_model(req: ModelConfig):
        return await qt_call(ctx, "put_config_model", {"config": req.model_dump()})

    @app.get("/api/agents")
    async def list_agents():
        data = await qt_call(ctx, "list_agents", {})
        return data.get("items", [])

    @app.get("/api/plugins")
    async def list_plugins():
        data = await qt_call(ctx, "list_plugins", {})
        return data.get("items", [])

    @app.post("/api/plugins/{name}/enable")
    async def enable_plugin(name: str):
        return await qt_call(ctx, "set_plugin_enabled", {"name": name, "enabled": True})

    @app.post("/api/plugins/{name}/disable")
    async def disable_plugin(name: str):
        return await qt_call(ctx, "set_plugin_enabled", {"name": name, "enabled": False})

    @app.post("/api/plugins/{name}/action")
    async def plugin_action(name: str, req: dict):
        try:
            return await qt_call(ctx, "plugin_action", {"name": name, "field_key": req.get("field_key", "")})
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    @app.delete("/api/plugins/{name}")
    async def uninstall_plugin(name: str):
        return await qt_call(ctx, "uninstall_plugin", {"name": name})

    @app.get("/api/context/usage")
    async def context_usage():
        return await qt_call(ctx, "context_usage", {})

    @app.get("/api/input-history")
    async def input_history():
        data = await qt_call(ctx, "input_history", {})
        return data.get("items", [])

    @app.get("/api/coding-plan/usage")
    async def coding_plan_usage():
        return await qt_call(ctx, "coding_plan_usage", {})

    @app.get("/api/user/profile")
    async def user_profile():
        return await qt_call(ctx, "user_profile", {})

    @app.get("/api/ui/sidebar-items")
    async def ui_sidebar_items():
        return await qt_call(ctx, "ui_sidebar_items", {})

    @app.put("/api/ui/pinned")
    async def set_ui_pinned(req: dict):
        return await qt_call(ctx, "set_ui_pinned", {
            "key": req.get("key", ""), "value": bool(req.get("value", False)),
        })

    @app.get("/api/ui/context-menu")
    async def ui_context_menu():
        return await qt_call(ctx, "ui_context_menu", {})

    @app.get("/api/ui/plugin-windows")
    async def ui_plugin_windows():
        return await qt_call(ctx, "ui_plugin_windows", {})

    @app.get("/api/plugins/{name}/welcome-tabs")
    async def welcome_tabs(name: str):
        data = await qt_call(ctx, "welcome_tabs", {})
        items = [i for i in data.get("items", []) if i.get("plugin_name") == name]
        return {"items": items}

    # ---------- WS ----------

    async def _handle_client_cmd(msg: dict):
        """client_cmd → Qt 主线程执行 → ack{ok, cid, ...}。"""
        cid = msg.get("cid", "")
        name = msg.get("name", "")
        args = msg.get("args", {}) or {}
        try:
            if name == "ping":
                result = {"pong": True}
            elif name == "send":
                result = await qt_call(ctx, "send", args)
            elif name == "stop":
                result = await qt_call(ctx, "stop", args)
            else:
                await _send_envelope(
                    "error", {"message": f"未知命令: {name}", "cid": cid},
                    etype="ack", sid="",
                )
                return
            await _send_envelope(
                "ack", {"ok": True, "cid": cid, **result}, etype="ack", sid=""
            )
        except Exception as e:
            logger.exception("client_cmd 执行失败: %s", name)
            await _send_envelope(
                "error", {"ok": False, "cid": cid, "message": str(e)[:300]},
                etype="ack", sid="",
            )

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        # 🟡6 握手鉴权：query token 不匹配即拒（close 4401）；REST /api 本地绑定暂不校验（M3 评估）
        if ctx.auth_token and ws.query_params.get("token") != ctx.auth_token:
            await ws.close(code=4401, reason="invalid token")
            return
        await ws.accept()
        outq_pre: asyncio.Queue = asyncio.Queue()
        # 🟡2 修正：hello payload 不带 seq（信封 seq 已承载，避免双 seq 错位）
        hello_env = envelope(
            "hello",
            {
                "server_version": ctx.version,
                "active_sessions": active_sessions(),
            },
        )
        await ws.send_json(hello_env)
        # 断线重连补偿：?since={last_seq} → 重放缓冲 seq>last 再进实时流；
        # 缓冲不覆盖（最早缓冲 seq > last+1 或无缓冲且已发过事件）→ 单条 resync
        # 🔴1：重放帧不直接 send，交由 per-client sender 与实时队列串行化
        replay = []
        since_raw = ws.query_params.get("since")
        if since_raw is not None:
            try:
                last = int(since_raw)
            except ValueError:
                last = 0
            buffered = [e for e in event_buffer if e["seq"] > last]
            cur_head = seq_state["last"]  # 已分配最大 seq
            covered = bool(buffered) and buffered[0]["seq"] <= last + 1
            if last > 0 and not covered and cur_head > last:
                replay.append(envelope("resync", {"reason": "buffer_overflow"}, sid=""))
            else:
                replay.extend(buffered)
        outq: asyncio.Queue = asyncio.Queue()
        clients[ws] = outq
        sender = asyncio.create_task(_client_sender(ws, outq, replay))
        clients_senders[ws] = sender
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "client_cmd":
                    await _handle_client_cmd(msg)
                else:
                    await _send_envelope(
                        "error",
                        {"message": "仅接受 client_cmd", "cid": msg.get("cid", "")},
                        etype="ack", sid="",
                    )
        except WebSocketDisconnect:
            clients.pop(ws, None)
            clients_senders.pop(ws, None)
            sender.cancel()
        except Exception:
            clients.pop(ws, None)
            clients_senders.pop(ws, None)
            sender.cancel()
            logger.exception("WS 会话异常")

    async def _send_envelope(name, payload, *, etype="event", sid="", ws=None):
        env = envelope(name, payload, etype=etype, sid=sid)
        targets = [ws] if ws is not None else list(clients)
        for w in targets:
            try:
                await w.send_json(env)
            except Exception:
                if ws is None:
                    clients.discard(w)

    clients_senders: dict = {}  # ws -> sender task（断开时 cancel）

    async def _client_sender(ws: WebSocket, outq: asyncio.Queue, replay: list):
        """per-client 串行发送：重放帧 → 实时队列帧（🔴1 交接窗不丢不重）。"""
        try:
            for env in replay:
                await ws.send_json(env)
            while True:
                env = await outq.get()
                await ws.send_json(env)
        except asyncio.CancelledError:
            pass
        except Exception:
            clients.pop(ws, None)

    async def _drain_events() -> None:
        """事件 drain：asyncio_queue → 环形缓冲 + 广播全部 WS 客户端（信封带事件 sid）。"""
        while True:
            event = await ctx.asyncio_queue.get()
            env = envelope(event["type"], event["payload"], sid=event.get("sid", ""))
            event_buffer.append(env)
            for outq in list(clients.values()):
                outq.put_nowait(env)

    @app.on_event("startup")
    async def _startup():
        ctx.loop = asyncio.get_running_loop()
        ctx.asyncio_queue = asyncio.Queue()
        asyncio.create_task(_drain_events())
        if ctx.on_loop_ready is not None:
            ctx.on_loop_ready()

    return app
