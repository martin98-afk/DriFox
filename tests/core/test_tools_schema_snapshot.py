# -*- coding: utf-8 -*-
"""会话级工具 schema 快照（冻结）回归测试

契约：tools 是请求前缀最外层，服务端 prompt cache 按前缀匹配；
会话内必须冻结，插件工具热重载不得改动当前会话的工具定义。
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


def test_invalidate_snapshot_rebuilds_next_request():
    """失效后再取 → 重新聚合（拿到新注册表内容）"""
    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)
    engine.get_session_tools_schema("s1")

    mgr.tools = [_tool("bash", "新描述")]
    engine.invalidate_tools_snapshot()
    tools = engine.get_session_tools_schema("s1")

    assert mgr.calls == 2
    assert tools[0]["function"]["description"] == "新描述"


def test_plugin_diff_invalidates_only_on_tool_changes(monkeypatch):
    """PluginChanged diff：只有工具/MCP 签名变化才失效快照"""
    from app.core.conversation.backend import ChatBackend
    from app.core.hooks import hook_manager

    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)
    backend = type("B", (), {"chat_engine": engine})()
    monkeypatch.setattr(ChatBackend, "_active_instances", {backend}, raising=False)

    # 只有 UI 组件变化（diff 无工具/MCP 键）：不失效
    hook_manager.invalidate_tools_snapshots_if_changed({"ui": True})
    engine.get_session_tools_schema("s1")
    assert mgr.calls == 1

    # 工具描述/参数变了：失效 → 下次请求重建
    hook_manager.invalidate_tools_snapshots_if_changed({"tools_updated": ["bash"]})
    engine.get_session_tools_schema("s1")
    assert mgr.calls == 2


def test_plugin_diff_ignores_empty_changes():
    """空 diff / 全空列表：不失效（改实现不改 schema 的场景）"""
    from app.core.hooks import hook_manager

    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)
    engine.get_session_tools_schema("s1")

    hook_manager.invalidate_tools_snapshots_if_changed(None)
    hook_manager.invalidate_tools_snapshots_if_changed({})
    hook_manager.invalidate_tools_snapshots_if_changed({"tools_added": [], "tools_removed": [], "tools_updated": []})

    engine.get_session_tools_schema("s1")
    assert mgr.calls == 1


def test_plugin_diff_ignores_mcp_changes(monkeypatch):
    """MCP 启停不换代：前缀保持稳定，新工具需新会话才见到"""
    from app.core.conversation.backend import ChatBackend
    from app.core.hooks import hook_manager

    mgr = _FakeAgentMgr([_tool("bash")])
    engine = _engine(mgr)
    backend = type("B", (), {"chat_engine": engine})()
    monkeypatch.setattr(ChatBackend, "_active_instances", {backend}, raising=False)

    engine.get_session_tools_schema("s1")
    hook_manager.invalidate_tools_snapshots_if_changed({"mcp_added": ["github"], "mcp_removed": ["fs"]})
    engine.get_session_tools_schema("s1")

    assert mgr.calls == 1, "MCP 启停不得触发快照换代"


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
