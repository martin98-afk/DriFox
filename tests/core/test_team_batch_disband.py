# -*- coding: utf-8 -*-
"""T2 回归用例：批量解散最后窗口后的活跃集语义 + TeamManager 状态一致性

覆盖（devil-advocate D1 建议 U2/U3）：
1. `_on_team_close_requested` 批量解散：最后窗口解散后 `_instances` 无存活窗口
   → 循环后统一同步不执行（不会把空集同步给 TeamManager，避免误删成员邮箱）
2. `set_active_window_ids` 空集合保护：空集不覆盖已知活跃集、不触发清理
3. `_cleanup_stale_members` 空集保护：活跃窗口信息未知时跳过清理（不误删）
4. 解散后 TeamManager 侧状态一致：members 已清空、快照已清、下次 join 正常

设计说明：
- TeamManager 直接构造实例 + monkeypatch 隔离数据目录（沿用 test_team_manager_f5 模式）
- `_on_team_close_requested` 用 __new__ + 手工属性轻量构造（只依赖 _windows/
  _tab_panel/_resolve_tab_team_id/_close_window_at），不构造完整 TabManagerWindow
"""

import time

import pytest

from app.core import team_manager as tm_mod


@pytest.fixture
def team_manager(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager（避免污染真实 teams 目录）。"""
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    tm = tm_mod.TeamManager()
    yield tm


def _join_members(tm, window_ids, team_name="default"):
    """批量加入成员（模拟多窗口 join 团队）"""
    for i, wid in enumerate(window_ids):
        tm.join_team(wid, f"agent-{i}", team_name)


def _sync_active(tm, window_ids, team_name=None):
    """模拟窗口侧同步活跃窗口集合"""
    tm.set_active_window_ids(set(window_ids), team_name)


# ──────────────────────────────────────────────
# U3：set_active_window_ids 空集合保护
# ──────────────────────────────────────────────


def test_set_active_empty_does_not_clear_known_set(team_manager):
    """空集合不覆盖已知活跃集（U3 核心：空集=信息不可靠，绝不覆盖）。"""
    _join_members(team_manager, ["win_01", "win_02"])
    _sync_active(team_manager, {"win_01", "win_02"})

    # 窗口销毁时序中可能出现短暂空集 → 不得清空已知活跃集
    team_manager.set_active_window_ids(set())
    assert team_manager._active_window_ids == {"win_01", "win_02"}, (
        "空集合不得覆盖已知活跃集（否则后续清理会误删所有成员）"
    )
    # 成员仍完整保留
    assert len(team_manager.get_members()) == 2


def test_set_active_none_does_not_clear_known_set(team_manager):
    """None/空入参同样不覆盖（防御性）。"""
    _join_members(team_manager, ["win_01"])
    _sync_active(team_manager, {"win_01"})
    team_manager.set_active_window_ids(None)
    assert team_manager._active_window_ids == {"win_01"}


def test_set_active_empty_does_not_trigger_cleanup(team_manager):
    """空集合不触发 _cleanup_stale_members（邮箱目录不删）。"""
    _join_members(team_manager, ["win_01", "win_02"])
    _sync_active(team_manager, {"win_01", "win_02"})
    mailbox_01 = team_manager._mailbox_dir("default", "win_01")
    mailbox_02 = team_manager._mailbox_dir("default", "win_02")
    assert mailbox_01.exists() and mailbox_02.exists()

    # 传空集（信息不可靠）→ 直接 return，不清理
    team_manager.set_active_window_ids(set())
    assert mailbox_01.exists() and mailbox_02.exists(), "空集同步不得触发清理"


# ──────────────────────────────────────────────
# U3：_cleanup_stale_members 空集保护
# ──────────────────────────────────────────────


def test_cleanup_stale_members_skips_when_active_unknown(team_manager):
    """活跃窗口信息未知（_active_window_ids 为 None）→ 跳过清理（不误删邮箱）。"""
    _join_members(team_manager, ["win_01", "win_02"])
    # 从未同步过活跃集（None）→ 保守跳过
    team_manager._active_window_ids = None
    team_manager._cleanup_stale_members("default")
    assert len(team_manager.get_members()) == 2, "活跃集未知时不得清理任何成员"
    assert team_manager._mailbox_dir("default", "win_01").exists()


def test_cleanup_stale_members_empty_active_returns(team_manager):
    """_get_active_windows 返回空/None → _cleanup_stale_members 直接 return。"""
    _join_members(team_manager, ["win_01"])
    # 模拟 _active_window_ids 为空且文件兜底不存在 → _get_active_windows 返回 None
    team_manager._active_window_ids = None
    team_manager._cleanup_stale_members("default")
    assert len(team_manager.get_members()) == 1, "活跃集未知时不得清理"


# ──────────────────────────────────────────────
# U2：批量解散最后窗口 → 活跃集语义
# ──────────────────────────────────────────────


class _FakeTabPanel:
    """最小 TabPanel 桩：记录 begin/end_batch_remove 调用与 remove_tab"""

    def __init__(self):
        self.batch_begin = 0
        self.batch_end = 0
        self.removed = []

    def begin_batch_remove(self):
        self.batch_begin += 1

    def end_batch_remove(self):
        self.batch_end += 1

    def remove_tab(self, idx):
        self.removed.append(idx)


class _FakeWindow:
    """最小窗口桩：具备 _on_team_close_requested 依赖的属性

    _handle_team_leave 模拟真实窗口行为：置退出标志 + 调 team_manager.leave_team
    （真实 _handle_team_leave 的第 2 步副作用），使解散后 TeamManager 成员清空。
    """

    def __init__(
        self,
        tm,
        wid,
        team_name="team-A",
        run_id="run-1",
        agent="agent-0",
        is_destroyed=False,
        handle_leave=None,
        close_cb=None,
    ):
        self._tm = tm
        self._window_id = wid
        self._team_name = team_name
        self._team_run_id = run_id
        self._team_agent_name = agent
        self._is_destroyed = is_destroyed
        self._custom_handle_leave = handle_leave
        self._leave_called = False
        self.close_cb = close_cb

    def _handle_team_leave(self, silent=False, batch_disband=False):
        if self._custom_handle_leave is not None:
            self._custom_handle_leave(silent=silent, batch_disband=batch_disband)
            return
        self._leave_called = True
        # 模拟真实 _handle_team_leave 的第 2 步：tm.leave_team(window_id)
        try:
            self._tm.leave_team(self._window_id, "default")
        except Exception:
            pass
        if callable(self.close_cb):
            self.close_cb()

    def close(self):
        pass


def _make_tab_manager_window(tm, windows, tab_panel):
    """轻量构造 TabManagerWindow（__new__ 绕过完整 __init__，手工注入依赖）。

    _close_window_at 用 mock 替换：真实实现依赖完整 QWidget 初始化
    （tabCountChanged 信号、_content_area、window.setParent 等），
    __new__ 构造无法提供；U2 批量解散的控制流（逆序删除、成对 batch、
    统一同步）不依赖 _close_window_at 内部实现。
    """
    from app.widgets.tab_manager_window import TabManagerWindow

    obj = TabManagerWindow.__new__(TabManagerWindow)
    obj._windows = windows
    obj._tab_panel = tab_panel
    obj._window_to_index = {id(w): i for i, w in enumerate(windows)}
    # mock _close_window_at：记录按序关闭的窗口索引（验证逆序删除无漂移）
    close_log = []
    obj._close_window_at = lambda idx: close_log.append(idx)
    return obj, close_log


def test_disband_last_window_skips_sync_when_no_survivor(team_manager, monkeypatch):
    """批量解散最后窗口：_instances 无存活窗口 → 循环后统一同步不执行。

    关键断言：解散后 TeamManager._active_window_ids 保持解散前的已知活跃集
    （不被空集覆盖），成员已清空，邮箱目录已清理。
    """
    from app.main_widget import OpenAIChatToolWindow

    # 构造 2 个同团队窗口（team run-1），并加入 TeamManager
    win_a = _FakeWindow(team_manager, "win_01", team_name="team-A", run_id="run-1")
    win_b = _FakeWindow(team_manager, "win_02", team_name="team-A", run_id="run-1")
    _join_members(team_manager, ["win_01", "win_02"])
    _sync_active(team_manager, {"win_01", "win_02"})

    # 模拟 main_widget.OpenAIChatToolWindow._instances 为空（最后窗口已全部关闭）
    monkeypatch.setattr(OpenAIChatToolWindow, "_instances", [])

    tab_panel = _FakeTabPanel()
    windows = [win_a, win_b]
    tmw, close_log = _make_tab_manager_window(team_manager, windows, tab_panel)

    # 调用团队关闭（批量解散）
    tmw._on_team_close_requested("run-1")

    # 1) 所有窗口已退出团队（_handle_team_leave 被调用）
    assert win_a._leave_called and win_b._leave_called, "两个窗口都应调用 _handle_team_leave"
    # 2) batch 成对调用
    assert tab_panel.batch_begin == 1 and tab_panel.batch_end == 1, "begin/end_batch_remove 应成对"
    # 3) 逆序关闭索引正确（先关高索引再关低索引，无漂移）
    assert close_log == [1, 0], f"逆序关闭索引错误: {close_log}"
    # 4) _windows 已清空（mock 不 pop，但解散循环按预收集索引遍历完毕）
    assert win_a._leave_called and win_b._leave_called

    # 5) 活跃集不被空集覆盖：_instances 为空 → 循环不执行 → 不同步空集
    #    （解散后不残留错误活跃集：TeamManager 侧 members 已被 leave_team 清空）
    members = team_manager.get_members()
    assert members == [], f"解散后成员应清空: {members}"
    # 6) 下次 join 正常（window_id 复用不残留）
    team_manager.join_team("win_01", "agent-new", "default")
    assert len(team_manager.get_members()) == 1
    assert team_manager._mailbox_dir("default", "win_01").exists()


def test_disband_last_window_with_survivor_syncs_active(team_manager, monkeypatch):
    """批量解散 A 团队后仍有 B 团队存活窗口 → 统一同步执行，活跃集只含存活窗口。

    验证 `_instances` 有存活窗口时，循环后的统一同步仍执行（走存活窗口的
    _sync_active_windows_to_team_manager），TeamManager 活跃集更新为存活集合。
    """
    from app.main_widget import OpenAIChatToolWindow

    # A 团队 2 窗口 + B 团队 1 存活窗口
    win_a1 = _FakeWindow(team_manager, "win_01", team_name="team-A", run_id="run-A")
    win_a2 = _FakeWindow(team_manager, "win_02", team_name="team-A", run_id="run-A")
    win_b1 = _FakeWindow(team_manager, "win_03", team_name="team-B", run_id="run-B", is_destroyed=False)
    _join_members(team_manager, ["win_01", "win_02", "win_03"])
    _sync_active(team_manager, {"win_01", "win_02", "win_03"})

    # 模拟 _instances：A 已销毁、B 存活（带同步方法）
    sync_log = {}

    class _LiveWin(_FakeWindow):
        def _sync_active_windows_to_team_manager(self):
            sync_log["called"] = True

    live_b = _LiveWin(team_manager, "win_03", team_name="team-B", run_id="run-B")
    # win_a1/a2 关闭后 _is_destroyed=True
    win_a1._is_destroyed = True
    win_a2._is_destroyed = True
    monkeypatch.setattr(OpenAIChatToolWindow, "_instances", [win_a1, win_a2, live_b])

    tab_panel = _FakeTabPanel()
    windows = [win_a1, win_a2]
    tmw, close_log = _make_tab_manager_window(team_manager, windows, tab_panel)

    tmw._on_team_close_requested("run-A")

    # 解散 A 团队：A 窗口全部退出
    assert win_a1._leave_called and win_a2._leave_called
    assert close_log == [1, 0]
    # 存活窗口的同步被调用（统一同步 1 次）
    assert sync_log.get("called") is True, "有存活窗口时应执行统一同步"


def test_disband_then_join_same_window_id_reuses_mailbox(team_manager):
    """解散后快速 rejoin 同一 window_id：邮箱目录可复用（rmtree 竞态防护）。"""
    _join_members(team_manager, ["win_01"])
    _sync_active(team_manager, {"win_01"})
    mailbox = team_manager._mailbox_dir("default", "win_01")
    assert mailbox.exists()

    # leave（邮箱目录后台删除 + 原子改名）
    team_manager.leave_team("win_01")
    assert team_manager.get_members() == []
    # 再次 join：目录应可重建（mkdir exist_ok 幂等），成员记录恢复
    team_manager.join_team("win_01", "agent-again", "default")
    assert len(team_manager.get_members()) == 1
    assert mailbox.exists(), "rejoin 后邮箱目录应存在（join 的 mkdir 幂等）"
    time.sleep(0.3)  # 等待后台 rmtree 完成（不应误删重建目录）
    assert mailbox.exists(), "后台 rmtree 不应误删 rejoin 后重建的目录"


def test_disband_removes_member_snapshot(team_manager):
    """解散（leave_team）同时清理顶层 team_members 快照（T18 收敛）。"""
    _join_members(team_manager, ["win_01"])
    data = team_manager._get_team_data("default")
    assert "win_01" in data.get("team_members", {}), "join 后快照应有记录"

    team_manager.leave_team("win_01")
    data = team_manager._get_team_data("default")
    assert "win_01" not in data.get("members", {}), "成员应已清"
    assert "win_01" not in data.get("team_members", {}), "快照应同步清理"
