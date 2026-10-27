# -*- coding: utf-8 -*-
"""会话控制能力（session.interrupt / new / send）：入队语义与窗口精确匹配"""

from unittest.mock import MagicMock

from app.core.conversation.action_queue import get_action_queue
from app.plugins.capabilities.providers.ui_core import _session_control


def _ui_window(wid: str):
    w = MagicMock()
    w._window_id = wid
    return w


def test_ui_provider_enqueues_in_order():
    q = get_action_queue()
    q.clear("w-test")
    api = _session_control(_ui_window("w-test"))
    assert api["interrupt"]() is True
    assert api["new"]() is True
    assert api["send"]("继续", ephemeral=True) is True
    assert [a["action"] for a in q.peek("w-test")] == ["interrupt", "new", "send"]
    q.clear("w-test")


def test_send_payload_keeps_ephemeral_flag():
    q = get_action_queue()
    q.clear("w-test2")
    api = _session_control(_ui_window("w-test2"))
    api["send"]("补写", ephemeral=False)
    payload = q.peek("w-test2")[0]["payload"]
    assert payload == {"text": "补写", "ephemeral": False}
    q.clear("w-test2")


def test_ui_provider_rejects_without_window():
    api = _session_control(None)
    assert api["interrupt"]() is False


def test_dedup_interrupt_within_window():
    q = get_action_queue()
    q.clear("w-test3")
    api = _session_control(_ui_window("w-test3"))
    assert api["interrupt"]() is True
    assert api["interrupt"]() is False
    q.clear("w-test3")
