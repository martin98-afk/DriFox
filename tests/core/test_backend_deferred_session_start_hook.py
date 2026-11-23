# -*- coding: utf-8 -*-
"""backend.trigger_session_start_hook 回归（实施单 #R5）。

覆盖：
- create_session(trigger_hook=False) 不触发 SessionStart（可交互判定点不等 575ms 的机制前提）
- trigger_session_start_hook 成功路径：context 构建 / trigger_event 调用 / 注入 / emit
- hook_manager 缺失静默返回（兜底不抛）
- trigger_event 抛异常向上传播（调用方 _run_deferred_session_start_hook 的 try/finally 负责放行）

实现说明：ChatBackend 继承 QObject（禁止 object.__new__ 绕过），故以 MagicMock
为宿主、显式传 self 调用真方法——方法体逻辑真实执行，协作属性由 mock 提供。
"""

from unittest.mock import MagicMock, patch

from app.core.conversation.backend import ChatBackend
from app.core.conversation import backend as backend_mod

def _bare_backend(hook_manager=None):
    """Mock 宿主：以 MagicMock 为 self 调用真方法（逻辑真实执行，协作属性 mock）"""
    b = MagicMock()
    b._session_manager = MagicMock()
    b._hook_manager = hook_manager
    b._build_session_context = MagicMock(return_value={"startup": True})
    return b

def _session():
    s = MagicMock()
    s.session_id = "sid-1"
    return s

def _hook_manager(results=None):
    hm = MagicMock()
    hm.trigger_event.return_value = results if results is not None else []
    return hm

def test_create_session_false_skips_hook_segment():
    """trigger_hook=False：不建 context、不调 trigger_event、不 emit（R5 机制前提）"""
    hm = _hook_manager()
    b = _bare_backend(hm)
    b._session_manager.create_new_session.return_value = _session()

    with patch.object(ChatBackend, "trigger_session_start_hook") as spy_hook:
        ChatBackend.create_session(b, trigger_hook=False)

    spy_hook.assert_not_called()
    hm.trigger_event.assert_not_called()
    b._build_session_context.assert_not_called()
    b.session_created.emit.assert_called_once_with("sid-1")

def test_create_session_true_delegates_to_hook_exit():
    """trigger_hook=True：委托给 trigger_session_start_hook（原语义不变）"""
    hm = _hook_manager()
    b = _bare_backend(hm)
    created = _session()
    b._session_manager.create_new_session.return_value = created

    # 宿主是 MagicMock：create_session 内 self.trigger_session_start_hook 走
    # 实例属性查找 → 断言 mock 宿主上的调用（与运行期实例走类方法等价）
    ChatBackend.create_session(b, trigger_hook=True)

    b.trigger_session_start_hook.assert_called_once_with(created)

def test_trigger_session_start_hook_success_injects_and_emits():
    """成功路径：context 带 session_id、结果逐条注入、emit 收尾"""
    ok = MagicMock(success=True, output="injected-context", status_message="ok")
    bad = MagicMock(success=False, output="should-not-inject", status_message="err")
    hm = _hook_manager([ok, bad])
    b = _bare_backend(hm)
    session = _session()

    with patch.object(backend_mod, "_inject_hook_to_session") as spy_inject:
        ChatBackend.trigger_session_start_hook(b, session)

    ctx = b._build_session_context.return_value
    assert ctx["session_id"] == "sid-1"
    hm.trigger_event.assert_called_once()
    kwargs = hm.trigger_event.call_args.kwargs
    assert kwargs["context"] is ctx and kwargs["current_message"] == ""
    # 只注入成功结果
    assert spy_inject.call_count == 1
    assert spy_inject.call_args.args[1] == "SessionStart"
    assert spy_inject.call_args.args[2] == "injected-context"
    b._hook_messages_updated.emit.assert_called_once()

def test_trigger_session_start_hook_without_hook_manager_is_noop():
    """hook_manager 缺失：静默返回不抛（兜底路径，调用线程 finally 正常放行）"""
    b = _bare_backend(None)
    ChatBackend.trigger_session_start_hook(b, _session())  # 不抛即过
    b._hook_messages_updated.emit.assert_not_called()

def test_trigger_event_exception_propagates():
    """trigger_event 抛错向上传播：由调用方 _run_deferred_session_start_hook 的
    try/except/finally 负责兜底（日志 + Event 复位放行发送）"""
    hm = _hook_manager()
    hm.trigger_event.side_effect = RuntimeError("hook down")
    b = _bare_backend(hm)
    try:
        ChatBackend.trigger_session_start_hook(b, _session())
        raise AssertionError("应向上传播")
    except RuntimeError:
        pass
    b._hook_messages_updated.emit.assert_not_called()
