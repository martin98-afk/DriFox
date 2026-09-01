# -*- coding: utf-8 -*-
"""test_llm_client.py — assistant_hub core/llm_client 单元测试（走主对话引擎版）。

守卫三件事：
1. chat_once 确实经 services["create_engine_session"] 驱动引擎，且带
   single_turn 循环策略、无 tools、无 hook（记忆整理 = 纯文本单回合）。
2. 引擎侧超时/错误/空响应 → LLMUnavailableError（调用方静默降级）。
3. 插件注册的 loop policy id 与 llm_client 声明的常量一致（两处以字面量
   各自声明，防止改一处漏一处）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "llm_client.py"
_POLICY = _ROOT / "plugins" / "assistant_hub" / "loop_policies" / "single_turn.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load("test_llm_client_mod", _MODULE)


class _FakeResult:
    def __init__(self, text="", error=None, cancelled=False, timed_out=False):
        self.text = text
        self.error = error
        self.cancelled = cancelled
        self.timed_out = timed_out


class _FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.turns = []

    def turn(self, messages=None, *, system=None, user=None, tools=None, callbacks=None, timeout=300.0,
             auto_history=False):
        self.turns.append({"messages": list(messages or []), "tools": tools, "timeout": timeout})
        r = self.results.pop(0) if self.results else _FakeResult("默认文本")
        if isinstance(r, Exception):
            raise r
        return r

    def cleanup(self):
        pass


@pytest.fixture(autouse=True)
def _clean_state():
    m.reset_sessions()
    with m._services_lock:
        m._services.clear()
    yield
    m.reset_sessions()
    with m._services_lock:
        m._services.clear()


def _install_engine(monkeypatch, session):
    created = {}

    def _create(engine_name, **kwargs):
        created["engine_name"] = engine_name
        created["kwargs"] = kwargs
        return session

    m.set_services({"create_engine_session": _create})
    return created


# ── 1. 引擎驱动 + 单回合策略 ──────────────────────────────────────


def test_chat_once_drives_engine_with_single_turn_policy(monkeypatch):
    session = _FakeSession([_FakeResult("  整理结果  ")])
    created = _install_engine(monkeypatch, session)

    out = m.chat_once([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])

    assert out == "整理结果"
    assert created["engine_name"] == m.ENGINE_NAME
    # 引擎级循环策略：按 id 取，不碰全局激活槽
    assert created["kwargs"]["loop_policy_id"] == m.SINGLE_TURN_LOOP_POLICY_ID
    # 记忆整理：不触发全局 hooks，不放行任何工具
    assert created["kwargs"]["hook_policy"] == "none"
    assert created["kwargs"]["permission_strategy"] == "auto_deny"
    # 未指定模型 → 只叠加工具型参数（温度 0.3），模型本身跟随窗口当前选择
    assert created["kwargs"]["model_config_override"] == {"温度": m.UTILITY_TEMPERATURE}

    assert len(session.turns) == 1
    assert session.turns[0]["tools"] == []
    assert session.turns[0]["messages"][1]["content"] == "u"


def test_chat_once_passes_model_config_override(monkeypatch):
    session = _FakeSession([_FakeResult("ok")])
    created = _install_engine(monkeypatch, session)
    cfg = {"API_URL": "https://api.test/v1", "模型名称": "m1"}

    m.chat_once([{"role": "user", "content": "x"}], model_config=cfg, temperature=0.9)

    ov = created["kwargs"]["model_config_override"]
    assert ov["API_URL"] == "https://api.test/v1"
    assert ov["模型名称"] == "m1"
    assert ov["温度"] == 0.9


def test_chat_once_no_engine_service(monkeypatch):
    with pytest.raises(m.LLMUnavailableError):
        m.chat_once([{"role": "user", "content": "x"}])


def test_chat_once_empty_messages():
    with pytest.raises(m.LLMUnavailableError):
        m.chat_once([])


# ── 2. 失败语义 ──────────────────────────────────────────────────


def test_chat_once_engine_error(monkeypatch):
    _install_engine(monkeypatch, _FakeSession([_FakeResult(error="boom")]))
    with pytest.raises(m.LLMUnavailableError) as ei:
        m.chat_once([{"role": "user", "content": "x"}], retries=0)
    assert "boom" in str(ei.value)


def test_chat_once_timeout(monkeypatch):
    _install_engine(monkeypatch, _FakeSession([_FakeResult(timed_out=True)]))
    with pytest.raises(m.LLMUnavailableError) as ei:
        m.chat_once([{"role": "user", "content": "x"}], timeout=5, retries=0)
    assert "超时" in str(ei.value)


def test_chat_once_worker_exception(monkeypatch):
    _install_engine(monkeypatch, _FakeSession([RuntimeError("worker 启动失败")]))
    with pytest.raises(m.LLMUnavailableError) as ei:
        m.chat_once([{"role": "user", "content": "x"}])
    assert "worker 启动失败" in str(ei.value)


def test_chat_once_retries_empty_then_returns(monkeypatch):
    session = _FakeSession([_FakeResult(""), _FakeResult("第二次拿到了")])
    _install_engine(monkeypatch, session)
    assert m.chat_once([{"role": "user", "content": "x"}], retries=1) == "第二次拿到了"
    assert len(session.turns) == 2


def test_chat_once_retries_exhausted(monkeypatch):
    session = _FakeSession([_FakeResult(""), _FakeResult("")])
    _install_engine(monkeypatch, session)
    with pytest.raises(m.LLMUnavailableError) as ei:
        m.chat_once([{"role": "user", "content": "x"}], retries=1)
    assert "空内容" in str(ei.value)
    assert len(session.turns) == 2


# ── 3. 工具型模型覆盖参数 ─────────────────────────────────────────


def test_build_utility_override_default_temperature():
    ov = m.build_utility_override(None)
    assert ov["温度"] == m.UTILITY_TEMPERATURE
    assert "思考模式" not in ov  # 未知模型不下发 thinking（会被网关 400）


def test_build_utility_override_disables_thinking_when_supported(monkeypatch):
    monkeypatch.setattr(m, "_supports_thinking", lambda model: True)
    ov = m.build_utility_override({"模型名称": "deepseek-reasoner"})
    assert ov["思考模式"] is False


def test_build_utility_override_keeps_explicit_temperature():
    ov = m.build_utility_override({}, temperature=0.0)
    assert ov["温度"] == 0.0


# ── 4. 会话池 ────────────────────────────────────────────────────


def test_session_reused_for_same_override(monkeypatch):
    session = _FakeSession([_FakeResult("a"), _FakeResult("b")])
    _install_engine(monkeypatch, session)
    m.chat_once([{"role": "user", "content": "x"}])
    m.chat_once([{"role": "user", "content": "y"}])
    assert len(session.turns) == 2  # 同一会话复用，未重建


def test_reset_sessions_clears_pool(monkeypatch):
    """reset 后旧会话被丢弃 → 下次调用走新建会话（每次新建独立对象）。"""
    sessions = []

    def _create(engine_name, **kwargs):
        s = _FakeSession([_FakeResult("ok")])
        sessions.append(s)
        return s

    m.set_services({"create_engine_session": _create})
    m.chat_once([{"role": "user", "content": "x"}])
    m.reset_sessions()
    m.chat_once([{"role": "user", "content": "y"}])

    assert len(sessions) == 2  # 旧会话已丢弃，新建了一个
    assert len(sessions[0].turns) == 1
    assert len(sessions[1].turns) == 1


def test_different_override_gets_own_session(monkeypatch):
    sessions = []

    def _create(engine_name, **kwargs):
        s = _FakeSession([_FakeResult("ok")])
        sessions.append(s)
        return s

    m.set_services({"create_engine_session": _create})
    m.chat_once([{"role": "user", "content": "x"}])
    m.chat_once([{"role": "user", "content": "y"}], model_config={"模型名称": "other"})
    assert len(sessions) == 2


# ── 5. 循环策略一致性 ────────────────────────────────────────────


def test_single_turn_policy_matches_client_constant(monkeypatch):
    """插件注册的 policy id 必须与 llm_client 声明的常量一致。"""
    policy_mod = _load("test_assistant_hub_single_turn_policy", _POLICY)

    class _FakeRegistry:
        def __init__(self):
            self.policies = []

        def register(self, policy):
            self.policies.append(policy)

    reg = _FakeRegistry()
    policy_mod.register(reg)
    assert len(reg.policies) == 1
    policy = reg.policies[0]

    assert policy.id == m.SINGLE_TURN_LOOP_POLICY_ID
    assert policy.max_rounds({}) == 1

    from app.plugins.contracts.loop_policy import LoopDecision, LoopState

    # 任何续命理由（工具调用 / Stop hook 注入 / 重复循环）一律即停
    assert policy.should_continue(LoopState(tool_calls_found=True)) is LoopDecision.STOP
    assert policy.should_continue(LoopState(stop_hook_injected=True)) is LoopDecision.STOP
    assert policy.should_continue(LoopState(repetitive_loop_detected=True)) is LoopDecision.STOP


def test_single_turn_policy_is_discoverable_in_registry():
    """策略经 runtime_component_loader 注册后，引擎能按 id 直接取到。"""
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    registry = LoopPolicyRegistry.get_instance()
    # 若尚未扫描（单跑本文件），手动注册一份再校验按 id 取用路径
    if m.SINGLE_TURN_LOOP_POLICY_ID not in registry.policies():
        policy_mod = _load("test_assistant_hub_single_turn_policy_runtime", _POLICY)
        policy_mod.register(registry)
    assert m.SINGLE_TURN_LOOP_POLICY_ID in registry.policies()
