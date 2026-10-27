# -*- coding: utf-8 -*-
"""tool 侧 provider 的 host 兼容：工具 impl 传 ToolExecutor，hook 传 MainWidget"""

from app.core.conversation.action_queue import get_action_queue
from app.plugins.capabilities.providers import tool_core


class _Registry:
    def __init__(self):
        self.providers = {}

    def register(self, cap_id, provider, **kwargs):
        self.providers.setdefault(cap_id, provider)


class _ToolExecutor:
    def __init__(self, window_id: str):
        self._window_id = window_id
        self._state = {}

    def window_state_get(self, key, default=None):
        return self._state.get(key, default)

    def window_state_set(self, key, value):
        self._state[key] = value

    def window_state_delete(self, key):
        return self._state.pop(key, None)


class _Backend:
    def __init__(self, window_id: str):
        self._window_id = window_id
        self.tool_executor = _ToolExecutor(window_id)

    def get_current_session(self):
        return None


class _MainWidget:
    """hook 场景的 host：没有 window_state_*，只有 backend 属性"""

    def __init__(self, window_id: str):
        self.backend = _Backend(window_id)


def _providers():
    reg = _Registry()
    tool_core.register(reg)
    return reg.providers


def test_kv_works_with_tool_executor_host():
    te = _ToolExecutor("w-tool")
    kv = _providers()["state.kv"](te)
    assert kv is not None
    kv["set"]("k", 1)
    assert kv["get"]("k") == 1


def test_kv_works_with_main_widget_host():
    window = _MainWidget("w-hook")
    kv = _providers()["state.kv"](window)
    assert kv is not None
    kv["set"]("handoff_active", {"ts": 1})
    assert window.backend.tool_executor.window_state_get("handoff_active") == {"ts": 1}


def test_session_control_works_with_main_widget_host():
    q = get_action_queue()
    q.clear("w-hook")
    window = _MainWidget("w-hook")
    interrupt = _providers()["session.interrupt"](window)
    assert interrupt() is True
    assert [a["action"] for a in q.peek("w-hook")] == ["interrupt"]
    q.clear("w-hook")


def test_kv_returns_none_without_any_host():
    assert _providers()["state.kv"](object()) is None
