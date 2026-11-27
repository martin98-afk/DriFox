"""子任务先完成工具时，主会话仍须收到插话。"""
import queue
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

from PyQt5.QtCore import QObject

from app.core.tools.tool_executor import ToolExecutor
from app.core.workers.chat_worker import OpenAIChatWorker
from app.core.workers.subagent_worker import SubAgentExecutor
from app.tools import ToolResult


def test_child_finishes_tool_before_parent_consumes_interjection(monkeypatch):
    backend = NS(_hook_message_queue=queue.Queue(), _pre_tool_message_queue=queue.Queue())
    backend._hook_message_queue.put({'role': 'user', 'content': '改看这个问题', '_interject': True})
    pre = {'role': 'user', 'content': '父任务工具提示', '_hook_event': 'PreToolUse'}
    backend._pre_tool_message_queue.put(pre)
    executor = NS(_backend=backend, execute=Mock(return_value=ToolResult(True, content='完成')))
    child = SubAgentExecutor(task_id='child', agent_name='build', task_description='测试',
                             llm_config={}, agent_manager=None, tool_executor=executor)
    monkeypatch.setattr(child, '_check_ui_tool_permission', lambda *a: 'allow')
    monkeypatch.setattr(child, '_serialize_for_api', lambda msgs: NS(messages=msgs))
    _, child_messages = child._execute_tools([{'id': 'c1', 'function': {'name': 'read', 'arguments': {}}}])
    assert child_messages == []
    assert backend._pre_tool_message_queue.get_nowait() == pre

    parent = OpenAIChatWorker.__new__(OpenAIChatWorker)
    QObject.__init__(parent)
    parent.tool_executor = executor
    parent.session_id = 'parent'
    parent._live_messages = []
    parent._append_to_api_cache = Mock()
    parent._trigger_interject_user_hooks = lambda m: [m]
    parent._emit_with_callback = Mock()
    session = []
    for _ in range(2):
        parent._inject_pending_hook_messages(session, include_team_mail=False)
    assert session == parent._live_messages == [{'role': 'user', 'content': '改看这个问题'}]
    parent._append_to_api_cache.assert_called_once_with(session)
    assert [(c.args[0], c.args[2]) for c in parent._emit_with_callback.call_args_list] == [('queued_user_injected', 1)]


def test_tool_hooks_delivered_to_caller_and_replace_does_not_drain_parent(monkeypatch):
    def hook(text):
        return NS(output=text, success=True, decision='continue', status_message='', add_to_context=True)

    replace = '{"hookSpecificOutput":{"hookEventName":"PostToolUse","replaceContent":"处理后的结果"}}'
    hm = Mock()
    hm.trigger_event.side_effect = lambda event, **kw: [hook('调用前')] if event == 'PreToolUse' else [hook(replace), hook('调用后')]
    backend = NS(hook_manager=hm, session_manager=None, _hook_message_queue=queue.Queue(),
                 _pre_tool_message_queue=queue.Queue(), _hook_messages_updated=Mock())
    pending = {'role': 'user', '_interject': True, 'content': '留给主会话'}
    backend._hook_message_queue.put(pending)
    te = ToolExecutor.__new__(ToolExecutor)
    te._backend, te._workdir, te._lock = backend, '', threading.Lock()
    te._session_id, te._call_id = 'parent', 'c1'
    te._builtin_tools = NS(mcp_manager=NS(is_connected=True, call_tool_sync=lambda *a: ToolResult(True, content='原始结果')))
    for name, value in [('is_valid', True), ('_try_recover_mcp_prefix', None), ('_get_required_args', []),
                        ('_record_file_operation_before', None), ('_check_team_member', False),
                        ('_get_context_usage_info', (0, 0))]:
        monkeypatch.setattr(te, name, Mock(return_value=value))
    child = []
    result = te.execute('mcp__test__read', {}, hook_messages=child,
                        hook_context={'current_role': 'subagent', 'is_subagent_call': True})
    assert result.content == '处理后的结果'
    assert [m['_hook_event'] for m in child] == ['PreToolUse', 'PostToolUse']
    assert '调用前' in child[0]['content'] and '调用后' in child[1]['content']
    assert all(c.kwargs['skip_finished_callback'] for c in hm.trigger_event.call_args_list)
    assert backend._hook_message_queue.get_nowait() is pending
    assert backend._hook_message_queue.empty() and backend._pre_tool_message_queue.empty()

