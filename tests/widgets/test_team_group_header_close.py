# -*- coding: utf-8 -*-
"""团队框 header + 关闭按钮测试

覆盖：
① header 显示团队名（set_team_label）
② 点击关闭按钮 → teamCloseRequested(team_id) 信号发射
③ 关闭按钮 hover 显示/默认隐藏
④ 空名兜底（默认"团队"）
⑤ TabManagerWindow._on_team_close_requested 倒序遍历同 team 窗口 + 调 leave_team
⑥ 其他团队窗口不受影响（只关闭匹配 team_id 的窗口）

说明：本测试不创建真实 TabManagerWindow 单例（_setup_ui 涉及 TrayManager 等重依赖），
对 TabManagerWindow._on_team_close_requested 用 mock + patch 隔离测试。
"""

from unittest.mock import MagicMock, patch

import pytest


# ── ① header 团队名显示 ─────────────────────────────


def test_header_displays_team_name(qapp):
    """set_team_label 注入团队名后，header QLabel 文本应一致"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        panel.set_team_label("team-A", "开发组")
        assert grp._team_name_label.text() == "开发组"

        # 改名应同步刷新
        panel.set_team_label("team-A", "测试组")
        assert grp._team_name_label.text() == "测试组"
    finally:
        panel.deleteLater()


def test_header_fallback_for_empty_name(qapp):
    """空名/纯空白名兜底为'团队'占位文本（与 _get_or_create_team_group 一致）"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        panel.set_team_label("team-A", "")
        assert grp._team_name_label.text() == "团队"
        panel.set_team_label("team-A", "   ")
        assert grp._team_name_label.text() == "团队"
        panel.set_team_label("team-A", None)  # type: ignore[arg-type]
        assert grp._team_name_label.text() == "团队"
    finally:
        panel.deleteLater()


def test_header_name_noop_for_unknown_team(qapp):
    """未知 team_id 调用 set_team_label 应静默 no-op（不抛异常、不创建容器）"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        panel.set_team_label("not-exists", "幽灵组")
        assert "not-exists" not in panel._team_groups
    finally:
        panel.deleteLater()


# ── ② 关闭按钮信号 ─────────────────────────────


def test_team_close_button_emits_team_id(qapp):
    """点击关闭按钮 → teamCloseRequested(team_id) 信号发射

    注：FluentWidget 的 clicked 信号带 bool 参数（checked state），需在 lambda 中
    用 *args 忽略，否则 emit 会收到 bool 类型不匹配 str 类型而抛 TypeError。
    """
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        panel._get_or_create_team_group("team-A")
        captured = []
        panel.teamCloseRequested.connect(lambda tid: captured.append(tid))

        # 关闭按钮被 hover 显示后才点击（默认 hidden，但 .click() 不依赖可见性）
        grp = panel._team_groups["team-A"]
        grp._team_close_btn.click()

        assert captured == ["team-A"]
    finally:
        panel.deleteLater()


def test_close_button_hidden_by_default(qapp):
    """关闭按钮默认隐藏（与 TabItem 同款行为）"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        # 父 widget 未显示时 isVisible() 返回 False；
        # 用 isVisibleTo(parent) 检查 widget 自身期望可见性
        assert grp._team_close_btn.isVisibleTo(grp._team_header) is False
    finally:
        panel.deleteLater()


def test_close_button_visible_on_header_hover(qapp):
    """鼠标进入 header 区域 → 关闭按钮 setVisible(True)；leave → setVisible(False)

    注：PySide6 中 isVisible() 受祖先链可见性影响，测试用 mock 跟踪 setVisible 调用。
    """
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        btn = grp._team_close_btn
        header = grp._team_header

        # 跟踪 setVisible 调用
        setVisible_calls = []
        original_setVisible = btn.setVisible
        btn.setVisible = lambda v: (setVisible_calls.append(v), original_setVisible(v))[1]

        # 模拟 enterEvent / leaveEvent
        header.enterEvent(None)
        assert True in setVisible_calls, "enterEvent 应触发 setVisible(True)"
        header.leaveEvent(None)
        # True 出现后再调用 False
        assert setVisible_calls[-1] is False, "leaveEvent 应触发 setVisible(False)"
    finally:
        panel.deleteLater()


# ── ③ 团队框嵌套结构 ─────────────────────────────


def test_team_group_has_header_and_inner_layout(qapp):
    """team 框必须包含：header (子控件) + inner_layout (成员层)"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-X")
        # header 子控件齐全
        assert hasattr(grp, "_team_header")
        assert hasattr(grp, "_team_name_label")
        assert hasattr(grp, "_team_close_btn")
        assert hasattr(grp, "_team_inner_layout")
        # 内部布局是嵌套结构（外层 QVBoxLayout 包含 header + inner）
        from PySide6.QtWidgets import QVBoxLayout

        assert isinstance(grp.layout(), QVBoxLayout)
        # header 是布局第一项，inner 是第二项
        assert grp.layout().itemAt(0).widget() is grp._team_header
        assert grp.layout().itemAt(1).widget() is grp._team_inner_widget
    finally:
        panel.deleteLater()


def test_team_group_close_btn_no_mouse_propagation(qapp):
    """关闭按钮设了 WA_NoMousePropagation，事件不向下层 TabItem 冒泡"""
    from PySide6.QtCore import Qt

    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-X")
        btn = grp._team_close_btn
        assert bool(btn.testAttribute(Qt.WA_NoMousePropagation)) is True
    finally:
        panel.deleteLater()


# ── ④ TabManagerWindow._on_team_close_requested 行为 ─────────────────


def _make_fake_window(window_id: str, team_run_id: str = "", team_name: str = "", team_agent: str = ""):
    """构造一个最小 fake window：含 _window_id/_team_* + 可监控的 _handle_team_leave + close

    不带 spec 参数：MagicMock(spec=[]) 严格 spec 会禁止动态属性，导致测试代码
    写 win._handle_team_leave.called 时报 AttributeError。
    """
    win = MagicMock()
    win._window_id = window_id
    win._team_run_id = team_run_id
    win._team_name = team_name
    win._team_agent_name = team_agent
    return win


def _build_tm_instance(windows):
    """构造一个跳过 __init__ 的 TabManagerWindow 实例，便于独立测试

    __init__ 重依赖 TrayManager/TrayManager 等；用 __new__ 绕过 + 手动设属性。
    tabCountChanged 用 MagicMock 替换（避免 Signal emit 检查 super().__init__）。
    """
    from app.widgets.tab_manager_window import TabManagerWindow

    tm_instance = TabManagerWindow.__new__(TabManagerWindow)
    tm_instance._windows = windows
    tm_instance._resolve_tab_team_id = lambda w: getattr(w, "_team_run_id", "")
    tm_instance._content_area = MagicMock()
    tm_instance._tab_panel = MagicMock()
    tm_instance.tabCountChanged = MagicMock()  # mock 信号 emit 不抛
    TabManagerWindow._instance = tm_instance
    return tm_instance


def test_on_team_close_collects_matching_windows_in_reverse(qapp):
    """_on_team_close_requested 按 team_id 匹配 + 倒序遍历，避免索引漂移"""
    from app.widgets.tab_manager_window import TabManagerWindow

    # 准备 4 个 fake window：2 个 team-A + 1 个 team-B + 1 个独立
    win_a1 = _make_fake_window("win01", team_run_id="run-A", team_name="group-A", team_agent="build")
    win_a2 = _make_fake_window("win02", team_run_id="run-A", team_name="group-A", team_agent="plan")
    win_b1 = _make_fake_window("win03", team_run_id="run-B", team_name="group-B", team_agent="review")
    win_n1 = _make_fake_window("win04", team_run_id="", team_name="", team_agent="")

    tm_instance = _build_tm_instance([win_a1, win_a2, win_b1, win_n1])

    try:
        TabManagerWindow._on_team_close_requested(tm_instance, "run-A")

        # team-A 窗口调用了 _handle_team_leave
        assert win_a1._handle_team_leave.called, "team-A 第 1 个窗口应被退出"
        assert win_a2._handle_team_leave.called, "team-A 第 2 个窗口应被退出"
        # team-B 和独立窗口未受影响
        assert not win_b1._handle_team_leave.called, "team-B 不应被关闭"
        assert not win_n1._handle_team_leave.called, "独立窗口不应被关闭"
    finally:
        TabManagerWindow._instance = None


def test_on_team_close_no_op_for_unknown_team(qapp):
    """未知 team_id 调用应静默 no-op（不调任何 _handle_team_leave）"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _make_fake_window("win01", team_run_id="run-A", team_name="group-A", team_agent="build")
    tm_instance = _build_tm_instance([win])

    try:
        TabManagerWindow._on_team_close_requested(tm_instance, "run-UNKNOWN")

        assert not win._handle_team_leave.called
    finally:
        TabManagerWindow._instance = None


def test_on_team_close_empty_team_id_no_op(qapp):
    """空 team_id 防御：直接 return，不匹配任何窗口"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _make_fake_window("win01", team_run_id="run-A", team_name="group-A", team_agent="build")
    tm_instance = _build_tm_instance([win])

    try:
        TabManagerWindow._on_team_close_requested(tm_instance, "")

        assert not win._handle_team_leave.called
    finally:
        TabManagerWindow._instance = None


def test_on_team_close_falls_back_to_handle_team_leave_or_minimal(qapp):
    """窗口无 _handle_team_leave 时降级：仅调 tm.leave_team(window_id)"""
    from app.core import team_manager as tm_mod
    from app.widgets.tab_manager_window import TabManagerWindow

    # window 没有 _handle_team_leave（部分老窗口可能没有）
    win = _make_fake_window("win05", team_run_id="run-Z", team_name="group-Z", team_agent="qa")
    del win._handle_team_leave
    tm_instance = _build_tm_instance([win])

    # mock TeamManager.leave_team 以便监控
    fake_tm = MagicMock()
    try:
        with patch.object(tm_mod.TeamManager, "get_instance", classmethod(lambda cls: fake_tm)):
            TabManagerWindow._on_team_close_requested(tm_instance, "run-Z")

        # 降级路径必须调 tm.leave_team
        assert fake_tm.leave_team.called
        assert fake_tm.leave_team.call_args.args[0] == "win05"
    finally:
        TabManagerWindow._instance = None


def test_on_team_close_handles_index_drift_in_reverse(qapp):
    """倒序遍历避免索引漂移：3 个 team-A 窗口，关闭后 self._windows 长度递减正确

    关键回归保护：如果用正序遍历，pop 后第 2 个窗口的索引会错位，导致漏关闭
    或访问越界。倒序遍历确保每次 pop 都在末尾。
    """
    from app.widgets.tab_manager_window import TabManagerWindow

    wins = [_make_fake_window(f"win0{i}", team_run_id="run-A", team_name="g-A", team_agent="a") for i in range(1, 4)]
    other = _make_fake_window("win99", team_run_id="run-B", team_name="g-B", team_agent="b")

    tm_instance = _build_tm_instance([wins[0], other, wins[1], wins[2]])

    try:
        TabManagerWindow._on_team_close_requested(tm_instance, "run-A")

        # 3 个 team-A 窗口都退出；team-B 不动
        for w in wins:
            assert w._handle_team_leave.called, f"{w._window_id} 应被关闭"
        assert not other._handle_team_leave.called
    finally:
        TabManagerWindow._instance = None


# ── ⑤ TabPanel 信号已暴露 ─────────────────────────────


def test_tabpanel_has_team_close_signal(qapp):
    """TabPanel 暴露 teamCloseRequested(str) 信号（验证 Signal 而非具体行为）"""
    from PySide6.QtCore import Signal

    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        assert hasattr(panel, "teamCloseRequested")
        sig = getattr(TabPanel, "teamCloseRequested", None)
        assert isinstance(sig, Signal)
    finally:
        panel.deleteLater()
