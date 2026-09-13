# -*- coding: utf-8 -*-
"""skip_finished_callback 回归测试（2026-09-13 根因修复）

背景：
chat_worker._trigger_worker_hook 旧实现按「队列长度差 + FIFO 头取」排出 hook
自身入队的同步输出（prompt 类型 hook 的完成回调会入队，而其输出已由 results
直接注入消息列表，属重复投递）。该排出无法区分 hook 自身入队与同期到达的外部
消息：用户插话（_interject_entry）、TeamMail 等排在 hook 消息之前时会被
get_nowait() 当作 hook 输出丢弃 → 消息进了 UI 但 AI 永不响应
（「stophook 执行期间发送消息无反应」的根因）。

修复：HookManager.trigger_event 增加 skip_finished_callback 开关，worker 内部
hook（PreAssistantMessage / PostAssistantMessage / Stop）从源头不再入队。

本测试锁定两条不变式：
1. skip_finished_callback=True 时同步 hook 不触发完成回调（不入队），输出仍由
   返回值给出；
2. 队列中已有的外部消息（用户插话）在 hook 触发后原样保留。
"""

import queue

import pytest

from app.core.hook_manager import Hook, HookManager, HookMatchRule, HookType


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
    """注册一条 prompt 类型 hook（同步分支执行，无 I/O）"""
    hook = Hook(id="test_hook", type=HookType.PROMPT.value, prompt=prompt, add_output_to_context=True)
    hm._hooks.setdefault(event, []).append(HookMatchRule(matcher=None, hooks=[hook], skill_name="test"))


class TestSkipFinishedCallback:
    def test_skip_suppresses_callback_but_returns_result(self, hm):
        """skip=True：不触发完成回调，输出仍通过 results 返回（调用方自行注入）"""
        _add_prompt_hook(hm, "Stop", "收尾检查")
        calls: list = []
        hm.set_on_finished_callback(lambda *a: calls.append(a))

        results = hm.trigger_event("Stop", context={}, trigger_async=False, skip_finished_callback=True)

        assert calls == []
        assert any(r.success and r.add_to_context and r.output == "收尾检查" for r in results)

    def test_default_still_calls_callback(self, hm):
        """默认行为不变：PostToolUse 等路径依赖完成回调入队"""
        _add_prompt_hook(hm, "PostToolUse", "工具后检查")
        calls: list = []
        hm.set_on_finished_callback(lambda *a: calls.append(a))

        hm.trigger_event("PostToolUse", context={}, trigger_async=False)

        assert len(calls) == 1
        assert calls[0][0] == "__prompt__:PostToolUse"
        assert calls[0][1] == "工具后检查"


class TestExternalQueueItemsSurvive:
    """核心回归：hook 触发不再吞掉队列中的用户插话"""

    def test_interject_kept_when_sync_hook_triggers(self, hm):
        """插话先入队 + 同步 hook 触发 → 插话必须原样保留在队列中"""
        _add_prompt_hook(hm, "Stop", "收尾检查")
        q: queue.Queue = queue.Queue()
        interject_text = "用户在 AI 收尾时插入的消息"
        interject = {"role": "user", "content": interject_text, "_interject": True}

        def on_finished(event_name, output, success, status_message=""):
            """模拟 backend.on_hook_finished 的入队行为（旧实现下会与插话竞争）"""
            if success and output and output.strip():
                q.put({"role": "user", "content": output, "_hook_event": event_name})

        hm.set_on_finished_callback(on_finished)
        q.put(interject)  # 插话先入队：旧 drain 按 FIFO 头取会先取走它

        hm.trigger_event("Stop", context={}, trigger_async=False, skip_finished_callback=True)

        assert q.qsize() == 1, "hook 同步输出不应入队"
        item = q.get_nowait()
        assert item.get("_interject") is True, f"队列头部应是用户插话，实际: {item}"
        assert item["content"] == interject_text


class TestWorkerTriggerHookKeepsInterject:
    """真实调用链回归：chat_worker._trigger_worker_hook 触发 Stop hook 后，
    队列中的用户插话必须原样保留（旧实现按长度差排出并吞掉插话）"""

    @staticmethod
    def _make_worker(hm: HookManager, q: queue.Queue):
        worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
        w = worker_cls.__new__(worker_cls)  # 绕过 __init__：仅注入本方法依赖的状态

        class _Backend:
            def __init__(self) -> None:
                self.hook_manager = hm
                self.tool_executor = None
                self._hook_message_queue = q

            def get_current_session(self):
                return None

        w.tool_executor = type("T", (), {"_backend": _Backend()})()
        w._should_run_hook = lambda event: True
        w._append_to_api_cache = lambda msgs: None
        w._emit_with_callback = lambda *a, **k: None
        w._current_session_messages = []
        w._api_messages_cache = None
        return w

    def test_stop_hook_no_longer_swallows_interject(self, hm, monkeypatch):
        from app.core.workers import chat_worker as cw

        monkeypatch.setattr(cw, "_check_team_member", lambda backend: False)
        _add_prompt_hook(hm, "Stop", "收尾检查")

        q: queue.Queue = queue.Queue()
        interject = {"role": "user", "content": "插话内容", "_interject": True}

        def on_finished(event_name, output, success, status_message=""):
            """模拟 backend.on_hook_finished 的入队行为"""
            if success and output and output.strip():
                q.put({"role": "user", "content": output, "_hook_event": event_name})

        hm.set_on_finished_callback(on_finished)
        q.put(interject)
        w = self._make_worker(hm, q)

        messages: list = []
        session_messages: list = []
        w._trigger_worker_hook("Stop", messages, session_messages, extra_context={"reason": "completed"})

        # hook 输出仍由 results 路径注入消息列表
        assert any("收尾检查" in str(m.get("content", "")) for m in messages)
        # 插话原样保留（旧实现此处被 FIFO 头取吞掉）
        assert q.qsize() == 1, f"队列应只剩插话，实际长度 {q.qsize()}"
        item = q.get_nowait()
        assert item.get("_interject") is True, f"队列头部应是用户插话，实际: {item}"
        assert item["content"] == "插话内容"
