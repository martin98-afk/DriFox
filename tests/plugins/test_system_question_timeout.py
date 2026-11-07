# -*- coding: utf-8 -*-
"""system-question 超时控制器测试：策略读取 / 跳过与 AI 代答文本 / 计时重置 / 事件驱动

不启真实 QTimer 与网络：控制器实例直接用 __new__ 构造绕过 __init__ 的 Qt 依赖，
_tick 语义靠手调 _on_tick / _on_question_event 验证。
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_NAME = "ui_plugin_system_question.timeout"
_TIMEOUT_PATH = _REPO_ROOT / "plugins" / "system-question" / "ui" / "timeout.py"


def _load_timeout_module():
    """按 ui loader 同款方式加载 timeout.py（无相对导入，可独立加载）"""
    for name in list(sys.modules):
        if name == _MODULE_NAME or name.startswith(_MODULE_NAME + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _TIMEOUT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeAPI:
    """question.control 能力替身：记录调用"""

    def __init__(self, snapshot: dict):
        self.calls: list = []
        self._snapshot = dict(snapshot)

    def __getitem__(self, name):
        def _fn(*args):
            self.calls.append((name, args))
            if name == "snapshot":
                return dict(self._snapshot)
            return True

        return _fn

    def __contains__(self, name):
        return True


class _FakePool:
    def __init__(self, api):
        self._api = api

    def __getitem__(self, cap_id):
        assert cap_id == "question.control"
        return self._api

    def __contains__(self, cap_id):
        return cap_id == "question.control"


class _Controller:
    """把真实控制器的方法绑定到最小状态（不跑 QTimer）"""

    def __init__(self, mod, action: str, sec: int = 30, model: str = ""):
        self.mod = mod
        self.ctrl = mod.QuestionTimeoutController.__new__(mod.QuestionTimeoutController)
        self.ctrl._sessions = {}
        self.ctrl._ai_pending = set()
        self.ctrl._aiResult = type("Sig", (), {"emit": lambda self, *a: None, "connect": lambda self, *a: None})()
        self.action = action
        self.sec = sec
        self.model = model
        # 测试不跑真实窗口扫描：_on_tick 里的 _ensure_subscriptions 会把
        # 没有对应主窗口的 session 当作已关窗口清理掉
        mod.QuestionTimeoutController._ensure_subscriptions = lambda self: None
        mod.QuestionTimeoutController._read_policy = staticmethod(
            lambda: {"action": self.action, "sec": self.sec, "model": self.model}
        )

    def open_question(self, window_id="w1", snapshot=None, pool=None):
        api = _FakeAPI(snapshot or {"tool_call_id": "q1", "questions": [{"question": "选哪个"}], "answers": {}})
        pool = pool or _FakePool(api)
        session = self.mod._WindowSession(window_id, pool)
        self.ctrl._sessions[window_id] = session
        return session, api


def test_skip_text_marks_unanswered_questions():
    mod = _load_timeout_module()
    snap = {
        "questions": [{"question": "城市"}, {"question": "预算"}],
        "answers": {0: "北京"},
        "index": 1,
        "total": 2,
    }
    text = mod.QuestionTimeoutController._build_skip_text(snap, 120)
    assert "问题「城市」的回答：\n北京" in text
    assert "问题「预算」的回答：\n（用户未在 120 秒内回答，已跳过）" in text


def test_ai_text_prefers_user_answer_then_ai_then_skip():
    mod = _load_timeout_module()
    snap = {
        "questions": [{"question": "a"}, {"question": "b"}, {"question": "c"}],
        "answers": {0: "用户答案"},
    }
    raw = '{"answers": [{"index": 1, "answer": "AI 答案"}]}'
    text = mod.QuestionTimeoutController._build_ai_text(snap, raw, 60)
    assert "问题「a」的回答：\n用户答案" in text
    assert "问题「b」的回答：\n[AI 代答] AI 答案" in text
    assert "问题「c」的回答：\n（用户未在 60 秒内回答，已跳过）" in text


def test_ai_text_invalid_json_falls_back_to_skip():
    mod = _load_timeout_module()
    snap = {"questions": [{"question": "a"}], "answers": {}}
    text = mod.QuestionTimeoutController._build_ai_text(snap, "不是 JSON", 30)
    assert "（用户未在 30 秒内回答，已跳过）" in text


def test_action_none_arms_nothing():
    mod = _load_timeout_module()
    ctx = _Controller(mod, action="none")
    session, api = ctx.open_question()
    ctx.ctrl._on_question_event("w1", "asked", {"tool_call_id": "q1"})
    assert session.deadline is None
    assert not any(name == "countdown" for name, _ in api.calls)


def test_asked_arms_deadline_and_countdown():
    mod = _load_timeout_module()
    ctx = _Controller(mod, action="skip", sec=45)
    session, api = ctx.open_question()
    ctx.ctrl._on_question_event("w1", "asked", {"tool_call_id": "q1"})
    assert session.deadline is not None
    assert ("countdown", (45,)) in api.calls


def test_page_event_resets_deadline():
    mod = _load_timeout_module()
    ctx = _Controller(mod, action="skip", sec=45)
    session, api = ctx.open_question()
    ctx.ctrl._on_question_event("w1", "asked", {"tool_call_id": "q1"})
    first = session.deadline
    ctx.ctrl._on_question_event("w1", "page", {"index": 1, "total": 2})
    assert session.deadline is not None and session.deadline >= first
    assert sum(1 for name, _ in api.calls if name == "countdown") == 2


def test_closed_stops_countdown_and_clears_deadline():
    mod = _load_timeout_module()
    ctx = _Controller(mod, action="skip", sec=45)
    session, api = ctx.open_question()
    ctx.ctrl._on_question_event("w1", "asked", {"tool_call_id": "q1"})
    ctx.ctrl._on_question_event("w1", "closed", {"reason": "answered"})
    assert session.deadline is None
    assert session.tool_call_id == ""
    assert any(name == "stop_countdown" for name, _ in api.calls)


def test_timeout_fires_skip_answer():
    mod = _load_timeout_module()
    ctx = _Controller(mod, action="skip", sec=10)
    session, api = ctx.open_question(
        snapshot={"tool_call_id": "q1", "questions": [{"question": "选哪个"}], "answers": {}}
    )
    session.deadline = 0  # 立即到期
    ctx.ctrl._on_tick()
    answers = [args[0] for name, args in api.calls if name == "answer"]
    assert answers and "已跳过" in answers[0]


def test_timeout_skips_when_question_already_closed():
    mod = _load_timeout_module()
    ctx = _Controller(mod, action="skip", sec=10)
    session, api = ctx.open_question(snapshot={"questions": [], "answers": {}})
    session.deadline = 0
    ctx.ctrl._on_tick()
    assert not any(name == "answer" for name, _ in api.calls)


def test_ai_failure_submits_skip_text(monkeypatch):
    mod = _load_timeout_module()
    ctx = _Controller(mod, action="ai", sec=30)
    session, api = ctx.open_question(
        snapshot={"tool_call_id": "q1", "questions": [{"question": "选哪个"}], "answers": {}}
    )
    session.tool_call_id = "q1"
    session.deadline = 0
    # 让后台线程的模型调用直接返回空（模拟失败）
    monkeypatch.setattr(mod.QuestionTimeoutController, "_ask_model", staticmethod(lambda *a, **k: ""))
    # 同步跑：直接调用结果落点，绕过线程调度
    ctx.ctrl._on_ai_result("w1", "")
    answers = [args[0] for name, args in api.calls if name == "answer"]
    assert answers and "已跳过" in answers[0]
