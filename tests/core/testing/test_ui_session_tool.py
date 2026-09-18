# -*- coding: utf-8 -*-
"""ui_session 二批工具单测（秒级，mock bus/主窗口，无 QApplication）。"""

from __future__ import annotations

import pytest

from tools.ui_test_server.tokens import TokenStore


@pytest.fixture()
def srv(monkeypatch, tmp_path):
    """加载 server 模块：invoke 直跑（无 Qt）、隔离数据目录、清 token 表、ARM 置位。"""
    monkeypatch.setenv("DRIFOX_DATA_DIR", str(tmp_path))
    import tools.ui_test_server.server as srv_mod

    monkeypatch.setattr(srv_mod, "invoke", lambda fn, timeout_ms=15000: fn())
    srv_mod._tokens.clear()
    from tools.ui_driver import setArmed

    setArmed(True)
    yield srv_mod
    setArmed(False)


@pytest.fixture()
def fake_mw(monkeypatch):
    """伪造主窗口：session_manager / history_manager / load 链全 mock。"""
    from types import SimpleNamespace

    calls: dict = {"loaded": None}
    sess = SimpleNamespace(session_id="sess-aaaa-1111", messages=[])
    mw = SimpleNamespace()
    mw.session_manager = SimpleNamespace(
        create_new_session=lambda: sess,
        get_current_session=lambda: sess,
    )
    mw.history_manager = SimpleNamespace(
        get_session_by_session_id=lambda sid: {"session_id": sid, "title": "t"} if sid == "known" else None
    )
    mw._display_current_session = lambda: None

    def _load(record):
        calls["loaded"] = record.get("session_id")

    mw._load_session_from_record = _load
    mw.calls = calls
    monkeypatch.setattr(
        "app.core.infra.window_registry.alive_window_instances",
        lambda: [mw],
    )
    return mw


def _issue_token(srv, object_name: str, expired: bool = False) -> str:
    token, _ = srv._tokens.issue(object_name)
    if expired:
        srv._tokens._store[token]["expires"] = 0.0
    return token


def test_new_action_no_token(srv, fake_mw):
    out = srv._tool_ui_session(action="new")
    assert out["ok"] is True and out["action"] == "new"


def test_load_without_token_rejected(srv, fake_mw):
    out = srv._tool_ui_session(action="load", session_id="known")
    assert out["ok"] is False and out["blocked_by"] == "missing_token"


def test_load_expired_token_rejected(srv, fake_mw):
    tok = _issue_token(srv, "known", expired=True)
    out = srv._tool_ui_session(action="load", session_id="known", confirm_token=tok)
    assert out["ok"] is False and out["blocked_by"] == "expired_token"


def test_load_wrong_binding_rejected(srv, fake_mw):
    tok = _issue_token(srv, "other")
    out = srv._tool_ui_session(action="load", session_id="known", confirm_token=tok)
    assert out["ok"] is False and out["blocked_by"] == "token_mismatch"


def test_load_valid_token_calls_load_chain(srv, fake_mw):
    tok = _issue_token(srv, "known")
    out = srv._tool_ui_session(action="load", session_id="known", confirm_token=tok)
    assert out["ok"] is True and fake_mw.calls["loaded"] == "known"
    # 一次性：消费即作废
    out2 = srv._tool_ui_session(action="load", session_id="known", confirm_token=tok)
    assert out2["ok"] is False and out2["blocked_by"] == "missing_token"


def test_ui_memory_shape(srv, fake_mw):
    from tools.ui_driver import observe

    snap = observe.memory()
    assert "main_private_mb" in snap and "containers" in snap
