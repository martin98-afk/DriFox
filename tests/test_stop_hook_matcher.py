# -*- coding: utf-8 -*-
"""Stop hook matcher 单元测试

验证 HookMatchRule.matches() 对 Stop 事件 3 种 reason 的精确匹配：
- completed（正常完成）
- cancelled（用户取消）
- error（API 异常）

同时验证 #team_member 特例与空 matcher 的向后兼容行为。
"""

import pytest

from app.core.hook_manager import HookMatchRule


def _ctx(event: str, reason: str = "", is_team_member: bool = False) -> dict:
    """构造一个最小化的 Stop 事件上下文"""
    return {
        "event_name": event,
        "reason": reason,
        "is_team_member": is_team_member,
        "message": "",
        "response": "",
        "assistant_response": "",
    }


# ───────────────────── Stop reason 精确匹配 ─────────────────────


@pytest.mark.parametrize(
    "reason",
    ["completed", "cancelled", "error"],
)
def test_stop_single_reason_match(reason):
    """单值 matcher 应匹配对应 reason 的 Stop 事件"""
    rule = HookMatchRule(matcher=reason)
    assert rule.matches(_ctx("Stop", reason=reason)) is True


@pytest.mark.parametrize(
    "set_reasons,trigger_reason",
    [
        (["completed", "cancelled"], "completed"),
        (["completed", "cancelled"], "cancelled"),
        (["completed", "error"], "error"),
        (["cancelled", "error"], "error"),
        (["completed", "cancelled", "error"], "completed"),
    ],
)
def test_stop_multi_reason_match(set_reasons, trigger_reason):
    """pipe 分隔的 matcher 应匹配集合内任一 reason"""
    rule = HookMatchRule(matcher="|".join(set_reasons))
    assert rule.matches(_ctx("Stop", reason=trigger_reason)) is True


@pytest.mark.parametrize(
    "matcher,trigger_reason",
    [
        ("completed", "cancelled"),
        ("completed", "error"),
        ("cancelled", "completed"),
        ("error", "completed"),
        ("completed|cancelled", "error"),
        ("error|cancelled", "completed"),
    ],
)
def test_stop_reason_no_match(matcher, trigger_reason):
    """不在 matcher 集合内的 reason 应不匹配"""
    rule = HookMatchRule(matcher=matcher)
    assert rule.matches(_ctx("Stop", reason=trigger_reason)) is False


def test_stop_reason_default_completed_when_missing():
    """未传 reason 时默认按 'completed' 匹配（兼容旧 hook）"""
    # matcher="completed" 应匹配缺少 reason 的上下文（fallback 默认值）
    rule = HookMatchRule(matcher="completed")
    assert rule.matches({"event_name": "Stop"}) is True


def test_stop_empty_matcher_matches_all():
    """空 matcher 仍匹配所有 Stop 事件（向后兼容）"""
    rule = HookMatchRule(matcher=None)
    for reason in ["completed", "cancelled", "error"]:
        assert rule.matches(_ctx("Stop", reason=reason)) is True


# ───────────────────── #team_member 特例 ─────────────────────


def test_stop_team_member_match_when_member():
    """Stop 事件下 #team_member 应匹配团队成员窗口"""
    rule = HookMatchRule(matcher="#team_member")
    assert rule.matches(_ctx("Stop", reason="completed", is_team_member=True)) is True


def test_stop_team_member_no_match_when_not_member():
    """Stop 事件下 #team_member 在非团队窗口应不匹配"""
    rule = HookMatchRule(matcher="#team_member")
    assert rule.matches(_ctx("Stop", reason="completed", is_team_member=False)) is False


# ───────────────────── 行为边界：Stop 分支走完后直接 return ─────────────────────


def test_stop_branch_returns_immediately_no_fallback_to_regex():
    """Stop 段命中 reason 分支后直接 return，不回退到正则匹配 response

    与 SessionStart 保持一致：reason 不匹配 = 整个 rule 不触发（即使正则本可匹配 response）
    这样 hooks.json 写 matcher=".*错误.*" 想在 error 场景下触发的旧写法
    在新实现下不会生效，必须迁移到 matcher="error"
    """
    rule = HookMatchRule(matcher=".*错误.*")
    # reason="error" 不在 matcher 集合 ["错误"] 中 → 应不匹配（即使 response 含"错误"）
    assert rule.matches(_ctx("Stop", reason="error")) is False
    assert rule.matches(_ctx("Stop", reason="completed")) is False


def test_stop_branch_consistent_with_sessionstart_pattern():
    """Stop 分支的语义应与 SessionStart 一致：精确匹配后立即返回"""
    # SessionStart: matcher="startup" 在 state="startup" 时匹配，其他状态不匹配
    sessionstart_rule = HookMatchRule(matcher="startup")
    assert sessionstart_rule.matches({"event_name": "SessionStart", "state": "startup"}) is True
    assert sessionstart_rule.matches({"event_name": "SessionStart", "state": "compact"}) is False

    # Stop: matcher="completed" 在 reason="completed" 时匹配，其他 reason 不匹配
    stop_rule = HookMatchRule(matcher="completed")
    assert stop_rule.matches({"event_name": "Stop", "reason": "completed"}) is True
    assert stop_rule.matches({"event_name": "Stop", "reason": "cancelled"}) is False


# ───────────────────── SessionStart 不应被 Stop 分支误影响 ─────────────────────


def test_sessionstart_not_affected_by_stop_branch():
    """SessionStart 仍按 state 字段匹配，不走 Stop 分支"""
    rule = HookMatchRule(matcher="completed")
    assert rule.matches({"event_name": "SessionStart", "state": "startup"}) is False
    assert rule.matches({"event_name": "SessionStart", "state": "completed"}) is True


# ───────────────────── Stop 事件下 #team_member 不消费 reason ─────────────────────


def test_stop_team_member_overrides_reason():
    """Stop 事件下 #team_member 仅看 is_team_member，不应被 reason 影响"""
    rule = HookMatchRule(matcher="#team_member")
    # 非团队成员 + 任意 reason：均不匹配
    for r in ["completed", "cancelled", "error"]:
        assert rule.matches(_ctx("Stop", reason=r, is_team_member=False)) is False
    # 团队成员 + 任意 reason：均匹配
    for r in ["completed", "cancelled", "error"]:
        assert rule.matches(_ctx("Stop", reason=r, is_team_member=True)) is True