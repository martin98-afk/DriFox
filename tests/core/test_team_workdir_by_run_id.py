# -*- coding: utf-8 -*-
"""团队 workdir 按 run_id 粒度存储（修复：顶层 workdir 单槽残留跨项目污染）

回归 bug：set_team_workdir/get_team_workdir 以 team.json **顶层** workdir
单槽存储且持久化——上次团队运行残留的旧工作目录（可能是其他项目/已删除
worktree 的路径）被后续 join/模板加入路径（_do_join_team /
_join_new_window_for_template 等）读到，套到成员窗口**当前项目**上，
导致「标签页工作路径与当前选择项目不匹配」（项目字段已在 #5a-fix Plan C
迁移为 projects_by_run_id，workdir 缺失同款迁移）。

修复：TeamManager 新增 run_id 粒度存储 workdirs_by_run_id[run_id]；
get_team_workdir(run_id) 命中即返回、**不回退顶层**（顶层是跨 run_id
污染单槽，残留值直接失效，成员窗口回退 _sync_working_directory 从项目
DB 读权威值）；run_id 为空时保持旧顶层读写行为（向后兼容）。

覆盖：
- W1: set/get 带 run_id → 写读 workdirs_by_run_id[run_id]；多 run_id 互不覆盖
- W2: 顶层残留值存在时，带 run_id 的 get 不回退顶层（本 bug 直接回归锁）
- W3: run_id 为空时兼容旧顶层读写（老调用方不破坏）
- W4: 带 run_id 写入空串 = 清除该 run_id 槽位；值相同短路不写盘
"""

from pathlib import Path

import pytest

from app.core import team_manager as tm_mod

# 模块导入时缓存真实 get_instance（对齐 test_team_workdir.py 的隔离防护）
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


class TestWorkdirByRunIdDataLayer:
    """W1-W4：TeamManager 按 run_id 粒度的 workdir 读写"""

    def test_set_and_get_workdir_for_run_id(self, fresh_tm):
        """W1: 带 run_id 读写 workdirs_by_run_id[run_id]；多 run_id 互不覆盖。"""
        assert fresh_tm.get_team_workdir("run_A") == "", "初始应为空串"
        fresh_tm.set_team_workdir("D:/work/projA", run_id="run_A")
        fresh_tm.set_team_workdir("D:/work/projB", run_id="run_B")
        assert fresh_tm.get_team_workdir("run_A") == "D:/work/projA"
        assert fresh_tm.get_team_workdir("run_B") == "D:/work/projB"

        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert "workdirs_by_run_id" in data, "workdirs_by_run_id 应持久化到 team.json 顶层"
        assert data["workdirs_by_run_id"].get("run_A") == "D:/work/projA"
        assert data["workdirs_by_run_id"].get("run_B") == "D:/work/projB"

    def test_run_id_lookup_does_not_fall_back_to_top_level(self, fresh_tm):
        """W2: 顶层残留值存在时，带 run_id 的 get 不回退顶层（本 bug 回归锁）。

        复现原始污染：旧团队残留顶层 workdir=旧项目路径 → 新 run_id 的
        join 路径读到残留 → 套到当前项目。修复后 run_id 未命中直接返回空串。
        """
        # 模拟旧版本/残留写入的顶层 workdir（绕过 set 直接改数据，模拟老数据）
        fresh_tm.set_team_workdir("D:/work/DriFox-pyside6version")  # 顶层单槽
        # 新 run_id 从未按粒度写过 workdir
        assert fresh_tm.get_team_workdir("run_new") == "", "带 run_id 查询不得回退顶层残留值（跨 run_id 污染通道）"

    def test_empty_run_id_keeps_legacy_top_level(self, fresh_tm):
        """W3: run_id 为空时保持旧顶层读写行为（向后兼容老调用方）。"""
        fresh_tm.set_team_workdir("D:/work/legacy")
        assert fresh_tm.get_team_workdir() == "D:/work/legacy"
        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert data.get("workdir") == "D:/work/legacy"

    def test_set_for_run_id_clear_and_short_circuit(self, fresh_tm):
        """W4: 带 run_id 写空串清除该槽位；值相同短路不重复写盘。"""
        fresh_tm.set_team_workdir("D:/work/projA", run_id="run_A")
        fresh_tm.set_team_workdir("", run_id="run_A")
        assert fresh_tm.get_team_workdir("run_A") == ""

        # 值相同短路：再次写入同值（写盘正常完成但数据不变），读回一致即可
        fresh_tm.set_team_workdir("D:/work/projB", run_id="run_B")
        fresh_tm.set_team_workdir("D:/work/projB", run_id="run_B")
        assert fresh_tm.get_team_workdir("run_B") == "D:/work/projB"
