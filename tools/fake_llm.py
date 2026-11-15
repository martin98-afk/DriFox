#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OpenAI 兼容的本地假 LLM 服务（冒烟/压测专用，零出网）。

用途
----
场景 F（窗口级会话开关）需要一个可控、零延迟抖动的消息源，避免真实模型
影响判据（心跳 / RSS）。把隔离数据目录的 ``llm_saved_providers`` 指向本服务
即可：窗口引擎与 provider 逻辑零改动，出网为零。

用法
----
    python tools/fake_llm.py                       # 默认 30 chunk、间隔 30ms
    python tools/fake_llm.py --chunks 40 --interval 0.03 --port 18081

启动后打印一行::

    FAKE_LLM_BASE_URL=http://127.0.0.1:<port>/v1

把该 URL 填进 provider 的 ``base_url``，``api_key`` 任意非空字符串。
端点：
- ``POST /v1/chat/completions``：``stream=true`` 走 SSE，否则返回完整 JSON
- ``GET  /v1/models``：返回单个模型 ``fake-llm``
- ``GET  /health``：健康检查
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

_MODEL = "fake-llm"

# 短文本池：固定句子保证输出稳定，便于按 chunk 数校验渲染。
_PHRASES = (
    "性能压测占位文本。",
    "这是一条来自本地假模型的消息。",
    "会话开关场景需要稳定的流式输出。",
    "没有网络请求，也没有真实 token 消耗。",
    "流式分块用于观察窗口渲染与内存曲线。",
)


def _build_text(chunks: int) -> str:
    parts = []
    for i in range(max(1, chunks)):
        parts.append(_PHRASES[i % len(_PHRASES)])
    return "".join(parts)


def _split_chunks(text: str, chunks: int) -> list[str]:
    """把文本切成 chunks 份（尽量均分，保证非空）。"""
    n = max(1, chunks)
    size = max(1, len(text) // n)
    out = [text[i:i + size] for i in range(0, len(text), size)]
    return out[:n] if len(out) >= n else out


class _Handler(BaseHTTPRequestHandler):
    chunks: int = 30
    interval: float = 0.03

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002,N802
        pass

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/health", ""):
            self._json(200, {"ok": True, "service": "fake-llm"})
            return
        if path == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [{"id": _MODEL, "object": "model", "owned_by": "local"}],
                },
            )
            return
        self._json(404, {"error": {"message": f"not found: {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/v1/chat/completions":
            self._json(404, {"error": {"message": f"not found: {path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as exc:  # noqa: BLE001 — 假服务不做严格校验
            self._json(400, {"error": {"message": f"bad request: {exc}"}})
            return

        text = _build_text(self.chunks)
        if not req.get("stream"):
            self._json(
                200,
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": _MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": len(text), "total_tokens": len(text) + 1},
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        base = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": _MODEL,
        }

        def _send(delta: Dict[str, Any], finish: Any = None) -> None:
            payload = dict(base)
            payload["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
            self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            _send({"role": "assistant"})
            for piece in _split_chunks(text, self.chunks):
                time.sleep(max(0.0, self.interval))
                _send({"content": piece})
            _send({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return  # 客户端提前断开：正常退出


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenAI 兼容假 LLM（SSE）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = 随机端口")
    parser.add_argument("--chunks", type=int, default=30, help="流式分块数（20-50）")
    parser.add_argument("--interval", type=float, default=0.03, help="chunk 间隔秒")
    args = parser.parse_args(argv)

    _Handler.chunks = max(1, int(args.chunks))
    _Handler.interval = max(0.0, float(args.interval))
    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    port = httpd.server_address[1]
    print(f"FAKE_LLM_BASE_URL=http://{args.host}:{port}/v1", flush=True)
    print(f"[fake-llm] chunks={_Handler.chunks} interval={_Handler.interval}s", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
