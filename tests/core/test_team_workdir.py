# -*- coding: utf-8 -*-
"""#5b 团队级统一工作目录/工作树（一人改工作目录全员同步）测试

覆盖：
数据层（TeamManager）：
- B1: set_team_workdir/get_team_workdir 顶层 workdir 字段读写；team.json 顶层出现
  "workdir"；值相同不触发写盘（幂等短路）
- B2: 旧 team.json（无 workdir 字段）兼容 → get_team_workdir 返回 "" 不抛异常
- B3: workdir 放顶层：成员清理不丢失

广播层（main_widget._broadcast_team_workdir）：
- B4: 广播仅发给同团队（同 run_id）成员；非成员/他团队不受影响；发送方不自发
- B5: 发送方不自发应用（防循环）；接收方不转发（静态校验 + mock）
- B6: _apply_team_workdir 值相等跳过；不同值同步 _current_workdir/tool_executor/
  记忆卡片分支；空值回退临时工作目录
- B7: 注入点：_on_working_dir_changed / _switch_to_worktree / _restore_main_repo
  应广播；成员创建/加入路径（_spawn_team_member_window / _do_join_team /
  _join_new_window_for_template）应读取并应用团队 workdir

风格对齐 tests/core/test_team_project.py：
- fresh_tm fixture（tmp_path + monkeypatch 隔离 TeamManager）
- 窗口行为用例用 object.__new__ 轻量实例 + mock _instances，避免完整 UI 初始化
- 注入点静态校验读 main_widget.py 源码文本
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core import team_manager as tm_mod

# 模块导入时缓存真实 get_instance（对齐 test_team_project.py 的隔离防护）
_ORIG_GET_INSTANCE = tm_mod.TeamManager.__dict__["get_instance"]

_MAIN_WIDGET = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"


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


def _method_body(text: str, method_name: str) -> str:
    """提取 main_widget 中指定方法的源码文本（对齐 test_team_project.py 风格）。"""
    start = text.find(f"    def {method_name}(")
    assert start >= 0, f"main_widget 缺少方法 {method_name}"
    body_end = len(text)
    for probe in ("\n    def ", "\n    class "):
        idx = text.find(probe, start + 10)
        if idx >= 0:
            body_end = min(body_end, idx)
    return text[start:body_end]


def _method_calls(method_name: str) -> set:
    """AST 解析 main_widget 指定方法体内的函数调用名集合（忽略 docstring 文本）。"""
    import ast

    tree = ast.parse(_MAIN_WIDGET.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != method_name:
            continue
        calls = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                if isinstance(sub.func, ast.Name):
                    calls.add(sub.func.id)
                elif isinstance(sub.func, ast.Attribute):
                    calls.add(sub.func.attr)
        return calls
    return set()


class TestTeamWorkdirDataLayer:
    """B1/B2/B3：TeamManager 顶层 workdir 字段读写与旧数据兼容"""

    def test_set_and_get_team_workdir(self, fresh_tm, tmp_path):
        """B1: set_team_workdir 写入顶层、get_team_workdir 读回。"""
        assert fresh_tm.get_team_workdir() == "", "初始应为空串"
        wd = str(tmp_path / "workdir")
        fresh_tm.set_team_workdir(wd)
        assert fresh_tm.get_team_workdir() == wd

        # team.json 顶层出现 "workdir"（与 project/run_id 平级，非成员级）
        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert data.get("workdir") == wd, "workdir 应持久化到 team.json 顶层"
        assert "workdir" in data
        members = data.get("members", {})
        assert not any("workdir" in m for m in members.values()), "workdir 不应放在成员级"

    def test_set_team_workdir_update_and_clear(self, fresh_tm, tmp_path):
        """B1: 更新/清除工作目录值正常覆盖。"""
        wd_a = str(tmp_path / "a")
        wd_b = str(tmp_path / "b")
        fresh_tm.set_team_workdir(wd_a)
        fresh_tm.set_team_workdir(wd_b)
        assert fresh_tm.get_team_workdir() == wd_b
        fresh_tm.set_team_workdir("")
        assert fresh_tm.get_team_workdir() == "", "清除后应回空串"

    def test_set_team_workdir_idempotent_skips_write(self, fresh_tm, tmp_path):
        """B1: 值相同不触发写盘（幂等短路）。"""
        wd = str(tmp_path / "x")
        fresh_tm.set_team_workdir(wd)
        with patch.object(fresh_tm, "_save_team_data") as mock_save:
            fresh_tm.set_team_workdir(wd)  # 相同值
            mock_save.assert_not_called(), "相同值不应触发写盘"
        with patch.object(fresh_tm, "_save_team_data") as mock_save2:
            fresh_tm.set_team_workdir(str(tmp_path / "y"))  # 不同值
            mock_save2.assert_called_once(), "不同值应触发写盘"

    def test_get_team_workdir_empty_for_old_team(self, fresh_tm, tmp_path):
        """B2: 旧 team.json（无 workdir 字段）→ 返回 "" 不抛异常。"""
        old = {"name": "default", "created_at": 0, "members": {}, "project": "P0"}
        team_file = _team_file(fresh_tm, fresh_tm.DEFAULT_TEAM)
        team_file.write_text(__import__("json").dumps(old), encoding="utf-8")
        fresh_tm._team_cache[fresh_tm.DEFAULT_TEAM] = old
        assert fresh_tm.get_team_workdir() == "", "旧数据应返回空串"
        data = fresh_tm._read_json(team_file)
        assert "workdir" not in data, "读取不应凭空生成 workdir 键"

    def test_workdir_survives_stale_member_cleanup(self, fresh_tm, tmp_path):
        """B3: workdir 放顶层：成员清理不丢失（对齐 project/run_id 语义）。"""
        wd = str(tmp_path / "wt")
        fresh_tm.set_team_workdir(wd)
        fresh_tm.set_active_window_ids({"win_01", "win_02"})
        fresh_tm.join_team("win_01", "build")
        fresh_tm.join_team("win_02", "plan")
        fresh_tm.set_active_window_ids({"win_01"})
        fresh_tm._cleanup_stale_members(fresh_tm.DEFAULT_TEAM)
        assert fresh_tm.get_team_workdir() == wd, "成员清理不应丢失团队 workdir"


class TestTeamWorkdirBroadcast:
    """B4/B5：_broadcast_team_workdir 广播过滤与防循环"""

    @staticmethod
    def _make_win(agent_name="", run_id="", team_name="", window_id="", project="P0"):
        """object.__new__ 轻量窗口替身（绕过 __init__，仅设广播所需属性）。"""
        from app.main_widget import OpenAIChatToolWindow

        win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
        win._window_id = window_id
        win._team_agent_name = agent_name
        win._team_run_id = run_id
        win._team_name = team_name
        win._is_destroyed = False
        win._current_project = project
        win._apply_team_workdir = Mock()
        return win

    def test_broadcast_only_to_same_team_members(self, fresh_tm, tmp_path):
        """B4+B5: 广播仅发给同团队（同 run_id）成员；非成员/他团队不受影响。"""
        from app.main_widget import OpenAIChatToolWindow

        wd = str(tmp_path / "w")
        sender = self._make_win(agent_name="build", run_id="run_1", window_id="win_01")
        member1 = self._make_win(agent_name="plan", run_id="run_1", window_id="win_02")
        member2 = self._make_win(agent_name="review", run_id="run_1", window_id="win_03")
        # 他团队 run_2：is_team_member 为 True，但 run_id 不同
        other_team = self._make_win(agent_name="coder", run_id="run_2", window_id="win_04")
        # 非团队成员：_team_agent_name 为空
        nonmember = self._make_win(agent_name="", run_id="", window_id="win_05")

        for w in (sender, member1, member2, other_team):
            fresh_tm.join_team(w._window_id, w._team_agent_name)

        with patch.object(OpenAIChatToolWindow, "_instances", [sender, member1, member2, other_team, nonmember]):
            sender._broadcast_team_workdir(wd)

        # 团队级 workdir 已写入
        assert fresh_tm.get_team_workdir() == wd
        # 同团队成员收到广播
        member1._apply_team_workdir.assert_called_once_with(wd)
        member2._apply_team_workdir.assert_called_once_with(wd)
        # 发送方自身不重复应用（防循环）
        sender._apply_team_workdir.assert_not_called()
        # 他团队窗口不受影响
        other_team._apply_team_workdir.assert_not_called()
        # 非团队成员不受影响
        nonmember._apply_team_workdir.assert_not_called()

    def test_broadcast_skips_when_sender_not_in_team(self, fresh_tm, tmp_path):
        """B4: 发送方非团队 -> 不写团队级 workdir、不广播。"""
        from app.main_widget import OpenAIChatToolWindow

        wd = str(tmp_path / "wd")
        sender = self._make_win(agent_name="", run_id="", window_id="win_01")
        member1 = self._make_win(agent_name="plan", run_id="run_1", window_id="win_02")
        fresh_tm.join_team(member1._window_id, "plan")

        with patch.object(OpenAIChatToolWindow, "_instances", [sender, member1]):
            sender._broadcast_team_workdir(wd)

        assert fresh_tm.get_team_workdir() == "", "非团队发送方不应写团队级 workdir"
        member1._apply_team_workdir.assert_not_called()

    def test_broadcast_does_not_forward(self, fresh_tm, tmp_path):
        """B4: 广播仅发送方触发一次，接收方不转发（无递归调用）。"""
        from app.core.team_manager import TeamManager
        from app.main_widget import OpenAIChatToolWindow

        wd = str(tmp_path / "wd")
        sender = self._make_win(agent_name="build", run_id="run_1", window_id="win_01")
        member1 = self._make_win(agent_name="plan", run_id="run_1", window_id="win_02")
        fresh_tm.join_team(sender._window_id, "build")
        fresh_tm.join_team(member1._window_id, "plan")

        # 发送方真正的工作目录（走完整方法），接收方替身只记录调用
        sender._current_workdir = {"P0": str(tmp_path / "old")}
        with patch.object(OpenAIChatToolWindow, "_instances", [sender, member1]):
            # 直接调用真实方法（sender 有 _team_agent_name，会广播）
            with patch.object(TeamManager, "get_instance", return_value=fresh_tm):
                sender._broadcast_team_workdir(wd)

        member1._apply_team_workdir.assert_called_once_with(wd)
        # 若接收方转发，会再次触发 sender 的 _apply_team_workdir（此处不应发生）
        sender._apply_team_workdir.assert_not_called()


class TestTeamWorkdirApply:
    """B5/B6：_apply_team_workdir 接收方行为"""

    @staticmethod
    def _make_target(project="P0"):
        """构造可调用 _apply_team_workdir 的轻量窗口替身。"""
        from app.main_widget import OpenAIChatToolWindow

        win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
        win._is_destroyed = False
        win._window_id = "win_t"
        win._current_project = project
        win._current_workdir = {}
        win.backend = SimpleNamespace(tool_executor=Mock())
        win._memory_card_popup = None
        win._ensure_temp_workdir = Mock(return_value="")
        win._update_branch = Mock()
        return win

    def test_apply_same_value_short_circuits(self):
        """B5: 相同值短路——不重写 tool_executor，不刷新 UI。"""
        win = self._make_target()
        win._current_workdir["P0"] = "/x"
        win._apply_team_workdir("/x")

        win.backend.tool_executor.set_workdir.assert_not_called()
        win._update_branch.assert_not_called()

    def test_apply_different_value_syncs_state(self):
        """B5(反面): 不同值同步 _current_workdir/tool_executor/memory_card/分支。"""
        win = self._make_target()
        win._apply_team_workdir("/x")

        assert win._current_workdir["P0"] == "/x", "实例缓存应更新"
        win.backend.tool_executor.set_workdir.assert_called_once_with("/x")
        win._update_branch.assert_called_once()

    def test_apply_empty_workdir_falls_back_to_temp(self):
        """B6: 空 workdir -> 回退临时工作区；tool_executor 收到兜底路径。"""
        win = self._make_target()
        win._ensure_temp_workdir.return_value = "/tmp/ws"
        win._apply_team_workdir("")

        assert "P0" not in win._current_workdir, "空值应清除实例缓存"
        win._ensure_temp_workdir.assert_called_once()
        win.backend.tool_executor.set_workdir.assert_called_once_with("/tmp/ws")
        win._update_branch.assert_called_once()

    def test_apply_no_local_broadcast(self):
        """B5: 接收方方法体内无广播调用（静态校验见 inject 类，此处行为兜底）。"""
        win = self._make_target()
        win._broadcast_team_workdir = Mock()
        win._apply_team_workdir("/y")
        win._broadcast_team_workdir.assert_not_called(), "接收方不应再次广播"


class TestTeamWorkdirInjectPoints:
    """B4/B5/B7 注入点静态校验（读 main_widget.py 源码）"""

    def test_broadcast_has_no_forward_call(self):
        """B4: _broadcast_team_workdir 方法体内无递归/转发调用（AST 精确解析）。"""
        calls = _method_calls("_broadcast_team_workdir")
        assert "_broadcast_team_workdir" not in calls, "广播方法不应递归调用自身"
        assert "_apply_team_workdir" in calls, "广播应调用接收方 _apply_team_workdir"

    def test_apply_has_no_broadcast(self):
        """B5: _apply_team_workdir 不含广播转发（防循环）。"""
        calls = _method_calls("_apply_team_workdir")
        assert "_broadcast_team_workdir" not in calls, "接收方不应转发广播（防循环）"

    def test_workdir_change_triggers_broadcast(self):
        """B7: 工作目录/工作树变更入口应广播团队 workdir。"""
        text = _MAIN_WIDGET.read_text(encoding="utf-8")
        for method in ("_on_working_dir_changed", "_switch_to_worktree", "_restore_main_repo"):
            body = _method_body(text, method)
            assert "_broadcast_team_workdir" in body, f"{method} 未广播团队 workdir"

    def test_team_join_applies_team_workdir(self):
        """B7: 团队成员加入/创建路径应读取并应用团队级 workdir。"""
        text = _MAIN_WIDGET.read_text(encoding="utf-8")
        for method in ("_spawn_team_member_window", "_join_new_window_for_template", "_do_join_team"):
            body = _method_body(text, method)
            assert "get_team_workdir" in body, f"{method} 未读取团队 workdir"
            assert "_apply_team_workdir" in body, f"{method} 未应用团队 workdir"
