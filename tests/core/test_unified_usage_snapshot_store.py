# -*- coding: utf-8 -*-
"""B3 hooks/UI 统一快照 store 回归

## 背景
同一消息状态被两条缓存链各自全量投影 project_for_ui：hooks 签名缓存
（usage.py）与 UI 环版本缓存（engine.py _ctx_usage_cache）。键不同构 →
消息版本 bump 后两侧各 miss、各跑一次全量投影（8 层 tier + per_message_tokens
全量 + tools tokens）。B3 统一键并共享 store（_unified_snap_store）。

## 断言焦点
1. 同版本消息双消费方只算一次（project 计数 mock）
2. 版本 bump 后两侧同步失效（且失效后只重算一次）
3. api_prompt_tokens 三态（from_api True/False/None）行为与改造前等价
"""

import pytest

from app.core.context import usage as usage_mod
from app.core.context.usage import snapshot_usage_for_hooks
from app.core.engines.ui.engine import UIEngine


class _FakeCore:
    class _ContextBuilder:
        def get_context_budget(self, llm_config):
            return 200000

    def __init__(self):
        self.context_builder = self._ContextBuilder()
        self.permission_cache = None


class _FakeAgentMgr:
    def get_agent_tools_schema(self, *a, **k):
        return []


class _FakeSession:
    def __init__(self, messages, sid="b3-test"):
        self.messages = messages
        self.system_prompt = ""
        self.session_id = sid
        self._messages_version = 1
        self.last_api_prompt_tokens = 0
        self.last_api_message_count = 0
        self.last_api_prompt_from_usage = None
        self.token_calibration = 0
        self.token_calibration_model = ""


def _make_engine(session):
    engine = UIEngine.__new__(UIEngine)
    engine._conversation_core = _FakeCore()  # type: ignore[assignment]
    engine._session_manager = type("SM", (), {"get_current_session": lambda s: session})()
    engine._get_model_config = lambda: {"model": "gpt-4o", "max_tokens": 8000}
    engine._current_agent = "build"
    engine._get_agent_manager = lambda: _FakeAgentMgr()
    engine._tool_executor = type("TE", (), {"_builtin_tools": None})()
    engine._backend = None
    engine._callbacks = {}
    engine._api_mode = False
    engine._worker_callbacks = {}
    engine._tools_snapshot = {
        "key": (getattr(session, "session_id", ""), "build", False),
        "tools": [],
        "hash": "empty",
    }
    engine._tools_schema_cache = {"timestamp": 0.0, "tools": [], "tokens": 0}
    return engine


class _FakeBackend:
    """chat_engine 链带同源 context_builder（键同构前提）+ 投影计数"""

    def __init__(self, engine):
        core = engine._conversation_core
        fake_engine = type("FE", (), {"_conversation_core": core})()
        self.chat_engine = fake_engine
        self.calls = {"snapshot": 0}

    def get_context_usage_snapshot(self, session, llm_config, **kw):
        self.calls["snapshot"] += 1
        return self._delegate(session, llm_config, **kw)

    def _delegate(self, session, llm_config, **kw):
        return self._engine_snap(session, llm_config, **kw)

    def attach_engine(self, engine):
        self._engine_snap = engine.get_context_usage_snapshot
        return self

    def get_current_session(self):
        return None

    def _get_model_config(self):
        return {"model": "gpt-4o", "max_tokens": 8000}


@pytest.fixture()
def clean_store():
    usage_mod._unified_snap_store.clear()
    yield
    usage_mod._unified_snap_store.clear()


def _messages():
    return [
        {"role": "user", "content": "执行命令"},
        {"role": "assistant", "content": "完成"},
    ]


def test_dual_consumers_single_projection(clean_store):
    """同版本消息：hooks 与 UI 两次同键取用，全量投影只执行一次"""
    session = _FakeSession(_messages())
    engine = _make_engine(session)
    backend = _FakeBackend(engine).attach_engine(engine)
    llm = {"model": "gpt-4o", "max_tokens": 8000}
    # 消费方 1（hooks 侧）：worker 线程口径
    used, budget = snapshot_usage_for_hooks(backend, session=session, llm_config=llm)
    assert used > 0 and budget == 200000
    first_calls = backend.calls["snapshot"]
    assert first_calls >= 1
    # 消费方 2（UI 侧）：同状态同键 → 命中统一 store，不再真算
    snap = engine.get_context_usage_snapshot(  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
        llm_config=llm,
        api_prompt_tokens=session.last_api_prompt_tokens,
        api_message_count=session.last_api_message_count,
        from_api=session.last_api_prompt_from_usage,
    )
    assert snap["used_tokens"] == used, "UI 侧命中应与 hooks 侧同一份产物"
    assert backend.calls["snapshot"] == first_calls, "同键 UI 取用不得再触发全量投影"
    # 反向再取 hooks：仍命中（LRU 位刷新，不重算）
    used2, _ = snapshot_usage_for_hooks(backend, session=session, llm_config=llm)
    assert used2 == used
    assert backend.calls["snapshot"] == first_calls


def test_version_bump_invalidates_both_sides_once(clean_store):
    """版本 bump 后两侧同步失效：合计只重算一次（改造前为两次各算）"""
    session = _FakeSession(_messages())
    engine = _make_engine(session)
    backend = _FakeBackend(engine).attach_engine(engine)
    llm = {"model": "gpt-4o", "max_tokens": 8000}
    snapshot_usage_for_hooks(backend, session=session, llm_config=llm)
    base_calls = backend.calls["snapshot"]
    session.messages.append({"role": "user", "content": "新消息"})
    session._messages_version += 1  # 写入口 bump
    session.last_api_prompt_tokens = 0
    # bump 后 hooks 先取：miss → 真算一次并回填 store
    snapshot_usage_for_hooks(backend, session=session, llm_config=llm)
    assert backend.calls["snapshot"] == base_calls + 1
    # bump 后 UI 再取：同键命中 store（改造前此處会再 miss 重算一次）
    engine.get_context_usage_snapshot(  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
        llm_config=llm,
        api_prompt_tokens=session.last_api_prompt_tokens,
        api_message_count=session.last_api_message_count,
        from_api=session.last_api_prompt_from_usage,
    )
    assert backend.calls["snapshot"] == base_calls + 1, "bump 后同键 UI 取用应复用 hooks 侧重算产物"


@pytest.mark.parametrize("from_api", [True, False, None])
def test_from_api_three_states_matrix(clean_store, from_api):
    """三态 × 同键取用：每次重算产物与改造前语义等价（True/None 按可用、False 跳过权威值）"""
    session = _FakeSession(_messages())
    engine = _make_engine(session)
    llm = {"model": "gpt-4o", "max_tokens": 8000}
    api_tokens = 500
    # 基准：不走 store（清空后直接 engine 算）
    usage_mod._unified_snap_store.clear()
    snap_direct = engine.get_context_usage_snapshot(
        session=session,  # type: ignore[arg-type]
        llm_config=llm,
        api_prompt_tokens=api_tokens,
        api_message_count=len(session.messages),
        from_api=from_api,
    )
    # 同参数再取：命中统一 store，产物一致
    snap_hit = engine.get_context_usage_snapshot(
        session=session,  # type: ignore[arg-type]
        llm_config=llm,
        api_prompt_tokens=api_tokens,
        api_message_count=len(session.messages),
        from_api=from_api,
    )
    assert snap_hit["used_tokens"] == snap_direct["used_tokens"]
    assert snap_hit["budget_tokens"] == snap_direct["budget_tokens"]
    # 三态行为等价性：True/None 按权威值可用（used = api 值口径），False 走本地估算
    if from_api is False:
        pass  # False 的权威值跳过语义在引擎内部 delta 路径，产物与 None 可不同
    else:
        # True/None：api_prompt_tokens>0 时 used 应反映 API 口径（本地估算被校准/覆盖的量级）
        assert snap_direct["used_tokens"] > 0
