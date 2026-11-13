# -*- coding: utf-8 -*-
"""Gateway 群聊会话口径单测。

覆盖：
- 群聊按 chat_id 共享一条 GatewaySession（群内成员不各建一条）
- 私聊仍按 user_id 隔离；群聊与私聊同 chat_id 不混用
- chat_type 落盘，重启后索引按同一口径恢复（第三方 str 平台同样恢复）
- MessageHandler / GatewayService 把 chat_type 从事件透传到主线程
"""
import asyncio
from types import SimpleNamespace

from app.gateway.base import MessageEvent, Platform, SendResult
from system_im_core.message_handler import MessageHandler
from system_im_core.session_manager import GatewaySessionManager


def _event(user_id, chat_id, chat_type="dm", platform=Platform.WECOM, text="hi"):
    return MessageEvent(
        text=text,
        chat_id=chat_id,
        user_id=user_id,
        platform=platform,
        chat_type=chat_type,
    )


# ==================== 会话索引口径 ====================


def test_group_members_share_one_session(tmp_path):
    mgr = GatewaySessionManager(tmp_path)
    a = mgr.get_or_create_session(_event("u_a", "group_1", "group"))
    b = mgr.get_or_create_session(_event("u_b", "group_1", "group"))
    assert a.session_id == b.session_id
    assert b.message_count == 1  # 首条建会话不计，第二个成员命中后 +1
    assert a.chat_type == "group"


def test_dm_keeps_session_per_user(tmp_path):
    mgr = GatewaySessionManager(tmp_path)
    a = mgr.get_or_create_session(_event("u_a", "chat_a"))
    b = mgr.get_or_create_session(_event("u_b", "chat_b"))
    assert a.session_id != b.session_id


def test_group_and_dm_do_not_share(tmp_path):
    mgr = GatewaySessionManager(tmp_path)
    dm = mgr.get_or_create_session(_event("u_a", "same_chat"))
    grp = mgr.get_or_create_session(_event("u_a", "same_chat", "group"))
    assert dm.session_id != grp.session_id


def test_group_index_survives_restart(tmp_path):
    """重启后按 chat_type + chat_id 恢复索引（含第三方 str 平台）。"""
    first = GatewaySessionManager(tmp_path)
    s = first.get_or_create_session(_event("u_a", "group_1", "group", platform="feishu"))

    again = GatewaySessionManager(tmp_path)
    s2 = again.get_or_create_session(_event("u_b", "group_1", "group", platform="feishu"))
    assert s2.session_id == s.session_id
    assert s2.chat_type == "group"


def test_delete_session_removes_it(tmp_path):
    mgr = GatewaySessionManager(tmp_path)
    s = mgr.get_or_create_session(_event("u_a", "group_1", "group"))
    assert mgr.delete_session(s.session_id) is True
    assert mgr.get_session(s.session_id) is None
    assert mgr.session_count == 0


# ==================== chat_type 透传 ====================


def test_message_handler_passes_chat_type(tmp_path):
    seen = {}

    async def _process(**kwargs):
        seen.update(kwargs)
        return ""

    async def _send(**kwargs):
        return SendResult(success=True)

    handler = MessageHandler(GatewaySessionManager(tmp_path), _process, _send)
    asyncio.run(handler.handle(_event("u_a", "group_1", "group")))
    assert seen["chat_type"] == "group"
    assert seen["chat_id"] == "group_1"


class _FakeAdapter:
    def supports_streaming(self, chat_id):
        return False


class _FakeManager:
    def get_adapter(self, platform):
        return _FakeAdapter()


class _FakeSignal:
    def __init__(self):
        self.payloads = []

    def emit(self, payload):
        self.payloads.append(payload)
        payload["future"].set_result("")


def test_process_message_emits_group_chat_type():
    """worker 线程 emit 的 dict 必须带 chat_type，否则主线程重建事件时降级为 dm。"""
    from system_im_service import GatewayService

    sent = []

    async def _send_message(platform, chat_id, content, **kwargs):
        sent.append(content)
        return SendResult(success=True)

    svc = SimpleNamespace(
        _manager=_FakeManager(),
        _send_message=_send_message,
        gateway_input_received=_FakeSignal(),
    )
    out = asyncio.run(
        GatewayService._process_message(
            svc, "sid", "hi", Platform.WECOM, "group_1", "u_a", chat_type="group"
        )
    )

    payload = svc.gateway_input_received.payloads[0]
    assert payload["chat_type"] == "group"
    assert payload["chat_id"] == "group_1"
    assert out == ""
    assert sent == ["🤔 正在思考，请稍候..."]
