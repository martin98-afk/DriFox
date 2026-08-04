# -*- coding: utf-8 -*-
"""团队框"新建任务" + "快速新建成员"按钮测试（F14 返工版）

覆盖范围：
① TabPanel 暴露 teamAddMemberRequested(str) / teamNewTaskRequested(str) 信号
② header 含 _team_add_btn / _team_new_task_btn 访问器；点击各自发射信号
③ 按钮默认隐藏；hover 联动（new_task/add/close 三按钮）；折叠态隐藏
④ TabManagerWindow._on_team_add_member_requested / _on_team_new_task_requested 委托
⑤ main_widget._spawn_team_member_window：run_id 复用（不 start_team_run）+ 同步 join + 允许重复角色
⑥ main_widget._spawn_team_members：批量计数 / 归零排列
⑦ main_widget._handle_team_add_member（快速新建成员）：可重复角色（不置灰不去重）、无批量补齐
⑧ main_widget._handle_team_new_task（新建任务）：全员 _create_new_session + 新 run_id + 窗口更新

设计说明：
- qapp 由 pytest-qt 提供（session 级 QApplication）
- 重依赖（backend/QWebEngine/TrayManager）一律用 __new__ 绕过 + MagicMock 隔离
- 菜单交互通过 patch PyQt5.QtWidgets.QMenu 模拟用户选择
"""

from unittest.mock import MagicMock, patch

import pytest


# ══════════════════════════════════════════════════════════
# ① TabPanel 信号
# ══════════════════════════════════════════════════════════


def test_tabpanel_has_team_add_member_signal(qapp):
    """TabPanel 暴露 teamAddMemberRequested(str) 信号"""
    from PyQt5.QtCore import pyqtSignal

    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        assert hasattr(panel, "teamAddMemberRequested")
        sig = getattr(TabPanel, "teamAddMemberRequested", None)
        assert isinstance(sig, pyqtSignal)
    finally:
        panel.deleteLater()


def test_tabpanel_has_team_new_task_signal(qapp):
    """TabPanel 暴露 teamNewTaskRequested(str) 信号（F14 新增）"""
    from PyQt5.QtCore import pyqtSignal

    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        assert hasattr(panel, "teamNewTaskRequested")
        sig = getattr(TabPanel, "teamNewTaskRequested", None)
        assert isinstance(sig, pyqtSignal)
    finally:
        panel.deleteLater()


# ══════════════════════════════════════════════════════════
# ② header 按钮：访问器 + 信号发射
# ══════════════════════════════════════════════════════════


def test_team_group_has_add_btn(qapp):
    """team 框 header 必须包含 _team_add_btn 与 _team_new_task_btn 访问器"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-X")
        assert hasattr(grp, "_team_add_btn")
        assert hasattr(grp, "_team_new_task_btn")
        assert grp._team_add_btn.parent() is grp._team_header
        assert grp._team_new_task_btn.parent() is grp._team_header
        assert grp._team_close_btn.parent() is grp._team_header
    finally:
        panel.deleteLater()


def test_team_add_btn_emits_team_id(qapp):
    """点击快速新建成员按钮 → teamAddMemberRequested(team_id) 信号发射"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        panel._get_or_create_team_group("team-A")
        captured = []
        panel.teamAddMemberRequested.connect(lambda tid: captured.append(tid))

        grp = panel._team_groups["team-A"]
        grp._team_add_btn.click()

        assert captured == ["team-A"]
    finally:
        panel.deleteLater()


def test_team_new_task_btn_emits_team_id(qapp):
    """点击新建任务按钮 → teamNewTaskRequested(team_id) 信号发射"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        panel._get_or_create_team_group("team-A")
        captured = []
        panel.teamNewTaskRequested.connect(lambda tid: captured.append(tid))

        grp = panel._team_groups["team-A"]
        grp._team_new_task_btn.click()

        assert captured == ["team-A"]
    finally:
        panel.deleteLater()


# ══════════════════════════════════════════════════════════
# ③ 按钮可见性：默认隐藏 / hover 联动 / 折叠态隐藏
# ══════════════════════════════════════════════════════════


def test_buttons_hidden_by_default(qapp):
    """new_task/add/close 三按钮默认隐藏"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        assert grp._team_new_task_btn.isVisibleTo(grp._team_header) is False
        assert grp._team_add_btn.isVisibleTo(grp._team_header) is False
        assert grp._team_close_btn.isVisibleTo(grp._team_header) is False
    finally:
        panel.deleteLater()


def test_buttons_visible_on_header_hover(qapp):
    """鼠标进入 header → new_task/add/close 三按钮都显示；leave → 都隐藏"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        add_btn = grp._team_add_btn
        close_btn = grp._team_close_btn
        task_btn = grp._team_new_task_btn
        header = grp._team_header

        add_calls, close_calls, task_calls = [], [], []
        add_btn.setVisible = lambda v: (add_calls.append(v), MagicMock())[1]
        close_btn.setVisible = lambda v: (close_calls.append(v), MagicMock())[1]
        task_btn.setVisible = lambda v: (task_calls.append(v), MagicMock())[1]

        header.enterEvent(None)
        assert add_calls[-1] is True
        assert close_calls[-1] is True
        assert task_calls[-1] is True

        header.leaveEvent(None)
        assert add_calls[-1] is False
        assert close_calls[-1] is False
        assert task_calls[-1] is False
    finally:
        panel.deleteLater()


def test_buttons_hidden_in_compact_mode(qapp):
    """折叠态（_apply_team_compact(True)）→ 三按钮隐藏；展开恢复 hover 逻辑"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        add_btn = grp._team_add_btn
        close_btn = grp._team_close_btn
        task_btn = grp._team_new_task_btn

        panel._apply_team_compact(grp, True)
        assert task_btn.isVisibleTo(grp._team_header) is False
        assert add_btn.isVisibleTo(grp._team_header) is False
        assert close_btn.isVisibleTo(grp._team_header) is False
        # 折叠态 enter 不应弹按钮（紧凑态守卫）
        grp._team_header.enterEvent(None)
        assert task_btn.isVisibleTo(grp._team_header) is False

        panel._apply_team_compact(grp, False)
        assert task_btn.isVisibleTo(grp._team_header) is False
        assert add_btn.isVisibleTo(grp._team_header) is False
        assert close_btn.isVisibleTo(grp._team_header) is False
        # 展开态 hover 可显示
        grp._team_header.enterEvent(None)
        assert task_btn.isVisibleTo(grp._team_header) is True
        assert add_btn.isVisibleTo(grp._team_header) is True
    finally:
        panel.deleteLater()


# ══════════════════════════════════════════════════════════
# ④ TabManagerWindow 委托
# ══════════════════════════════════════════════════════════


def _make_fake_window(window_id: str, team_run_id: str = "", team_name: str = "", team_agent: str = ""):
    """构造最小 fake window（与 test_team_group_header_close 一致）"""
    win = MagicMock()
    win._window_id = window_id
    win._team_run_id = team_run_id
    win._team_name = team_name
    win._team_agent_name = team_agent
    return win


def _build_tm_instance(windows):
    """构造跳过 __init__ 的 TabManagerWindow 实例（重依赖隔离）"""
    from app.widgets.tab_manager_window import TabManagerWindow

    tm_instance = TabManagerWindow.__new__(TabManagerWindow)
    tm_instance._windows = windows
    tm_instance._resolve_tab_team_id = lambda w: getattr(w, "_team_run_id", "")
    tm_instance._content_area = MagicMock()
    tm_instance._tab_panel = MagicMock()
    tm_instance.tabCountChanged = MagicMock()
    TabManagerWindow._instance = tm_instance
    return tm_instance


def test_on_team_add_member_delegates_to_ref_window(qapp):
    """匹配 team_id 的窗口 → 委托其 _handle_team_add_member"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _make_fake_window("win01", team_run_id="run-A", team_name="group-A", team_agent="build")
    win._handle_team_add_member = MagicMock()
    tm_instance = _build_tm_instance([win])

    try:
        TabManagerWindow._on_team_add_member_requested(tm_instance, "run-A")
        win._handle_team_add_member.assert_called_once_with()
    finally:
        TabManagerWindow._instance = None


def test_on_team_new_task_delegates_to_ref_window(qapp):
    """匹配 team_id 的窗口 → 委托其 _handle_team_new_task（F14）"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _make_fake_window("win01", team_run_id="run-A", team_name="group-A", team_agent="build")
    win._handle_team_new_task = MagicMock()
    tm_instance = _build_tm_instance([win])

    try:
        TabManagerWindow._on_team_new_task_requested(tm_instance, "run-A")
        win._handle_team_new_task.assert_called_once_with()
    finally:
        TabManagerWindow._instance = None


def test_on_team_add_member_unknown_team_no_op(qapp):
    """未知 team_id → 静默 no-op（不调任何 handler）"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _make_fake_window("win01", team_run_id="run-A", team_name="group-A", team_agent="build")
    win._handle_team_add_member = MagicMock()
    tm_instance = _build_tm_instance([win])

    try:
        TabManagerWindow._on_team_add_member_requested(tm_instance, "run-UNKNOWN")
        assert not win._handle_team_add_member.called
    finally:
        TabManagerWindow._instance = None


def test_on_team_new_task_unknown_team_no_op(qapp):
    """未知 team_id → 静默 no-op（F14）"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _make_fake_window("win01", team_run_id="run-A", team_name="group-A", team_agent="build")
    win._handle_team_new_task = MagicMock()
    tm_instance = _build_tm_instance([win])

    try:
        TabManagerWindow._on_team_new_task_requested(tm_instance, "run-UNKNOWN")
        assert not win._handle_team_new_task.called
    finally:
        TabManagerWindow._instance = None


def test_on_team_add_member_missing_handler_no_op(qapp):
    """窗口无 _handle_team_add_member 时静默跳过（不抛异常）"""
    from app.widgets.tab_manager_window import TabManagerWindow

    win = _make_fake_window("win01", team_run_id="run-A", team_name="group-A", team_agent="build")
    tm_instance = _build_tm_instance([win])

    try:
        # 无 handler 直接调用不应抛异常
        TabManagerWindow._on_team_add_member_requested(tm_instance, "run-A")
    finally:
        TabManagerWindow._instance = None


# ══════════════════════════════════════════════════════════
# ⑤ main_widget._spawn_team_member_window：run_id 复用 + 同步 join + 可重复角色
# ══════════════════════════════════════════════════════════


def _make_main_widget_instance():
    """构造跳过 __init__ 的 OpenAIChatToolWindow 实例（重依赖隔离）"""
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._TEMPLATE_JOIN_DELAY_MS = 300
    inst._pending_arrange_count = 0
    inst.window = MagicMock(return_value=None)
    return inst


def test_spawn_team_member_window_reuses_run_id(qapp):
    """⚠️ 核心约束：新建成员必须复用现有 run_id，禁止 start_team_run(force=True)"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()

    fake_win = MagicMock()
    fake_win._window_id = "win-99"
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {"name": "default-team", "agents": [{"agent_name": "build"}]}
    fake_tm.get_team_run_id.return_value = "run-ABC"
    fake_tm.get_team_project.return_value = ""

    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._join_new_window_for_template = MagicMock()

    with patch.object(mw.QTimer, "singleShot") as m_single_shot:
        result = inst._spawn_team_member_window("build")

    assert result is fake_win
    # 复用现有 run_id（T3 坑 1 防线）
    assert fake_win._team_run_id == "run-ABC"
    assert fake_win._team_agent_name == "build"
    assert fake_win._team_name == "default-team"
    # 禁止 force 新 run_id
    assert not fake_tm.start_team_run.called, "新建成员不得 start_team_run(force=True)"
    # 同步 join（前置登记成员身份）
    fake_tm.join_team.assert_called_once_with(window_id="win-99", agent_name="build")
    # 300ms 延迟 join 已安排
    assert m_single_shot.called
    delay = m_single_shot.call_args.args[0]
    assert delay == 300


def test_spawn_team_member_window_allows_duplicate_role(qapp):
    """⚠️ F14：允许重复角色——同 agent 可建多个窗口（join_team 以 window_id 为 key）"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()

    fake_win = MagicMock()
    fake_win._window_id = "win-99"
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {"name": "t", "agents": []}
    fake_tm.get_team_run_id.return_value = "run-X"
    fake_tm.get_team_project.return_value = ""

    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)

    with patch.object(mw.QTimer, "singleShot"):
        # 同角色创建两次（模拟重复角色）→ 两次都成功且各自独立窗口
        inst._spawn_team_member_window("build")
        inst._spawn_team_member_window("build")

    assert inst._create_fresh_window.call_count == 2, "重复角色应创建两个独立窗口"
    assert fake_tm.join_team.call_count == 2, "每个窗口独立 join"


def test_spawn_team_member_window_creates_fresh_window(qapp):
    """创建链路必须走 _create_fresh_window（全新空白窗口，不复制上下文）"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    fake_win = MagicMock()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {"name": "t", "agents": []}
    fake_tm.get_team_run_id.return_value = "run-X"
    fake_tm.get_team_project.return_value = ""

    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)

    with patch.object(mw.QTimer, "singleShot"):
        inst._spawn_team_member_window("plan")

    inst._create_fresh_window.assert_called_once_with()


def test_spawn_team_member_window_none_on_failure(qapp):
    """_create_fresh_window 返回 None → 方法返回 None（不抛异常）"""
    inst = _make_main_widget_instance()
    inst._create_fresh_window = MagicMock(return_value=None)
    inst._get_team_manager = MagicMock(return_value=MagicMock())

    assert inst._spawn_team_member_window("build") is None


# ══════════════════════════════════════════════════════════
# ⑥ main_widget._spawn_team_members：批量计数 / 归零排列
# ══════════════════════════════════════════════════════════


def test_spawn_team_members_counts_and_arranges(qapp):
    """批量创建：计数 = 创建数；未归零不排列；全失败时立即排列"""
    inst = _make_main_widget_instance()

    # 场景 1：全部成功 → 计数 2，不触发立即排列
    w1, w2 = MagicMock(), MagicMock()
    inst._spawn_team_member_window = MagicMock(side_effect=[w1, w2])
    inst._do_team_window_arrange = MagicMock()
    n = inst._spawn_team_members(["build", "plan"])
    assert n == 2
    assert inst._pending_arrange_count == 2
    inst._do_team_window_arrange.assert_not_called()

    # 场景 2：全部失败 → 计数 0，立即排列（join 无人递减）
    inst._spawn_team_member_window = MagicMock(return_value=None)
    n = inst._spawn_team_members(["build", "plan"])
    assert n == 0
    assert inst._pending_arrange_count == 0
    inst._do_team_window_arrange.assert_called_once()


# ══════════════════════════════════════════════════════════
# ⑦ main_widget._handle_team_add_member：快速新建成员（可重复角色）
# ══════════════════════════════════════════════════════════


def test_handle_team_add_member_no_template_no_member_warns(qapp):
    """无模板且无成员 → InfoBar.warning 提示（F14：不再要求必须有模板）"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = None
    fake_tm.get_members.return_value = []
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock()

    with patch.object(InfoBar, "warning") as m_warn:
        inst._handle_team_add_member()

    m_warn.assert_called_once()
    assert not inst._spawn_team_members.called


def test_handle_team_add_member_menu_lists_template_and_members(qapp):
    """菜单列出模板角色 ∪ 当前成员角色（F14：不再有"角色已齐"拦截）"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {
        "name": "t",
        "agents": [{"agent_name": "build"}, {"agent_name": "plan"}],
    }
    # build/plan 都已加入 → 仍弹菜单（可重复选，F14 不再去重）
    fake_tm.get_members.return_value = [{"agent_name": "build"}, {"agent_name": "plan"}]
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock(return_value=1)

    fake_menu_cls, actions = _setup_add_member_menu_patch("build")
    with patch("PyQt5.QtWidgets.QMenu", fake_menu_cls), patch.object(InfoBar, "success"):
        inst._handle_team_add_member()

    # 已加入角色也可选（重复创建）：菜单含 build + plan，点击 build 创建
    inst._spawn_team_members.assert_called_once_with(["build"])
    assert "build" in actions and "plan" in actions


def test_handle_team_add_member_no_template_uses_member_roles(qapp):
    """无模板（手动 join 团队）→ 菜单列出当前成员角色（F14 兜底）"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = None
    fake_tm.get_members.return_value = [{"agent_name": "build"}, {"agent_name": "build"}]  # 重复成员
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock(return_value=1)

    fake_menu_cls, actions = _setup_add_member_menu_patch("build")
    with patch("PyQt5.QtWidgets.QMenu", fake_menu_cls), patch.object(InfoBar, "success"):
        inst._handle_team_add_member()

    inst._spawn_team_members.assert_called_once_with(["build"])
    assert "build" in actions


def _setup_add_member_menu_patch(choice):
    """构造 QMenu patch：addAction 动态创建 action，exec_ 返回指定项

    Args:
        choice: 角色名 / None（用户取消）

    Returns:
        (fake_menu_cls, actions_by_text)
    """
    fake_menu_cls = MagicMock()
    fake_menu = fake_menu_cls.return_value
    actions_by_text = {}

    def _make_action(text):
        action = MagicMock()
        action.text.return_value = text
        actions_by_text[text] = action
        return action

    fake_menu.addAction.side_effect = _make_action
    fake_menu.exec_ = MagicMock(side_effect=lambda pos: actions_by_text.get(choice))
    return fake_menu_cls, actions_by_text


def test_handle_team_add_member_single_create(qapp):
    """单角色创建：菜单选择角色 → _spawn_team_members([该角色])"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {
        "name": "t",
        "agents": [{"agent_name": "build"}, {"agent_name": "plan"}],
    }
    fake_tm.get_members.return_value = [{"agent_name": "build"}]
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock(return_value=1)

    fake_menu_cls, actions = _setup_add_member_menu_patch("plan")
    with patch("PyQt5.QtWidgets.QMenu", fake_menu_cls), patch.object(InfoBar, "success") as m_success:
        inst._handle_team_add_member()

    inst._spawn_team_members.assert_called_once_with(["plan"])
    m_success.assert_called_once()


def test_handle_team_add_member_no_batch_fill_action(qapp):
    """F14：菜单不再包含"批量补齐全部角色"入口（原补齐逻辑移除）"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {
        "name": "t",
        "agents": [{"agent_name": "build"}, {"agent_name": "plan"}, {"agent_name": "review"}],
    }
    fake_tm.get_members.return_value = [{"agent_name": "build"}]
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock(return_value=1)

    fake_menu_cls, actions = _setup_add_member_menu_patch("build")
    with patch("PyQt5.QtWidgets.QMenu", fake_menu_cls), patch.object(InfoBar, "success"):
        inst._handle_team_add_member()

    # 只 addAction 角色（3 个），无批量补齐项
    assert "批量补齐全部角色" not in actions, "F14 应移除批量补齐入口"


# ══════════════════════════════════════════════════════════
# ⑧ main_widget._handle_team_new_task：新建任务（全员新会话 + 新 run_id）
# ══════════════════════════════════════════════════════════


def _make_member_window(window_id: str, run_id: str):
    """构造成员窗口 fake（含 _create_new_session / _team_run_id）"""
    win = MagicMock()
    win._window_id = window_id
    win._team_run_id = run_id
    win._is_destroyed = False
    return win


def test_handle_team_new_task_rotates_run_id(qapp):
    """新建任务：全员 _create_new_session + 生成新 run_id + 窗口 _team_run_id 更新"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_members.return_value = [
        {"window_id": "win01", "agent_name": "build"},
        {"window_id": "win02", "agent_name": "plan"},
    ]
    fake_tm.start_team_run.return_value = "new-run-123"
    inst._get_team_manager = MagicMock(return_value=fake_tm)

    win1 = _make_member_window("win01", "old-run-1")
    win2 = _make_member_window("win02", "old-run-1")

    # 注册到 _instances 供收集（只放完整成员 fake；inst 是 __new__ 实例不能放，
    # PyQt 未初始化实例访问属性会抛 RuntimeError。保存原列表，测试后恢复）
    from app.main_widget import OpenAIChatToolWindow

    orig_instances = list(getattr(OpenAIChatToolWindow, "_instances", []))
    OpenAIChatToolWindow._instances = [win1, win2]

    # mock TabManagerWindow.get_instance（刷新分组）
    with (
        patch("app.widgets.tab_manager_window.TabManagerWindow.get_instance") as m_tm_win,
        patch.object(InfoBar, "success") as m_success,
    ):
        try:
            inst._handle_team_new_task()
        finally:
            OpenAIChatToolWindow._instances = orig_instances

    # 1) 全员先 _create_new_session（保存旧历史到旧 run_id）
    win1._create_new_session.assert_called_once_with()
    win2._create_new_session.assert_called_once_with()
    # 2) force 生成新 run_id
    fake_tm.start_team_run.assert_called_once_with(force=True)
    # 3) 窗口 _team_run_id 更新为新值
    assert win1._team_run_id == "new-run-123", "成员窗口应更新为新 run_id（后续保存落新 run）"
    assert win2._team_run_id == "new-run-123"
    # 4) Tab 分组刷新（run_id 变化 → 窗口移入新组）
    assert m_tm_win.return_value.refresh_capsule_for_window.call_count == 2
    m_success.assert_called_once()


def test_handle_team_new_task_no_members_warns(qapp):
    """无成员 → InfoBar.warning，不 start_team_run"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_members.return_value = []
    inst._get_team_manager = MagicMock(return_value=fake_tm)

    with patch.object(InfoBar, "warning") as m_warn:
        inst._handle_team_new_task()

    m_warn.assert_called_once()
    assert not fake_tm.start_team_run.called


def test_handle_team_new_task_no_active_windows_warns(qapp):
    """成员记录存在但无活跃窗口 → InfoBar.warning，不 start_team_run"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_members.return_value = [{"window_id": "win01", "agent_name": "build"}]
    inst._get_team_manager = MagicMock(return_value=fake_tm)

    from app.main_widget import OpenAIChatToolWindow

    orig_instances = list(getattr(OpenAIChatToolWindow, "_instances", []))
    OpenAIChatToolWindow._instances = []  # 无 win01（inst 是 __new__ 实例不能放）
    try:
        with patch.object(InfoBar, "warning") as m_warn:
            inst._handle_team_new_task()
    finally:
        OpenAIChatToolWindow._instances = orig_instances

    m_warn.assert_called_once()
    assert not fake_tm.start_team_run.called
