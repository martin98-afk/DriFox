# -*- coding: utf-8 -*-
"""登录/绑定「可中止」回归测试。

覆盖三处 OAuth 入口的中止能力（用户痛点：关掉授权页后只能干等超时）：
1. 服务商编辑卡：登录中按钮变「中止」可点；中止后陈旧结果被丢弃
2. Gitee 绑定：cancel_event 透传到 run_authorization_code_flow，置位即退出等待
3. 认证模块：buddy_auth / copilot_auth 的轮询在 cancel_event 置位后抛 LoginCancelled

关键约束：这些等待都在后台线程，中止必须是「置标记 + 唤醒」而非 UI 线程阻塞关闭。
"""

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

try:  # 双栈兼容：dev 分支是 PyQt5，pyside6 分支是 PySide6
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - 仅 PyQt5 环境
    from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication(sys.argv)


# ═══════════════════════════════════════════════════
# 服务商编辑卡：登录可中止
# ═══════════════════════════════════════════════════


def _make_edit_card():
    """构造最小可用的 ProviderEditCard（绕开重型 _init_ui）"""
    from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

    card = ProviderEditCard.__new__(ProviderEditCard)
    card._login_generation = 0
    card._login_running = False
    card._login_cancel = None
    card.loginBtn = MagicMock()
    card.window = MagicMock(return_value=MagicMock())
    return card


class TestProviderLoginCancel:
    def test_hook_accepts_cancel_detects_kwarg(self):
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        def hook(cancel_event=None):
            return {}

        assert ProviderEditCard._hook_accepts_cancel(hook) is True

    def test_hook_accepts_cancel_rejects_plain_callable(self):
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        assert ProviderEditCard._hook_accepts_cancel(lambda: {}) is False

    def test_hook_accepts_cancel_supports_kwargs(self):
        """**kwargs 形式的钩子同样能接收 cancel_event"""
        from app.widgets.cards.settings.provider_edit_card import ProviderEditCard

        def hook(**kwargs):
            return {}

        assert ProviderEditCard._hook_accepts_cancel(hook) is True

    def test_old_hook_called_without_args(self):
        """不支持 cancel_event 的旧钩子必须按无参调用，不能因签名报错"""
        card = _make_edit_card()
        called = {"n": 0}

        def hook():
            called["n"] += 1
            return {"api_key": "k"}

        card.loginSuccess = MagicMock()
        card.loginFailed = MagicMock()
        card._do_login_thread(hook, 0, threading.Event())

        assert called["n"] == 1
        card.loginSuccess.emit.assert_called_once()

    def test_cancel_event_passed_to_supporting_hook(self):
        card = _make_edit_card()
        seen = {}

        def hook(cancel_event=None):
            seen["event"] = cancel_event
            return {"api_key": "k"}

        card.loginSuccess = MagicMock()
        card.loginFailed = MagicMock()
        ev = threading.Event()
        card._do_login_thread(hook, 0, ev)

        assert seen["event"] is ev

    def test_stale_result_discarded_after_abort(self):
        """中止后旧线程回来：结果丢弃，不再弹成功提示"""
        card = _make_edit_card()
        card.loginSuccess = MagicMock()
        card.loginFailed = MagicMock()
        card._login_generation = 5  # 用户已中止（中止时自增）

        card._do_login_thread(lambda: {"api_key": "k"}, 3, threading.Event())

        card.loginSuccess.emit.assert_not_called()
        card.loginFailed.emit.assert_not_called()

    def test_abort_sets_event_and_resets_button(self):
        card = _make_edit_card()
        ev = threading.Event()
        card._login_cancel = ev
        card._login_running = True

        with patch("qfluentwidgets.InfoBar"):
            card._abort_login()

        assert ev.is_set() is True
        assert card._login_running is False
        card.loginBtn.setText.assert_called_with("登录")


# ═══════════════════════════════════════════════════
# 通用授权码流程：cancel_event 立即退出
# ═══════════════════════════════════════════════════


class TestAuthorizationCodeFlowCancel:
    def test_cancel_returns_immediately(self):
        """置位后 0.5s 内返回「授权已中止」，不必等满 timeout"""
        from app.gateway.auth.base import CANCEL_MESSAGE, OAuthAppConfig, run_authorization_code_flow

        app_cfg = OAuthAppConfig(
            client_id="cid",
            client_secret="csec",
            authorize_url="https://example.invalid/authorize",
            token_url="https://example.invalid/token",
            redirect_port=0,  # 端口 0 由系统分配，避免测试抢占固定端口
        )
        cancel = threading.Event()
        cancel.set()

        with patch("app.gateway.auth.base.webbrowser.open"):
            started = time.monotonic()
            token_data, err = run_authorization_code_flow(app_cfg, timeout=60, cancel_event=cancel)
            elapsed = time.monotonic() - started

        assert token_data is None
        assert err == CANCEL_MESSAGE
        assert elapsed < 5, f"中止应立即返回，实际耗时 {elapsed:.1f}s"


# ═══════════════════════════════════════════════════
# 认证模块：轮询可中断
# ═══════════════════════════════════════════════════


class TestBuddyAuthCancel:
    def _module(self):
        import importlib.util
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent.parent
        path = repo / "plugins" / "system-providers" / "providers" / "buddy_auth.py"
        spec = importlib.util.spec_from_file_location("buddy_auth_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_sleep_or_cancel_raises_when_set(self):
        mod = self._module()
        ev = threading.Event()
        ev.set()
        with pytest.raises(mod.LoginCancelled):
            mod._sleep_or_cancel(10, ev)

    def test_login_raises_cancel_during_poll(self):
        mod = self._module()
        ev = threading.Event()
        state_sent = {"done": False}

        def fake_request(method, path, **kwargs):
            if path.startswith(mod.AUTH_STATE_PATH):
                # auth/state 需返回可用的 state 与 authUrl，否则流程走不到轮询
                return {"code": 0, "data": {"state": "s", "authUrl": "https://example.invalid/a"}}
            # token 轮询：首轮请求就模拟用户点中上
            state_sent["done"] = True
            ev.set()
            return {"code": mod.CODE_TOKEN_NOT_READY}

        with patch.object(mod, "_request", side_effect=fake_request):
            with pytest.raises(mod.LoginCancelled):
                mod.login(timeout_sec=30, poll_interval=0.01, open_browser=False, cancel_event=ev)

        assert state_sent["done"] is True, "未走到 token 轮询阶段"


class TestCopilotAuthCancel:
    def _module(self):
        import importlib.util
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent.parent
        path = repo / "plugins" / "system-providers" / "providers" / "copilot_auth.py"
        spec = importlib.util.spec_from_file_location("copilot_auth_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_poll_raises_cancel(self):
        mod = self._module()
        ev = threading.Event()
        ev.set()
        with pytest.raises(mod.LoginCancelled):
            mod.poll_access_token("dev-code", timeout_sec=30, cancel_event=ev)
