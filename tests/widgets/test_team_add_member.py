# -*- coding: utf-8 -*-
"""团队框"新建成员"按钮 + 新建/批量补齐成员会话测试

覆盖范围：
① TabPanel 暴露 teamAddMemberRequested(str) 信号（验证 Signal 而非具体行为）
② header 含 _team_add_btn 访问器；点击 → teamAddMemberRequested(team_id) 发射
③ add 按钮默认隐藏；hover 显示（与 close 联动）；折叠态隐藏
④ TabManagerWindow._on_team_add_member_requested 委托参照窗口 _handle_team_add_member
⑤ main_widget._spawn_team_member_window：run_id 复用（不 start_team_run）+ 同步 join
⑥ main_widget._spawn_team_members：批量计数 / 归零排列
⑦ main_widget._handle_team_add_member：无模板提示 / 角色已齐 / 单角色创建 / 批量补齐（去重）

设计说明：
- qapp 由 pytest-qt 提供（session 级 QApplication）
- 重依赖（backend/QWebEngine/TrayManager）一律用 __new__ 绕过 + MagicMock 隔离，
  与 tests/widgets/test_team_group_header_close.py 风格一致
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


# ══════════════════════════════════════════════════════════
# ② header 新建成员按钮：访问器 + 信号发射
# ══════════════════════════════════════════════════════════


def test_team_group_has_add_btn(qapp):
    """team 框 header 必须包含 _team_add_btn 访问器（与 close_btn 并列）"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-X")
        assert hasattr(grp, "_team_add_btn")
        # 布局顺序：header 布局含 add_btn 与 close_btn
        assert grp._team_add_btn.parent() is grp._team_header
        assert grp._team_close_btn.parent() is grp._team_header
    finally:
        panel.deleteLater()


def test_team_add_btn_emits_team_id(qapp):
    """点击新建成员按钮 → teamAddMemberRequested(team_id) 信号发射

    注：FluentWidget 的 clicked 信号带 bool 参数（checked state），需在 lambda 中
    用 *args 忽略，否则 emit 会收到 bool 类型不匹配 str 类型而抛 TypeError。
    """
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


# ══════════════════════════════════════════════════════════
# ③ 按钮可见性：默认隐藏 / hover 显示 / 折叠态隐藏
# ══════════════════════════════════════════════════════════


def test_add_btn_hidden_by_default(qapp):
    """新建成员按钮默认隐藏（与 close_btn 同款行为）"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        assert grp._team_add_btn.isVisibleTo(grp._team_header) is False
        assert grp._team_close_btn.isVisibleTo(grp._team_header) is False
    finally:
        panel.deleteLater()


def test_add_btn_visible_on_header_hover(qapp):
    """鼠标进入 header → add/close 按钮都显示；leave → 都隐藏"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        add_btn = grp._team_add_btn
        close_btn = grp._team_close_btn
        header = grp._team_header

        add_calls, close_calls = [], []
        add_btn.setVisible = lambda v: (add_calls.append(v), MagicMock())[1]
        close_btn.setVisible = lambda v: (close_calls.append(v), MagicMock())[1]

        header.enterEvent(None)
        assert add_calls[-1] is True, "enterEvent 应触发 add_btn setVisible(True)"
        assert close_calls[-1] is True, "enterEvent 应触发 close_btn setVisible(True)"

        header.leaveEvent(None)
        assert add_calls[-1] is False, "leaveEvent 应触发 add_btn setVisible(False)"
        assert close_calls[-1] is False, "leaveEvent 应触发 close_btn setVisible(False)"
    finally:
        panel.deleteLater()


def test_add_btn_hidden_in_compact_mode(qapp):
    """折叠态（_apply_team_compact(True)）→ add/close 按钮隐藏；展开恢复 hover 逻辑"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        add_btn = grp._team_add_btn
        close_btn = grp._team_close_btn

        panel._apply_team_compact(grp, True)
        assert add_btn.isVisibleTo(grp._team_header) is False
        assert close_btn.isVisibleTo(grp._team_header) is False
        # 折叠态 enter 不应弹按钮（紧凑态守卫）
        grp._team_header.enterEvent(None)
        assert add_btn.isVisibleTo(grp._team_header) is False

        panel._apply_team_compact(grp, False)
        # 展开态恢复默认（隐藏，等待 hover）
        assert add_btn.isVisibleTo(grp._team_header) is False
        assert close_btn.isVisibleTo(grp._team_header) is False
        # 展开态 hover 可显示
        grp._team_header.enterEvent(None)
        assert add_btn.isVisibleTo(grp._team_header) is True
    finally:
        panel.deleteLater()


# ══════════════════════════════════════════════════════════
# ④ TabManagerWindow._on_team_add_member_requested 委托
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
# ⑤ main_widget._spawn_team_member_window：run_id 复用 + 同步 join
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
    """⚠️ 核心约束：补成员必须复用现有 run_id，禁止 start_team_run(force=True)"""
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

    try:
        with patch.object(mw.QTimer, "singleShot") as m_single_shot:
            result = inst._spawn_team_member_window("build")

        assert result is fake_win
        # 复用现有 run_id（T3 坑 1 防线）
        assert fake_win._team_run_id == "run-ABC"
        assert fake_win._team_agent_name == "build"
        assert fake_win._team_name == "default-team"
        # 禁止 force 新 run_id
        assert not fake_tm.start_team_run.called, "补成员不得 start_team_run(force=True)"
        # 同步 join（前置登记成员身份）
        fake_tm.join_team.assert_called_once_with(window_id="win-99", agent_name="build")
        # 300ms 延迟 join 已安排
        assert m_single_shot.called
        delay = m_single_shot.call_args.args[0]
        assert delay == 300
    finally:
        pass


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
# ⑦ main_widget._handle_team_add_member：交互兜底 + 创建
# ══════════════════════════════════════════════════════════


def test_handle_team_add_member_no_template_warns(qapp):
    """无模板（手动 /team --join 团队）→ InfoBar.warning 提示先 /team --load"""
    from qfluentwidgets import InfoBar

    from app.main_widget import OpenAIChatToolWindow

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = None
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock()

    with patch.object(InfoBar, "warning") as m_warn:
        inst._handle_team_add_member()

    m_warn.assert_called_once()
    assert not inst._spawn_team_members.called


def test_handle_team_add_member_all_joined_info(qapp):
    """模板角色全部已加入 → InfoBar.info"团队角色已齐"，不弹菜单"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {
        "name": "t",
        "agents": [{"agent_name": "build"}, {"agent_name": "plan"}],
    }
    fake_tm.get_members.return_value = [{"agent_name": "build"}, {"agent_name": "plan"}]
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock()

    with patch.object(InfoBar, "info") as m_info, patch("PyQt5.QtWidgets.QMenu") as m_menu_cls:
        inst._handle_team_add_member()

    assert m_info.called
    assert not inst._spawn_team_members.called
    assert not m_menu_cls.called, "角色已齐时不应弹菜单"


def _setup_add_member_menu_patch(choice):
    """构造 QMenu patch：addAction 动态创建 action，exec_ 返回指定项

    Args:
        choice: "build" / "plan" / "batch" / None（None 表示用户取消，exec_ 返回 None）

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
    # 延迟解析：exec_ 被调用时（addAction 已执行完）再从 actions_by_text 取目标 action
    # choice 键："build"/"plan"/"batch"（映射到实际菜单文本）/None（用户取消）
    _choice_map = {
        "build": "build",
        "plan": "plan",
        "batch": "批量补齐全部角色",
    }
    fake_menu.exec_ = MagicMock(side_effect=lambda pos: actions_by_text.get(_choice_map.get(choice)))
    return fake_menu_cls, actions_by_text


def test_handle_team_add_member_single_create(qapp):
    """单角色创建：菜单选择未加入角色 → _spawn_team_members([该角色])"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {
        "name": "t",
        "agents": [{"agent_name": "build"}, {"agent_name": "plan"}],
    }
    fake_tm.get_members.return_value = [{"agent_name": "build"}]  # plan 未加入
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock(return_value=1)

    fake_menu_cls, actions = _setup_add_member_menu_patch("plan")
    with patch("PyQt5.QtWidgets.QMenu", fake_menu_cls), patch.object(InfoBar, "success") as m_success:
        inst._handle_team_add_member()

    inst._spawn_team_members.assert_called_once_with(["plan"])
    m_success.assert_called_once()


def test_handle_team_add_member_batch_fill(qapp):
    """批量补齐：选择"批量补齐全部角色" → _spawn_team_members(全部未加入)"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {
        "name": "t",
        "agents": [{"agent_name": "build"}, {"agent_name": "plan"}, {"agent_name": "review"}],
    }
    fake_tm.get_members.return_value = [{"agent_name": "build"}]  # plan/review 未加入
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock(return_value=2)

    fake_menu_cls, actions = _setup_add_member_menu_patch("batch")
    with patch("PyQt5.QtWidgets.QMenu", fake_menu_cls), patch.object(InfoBar, "success") as m_success:
        inst._handle_team_add_member()

    # 去重断言（T3 坑 2）：build 已加入，只创建 plan/review
    inst._spawn_team_members.assert_called_once_with(["plan", "review"])
    m_success.assert_called_once()


def test_handle_team_add_member_menu_actions_disabled_for_joined(qapp):
    """菜单中已加入角色 action 应被置灰（setEnabled(False)）"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {
        "name": "t",
        "agents": [{"agent_name": "build"}, {"agent_name": "plan"}],
    }
    fake_tm.get_members.return_value = [{"agent_name": "build"}]  # build 已加入
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock(return_value=1)

    fake_menu_cls, actions = _setup_add_member_menu_patch("plan")
    with patch("PyQt5.QtWidgets.QMenu", fake_menu_cls), patch.object(InfoBar, "success"):
        inst._handle_team_add_member()

    # build 已加入 → setEnabled(False)；plan 未加入 → setEnabled(True)
    actions["build"].setEnabled.assert_called_once_with(False)
    actions["plan"].setEnabled.assert_called_once_with(True)
