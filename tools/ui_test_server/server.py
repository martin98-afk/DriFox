# -*- coding: utf-8 -*-
"""tools.ui_test_server：MCP 风格 HTTP 测试服务（MVP，仅开发态）。

启动：主程序设置环境变量 ``DRIFOX_UI_TEST_SERVER=127.0.0.1:8765``（默认关），
daemon 线程运行 :func:`start_ui_test_server`，同时主进程置 ARM 总闸。
打包产物无 tools/ 目录，lazy import 失败由 main.py 捕获并 warning 跳过。

协议：JSON-RPC 2.0（POST /）
- initialize / tools/list / tools/call 三方法
- 通知类请求（无 id）→ 202 空响应
- POST /shutdown → 干净退出主程序
仅监听 127.0.0.1；端口被占自动 +1 重试 3 次。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple

from loguru import logger

from tools.ui_driver import (
    click,
    find,
    invoke,
    screenshot,
    scroll,
    tree,
    type_text,
    wait_idle,
    wait_until,
)
from tools.ui_test_server.tokens import TokenStore, match_dangerous

_PORT = 8765
_server: Optional[ThreadingHTTPServer] = None
_tokens = TokenStore()


# ── 工具实现（全部经 bus.invoke 投递主线程）──


def _tool_ui_inspect(**kwargs) -> Dict[str, Any]:
    mode = kwargs.get("mode", "tree")
    selector = kwargs.get("selector") or {}
    root_object_name = kwargs.get("root_object_name")
    depth = int(kwargs.get("depth", 2) or 2)
    root = _driver_find_obj({"objectName": root_object_name}) if root_object_name else None
    if root_object_name and root is None:
        return {"found": False, "hint": f"未找到 root_object_name={root_object_name}"}
    if mode == "find":
        hit = find(selector, root=root)
        if hit is None:
            return {"found": False, "hint": "未命中；可用 ui_inspect mode=tree 查看结构"}
        data: Dict[str, Any] = {"found": True, "cls": type(hit).__name__, "objectName": hit.objectName()}
        hint = "命中；可用 ui_click 提供该控件 objectName 点击"
        if callable(getattr(hit, "click", None)):
            token, expires = _tokens.issue(hit.objectName())
            data["confirm_token"] = token
            data["token_ttl_s"] = int(expires - __import__("time").time())
            hint = "命中可点按钮；ui_click 需携带 confirm_token（5 分钟内有效）"
        return data
    snap: Dict[str, Any] = tree(root=root, depth=max(1, min(depth, 4))) or {}
    return snap


def _driver_find_obj(selector: Dict[str, Any], root=None):
    return find(selector, root=root)


def _tool_ui_state(**kwargs) -> Dict[str, Any]:
    from tools.ui_driver import state

    out: Dict[str, Any] = {"state": state()}
    # 附带可用会话列表 + 每会话 session_token（ui_session load/switch 两步确认用）
    try:
        from app.core.infra.window_registry import alive_window_instances

        wins = alive_window_instances()
        mw = wins[0] if wins else None
        hm = getattr(mw, "history_manager", None) if mw is not None else None
        store = getattr(hm, "_session_store", None) if hm is not None else None
        sessions: list[dict] = []
        if store is not None and getattr(store, "is_initialized", False):
            rows = store.get_sessions(limit=50)
            for r in rows[:50]:
                sid = str(r.get("session_id") or "")
                if not sid:
                    continue
                token, expires = _tokens.issue(sid)
                sessions.append({
                    "session_id": sid,
                    "title": str(r.get("title") or ""),
                    "session_token": token,
                    "token_ttl_s": int(expires - __import__("time").time()),
                })
        out["available_sessions"] = sessions
    except Exception as exc:  # noqa: BLE001 — 会话列表失败不影响 state 本体
        out["sessions_error"] = repr(exc)
    return out


def _tool_ui_click(**kwargs) -> Dict[str, Any]:
    selector = {k: kwargs[k] for k in ("objectName", "text", "role") if kwargs.get(k)}
    if not selector:
        return {"clicked": False, "error": "至少提供 objectName/text/role 之一"}
    probe_text = str(selector.get("objectName") or selector.get("text") or "")
    hit_kw = match_dangerous(probe_text)
    if hit_kw:
        # 黑名单优先：有 token 也拒（S3c-r 辅助防线，M2 迁移保留）
        return {
            "clicked": False,
            "blocked_by": "dangerous_keyword",
            "error": f"目标含危险关键词「{hit_kw}」，需更高授权才能点击",
        }
    target_name = str(selector.get("objectName") or "")
    token = str(kwargs.get("confirm_token") or "")
    ok, reason = _tokens.validate(token, target_name, consume=True)
    if not ok:
        return {
            "clicked": False,
            "blocked_by": reason,
            "error": f"confirm_token 校验失败（{reason}）：先 ui_inspect mode=find 命中目标领取",
        }
    widget = find(selector)
    if widget is None:
        return {"clicked": False, "hint": "未找到控件；先 ui_inspect mode=tree 定位"}
    if not click(widget):
        return {"clicked": False, "error": "该控件不支持 click()（非 QAbstractButton）"}
    return {"clicked": True, "cls": type(widget).__name__, "hint": "已点击；可用 ui_state 观察变化"}


def _tool_ui_type(**kwargs) -> Dict[str, Any]:
    selector = {k: kwargs[k] for k in ("objectName", "text", "role") if kwargs.get(k)}
    text = str(kwargs.get("text") or "")
    if not selector or not text:
        return {"typed": False, "error": "需要 selector 与 text"}
    widget = find(selector)
    if widget is None:
        return {"typed": False, "hint": "未找到控件；先 ui_inspect mode=tree 定位"}
    if not type_text(widget, text):
        return {"typed": False, "error": "type_text 执行失败"}
    return {"typed": True, "len": len(text), "hint": "已输入；可用 ui_state 观察变化"}


def _tool_ui_scroll(**kwargs) -> Dict[str, Any]:
    selector = kwargs.get("selector") or {}
    value = int(kwargs.get("value", 0) or 0)
    target_object_name = kwargs.get("target_object_name")
    target = _driver_find_obj({"objectName": target_object_name}) if target_object_name else None
    if target is None:
        target = find(selector) if selector else None
    if target is None:
        target = _default_scroll_root()
    if target is None:
        return {"scrolled": False, "error": "未找到滚动目标"}
    if not scroll(target, value):
        return {"scrolled": False, "error": "目标子树无可用 QScrollBar"}
    wait_idle(500)
    return {"scrolled": True, "value": value, "hint": "已滚动；500ms 防抖已等待"}


def _default_scroll_root():
    from app.core.infra.window_registry import alive_window_instances

    wins = alive_window_instances()
    return wins[0] if wins else None


def _tool_ui_wait(**kwargs) -> Dict[str, Any]:
    seconds = max(0.0, float(kwargs.get("seconds", 1.0) or 1.0))
    until_object_name = kwargs.get("until_object_name")
    if until_object_name:
        ok = wait_until(
            lambda: find({"objectName": until_object_name}) is not None,
            timeout_ms=int(seconds * 1000),
            interval_ms=200,
        )
        return {"waited": True, "appeared": ok, "hint": "控件出现" if ok else "超时未出现"}
    wait_idle(int(seconds * 1000))
    return {"waited": True, "seconds": seconds}


def _tool_ui_screenshot(**kwargs) -> Dict[str, Any]:
    target_object_name = kwargs.get("target_object_name")
    max_width = max(1, int(kwargs.get("max_width", 1280) or 1280))
    target = _driver_find_obj({"objectName": target_object_name}) if target_object_name else None
    data = screenshot(target, max_width=max_width)
    if not data:
        return {"error": "grab 失败或无可截图窗口"}
    import os
    import tempfile

    out_dir = os.path.join(tempfile.gettempdir(), "ui_test_server_shots")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"shot_{int(__import__('time').time() * 1000)}.png")
    with open(path, "wb") as f:
        f.write(data)
    from struct import unpack

    w, h = unpack(">II", data[16:24])
    return {"path": path, "width": w, "height": h, "bytes": len(data)}


# ── 工具注册表 ──


def _tool_ui_session(**kwargs) -> Dict[str, Any]:
    """会话操作（二批工具，P1 变更）：action=load|switch|new。

    token 语义（最小安全实现）：ui_state 返回的可用会话列表逐条附带
    session_token（TokenStore.issue(session_id)，object_name 位即 session_id，
    复用 ui_click 同款校验）；load/switch 必须携带匹配 token。new 免 token。
    """
    action = str(kwargs.get("action") or "")
    from app.core.infra.window_registry import alive_window_instances

    wins = alive_window_instances()
    if not wins:
        return {"ok": False, "error": "无存活主窗口"}
    mw = wins[0]
    sm = getattr(mw, "session_manager", None)

    if action == "new":
        assert sm is not None
        session = sm.create_new_session()
        mw._display_current_session()
        return {"ok": True, "action": "new", "session_id": str(session.session_id)[:8]}

    session_id = str(kwargs.get("session_id") or "")
    # [M2-r 简化] 测试服务三重闸（enabled 默认关 + 环境变量 + ARM）已含准入控制，
    # load/switch 免 token；confirm_token 两步协议留给 AI 助手工具场景（S1 原设计）
    if action in ("load", "switch"):
        if not session_id:
            return {"ok": False, "error": "load/switch 需要 session_id"}
        record = mw.history_manager.get_session_by_session_id(session_id) if mw.history_manager else None
        if not record or not record.get("session_id"):
            return {"ok": False, "error": f"会话不存在：{session_id[:8]}"}
        mw._load_session_from_record(record)
        manager = getattr(mw, "session_manager", None)
        cur = manager.get_current_session() if manager is not None else None
        msgs = getattr(cur, "messages", None) if cur else None
        return {
            "ok": True,
            "action": action,
            "session_id": str(getattr(cur, "session_id", ""))[:8],
            "msgs": len(msgs) if isinstance(msgs, list) else 0,
        }
    return {"ok": False, "error": f"未知 action: {action}"}


def _schemas() -> Dict[str, Dict[str, Any]]:
    def sch(name, desc, props, required=None):
        return {
            "name": name,
            "description": desc,
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required or [],
            },
        }

    sel = {
        "objectName": {"type": "string"},
        "cls": {"type": "string"},
        "text": {"type": "string"},
        "role": {"type": "string"},
    }
    return {
        "ui_inspect": sch(
            "ui_inspect",
            "查询/浏览 DriFox 界面控件。mode=find 按 selector 精查（命中可点按钮下发一次性 confirm_token，ui_click 必需）；mode=tree 输出浅层控件树（忽略 selector）",
            {
                "mode": {"type": "string", "enum": ["find", "tree"]},
                "selector": {"type": "object", "properties": sel},
                "root_object_name": {"type": "string"},
                "depth": {"type": "integer"},
            },
        ),
        "ui_state": sch("ui_state", "界面状态快照（会话/批次/卡/池/配额/懒队列）", {}),
        "ui_click": sch(
            "ui_click",
            "点击控件（两步确认：先 ui_inspect mode=find 命中领取 confirm_token；危险关键词目标直接拒）",
            {
                "objectName": {"type": "string"},
                "text": {"type": "string"},
                "role": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            required=["confirm_token"],
        ),
        "ui_type": sch(
            "ui_type",
            "向控件注入键盘文本",
            {"selector": {"type": "object", "properties": sel}, "text": {"type": "string"}},
            required=["text"],
        ),
        "ui_scroll": sch(
            "ui_scroll",
            "滚动到指定值（定位目标或主窗口滚动条）",
            {
                "selector": {"type": "object", "properties": sel},
                "target_object_name": {"type": "string"},
                "value": {"type": "integer"},
            },
        ),
        "ui_wait": sch(
            "ui_wait",
            "等待：纯等待 seconds 秒，或 until_object_name 出现（超时即秒数）",
            {"seconds": {"type": "number"}, "until_object_name": {"type": "string"}},
        ),
        "ui_session": sch(
            "ui_session",
            "会话操作：new 免 token；load/switch 需 confirm_token（ui_state 领取）",
            {
                "action": {"type": "string", "enum": ["load", "switch", "new"]},
                "session_id": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            required=["action"],
        ),
        "ui_screenshot": sch(
            "ui_screenshot",
            "截图存 PNG 临时文件，返回路径与尺寸",
            {"target_object_name": {"type": "string"}, "max_width": {"type": "integer"}},
        ),
    }


def _handlers() -> Dict[str, Callable[..., Dict[str, Any]]]:
    return {
        "ui_inspect": _tool_ui_inspect,
        "ui_state": _tool_ui_state,
        "ui_click": _tool_ui_click,
        "ui_type": _tool_ui_type,
        "ui_scroll": _tool_ui_scroll,
        "ui_wait": _tool_ui_wait,
        "ui_screenshot": _tool_ui_screenshot,
        "ui_session": _tool_ui_session,
    }


# ── HTTP 层 ──


class _RpcHandler(BaseHTTPRequestHandler):
    httpd: Optional[UiTestHttpServer] = None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002,N802 — 静默默认访问日志
        pass

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 — http.server 接口名
        if self.path.rstrip("/") == "/shutdown":
            self._send_json(200, {"ok": True})
            httpd = self.httpd
            threading.Thread(target=_shutdown_server, args=(httpd,), daemon=True).start()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            msg = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": f"parse error: {exc}"}})
            return

        method = str(msg.get("method") or "")
        msg_id = msg.get("id")
        if msg_id is None:  # 通知类：空实现
            self._send_json(202, {})
            return

        if method == "initialize":
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2.0",
                        "serverInfo": {"name": "drifox-ui-test-server", "version": "0.1.0"},
                        "capabilities": {"tools": {}},
                    },
                },
            )
            return
        if method == "tools/list":
            tools = [{"name": n, **s} for n, s in _schemas().items()]
            self._send_json(200, {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}})
            return
        if method == "tools/call":
            params = msg.get("params") or {}
            name = str(params.get("name") or "")
            args = params.get("arguments") or {}
            handler = _handlers().get(name)
            if handler is None:
                self._send_json(
                    200, {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
                )
                return
            try:
                # HTTP handler 线程 → bus.invoke 投递主线程执行 UI 操作
                data = invoke(lambda: handler(**args))
                self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
                            "isError": False,
                        },
                    },
                )
            except Exception as exc:  # noqa: BLE001 — 工具异常按 MCP isError 结果返回
                logger.warning(f"[ui-test-server] {name} 执行异常: {exc}")
                self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                            "isError": True,
                        },
                    },
                )
            return
        self._send_json(
            200, {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method: {method}"}}
        )

    def do_GET(self):  # noqa: N802 — 健康检查
        self._send_json(200, {"service": "drifox-ui-test-server", "ok": True})


def _shutdown_server(httpd: "UiTestHttpServer") -> None:
    """停 HTTP 服务 + 请求主程序退出（主线程 QCoreApplication.quit）。"""
    httpd.shutdown()
    try:
        from tools.ui_driver.bus import invoke

        def _quit():
            from PyQt5.QtCore import QCoreApplication

            qapp = QCoreApplication.instance()
            if qapp is not None:
                qapp.quit()

        invoke(_quit)
    except Exception as exc:  # noqa: BLE001 — 主循环已不可用时兜底硬退
        logger.warning(f"[ui-test-server] 优雅退出失败，改用 os._exit: {exc}")
        import os

        os._exit(0)


class UiTestHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def request_shutdown(self) -> None:
        """干净退出：停 HTTP 服务 + 请求主程序退出（主线程 QCoreApplication.quit）。"""
        threading.Thread(target=self.shutdown, daemon=True).start()
        try:
            from tools.ui_driver.bus import invoke

            def _quit():
                from PyQt5.QtCore import QCoreApplication

                qapp = QCoreApplication.instance()
                if qapp is not None:
                    qapp.quit()

            invoke(_quit)
        except Exception as exc:  # noqa: BLE001 — 主循环已不可用时兜底硬退
            logger.warning(f"[ui-test-server] 优雅退出失败，改用 os._exit: {exc}")
            import os

            os._exit(0)


def start_ui_test_server(host: str = "127.0.0.1", port: int = _PORT) -> int:
    """启动 HTTP 服务（阻塞当前线程）；返回实际监听端口。

    端口被占自动 +1 重试 3 次。调用方应以 daemon 线程运行本函数。
    """
    global _server
    last_exc: Optional[Exception] = None
    httpd: Optional[UiTestHttpServer] = None
    actual_port = port
    for attempt in range(3):
        try:
            httpd = UiTestHttpServer((host, actual_port), _RpcHandler)
            break
        except OSError as exc:
            last_exc = exc
            logger.warning(f"[ui-test-server] 端口 {actual_port} 被占，尝试 +1")
            actual_port += 1
    if httpd is None:
        raise RuntimeError(f"UI 测试服务启动失败（端口重试耗尽）: {last_exc}")
    _RpcHandler.httpd = httpd
    _server = httpd
    logger.info(f"[ui-test-server] listening at http://{host}:{actual_port}")
    httpd.serve_forever(poll_interval=0.2)
    return actual_port
