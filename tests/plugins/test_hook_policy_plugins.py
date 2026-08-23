# -*- coding: utf-8 -*-
"""HookPolicy 插件契约回归测试

覆盖：
1. 5 个内置策略 should_trigger 的事件级判定（all / tool_only / none /
   subagent_default / team_member）
2. HookPolicyRegistry 按 scope 分域激活（main / subagent / team_member）
3. ChatWorker._hook_policy_obj_resolve 修复：
   - 未指定 id → 尊重当前主域激活（用户切换 NONE/TOOL_EVENTS_ONLY 生效）
   - 指定 id（team_member）→ 按 id 直接定位策略对象，不污染 main scope 激活
4. 消费方触发点：团队成员窗口用 team_member 策略跳过 Pre/PostAssistant，
   主窗口用 all 策略触发全部
5. 团队窗口注入链路：backend 创建引擎时按 is_team_member 透传 hook_policy_id
"""

from app.plugins.contracts.hook_policy import (
    HookDecision,
    PostAssistantMessageEvent,
    PostToolUseEvent,
    PreAssistantMessageEvent,
    PreToolUseEvent,
    SessionStartEvent,
    StopEvent,
    TeamMailEvent,
)


def _register_system_policies():
    """扫描并注册 system 插件的 hook_policies（仿运行时 loader）。"""
    import importlib.util
    from pathlib import Path

    from app.plugins.registries.hook_policy_registry import HookPolicyRegistry

    reg = HookPolicyRegistry.get_instance()
    base = Path(__file__).resolve().parents[2] / "plugins" / "system" / "hook_policies"
    for name in ("all", "tool_only", "none", "subagent_default", "team_member"):
        spec = importlib.util.spec_from_file_location(f"_hp_{name}", base / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.register(reg)


# ============================================================
# 1. 内置策略事件级判定
# ============================================================


def test_all_policy_triggers_everything():
    from plugins.system.hook_policies.all import AllHookPolicy

    p = AllHookPolicy()
    for ev in (
        PreToolUseEvent(),
        PostToolUseEvent(),
        PreAssistantMessageEvent(),
        PostAssistantMessageEvent(),
        StopEvent(),
        TeamMailEvent(),
    ):
        assert p.should_trigger(ev) == HookDecision.TRIGGER


def test_tool_only_policy_only_tool_events():
    from plugins.system.hook_policies.tool_only import ToolOnlyHookPolicy

    p = ToolOnlyHookPolicy()
    assert p.should_trigger(PreToolUseEvent()) == HookDecision.TRIGGER
    assert p.should_trigger(PostToolUseEvent()) == HookDecision.TRIGGER
    assert p.should_trigger(PreAssistantMessageEvent()) == HookDecision.SKIP
    assert p.should_trigger(PostAssistantMessageEvent()) == HookDecision.SKIP


def test_none_policy_skips_all():
    from plugins.system.hook_policies.none import NoneHookPolicy

    p = NoneHookPolicy()
    for ev in (
        PreToolUseEvent(),
        PostToolUseEvent(),
        PreAssistantMessageEvent(),
        PostAssistantMessageEvent(),
        StopEvent(),
    ):
        assert p.should_trigger(ev) == HookDecision.SKIP


def test_subagent_default_policy_scope():
    from plugins.system.hook_policies.subagent_default import (
        SubagentDefaultHookPolicy,
    )

    p = SubagentDefaultHookPolicy()
    # 保留：工具级 / Stop / PluginChanged
    assert p.should_trigger(PreToolUseEvent()) == HookDecision.TRIGGER
    assert p.should_trigger(PostToolUseEvent()) == HookDecision.TRIGGER
    assert p.should_trigger(StopEvent()) == HookDecision.TRIGGER
    # 跳过：无独立会话生命周期 / 自注入 / 团队邮件
    assert p.should_trigger(PreAssistantMessageEvent()) == HookDecision.SKIP
    assert p.should_trigger(PostAssistantMessageEvent()) == HookDecision.SKIP
    assert p.should_trigger(TeamMailEvent()) == HookDecision.SKIP


def test_team_member_policy_skips_pre_post_assistant():
    from plugins.system.hook_policies.team_member import TeamMemberHookPolicy

    p = TeamMemberHookPolicy()
    # 保留：工具级 / SessionStart / Stop / TeamMail / PluginChanged
    assert p.should_trigger(PreToolUseEvent()) == HookDecision.TRIGGER
    assert p.should_trigger(PostToolUseEvent()) == HookDecision.TRIGGER
    assert p.should_trigger(SessionStartEvent()) == HookDecision.TRIGGER
    assert p.should_trigger(StopEvent()) == HookDecision.TRIGGER
    assert p.should_trigger(TeamMailEvent()) == HookDecision.TRIGGER
    # 跳过：成员窗口助手回复由邮件驱动注入，不应被主对话语义 hook 拦截
    assert p.should_trigger(PreAssistantMessageEvent()) == HookDecision.SKIP
    assert p.should_trigger(PostAssistantMessageEvent()) == HookDecision.SKIP


# ============================================================
# 2. 注册表按 scope 分域
# ============================================================


def test_registry_activates_by_scope():
    _register_system_policies()
    from app.plugins.contracts.hook_policy import (
        SCOPE_MAIN,
        SCOPE_SUBAGENT,
        SCOPE_TEAM_MEMBER,
    )
    from app.plugins.registries.hook_policy_registry import HookPolicyRegistry

    reg = HookPolicyRegistry.get_instance()
    # 默认激活映射
    assert reg.get_active(SCOPE_MAIN).id == "all"
    assert reg.get_active(SCOPE_SUBAGENT).id == "subagent_default"
    assert reg.get_active(SCOPE_TEAM_MEMBER).id == "team_member"
    # 各 scope 独立查询
    assert reg.policies(SCOPE_MAIN).keys() >= {"all", "tool_only", "none"}
    assert reg.policies(SCOPE_TEAM_MEMBER).keys() == {"team_member"}


# ============================================================
# 3. ChatWorker resolve 修复（不污染 main scope）
# ============================================================


def _make_worker(hook_policy_id=None, hook_policy=None):
    from app.core.workers.chat_worker import OpenAIChatWorker

    return OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={},
        hook_policy_id=hook_policy_id,
        hook_policy=hook_policy,
    )


def test_chat_worker_resolve_returns_registered_object_by_id():
    _register_system_policies()
    w = _make_worker(hook_policy_id="team_member")
    obj = w._hook_policy_obj_resolve()
    assert obj.id == "team_member"
    # 团队成员策略对象跳过 Pre/PostAssistant
    assert obj.should_trigger(PreAssistantMessageEvent()) == HookDecision.SKIP
    assert obj.should_trigger(PostAssistantMessageEvent()) == HookDecision.SKIP
    assert obj.should_trigger(PreToolUseEvent()) == HookDecision.TRIGGER


def test_chat_worker_resolve_no_main_scope_pollution():
    """团队窗口指定 id 经 resolve 不污染 main scope 的激活状态。"""
    _register_system_policies()
    from app.plugins.contracts.hook_policy import SCOPE_MAIN
    from app.plugins.registries.hook_policy_registry import HookPolicyRegistry

    reg = HookPolicyRegistry.get_instance()
    assert reg.get_active(SCOPE_MAIN).id == "all"  # 基线

    # 解析团队窗口的策略对象
    w = _make_worker(hook_policy_id="team_member")
    resolved = w._hook_policy_obj_resolve()
    assert resolved.id == "team_member"

    # main scope 激活必须仍保持 "all"，未被 set_active 篡改
    assert reg.get_active(SCOPE_MAIN).id == "all"


def test_chat_worker_resolve_respects_active_when_no_id():
    """未指定 id：尊重当前主域激活（模拟用户切换 NONE）。"""
    _register_system_policies()
    from app.plugins.contracts.hook_policy import SCOPE_MAIN
    from app.plugins.registries.hook_policy_registry import HookPolicyRegistry

    reg = HookPolicyRegistry.get_instance()
    reg.set_active("none", SCOPE_MAIN)
    try:
        w = _make_worker(hook_policy_id=None)
        assert w._hook_policy_obj_resolve().id == "none"
    finally:
        reg.set_active("all", SCOPE_MAIN)


def test_chat_worker_should_run_hook_uses_resolved_policy():
    """消费方触发点：团队成员窗口对 PreAssistantMessage 短路返回 False。"""
    _register_system_policies()
    w = _make_worker(hook_policy_id="team_member")
    assert w._should_run_hook(PreAssistantMessageEvent()) is False
    assert w._should_run_hook(PreToolUseEvent()) is True


# ============================================================
# 4. 团队窗口注入链路（backend → engine → config）
# ============================================================


def test_backend_injects_team_member_policy(monkeypatch):
    """backend 创建 UI 引擎时，按 is_team_member 透传 hook_policy_id。"""
    from unittest.mock import MagicMock

    from app.core.backend import ChatBackend

    backend = ChatBackend(window_id="win_team_1")

    # 桩 TeamManager：本窗口是团队成员
    tm = MagicMock()
    tm.is_team_member.return_value = True
    monkeypatch.setattr("app.core.team_manager.TeamManager", MagicMock(get_instance=lambda: tm))

    captured = {}

    def fake_create_engine_for_slot(slot, fallback_cls, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(
        "app.plugins.registries.engine_registry.create_engine_for_slot",
        fake_create_engine_for_slot,
    )
    # 避免延迟创建触发真实子系统
    monkeypatch.setattr(backend, "_flush_pending_engine_callbacks", lambda: None)
    backend._tool_executor = MagicMock()
    backend._session_manager = MagicMock()
    backend._agent_manager = MagicMock()
    backend._get_model_config = lambda: {}

    backend._deferred_create_engines()
    assert captured.get("hook_policy_id") == "team_member"


def test_backend_no_policy_for_non_team(monkeypatch):
    """非团队成员窗口不注入 hook_policy_id（走主域默认激活）。"""
    from unittest.mock import MagicMock

    from app.core.backend import ChatBackend

    backend = ChatBackend(window_id="win_main_1")

    tm = MagicMock()
    tm.is_team_member.return_value = False
    monkeypatch.setattr("app.core.team_manager.TeamManager", MagicMock(get_instance=lambda: tm))

    captured = {}

    def fake_create_engine_for_slot(slot, fallback_cls, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(
        "app.plugins.registries.engine_registry.create_engine_for_slot",
        fake_create_engine_for_slot,
    )
    monkeypatch.setattr(backend, "_flush_pending_engine_callbacks", lambda: None)
    backend._tool_executor = MagicMock()
    backend._session_manager = MagicMock()
    backend._agent_manager = MagicMock()
    backend._get_model_config = lambda: {}

    backend._deferred_create_engines()
    assert captured.get("hook_policy_id") is None
