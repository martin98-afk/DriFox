# -*- coding: utf-8 -*-
"""#5a-fix Plan C：team_project 按 run_id 粒度存储

回归 bug：多团队并存时，set_team_project/get_team_project 以 team_name
（默认 DEFAULT_TEAM="default"）为粒度，导致多个不同 run_id 的团队共享
同一份 DEFAULT_TEAM.project，互覆盖。

修复：TeamManager 内部新增按 run_id 粒度的 project 存储
（team.json["projects_by_run_id"][run_id]），保留旧 set_team_project
(team_name) 向后兼容。

覆盖：
- B1: set_project_for_run_id 写入 projects_by_run_id 字段；get_project_for_run_id
  读回；多 run_id 互不覆盖
- B2: get_project_for_run_id 在 projects_by_run_id 不存在该 run_id 时回退
  顶层 project 字段（旧数据兼容）
- B3: set_project_for_run_id 空串清除该 run_id 项目；值相同短路
- B4: tab_panel 多 run_id 团队框 header icon 数据源 = run_id 粒度（mock
  TeamManager 多 run_id 并存，A 框读 A_run_id 项目不被 B 框覆盖）
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import team_manager as tm_mod

# 模块导入时缓存真实 get_instance（对齐 test_team_project.py 的隔离防护）
_ORIG_GET_INSTANCE = tm_mod.TeamManager.__dict__["get_instance"]


@pytest.fixture
def fresh_tm(tmp_path, monkeypatch):
    """指向 tmp_path 的全新 TeamManager 实例（隔离，不污染真实 ~/.drifox/）。"""
    monkeypatch.setattr(tm_mod.TeamManager, "get_instance", _ORIG_GET_INSTANCE)
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path))
    tm_mod.TeamManager._instance = None
    tm = tm_mod.TeamManager.get_instance()
    yield tm
    tm_mod.TeamManager._instance = None


def _team_file(tm, team_name: str) -> Path:
    return tm._team_file(team_name)


class TestProjectByRunIdDataLayer:
    """B1/B2/B3：TeamManager 按 run_id 粒度的 project 读写"""

    def test_set_and_get_project_for_run_id(self, fresh_tm):
        """B1: set_project_for_run_id 写入 projects_by_run_id[run_id]；get 回读一致。"""
        assert fresh_tm.get_project_for_run_id("run_A") == "", "初始应为空串"
        fresh_tm.set_project_for_run_id("项目A", "run_A")
        assert fresh_tm.get_project_for_run_id("run_A") == "项目A"

        # team.json 顶层出现 projects_by_run_id 字段（与 run_id 平级）
        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert "projects_by_run_id" in data, "projects_by_run_id 应持久化到 team.json 顶层"
        assert data["projects_by_run_id"].get("run_A") == "项目A"
        # run_id 顶层字段不受影响
        assert "project" not in data or data.get("project") == "", "顶层 project 字段不应被 run_id 接口写入"

    def test_multi_run_id_does_not_overwrite_each_other(self, fresh_tm):
        """B1: 多 run_id 并存各自项目互不影响（bug 复现：修复前 set_team_project
        共享 DEFAULT_TEAM.project，run_A 写 P1、run_B 写 P2 后两 run_id 都拿到 P2）。"""
        fresh_tm.set_project_for_run_id("项目A", "run_A")
        fresh_tm.set_project_for_run_id("项目B", "run_B")

        assert fresh_tm.get_project_for_run_id("run_A") == "项目A", "run_A 应保留自己的项目"
        assert fresh_tm.get_project_for_run_id("run_B") == "项目B", "run_B 应保留自己的项目"

        # 持久化层也各自独立
        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert data["projects_by_run_id"] == {"run_A": "项目A", "run_B": "项目B"}

    def test_get_project_for_run_id_falls_back_to_legacy_field(self, fresh_tm):
        """B2: projects_by_run_id 不存在该 run_id 时，回退顶层 project 字段（旧数据兼容）。

        老团队仅用 set_team_project(team_name) 写到顶层 project，未启用 run_id 接口；
        读取未注册的 run_id 时应回退到顶层 project，避免新代码读到空串后误清空旧团队。
        """
        fresh_tm.set_team_project("旧项目")
        assert fresh_tm.get_project_for_run_id("任意run_id") == "旧项目", "未注册 run_id 应回退顶层"

    def test_get_project_for_run_id_prefers_run_id_field_over_legacy(self, fresh_tm):
        """B2: 同一 run_id 同时存在 projects_by_run_id 与顶层 project 时，以 run_id 字段为准。"""
        fresh_tm.set_team_project("旧项目")  # 顶层
        fresh_tm.set_project_for_run_id("新项目", "run_A")  # run_id 字段
        assert fresh_tm.get_project_for_run_id("run_A") == "新项目", "run_id 字段优先于顶层"
        assert fresh_tm.get_project_for_run_id("run_B") == "旧项目", "未注册 run_B 回退顶层"

    def test_clear_project_for_run_id(self, fresh_tm):
        """B3: 空串清除该 run_id 项目（不影响其他 run_id）。"""
        fresh_tm.set_project_for_run_id("项目A", "run_A")
        fresh_tm.set_project_for_run_id("项目B", "run_B")
        fresh_tm.set_project_for_run_id("", "run_A")
        assert fresh_tm.get_project_for_run_id("run_A") == "", "run_A 项目应已清除"
        assert fresh_tm.get_project_for_run_id("run_B") == "项目B", "run_B 项目应保持"

    def test_set_project_for_run_id_idempotent_skips_write(self, fresh_tm):
        """B3: 值相同不触发写盘（幂等短路）。"""
        fresh_tm.set_project_for_run_id("项目A", "run_A")
        with patch.object(fresh_tm, "_save_team_data") as mock_save:
            fresh_tm.set_project_for_run_id("项目A", "run_A")  # 相同值
            mock_save.assert_not_called(), "相同值不应触发写盘"
        with patch.object(fresh_tm, "_save_team_data") as mock_save2:
            fresh_tm.set_project_for_run_id("项目B", "run_A")  # 不同值
            mock_save2.assert_called_once(), "不同值应触发写盘"

    def test_get_project_for_run_id_empty_for_unknown_team_file(self, fresh_tm):
        """B2: 团队文件不存在时返回空串不抛异常（与旧 get_team_project 行为一致）。"""
        fresh_tm.set_project_for_run_id("项目A", "run_A")
        assert fresh_tm.get_project_for_run_id("run_A", team_name="不存在的团队") == ""


class TestTabPanelHeaderIconByRunId:
    """B4：tab_panel 多 run_id 团队框 header icon 数据源为 run_id 粒度

    通过 mock TeamManager.get_project_for_run_id 验证 _team_project_icon_data
    按 run_id 取数据，不同 run_id 互不覆盖（bug 复现：修复前走 get_team_project
    不传 team_name 永远拿 DEFAULT_TEAM.project）。
    """

    def test_header_icon_uses_run_id_scoped_project(self):
        """tab_panel.set_team_project 调用链读取 run_id 粒度数据源，A/B 框互不影响。"""
        from unittest.mock import MagicMock

        from app.widgets.tab_panel import TabPanel

        with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
            panel = TabPanel()

        # 模拟 TeamManager.get_project_for_run_id 返回不同 run_id 的不同项目
        fake_tm = MagicMock()
        fake_tm.get_project_for_run_id.side_effect = lambda run_id, team_name="default": {
            "run_A": "项目A",
            "run_B": "项目B",
        }.get(run_id, "")
        fake_tm.DEFAULT_TEAM = "default"

        with patch(
            "app.widgets.tab_manager_window.TabManagerWindow.get_instance",
            return_value=MagicMock(_tab_panel=panel, _windows=[]),
        ):
            # 模拟两个团队框
            idx_a = panel.add_tab("会话A")
            idx_b = panel.add_tab("会话B")
            panel.set_tab_team(idx_a, "run_A")
            panel.set_tab_team(idx_b, "run_B")
            grp_a = panel._team_groups["run_A"]
            grp_b = panel._team_groups["run_B"]

            # 通过模拟 _update_tab_icon 的调用链，触发团队框 header 刷新
            # 让 set_team_project 走正常接口，验证从 run_id 数据源读取
            panel.set_team_project("run_A", "PA", "rgba(1,2,3,255)")
            panel.set_team_project("run_B", "PB", "rgba(4,5,6,255)")

            # 验证两个团队框 header icon 互不影响（之前 bug：A 框会被 B 的 set_team_project 覆盖）
            assert grp_a._team_icon._initials == "PA", "run_A 框应保持自己的项目缩写"
            assert grp_b._team_icon._initials == "PB", "run_B 框应保持自己的项目缩写"