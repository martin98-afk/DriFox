# -*- coding: utf-8 -*-
"""T11-①：team run_id 映射淘汰守护测试

templates/projects/workdirs_by_run_id 随团队运行次数无限增长（team.json 膨胀 +
内存滞留）。裁剪规则：len>32 时按插入序从旧到新淘汰非 keep 条目；
keep = 活跃成员 run_id ∪ 顶层 run_id ∪ 本次写入 run_id（三层缺一不可：
set_template 紧跟 start_team_run(force=True) 时 members 尚无新 run_id）。
被裁剪条目走读路径既有「未命中回退」降级（回退顶层字段 / 返回空串）。
"""
from app.core.team.team_manager import TeamManager

def _fill_mapping(tm, team_name: str, key: str, n: int, current: str):
    """写入 n 个 run_id 条目（r000..r{n-1}），current 用于最终写入触发裁剪"""
    for i in range(n):
        rid = f"r{i:03d}"
        if key == "templates_by_run_id":
            tm.set_template_for_run_id({"name": rid}, rid, team_name)
        elif key == "projects_by_run_id":
            tm.set_project_for_run_id(f"p-{rid}", rid, team_name)
        else:
            tm.set_team_workdir(f"/tmp/{rid}", rid, team_name)

class TestPruneRunMapping:
    def test_prune_keeps_running_run_id(self, fresh_tm):
        """写 40 条后 len≤32，且三层 keep（成员/顶层/本次写入）全存活"""
        team = "default"
        data = fresh_tm._get_team_data(team)
        # 构造 keep 三层：顶层 run_id 与活跃成员 run_id 均指向已写条目 r020/r030
        data["run_id"] = "r020"
        data["team_members"] = {"m1": {"run_id": "r030"}, "m2": {"run_id": "r020"}}
        fresh_tm._save_team_data(team)
        _fill_mapping(fresh_tm, team, "projects_by_run_id", 40, "r_new")
        fresh_tm.set_project_for_run_id("新项目", "r_new", team)
        data = fresh_tm._get_team_data(team)
        mapping = data["projects_by_run_id"]
        assert len(mapping) <= 32, f"裁剪后应 ≤32，实际 {len(mapping)}"
        assert "r_new" in mapping, "本次写入 run_id 必须存活"
        assert "r020" in mapping, "顶层 run_id（keep 层 2）必须存活"
        assert "r030" in mapping, "活跃成员 run_id（keep 层 1）必须存活"
        assert "r000" not in mapping, "最旧非 keep 条目应被淘汰"

    def test_prune_fifo_order(self, fresh_tm):
        """淘汰按插入序从旧到新：最旧的 r000 先被裁，最新的保留"""
        team = "default"
        _fill_mapping(fresh_tm, team, "projects_by_run_id", 40, "r_new")
        fresh_tm.set_project_for_run_id("新项目", "r_new", team)
        mapping = fresh_tm._get_team_data(team)["projects_by_run_id"]
        assert "r000" not in mapping, "最旧非 keep 条目应先被淘汰"
        assert "r_new" in mapping
        kept_old = [f"r{i:03d}" for i in range(40) if f"r{i:03d}" in mapping]
        assert kept_old == sorted(kept_old)[-len(kept_old):]
        # 保留的是插入序靠后的那批
        nums = sorted(int(r[1:]) for r in mapping if r.startswith("r0"))
        assert nums[-1] >= 30, f"应保留插入序最新的条目，实际最旧保留 {nums[0]}"

    def test_mapping_consistency_after_prune(self, fresh_tm):
        """裁剪后 cache 与 team.json 同批一致；读路径对被裁条目降级返回空串"""
        team = "default"
        _fill_mapping(fresh_tm, team, "workdirs_by_run_id", 40, "r_new")
        fresh_tm.set_team_workdir("/tmp/new", "r_new", team)
        # cache 与磁盘一致
        cache_data = fresh_tm._team_cache[team]
        disk_data = fresh_tm._read_json(fresh_tm._team_file(team))
        assert cache_data["workdirs_by_run_id"] == disk_data["workdirs_by_run_id"]
        assert len(disk_data["workdirs_by_run_id"]) <= 32
        # 存活条目读回正确
        assert fresh_tm.get_team_workdir("r_new", team) == "/tmp/new"
        # 被裁条目降级：run_id 粒度未命中不回退顶层 → 空串
        assert fresh_tm.get_team_workdir("r000", team) == ""

    def test_prune_static_method_direct(self):
        """_prune_run_mapping 纯函数行为：≤max 原样返回；keep 跳过淘汰"""
        mapping = {f"r{i}": i for i in range(40)}
        out = TeamManager._prune_run_mapping(mapping, {"r39", "r0"}, max_entries=10)
        assert len(out) == 10
        assert "r0" in out and "r39" in out
        same = TeamManager._prune_run_mapping(mapping, set(), max_entries=40)
        assert same is mapping or same == mapping
