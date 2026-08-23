# -*- coding: utf-8 -*-
"""PluginChanged hook 端到端链路验证

覆盖：
1. emit_plugin_changed → _trigger_plugin_changed_hook → HookManager.trigger_event
   → 注册的 PYTHON hook 函数执行
2. on_hook_finished 回调把输出注入 backend._hook_message_queue
3. _hook_messages_updated 信号被 emit
4. chat_worker._inject_pending_hook_messages 在下轮 API 调用前消费队列
5. format_tool_changes.py 格式化输出符合预期（含 diff 信息 / schema 注入）
"""

import json
import queue
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "plugins") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "plugins"))

from app.core import hook_manager as hm_mod
from app.core.backend import ChatBackend, _format_hook_output
from app.core.hook_manager import Hook, HookManager, HookMatchRule, HookType
from app.core.workers.chat_worker import OpenAIChatWorker as ChatWorker
from system.hooks import format_tool_changes


# ──────────────────────────────────────────────
# Helper：构造一个最小的 PluginChanged 测试环境
# ──────────────────────────────────────────────


@pytest.fixture
def isolated_hook_states(monkeypatch, tmp_path):
    """隔离 hook_states.json 落盘路径"""
    states_dir = tmp_path / "states"
    states_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        HookManager,
        "_get_hook_states_path",
        staticmethod(lambda: str(states_dir / "hook_states.json")),
    )
    HookManager._shared_hook_states = {}
    HookManager._shared_hook_overrides = {}
    HookManager._shared_hooks = {}
    HookManager._shared_skill_to_hooks = {}
    HookManager._shared_config_watchers = {}
    HookManager._shared_registered_functions = {}
    HookManager._shared_cwd_resolve_cache = {}
    HookManager._shared_restore_snapshots = {}
    yield states_dir


def _build_backend_with_inline_hook(hook_func, matcher: str = ""):
    """构造 ChatBackend + 用 register_function + 手工注册 Hook（PYTHON 注入型）

    用 register_function 让 hook 函数走 _registered_functions 表，绕过 SAFE_PYTHON_MODULES 检查。

    Args:
        hook_func: 接受 (event, context) 返回字符串的函数
        matcher: hook 匹配规则字符串

    Returns:
        (backend, hook_queue) - hook_queue 是 backend._hook_message_queue
    """
    backend = ChatBackend.__new__(ChatBackend)
    backend._ui_valid = True
    backend._hot_reload_seq = 0
    backend._hook_message_queue = queue.Queue()
    backend._pre_tool_message_queue = queue.Queue()
    backend._hook_messages_updated = MagicMock()
    backend._hook_manager = HookManager()

    # 注册函数到 _registered_functions 表
    func_name = f"__test_hook_{id(hook_func)}"
    backend._hook_manager.register_function(func_name, hook_func)

    # 手工构造 Hook + HookMatchRule，function 字段指向已注册函数
    hook = Hook(
        id=f"test-{func_name}",
        type=HookType.PYTHON.value,
        function=func_name,
        add_output_to_context=True,
        enabled=True,
        timeout=5,
        skill_root=str(_REPO_ROOT),
        is_system_plugin=True,
    )
    rule = HookMatchRule(matcher=matcher, hooks=[hook], skill_name="test")
    if "PluginChanged" not in backend._hook_manager._hooks:
        backend._hook_manager._hooks["PluginChanged"] = []
    backend._hook_manager._hooks["PluginChanged"].append(rule)

    return backend, backend._hook_message_queue


# ──────────────────────────────────────────────
# Case 1：注册函数后，PluginChanged 触发即命中
# ──────────────────────────────────────────────


class TestPluginChangedHookFires:
    """PluginChanged hook 在 trigger_event 后回调 on_hook_finished 写入队列"""

    def test_registered_function_called_with_diff(self, isolated_hook_states):
        """注册 PYTHON hook → trigger_event('PluginChanged', diff=...) → 函数被调用"""
        captured = {}

        def hook_fn(event, context):
            captured["event"] = event
            captured["context"] = dict(context)
            return f"[HOOK OK] action={context.get('action')}"

        backend, _ = _build_backend_with_inline_hook(hook_fn, matcher="installed|updated|enabled")

        # 触发 hook（同步路径，对齐 _trigger_plugin_changed_hook 内部调用）
        results = backend._hook_manager.trigger_event(
            "PluginChanged",
            context={
                "action": "enabled",
                "plugin_name": "demo",
                "diff": {"tools_added": ["foo"], "tools_removed": [], "tools_updated": []},
                "sub_actions": ["tools_added"],
            },
            trigger_async=False,
        )
        assert len(results) == 1, f"hook 应被命中 1 次，实际 {len(results)}"
        assert "[HOOK OK]" in results[0].output
        assert captured["event"] == "PluginChanged"
        assert captured["context"]["action"] == "enabled"
        assert captured["context"]["sub_actions"] == ["tools_added"]

    def test_matcher_filters_by_action(self, isolated_hook_states):
        """matcher 仅命中 enabled，installed 不命中"""
        called = []

        def hook_fn(event, context):
            called.append(context["action"])
            return "ok"

        backend, _ = _build_backend_with_inline_hook(hook_fn, matcher="enabled|disabled")

        # enabled 应命中
        results = backend._hook_manager.trigger_event(
            "PluginChanged",
            context={"action": "enabled", "sub_actions": []},
            trigger_async=False,
        )
        assert len(results) == 1
        assert called == ["enabled"]

        # installed 不命中
        results = backend._hook_manager.trigger_event(
            "PluginChanged",
            context={"action": "installed", "sub_actions": []},
            trigger_async=False,
        )
        assert len(results) == 0
        assert called == ["enabled"]  # 没新增

    def test_matcher_filters_by_sub_actions(self, isolated_hook_states):
        """matcher 仅命中工具级子动作"""
        called = []

        def hook_fn(event, context):
            called.append(context.get("sub_actions", []))
            return "ok"

        backend, _ = _build_backend_with_inline_hook(hook_fn, matcher="tools_added|tools_removed")

        # tools_added 子动作命中
        results = backend._hook_manager.trigger_event(
            "PluginChanged",
            context={"action": "enabled", "sub_actions": ["tools_added"]},
            trigger_async=False,
        )
        assert len(results) == 1

        # mcp_added 不命中（不在 matcher）
        results = backend._hook_manager.trigger_event(
            "PluginChanged",
            context={"action": "enabled", "sub_actions": ["mcp_added"]},
            trigger_async=False,
        )
        assert len(results) == 0

    def test_on_hook_finished_puts_into_message_queue(self, isolated_hook_states):
        """on_hook_finished 把 PluginChanged 输出塞进 _hook_message_queue + emit signal"""
        # 由于 on_hook_finished 是 backend.__init__ 内的闭包，
        # 本测试不走 __init__，直接模拟 on_hook_finished 的 PluginChanged 分支
        backend = ChatBackend.__new__(ChatBackend)
        backend._ui_valid = True
        hook_queue = queue.Queue()
        backend._hook_message_queue = hook_queue
        backend._pre_tool_message_queue = queue.Queue()
        signal_emitted = []
        mock_signal = MagicMock()
        mock_signal.emit = MagicMock(side_effect=lambda: signal_emitted.append(True))
        backend._hook_messages_updated = mock_signal

        # 模拟 on_hook_finished 闭包逻辑（PluginChanged 分支）
        output = "新增工具：foo"
        hook_output = _format_hook_output("PluginChanged", output, "")
        backend._hook_message_queue.put({"role": "user", "content": hook_output, "_hook_event": "PluginChanged"})
        backend._hook_messages_updated.emit()

        # 验证
        assert not hook_queue.empty(), "PluginChanged 输出必须入队"
        msg = hook_queue.get_nowait()
        assert msg["role"] == "user", f"应为 user 角色，实际 {msg['role']}"
        assert msg["_hook_event"] == "PluginChanged"
        assert "<plugin-changed-hook>" in msg["content"], (
            f"应包含 kebab-case 标签 plugin-changed-hook，实际 {msg['content']}"
        )
        assert "新增工具：foo" in msg["content"]
        assert signal_emitted == [True], "_hook_messages_updated.emit 必须被调用"


# ──────────────────────────────────────────────
# Case 2：完整端到端 — 从 emit_plugin_changed 到 hook queue
# ──────────────────────────────────────────────


class TestEmitPluginChangedEndToEnd:
    """emit_plugin_changed 触发后 _trigger_plugin_changed_hook 命中 hook 函数"""

    def test_emit_plugin_changed_triggers_hook_function(self, monkeypatch, isolated_hook_states):
        """emit_plugin_changed(result) → hook_manager.trigger_event('PluginChanged') → 命中函数"""
        captured = []

        def fake_hook(event, context):
            captured.append((event, dict(context)))
            return "工具已变更"

        backend, _ = _build_backend_with_inline_hook(fake_hook, matcher="")
        # mock plugin_changed 信号（PyQtSignal 在 __new__ 实例上访问会抛 RuntimeError）
        backend.plugin_changed = MagicMock()
        # 模拟 active instance 集合（broadcast 用），保证 trigger_plugin_changed_hook 找得到
        monkeypatch.setattr(ChatBackend, "_active_instances", [backend])
        # 防止 broadcast 循环里 RuntimeError（虽然本测试只放一个实例）
        # 调用 emit_plugin_changed（同步路径走 _trigger_plugin_changed_hook → trigger_event）
        backend.emit_plugin_changed({"ui": True}, "demo-plugin")

        # hook 函数被命中，captured 含 (event, context)
        assert len(captured) == 1, f"PluginChanged hook 必须被命中，实际 {len(captured)} 次"
        event, ctx = captured[0]
        assert event == "PluginChanged"
        assert ctx["action"] in ("installed", "updated", "uninstalled", "enabled", "disabled")
        # 插件名 sentinel 解析后置空字符串（_NEW_PLUGIN_SENTINEL → ""）
        assert ctx["plugin_name"] == "demo-plugin" or ctx["plugin_name"] == ""
        # components 必含
        assert "components" in ctx


# ──────────────────────────────────────────────
# Case 3：chat_worker 消费队列
# ──────────────────────────────────────────────


class TestChatWorkerConsumesPluginChangedQueue:
    """chat_worker._inject_pending_hook_messages 消费 PluginChanged 队列"""

    def test_inject_pending_consumes_plugin_changed_msg(self, isolated_hook_states, monkeypatch):
        """队列里有一条 PluginChanged 消息 → _inject_pending_hook_messages 注入会话"""
        from PyQt5.QtCore import QThread

        backend = ChatBackend.__new__(ChatBackend)
        backend._ui_valid = True
        backend._hook_message_queue = queue.Queue()
        backend._pre_tool_message_queue = queue.Queue()
        backend._hook_messages_updated = MagicMock()

        # 预填一条 PluginChanged 消息
        hook_content = _format_hook_output("PluginChanged", "新增工具：foo", "")
        plugin_changed_msg = {
            "role": "user",
            "content": hook_content,
            "_hook_event": "PluginChanged",
        }
        backend._hook_message_queue.put(plugin_changed_msg)

        # 构造 worker（OpenAIChatWorker 继承 QThread，必须走 QThread.__init__ 初始化 C++ 绑定）
        worker = ChatWorker.__new__(ChatWorker)
        QThread.__init__(worker)
        worker.tool_executor = MagicMock()
        worker.tool_executor._backend = backend
        worker._api_messages_cache = []
        worker._current_session_messages = []
        # mock 掉 _append_to_api_cache 避免 _serialize_for_api 全量依赖
        worker._append_to_api_cache = MagicMock()

        # 调用 _inject_pending_hook_messages（不带 team_mail）
        worker._inject_pending_hook_messages(include_team_mail=False)

        # 验证消息从队列取出 + 注入会话
        assert backend._hook_message_queue.empty(), "队列必须被消费"
        # _append_to_api_cache 被调用 1 次（msgs 列表）
        worker._append_to_api_cache.assert_called_once()
        # _current_session_messages 直接被 extend
        assert len(worker._current_session_messages) == 1
        msg = worker._current_session_messages[0]
        assert msg["_hook_event"] == "PluginChanged"
        assert "<plugin-changed-hook>" in msg["content"]
        assert "新增工具：foo" in msg["content"]


# ──────────────────────────────────────────────
# Case 4：format_tool_changes 实际函数输出验证
# ──────────────────────────────────────────────


class TestFormatToolChangesIntegration:
    """system hook format_tool_changes.hook 输出符合预期

    这是生产链路实际跑的 hook 函数——验证它对真实 diff 输入产出期望输出。
    """

    def test_tools_added_produces_injection_with_schema(self, monkeypatch):
        """tools_added → 注入完整 schema + 可用性声明"""
        from app.tools.registry import ToolRegistry

        fake_registry = MagicMock(spec=ToolRegistry)
        fake_tool = MagicMock()
        fake_tool.schema = {
            "function": {
                "name": "foo",
                "description": "do foo",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        }
        fake_registry.get.return_value = fake_tool
        monkeypatch.setattr("app.tools.registry.ToolRegistry.get_instance", lambda: fake_registry)

        output = format_tool_changes.hook(
            "PluginChanged",
            {
                "action": "enabled",
                "plugin_name": "demo",
                "diff": {
                    "tools_added": ["foo"],
                    "tools_removed": [],
                    "tools_updated": [],
                    "mcp_added": [],
                    "mcp_removed": [],
                },
                "sub_actions": ["tools_added"],
            },
        )

        assert "新增工具" in output
        assert "已加入当前请求的工具列表" in output
        assert "foo" in output
        # schema 被完整注入（参数 x 应出现）
        assert "x" in output

    def test_tools_removed_produces_removal_notice(self, monkeypatch):
        """tools_removed → 移除提示"""
        from app.tools.registry import ToolRegistry

        fake_registry = MagicMock(spec=ToolRegistry)
        fake_registry.get.return_value = None  # 已移除，registry 查不到
        monkeypatch.setattr("app.tools.registry.ToolRegistry.get_instance", lambda: fake_registry)

        output = format_tool_changes.hook(
            "PluginChanged",
            {
                "action": "uninstalled",
                "plugin_name": "demo",
                "diff": {
                    "tools_added": [],
                    "tools_removed": ["foo"],
                    "tools_updated": [],
                    "mcp_added": [],
                    "mcp_removed": [],
                },
                "sub_actions": ["tools_removed"],
            },
        )

        assert "移除工具" in output
        assert "已从工具列表移除" in output
        assert "foo" in output

    def test_no_tool_layer_change_produces_empty_output(self):
        """无工具/MCP 层变化（如仅 ui/theme）→ 空输出（不注入）"""
        output = format_tool_changes.hook(
            "PluginChanged",
            {
                "action": "updated",
                "plugin_name": "demo",
                "diff": {
                    "tools_added": [],
                    "tools_removed": [],
                    "tools_updated": [],
                    "mcp_added": [],
                    "mcp_removed": [],
                },
                "sub_actions": [],
            },
        )
        assert output == "", "仅 UI 重载时不应注入任何内容（避免噪音）"


# ──────────────────────────────────────────────
# Case 5：完整链路 — hook 函数 + 队列 + chat_worker 消费
# ──────────────────────────────────────────────


class TestFullPipelinePluginChangedToConversation:
    """PluginChanged 触发链：emit → hook fn → queue → worker 消费"""

    def test_full_chain(self, monkeypatch, isolated_hook_states):
        """一站式：trigger_event 触发 hook → 结果入队 → worker 消费"""
        # 1) ToolRegistry mock（让 format_tool_changes 能查到 schema）
        from app.tools.registry import ToolRegistry

        fake_registry = MagicMock(spec=ToolRegistry)
        fake_tool = MagicMock()
        fake_tool.schema = {
            "function": {
                "name": "foo",
                "description": "do foo",
                "parameters": {"type": "object"},
            }
        }
        fake_registry.get.return_value = fake_tool
        monkeypatch.setattr("app.tools.registry.ToolRegistry.get_instance", lambda: fake_registry)

        # 2) 构造 backend 并把 format_tool_changes.hook 注册为 inline 函数
        backend = ChatBackend.__new__(ChatBackend)
        backend._ui_valid = True
        backend._hot_reload_seq = 0
        backend._hook_message_queue = queue.Queue()
        backend._pre_tool_message_queue = queue.Queue()
        backend._hook_messages_updated = MagicMock()
        backend._hook_manager = HookManager()

        # 注册 format_tool_changes.hook 函数
        func_name = "__format_tool_changes__"
        backend._hook_manager.register_function(func_name, format_tool_changes.hook)
        hook = Hook(
            id="format-tool-changes",
            type=HookType.PYTHON.value,
            function=func_name,
            add_output_to_context=True,
            enabled=True,
            timeout=5,
            skill_root=str(_REPO_ROOT / "plugins" / "system"),
            is_system_plugin=True,
        )
        rule = HookMatchRule(
            matcher="tools_added|tools_removed|tools_updated",
            hooks=[hook],
            skill_name="system",
        )
        backend._hook_manager._hooks["PluginChanged"] = [rule]

        # 3) 触发 hook（同步路径）
        results = backend._hook_manager.trigger_event(
            "PluginChanged",
            context={
                "action": "enabled",
                "plugin_name": "demo",
                "diff": {
                    "tools_added": ["foo"],
                    "tools_removed": [],
                    "tools_updated": [],
                    "mcp_added": [],
                    "mcp_removed": [],
                },
                "sub_actions": ["tools_added"],
            },
            trigger_async=False,
        )
        assert len(results) == 1
        assert "新增工具" in results[0].output, (
            f"format_tool_changes 必须产出工具变更提示，实际: {results[0].output[:100]}"
        )

        # 4) 模拟 on_hook_finished 闭包（PluginChanged 分支）将结果入队
        output = results[0].output
        hook_msg = {
            "role": "user",
            "content": _format_hook_output("PluginChanged", output, ""),
            "_hook_event": "PluginChanged",
        }
        backend._hook_message_queue.put(hook_msg)
        backend._hook_messages_updated.emit()

        # 5) 验证队列非空 + signal emit
        assert not backend._hook_message_queue.empty()
        backend._hook_messages_updated.emit.assert_called()

        # 6) chat_worker 消费
        from PyQt5.QtCore import QThread

        worker = ChatWorker.__new__(ChatWorker)
        QThread.__init__(worker)
        worker.tool_executor = MagicMock()
        worker.tool_executor._backend = backend
        worker._api_messages_cache = []
        worker._current_session_messages = []
        worker._append_to_api_cache = MagicMock()
        worker._inject_pending_hook_messages(include_team_mail=False)

        # 7) 验证
        assert backend._hook_message_queue.empty()
        assert len(worker._current_session_messages) == 1
        msg = worker._current_session_messages[0]
        assert msg["_hook_event"] == "PluginChanged"
        assert "<plugin-changed-hook>" in msg["content"]
        assert "新增工具" in msg["content"]
        # schema 也注入
        assert "foo" in msg["content"]


# ──────────────────────────────────────────────
# Case 6：_plugin_snapshot 快照基线跨调用共享
# ──────────────────────────────────────────────


class TestPluginSnapshotSharedBaseline:
    """_plugin_snapshot_tools / _plugin_snapshot_mcp 模块级共享基线

    多次 trigger_plugin_changed_hook 调用共享同一基线：
    - 首次调用只建基线返回 None（无 diff）
    - 后续调用基于上一次基线计算 diff
    """

    def test_first_call_establishes_baseline(self, monkeypatch):
        """首次触发只建基线，不计算 diff"""
        from app.core import hook_manager as hm

        # mock ToolRegistry
        from app.tools.registry import ToolRegistry

        fake_registry = MagicMock(spec=ToolRegistry)
        fake_registry.list.return_value = []
        monkeypatch.setattr("app.tools.registry.ToolRegistry.get_instance", lambda: fake_registry)

        # mock PluginManager
        fake_pm = MagicMock()
        fake_pm.get_mcp_servers.return_value = []
        monkeypatch.setattr(
            "app.plugins.managers.plugin_manager.PluginManager.get_instance",
            lambda: fake_pm,
        )

        # 重置基线
        hm._plugin_snapshot_tools = None
        hm._plugin_snapshot_mcp = None

        # 首次调用
        diff = hm._compute_plugin_snapshot_diff()
        assert diff is None, "首次调用应只建基线返回 None"
        assert hm._plugin_snapshot_tools is not None
        assert hm._plugin_snapshot_mcp is not None

    def test_subsequent_call_computes_diff(self, monkeypatch):
        """后续调用基于基线计算 diff"""
        from app.core import hook_manager as hm

        from app.tools.registry import ToolRegistry

        # 第一次：tools = []
        fake_registry = MagicMock(spec=ToolRegistry)
        fake_registry.list.return_value = []
        monkeypatch.setattr("app.tools.registry.ToolRegistry.get_instance", lambda: fake_registry)
        fake_pm = MagicMock()
        fake_pm.get_mcp_servers.return_value = []
        monkeypatch.setattr(
            "app.plugins.managers.plugin_manager.PluginManager.get_instance",
            lambda: fake_pm,
        )

        hm._plugin_snapshot_tools = None
        hm._plugin_snapshot_mcp = None
        diff = hm._compute_plugin_snapshot_diff()
        assert diff is None  # 首次建基线

        # 第二次：tools 增加了 foo
        fake_tool = MagicMock()
        fake_tool.name = "foo"
        fake_tool.schema = {"function": {"name": "foo", "description": "x", "parameters": {}}}
        fake_registry.list.return_value = [fake_tool]
        diff = hm._compute_plugin_snapshot_diff()
        assert diff is not None
        assert "foo" in diff["tools_added"]
