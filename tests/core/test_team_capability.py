# -*- coding: utf-8 -*-
"""F9：成员能力自动登记 + 派单前可见（能力推导 / 快照兜底 / 匹配筛选）

覆盖：
1. join_team 自动登记 capability（含 can_write / task_tags）
2. get_member_capability 动态解析优先 + join 快照兜底
3. _derive_capability task_tags 关键词推导（build→implement / plan→plan /
   review→review / diagnose→diagnose / explore→explore / lead→lead）
4. match_members 按任务类型过滤
5. team_tools.team_list_members 权限摘要渲染

设计说明：
- 直接构造 TeamManager 实例（隔离 tmp_path 数据目录，绕过 get_instance 单例污染）
- 用 SimpleNamespace 伪造 Agent 对象（name/description/mode/permission/tools），
  避免依赖真实 AgentManager 加载
- monkeypatch AgentManager.get_instance 返回伪造管理器，验证 join 登记与动态解析
"""

from types import SimpleNamespace

import pytest

from app.core import team_manager as tm_mod


@pytest.fixture
def tm(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager 实例。"""
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    return tm_mod.TeamManager()


def _fake_agent(
    name: str,
    description: str = "",
    mode: str = "subagent",
    permission: dict = None,
    tools: dict = None,
) -> SimpleNamespace:
    """构造伪造 Agent 对象（与 app.core.agent.Agent 字段兼容的子集）。"""
    return SimpleNamespace(
        name=name,
        description=description,
        mode=mode,
        permission=permission if permission is not None else {},
        tools=tools if tools is not None else {},
    )


def _patch_agent_manager(monkeypatch, agents: dict):
    """把 app.core.agent.AgentManager.get_instance 指向伪造管理器。

    team_manager 内部延迟 import AgentManager 后调 get_instance()，
    因此 patch 目标是 agent 模块的 AgentManager.get_instance。
    """
    from app.core import agent as agent_mod

    fake_mgr = SimpleNamespace(get_agent=lambda name: agents.get(name))
    monkeypatch.setattr(agent_mod.AgentManager, "get_instance", classmethod(lambda cls: fake_mgr))
    return fake_mgr


class TestDeriveCapability:
    """_derive_capability：tags 推导 + can_write/can_bash/can_team 判定"""

    def test_build_agent_tags_implement(self, tm):
        """build 角色（编码/实现）→ task_tags 含 implement，can_write=True。"""
        agent = _fake_agent(
            "build",
            "负责编码实现与验证",
            permission={"write": "allow", "edit": "allow", "multi_edit": "allow", "bash": "allow"},
        )
        cap = tm._derive_capability(agent)
        assert "implement" in cap["task_tags"], f"build 应推导 implement，实际 {cap['task_tags']}"
        assert cap["can_write"] is True
        assert cap["can_bash"] is True
        assert cap["agent_name"] == "build"
        assert "编码实现" in cap["role_desc"]

    def test_plan_agent_tags_plan(self, tm):
        """plan 角色（规划/只读）→ task_tags 含 plan。"""
        agent = _fake_agent(
            "plan",
            "面向代码库分析和实现规划的只读智能体",
            permission={"write": "deny", "edit": "deny", "multi_edit": "deny"},
        )
        cap = tm._derive_capability(agent)
        assert "plan" in cap["task_tags"], f"plan 应推导 plan，实际 {cap['task_tags']}"
        assert cap["can_write"] is False

    def test_review_agent_tags_review(self, tm):
        """review 角色（审查/审计）→ task_tags 含 review。"""
        agent = _fake_agent("review", "代码审查智能体，系统性审查变更", permission={"write": "deny"})
        cap = tm._derive_capability(agent)
        assert "review" in cap["task_tags"], f"review 应推导 review，实际 {cap['task_tags']}"

    def test_diagnose_agent_tags_diagnose(self, tm):
        """diagnose 角色（诊断/修复）→ task_tags 含 diagnose。"""
        agent = _fake_agent("diagnose", "严谨的 Bug 诊断专家，负责复现问题、定位根因、修复")
        cap = tm._derive_capability(agent)
        assert "diagnose" in cap["task_tags"], f"diagnose 应推导 diagnose，实际 {cap['task_tags']}"

    def test_explore_agent_tags_explore(self, tm):
        """explore 角色（探索）→ task_tags 含 explore。"""
        agent = _fake_agent("explore", "快速代码探索智能体，深入分析代码库")
        cap = tm._derive_capability(agent)
        assert "explore" in cap["task_tags"], f"explore 应推导 explore，实际 {cap['task_tags']}"

    def test_leader_agent_tags_lead(self, tm):
        """leader 角色（统筹）→ task_tags 含 lead。"""
        agent = _fake_agent("leader", "统筹团队任务拆解/分发/汇总")
        cap = tm._derive_capability(agent)
        assert "lead" in cap["task_tags"], f"leader 应推导 lead，实际 {cap['task_tags']}"

    def test_no_match_falls_back_to_can_write(self, tm):
        """无关键词匹配：可写→implement，只读→plan（can_write 兜底）。"""
        write_agent = _fake_agent(
            "custom", "自定义角色", permission={"write": "allow", "edit": "allow", "multi_edit": "allow"}
        )
        assert "implement" in tm._derive_capability(write_agent)["task_tags"]

        read_agent = _fake_agent("custom", "自定义角色", permission={"write": "deny"})
        assert "plan" in tm._derive_capability(read_agent)["task_tags"]

    def test_tools_whitelist_semantics(self, tm):
        """agent.tools 白名单：未列出工具全部拒绝（can_write/can_bash 按白名单判定）。

        T16：can_team 例外——团队成员恒具团队工具（schema 层按 is_in_team
        放行，与 tools 白名单无关），即使白名单未列 team 工具也恒 True。
        """
        agent = _fake_agent("build", "构建", tools={"read": True, "write": True, "edit": True, "multi_edit": True})
        cap = tm._derive_capability(agent)
        assert cap["can_write"] is True
        assert cap["can_bash"] is False, "tools 白名单未列 bash → 拒绝"
        assert cap["can_team"] is True, "T16：团队成员恒具团队工具（与静态白名单无关）"

    def test_review_agent_can_team_true(self, tm):
        """T16：review 只读角色（write deny、无 team 权限条目）→ can_team 恒 True。

        回归根因：旧逻辑按静态 permission 判定 team_* allow → review 显示
        团队✗；实际团队工具由 is_in_team 放行，团队成员必然可用。
        """
        agent = _fake_agent(
            "review",
            "代码审查智能体，系统性审查变更",
            permission={"write": "deny", "edit": "deny", "multi_edit": "deny", "bash": "deny"},
        )
        cap = tm._derive_capability(agent)
        assert cap["can_team"] is True, "review 团队成员应恒具团队工具能力"
        assert cap["can_write"] is False, "can_write 仍按静态权限判定"
        assert cap["can_bash"] is False


class TestJoinAutoRegistration:
    """join_team 自动登记 capability"""

    def test_join_registers_capability(self, tm, monkeypatch):
        """join 后成员记录含 capability（can_write/task_tags）。"""
        _patch_agent_manager(
            monkeypatch,
            {"build": _fake_agent("build", "负责编码实现与验证", permission={"write": "allow"})},
        )
        tm.join_team("win_01", "build")
        members = tm.get_members()
        assert len(members) == 1
        cap = members[0].get("capability")
        assert cap is not None, "join 应登记 capability"
        assert cap["agent_name"] == "build"
        assert "can_write" in cap and "task_tags" in cap
        assert "implement" in cap["task_tags"]

    def test_join_unknown_agent_no_crash(self, tm, monkeypatch):
        """agent 不存在（get_agent 返回 None）→ 不登记 capability，不抛异常。"""
        _patch_agent_manager(monkeypatch, {})
        tm.join_team("win_01", "ghost-agent")
        members = tm.get_members()
        assert len(members) == 1
        assert "capability" not in members[0], "未知 agent 无 capability 快照"


class TestGetMemberCapability:
    """get_member_capability：动态解析优先 + 快照兜底"""

    def test_dynamic_preferred_over_snapshot(self, tm, monkeypatch):
        """动态解析优先：即使快照存在，也返回 Agent 最新能力。"""
        # 先 join 登记快照（旧能力）
        _patch_agent_manager(
            monkeypatch,
            {"build": _fake_agent("build", "负责编码实现", permission={"write": "allow"})},
        )
        tm.join_team("win_01", "build")
        assert "implement" in tm.get_member_capability("build")["task_tags"]

        # agent 描述更新（动态解析应反映新能力，而非旧快照）
        _patch_agent_manager(
            monkeypatch,
            {"build": _fake_agent("build", "负责审查与审计", permission={"write": "deny"})},
        )
        cap = tm.get_member_capability("build")
        assert "review" in cap["task_tags"], f"动态解析应优先，实际 {cap['task_tags']}"
        assert cap["can_write"] is False

    def test_snapshot_fallback_when_agent_missing(self, tm, monkeypatch):
        """agent 文件删除（get_agent 返回 None）→ 快照兜底返回登记时能力。"""
        _patch_agent_manager(
            monkeypatch,
            {"build": _fake_agent("build", "负责编码实现", permission={"write": "allow"})},
        )
        tm.join_team("win_01", "build")
        # 之后 agent 被删除
        _patch_agent_manager(monkeypatch, {})
        cap = tm.get_member_capability("build")
        assert cap is not None, "快照应兜底"
        assert "implement" in cap["task_tags"]

    def test_no_capability_anywhere_returns_none(self, tm, monkeypatch):
        """既无 agent 又无快照 → 返回 None（不抛异常）。"""
        _patch_agent_manager(monkeypatch, {})
        tm.join_team("win_01", "ghost")  # 无 capability 快照
        assert tm.get_member_capability("ghost") is None

    def test_empty_agent_name_returns_none(self, tm):
        assert tm.get_member_capability("") is None


class TestMatchMembers:
    """match_members：按 task_tags 过滤成员"""

    def test_match_by_task_type(self, tm, monkeypatch):
        """多成员：implement 类型只返回 build。"""
        _patch_agent_manager(
            monkeypatch,
            {
                "build": _fake_agent("build", "负责编码实现", permission={"write": "allow"}),
                "plan": _fake_agent("plan", "面向规划的只读智能体", permission={"write": "deny"}),
                "review": _fake_agent("review", "代码审查", permission={"write": "deny"}),
            },
        )
        tm.join_team("win_01", "build")
        tm.join_team("win_02", "plan")
        tm.join_team("win_03", "review")

        impl = tm.match_members("implement")
        assert [m["agent_name"] for m in impl] == ["build"], f"implement 应匹配 build，实际 {impl}"

        plans = tm.match_members("plan")
        assert "plan" in [m["agent_name"] for m in plans]

        reviews = tm.match_members("review")
        assert "review" in [m["agent_name"] for m in reviews]

    def test_match_no_results(self, tm, monkeypatch):
        """无匹配类型 → 空列表。"""
        _patch_agent_manager(monkeypatch, {"build": _fake_agent("build", "编码实现")})
        tm.join_team("win_01", "build")
        assert tm.match_members("nonexistent-type") == []


class TestTeamToolsCapabilityDisplay:
    """team_tools：权限摘要渲染（_format_capability）"""

    def test_format_capability(self, tm):
        from app.tools.team_tools import TeamTools

        cap = {
            "can_write": True,
            "can_bash": True,
            "can_team": True,
            "task_tags": ["implement"],
        }
        text = TeamTools._format_capability(cap)
        assert "写✓" in text, f"can_write=True 应显示写✓，实际 {text}"
        assert "bash✓" in text
        assert "团队✓" in text
        assert "implement" in text

    def test_format_capability_denied(self, tm):
        from app.tools.team_tools import TeamTools

        cap = {
            "can_write": False,
            "can_bash": False,
            "can_team": False,
            "task_tags": ["review"],
        }
        text = TeamTools._format_capability(cap)
        assert "写✗" in text and "bash✗" in text
        # T16：团队工具对团队成员恒真 → 显示层恒 团队✓（不再出现团队✗），
        # 即使传入的 cap.can_team=False（存量老快照防御）
        assert "团队✓" in text, f"团队成员恒具团队工具，实际 {text}"
        assert "团队✗" not in text, "团队成员列表不应再出现团队✗"
        assert "review" in text

    def test_team_list_members_contains_capability_line(self, tm, monkeypatch):
        """team_list_members 输出包含权限摘要行（F9 主链路）。"""
        from app.tools.team_tools import TeamTools

        _patch_agent_manager(
            monkeypatch,
            {"build": _fake_agent("build", "负责编码实现", permission={"write": "allow"})},
        )
        tm.join_team("win_01", "build")

        bt = SimpleNamespace(_team_window_id="win_01", _team_agent_name="build")
        tool = TeamTools(bt)
        # 需要真实 TeamManager.get_instance 指向我们的实例
        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm))
        result = tool.team_list_members()
        assert result.success, result.error
        assert "build@win_01" in result.content
        assert "权限:" in result.content, f"应含权限摘要行，实际:\n{result.content}"
        assert "implement" in result.content, f"build 标签应为 implement，实际:\n{result.content}"

    def test_team_list_members_review_shows_team_check(self, tm, monkeypatch):
        """T16 回归：只读角色 review 成员列表显示 团队✓（不再出现 团队✗）。

        根因复刻：review permission 全 deny（write/edit/bash 全 deny、无
        team_* 条目）→ 旧逻辑 can_team=False → 显示"团队✗"；修复后成员
        列表恒显 团队✓（团队成员必具团队工具）。
        """
        from app.tools.team_tools import TeamTools

        _patch_agent_manager(
            monkeypatch,
            {
                "review": _fake_agent(
                    "review",
                    "代码审查智能体，系统性审查变更",
                    permission={"write": "deny", "edit": "deny", "multi_edit": "deny", "bash": "deny"},
                )
            },
        )
        tm.join_team("win_01", "review")

        bt = SimpleNamespace(_team_window_id="win_01", _team_agent_name="review")
        tool = TeamTools(bt)
        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm))

        result = tool.team_list_members()
        assert result.success, result.error
        assert "review@win_01" in result.content
        assert "权限:" in result.content, f"应含权限摘要行，实际:\n{result.content}"
        assert "团队✓" in result.content, f"review 成员应显示 团队✓，实际:\n{result.content}"
        assert "团队✗" not in result.content, "团队成员列表不应再出现 团队✗"
        assert "写✗" in result.content, "can_write 仍按静态权限判定（写✗ 正常显示）"

    def test_team_send_message_attaches_capability_hint(self, tm, monkeypatch):
        """team_send_message 结果附带目标能力提示。"""
        from app.tools.team_tools import TeamTools

        _patch_agent_manager(
            monkeypatch,
            {
                "build": _fake_agent("build", "负责编码实现", permission={"write": "allow"}),
                "plan": _fake_agent("plan", "面向规划的只读智能体", permission={"write": "deny"}),
            },
        )
        tm.join_team("win_01", "build")
        tm.join_team("win_02", "plan")

        bt = SimpleNamespace(_team_window_id="win_01", _team_agent_name="build")
        tool = TeamTools(bt)
        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm))

        result = tool.team_send_message(to_agent="plan", message="做规划")
        assert result.success, result.error
        assert "目标能力:" in result.content, f"应附目标能力提示，实际:\n{result.content}"
        assert "plan" in result.content
