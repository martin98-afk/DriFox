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
        # "known" 为 load 链路用例的固定会话；sess-aaaa-1111 为 new 创建的会话
        # （真实场景 create_new_session 会落历史登记，switch 可查）
        get_session_by_session_id=lambda sid: {"session_id": sid, "title": "t"}
        if sid in ("known", "sess-aaaa-1111") else None
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

def test_new_returns_full_session_id_usable_for_switch(srv, fake_mw):
    """[T34] new 返回完整 session_id（>8 位）且可直接用于 switch——
    旧实现返回 [:8] 截断 ID，new→switch 自动化序列恒失败。"""
    out = srv._tool_ui_session(action="new")
    assert out["ok"] is True
    assert len(out["session_id"]) > 8
    assert out["session_id"] == "sess-aaaa-1111"
    assert out["session_id_short"] == out["session_id"][:8]
    # 直接把 new 返回的完整 ID 交给 switch，必须成功
    out2 = srv._tool_ui_session(action="switch", session_id=out["session_id"])
    assert out2["ok"] is True
    assert out2["session_id"] == out["session_id"]
    assert out2["session_id_short"] == out["session_id"][:8]


def test_load_without_token_allowed(srv, fake_mw):
    # [M2-r 简化] 测试服务免 token（三重闸已含准入控制）
    out = srv._tool_ui_session(action="load", session_id="known")
    assert out["ok"] is True


def test_load_token_field_ignored(srv, fake_mw):
    tok = _issue_token(srv, "known", expired=True)
    out = srv._tool_ui_session(action="load", session_id="known", confirm_token=tok)
    assert out["ok"] is True


def test_load_wrong_binding_still_works(srv, fake_mw):
    tok = _issue_token(srv, "other")
    out = srv._tool_ui_session(action="load", session_id="known", confirm_token=tok)
    assert out["ok"] is True


def test_load_valid_token_calls_load_chain(srv, fake_mw):
    tok = _issue_token(srv, "known")
    out = srv._tool_ui_session(action="load", session_id="known", confirm_token=tok)
    assert out["ok"] is True and fake_mw.calls["loaded"] == "known"
    # 免 token 后可重复 load（幂等）
    out2 = srv._tool_ui_session(action="load", session_id="known", confirm_token=tok)
    assert out2["ok"] is True


def test_ui_memory_shape(srv, fake_mw, monkeypatch):
    from tools.ui_driver import observe
    from tools.ui_driver.bus import setArmed

    setArmed(True)
    monkeypatch.setattr(
        "app.core.infra.window_registry.alive_window_instances",
        lambda: [],
    )
    snap = observe.memory()
    assert "main_private_mb" in snap and "containers" in snap
