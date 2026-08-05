# -*- coding: utf-8 -*-
"""子任务 #B2-T：成员实时工作状态（runtime status）通道单测

覆盖：
1. set_member_runtime_status(window_id, "busy") 后 get_member_runtime_status 返回 busy
2. "idle" 与 "busy" 互切正常
3. 非团队成员窗口 set/get 安全返回 idle、不落脏数据
4. 成员被清理（_cleanup_stale_members / leave_team）时 runtime_status 随 members 一并清除
5. team.json members[wid] 中 runtime_status 字段正确落盘/读取
6. get_member_busy_status 查询合并：无邮件时 runtime busy → "busy"；idle → "idle"；
   有 pending/running 邮件时邮件状态优先级不破坏
7. team_list_members 显示路径：runtime busy 无邮件 → 「🟡 执行任务中」（回归锁定）

设计说明：
- TeamManager 直接构造实例（隔离 tmp_path）+ 将 get_instance 单例指向该隔离实例
  （monkeypatch 自动恢复），保证 team_tools 查询路径（TeamTools._get_team_manager
  → get_instance）也命中隔离数据，避免读到真实 ~/.drifox/teams 残留成员
- 落盘断言直接读 team.json 文件，验证持久化语义而非仅内存缓存
"""

import pytest

from app.core import team_manager as tm_mod


@pytest.fixture
def tm(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager 实例。"""
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    tm = tm_mod.TeamManager()
    # 🛡️ 让 get_instance 单例指向隔离实例（TeamTools 查询路径走单例），
    # 避免读到真实 teams 目录残留数据导致断言漂移。
    monkeypatch.setattr(tm_mod.TeamManager, "_instance", tm, raising=False)
    return tm


def _joined_tm(tm):
    """构造含 2 名成员的团队：win_01(leader) / win_02(build)。"""
    tm.join_team("win_01", "leader")
    tm.join_team("win_02", "build")


def _read_team_file(tm):
    """直接读 team.json（绕过缓存，验证落盘）。"""
    import json

    return json.loads(tm._team_file(tm.DEFAULT_TEAM).read_text(encoding="utf-8"))


class TestRuntimeStatusBasic:
    """1-2：busy 设置 / idle↔busy 互切"""

    def test_set_busy_then_get_returns_busy(self, tm):
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "busy")
        assert tm.get_member_runtime_status("win_02") == "busy"

    def test_idle_busy_switch_both_directions(self, tm):
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "busy")
        tm.set_member_runtime_status("win_02", "idle")
        assert tm.get_member_runtime_status("win_02") == "idle"
        tm.set_member_runtime_status("win_02", "busy")
        assert tm.get_member_runtime_status("win_02") == "busy"
        tm.set_member_runtime_status("win_02", "idle")
        assert tm.get_member_runtime_status("win_02") == "idle", "idle↔busy 应可反复互切"

    def test_member_without_record_defaults_to_idle(self, tm):
        _joined_tm(tm)
        assert tm.get_member_runtime_status("win_02") == "idle", "从未设置过的成员默认空闲"


class TestNonMemberSafety:
    """3：非团队成员窗口 set/get 安全"""

    def test_get_non_member_returns_idle(self, tm):
        _joined_tm(tm)
        assert tm.get_member_runtime_status("win_99") == "idle", "非成员读取应安全返回 idle"

    def test_set_non_member_ignored_no_dirty_data(self, tm):
        _joined_tm(tm)
        tm.set_member_runtime_status("win_99", "busy")
        assert tm.get_member_runtime_status("win_99") == "idle", "非成员 busy 设置应被忽略"
        data = _read_team_file(tm)
        assert "win_99" not in data["members"], "非成员不应出现在 team.json members 中"
        # 落盘内容不被污染：win_99 不产生 runtime_status 记录（也没有 members 条目）

    def test_set_invalid_status_ignored(self, tm):
        """非法状态（非 busy/idle）应静默忽略，保持原值。"""
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "weird")
        assert tm.get_member_runtime_status("win_02") == "idle", "非法状态不应生效"
        tm.set_member_runtime_status("win_02", "busy")
        tm.set_member_runtime_status("win_02", "hacked")
        assert tm.get_member_runtime_status("win_02") == "busy", "非法状态不应覆盖有效值"


class TestCleanupRemovesRuntimeStatus:
    """4：成员被清理时 runtime_status 随 members 一并清除"""

    def test_cleanup_stale_members_removes_runtime_status(self, tm):
        _joined_tm(tm)
        tm.set_member_runtime_status("win_01", "busy")
        tm.set_member_runtime_status("win_02", "busy")
        # 只有 win_02 活跃 → win_01 被判定失效清理
        tm.set_active_window_ids({"win_02"})
        tm._cleanup_stale_members(tm.DEFAULT_TEAM)
        assert "win_01" not in tm._get_team_data("default")["members"], "失效成员应从 members 移除"
        assert tm.get_member_runtime_status("win_01") == "idle", "被清理成员的 runtime 状态应随成员一并消失"

    def test_leave_team_removes_runtime_status(self, tm):
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "busy")
        tm.leave_team("win_02")
        assert tm.get_member_runtime_status("win_02") == "idle", "离开团队后 runtime 状态应随成员一并清除"
        data = _read_team_file(tm)
        assert "win_02" not in data["members"]


class TestPersistence:
    """5：team.json members[wid] 中 runtime_status 字段落盘/读取"""

    def test_runtime_status_persisted_to_team_json(self, tm):
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "busy")
        data = _read_team_file(tm)
        assert data["members"]["win_02"].get("runtime_status") == "busy", "busy 应落盘到 team.json"
        tm.set_member_runtime_status("win_02", "idle")
        data = _read_team_file(tm)
        assert data["members"]["win_02"].get("runtime_status") == "idle", "idle 应落盘到 team.json"

    def test_member_other_fields_preserved(self, tm):
        """写入 runtime_status 不破坏成员既有字段（agent_name/joined_at 等）。"""
        _joined_tm(tm)
        before = tm.get_member_by_agent("build").get("window_id")
        tm.set_member_runtime_status("win_02", "busy")
        member = tm.get_member_by_agent("build")
        assert member["window_id"] == before == "win_02"
        assert member["agent_name"] == "build"
        assert member["runtime_status"] == "busy"


class TestBusyStatusMerge:
    """6：get_member_busy_status 查询合并（邮件状态优先级不破坏）"""

    def test_no_mail_runtime_busy_returns_busy(self, tm):
        tm.set_active_window_ids({"win_01", "win_02"})
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "busy")
        assert tm.get_member_busy_status("win_02") == "busy", "无邮件时 runtime busy → 判定 busy"

    def test_no_mail_runtime_idle_returns_idle(self, tm):
        tm.set_active_window_ids({"win_01", "win_02"})
        _joined_tm(tm)
        assert tm.get_member_busy_status("win_02") == "idle", "无邮件且 runtime 空闲 → idle"
        tm.set_member_runtime_status("win_02", "idle")
        assert tm.get_member_busy_status("win_02") == "idle"

    def test_pending_mail_takes_priority_over_runtime(self, tm):
        """有 pending 任务邮件时即使 runtime idle 也应 busy（邮件状态优先）。"""
        tm.set_active_window_ids({"win_01", "win_02"})
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "idle")
        mail_id = tm.send_task(
            from_window="win_01", from_agent="leader", to_identifier="build", task_description="请修复 bug"
        )
        assert mail_id is not None
        assert tm.get_member_busy_status("win_02") == "busy", "pending 邮件应优先于 runtime idle"

    def test_running_mail_takes_priority_over_runtime(self, tm):
        """有 running 任务邮件时即使 runtime idle 也应 busy。"""
        tm.set_active_window_ids({"win_01", "win_02"})
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "idle")
        mail_id = tm.send_task(
            from_window="win_01", from_agent="leader", to_identifier="build", task_description="正在执行"
        )
        tm.mark_mail_running(mail_id, "win_02")
        assert tm.get_member_busy_status("win_02") == "busy", "running 邮件应优先于 runtime idle"

    def test_mail_done_falls_back_to_runtime(self, tm):
        """邮件 done 后回落：runtime busy → busy；runtime idle → idle。"""
        tm.set_active_window_ids({"win_01", "win_02"})
        _joined_tm(tm)
        mail_id = tm.send_task(
            from_window="win_01", from_agent="leader", to_identifier="build", task_description="任务"
        )
        tm.mark_mail_done(mail_id, "win_02", "ok")
        tm.set_member_runtime_status("win_02", "busy")
        assert tm.get_member_busy_status("win_02") == "busy", "邮件 done 后应回落到 runtime busy"
        tm.set_member_runtime_status("win_02", "idle")
        assert tm.get_member_busy_status("win_02") == "idle", "邮件 done 后应回落到 runtime idle"


class TestTeamToolsDisplay:
    """7：team_list_members 显示路径锁定（runtime busy 无邮件 → 执行任务中）"""

    @staticmethod
    def _make_tools(window_id, agent_name):
        from app.tools.team_tools import TeamTools

        class _BT:
            _team_window_id = window_id
            _team_agent_name = agent_name

        return TeamTools(_BT())

    @staticmethod
    def _line(result, member_id):
        """取某成员的行（如 build@win_02），供逐成员断言。"""
        for line in result.content.splitlines():
            if member_id in line:
                return line
        raise AssertionError(f"输出中未找到成员 {member_id}:\n{result.content}")

    def test_runtime_busy_shows_executing(self, tm):
        """成员流式/思考中（runtime busy、无任务邮件）查询显示「🟡 执行任务中」。"""
        tm.set_active_window_ids({"win_01", "win_02"})
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "busy")
        tools = self._make_tools("win_01", "leader")
        result = tools.team_list_members()
        assert result.success, result.error
        build_line = self._line(result, "build@win_02")
        assert "🟡 执行任务中" in build_line, f"runtime busy 应显示执行任务中:\n{result.content}"
        assert "🟢 空闲" not in build_line, "busy 成员自身不应显示空闲"

    def test_runtime_idle_shows_idle(self, tm):
        """成员空闲时显示「🟢 空闲」。"""
        tm.set_active_window_ids({"win_01", "win_02"})
        _joined_tm(tm)
        tools = self._make_tools("win_01", "leader")
        result = tools.team_list_members()
        assert result.success, result.error
        build_line = self._line(result, "build@win_02")
        assert "🟢 空闲" in build_line, f"空闲成员应显示空闲:\n{result.content}"

    def test_pending_mail_still_shows_waiting_label(self, tm):
        """回归：有 pending 邮件时仍显示「⏳ 等待处理」（邮件状态显示逻辑不被破坏）。"""
        tm.set_active_window_ids({"win_01", "win_02"})
        _joined_tm(tm)
        tm.send_task(
            from_window="win_01", from_agent="leader", to_identifier="build", task_description="等待处理的活"
        )
        tools = self._make_tools("win_01", "leader")
        result = tools.team_list_members()
        assert result.success, result.error
        build_line = self._line(result, "build@win_02")
        assert "⏳ 等待处理" in build_line, f"pending 邮件应显示等待处理:\n{result.content}"
        assert "等待处理的活" in build_line, "pending 邮件应带任务摘要"