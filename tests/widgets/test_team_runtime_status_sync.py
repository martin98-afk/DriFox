# -*- coding: utf-8 -*-
"""子任务 #B2-2：main_widget._sync_team_member_runtime_status 映射逻辑单测

覆盖（对应 app/main_widget.py L6868-6890，#B2 实现）：
1. state ∈ {thinking, streaming, question} → set_member_runtime_status(wid, "busy")
2. state == idle → "idle"
3. state = error 等其他状态 → 不覆盖（set 不被调用，成员保持原值）
4. 非团队成员窗口 → 短路不调用（_set_ai_state 仍正常更新 AI 状态/发射信号）
5. 同步链路上 team.json 落盘生效（get_member_busy_status 查询反映忙/闲）
6. _set_ai_state 同状态重复调用不重复同步（幂等）

设计说明：
- **只写测试文件，绝不触碰 app/ 下任何文件**（build@win_1798 并行修改 main_widget.py）
- 被测方法 `from app.core.team_manager import ...` 是**方法内运行时导入**，
  因此 monkeypatch 模块属性即可生效（无需 import 级 mock）
- stub 用 SimpleNamespace + MethodType 绑定真实方法：绕开 PyQt 构造函数，
  避免 review 发现的「stub 不调 super().__init__ 时 getattr 窗口属性抛
  RuntimeError」坑（方法只访问 self._window_id / self._ai_state /
  self.ai_state_changed，全部由 stub 提供）
- 真实 TeamManager 隔离实例 + monkeypatch 单例 _instance：check_team_member
  与 set_member_runtime_status 走真实链路，同时验证持久化
"""

from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core import team_manager as tm_mod


@pytest.fixture
def tm(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager 实例，并让 get_instance 单例指向它。"""
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    tm = tm_mod.TeamManager()
    # 🛡️ 恢复 get_instance → 隔离实例（monkeypatch 自动恢复）。
    # 注意：test_team_template.py 有**直接赋值**污染 TeamManager.get_instance
    # （非 monkeypatch，永久生效），tests/core 先于 tests/widgets 收集，
    # 本文件运行前单例可能已被 _FakeTM 顶替——必须显式 patch 才能让
    # check_team_member 走真实 is_team_member 链路。
    monkeypatch.setattr(tm_mod.TeamManager, "get_instance", staticmethod(lambda: tm), raising=False)
    return tm


def _joined_tm(tm):
    """构造含 2 名成员的团队：win_01(leader) / win_02(build)。"""
    tm.join_team("win_01", "leader")
    tm.join_team("win_02", "build")


def _make_stub(window_id):
    """构造绑定真实 _set_ai_state / _sync_team_member_runtime_status 的 stub。

    不调用构造函数（无 PyQt C++ 对象），方法所需属性由 SimpleNamespace 提供。
    """
    from app.main_widget import OpenAIChatToolWindow

    stub = SimpleNamespace(
        _window_id=window_id,
        _ai_state="idle",
        ai_state_changed=MagicMock(),
    )
    stub._set_ai_state = MethodType(OpenAIChatToolWindow._set_ai_state, stub)
    stub._sync_team_member_runtime_status = MethodType(
        OpenAIChatToolWindow._sync_team_member_runtime_status, stub
    )
    return stub


def _spy_set_runtime_status(tm, spy_target=None):
    """把 tm.set_member_runtime_status 包一层 MagicMock（wraps 保留真实行为），
    用于断言「被调用/未被调用」与调用参数。"""
    original = tm.set_member_runtime_status
    spy = MagicMock(wraps=original)
    tm.set_member_runtime_status = spy
    return spy


class TestBusyMapping:
    """1：thinking/streaming/question → busy"""

    @pytest.mark.parametrize("state", ["thinking", "streaming", "question"])
    def test_busy_states_call_set_busy(self, tm, state):
        _joined_tm(tm)
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_02")

        stub._sync_team_member_runtime_status(state)

        spy.assert_called_once_with("win_02", "busy")
        assert tm.get_member_runtime_status("win_02") == "busy", "调用后 runtime 状态应真正生效"

    def test_set_ai_state_streaming_forwards_busy(self, tm):
        """_set_ai_state('streaming') 整体链路：AI 状态更新 + 发射信号 + 同步 busy。"""
        _joined_tm(tm)
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_02")

        stub._set_ai_state("streaming")

        assert stub._ai_state == "streaming", "AI 状态应更新"
        stub.ai_state_changed.emit.assert_called_once_with("streaming")
        spy.assert_called_once_with("win_02", "busy")


class TestIdleMapping:
    """2：idle → idle"""

    def test_idle_calls_set_idle(self, tm):
        _joined_tm(tm)
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_02")

        stub._sync_team_member_runtime_status("idle")

        spy.assert_called_once_with("win_02", "idle")
        assert tm.get_member_runtime_status("win_02") == "idle"

    def test_busy_to_idle_transition(self, tm):
        """先 busy 后 idle：runtime 状态应从 busy 回落到 idle。"""
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "busy")
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_02")

        stub._sync_team_member_runtime_status("idle")

        spy.assert_called_once_with("win_02", "idle")
        assert tm.get_member_runtime_status("win_02") == "idle", "busy → idle 应回落"


class TestNoOverrideStates:
    """3：error 等其他状态 → 不覆盖（set 不被调用，成员保持原值）"""

    @pytest.mark.parametrize("state", ["error", "unknown", ""])
    def test_other_states_do_not_call_set(self, tm, state):
        _joined_tm(tm)
        tm.set_member_runtime_status("win_02", "busy")  # 成员当前 busy（重试期）
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_02")

        stub._sync_team_member_runtime_status(state)

        spy.assert_not_called(), f"state={state!r} 不应触发 set（保持原值）"
        assert tm.get_member_runtime_status("win_02") == "busy", "error 等状态应保持原 busy（重试期不闪烁）"

    def test_error_keeps_idle_member_idle(self, tm):
        """error 状态也不把 idle 成员误标 busy。"""
        _joined_tm(tm)
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_02")

        stub._sync_team_member_runtime_status("error")

        spy.assert_not_called()
        assert tm.get_member_runtime_status("win_02") == "idle", "error 不应把 idle 成员标 busy"


class TestNonMemberShortCircuit:
    """4：非团队成员窗口 → 短路不调用"""

    def test_non_member_sync_skipped(self, tm):
        _joined_tm(tm)
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_99")  # 非团队成员

        stub._sync_team_member_runtime_status("streaming")

        spy.assert_not_called(), "非成员窗口即使 streaming 也不应写 runtime 状态"
        assert tm.get_member_runtime_status("win_99") == "idle", "非成员读取应安全返回 idle"

    def test_non_member_set_ai_state_skips_sync_but_updates_ai(self, tm):
        """非成员窗口 _set_ai_state：AI 状态/信号正常，但同步链路短路。"""
        _joined_tm(tm)
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_99")

        stub._set_ai_state("thinking")

        assert stub._ai_state == "thinking", "AI 状态应照常更新（桌宠动画不受影响）"
        stub.ai_state_changed.emit.assert_called_once_with("thinking")
        spy.assert_not_called(), "非成员窗口同步链路应短路"


class TestPersistence:
    """5：同步链路上 team.json 落盘生效"""

    def test_team_json_persisted_through_sync(self, tm):
        """_set_ai_state → 同步后 team.json members[wid].runtime_status 落盘。"""
        import json

        _joined_tm(tm)
        stub = _make_stub("win_02")

        stub._set_ai_state("streaming")
        data = json.loads(tm._team_file("default").read_text(encoding="utf-8"))
        assert data["members"]["win_02"].get("runtime_status") == "busy", "busy 应落盘到 team.json"

        stub._set_ai_state("idle")
        data = json.loads(tm._team_file("default").read_text(encoding="utf-8"))
        assert data["members"]["win_02"].get("runtime_status") == "idle", "idle 应落盘到 team.json"

    def test_query_path_reflects_sync(self, tm):
        """同步后 get_member_busy_status 查询立即反映忙/闲（流式 → busy）。"""
        _joined_tm(tm)
        stub = _make_stub("win_02")

        stub._set_ai_state("streaming")
        assert tm.get_member_busy_status("win_02") == "busy", "流式同步后查询应 busy"

        stub._set_ai_state("idle")
        assert tm.get_member_busy_status("win_02") == "idle", "空闲同步后查询应 idle"


class TestSetAiStateIdempotent:
    """6：_set_ai_state 同状态重复调用不重复同步"""

    def test_same_state_no_resync(self, tm):
        _joined_tm(tm)
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_02")

        stub._set_ai_state("streaming")
        stub._set_ai_state("streaming")  # 同状态重复

        assert stub.ai_state_changed.emit.call_count == 1, "同状态不应重复发射信号"
        spy.assert_called_once_with("win_02", "busy"), "同状态不应重复同步"

    def test_streaming_error_streaming_flow(self, tm):
        """全链路：streaming → error（不覆盖）→ streaming 恢复。

        error 期间保持 busy（重试语义）；恢复 streaming 是 error→streaming
        的真实状态迁移，会再次同步 busy（set 内部同值早退，幂等无副作用）。
        """
        _joined_tm(tm)
        spy = _spy_set_runtime_status(tm)
        stub = _make_stub("win_02")

        stub._set_ai_state("streaming")
        stub._set_ai_state("error")  # 不覆盖
        assert tm.get_member_runtime_status("win_02") == "busy", "error 期间保持 busy（重试语义）"
        stub._set_ai_state("streaming")
        # 两次 streaming 因中间隔着 error 而各触发一次同步，参数均为 busy
        assert spy.call_count == 2, f"streaming(2) 应为两次同步，实际 {spy.call_count}"
        spy.assert_has_calls(
            [(( "win_02", "busy"),), (("win_02", "busy"),)],
        )
        assert tm.get_member_runtime_status("win_02") == "busy", "恢复后仍 busy"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))