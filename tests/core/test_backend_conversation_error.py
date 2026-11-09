# -*- coding: utf-8 -*-
"""conversation_error 结构化报错事件：分类 / payload 组装 / error 分支双信号。

背景：能力池 stream.errors 需要结构化报错（窗口/服务商/模型/错误类型），
backend error 转发点在旧 error_occurred(str) 之外补发 conversation_error(dict)。
"""
from unittest.mock import MagicMock

from app.core.conversation.backend import ChatBackend, _classify_error_text


def test_classify_error_text():
    assert _classify_error_text("Request timed out after 30s") == "timeout"
    assert _classify_error_text("HTTP 429 Too Many Requests") == "rate_limit"
    assert _classify_error_text("401 Unauthorized: invalid api key") == "auth"
    assert _classify_error_text("Connection refused by peer") == "network"
    assert _classify_error_text("服务器开小差了") == "unknown"
    assert _classify_error_text("") == "unknown"


class _Session:
    session_id = "sess-1"


def _make_backend(window_id="win_err_1", model_config=None, session=None):
    backend = ChatBackend(window_id=window_id)
    backend._session_manager = MagicMock()
    backend._session_manager.get_current_session.return_value = session
    backend._get_model_config = model_config if model_config is not None else None
    return backend


def test_payload_fields(qapp):
    backend = _make_backend(
        model_config=lambda: {
            "display_name": "DeepSeek 官方",
            "provider_name": "deepseek",
            "模型名称": "deepseek-chat",
            "API_KEY": "sk-secret",
        },
        session=_Session(),
    )
    payload = backend._build_conversation_error_payload("Request timeout")
    assert payload["window_id"] == "win_err_1"
    assert payload["session_id"] == "sess-1"
    assert payload["provider"] == "DeepSeek 官方"
    assert payload["model"] == "deepseek-chat"
    assert payload["message"] == "Request timeout"
    assert payload["error_type"] == "timeout"
    assert isinstance(payload["timestamp"], float)
    assert "API_KEY" not in payload


def test_payload_degrades_without_config_and_session(qapp):
    backend = _make_backend(model_config=None, session=None)
    payload = backend._build_conversation_error_payload("boom")
    assert payload["provider"] == ""
    assert payload["model"] == ""
    assert payload["session_id"] == ""
    assert payload["error_type"] == "unknown"


def test_error_branch_emits_both_signals(qapp):
    backend = _make_backend(model_config=lambda: {"provider_name": "P", "模型名称": "m"})
    old, new = [], []
    backend.error_occurred.connect(old.append)
    backend.conversation_error.connect(new.append)
    ui_cb = MagicMock()
    backend.set_callback("error", ui_cb)
    # 引擎未创建时 wrapped 回调暂存于 _pending_engine_callbacks，直接取出触发
    wrapped = backend._pending_engine_callbacks["error"]
    wrapped("Connection reset by peer")
    ui_cb.assert_called_once_with("Connection reset by peer")
    assert old == ["Connection reset by peer"]
    assert len(new) == 1
    assert new[0]["error_type"] == "network"
    assert new[0]["provider"] == "P"
    assert new[0]["model"] == "m"
