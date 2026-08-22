# -*- coding: utf-8 -*-
"""HookPolicy 规范化 + EngineSession 契约测试

覆盖：
1. ConversationConfig.hook_policy 默认 ALL（UI/Gateway 零影响）
2. ChatWorker 归一化 hook_policy（None → ALL 兼容旧调用方）
3. 消息级拦截：policy != ALL 时 _trigger_worker_hook 短路返回 None
4. 工具级传参：policy NONE → _execute_tool 传 trigger_hooks=False
5. tool_executor.execute(trigger_hooks=False) 跳过 PreToolUse/PostToolUse
6. EngineSession 契约：默认 hook_policy=NONE、通用 turn() 语义、逃生舱
"""


def test_hook_policy_enum_values():
    from app.core.conversation.config import HookPolicy

    assert HookPolicy.ALL.value == "all"
    assert HookPolicy.TOOL_EVENTS_ONLY.value == "tool_events_only"
    assert HookPolicy.NONE.value == "none"


def test_conversation_config_default_hook_policy_all():
    """默认 ALL：既有 UI/Gateway 引擎行为零变化"""
    from app.core.conversation.config import ConversationConfig, HookPolicy

    config = ConversationConfig()
    assert config.hook_policy == HookPolicy.ALL


def test_chat_worker_normalizes_hook_policy():
    """ChatWorker 构造：None/非法值归一化为 ALL（兼容旧调用方）"""
    from app.core.conversation.config import HookPolicy
    from app.core.workers.chat_worker import OpenAIChatWorker

    def make(policy):
        return OpenAIChatWorker(
            messages=[{"role": "user", "content": "hi"}],
            session_messages=[],
            llm_config={},
            hook_policy=policy,
        )

    assert make(None)._hook_policy == HookPolicy.ALL
    assert make("garbage")._hook_policy == HookPolicy.ALL
    assert make(HookPolicy.NONE)._hook_policy == HookPolicy.NONE


def test_worker_hook_blocked_by_policy():
    """消息级拦截：policy != ALL 时 _trigger_worker_hook 直接返回 None（不触发任何全局 hook）"""
    from app.core.conversation.config import HookPolicy
    from app.core.workers.chat_worker import OpenAIChatWorker

    worker = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={},
        hook_policy=HookPolicy.NONE,
    )

    # tool_executor 传一个桩，若 hook 未被拦截则会在取 _backend 时走到
    # trigger_event——桩上放哨兵验证不会被调用
    class _Sentinel:
        triggered = False

        def trigger_event(self, *a, **k):
            _Sentinel.triggered = True
            return []

    class _HM:
        hook_manager = _Sentinel()

    class _TE:
        _backend = _HM()

    worker.tool_executor = _TE()
    ret = worker._trigger_worker_hook("Stop", [], [])
    assert ret is None
    assert _Sentinel.triggered is False


def test_executor_passes_hook_policy_to_worker():
    """ConversationExecutor 把 config.hook_policy 传进 worker_kwargs"""
    from unittest.mock import MagicMock

    from app.core.conversation.config import ConversationConfig, HookPolicy
    from app.core.conversation.executor import ConversationExecutor

    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.finished = MagicMock()
        m.finished.connect = MagicMock()
        m.start = MagicMock()
        return m

    core = MagicMock()
    session = MagicMock()
    session.session_id = "s1"
    session.get_context_messages.return_value = []
    core.session_manager.get_current_session.return_value = session

    ex = ConversationExecutor(
        core=core,
        config=ConversationConfig(hook_policy=HookPolicy.NONE),
        tool_executor=None,
        agent_manager=None,
        worker_factory=factory,
    )
    ex.execute(messages=[], llm_config={}, tools=[])
    assert captured.get("hook_policy") == HookPolicy.NONE


def test_tool_executor_trigger_hooks_false_skips_hooks():
    """trigger_hooks=False：PostToolUse 不触发（enabled 短路 + 无 backend 跳过）"""
    from app.core.tool_executor import ToolExecutor

    te = ToolExecutor.__new__(ToolExecutor)
    calls = []

    class _HM:
        def trigger_event(self, *a, **k):
            calls.append(a[0] if a else k.get("event_name"))
            return []

    class _Backend:
        hook_manager = _HM()
        session_manager = None

    te._backend = _Backend()
    te._workdir = ""

    te._trigger_post_tool_use("read", {}, None, enabled=False)
    assert calls == []

    te._trigger_post_tool_use("read", {}, None, enabled=True)
    assert calls == ["PostToolUse"]

    # 无 backend 时不触发（既有行为）
    calls.clear()
    te._backend = None
    te._trigger_post_tool_use("read", {}, None, enabled=True)
    assert calls == []


# ============================================================
# EngineSession 契约
# ============================================================


def test_chat_result_semantics():
    from app.core.conversation.engine_session import ChatResult

    assert ChatResult(text="x").ok is True
    assert ChatResult(error="boom").ok is False
    assert ChatResult(cancelled=True).ok is False
    assert ChatResult(timed_out=True).ok is False


def test_engine_session_contract_importable():
    from app.plugins.contracts.engine_session import ChatResultLike, EngineSession

    assert hasattr(EngineSession, "__protocol_attrs__")
    assert hasattr(ChatResultLike, "__protocol_attrs__")


def test_engine_session_defaults_and_escape_hatch():
    """EngineSession：字符串策略映射 + 逃生舱（core/executor）公开"""
    from app.core.conversation.config import HookPolicy
    from app.core.conversation.engine_session import EngineSessionImpl
    from app.plugins.contracts.engine_session import EngineSession

    s = EngineSessionImpl(
        engine_name="t",
        get_model_config=lambda: {},
        hook_policy="none",
        permission_strategy="auto_allow",
    )
    assert isinstance(s, EngineSession)
    assert s.engine_name == "t"
    # 逃生舱公开：插件可直接操作完整执行栈
    assert s.core is not None
    assert s.executor is not None
    # hook_policy 经 config 声明（引擎决定）
    assert s.executor._config.hook_policy == HookPolicy.NONE
    s.cleanup()


def test_engine_session_turn_requires_messages_or_user():
    """messages 与 user 均为空 → 报错结果"""
    from app.core.conversation.engine_session import EngineSessionImpl

    s = EngineSessionImpl(engine_name="t", get_model_config=lambda: {})
    r = s.turn()
    assert r.ok is False
    assert "messages" in (r.error or "")
    s.cleanup()


def test_engine_session_auto_history_accumulation():
    """auto_history：history 可读写，追加语义由调用方选择"""
    from app.core.conversation.engine_session import EngineSessionImpl

    s = EngineSessionImpl(engine_name="t", get_model_config=lambda: {})
    assert s.history == []
    s.history.extend([{"role": "user", "content": "seed"}])
    assert len(s.history) == 1
    s.cleanup()
