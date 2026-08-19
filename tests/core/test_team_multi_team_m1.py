# -*- coding: utf-8 -*-
"""M1：多智能体团队并存 — 团队归属（run_id/team_label）与 team_list_members 分组展示

覆盖：
1. TeamManager.join_team 记录 run_id / team_label 到成员记录
2. get_members(run_id=...) 按 run 过滤（None 返回全部，空串返回无归属成员）
3. get_member_run_id / get_team_run_ids / get_team_label / update_member_team
4. team_list_members 工具：多团队并存时分组输出（本团队明细 + 其他团队概要 + 沟通提示）
5. inject_team_context hook：多团队并存时注入团队间沟通规则
"""

from pathlib import Path

import pytest

from app.core import team_manager as tm_mod

try:
    from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
except Exception:  # pragma: no cover
    load_plugin_tools = None


def _load_subagent_tools():
    """加载团队工具插件模块（幂等），返回模块。"""
    import importlib.util

    module_path = Path(__file__).resolve().parent.parent.parent / "plugins" / "system" / "tools" / "subagent_tools.py"
    spec = importlib.util.spec_from_file_location("subagent_tools_m1", module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tm(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager 实例。"""
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    return tm_mod.TeamManager()


def _make_tools(window_id, agent_name):
    """构造插件 impl 的 tool_ctx（含团队窗口上下文）"""
    return {"team_window_id": window_id, "team_agent_name": agent_name}


class TestTeamManagerRunAttribution:
    """TeamManager 团队归属（run_id/team_label）存储与查询"""

    def test_join_records_run_id_and_label(self, tm):
        """join_team 传 run_id/team_label → 成员记录含归属字段。"""
        tm.set_active_window_ids({"win_01"})
        tm.join_team("win_01", "build", run_id="run_abc", team_label="dev-team")
        members = tm.get_members()
        assert members[0]["run_id"] == "run_abc"
        assert members[0]["team_label"] == "dev-team"

    def test_join_without_attribution_legacy_compat(self, tm):
        """老调用（不传 run_id/team_label）→ 成员记录空归属（兼容，list 归入默认团队）。"""
        tm.set_active_window_ids({"win_01"})
        tm.join_team("win_01", "build")
        members = tm.get_members()
        assert members[0].get("run_id", "") == ""
        assert members[0].get("team_label", "") == ""

    def test_get_members_filter_by_run_id(self, tm):
        """get_members(run_id=...) 只返回该 run 的成员；None 返回全部。"""
        tm.set_active_window_ids({"win_01", "win_02", "win_03"})
        tm.join_team("win_01", "leader", run_id="run_a", team_label="team-a")
        tm.join_team("win_02", "build", run_id="run_a", team_label="team-a")
        tm.join_team("win_03", "build", run_id="run_b", team_label="team-b")

        all_members = tm.get_members()
        assert len(all_members) == 3

        run_a = tm.get_members(run_id="run_a")
        assert {m["window_id"] for m in run_a} == {"win_01", "win_02"}

        run_b = tm.get_members(run_id="run_b")
        assert {m["window_id"] for m in run_b} == {"win_03"}

        no_run = tm.get_members(run_id="")
        assert no_run == [], "传空串应返回无归属成员（本测试全有归属 → 空）"

    def test_get_member_run_id(self, tm):
        """get_member_run_id 反查窗口所属 run；非成员返回空。"""
        tm.set_active_window_ids({"win_01"})
        tm.join_team("win_01", "build", run_id="run_x")
        assert tm.get_member_run_id("win_01") == "run_x"
        assert tm.get_member_run_id("win_99") == ""

    def test_get_team_run_ids(self, tm):
        """get_team_run_ids 列出所有 run_id（去重保序）；无归属成员不产生 run。"""
        tm.set_active_window_ids({"win_01", "win_02", "win_03"})
        tm.join_team("win_01", "leader", run_id="run_a")
        tm.join_team("win_02", "build", run_id="run_a")
        tm.join_team("win_03", "build", run_id="run_b")
        tm.join_team("win_04", "plan")  # 无归属
        tm.set_active_window_ids({"win_01", "win_02", "win_03", "win_04"})
        tm.join_team("win_04", "plan")
        tm.set_active_window_ids({"win_01", "win_02", "win_03", "win_04"})
        assert tm.get_team_run_ids() == ["run_a", "run_b"]

    def test_get_team_label_fallback(self, tm):
        """无模板时 get_team_label 回退数据目录团队名（default）。"""
        assert tm.get_team_label() == "default"
        tm.set_template({"name": "dev-team", "description": "", "agents": []})
        assert tm.get_team_label() == "dev-team"

    def test_update_member_team_backfills_attribution(self, tm):
        """update_member_team 补写归属（历史会话恢复路径）；非成员静默跳过。"""
        tm.set_active_window_ids({"win_01"})
        tm.join_team("win_01", "build")  # 无归属
        tm.update_member_team("win_01", run_id="run_z", team_label="team-z")
        assert tm.get_member_run_id("win_01") == "run_z"
        assert tm.get_members(run_id="run_z")[0]["agent_name"] == "build"
        # 非成员静默跳过（不抛异常）
        tm.update_member_team("win_99", run_id="run_z", team_label="team-z")

    def test_cleanup_preserves_run_attribution(self, tm):
        """成员清理不影响存活成员的 run 归属。"""
        tm.set_active_window_ids({"win_01", "win_02"})
        tm.join_team("win_01", "build", run_id="run_a")
        tm.join_team("win_02", "plan", run_id="run_a")
        tm.set_active_window_ids({"win_01"})
        tm._cleanup_stale_members(tm.DEFAULT_TEAM)
        members = tm.get_members()
        assert len(members) == 1
        assert members[0]["run_id"] == "run_a"


class TestTeamListMembersMultiTeam:
    """team_list_members 多团队并存分组输出"""

    def _make_tm(self, members):
        """真实 TeamManager + 手工写入成员（含归属）"""
        tm = tm_mod.TeamManager()
        tm.set_active_window_ids({m["window_id"] for m in members})
        for m in members:
            tm.join_team(
                m["window_id"],
                m["agent_name"],
                run_id=m.get("run_id", ""),
                team_label=m.get("team_label", ""),
            )
        tm.set_template(
            {"name": "team-a", "description": "A 团队", "agents": [{"agent_name": "leader", "description": "统筹"}]}
        )
        return tm

    def test_single_team_no_other_section(self, monkeypatch, tmp_path):
        """单团队（无其他 run）→ 输出本团队明细，无其他团队段、无跨团队提示。"""
        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
        subagent = _load_subagent_tools()
        tm = self._make_tm(
            [
                {"window_id": "win_01", "agent_name": "leader", "run_id": "run_a", "team_label": "team-a"},
                {"window_id": "win_02", "agent_name": "build", "run_id": "run_a", "team_label": "team-a"},
            ]
        )
        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm))
        result = subagent._team_list_members(_make_tools("win_01", "leader"))
        assert result.success
        content = result.content
        assert "团队「team-a」" in content
        assert "leader@win_01" in content
        assert "build@win_02" in content
        assert "其他团队" not in content
        assert "团队间沟通" not in content

    def test_multi_team_groups_other_team(self, monkeypatch, tmp_path):
        """多团队并存 → 本团队明细 + 其他团队概要 + 跨团队沟通提示。"""
        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
        subagent = _load_subagent_tools()
        tm = self._make_tm(
            [
                {"window_id": "win_01", "agent_name": "leader", "run_id": "run_a", "team_label": "team-a"},
                {"window_id": "win_02", "agent_name": "build", "run_id": "run_a", "team_label": "team-a"},
                {"window_id": "win_03", "agent_name": "leader", "run_id": "run_b", "team_label": "team-b"},
                {"window_id": "win_04", "agent_name": "review", "run_id": "run_b", "team_label": "team-b"},
            ]
        )
        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm))
        # 当前窗口属于 team-a
        result = subagent._team_list_members(_make_tools("win_01", "leader"))
        assert result.success
        content = result.content
        # 本团队明细
        assert "团队「team-a」" in content
        assert "leader@win_01" in content
        assert "build@win_02" in content
        # 其他团队概要
        assert "其他团队 (1 个):" in content
        assert "团队「team-b」(2 人" in content
        assert "leader: leader" in content
        assert "review@win_04" in content
        # 跨团队沟通提示
        assert "团队间沟通" in content
        assert "只通过各自 leader" in content

    def test_multi_team_other_side_view(self, monkeypatch, tmp_path):
        """站在 team-b 视角 → team-a 成为「其他团队」。"""
        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
        subagent = _load_subagent_tools()
        tm = self._make_tm(
            [
                {"window_id": "win_01", "agent_name": "leader", "run_id": "run_a", "team_label": "team-a"},
                {"window_id": "win_03", "agent_name": "leader", "run_id": "run_b", "team_label": "team-b"},
            ]
        )
        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm))
        result = subagent._team_list_members(_make_tools("win_03", "leader"))
        assert result.success
        content = result.content
        assert "团队「team-b」" in content
        assert "团队「team-a」(1 人" in content

    def test_legacy_members_without_run_id_all_in_one(self, monkeypatch, tmp_path):
        """老成员（无 run_id）→ 全部视为本团队（单团队行为回归）。"""
        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
        subagent = _load_subagent_tools()
        tm = self._make_tm(
            [
                {"window_id": "win_01", "agent_name": "leader"},
                {"window_id": "win_02", "agent_name": "build"},
            ]
        )
        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm))
        result = subagent._team_list_members(_make_tools("win_01", "leader"))
        assert result.success
        content = result.content
        assert "leader@win_01" in content
        assert "build@win_02" in content
        assert "其他团队" not in content


class TestInjectTeamContextMultiTeam:
    """inject_team_context hook 多团队并存提示"""

    def _load_hook(self):
        import importlib.util

        module_path = (
            Path(__file__).resolve().parent.parent.parent / "plugins" / "system" / "hooks" / "inject_team_context.py"
        )
        spec = importlib.util.spec_from_file_location("inject_team_context_m1", module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_multi_team_injects_communication_rule(self, monkeypatch, tmp_path):
        """多团队并存 → hook 注入团队间沟通规则（推荐经 leader）。"""
        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
        hook_mod = self._load_hook()
        tm = tm_mod.TeamManager()
        tm.set_active_window_ids({"win_01", "win_03"})
        tm.join_team("win_01", "leader", run_id="run_a", team_label="team-a")
        tm.join_team("win_03", "leader", run_id="run_b", team_label="team-b")
        tm.set_template(
            {"name": "team-a", "description": "A 团队描述", "agents": [{"agent_name": "leader", "description": "统筹"}]}
        )
        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm))

        out = hook_mod.hook("SessionStart", {"is_team_member": True, "window_id": "win_01"})
        assert "团队「team-a」" in out
        assert "多团队并存" in out
        assert "只通过各自 leader 传递团队间消息" in out

    def test_single_team_no_communication_rule(self, monkeypatch, tmp_path):
        """单团队 → 不注入多团队并存提示（避免噪音）。"""
        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
        hook_mod = self._load_hook()
        tm = tm_mod.TeamManager()
        tm.set_active_window_ids({"win_01"})
        tm.join_team("win_01", "leader", run_id="run_a", team_label="team-a")
        tm.set_template(
            {"name": "team-a", "description": "A 团队描述", "agents": [{"agent_name": "leader", "description": "统筹"}]}
        )
        monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm))

        out = hook_mod.hook("SessionStart", {"is_team_member": True, "window_id": "win_01"})
        assert "团队「team-a」" in out
        assert "多团队并存" not in out

    def test_non_team_member_returns_empty(self, monkeypatch, tmp_path):
        """非团队成员 → 空串（不注入）。"""
        monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
        hook_mod = self._load_hook()
        assert hook_mod.hook("SessionStart", {"is_team_member": False, "window_id": "win_01"}) == ""
