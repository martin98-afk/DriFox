# -*- coding: utf-8 -*-
"""#5a 团队级统一项目（一人改项目全员同步）测试

覆盖：
数据层（TeamManager）：
- A1: set_team_project/get_team_project 顶层 project 字段读写；team.json 顶层出现
  "project"；值相同不触发写盘（幂等短路）
- A2: 旧 team.json（无 project 字段）兼容 → get_team_project 返回 "" 不抛异常
- A4: 防循环广播（广播仅发送方触发，接收方不转发）——静态校验 + 多窗口 mock
- A5: 值相等跳过（_apply_team_project 相同值短路，不重写 backend/tool_executor）
- A6: 不同团队隔离（team1 广播不影响 team2）
- A7: 非团队成员窗口不受影响
- A9: 接收方不触发 _create_new_session

风格对齐 tests/core/test_team_run_id.py：
- fresh_tm fixture（tmp_path + monkeypatch 隔离 TeamManager）
- 窗口行为用例用 object.__new__ 轻量实例 + mock _instances，避免完整 UI 初始化
- 注入点静态校验读 main_widget.py 源码文本
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core import team_manager as tm_mod

# 模块导入时缓存真实 get_instance（对齐 test_team_run_id.py 的隔离防护）
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
    """提取 main_widget 中指定方法的源码文本（对齐 test_team_run_id.py 风格）。"""
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


class TestTeamProjectDataLayer:
    """A1/A2：TeamManager 顶层 project 字段读写与旧数据兼容"""

    def test_set_and_get_team_project(self, fresh_tm, tmp_path):
        """A1: set_team_project 写入顶层、get_team_project 读回。"""
        assert fresh_tm.get_team_project() == "", "初始应为空串"
        fresh_tm.set_team_project("项目A")
        assert fresh_tm.get_team_project() == "项目A"

        # team.json 顶层出现 "project"（与 run_id 平级，非成员级）
        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert data.get("project") == "项目A", "project 应持久化到 team.json 顶层"
        assert "project" in data
        members = data.get("members", {})
        assert not any("project" in m for m in members.values()), "project 不应放在成员级"

    def test_set_team_project_update(self, fresh_tm):
        """A1: 更新项目值正常覆盖。"""
        fresh_tm.set_team_project("项目A")
        fresh_tm.set_team_project("项目B")
        assert fresh_tm.get_team_project() == "项目B"

    def test_set_team_project_idempotent_skips_write(self, fresh_tm):
        """A1: 值相同不触发写盘（幂等短路）。"""
        fresh_tm.set_team_project("项目A")
        with patch.object(fresh_tm, "_save_team_data") as mock_save:
            fresh_tm.set_team_project("项目A")  # 相同值
            mock_save.assert_not_called(), "相同值不应触发写盘"
        with patch.object(fresh_tm, "_save_team_data") as mock_save2:
            fresh_tm.set_team_project("项目B")  # 不同值
            mock_save2.assert_called_once(), "不同值应触发写盘"

    def test_get_team_project_empty_for_old_team(self, fresh_tm, tmp_path):
        """A2: 旧 team.json（无 project 字段）→ 返回 "" 不抛异常。"""
        # 构造无 project 字段的旧 team.json
        old = {"name": "default", "created_at": 0, "members": {}}
        team_file = _team_file(fresh_tm, fresh_tm.DEFAULT_TEAM)
        team_file.write_text(__import__("json").dumps(old), encoding="utf-8")
        fresh_tm._team_cache[fresh_tm.DEFAULT_TEAM] = old
        assert fresh_tm.get_team_project() == "", "旧数据应返回空串"
        data = fresh_tm._read_json(team_file)
        assert "project" not in data, "读取不应凭空生成 project 键"

    def test_project_survives_stale_member_cleanup(self, fresh_tm):
        """project 放顶层：成员清理不丢失（对齐 run_id 的模板级语义）。"""
        fresh_tm.set_team_project("项目A")
        fresh_tm.set_active_window_ids({"win_01", "win_02"})
        fresh_tm.join_team("win_01", "build")
        fresh_tm.join_team("win_02", "plan")
        fresh_tm.set_active_window_ids({"win_01"})
        fresh_tm._cleanup_stale_members(fresh_tm.DEFAULT_TEAM)
        assert fresh_tm.get_team_project() == "项目A", "成员清理不应丢失团队 project"


class TestTeamProjectBroadcast:
    """A4/A6/A7：_broadcast_team_project 广播过滤与防循环"""

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
        win._apply_team_project = Mock()
        return win

    def test_broadcast_only_to_same_team_members(self, fresh_tm):
        """A4+A6+A7: 广播仅发给同团队（同 run_id）成员；非成员/他团队不受影响。"""
        from app.main_widget import OpenAIChatToolWindow

        # 团队 run_1：sender + member1 + member2（均注册为 default 团队成员）
        sender = self._make_win(agent_name="build", run_id="run_1", window_id="win_01")
        member1 = self._make_win(agent_name="plan", run_id="run_1", window_id="win_02")
        member2 = self._make_win(agent_name="review", run_id="run_1", window_id="win_03")
        # 他团队 run_2（A6）：is_team_member 为 True，但 run_id 不同
        other_team = self._make_win(agent_name="coder", run_id="run_2", window_id="win_04")
        # 非团队成员（A7）：_team_agent_name 为空
        nonmember = self._make_win(agent_name="", run_id="", window_id="win_05")

        for w in (sender, member1, member2, other_team):
            fresh_tm.join_team(w._window_id, w._team_agent_name)

        with patch.object(OpenAIChatToolWindow, "_instances", [sender, member1, member2, other_team, nonmember]):
            sender._broadcast_team_project("P1")

        # 团队级 project 已写入
        assert fresh_tm.get_team_project() == "P1"
        # 同团队成员收到广播
        member1._apply_team_project.assert_called_once_with("P1")
        member2._apply_team_project.assert_called_once_with("P1")
        # 发送方自身不重复应用（防循环：非发送方不转发，发送方不 self 应用）
        sender._apply_team_project.assert_not_called()
        # 他团队窗口不受影响（A6）
        other_team._apply_team_project.assert_not_called()
        # 非团队成员不受影响（A7）
        nonmember._apply_team_project.assert_not_called()

    def test_broadcast_does_not_forward(self, fresh_tm):
        """A4: 广播仅发送方触发一次，接收方不转发（无递归调用）。"""
        from app.main_widget import OpenAIChatToolWindow

        sender = self._make_win(agent_name="build", run_id="run_1", window_id="win_01")
        member1 = self._make_win(agent_name="plan", run_id="run_1", window_id="win_02")
        fresh_tm.join_team(sender._window_id, "build")
        fresh_tm.join_team(member1._window_id, "plan")

        with patch.object(OpenAIChatToolWindow, "_instances", [sender, member1]):
            sender._broadcast_team_project("P1")

        # 接收方 _apply_team_project 不调用任何转发/广播方法
        member1._apply_team_project.assert_called_once_with("P1")
        # 若接收方转发，会再次触发 sender 的 _apply_team_project（此处不应发生）
        sender._apply_team_project.assert_not_called()

    def test_broadcast_skips_receiver_with_different_project(self, fresh_tm):
        """P2-B: 接收方当前项目与发送方切换前项目不一致 → 不应用广播（Bug A）。

        sender 在项目 P0 广播切到 P1：同团队、同 run_id 但当前项目为 P9 的
        接收方（独立/其他项目窗口）应被跳过，避免 A 项目团队误广播到 B 项目窗口。
        """
        from app.main_widget import OpenAIChatToolWindow

        sender = self._make_win(agent_name="build", run_id="run_1", window_id="win_01", project="P0")
        # 接收方项目与发送方切换前一致（P0）→ 应收到广播
        member_same = self._make_win(agent_name="plan", run_id="run_1", window_id="win_02", project="P0")
        # 接收方项目不同（P9）→ 应被跳过
        member_diff = self._make_win(agent_name="review", run_id="run_1", window_id="win_03", project="P9")

        for w in (sender, member_same, member_diff):
            fresh_tm.join_team(w._window_id, w._team_agent_name)

        with patch.object(OpenAIChatToolWindow, "_instances", [sender, member_same, member_diff]):
            sender._broadcast_team_project("P1", prev_project="P0")

        # 团队级 project 已写入
        assert fresh_tm.get_team_project() == "P1"
        # 项目一致的接收方收到广播
        member_same._apply_team_project.assert_called_once_with("P1")
        # 项目不一致的接收方被跳过（Bug A 防护）
        member_diff._apply_team_project.assert_not_called()

    def test_broadcast_prev_project_defaults_to_sender_current(self, fresh_tm):
        """P2-B: 未显式传 prev_project 时，兜底用发送方当前项目做一致性校验。"""
        from app.main_widget import OpenAIChatToolWindow

        sender = self._make_win(agent_name="build", run_id="run_1", window_id="win_01", project="P0")
        member_same = self._make_win(agent_name="plan", run_id="run_1", window_id="win_02", project="P0")
        member_diff = self._make_win(agent_name="review", run_id="run_1", window_id="win_03", project="P5")

        for w in (sender, member_same, member_diff):
            fresh_tm.join_team(w._window_id, w._team_agent_name)

        with patch.object(OpenAIChatToolWindow, "_instances", [sender, member_same, member_diff]):
            # 不传 prev_project → 兜底 sender._current_project == "P0"
            sender._broadcast_team_project("P1")

        member_same._apply_team_project.assert_called_once_with("P1")
        member_diff._apply_team_project.assert_not_called()


class TestTeamProjectApply:
    """A5/A9：_apply_team_project 接收方行为"""

    @staticmethod
    def _make_target(project="P0"):
        """构造可调用 _apply_team_project 的轻量窗口替身。"""
        from app.main_widget import OpenAIChatToolWindow

        win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
        win._is_destroyed = False
        win._current_project = project
        win._current_workdir = {}
        win.backend = SimpleNamespace(
            _current_project=project,
            tool_executor=Mock(),
            memory_manager=None,
        )
        win._project_label = Mock()
        win._memory_card_popup = None
        win._history_popup_card = None
        win._current_history_project = project
        win.cfg = SimpleNamespace(enable_tab_manager=SimpleNamespace(value=False))
        win._refresh_project_branch_style = Mock()
        win._update_branch = Mock()
        win._invalidate_welcome_card = Mock()
        win._refresh_history_toggle_panel = Mock()
        win._sync_working_directory = Mock()
        win._create_new_session = Mock()
        return win

    def test_apply_same_value_short_circuits(self):
        """A5: 相同值短路——不重写 backend/tool_executor，不刷新任何 UI。"""
        win = self._make_target(project="P0")
        win._apply_team_project("P0")  # 与当前值相同

        assert win.backend._current_project == "P0", "backend 不应被重写"
        win.backend.tool_executor.set_current_project.assert_not_called()
        win._project_label.setText.assert_not_called()
        win._refresh_project_branch_style.assert_not_called()
        win._create_new_session.assert_not_called()

    def test_apply_different_value_syncs_state(self):
        """A5(反面): 不同值正常同步 _current_project/backend/tool_executor/UI。"""
        win = self._make_target(project="P0")
        win._apply_team_project("P1")

        assert win._current_project == "P1"
        assert win.backend._current_project == "P1"
        win.backend.tool_executor.set_current_project.assert_called_once_with("P1")
        win._project_label.setText.assert_called_once_with("P1")
        win._refresh_project_branch_style.assert_called_once()
        win._update_branch.assert_called_once()

    def test_apply_does_not_create_new_session(self):
        """A9: 接收方不触发 _create_new_session（防连环新建会话）。"""
        win = self._make_target(project="P0")
        win._apply_team_project("P1")
        win._create_new_session.assert_not_called(), "接收方应用项目不得新建会话"

    def test_apply_syncs_workdir_without_memory_card(self):
        """Bug 回归：记忆卡片未构建（_memory_card_popup 为 None）时仍须同步 workdir。

        tool_executor.get_workdir() 是 PreUserMessage hook（githook）project_root
        的数据源；若 _sync_working_directory 挂在卡片惰性构建状态上，接收方窗口
        切换项目后 hook 仍注入旧项目根目录 + Git 状态（残留）。
        """
        win = self._make_target(project="P0")
        assert win._memory_card_popup is None, "前置条件：记忆卡片尚未惰性构建"
        win._apply_team_project("P1")
        win._sync_working_directory.assert_called_once(), "卡片未构建时也必须同步 workdir"


class TestTeamProjectInjectPoints:
    """A4/A9 注入点静态校验（读 main_widget.py 源码，对齐 test_team_run_id 风格）"""

    def test_broadcast_has_no_forward_call(self):
        """A4: _broadcast_team_project 方法体内无递归/转发调用（AST 精确解析）。"""
        calls = _method_calls("_broadcast_team_project")
        assert "_broadcast_team_project" not in calls, "广播方法不应递归调用自身"
        assert "_apply_team_project" in calls, "广播应调用接收方 _apply_team_project"

    def test_apply_has_no_broadcast_or_new_session(self):
        """A9: _apply_team_project 不含广播转发，也不调用 _create_new_session。"""
        calls = _method_calls("_apply_team_project")
        assert "_broadcast_team_project" not in calls, "接收方不应转发广播（防循环）"
        assert "_create_new_session" not in calls, "接收方不得触发新建会话"

    def test_team_load_applies_team_project(self):
        """团队加载路径应读取并应用团队级 project。

        T5 重构：创建链路抽到 _spawn_team_member_window（_handle_team_load 委托
        _spawn_team_members），团队 project 的读取/应用语义随创建链路迁移。
        """
        text = _MAIN_WIDGET.read_text(encoding="utf-8")
        for method in ("_spawn_team_member_window", "_join_new_window_for_template"):
            body = _method_body(text, method)
            assert "get_team_project" in body, f"{method} 未读取团队 project"
            assert "_apply_team_project" in body, f"{method} 未应用团队 project"
