# -*- coding: utf-8 -*-
"""回归测试：插话消费时补触发用户消息 hook 链

背景：
- 正常发送路径 PreSendWorker（engine.py）依次触发 UserPromptSubmit →
  PreUserMessage → PostUserMessage；插话经 _hook_message_queue 直通车绕过
  该链路，hook（长期记忆/系统时间/关键文档注入、正则匹配）对插话全部失效。
- 修复：chat_worker._inject_pending_hook_messages 消费 _interject 条目时调用
  _trigger_interject_user_hooks 补触发三事件，hook 输出注入插话消息之前
  （对齐正常路径顺序），PostUserMessage 仅感知不注入。
"""

import queue
from types import SimpleNamespace

from app.core.workers.chat_worker import OpenAIChatWorker


class _FakeHookManager:
    """记录触发调用的 hook manager 桩，按事件名返回预置输出。"""

    def __init__(self, outputs=None):
        self.outputs = outputs or {}
        self.calls = []  # (event_name, context, current_message)

    def trigger_event(
        self, event_name, context=None, current_message="", trigger_async=False, skip_finished_callback=False
    ):
        self.calls.append((event_name, dict(context or {}), current_message))
        return [
            SimpleNamespace(success=True, output=out, add_to_context=True, status_message="")
            for out in self.outputs.get(event_name, [])
        ]


def _make_backend(hook_mgr):
    return SimpleNamespace(
        hook_manager=hook_mgr,
        tool_executor=SimpleNamespace(get_workdir=lambda: "/tmp/proj"),
        get_current_session=lambda: SimpleNamespace(session_id="sess-1", is_temp=False),
        _build_worktree_context_dict=lambda: {"worktree_hint": "w"},
        build_key_documents_context=lambda: {"key_docs": "d"},
    )


def _make_worker(backend):
    worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
    worker.session_id = None
    worker._current_session_messages = []
    worker.tool_executor = SimpleNamespace(_backend=backend)
    worker.appended = []
    worker._append_to_api_cache = lambda msgs: worker.appended.extend(msgs)
    worker._emit_with_callback = lambda *a, **k: None
    # 绕过 QObject.__init__ 后访问真实信号会抛 super-class __init__ never called
    worker.queued_user_injected = SimpleNamespace(emit=lambda *a: None)
    return worker


def _consume(worker):
    worker._inject_pending_hook_messages(session_messages_target=None, include_team_mail=False)


def test_interject_triggers_user_message_hooks():
    """核心：插话消费触发三事件，hook 输出注入在插话消息之前。"""
    hook_mgr = _FakeHookManager(
        outputs={
            "UserPromptSubmit": ["ups-inject"],
            "PreUserMessage": ["pre-inject"],
            "PostUserMessage": ["post-should-not-inject"],
        }
    )
    worker = _make_worker(_make_backend(hook_mgr))

    hook_q = queue.Queue()
    hook_q.put({"role": "user", "content": "插话内容", "_interject": True, "_interject_text": "插话内容"})
    worker.tool_executor._backend._hook_message_queue = hook_q

    _consume(worker)

    # 三事件按序触发，current_message 为插话文本
    events = [name for name, _, _ in hook_mgr.calls]
    assert events == ["UserPromptSubmit", "PreUserMessage", "PostUserMessage"], f"事件序错误: {events}"
    assert all(msg == "插话内容" for _, _, msg in hook_mgr.calls)

    # 注入顺序：hook 输出在前，插话消息在后；PostUserMessage 输出不注入
    # （_make_hook_message 会把输出包进 <system-reminder> 标签，用子串断言）
    contents = [m.get("content") for m in worker.appended]
    assert len(contents) == 3, f"注入条数错误: {contents}"
    assert "ups-inject" in contents[0] and "user-prompt-submit-hook" in contents[0]
    assert "pre-inject" in contents[1] and "pre-user-message-hook" in contents[1]
    assert contents[2] == "插话内容"
    assert all("post-should-not-inject" not in c for c in contents), "PostUserMessage 输出不得注入"
    assert worker._current_session_messages == worker.appended


def test_interject_ctx_carries_session_and_workdir():
    """hook ctx 携带 message/session_id/project_root；PreUserMessage 带增强 ctx。"""
    hook_mgr = _FakeHookManager()
    worker = _make_worker(_make_backend(hook_mgr))

    hook_q = queue.Queue()
    hook_q.put({"role": "user", "content": "hi", "_interject": True})
    worker.tool_executor._backend._hook_message_queue = hook_q

    _consume(worker)

    ctx_by_event = {name: ctx for name, ctx, _ in hook_mgr.calls}
    assert ctx_by_event["UserPromptSubmit"]["project_root"] == "/tmp/proj"
    assert ctx_by_event["UserPromptSubmit"]["session_id"] == "sess-1"
    assert ctx_by_event["UserPromptSubmit"]["message"] == "hi"
    assert ctx_by_event["PreUserMessage"]["worktree_hint"] == "w"
    assert ctx_by_event["PreUserMessage"]["key_docs"] == "d"


def test_non_interject_entry_skips_user_hooks():
    """对照：TeamMail 等非插话条目不触发用户消息 hook 链。"""
    hook_mgr = _FakeHookManager()
    worker = _make_worker(_make_backend(hook_mgr))

    hook_q = queue.Queue()
    hook_q.put({"role": "user", "content": "📨 邮件", "_hook_event": "TeamMail"})
    worker.tool_executor._backend._hook_message_queue = hook_q

    _consume(worker)

    assert hook_mgr.calls == [], "非插话条目不得触发用户消息 hook 链"
    assert [m.get("content") for m in worker.appended] == ["📨 邮件"]


def test_no_hook_manager_degrades_to_direct_inject():
    """降级：无 hook_manager 时插话消息仍正常注入（消费主流程不受影响）。"""
    backend = _make_backend(None)
    backend.hook_manager = None
    worker = _make_worker(backend)

    hook_q = queue.Queue()
    hook_q.put({"role": "user", "content": "插话", "_interject": True})
    worker.tool_executor._backend._hook_message_queue = hook_q

    _consume(worker)

    assert [m.get("content") for m in worker.appended] == ["插话"]


def test_transport_keys_stripped_before_inject():
    """传输键 _interject* 注入前剥净，落库是干净 user 消息。"""
    hook_mgr = _FakeHookManager()
    worker = _make_worker(_make_backend(hook_mgr))

    hook_q = queue.Queue()
    hook_q.put(
        {
            "role": "user",
            "content": "插话",
            "_interject": True,
            "_interject_text": "插话",
            "_interject_image_paths": ["/tmp/a.png"],
        }
    )
    worker.tool_executor._backend._hook_message_queue = hook_q

    _consume(worker)

    injected_user = [m for m in worker.appended if m.get("role") == "user" and m.get("content") == "插话"]
    assert len(injected_user) == 1
    clean = injected_user[0]
    assert not any(k.startswith("_interject") for k in clean), f"传输键残留: {list(clean)}"
