# -*- coding: utf-8 -*-
"""宿主进程内 MCP gateway — 本地插件以 HTTP 提供 MCP 工具，无需外部子进程。

只讲 Streamable HTTP 的纯 JSON 分支：不返回 Mcp-Session-Id，客户端
streamablehttp_client 收到 200 + application/json 后不会发起 GET SSE 流。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from loguru import logger

ProtocolVersion = "2025-06-18"


class _Entry:
    __slots__ = ("plugin", "handler", "tools")

    def __init__(self, plugin: str, handler: Callable[[dict], dict], tools: List[dict]):
        self.plugin = plugin
        self.handler = handler
        self.tools = tools


class LocalMcpGateway:
    """单例由模块级 get_gateway() 持有。端口 :0 由 OS 分配。"""

    def __init__(self) -> None:
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._handlers: Dict[str, _Entry] = {}
        self._port = 0

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> int:
        with self._lock:
            if self._server is not None:
                return self._port
            gw = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    try:
                        req = json.loads(self.rfile.read(length) or b"{}")
                    except (ValueError, OSError):
                        self._json({"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "Parse error"}})
                        return
                    name = urlparse(self.path).path.rsplit("/", 1)[-1]
                    resp = gw._dispatch(name, req)
                    if resp is None:  # notification
                        self.send_response(202)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self._json(resp)

                def _json(self, obj):
                    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._server = server
            self._port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True,
                                 name="local-mcp-gateway")
            t.start()
            self._thread = t
            logger.info(f"[LocalMcpGateway] 已启动 127.0.0.1:{self._port}")
            return self._port

    def stop(self) -> None:
        with self._lock:
            if self._server is None:
                return
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None
            self._port = 0
            self._handlers.clear()

    def register(self, server_name: str, handler: Callable[[dict], dict],
                 *, plugin: str = "", tools: Optional[List[dict]] = None) -> bool:
        """handler(req_dict) -> {'text': str} 或 {'text': str, 'isError': bool}。"""
        try:
            with self._lock:
                self._handlers[server_name] = _Entry(plugin, handler, tools or [])
            return True
        except Exception as e:
            logger.warning(f"[LocalMcpGateway] 注册 {server_name} 失败: {e}")
            return False

    def unregister_plugin(self, plugin: str) -> None:
        with self._lock:
            for name in [k for k, v in self._handlers.items() if v.plugin == plugin]:
                self._handlers.pop(name, None)

    def _dispatch(self, server_name: str, req: dict) -> Optional[dict]:
        method = req.get("method", "")
        rid = req.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": ProtocolVersion,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": server_name, "version": "1.0"},
            }}
        if method.startswith("notifications/"):
            return None
        with self._lock:
            entry = self._handlers.get(server_name)
        if entry is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"未注册 MCP server: {server_name}"}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": entry.tools}}
        if method == "tools/call":
            try:
                out = entry.handler(req.get("params") or {})
                text = out.get("text", "") if isinstance(out, dict) else str(out)
                is_error = bool(out.get("isError")) if isinstance(out, dict) else False
                return {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": text}], "isError": is_error}}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": f"工具执行异常: {e}"}],
                    "isError": True}}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"未知方法: {method}"}}


_gateway: Optional[LocalMcpGateway] = None


def get_gateway() -> LocalMcpGateway:
    global _gateway
    if _gateway is None:
        _gateway = LocalMcpGateway()
        _gateway.start()
    return _gateway
