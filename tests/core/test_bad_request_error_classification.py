# -*- coding: utf-8 -*-
"""400 错误归类：服务端「不认字段」不得被误报成「参数过长」。

实测 2026-11-19（Gemini OpenAI 兼容层）：把 OpenAI 专有的 frequency_penalty
发给 generateContent，上游返回
    Invalid JSON payload received. Unknown name "frequency_penalty": Cannot find field
归因链用 "payload" 关键词判「参数过长」，把用户引向「拆工具参数、分批写入」的
错误方向。本组用例锁住：未知字段优先命中「参数不支持」，真正的超限消息不受影响。
"""

import httpx
import pytest

from app.core.workers.chat_worker import OpenAIChatWorker


class _SignalStub:
    def __init__(self):
        self.emitted = []

    def emit(self, *args, **kwargs):
        self.emitted.append(args)


def _make_worker(monkeypatch):
    """只测 _handle_error 文案分派：剥离健康上报链路。"""
    worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
    worker.error_occurred = _SignalStub()
    worker._emit_with_callback = lambda _name, signal, msg: signal.emit(msg)
    monkeypatch.setattr(worker, "_classify_error_for_health", lambda error: None, raising=False)
    monkeypatch.setattr(worker, "_report_health", lambda status: None, raising=False)
    return worker


def _make_bad_request(message: str):
    from openai import BadRequestError

    return BadRequestError(
        message,
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.example/v1/chat/completions")),
        body=None,
    )


def test_unknown_field_reports_unsupported_param(monkeypatch):
    """Google protobuf 未知字段：文案必须是「参数不支持」，且不得提「参数过长」。"""
    worker = _make_worker(monkeypatch)
    worker._handle_error(
        _make_bad_request(
            "Error code: 400 - [{'error': {'code': 400, 'message': 'Invalid JSON payload received. "
            "Unknown name \"frequency_penalty\": Cannot find field.', 'status': 'INVALID_ARGUMENT'}}]"
        )
    )
    assert worker.error_occurred.emitted, "必须发出错误文案"
    message = worker.error_occurred.emitted[0][0]
    assert message.startswith("[参数不支持]"), message
    assert "参数过长" not in message
    assert "frequency_penalty" in message  # 原始错误保留，用户能自查字段名


def test_cannot_find_field_reports_unsupported_param(monkeypatch):
    """同类变体（只带 Cannot find field）同样归入不支持。"""
    worker = _make_worker(monkeypatch)
    worker._handle_error(_make_bad_request('Error code: 400 - Unknown field "thinking_budget"'))
    message = worker.error_occurred.emitted[0][0]
    assert message.startswith("[参数不支持]"), message


def test_genuine_payload_too_large_still_reports_too_long(monkeypatch):
    """真正的超限消息（无未知字段特征）仍走「参数过长」，防回归。"""
    worker = _make_worker(monkeypatch)
    worker._handle_error(_make_bad_request("Error code: 400 - Request payload too large: 413"))
    message = worker.error_occurred.emitted[0][0]
    assert message.startswith("[参数过长]"), message
