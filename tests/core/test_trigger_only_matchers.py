# -*- coding: utf-8 -*-
"""trigger_event only_matchers 规则组过滤回归测试（2026-11-15）

背景：
团队三 hook（协作指引/团队上下文/智能体身份）挂在 SessionStart 的
#team_member matcher 组下，只在 create_session 时触发。手动 /team --join
（main_widget._do_join_team）不新建会话，SessionStart 不再触发 → 已有会话
的团队 hook 永久缺失，须手动新建会话才补上。

修复：HookManager.trigger_event 增加 only_matchers 参数（只执行 matcher
命中该集合的规则组），backend.trigger_session_event 透传；_do_join_team
成功后补触发 SessionStart(startup, only_matchers={"#team_member"})。

本测试锁定三条不变式：
1. only_matchers 非 None 时只跑指定组，无 matcher 组（技能/子智能体列表）
   不得重复注入；
2. 不传 only_matchers 行为完全不变（整事件全部命中）；
3. only_matchers 与常规 matches 是串联关系：matcher 命中但上下文不匹配
   （非团队成员）时仍不触发。
"""

import pytest

from app.core.hooks.hook_manager import Hook, HookManager, HookMatchRule, HookType


@pytest.fixture
def hm():
    """干净 HookManager：隔离类级共享注册表，避免污染其它测试"""
    hooks = HookManager._shared_hooks
    skill_index = HookManager._shared_skill_to_hooks
    states = HookManager._shared_hook_states
    HookManager._shared_hooks = {}
    HookManager._shared_skill_to_hooks = {}
    HookManager._shared_hook_states = {}
    try:
        yield HookManager(thread_pool=object())
    finally:
        HookManager._shared_hooks = hooks
        HookManager._shared_skill_to_hooks = skill_index
        HookManager._shared_hook_states = states


def _setup_sessionstart_rules(hm: HookManager) -> None:
    """复刻真实 hooks.json 的 SessionStart 形态：无 matcher 组 + #team_member 组"""
    generic = Hook(id="generic", type=HookType.PROMPT.value, prompt="GENERIC", add_output_to_context=True)
    team = Hook(id="team", type=HookType.PROMPT.value, prompt="TEAM", add_output_to_context=True)
    hm._hooks["SessionStart"] = [
        HookMatchRule(matcher=None, hooks=[generic], skill_name="sys"),
        HookMatchRule(matcher="#team_member", hooks=[team], skill_name="sys"),
    ]


class TestOnlyMatchers:
    def test_only_team_member_skips_generic_group(self, hm):
        """补触发场景：只跑 #team_member 组，无 matcher 组不重复注入"""
        _setup_sessionstart_rules(hm)
        results = hm.trigger_event(
            "SessionStart",
            context={"event_name": "SessionStart", "state": "startup", "is_team_member": True},
            trigger_async=False,
            only_matchers={"#team_member"},
        )
        outputs = [r.output for r in results]
        assert outputs == ["TEAM"]

    def test_without_only_matchers_all_groups_run(self, hm):
        """不传 only_matchers 行为不变：整事件全部规则组命中"""
        _setup_sessionstart_rules(hm)
        results = hm.trigger_event(
            "SessionStart",
            context={"event_name": "SessionStart", "state": "startup", "is_team_member": True},
            trigger_async=False,
        )
        assert sorted(r.output for r in results) == ["GENERIC", "TEAM"]

    def test_only_matchers_still_respects_matches(self, hm):
        """only_matchers 与 matches 串联：非团队成员窗口即使命中组也不触发"""
        _setup_sessionstart_rules(hm)
        results = hm.trigger_event(
            "SessionStart",
            context={"event_name": "SessionStart", "state": "startup", "is_team_member": False},
            trigger_async=False,
            only_matchers={"#team_member"},
        )
        assert results == []

    def test_only_matchers_multiple_values(self, hm):
        """集合含多个 matcher 时任一命中即跑"""
        team = Hook(id="team", type=HookType.PROMPT.value, prompt="TEAM", add_output_to_context=True)
        other = Hook(id="other", type=HookType.PROMPT.value, prompt="OTHER", add_output_to_context=True)
        hm._hooks["SessionStart"] = [
            HookMatchRule(matcher=None, hooks=[Hook(id="g", type=HookType.PROMPT.value, prompt="GENERIC")], skill_name="sys"),
            HookMatchRule(matcher="#team_member", hooks=[team], skill_name="sys"),
            HookMatchRule(matcher="resume|compact", hooks=[other], skill_name="sys"),
        ]
        results = hm.trigger_event(
            "SessionStart",
            context={"event_name": "SessionStart", "state": "resume", "is_team_member": True},
            trigger_async=False,
            only_matchers={"#team_member", "resume|compact"},
        )
        assert sorted(r.output for r in results) == ["OTHER", "TEAM"]

    def test_empty_matcher_set_runs_nothing(self, hm):
        """空集合语义：不执行任何规则组（非 None 即启用过滤）"""
        _setup_sessionstart_rules(hm)
        results = hm.trigger_event(
            "SessionStart",
            context={"event_name": "SessionStart", "state": "startup", "is_team_member": True},
            trigger_async=False,
            only_matchers=set(),
        )
        assert results == []
