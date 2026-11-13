# -*- coding: utf-8 -*-
"""延迟发送收尾段的忙状态守卫（2026-11-13 子智能体注入半死态）

缺陷现场：子智能体完成注入撞上 worker 抢跑时，`send_message` 的 execute()
在 PreSendWorker **写进 session 之后**才拒绝（"Already streaming"）。旧代码
直接回滚 UI：摘卡 + `_current_assistant_card=None` + 按钮回发送态，而真实
worker 仍在跑 —— 正文被 `_on_content_received` 丢弃（无当前卡），工具块却
经 `_on_tool_call_started` 的 `_find_latest_assistant_card()` 兜底继续渲染，
用户看到「没有流式状态、卡片只动工具运行框、按钮不是停止」。

加固：写 session 之前复核 worker 真实状态，忙则摘卡后按繁忙策略重投。
"""

import app.utils.config as cfg_mod
from app.main_widget import OpenAIChatToolWindow


class _FakeCard:
    def __init__(self):
        self.deleted = False

    def deleteLater(self):
        self.deleted = True


class _FakeBehavior:
    value = "queue"


class _FakeSettings:
    busy_enter_behavior = _FakeBehavior()

    @classmethod
    def get_instance(cls):
        return cls()


def _make_widget(monkeypatch):
    w = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    w._is_destroyed = False
    w._is_streaming = True
    w._current_assistant_card = object()
    w._toggle_send_stop = lambda flag: setattr(w, "_stop_flag", flag)
    w.backend = type("B", (), {"chat_engine": type("E", (), {"is_streaming": True})()})()
    w._scheduled = []
    w._schedule_busy_send = lambda text, paths, behavior, preserve, hook_event=None: w._scheduled.append(
        (text, paths, behavior, preserve, hook_event)
    )
    monkeypatch.setattr(cfg_mod, "Settings", _FakeSettings)
    return w


def _call(w, assistant_card, user_card, ephemeral=False):
    OpenAIChatToolWindow._finish_deferred_send(
        w,
        user_text="子智能体任务完成: ✅ [build] ...",
        hook_event="SubAgentFinished",
        image_paths=None,
        input_attachments=None,
        raw_input_text=None,
        session=None,
        assistant_card=assistant_card,
        user_content=None,
        user_card=user_card,
        expect_session_id=None,
        ephemeral=ephemeral,
    )


def test_busy_worker_reroutes_instead_of_rolling_back(monkeypatch):
    """worker 真忙：摘占位卡 + 复位按钮 + 按繁忙策略重投（注入标记保留）"""
    w = _make_widget(monkeypatch)
    card, user_card = _FakeCard(), _FakeCard()

    _call(w, card, user_card)

    assert card.deleted is True
    assert user_card.deleted is True
    assert w._current_assistant_card is None
    assert w._is_streaming is False
    assert w._stop_flag is False
    assert w._scheduled == [("子智能体任务完成: ✅ [build] ...", [], "queue", True, "SubAgentFinished")]


def test_busy_worker_ephemeral_drops_without_requeue(monkeypatch):
    """ephemeral（交接自动继续）：撞忙只回滚，不进排队队列"""
    w = _make_widget(monkeypatch)
    card, user_card = _FakeCard(), _FakeCard()

    _call(w, card, user_card, ephemeral=True)

    assert card.deleted is True
    assert user_card.deleted is True
    assert w._is_streaming is False
    assert w._scheduled == []


def test_idle_engine_does_not_treat_ui_flag_as_busy(monkeypatch):
    """engine 不忙时不得因 _is_streaming=True（刚由 _on_send_clicked 置位）误判撞忙

    回归：_worker_actually_busy() 在 engine 缺失时回退 UI 标志，若守卫沿用它，
    每一次正常发送都会被重投成排队/插话。
    """
    w = _make_widget(monkeypatch)
    w.backend = type("B", (), {"chat_engine": type("E", (), {"is_streaming": False})()})()
    card, user_card = _FakeCard(), _FakeCard()

    # engine 不忙 → 不应提前 return（后续要碰 send_message，这里用必抛的
    # backend 证明已走到直发段而不是撞忙分支）
    w.backend = type(
        "B",
        (),
        {
            "chat_engine": type("E", (), {"is_streaming": False})(),
            "send_message_to_engine": lambda *a, **k: (_ for _ in ()).throw(AssertionError("已走到直发段")),
        },
    )()
    w.session_manager = None
    try:
        _call(w, card, user_card)
    except AssertionError as exc:
        assert "已走到直发段" in str(exc)
    assert w._scheduled == []
    assert card.deleted is False
