# -*- coding: utf-8 -*-
"""MCP OAuth 本地回调 server。

授权完成后浏览器会跳转到 redirect_uri（http://127.0.0.1:<port>/callback），
本模块负责起一个临时 HTTP server 收下授权码并交给 SDK 继续换 token。

设计要点：
- 端口用 0 让 OS 分配，避开固定端口冲突。redirect_uri 必须在授权前确定
  （DCR 注册时要提交），所以顺序是：先起 server 拿端口，再构造 client metadata。
- 用完即关。授权是偶发操作，常驻监听端口只会扩大攻击面。
- state 校验由 SDK 内部完成（secrets.compare_digest），此处只负责把
  code 与 state 原样带回，不做二次判断以免两处口径不一致。
- 只用标准库 http.server，不引入 aiohttp 等新依赖。
"""

from __future__ import annotations

import asyncio
import html
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from loguru import logger

# 授权完成后展示给用户的页面。故意不含任何脚本与外链。
_SUCCESS_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>授权完成</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:60px">
<h2>授权成功</h2>
<p>可以关闭此页面，返回 DriFox。</p>
</body>
</html>
"""

_ERROR_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>授权失败</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:60px">
<h2>授权未完成</h2>
<p>{reason}</p>
<p>请返回 DriFox 重试。</p>
</body>
</html>
"""


@dataclass
class CallbackResult:
    """一次回调的结果。code 为 None 表示未收到授权码。

    cancelled=True 表示用户在 DriFox 界面主动中止（区别于超时与授权服务器报错）。
    """

    code: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None
    error_description: Optional[str] = None
    cancelled: bool = False


class _CallbackHandler(BaseHTTPRequestHandler):
    """只处理 GET /callback，其余路径一律 404。

    server 属性在运行期由 CallbackServer 注入 result 与 notify，
    不通过类属性共享，避免多实例互相串扰。
    """

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # 静默默认的 stderr 访问日志，改走 loguru。
        logger.debug(f"[MCP-OAuth] callback server: {format % args}")

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return

        params = parse_qs(parsed.query)
        result = self.server.result

        err = params.get("error", [None])[0]
        if err:
            result.error = err
            result.error_description = params.get("error_description", [None])[0]
            reason = html.escape(result.error_description or err)
            self._send(400, _ERROR_HTML.format(reason=reason).encode("utf-8"), "text/html; charset=utf-8")
        else:
            result.code = params.get("code", [None])[0]
            result.state = params.get("state", [None])[0]
            if result.code:
                self._send(200, _SUCCESS_HTML.encode("utf-8"), "text/html; charset=utf-8")
            else:
                result.error = "missing_code"
                self._send(400, _ERROR_HTML.format(reason="未收到授权码").encode("utf-8"), "text/html; charset=utf-8")

        # 收到结果立即唤醒等待方，不必等页面关掉。
        self.server.notify()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class CallbackServer:
    """临时回调 server。同一实例一次授权用完后即关闭。"""

    def __init__(self) -> None:
        self.result = CallbackResult()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # 收到回调后置位，等待方据此返回。
        self._event = threading.Event()

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("回调 server 尚未启动")
        return int(self._httpd.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    def start(self) -> str:
        """启动 server 并返回 redirect_uri。

        Raises:
            OSError: 端口绑定失败（极端情况，port=0 下几乎不会发生）
        """
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
        # 把自身注入 handler，替代类属性共享。
        self._httpd.result = self.result  # type: ignore[attr-defined]
        self._httpd.notify = self._event.set  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="mcp-oauth-callback", daemon=True)
        self._thread.start()
        logger.info(f"[MCP-OAuth] 回调 server 已启动: {self.redirect_uri}")
        return self.redirect_uri

    def wait(self, timeout: float) -> CallbackResult:
        """阻塞等待回调，超时返回当前结果（code 可能为 None）。"""
        if not self._event.wait(timeout=timeout):
            logger.warning(f"[MCP-OAuth] 等待回调超时（{timeout}秒）")
        return self.result

    def cancel(self) -> None:
        """中止等待：标记取消并立即唤醒等待方。幂等，且不覆盖已到达的回调结果。

        只置标记不关 server —— 关闭动作由授权线程的 finally 负责，
        这样 UI 线程调用本方法不会阻塞（shutdown + join 最长 2 秒）。
        """
        if self.result.code or self.result.error:
            return
        self.result.cancelled = True
        self._event.set()
        logger.info("[MCP-OAuth] 授权已被用户中止")

    def close(self) -> None:
        """关闭 server 并回收线程。幂等。

        同时清空回调结果与 event：一次性实例，旧回调（code/state）残留
        会误导复用方读出过期 state（apify state mismatch 事故根因之一）。
        """
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        self.result = CallbackResult()
        self._event.clear()
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception as e:  # pragma: no cover - 关闭路径容错
                logger.debug(f"[MCP-OAuth] 关闭回调 server 异常: {e}")
        if thread is not None:
            thread.join(timeout=2)
