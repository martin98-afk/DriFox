# -*- coding: utf-8 -*-
"""宿主 _publish_title_request 的守卫与 fallback 提取回归。

覆盖范围承接自旧 test_topic_summary_fallback.py：跳过 _hook_event 非 TeamMail、
跳过未打标任务邮件、跳过 assistant、首条真实用户消息、15 字截断。
旧实现这些规则在已删除的旧 worker 回退函数里，收口后移到宿主。
"""
import types

import pytest

from app.main_widget import OpenAIChatToolWindow


def _user(content, **extra):
    msg = {"role": "user", "content": content}
    msg.update(extra)
    return msg


def _assistant(content):
    return {"role": "assistant", "content": content}


def _mail(content="📨 **来自 [leader@w0] 的任务邮件：**\n\n请完成登录功能", **extra):
    msg = {"role": "user", "content": content}
    msg.update(extra)
    return msg


def _host(messages, *, user_edited=False, topic_summary="", is_temp=False, session_id="s-1"):
    session = types.SimpleNamespace(
        user_edited_title=user_edited,
        topic_summary=topic_summary,
        is_temp=is_temp,
        messages=messages,
        session_id=session_id,
    )
    sm = types.SimpleNamespace(get_current_session=lambda: session)
    return types.SimpleNamespace(
        session_manager=sm,
        _window_id="w-1",
        backend=None,
    )


def _capture(monkeypatch, host, long_input=True):
    """跑 _publish_title_request，返回 (events, commits)。"""
    events = []
    commits = []

    from app.core.infra import ui_event_bus

    monkeypatch.setattr(
        ui_event_bus.UIEventBus,
        "publish",
        lambda self, event, **payload: events.append((event, payload)),
    )

    def fake_pool_for(plugin, surface="", host=None):
        return {"session.title.commit": lambda wid, title: commits.append((wid, title))}

    import app.plugins.capabilities.resolve as resolve

    monkeypatch.setattr(resolve, "pool_for", fake_pool_for)
    OpenAIChatToolWindow._publish_title_request(host)
    return events, commits


def test_publishes_first_real_user_question(monkeypatch):
    events, commits = _capture(
        monkeypatch, _host([_user("帮我排查登录接口 500 报错")])
    )
    assert commits == []
    assert len(events) == 1
    assert events[0][0] == "title.request"
    assert events[0][1]["fallback"] == "帮我排查登录接口 500 报错"
    assert events[0][1]["session_id"] == "s-1"
    assert events[0][1]["window_id"] == "w-1"


def test_skips_hook_event_non_team_mail(monkeypatch):
    events, _ = _capture(
        monkeypatch,
        _host([_mail(_hook_event="SessionStart"), _user("帮我设计这套数据库的表结构并给出索引建议")]),
    )
    assert len(events) == 1
    assert events[0][1]["fallback"] == "帮我设计这套数据库的表结构并给"


def test_untagged_mail_only_session_has_no_fallback(monkeypatch):
    """mail-only 会话首条是 📨 未打标邮件时，R3 防御跳过它，无可用标题 → 不触发。"""
    events, commits = _capture(
        monkeypatch, _host([_mail(_hook_event="TeamMail")])
    )
    assert events == []
    assert commits == []


def test_skips_untagged_mail_prefix(monkeypatch):
    """未打标 📨 前缀在 fallback 提取里被 R3 防御跳过，不作为标题。"""
    events, _ = _capture(
        monkeypatch, _host([_mail()])
    )
    # 单条消息时首问判据成立，但 R3 过滤后 first_text 为空 → 不发布、不提交。
    assert events == []


def test_skips_assistant_message(monkeypatch):
    events, _ = _capture(
        monkeypatch,
        _host([_assistant("好的，我来帮你。"), _user("请问怎么优化这个执行很慢的联表查询")]),
    )
    assert len(events) == 1
    assert events[0][1]["fallback"] == "请问怎么优化这个执行很慢的联表"


def test_fallback_truncated_to_15_chars(monkeypatch):
    long_question = "请帮我分析这份季度销售报表并生成一份完整的对比摘要"
    events, _ = _capture(monkeypatch, _host([_user(long_question)]))
    assert len(events) == 1
    assert events[0][1]["fallback"] == long_question[:15]


def test_short_input_commits_directly_no_event(monkeypatch):
    events, commits = _capture(monkeypatch, _host([_user("你好")]))
    assert events == []
    assert commits == [("w-1", "你好")]


def test_second_user_message_does_not_republish(monkeypatch):
    """首问判据：标题生成异步，完成前 topic_summary 仍为空，不得重复发起。"""
    events, _ = _capture(
        monkeypatch, _host([_user("第一个问题内容较长"), _user("第二个问题内容较长")])
    )
    assert events == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user_edited": True},
        {"topic_summary": "已有标题"},
        {"is_temp": True},
    ],
)
def test_guards_skip(monkeypatch, kwargs):
    events, commits = _capture(
        monkeypatch, _host([_user("帮我排查登录接口 500 报错")], **kwargs)
    )
    assert events == []
    assert commits == []


def test_multimodal_content_takes_text_blocks(monkeypatch):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image_url": "data:..."},
                {"type": "text", "text": "帮我看看这张架构图有什么问题"},
            ],
        }
    ]
    events, _ = _capture(monkeypatch, _host(messages))
    assert len(events) == 1
    assert events[0][1]["fallback"] == "帮我看看这张架构图有什么问题"
