# -*- coding: utf-8 -*-
"""回归测试：插话路径丢失命令/技能提示词注入（_pending_command/_pending_skill）

背景：
- `/cmd` 与 `/skill` 的提示词不直接改写用户消息，而是由 main_widget 写入
  session.metadata["_pending_command"/"_pending_skill"]，再由 PreUserMessage
  的 inject_command_prompt hook 读取并注入（engine.py PreSendWorker 负责 pop）。
- 繁忙插话经 _hook_message_queue 直通车，不经过 PreSendWorker：
  chat_worker._trigger_interject_user_hooks 的 pre_ctx 从未读取 pending，
  于是 hook 输出为空（命令提示词不注入），且 metadata 里的 pending 残留下来，
  污染下一轮正常发送（把上一条命令的提示词注进无关提问）。
- 修复：_interject_entry 在主线程 pop pending 并随消息带 _interject_pending_ctx，
  worker 侧读出后并入 PreUserMessage ctx，注入前剥净传输键。
"""

import inspect
import queue
from types import SimpleNamespace

import app.main_widget as mw
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


def _make_backend(hook_mgr, session=None):
    session = session or SimpleNamespace(session_id="sess-1", is_temp=False, metadata={})
    return SimpleNamespace(
        hook_manager=hook_mgr,
        tool_executor=SimpleNamespace(get_workdir=lambda: "/tmp/proj"),
        get_current_session=lambda: session,
        _build_worktree_context_dict=lambda: {"worktree_hint": "w"},
    )


def _make_worker(backend):
    worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
    worker.session_id = None
    worker._current_session_messages = []
    worker.tool_executor = SimpleNamespace(_backend=backend)
    worker.appended = []
    worker._append_to_api_cache = lambda msgs: worker.appended.extend(msgs)
    worker._emit_with_callback = lambda *a, **k: None
    worker.queued_user_injected = SimpleNamespace(emit=lambda *a: None)
    return worker


def _consume(worker):
    worker._inject_pending_hook_messages(session_messages_target=None, include_team_mail=False)


# ============ 源码契约：插话入队时把 pending 挂到消息上 ============


def test_interject_entry_carries_pending_ctx():
    """_interject_entry 必须 pop session.metadata 里的 pending 并挂到消息传输键上"""
    src = inspect.getsource(mw.OpenAIChatToolWindow._interject_entry)
    assert "_pending_command" in src, "插话入队必须提取 _pending_command，否则命令提示词无法注入"
    assert "_pending_skill" in src, "插话入队必须提取 _pending_skill，否则技能内容无法注入"
    assert "_interject_pending_ctx" in src, "pending 需通过消息传输键带给 worker 侧"


# ============ 行为：worker 消费插话时把 pending 并入 PreUserMessage ctx ============


def test_interject_pending_command_reaches_pre_user_hook():
    """核心：插话消息携带的 pending_command 必须进入 PreUserMessage 的 ctx"""
    hook_mgr = _FakeHookManager(outputs={"PreUserMessage": ["cmd-inject"]})
    backend = _make_backend(hook_mgr)
    worker = _make_worker(backend)

    hook_q = queue.Queue()
    hook_q.put(
        {
            "role": "user",
            "content": "/review 全部",
            "_interject": True,
            "_interject_text": "/review 全部",
            "_interject_pending_ctx": {
                "pending_command": {"prompt_text": "REVIEW PROMPT", "command_name": "review", "remainder": "全部"}
            },
        }
    )
    backend._hook_message_queue = hook_q

    _consume(worker)

    pre_ctx = next(ctx for name, ctx, _ in hook_mgr.calls if name == "PreUserMessage")
    pending = pre_ctx.get("pending_command") or {}
    assert pending.get("prompt_text") == "REVIEW PROMPT", f"pending_command 未进 PreUserMessage ctx: {pre_ctx}"
    assert pending.get("command_name") == "review"

    # hook 输出仍注入在插话消息之前，插话消息本身干净
    contents = [m.get("content") for m in worker.appended]
    assert any("cmd-inject" in (c or "") for c in contents), f"命令提示词未注入: {contents}"
    assert contents[-1] == "/review 全部"


def test_interject_pending_skill_reaches_pre_user_hook():
    """技能路径同理：pending_skill 进 PreUserMessage ctx"""
    hook_mgr = _FakeHookManager()
    backend = _make_backend(hook_mgr)
    worker = _make_worker(backend)

    hook_q = queue.Queue()
    hook_q.put(
        {
            "role": "user",
            "content": "/drifox-dev 修这个",
            "_interject": True,
            "_interject_text": "/drifox-dev 修这个",
            "_interject_pending_ctx": {
                "pending_skill": {
                    "name": "drifox-dev",
                    "content": "SKILL BODY",
                    "workspace": "/ws",
                    "remainder": "修这个",
                }
            },
        }
    )
    backend._hook_message_queue = hook_q

    _consume(worker)

    pre_ctx = next(ctx for name, ctx, _ in hook_mgr.calls if name == "PreUserMessage")
    skill = pre_ctx.get("pending_skill") or {}
    assert skill.get("name") == "drifox-dev"
    assert skill.get("content") == "SKILL BODY"


def test_interject_pending_ctx_key_not_leaked():
    """传输键 _interject_pending_ctx 注入前剥净，落库是干净 user 消息"""
    hook_mgr = _FakeHookManager()
    backend = _make_backend(hook_mgr)
    worker = _make_worker(backend)

    hook_q = queue.Queue()
    hook_q.put(
        {
            "role": "user",
            "content": "插话",
            "_interject": True,
            "_interject_text": "插话",
            "_interject_pending_ctx": {"pending_command": {"prompt_text": "X", "command_name": "x"}},
        }
    )
    backend._hook_message_queue = hook_q

    _consume(worker)

    injected_user = [m for m in worker.appended if m.get("role") == "user" and m.get("content") == "插话"]
    assert len(injected_user) == 1
    assert not any(k.startswith("_interject") for k in injected_user[0]), f"传输键残留: {list(injected_user[0])}"
