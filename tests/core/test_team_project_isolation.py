# -*- coding: utf-8 -*-
"""项目隔离：团队运行按项目解析（TeamManager.resolve_run_id_for_project）。

背景：`/team --join` 原先取 team.json 顶层 run_id（跨项目全局单槽），
任意项目的标签页都会并进最近一次团队运行。修复后按窗口 `_current_project`
匹配 `projects_by_run_id`，未命中才新建。

覆盖：
1. 同项目再次解析复用同一 run
2. 不同项目各自新建，成员不串团队
3. 新建分支写入 project / workdir / runtime 模板槽（团队框标题=项目名）
4. project 为空回退顶层 run（兼容无项目上下文调用）
5. 项目映射存在但无任何成员归属的 run 不复用（视为团队已解散）
"""

import pytest


@pytest.fixture
def tm(team_manager):
    """隔离数据目录的 TeamManager，活跃窗口集合显式可控。"""
    return team_manager


class TestResolveRunIdForProject:
    def test_same_project_reuses_run(self, tm):
        tm.set_active_window_ids({"win_a1"})
        run_a = tm.resolve_run_id_for_project("项目甲", "/tmp/a")
        tm.join_team("win_a1", "build", run_id=run_a, team_label="项目甲")

        assert tm.resolve_run_id_for_project("项目甲") == run_a

    def test_different_projects_get_separate_runs(self, tm):
        tm.set_active_window_ids({"win_a1", "win_b1"})
        run_a = tm.resolve_run_id_for_project("项目甲", "/tmp/a")
        tm.join_team("win_a1", "build", run_id=run_a, team_label="项目甲")

        run_b = tm.resolve_run_id_for_project("项目乙", "/tmp/b")
        assert run_b and run_b != run_a
        tm.join_team("win_b1", "build", run_id=run_b, team_label="项目乙")

        assert tm.get_member_run_id("win_b1") == run_b
        assert tm.get_member_run_id("win_a1") == run_a
        assert tm.resolve_run_id_for_project("项目甲") == run_a

    def test_new_run_records_project_workdir_and_label(self, tm):
        tm.set_active_window_ids(set())
        run_id = tm.resolve_run_id_for_project("项目甲", "/tmp/a")

        assert tm.get_project_for_run_id(run_id) == "项目甲"
        assert tm.get_team_workdir(run_id) == "/tmp/a"
        assert (tm.get_template_for_run_id(run_id) or {}).get("name") == "项目甲"

    def test_empty_project_falls_back_to_top_run(self, tm):
        tm.set_active_window_ids(set())
        top = tm.start_team_run()

        assert tm.resolve_run_id_for_project("") == top

    def test_run_without_members_not_reused(self, tm):
        tm.set_active_window_ids(set())
        orphan = tm.start_team_run(force=True)
        tm.set_project_for_run_id("项目甲", orphan)

        assert tm.resolve_run_id_for_project("项目甲") != orphan

    def test_legacy_top_level_project_reused(self, tm):
        """单团队老数据（只有顶层 project/run_id）同项目仍复用，不凭空建新团队。"""
        tm.set_active_window_ids({"win_a1"})
        top = tm.start_team_run()
        tm.set_team_project("项目甲")
        tm.join_team("win_a1", "build", run_id=top, team_label="项目甲")

        assert tm.resolve_run_id_for_project("项目甲") == top
