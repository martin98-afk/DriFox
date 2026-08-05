# -*- coding: utf-8 -*-
"""
回归测试：团队（邮件接收方）流式注入链路

测试目标：
1. _push_team_stream_to_requester 把流式回复以 hook 格式写入发起方窗口会话
2. hook 内容格式包含 <system-reminder><team-member-message-hook>...</team-member-message-hook></system-reminder>
3. 多次调用只就地更新同一条 hook 消息（按 _team_stream_task_id 关联），不碎片化
4. 发起方窗口不可用时（无对应窗口 / 与自身同窗口）不抛异常、不污染会话
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.main_widget as mw


_ORIG_INSTANCES = None


def _make_fake_session():
    return SimpleNamespace(messages=[], current_messages=[])


def _make_fake_backend():
    return SimpleNamespace(_hook_messages_updated=MagicMock())


def _make_requester(window_id: str, session):
    return SimpleNamespace(
        _window_id=window_id,
        _is_destroyed=False,
        session_manager=SimpleNamespace(get_current_session=lambda: session),
        backend=_make_fake_backend(),
    )


def _make_self(window_id: str, team_task: dict, *, with_card_text: str = None):
    self_obj = SimpleNamespace(
        _team_stream_task=team_task,
        _team_agent_name="bob",
        _window_id=window_id,
        _team_stream_injected_msg=None,
        _team_stream_partial="",
        _current_assistant_card=None,
    )
    # _push_team_stream_to_requester 内部会调用 self._find_team_window_by_id，
    # 这里直接复用类方法（它只依赖 OpenAIChatToolWindow._instances）。
    self_obj._find_team_window_by_id = (
        lambda wid: mw.OpenAIChatToolWindow._find_team_window_by_id(self_obj, wid)
    )
    if with_card_text is not None:
        self_obj._current_assistant_card = SimpleNamespace(get_plain_text=lambda: with_card_text)
    return self_obj


@pytest.fixture(autouse=True)
def _patch_instances():
    global _ORIG_INSTANCES
    _ORIG_INSTANCES = mw.OpenAIChatToolWindow._instances
    mw.OpenAIChatToolWindow._instances = []
    yield
    mw.OpenAIChatToolWindow._instances = _ORIG_INSTANCES


class TestTeamStreamInjection:
    def test_first_inject_creates_hook_message(self):
        """首次注入：在发起方会话中创建一条 TeamMemberMessage hook 消息"""
        session = _make_fake_session()
        requester = _make_requester("win_requester", session)
        mw.OpenAIChatToolWindow._instances = [requester]

        self_obj = _make_self(
            "win_bob",
            {"mail_id": "mail_1", "from_window": "win_requester", "from_agent": "alice", "sender_id": "alice@win_requester"},
        )
        self_obj._team_stream_partial = "正在分析中…"

        mw.OpenAIChatToolWindow._push_team_stream_to_requester(self_obj, is_final=False)

        # 发起方会话应当新增一条消息
        assert len(session.messages) == 1
        msg = session.messages[0]
        assert msg["role"] == "user"
        assert msg["_hook_event"] == "TeamMemberMessage"
        assert msg["_team_stream_task_id"] == "mail_1"
        # 同时写入 current_messages（供下一轮上下文）
        assert msg in session.current_messages

        content = msg["content"]
        assert "<system-reminder>" in content
        assert "<team-member-message-hook>" in content
        assert "</team-member-message-hook>" in content
        assert "</system-reminder>" in content
        # 内容应包含流式累计文本与来源标识
        assert "正在分析中" in content
        assert "[bob@win_bob]" in content
        assert "mail_1" in content
        # UI 刷新信号应被触发
        requester.backend._hook_messages_updated.emit.assert_called()

    def test_in_place_update_no_duplication(self):
        """多次调用只更新同一条消息，不向发起方会话追加新消息"""
        session = _make_fake_session()
        requester = _make_requester("win_requester", session)
        mw.OpenAIChatToolWindow._instances = [requester]

        self_obj = _make_self(
            "win_bob",
            {"mail_id": "mail_1", "from_window": "win_requester", "from_agent": "alice", "sender_id": "alice@win_requester"},
        )

        # 第一次流式片段
        self_obj._team_stream_partial = "第一段"
        mw.OpenAIChatToolWindow._push_team_stream_to_requester(self_obj, is_final=False)
        first_msg = session.messages[0]
        assert "第一段" in first_msg["content"]

        # 第二次流式片段：应当就地更新，会话仍只有一条消息
        self_obj._team_stream_partial = "第一段第二段"
        mw.OpenAIChatToolWindow._push_team_stream_to_requester(self_obj, is_final=False)
        assert len(session.messages) == 1
        assert session.messages[0] is first_msg  # 同一条消息对象
        assert "第一段第二段" in first_msg["content"]
        assert "第一段" in first_msg["content"]  # 累计文本

    def test_final_push_uses_card_text_and_marks_done(self):
        """最终态：使用卡片完整文本，并标记「已完成」"""
        session = _make_fake_session()
        requester = _make_requester("win_requester", session)
        mw.OpenAIChatToolWindow._instances = [requester]

        self_obj = _make_self(
            "win_bob",
            {"mail_id": "mail_1", "from_window": "win_requester", "from_agent": "alice", "sender_id": "alice@win_requester"},
            with_card_text="这是最终答复内容",
        )
        self_obj._team_stream_partial = "（不完整片段）"

        mw.OpenAIChatToolWindow._push_team_stream_to_requester(self_obj, is_final=True)

        msg = session.messages[0]
        # 最终态优先使用卡片完整文本，而非累计片段
        assert "这是最终答复内容" in msg["content"]
        assert "已完成" in msg["content"]
        assert "（不完整片段）" not in msg["content"]

    def test_missing_requester_is_safe(self):
        """发起方窗口不存在时不抛异常、不污染会话"""
        session = _make_fake_session()
        # 注意：_instances 中放一个 window_id 不匹配的窗口
        other = _make_requester("win_other", session)
        mw.OpenAIChatToolWindow._instances = [other]

        self_obj = _make_self(
            "win_bob",
            {"mail_id": "mail_1", "from_window": "win_requester", "from_agent": "alice", "sender_id": "alice@win_requester"},
        )
        self_obj._team_stream_partial = "x"

        # 不应抛异常
        mw.OpenAIChatToolWindow._push_team_stream_to_requester(self_obj, is_final=False)
        # 没有匹配窗口，发起方会话不应被写入
        assert len(session.messages) == 0

    def test_same_window_is_skipped(self):
        """发起方与自身为同一窗口（跨窗口团队不应出现）时跳过，不抛异常"""
        session = _make_fake_session()
        self_window = _make_requester("win_bob", session)  # 与 self 同 id
        mw.OpenAIChatToolWindow._instances = [self_window]

        self_obj = _make_self(
            "win_bob",
            {"mail_id": "mail_1", "from_window": "win_bob", "from_agent": "alice", "sender_id": "alice@win_bob"},
        )
        self_obj._team_stream_partial = "x"

        mw.OpenAIChatToolWindow._push_team_stream_to_requester(self_obj, is_final=False)
        assert len(session.messages) == 0
