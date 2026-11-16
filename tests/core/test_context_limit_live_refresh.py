# -*- coding: utf-8 -*-
"""回归：改上下文长度后必须立即生效，不得依赖新建标签页。

2026-11-16 用户实测症状：在设置里把「上下文长度」改大，会话圆环仍显示旧预算，
只有新建标签页（新 engine → 空缓存）才刷新。四处独立缺陷，本文件逐个锁定：

1. 快照缓存键缺 budget_tokens / ratio —— 未发新消息时永久命中旧快照
2. resolve_context_limit 不读「声明_上下文长度」—— 能力弹窗里填的值不影响预算
3. _refresh_context_usage_indicator 节流 return 时无补偿 —— 配置变更被静默丢弃
4. LLM.ModelOverrides 无 valueChanged 监听 —— 其他标签页不刷新
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_engine(messages=None, system_prompt="sys", budget_fn=None):
    """最小 UIEngine 装配（绕过 Qt/Backend 初始化，只留快照计算所需依赖）"""
    from app.core.engines.ui.engine import UIEngine

    session = SimpleNamespace()
    session.messages = messages or [{"role": "user", "content": "你好"}]
    session.system_prompt = system_prompt
    session.compaction_state = {"active": False}
    session.compaction_cache = {}
    session._messages_version = 1
    session.session_id = "s1"

    engine = UIEngine.__new__(UIEngine)
    engine._get_model_config = lambda: {}
    engine._get_agent_manager = MagicMock(return_value=None)
    engine._current_agent = "build"
    engine._session_manager = MagicMock()
    engine._session_manager.get_current_session.return_value = session
    core = MagicMock()
    core.context_builder.get_context_budget.side_effect = budget_fn
    engine._conversation_core = core
    # 工具快照冻结：键与 _tools_snapshot_key("s1") 一致，避免触达 agent_manager
    engine._tools_snapshot = {"key": ("s1", "build", False), "tools": [], "hash": "empty"}
    engine._tools_schema_cache = {"key": None, "timestamp": 0.0, "tokens": None}
    engine._tool_executor = None
    engine._backend = None
    return engine, session


def _cfg(context_limit):
    return {
        "模型名称": "custom-model",
        "provider_name": "Custom",
        "上下文长度": context_limit,
        "最大Token": context_limit,
    }


@pytest.fixture()
def budget_from_config():
    """预算按 llm_config 实际算（走真实 resolve_context_budget）"""
    from app.core.context.budget import resolve_context_budget

    return resolve_context_budget


# ══════════════════════════════════════════════════════════════════
# 缺陷 1：快照缓存键必须含预算
# ══════════════════════════════════════════════════════════════════
def test_budget_change_invalidates_snapshot_cache(budget_from_config):
    """同会话同消息，仅改上下文长度 → budget_tokens 必须跟着变。

    这是「新建标签页才生效」的直接根因：旧缓存键不含 budget，命中即返回旧值。
    """
    engine, session = _make_engine(budget_fn=budget_from_config)

    small = engine.get_context_usage_snapshot(session, _cfg(128000))
    large = engine.get_context_usage_snapshot(session, _cfg(1000000))

    assert small["budget_tokens"] < 200000, f"128000 窗口应小于 200000，实际 {small['budget_tokens']}"
    assert large["budget_tokens"] > 900000, f"改 1000000 后预算必须刷新，实际 {large['budget_tokens']}"


def test_budget_change_back_and_forth(budget_from_config):
    """来回改也必须每次都跟随，不能只单向失效（缓存键换值后仍可比对）"""
    engine, session = _make_engine(budget_fn=budget_from_config)

    first = engine.get_context_usage_snapshot(session, _cfg(128000))["budget_tokens"]
    engine.get_context_usage_snapshot(session, _cfg(1000000))
    back = engine.get_context_usage_snapshot(session, _cfg(128000))["budget_tokens"]

    assert back == first, f"改回原值应回到原预算 {first}，实际 {back}"


def test_same_config_still_hits_cache(budget_from_config):
    """同配置重复调用结果稳定（缓存命中路径不破坏一致性）"""
    engine, session = _make_engine(budget_fn=budget_from_config)
    cfg = _cfg(200000)
    a = engine.get_context_usage_snapshot(session, cfg)
    b = engine.get_context_usage_snapshot(session, cfg)
    assert a["budget_tokens"] == b["budget_tokens"]
    assert a["used_tokens"] == b["used_tokens"]


def test_cache_copy_isolation(budget_from_config):
    """命中缓存返回的 breakdown/compaction 必须是拷贝（消费方原地改不得污染缓存）"""
    engine, session = _make_engine(budget_fn=budget_from_config)
    cfg = _cfg(200000)
    first = engine.get_context_usage_snapshot(session, cfg)
    first["breakdown"].clear()
    first["compaction"]["active"] = True

    second = engine.get_context_usage_snapshot(session, cfg)
    assert second["breakdown"], "缓存内的 breakdown 被消费方清空污染"
    assert second["compaction"].get("active") is False, "缓存内的 compaction 被污染"


# ══════════════════════════════════════════════════════════════════
# 缺陷 2：预算链必须读能力声明
# ══════════════════════════════════════════════════════════════════
def test_resolve_context_limit_reads_declaration():
    """「声明_上下文长度」是模型级最高优先声明，预算链必须认它。

    UI 能力胶囊走 resolve_model_capabilities（含声明层），预算走
    resolve_context_limit——两者口径不一致时，用户在弹窗里填的值只改标签
    不改实际预算。
    """
    from app.core.modelmeta.model_capabilities import resolve_context_limit

    cfg = {"模型名称": "custom-model", "provider_name": "Custom", "声明_上下文长度": 500000}
    assert resolve_context_limit(cfg) == 500000


def test_declaration_beats_explicit_saved_value():
    """声明层高于 saved_providers 的「上下文长度」（用户在弹窗显式声明的意图更强）"""
    from app.core.modelmeta.model_capabilities import resolve_context_limit

    cfg = {
        "模型名称": "custom-model",
        "provider_name": "Custom",
        "上下文长度": 128000,
        "最大Token": 128000,
        "声明_上下文长度": 500000,
    }
    assert resolve_context_limit(cfg) == 500000


def test_invalid_declaration_falls_through():
    """声明值非法（非数字/非正）→ 跳过该层，不抛错，回落到普通链"""
    from app.core.modelmeta.model_capabilities import resolve_context_limit

    for bad in ("abc", 0, -1):
        cfg = {"模型名称": "custom-model", "provider_name": "Custom", "上下文长度": 128000, "声明_上下文长度": bad}
        assert resolve_context_limit(cfg) == 128000, f"非法声明 {bad!r} 不应生效"


def test_declaration_flows_into_budget():
    """声明经预算链落到 budget_tokens（端到端，与圆环同源）"""
    from app.core.context.budget import resolve_context_budget

    budget = resolve_context_budget(
        {"模型名称": "custom-model", "provider_name": "Custom", "声明_上下文长度": 500000}
    )
    assert budget > 400000, f"声明 500000 未进预算，实际 {budget}"


def test_declaration_drives_snapshot_budget(budget_from_config):
    """声明路径同样能刷新快照（与缺陷 1 的缓存放一起验证不漏）"""
    engine, session = _make_engine(budget_fn=budget_from_config)
    cfg = {"模型名称": "custom-model", "provider_name": "Custom", "声明_上下文长度": 500000}
    snap = engine.get_context_usage_snapshot(session, cfg)
    assert snap["budget_tokens"] > 400000


# ══════════════════════════════════════════════════════════════════
# 缺陷 3/4：刷新链（force 参数 + ModelOverrides 信号）
# ══════════════════════════════════════════════════════════════════
def test_refresh_accepts_force_keyword():
    """_refresh_context_usage_indicator 必须接受 force 关键字（配置变更绕过节流）"""
    import inspect

    from app.main_widget import OpenAIChatToolWindow

    params = inspect.signature(OpenAIChatToolWindow._refresh_context_usage_indicator).parameters
    assert "force" in params, "缺 force 参数则配置变更无法绕过节流"
    assert params["force"].default is False, "默认必须为 False（保持原有节流行为）"


def test_force_bypasses_throttle_window():
    """force=True 在节流窗口内也必须发起刷新（节流 return 无补偿，会静默丢弃）"""
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._is_streaming = False
    inst._worker_actually_busy = lambda: False
    inst.context_usage_ring = object()
    session = SimpleNamespace(
        session_id="s1",
        last_api_prompt_tokens=0,
        last_api_message_count=0,
        last_api_prompt_from_usage=None,
    )
    inst.session_manager = MagicMock()
    inst.session_manager.get_current_session.return_value = session
    inst.backend = MagicMock()
    inst.backend.get_context_usage_snapshot.return_value = {}
    inst._gen_thread_pool = MagicMock()
    inst._get_current_model_config = lambda: {"模型名称": "m"}
    inst._ctx_usage_ready = MagicMock()
    # QObject 子类未调 super().__init__：未设置过的属性 getattr 会抛 RuntimeError，
    # 故把 _refresh 内部读取的状态字段全部预置
    inst._ctx_usage_inflight = False
    inst._ctx_usage_pending = False
    inst._ctx_usage_gen = 0
    # 刚刷新过 → 落在 0.5s 节流窗口内
    import time

    inst._last_context_refresh_time = time.monotonic()

    inst._refresh_context_usage_indicator()  # 非 force：应被节流拦下
    assert inst._ctx_usage_inflight is False, "非 force 落在节流窗口内不应发起"

    inst._refresh_context_usage_indicator(force=True)  # force：必须越过
    assert inst._ctx_usage_inflight is True, "force=True 仍被节流拦下"
    assert inst._gen_thread_pool.start.called, "force=True 应真正投递后台计算"


def test_model_overrides_signal_wired():
    """LLM.ModelOverrides 必须注册 valueChanged 监听（多窗口同步的前提）"""
    import inspect

    from app.main_widget import OpenAIChatToolWindow

    # 注册点在 setup_ui（与 llm_saved_providers 同批），不在 __init__
    src = inspect.getsource(OpenAIChatToolWindow.setup_ui)
    assert "llm_model_overrides.valueChanged" in src, "ModelOverrides 变更未接监听"
    assert "_on_model_overrides_changed" in src, "监听未挂回调"


def test_overrides_callback_refreshes_ring_with_force():
    """回调必须以 force=True 刷圆环（否则落在 0.5s 窗口内的刷新被丢掉）"""
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    calls = []
    inst._refresh_context_usage_indicator = lambda force=False: calls.append(force)
    inst._card_manager = None
    inst._is_destroyed = False

    inst._on_model_overrides_changed({})

    assert calls == [True], f"应以 force=True 刷新圆环，实际 {calls}"


def test_overrides_callback_skips_destroyed_window():
    """已销毁窗口不刷新（防访问悬空 C++ 对象）"""
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    calls = []
    inst._refresh_context_usage_indicator = lambda force=False: calls.append(force)
    inst._is_destroyed = True

    inst._on_model_overrides_changed({})

    assert calls == [], "已销毁窗口不应刷新"
