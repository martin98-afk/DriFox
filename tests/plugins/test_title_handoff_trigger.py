# -*- coding: utf-8 -*-
"""交接自动继续（_hook_event="SessionSend"）的标题链路回归。

背景：update_handoff 的交接三连里，继续指令经 session.send 入队时默认带
_hook_event="SessionSend"（ui_core._send_with_mode）。宿主 _publish_title_request
的「首条真实用户消息」白名单原本只放 TeamMail，交接轮 len(user_messages)==0
直接 return → title.request 永不发布 → 新会话停在占位名，直到用户下一句话把它
改写成跟原任务无关的标题（实测标题变成「顺便把测试也补上」）。

修法双轨：
- 交接侧 session.new 直接携带源会话主题，不依赖模型（_handoff_title）；
- 宿主侧放行 SessionSend 并剥离「继续完成「…」会话的交接任务」固定句式，
  兜住定时器触发等其它经 session.send 驱动的系统轮次。
"""
import types

import pytest

from app.main_widget import OpenAIChatToolWindow

_HANDOFF_MSG = (
    "继续完成「排查会话标题未生成的根因」会话的交接任务。\n"
    "根据交接文档继续未完成的工作。从「剩余任务与下一步」的第一项开始执行。"
)


def _host(messages, *, topic_summary="", session_id="s-1"):
    session = types.SimpleNamespace(
        user_edited_title=False,
        topic_summary=topic_summary,
        is_temp=False,
        messages=messages,
        session_id=session_id,
    )
    return types.SimpleNamespace(
        session_manager=types.SimpleNamespace(get_current_session=lambda: session),
        _window_id="w-1",
        backend=None,
    )


def _capture(monkeypatch, host):
    """跑 _publish_title_request，返回 (events, commits)。"""
    events, commits = [], []

    from app.core.infra import ui_event_bus

    monkeypatch.setattr(
        ui_event_bus.UIEventBus,
        "publish",
        lambda self, event, **payload: events.append((event, payload)),
    )

    import app.plugins.capabilities.resolve as resolve

    monkeypatch.setattr(
        resolve,
        "pool_for",
        lambda plugin, surface="", host=None: {
            "session.title.commit": lambda wid, title: commits.append((wid, title))
        },
    )
    OpenAIChatToolWindow._publish_title_request(host)
    return events, commits


def test_handoff_turn_publishes_and_strips_prefix(monkeypatch):
    """交接轮触发标题，fallback 取「」里的原会话主题而非整段固定句式。"""
    events, _ = _capture(
        monkeypatch, _host([{"role": "user", "content": _HANDOFF_MSG, "_hook_event": "SessionSend"}])
    )
    assert len(events) == 1
    assert events[0][0] == "title.request"
    assert events[0][1]["fallback"] == "排查会话标题未生成的根因"


def test_handoff_short_title_commits_directly(monkeypatch):
    """剥离前缀后 ≤10 字直接提交，不再调模型。"""
    msg = "继续完成「修复登录」会话的交接任务。\n根据交接文档继续未完成的工作。"
    events, commits = _capture(
        monkeypatch, _host([{"role": "user", "content": msg, "_hook_event": "SessionSend"}])
    )
    assert events == []
    assert commits == [("w-1", "修复登录")]


def test_context_hook_does_not_count_as_user_message(monkeypatch):
    """SessionStart 之类上下文注入仍不算用户发言，交接轮依旧是首问。"""
    events, _ = _capture(
        monkeypatch,
        _host(
            [
                {"role": "user", "content": "交接文档正文", "_hook_event": "SessionStart"},
                {"role": "user", "content": _HANDOFF_MSG, "_hook_event": "SessionSend"},
            ]
        ),
    )
    assert len(events) == 1
    assert events[0][1]["fallback"] == "排查会话标题未生成的根因"


# ═══════════════════════════════════════════════════════════════
# 交接侧：session.new 携带标题
# ═══════════════════════════════════════════════════════════════


def _load_handoff_tool():
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[2] / "plugins" / "system-handoff" / "tools" / "update_handoff.py"
    spec = importlib.util.spec_from_file_location("handoff_tool_title_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_arm(mod, monkeypatch, tmp_path, *, source_title, body):
    """跑 _arm_handoff，返回 [(cap_id, kwargs)]。"""
    calls = []

    def _accept(name):
        def _fn(**kw):
            calls.append((name, kw))
            return True

        return _fn

    pool = {
        "state.kv": {"get": lambda k, d=None: None, "set": lambda k, v: None, "delete": lambda k: None},
        "session.read": lambda: {"name": source_title},
        "session.interrupt": _accept("session.interrupt"),
        "session.new": _accept("session.new"),
        "session.send": _accept("session.send"),
    }
    monkeypatch.setattr(mod, "_cap", lambda ctx, cap_id: pool.get(cap_id))
    mod._arm_handoff(
        {
            "workdir": str(tmp_path),
            "session_id": "s1",
            "env": {"app_data_dir": str(tmp_path)},
            "capabilities": pool,
        },
        tmp_path / "doc.md",
        body,
    )
    return calls


def test_new_session_carries_source_title(tmp_path, monkeypatch):
    """源会话标题可用时，新会话直接沿用它，不依赖模型再推一遍主题。"""
    mod = _load_handoff_tool()
    calls = _run_arm(
        mod, monkeypatch, tmp_path, source_title="排查标题生成链路", body="## 任务目标\nX"
    )
    by_cap = {c[0]: c[1] for c in calls}
    assert by_cap["session.new"]["title"] == "排查标题生成链路"
    assert "继续完成「排查标题生成链路」" in by_cap["session.send"]["text"]


@pytest.mark.parametrize("placeholder", ["", "新对话", "对话 11-15 15:14"])
def test_new_session_title_falls_back_to_goal_headline(tmp_path, monkeypatch, placeholder):
    """占位标题（含「对话 MM-DD HH:MM」）不算主题，回退交接正文的任务目标首行。"""
    mod = _load_handoff_tool()
    body = "## 任务目标\n发布 DriFox v0.6.11（GitHub Release + CI）\n\n## 已完成\n- x"
    calls = _run_arm(mod, monkeypatch, tmp_path, source_title=placeholder, body=body)
    by_cap = {c[0]: c[1] for c in calls}
    assert by_cap["session.new"]["title"] == "发布 DriFox v0.6.11（GitHub Release + CI）"[:40]


def test_new_session_title_empty_when_nothing_available(tmp_path, monkeypatch):
    """标题与任务目标都拿不到时传空串：宿主侧判空跳过，不写占位。"""
    mod = _load_handoff_tool()
    calls = _run_arm(mod, monkeypatch, tmp_path, source_title="", body="## 已完成\n- x")
    by_cap = {c[0]: c[1] for c in calls}
    assert by_cap["session.new"]["title"] == ""
