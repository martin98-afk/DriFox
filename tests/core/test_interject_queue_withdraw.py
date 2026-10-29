# -*- coding: utf-8 -*-
"""回归测试：中止后残留插话被下一个 worker 复活 → 双卡片（2026-10-29）

症状：流式中插话 → 上一轮未结束就手动中止 → 插话卡撤回/删除无效，
重新发送后消息列表出现两张同文本卡片（DB 中两条同文本 user 消息）。

根因链：
1. 插话入队即出用户卡（`_interject_entry`），此刻 session 无对应消息
2. 中止时 worker 卡在工具执行，`cancel()` 后主循环因 `while not self._is_cancelled`
   直接退出，不经过循环内 `_cancel_with_stop_hook` 的队列排空
3. 撤回/删除只删 UI 卡（`_remove_interject_ghost_card`），队列里的 `_interject`
   条目仍在
4. 下一个 worker 首轮 `_inject_pending_hook_messages` 把它当新插话消费
   → 已撤回消息复活，与用户重发的同一文本形成两张卡片

修复契约：
- `ChatBackend.withdraw_interject(text)`：撤回/删除时从队列摘除对应条目
- `ChatBackend.reclaim_pending_interjects()`：停止链路立即把队列插话转入 stash
- `ChatWorker._reclaim_pending_interjects_on_exit()`：取消退出的最后兜底
"""

import inspect
import queue
import threading
import types
from types import SimpleNamespace

from app.core.conversation.backend import ChatBackend
from app.core.workers.chat_worker import OpenAIChatWorker

# ============ 桩工具 ============


def _make_backend(hook_q):
    """构造最小 backend 替身：绑定真实方法，绕开 QObject 未初始化时的属性转发"""
    backend = SimpleNamespace(
        _hook_message_queue=hook_q,
        _interject_lock=threading.Lock(),
        _recovered_interjects=[],
    )
    for name in (
        "withdraw_interject",
        "reclaim_pending_interjects",
        "stash_recovered_interjects",
        "take_recovered_interjects",
    ):
        setattr(backend, name, types.MethodType(getattr(ChatBackend, name), backend))
    return backend


def _interject(text, **extra):
    msg = {"role": "user", "content": text, "_interject": True, "_interject_text": text}
    msg.update(extra)
    return msg


def _drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


# ============ ChatBackend.withdraw_interject ============


def test_withdraw_interject_removes_pending_entry():
    """撤回插话：队列中对应条目被摘除，其余条目（hook 通知/其他插话）原样保留"""
    q = queue.Queue()
    q.put(_interject("就拿当前项目"))
    q.put({"role": "user", "content": "post-tool hook"})
    q.put(_interject("另一条插话"))
    backend = _make_backend(q)

    assert backend.withdraw_interject("就拿当前项目") == 1

    remaining = _drain(q)
    texts = [m.get("content") for m in remaining]
    assert "就拿当前项目" not in texts, "已撤回的插话必须从队列摘除，否则会被下个 worker 复活"
    assert "post-tool hook" in texts, "非插话 hook 条目不得受影响"
    assert "另一条插话" in texts, "其他插话不得受影响"


def test_withdraw_interject_no_match_keeps_queue():
    """无匹配（worker 已消费 / 已回收）时返回 0，队列原样"""
    q = queue.Queue()
    q.put(_interject("还留在队列"))
    backend = _make_backend(q)

    assert backend.withdraw_interject("已被消费的文本") == 0
    assert [m.get("_interject_text") for m in _drain(q)] == ["还留在队列"]


def test_withdraw_interject_takes_latest_duplicate():
    """同文本多条插话：倒序取最近入队的一条（与幽灵卡倒序定位口径一致）"""
    q = queue.Queue()
    older = _interject("重复文本", _tag="older")
    newer = _interject("重复文本", _tag="newer")
    q.put(older)
    q.put(newer)
    backend = _make_backend(q)

    assert backend.withdraw_interject("重复文本") == 1
    remaining = _drain(q)
    assert [m.get("_tag") for m in remaining] == ["older"], "应摘除最近一条，保留更早的一条"


# ============ ChatBackend.reclaim_pending_interjects ============


def test_reclaim_pending_interjects_moves_to_stash():
    """停止链路立即回收：插话进 stash 供回填，TeamMail 丢弃，其他通知放回队列"""
    q = queue.Queue()
    q.put(_interject("中止时还在队列"))
    q.put({"role": "user", "content": "邮件", "_hook_event": "TeamMail"})
    q.put({"role": "user", "content": "子智能体完成"})
    backend = _make_backend(q)

    assert backend.reclaim_pending_interjects() == 1

    recovered = backend.take_recovered_interjects()
    assert [m.get("_interject_text") for m in recovered] == ["中止时还在队列"], "插话必须转入 stash 供停止回填"
    remaining = _drain(q)
    assert [m.get("content") for m in remaining] == ["子智能体完成"], "TeamMail 丢弃，其他通知保留"
    assert backend.take_recovered_interjects() == [], "stash 取出后清空"


def test_reclaim_pending_interjects_empty_queue_noop():
    """队列为空时返回 0 且不产生 stash 条目"""
    backend = _make_backend(queue.Queue())
    assert backend.reclaim_pending_interjects() == 0
    assert backend.take_recovered_interjects() == []


# ============ Worker 退出兜底 ============


def _make_worker_with_queue(q):
    worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
    worker.tool_executor = SimpleNamespace(_backend=_make_backend(q))
    worker._is_cancelled = True
    return worker


def test_worker_exit_reclaims_interjects():
    """worker 取消退出兜底：队列里的插话被回收进 backend stash（不丢、不留给下个 worker）"""
    q = queue.Queue()
    q.put(_interject("卡在工具执行时入队"))
    worker = _make_worker_with_queue(q)

    worker._reclaim_pending_interjects_on_exit()

    backend = worker.tool_executor._backend
    assert [m.get("_interject_text") for m in backend.take_recovered_interjects()] == ["卡在工具执行时入队"]
    assert q.qsize() == 0


def test_run_finally_calls_reclaim_on_cancel():
    """源码契约：run() 退出路径（finally）必须兜底回收，且以 _is_cancelled 守卫

    主循环 `while not self._is_cancelled` 直接退出时不会经过
    `_cancel_with_stop_hook`，这是 2026-10-29 残留插话复活的直接路径。
    """
    src = inspect.getsource(OpenAIChatWorker.run)
    assert "_reclaim_pending_interjects_on_exit" in src, (
        "run() 退出时必须兜底回收 hook 队列中的插话（取消退出不经过 _cancel_with_stop_hook）"
    )
    finally_pos = src.rfind("finally:")
    assert finally_pos != -1, "run() 应有 finally 收尾块"
    assert src.find("_reclaim_pending_interjects_on_exit", finally_pos) != -1, (
        "兜底回收必须放在 finally 块内，覆盖所有退出路径"
    )
    assert "_is_cancelled" in src[finally_pos:], "只在取消退出时回收（正常完成路径的队列收尾已有独立语义）"


# ============ 端到端：撤回后下一个 worker 不再消费 ============


def _make_consume_worker(backend):
    """构造驱动 _inject_pending_hook_messages 的 worker 桩

    queued_user_injected 信号写进实例 __dict__：__new__ 构造的 PyQt 对象读未初始化
    的 C++ 属性会抛 RuntimeError（super-class __init__ never called），__dict__ 优先命中。
    """
    worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
    worker.tool_executor = SimpleNamespace(_backend=backend)
    worker._current_session_messages = []
    worker._api_messages_cache = []
    worker.session_id = "test-session"
    worker._trigger_interject_user_hooks = lambda m: [m]
    worker._append_to_api_cache = lambda msgs: None
    worker._emit_with_callback = lambda *a, **k: None
    worker.__dict__["queued_user_injected"] = None
    return worker


def test_withdrawn_interject_not_consumed_by_next_worker():
    """撤回后新 worker 首轮消费：队列里已无该插话 → 不注入、不复活"""
    q = queue.Queue()
    q.put(_interject("被用户撤回的插话"))
    backend = _make_backend(q)

    assert backend.withdraw_interject("被用户撤回的插话") == 1

    worker = _make_consume_worker(backend)
    worker._inject_pending_hook_messages(include_team_mail=False)

    injected_texts = [str(m.get("content")) for m in worker._current_session_messages]
    assert "被用户撤回的插话" not in injected_texts, "已撤回插话不得被下一个 worker 注入（双卡片根因）"


def test_unwithdrawn_interject_still_consumed():
    """对照：未撤回的插话照常被消费（修复不得破坏正常插话）"""
    q = queue.Queue()
    q.put(_interject("正常插话"))
    backend = _make_backend(q)

    worker = _make_consume_worker(backend)
    worker._inject_pending_hook_messages(include_team_mail=False)

    injected_texts = [str(m.get("content")) for m in worker._current_session_messages]
    assert "正常插话" in injected_texts
