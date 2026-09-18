# -*- coding: utf-8 -*-
"""消息身份（MessageIdentity）：结构往返、解析链、缓存、团队邮件发送者、API 不泄漏。"""

import pytest

from app.core.infra import message_identity as mi
from app.core.conversation.message_content import messages_to_api, normalize_message


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后清空身份缓存（避免跨用例污染）。"""
    mi.clear_cache()
    yield
    mi.clear_cache()


# ── 结构往返 ──────────────────────────────────────────────


def test_identity_to_dict_drops_empty_fields():
    assert mi.MessageIdentity(name="hanako").to_dict() == {"name": "hanako"}
    assert mi.MessageIdentity(name="hanako", avatar="/a.png").to_dict() == {
        "name": "hanako",
        "avatar": "/a.png",
    }
    assert mi.MessageIdentity().to_dict() == {}


def test_identity_from_dict_rejects_invalid():
    assert mi.MessageIdentity.from_dict(None) is None
    assert mi.MessageIdentity.from_dict("hanako") is None
    assert mi.MessageIdentity.from_dict({}) is None
    assert mi.MessageIdentity.from_dict({"name": "   "}) is None
    assert mi.MessageIdentity.from_dict({"name": "hanako"}) == mi.MessageIdentity(name="hanako")


# ── 解析链 ────────────────────────────────────────────────


def test_default_identity_assistant_and_user():
    assistant = mi.resolve_identity("assistant", session_id="s1")
    assert assistant.name == mi.DEFAULT_ASSISTANT_NAME
    assert assistant.avatar == mi.BUILTIN_AVATAR_DRIFOX

    user = mi.resolve_identity("user", session_id="s1")
    assert user.name  # 系统用户名或回落「用户」
    assert user.avatar == ""


def test_team_agent_overrides_assistant_default():
    identity = mi.resolve_identity("assistant", session_id="s2", team_agent="build")
    assert identity.name == "build"


def test_provider_overrides_default_and_uses_priority():
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    registry = UIPluginRegistry.get_instance()
    registry.register_identity_name_provider("t_low", "p", lambda ctx: "low", priority=1)
    registry.register_identity_name_provider("t_high", "p", lambda ctx: "high", priority=9)
    registry.register_identity_avatar_provider("t_high", "p", lambda ctx: "/av.png", priority=9)
    try:
        identity = mi.resolve_identity("assistant", session_id="s3")
        assert identity.name == "high"
        assert identity.avatar == "/av.png"
    finally:
        for plugin in ("t_low", "t_high"):
            registry.unload_plugin(plugin)
        mi.clear_cache()


def test_provider_returning_empty_falls_through():
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    registry = UIPluginRegistry.get_instance()
    registry.register_identity_name_provider("t_empty", "p", lambda ctx: "", priority=9)
    try:
        identity = mi.resolve_identity("assistant", session_id="s4")
        assert identity.name == mi.DEFAULT_ASSISTANT_NAME
    finally:
        registry.unload_plugin("t_empty")
        mi.clear_cache()


def test_provider_exception_falls_through():
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    registry = UIPluginRegistry.get_instance()

    def _boom(ctx):
        raise RuntimeError("provider 炸了")

    registry.register_identity_name_provider("t_boom", "p", _boom, priority=9)
    try:
        identity = mi.resolve_identity("assistant", session_id="s5")
        assert identity.name == mi.DEFAULT_ASSISTANT_NAME
    finally:
        registry.unload_plugin("t_boom")
        mi.clear_cache()


def test_plugin_only_provides_name_keeps_default_avatar():
    """插件只提供 name 时，默认品牌头像不被继承（名字变了，品牌图标不适用）。"""
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    registry = UIPluginRegistry.get_instance()
    registry.register_identity_name_provider("t_name", "p", lambda ctx: "hanako", priority=9)
    try:
        identity = mi.resolve_identity("assistant", session_id="s6")
        assert identity.name == "hanako"
        assert identity.avatar == ""
    finally:
        registry.unload_plugin("t_name")
        mi.clear_cache()


# ── 会话级缓存 ────────────────────────────────────────────


def test_cache_returns_same_instance_per_context():
    first = mi.resolve_identity("assistant", session_id="s7")
    second = mi.resolve_identity("assistant", session_id="s7")
    assert first is second


def test_cache_separates_sessions_and_roles():
    a = mi.resolve_identity("assistant", session_id="s8")
    b = mi.resolve_identity("assistant", session_id="s9")
    c = mi.resolve_identity("user", session_id="s8")
    assert a is not b
    assert a is not c


def test_clear_cache_forces_reparse():
    first = mi.resolve_identity("assistant", session_id="s10")
    mi.clear_cache()
    second = mi.resolve_identity("assistant", session_id="s10")
    assert first is not second
    assert first == second


# ── 消息级身份 ────────────────────────────────────────────


def test_resolve_for_message_prefers_snapshot():
    message = {"role": "assistant", "_identity": {"name": "hanako"}}
    identity = mi.resolve_for_message(message, "assistant", session_id="s11")
    assert identity.name == "hanako"


def test_resolve_for_message_falls_back_to_context():
    identity = mi.resolve_for_message({}, "assistant", session_id="s12", team_agent="review")
    assert identity.name == "review"


# ── 团队邮件发送者 ────────────────────────────────────────


def test_team_mail_sender_extracts_agent_name():
    message = {
        "role": "user",
        "_hook_event": "TeamMail",
        "content": "<team-mail-hook>\n📨 **来自 [build@win_812] 的任务邮件：**\n\n做事\n</team-mail-hook>",
    }
    assert mi.team_mail_sender(message) == "build"


def test_team_mail_sender_requires_hook_marker():
    assert mi.team_mail_sender({"role": "user", "content": "📨 **来自 [build@w] 的任务邮件：**"}) == ""
    assert mi.team_mail_sender({"role": "user", "_hook_event": "TeamMail", "content": "无前缀"}) == ""
    assert mi.team_mail_sender(None) == ""
    assert mi.team_mail_sender({"role": "user", "_hook_event": "TeamMail", "content": ["list"]}) == ""


def test_team_mail_identity_resolution_ignores_snapshot_order():
    """快照优先于内容解析（历史消息不会因内容解析覆盖已冻结身份）。"""
    message = {
        "_identity": {"name": "hanako"},
        "_hook_event": "TeamMail",
        "content": "📨 **来自 [build@w] 的任务邮件：**",
    }
    assert mi.resolve_for_message(message, "user").name == "hanako"


def test_team_mail_without_snapshot_uses_sender():
    message = {
        "_hook_event": "TeamMail",
        "content": "📨 **来自 [review@win_813] 的任务邮件：**",
    }
    assert mi.resolve_for_message(message, "user").name == "review"


# ── 落库与 API 边界 ───────────────────────────────────────


def test_normalize_message_keeps_identity_snapshot():
    normalized = normalize_message(
        {
            "role": "assistant",
            "content": "hi",
            "timestamp": "2026-09-16 21:00:00",
            "_identity": {"name": "hanako", "avatar": "/a.png"},
        }
    )
    assert normalized is not None
    assert normalized["_identity"] == {"name": "hanako", "avatar": "/a.png"}


def test_normalize_message_drops_identity_without_name():
    base = {"role": "user", "content": "x", "timestamp": "2026-09-16 21:00:00"}
    assert "_identity" not in normalize_message({**base, "_identity": {"name": ""}})
    assert "_identity" not in normalize_message({**base, "_identity": "hanako"})


def test_normalize_message_filters_non_string_identity_values():
    normalized = normalize_message(
        {
            "role": "user",
            "content": "x",
            "timestamp": "2026-09-16 21:00:00",
            "_identity": {"name": "a", "extra": 123, "none": None},
        }
    )
    assert normalized["_identity"] == {"name": "a"}


def test_identity_never_leaks_into_api_payload():
    """API 请求只带协议字段——身份是 UI 态，绝不能发给模型。"""
    message = {
        "role": "assistant",
        "content": "hi",
        "timestamp": "2026-09-16 21:00:00",
        "_identity": {"name": "hanako", "avatar": "/a.png"},
    }
    api_message = messages_to_api([message], supports_vision=False)[0]
    assert "_identity" not in api_message
    assert set(api_message.keys()) <= {"role", "content", "tool_calls", "reasoning_content"}
