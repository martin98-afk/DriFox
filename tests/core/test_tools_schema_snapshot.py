# -*- coding: utf-8 -*-
"""会话级工具 schema 快照（冻结）回归测试

契约：tools 是请求前缀最外层，服务端 prompt cache 按前缀匹配；
会话内必须完全冻结，插件工具增删改、热重载、MCP 启停均不得改动当前会话的
工具定义（新工具由 PluginChanged hook 注入 schema + tool_execute 中转调用）。
换代条件只有：换会话 / 换 agent / 进出团队。
"""

import copy
import threading


class _FakeAgentMgr:
    """最小 agent_manager：记录聚合次数并返回可控 schema"""

    def __init__(self, tools):
        self.calls = 0
        self.tools = tools

    def get_agent_tools_schema(self, agent_name, **kwargs):
        self.calls += 1
        return copy.deepcopy(self.tools)


def _tool(name: str, desc: str = "d") -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": {}}},
    }


def _engine(mgr):
    from app.core.engines.ui.engine import UIEngine

    engine = UIEngine.__new__(UIEngine)
    engine._agent_manager = mgr
    engine._get_agent_manager = lambda: mgr
    engine._current_agent = "build"
    engine._tool_executor = None
    engine._tools_snapshot = None
    engine._tools_snapshot_lock = threading.Lock()
    return engine


def test_snapshot_reused_within_session():
    """同一会话反复取 → 只聚合一次"""
    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)

    first = engine.get_session_tools_schema("s1")
    second = engine.get_session_tools_schema("s1")

    assert mgr.calls == 1
    assert first == second


def test_snapshot_survives_hot_reload():
    """模拟插件工具热重载：注册表变了，当前会话快照不变"""
    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)
    before = engine.get_session_tools_schema("s1")

    # 热重载：工具描述变了 + 新增工具
    mgr.tools = [_tool("bash", "新描述"), _tool("grep")]

    after = engine.get_session_tools_schema("s1")
    assert mgr.calls == 1, "热重载不得触发重新聚合"
    assert after == before, "当前会话工具定义必须冻结"


def test_snapshot_rotates_on_new_session():
    """新会话 → 拿最新注册表内容"""
    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)
    engine.get_session_tools_schema("s1")

    mgr.tools = [_tool("bash", "新描述"), _tool("grep")]
    tools = engine.get_session_tools_schema("s2")

    assert mgr.calls == 2
    assert [t["function"]["name"] for t in tools] == ["bash", "grep"]


def test_snapshot_rotates_on_agent_switch():
    """切 agent → 换代（不同 agent 工具集不同）"""
    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)
    engine.get_session_tools_schema("s1")

    engine._current_agent = "explore"
    engine.get_session_tools_schema("s1")

    assert mgr.calls == 2


def test_snapshot_returned_copy_is_isolated():
    """返回值是深拷贝：调用方改写不污染快照"""
    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)

    tools = engine.get_session_tools_schema("s1")
    tools[0]["function"]["description"] = "被污染"

    assert engine.get_session_tools_schema("s1")[0]["function"]["description"] != "被污染"


def test_plugin_tool_changes_never_rotate_snapshot(monkeypatch):
    """插件工具增删改一律不换代：tools 前缀永远稳定

    新工具由 PluginChanged hook 注入 schema + tool_execute 中转调用；
    移除工具调用时由 ToolExecutor 返 Unknown tool。
    """
    from app.core.conversation.backend import ChatBackend
    from app.core.hooks import hook_manager

    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)
    backend = type("B", (), {"chat_engine": engine})()
    monkeypatch.setattr(ChatBackend, "_active_instances", {backend}, raising=False)

    engine.get_session_tools_schema("s1")

    # 模拟完整一轮插件工具变更：UI 组件、工具新增/变更/移除、MCP 启停
    hook_manager.trigger_plugin_changed_hook({"action": "updated", "diff": {"ui": True}})
    hook_manager.trigger_plugin_changed_hook({"action": "installed", "diff": {"tools_added": ["grep"]}})
    hook_manager.trigger_plugin_changed_hook({"action": "updated", "diff": {"tools_updated": ["bash"]}})
    hook_manager.trigger_plugin_changed_hook({"action": "uninstalled", "diff": {"tools_removed": ["grep"]}})
    hook_manager.trigger_plugin_changed_hook({"action": "mcp_connected", "diff": {"mcp_added": ["github"]}})

    after = engine.get_session_tools_schema("s1")
    assert mgr.calls == 1, "插件工具增删改不得触发快照换代"
    assert after == [_tool("bash")], "当前会话工具定义必须冻结"


def test_presend_worker_uses_tools_provider():
    """发送路径：_PreSendWorker 优先走注入的快照提供方，跳过自身聚合"""
    from unittest.mock import MagicMock

    from app.core.engines.ui.engine import _PreSendWorker

    worker = _PreSendWorker.__new__(_PreSendWorker)
    # QThread 桩：__new__ 跳过 __init__，wrapper 未初始化时 isInterruptionRequested 会抛
    worker.isInterruptionRequested = lambda: False
    worker._hook_mgr = None
    worker._session = MagicMock()
    worker._session.session_id = "s1"
    worker._session.messages = []
    worker._user_text = "hi"
    worker._content_to_store = "hi"
    worker._user_prompt_ctx = None
    worker._pre_user_ctx = None
    worker._post_user_ctx = None
    worker._llm_config = {}
    worker._window_workdir = "."
    worker._lock = threading.Lock()
    worker._hook_event = None
    worker._adapter = MagicMock()
    worker._adapter.build_messages.return_value = []
    worker._current_agent = "build"
    worker._agent_manager = MagicMock()
    worker._tool_executor = MagicMock()
    worker._messages = []
    worker._available_tools = []
    worker._error = None
    worker._ephemeral = False
    worker._image_attachments = None
    worker._input_attachments = None
    worker._raw_input_text = None

    calls = []
    worker._tools_provider = lambda sid: calls.append(sid) or [{"sentinel": True}]

    worker.run()

    assert calls == ["s1"]
    assert worker._available_tools == [{"sentinel": True}]
    worker._agent_manager.get_agent_tools_schema.assert_not_called()
