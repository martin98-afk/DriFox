# -*- coding: utf-8 -*-
"""ConfigSyncService 启动自恢复（resume_if_bound）测试。

背景（2026-11-03）：设置大卡改为懒构建、3500ms 预热被移除后，重启进程
只要不打开设置卡片，云同步就永不启动。本文件锁定启动期自动 enable 的契约：
已绑定 + 有有效 token → enable；未绑定 / 取 token 期间解绑 / 已在运行 → 不启动。
"""

from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QObject

from app.core.sync import config_sync as cs


class _FakeCfg:
    def __init__(self, bound=True, owner="u1"):
        self.gitee_bound = MagicMock(value=bound)
        self.gitee_user_owner = MagicMock(value=owner)


class _FakeThread:
    """不真正执行 target 的线程桩，避免后台线程与 Qt 信号时序互相干扰"""

    started: list = []

    def __init__(self, target=None, name=None, daemon=None):
        self.target = target
        _FakeThread.started.append(self)

    def start(self):
        pass


cfg_holder = [None]


@pytest.fixture
def svc(monkeypatch):
    _FakeThread.started = []
    monkeypatch.setattr(cs.threading, "Thread", _FakeThread)
    monkeypatch.setattr(cs.Settings, "get_instance", classmethod(lambda cls: cfg_holder[0]))

    cfg_holder[0] = _FakeCfg()
    s = cs.ConfigSyncService.__new__(cs.ConfigSyncService)
    QObject.__init__(s)
    s._state = "disabled"
    s._resume_pending = False
    s.enable = MagicMock()
    s.fake_cfg = cfg_holder[0]
    return s


def test_unbound_skips(svc):
    svc.fake_cfg.gitee_bound.value = False
    svc.resume_if_bound()
    assert _FakeThread.started == []
    assert svc._resume_pending is False
    svc.enable.assert_not_called()


def test_bound_starts_background_fetch(svc):
    svc.resume_if_bound()
    assert len(_FakeThread.started) == 1
    assert _FakeThread.started[0].target == svc._resume_fetch_token
    assert svc._resume_pending is True


def test_fetch_emits_token(svc, monkeypatch):
    got = []
    svc._resumeTokenReady.connect(lambda t, o: got.append((t, o)))
    backend = MagicMock()
    backend.get_bound_info.return_value = {"token": "tok", "owner": "own"}
    monkeypatch.setattr("app.core.auth.get_oauth_backend", lambda _name: backend)

    svc._resume_fetch_token()

    assert got == [("tok", "own")]


def test_token_ready_enables_sync(svc):
    svc._resume_pending = True
    svc._on_resume_token_ready("tok", "own")
    svc.enable.assert_called_once_with("tok", "own")
    assert svc._resume_pending is False


def test_empty_token_no_enable(svc):
    svc._resume_pending = True
    svc._on_resume_token_ready("", "")
    svc.enable.assert_not_called()
    assert svc._resume_pending is False


def test_unbound_during_fetch_no_enable(svc):
    """取 token 期间用户解绑：不得再把同步拉起来"""
    svc._resume_pending = True
    svc.fake_cfg.gitee_bound.value = False
    svc._on_resume_token_ready("tok", "own")
    svc.enable.assert_not_called()


def test_already_running_ignored(svc):
    svc._state = "syncing"
    svc.resume_if_bound()
    assert _FakeThread.started == []
    svc._on_resume_token_ready("tok", "own")
    svc.enable.assert_not_called()
