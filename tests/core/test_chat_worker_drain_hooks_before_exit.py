# -*- coding: utf-8 -*-
"""退出前 hook 队列收尾决策 — _drain_pending_hooks_before_exit 回归测试。

修复背景：主对话最后一轮（无下一轮 API 调用）期间到达的用户插话 / TeamMail /
SubAgentFinished 被「退出前最后消费」吞成孤儿——消息进入消息列表但 LLM 永远
不响应，插话触发的 queued_user_injected 还会让 UI 开出一张永远等不到流的空卡。
修复后队列非空、max_rounds 配额未耗尽且策略允许时注入并续跑一轮。

注：测试用 fake 覆盖 _inject_pending_hook_messages，屏蔽了其内层的
queued_user_injected 信号发射（那是注入方法的职责，不在本方法决策面内）。
"""

import queue
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QObject


def _make_worker(policy):
    """构造最小可用 worker：__new__ 绕过 __init__，仅注入本方法依赖的状态。"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    w = worker_cls.__new__(worker_cls)
    QObject.__init__(w)  # 补 QObject 初始化，否则访问 pyqtSignal 描述器报 super-class __init__ 未调用
    w.tool_executor = SimpleNamespace(_backend=SimpleNamespace(_hook_message_queue=queue.Queue()))
    w._loop_policy_obj = policy
    w.llm_config = {}  # __new__ 绕过 __init__，max_rounds 预检需要
    w._loop_round_count = 3
    # 记录注入调用（实例属性覆盖方法，不触发真实队列消费/TeamMail 逻辑）
    w._inject_calls = []
    w._inject_pending_hook_messages = lambda **k: w._inject_calls.append(k)
    # 记录信号发射（覆盖 _emit_with_callback，绕开事件总线与 PyQt 信号）
    w._emit_calls = []
    w._emit_with_callback = lambda name, sig, *args: w._emit_calls.append((name, args))
    return w


class _DefaultLikePolicy:
    """默认策略同构：stop_hook_injected=True → CONTINUE，否则 STOP；不限轮数"""

    id = "default-like"

    def should_continue(self, state):
        from app.plugins.contracts.loop_policy import LoopDecision

        return LoopDecision.CONTINUE if state.stop_hook_injected else LoopDecision.STOP

    def max_rounds(self, llm_config):
        return None


class _AlwaysStopPolicy:
    """SingleTurn/Minimal 同构：恒 STOP"""

    id = "always-stop"

    def should_continue(self, state):
        from app.plugins.contracts.loop_policy import LoopDecision

        return LoopDecision.STOP

    def max_rounds(self, llm_config):
        return 1


class _BoomPolicy:
    id = "boom"

    def should_continue(self, state):
        raise RuntimeError("boom")

    def max_rounds(self, llm_config):
        return None


class _LimitedContinuePolicy:
    """恒 CONTINUE 但轮数上限有限（配额耗尽分支专用）"""

    id = "limited-continue"

    def __init__(self, max_rounds=1):
        self._max_rounds = max_rounds

    def should_continue(self, state):
        from app.plugins.contracts.loop_policy import LoopDecision

        return LoopDecision.CONTINUE

    def max_rounds(self, llm_config):
        return self._max_rounds


def test_queue_empty_takes_finish_path():
    """队列空 → 完成路径（False），兜底消费 include_team_mail=False"""
    w = _make_worker(_DefaultLikePolicy())
    msgs = [{"role": "assistant", "content": "done"}]
    assert w._drain_pending_hooks_before_exit(msgs) is False
    assert w._inject_calls == [{"session_messages_target": msgs, "include_team_mail": False}]
    assert w._emit_calls == []


def test_interject_pending_continues_round():
    """队列有插话 + 默认策略 → 注入（含邮件）并续轮（True），发 finished_with_messages"""
    w = _make_worker(_DefaultLikePolicy())
    w.tool_executor._backend._hook_message_queue.put({"role": "user", "content": "插话", "_interject": True})
    msgs = [{"role": "assistant", "content": "partial"}]
    assert w._drain_pending_hooks_before_exit(msgs) is True
    assert w._inject_calls == [{"session_messages_target": msgs, "include_team_mail": True}]
    assert w._emit_calls == [("finished_with_messages", (msgs,))]


def test_policy_stop_rejects_continuation():
    """恒 STOP 策略拒绝续轮 → 完成路径（False），兜底 include_team_mail=False"""
    w = _make_worker(_AlwaysStopPolicy())
    w.tool_executor._backend._hook_message_queue.put(
        {"role": "user", "content": "SubAgentFinished 通知", "_hook_event": "SubAgentFinished"}
    )
    msgs = []
    assert w._drain_pending_hooks_before_exit(msgs) is False
    assert w._inject_calls == [{"session_messages_target": msgs, "include_team_mail": False}]
    assert w._emit_calls == []


def test_quota_exhausted_rejects_continuation():
    """策略 CONTINUE 但 max_rounds 配额耗尽 → 拒绝续轮，兜底 include_team_mail=False。

    防回归：若无预检，消息注入后才在 while 顶部撞 _check_loop_round_limit 超限，
    消息重新孤儿化且 finished_with_messages 与超限完成路径重复发射。
    """
    w = _make_worker(_LimitedContinuePolicy(max_rounds=1))
    w._loop_round_count = 1  # 下一轮计数 2 > 1 → 配额耗尽
    w.tool_executor._backend._hook_message_queue.put({"role": "user", "content": "插话"})
    msgs = []
    assert w._drain_pending_hooks_before_exit(msgs) is False
    assert w._inject_calls == [{"session_messages_target": msgs, "include_team_mail": False}]
    assert w._emit_calls == []


def test_quota_available_allows_continuation():
    """策略 CONTINUE 且配额未耗尽（计数+1 == max_rounds 边界）→ 正常续轮"""
    w = _make_worker(_LimitedContinuePolicy(max_rounds=4))
    w._loop_round_count = 3  # 下一轮计数 4，未超限
    w.tool_executor._backend._hook_message_queue.put({"role": "user", "content": "插话"})
    assert w._drain_pending_hooks_before_exit([]) is True
    assert w._inject_calls == [{"session_messages_target": [], "include_team_mail": True}]


def test_policy_exception_falls_back_continue():
    """策略 should_continue 抛异常 → 回退 CONTINUE 续轮（与 Stop hook 门控同构）"""
    w = _make_worker(_BoomPolicy())
    w.tool_executor._backend._hook_message_queue.put({"role": "user", "content": "插话"})
    assert w._drain_pending_hooks_before_exit([]) is True
    assert w._inject_calls == [{"session_messages_target": [], "include_team_mail": True}]
    assert w._emit_calls == [("finished_with_messages", ([],))]


def test_cancelled_reclaims_interject_without_continuation():
    """已取消 → 不注入不续跑（False），_interject 条目 stash 回收，其余放回队列

    场景（2026-10-10 双气泡根因）：第一轮流式自然结束后 worker 仍在完成路径
    （Stop hook 外部调用耗时），用户点停止（_is_cancelled=True）并撤回插话卡。
    退出前收尾若照旧注入续跑，已撤回的插话会被复活出回复卡，且
    finished_with_messages 把已撤回消息写回 session（数据复活）。
    取消 = 一切中止：插话 stash 回收交 UI 停止链路统一回填输入框。
    """
    w = _make_worker(_DefaultLikePolicy())
    w._is_cancelled = True
    backend = w.tool_executor._backend
    stashed = []
    backend.stash_recovered_interjects = lambda items: stashed.extend(items)
    q = backend._hook_message_queue
    q.put({"role": "user", "content": "插话", "_interject": True})
    q.put({"role": "user", "content": "通知", "_hook_event": "SubAgentFinished"})
    q.put({"role": "user", "content": "邮件", "_hook_event": "TeamMail"})

    assert w._drain_pending_hooks_before_exit([]) is False
    assert len(stashed) == 1 and stashed[0].get("_interject") is True, "插话必须 stash 回收"
    assert q.qsize() == 1, "非插话条目应放回队列供下一对话消费"
    leftover_back = q.get_nowait()
    assert leftover_back.get("_hook_event") == "SubAgentFinished", "放回的应只有非 TeamMail 通知"
    assert w._emit_calls == [], "取消路径不得发射续跑信号"
    assert w._inject_calls == [], "取消路径不得注入消息到对话流"


def test_not_cancelled_still_continues_round():
    """未取消 → 行为不变（对照）：队列有插话仍注入续跑"""
    w = _make_worker(_DefaultLikePolicy())
    w._is_cancelled = False
    w.tool_executor._backend._hook_message_queue.put({"role": "user", "content": "插话", "_interject": True})
    assert w._drain_pending_hooks_before_exit([]) is True
    assert w._inject_calls == [{"session_messages_target": [], "include_team_mail": True}]


def test_missing_backend_takes_finish_path():
    """tool_executor/backend/队列缺失 → 完成路径不炸（防御风格一致）"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    w = worker_cls.__new__(worker_cls)
    QObject.__init__(w)
    w.tool_executor = SimpleNamespace(_backend=None)
    w._loop_policy_obj = _DefaultLikePolicy()
    w._inject_calls = []
    w._inject_pending_hook_messages = lambda **k: w._inject_calls.append(k)
    w._emit_with_callback = lambda *a, **k: None
    assert w._drain_pending_hooks_before_exit([]) is False
    assert w._inject_calls == [{"session_messages_target": [], "include_team_mail": False}]
