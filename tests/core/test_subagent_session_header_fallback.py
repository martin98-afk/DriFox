# -*- coding: utf-8 -*-
"""回归：子智能体请求必须携带服务商网关会话头（OpenCode 400 MissingSessionID）。

背景（用户报告）：斜杠命令 /code-reviewer 派发子智能体，首轮即 400
``Request is missing x-opencode-session``。根因两层：

1. ``MainWidget._execute_subagent_task`` 调 ``execute_task`` 未传 session_id；
2. ``SubAgentManager._current_session_id`` 全仓无写入方（``set_current_session_id``
   无调用者），替补兜底也只能是空串。

空 session 标识让 ``resolve_extra_headers`` 整块跳过网关会话头（该函数契约：
无 ID 不发明假 ID），OpenCode Zen/Go 直接拒绝请求。

修复：执行器以 ``task_id`` 兜底会话标识（同一任务内稳定），任何派发路径都不再
发出无头请求；斜杠命令侧同时补传真实 session_id。
"""

from app.core.workers.subagent_worker import _request_session_id


class _Executor:
    def __init__(self, task_session_id=None):
        if task_session_id is not None:
            self._task_session_id = task_session_id


def test_prefers_locked_task_session_id():
    assert _request_session_id(_Executor("sess-1"), "agent_x") == "sess-1"


def test_falls_back_to_task_id_when_session_missing():
    assert _request_session_id(_Executor(), "agent_x") == "agent_x"


def test_falls_back_to_task_id_when_session_empty():
    assert _request_session_id(_Executor(""), "agent_x") == "agent_x"


def test_session_header_injected_with_fallback(monkeypatch):
    """兜底 ID 必须真的进入 provider 请求头，否则网关仍 400。"""
    from app.core.modelmeta import provider_profile

    monkeypatch.setattr(
        provider_profile,
        "get_provider_profile",
        lambda cfg: {"family": "opencode", "session_header": "x-opencode-session"},
    )
    headers = provider_profile.resolve_extra_headers(
        {"API_URL": "https://opencode.ai/zen/go/v1"},
        _request_session_id(_Executor(), "agent_x"),
    )
    assert headers == {"x-opencode-session": "agent_x"}
