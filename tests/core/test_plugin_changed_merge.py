# -*- coding: utf-8 -*-
"""PluginChanged 净零合并回归测试（2026-11-02）

场景：用户连续开关插件（中间 AI 未消费消息）时，各条 hook 消息的
结构化 diff 相互抵消（+a 后 -a = 净零），消费端合并判定后整批不注入，
不再污染对话上下文。

锁定三条不变式：
1. diff 合并判定：added/removed 双向抵消，updated 是净变化，tools 与
   mcp 命名空间隔离；
2. 回调链：PluginChanged 的结构化 diff 经完成回调第 5 参传出；
3. 消费端：净零批次的 PluginChanged 消息不入 API 缓存，其余消息原样保留。
"""

import queue

import pytest

from app.core.hooks.hook_manager import (
    Hook,
    HookManager,
    HookMatchRule,
    HookType,
    filter_net_zero_plugin_messages,
    merge_plugin_diffs_net_zero,
)


@pytest.fixture
def hm():
    """干净 HookManager：隔离类级共享注册表，避免污染其它测试"""
    hooks = HookManager._shared_hooks
    skill_index = HookManager._shared_skill_to_hooks
    states = HookManager._shared_hook_states
    HookManager._shared_hooks = {}
    HookManager._shared_skill_to_hooks = {}
    HookManager._shared_hook_states = {}
    try:
        yield HookManager(thread_pool=object())
    finally:
        HookManager._shared_hooks = hooks
        HookManager._shared_skill_to_hooks = skill_index
        HookManager._shared_hook_states = states


def _add_prompt_hook(hm: HookManager, event: str, prompt: str = "hook 输出") -> None:
    hook = Hook(id="test_hook", type=HookType.PROMPT.value, prompt=prompt, add_output_to_context=True)
    hm._hooks.setdefault(event, []).append(HookMatchRule(matcher=None, hooks=[hook], skill_name="test"))


def _d(add=None, remove=None, update=None, mcp_add=None, mcp_remove=None) -> dict:
    return {
        "tools_added": add or [],
        "tools_removed": remove or [],
        "tools_updated": update or [],
        "mcp_added": mcp_add or [],
        "mcp_removed": mcp_remove or [],
    }


class TestMergePluginDiffsNetZero:
    def test_single_add_not_zero(self):
        assert merge_plugin_diffs_net_zero([_d(add=["a"])]) is False

    def test_add_then_remove_cancels(self):
        """核心场景：开了又关 → 净零"""
        assert merge_plugin_diffs_net_zero([_d(add=["a"]), _d(remove=["a"])]) is True

    def test_remove_then_add_not_zero(self):
        assert merge_plugin_diffs_net_zero([_d(remove=["a"]), _d(add=["a"])]) is False

    def test_partial_cancel_not_zero(self):
        """开 A 开 B 关 A → 还剩 b，非零"""
        assert merge_plugin_diffs_net_zero([_d(add=["a", "b"]), _d(remove=["a"])]) is False

    def test_toggle_three_times_ends_on(self):
        """开→关→开：最终在场，非零"""
        assert (
            merge_plugin_diffs_net_zero([_d(add=["a"]), _d(remove=["a"]), _d(add=["a"])]) is False
        )

    def test_updated_alone_not_zero(self):
        assert merge_plugin_diffs_net_zero([_d(update=["a"])]) is False

    def test_updated_then_removed_cancels(self):
        """改了描述又卸载：工具没了，净零"""
        assert merge_plugin_diffs_net_zero([_d(update=["a"]), _d(remove=["a"])]) is True

    def test_mcp_cancels(self):
        assert merge_plugin_diffs_net_zero([_d(mcp_add=["s1"]), _d(mcp_remove=["s1"])]) is True

    def test_same_name_tool_and_mcp_isolated(self):
        """同名工具与 MCP 服务器不互相抵消"""
        assert merge_plugin_diffs_net_zero([_d(add=["x"]), _d(mcp_remove=["x"])]) is False

    def test_empty_diffs_all_zero(self):
        assert merge_plugin_diffs_net_zero([]) is True
        assert merge_plugin_diffs_net_zero([None, None]) is True


class TestFilterNetZeroPluginMessages:
    def test_cancelled_batch_dropped_others_kept(self):
        msgs = [
            {"role": "user", "content": "工具结果", "_hook_event": "PostToolUse"},
            {"role": "user", "content": "启用", "_hook_event": "PluginChanged", "_plugin_diff": _d(add=["a"])},
            {"role": "user", "content": "禁用", "_hook_event": "PluginChanged", "_plugin_diff": _d(remove=["a"])},
            {"role": "user", "content": "插话", "_interject": True},
        ]
        out = filter_net_zero_plugin_messages(msgs)
        assert len(out) == 2
        assert all(m.get("_hook_event") != "PluginChanged" for m in out)

    def test_single_plugin_changed_kept(self):
        msgs = [{"role": "user", "content": "启用", "_hook_event": "PluginChanged", "_plugin_diff": _d(add=["a"])}]
        assert filter_net_zero_plugin_messages(msgs) == msgs

    def test_no_diff_plugin_changed_kept(self):
        """无结构化 diff 的事件通知（如 ui 重载）保守保留"""
        msgs = [{"role": "user", "content": "重载", "_hook_event": "PluginChanged"}]
        assert filter_net_zero_plugin_messages(msgs) == msgs

    def test_non_zero_batch_kept(self):
        msgs = [
            {"role": "user", "content": "启用A", "_hook_event": "PluginChanged", "_plugin_diff": _d(add=["a"])},
            {"role": "user", "content": "启用B", "_hook_event": "PluginChanged", "_plugin_diff": _d(add=["b"])},
            {"role": "user", "content": "禁用A", "_hook_event": "PluginChanged", "_plugin_diff": _d(remove=["a"])},
        ]
        assert filter_net_zero_plugin_messages(msgs) == msgs


class TestCallbackDiffPropagation:
    def test_plugin_changed_diff_reaches_callback(self, hm):
        """完成回调第 5 参必须收到 context 中的结构化 diff"""
        _add_prompt_hook(hm, "PluginChanged", "插件变更通知")
        calls: list = []
        hm.set_on_finished_callback(lambda *a: calls.append(a))

        diff = _d(add=["a"])
        hm.trigger_event("PluginChanged", context={"diff": diff}, trigger_async=False)

        assert len(calls) == 1
        assert calls[0][0] == "__prompt__:PluginChanged"
        assert calls[0][4] == diff

    def test_no_diff_callback_gets_none(self, hm):
        _add_prompt_hook(hm, "PluginChanged", "插件变更通知")
        calls: list = []
        hm.set_on_finished_callback(lambda *a: calls.append(a))

        hm.trigger_event("PluginChanged", context={}, trigger_async=False)

        assert len(calls) == 1
        assert calls[0][4] is None


class TestWorkerConsumesFiltered:
    """消费端集成：chat_worker 排空队列时净零批次不入 API 缓存"""

    @staticmethod
    def _make_worker(q: queue.Queue):
        worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
        w = worker_cls.__new__(worker_cls)  # 绕过 __init__：仅注入本方法依赖的状态

        class _Backend:
            def __init__(self) -> None:
                self.tool_executor = None
                self._hook_message_queue = q

        w.tool_executor = type("T", (), {"_backend": _Backend()})()
        w._appended: list = []
        w._append_to_api_cache = lambda msgs, _w=w: _w._appended.extend(msgs)
        w._current_session_messages = []
        return w

    def test_cancelled_batch_not_injected(self):
        q: queue.Queue = queue.Queue()
        q.put({"role": "user", "content": "启用", "_hook_event": "PluginChanged", "_plugin_diff": _d(add=["a"])})
        q.put({"role": "user", "content": "禁用", "_hook_event": "PluginChanged", "_plugin_diff": _d(remove=["a"])})
        q.put({"role": "user", "content": "工具结果", "_hook_event": "PostToolUse"})
        w = self._make_worker(q)

        w._inject_pending_hook_messages(include_team_mail=False)

        assert len(w._appended) == 1, f"净零批次应被丢弃，实际注入: {w._appended}"
        assert w._appended[0]["_hook_event"] == "PostToolUse"
        assert q.empty()

    def test_plugin_diff_transport_key_stripped(self):
        """有净变化的 PluginChanged 消息注入前必须剥离 _plugin_diff 传输键"""
        q: queue.Queue = queue.Queue()
        q.put({"role": "user", "content": "启用", "_hook_event": "PluginChanged", "_plugin_diff": _d(add=["a"])})
        w = self._make_worker(q)

        w._inject_pending_hook_messages(include_team_mail=False)

        assert len(w._appended) == 1
        assert "_plugin_diff" not in w._appended[0], "传输键不得进入 API 缓存/持久化"

    def test_real_change_still_injected(self):
        q: queue.Queue = queue.Queue()
        q.put({"role": "user", "content": "启用", "_hook_event": "PluginChanged", "_plugin_diff": _d(add=["a"])})
        w = self._make_worker(q)

        w._inject_pending_hook_messages(include_team_mail=False)

        assert len(w._appended) == 1, "有净变化的 PluginChanged 消息必须正常注入"
