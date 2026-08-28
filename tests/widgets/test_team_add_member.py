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
from app.core import window_registry


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


def test_team_close_btn_has_tooltip(qapp):
    """关闭团队按钮必须有 tooltip（hover 提示"关闭团队"）"""
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        tip = grp._team_close_btn.toolTip()
        assert tip, "close_btn tooltip 不能为空"
        assert tip == "关闭团队"
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


def test_team_enter_sends_enter_to_button_under_cursor(qapp):
    """问题A：_enter 显示按钮后，若鼠标已在某按钮上方则补发 QEnterEvent"""
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QEnterEvent

    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        close_btn = grp._team_close_btn
        header = grp._team_header

        sent = []
        with (
            patch("PyQt5.QtWidgets.QApplication.widgetAt", return_value=close_btn),
            patch("PyQt5.QtGui.QCursor.pos", return_value=QPoint(100, 100)),
            patch(
                "PyQt5.QtWidgets.QApplication.sendEvent",
                side_effect=lambda w, e: sent.append((w, e)) or True,
            ),
        ):
            header.enterEvent(None)

        assert len(sent) == 1, "鼠标已在按钮上方时必须补发 Enter"
        w, ev = sent[0]
        assert w is close_btn
        assert isinstance(ev, QEnterEvent)
    finally:
        panel.deleteLater()


def test_team_enter_no_send_when_cursor_not_on_button(qapp):
    """问题A：鼠标不在任一按钮上 → 不补发 QEnterEvent"""
    from PyQt5.QtCore import QPoint

    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        header = grp._team_header

        with (
            patch("PyQt5.QtWidgets.QApplication.widgetAt", return_value=header),
            patch("PyQt5.QtGui.QCursor.pos", return_value=QPoint(5, 5)),
            patch("PyQt5.QtWidgets.QApplication.sendEvent") as m_send,
        ):
            header.enterEvent(None)

        m_send.assert_not_called()
    finally:
        panel.deleteLater()


def test_team_enter_no_send_in_compact_mode(qapp):
    """问题A：折叠态（紧凑模式）不显示按钮 → 不补发 QEnterEvent"""
    from PyQt5.QtCore import QPoint

    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        panel._apply_team_compact(grp, True)
        header = grp._team_header

        with (
            patch("PyQt5.QtWidgets.QApplication.widgetAt", return_value=grp._team_close_btn),
            patch("PyQt5.QtGui.QCursor.pos", return_value=QPoint(100, 100)),
            patch("PyQt5.QtWidgets.QApplication.sendEvent") as m_send,
        ):
            header.enterEvent(None)

        m_send.assert_not_called()
    finally:
        panel.deleteLater()


def test_team_replayed_enter_starts_tooltip_timer(qapp):
    """问题A（行为化）：真实补发 Enter 后 close_btn 的 tooltip filter 计时激活"""
    from PyQt5.QtCore import QPoint

    from app.widgets import simple_hover_tooltip as sht
    from app.widgets.tab_panel import TabPanel

    panel = TabPanel()
    try:
        grp = panel._get_or_create_team_group("team-A")
        close_btn = grp._team_close_btn
        header = grp._team_header

        # 不 mock sendEvent：真实发送，验证 filter 链路（Enter → timer.start）
        with (
            patch("PyQt5.QtWidgets.QApplication.widgetAt", return_value=close_btn),
            patch("PyQt5.QtGui.QCursor.pos", return_value=QPoint(100, 100)),
        ):
            header.enterEvent(None)

        f = sht._filters.get(id(close_btn))
        assert f is not None, "close_btn 应有 tooltip filter（setToolTip 自动安装）"
        assert f._timer.isActive(), "补发的 Enter 应启动 tooltip 计时"
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
    fake_tm.get_template_for_run_id.return_value = {"name": "default-team", "agents": [{"agent_name": "build"}]}
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
    # 同步 join（前置登记成员身份；M1：透传 run_id 归属）
    fake_tm.join_team.assert_called_once_with(window_id="win-99", agent_name="build", run_id="run-ABC")
    # 300ms 延迟 join 已安排
    assert m_single_shot.called
    delay = m_single_shot.call_args.args[0]
    assert delay == 300


def test_spawn_team_member_window_keep_team_name_preserves_run_id(qapp):
    """🐛 回归（快速新建成员漂移 bug）：延迟补注册必须传 keep_team_name=True，
    防止 _join_new_window_for_template 用 team.json 顶层 run_id / 当前模板名
    覆盖 _spawn_team_member_window 前置写入的权威归属（多团队并存时顶层 run_id
    是其他团队或新 run，新成员会被重分组进错误的 / 全新的团队框）。"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    fake_win = MagicMock()
    fake_win._window_id = "win-77"
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {"name": "other-template", "agents": []}
    fake_tm.get_team_run_id.return_value = "run-TOP"  # 顶层 run_id（其他团队/新 run）
    fake_tm.get_team_project.return_value = ""

    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._join_new_window_for_template = MagicMock()

    with patch.object(mw.QTimer, "singleShot") as m_single_shot:
        inst._spawn_team_member_window("build", run_id="run-A", team_label="团队A", team_name="团队A")

    # 创建时前置写入透传的权威归属（而非顶层 run_id）
    assert fake_win._team_run_id == "run-A"
    assert fake_win._team_name == "团队A"
    # 同步 join 透传权威 run_id（M1）
    fake_tm.join_team.assert_called_once_with(
        window_id="win-77", agent_name="build", run_id="run-A", team_label="团队A"
    )
    # 延迟回调必须 keep_team_name=True：执行回调后不应覆盖窗口归属
    assert m_single_shot.called
    delay, callback = m_single_shot.call_args.args[:2]
    assert delay == 300
    callback()
    inst._join_new_window_for_template.assert_called_once_with(fake_win, "build", "win-77", keep_team_name=True)


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

    inst._create_fresh_window.assert_called_once()


def test_spawn_team_member_window_inherits_source_project_when_team_empty(qapp):
    """🐛 回归：构建团队（/team --load / 快速新建成员）时若团队级项目尚未
    设置，新成员窗口应继承执行构建的源窗口（self._current_project）的项目，
    而非回落到全局默认项目。
    #5a-fix Plan C：按 run_id 粒度写入 projects_by_run_id[run_id]。
    """
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    inst.__dict__["_current_project"] = "项目-X"  # 源窗口当前项目

    fake_win = MagicMock()
    fake_win._window_id = "win-9"
    fake_tm = MagicMock()
    fake_tm.get_template_for_run_id.return_value = {"name": "t", "agents": []}
    fake_tm.get_team_run_id.return_value = "run-X"
    fake_tm.get_project_for_run_id.return_value = ""  # 团队级项目未设置
    fake_tm.get_team_project.return_value = ""  # 团队级项目未设置（回退接口）

    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)

    with patch.object(mw.QTimer, "singleShot"):
        inst._spawn_team_member_window("build")

    # 源窗口项目被复制并按 run_id 粒度落盘（#5a-fix Plan C）
    fake_tm.set_project_for_run_id.assert_called_once_with("项目-X", "run-X", team_name="t")
    # 并把项目应用到新窗口
    fake_win._apply_team_project.assert_called_once_with("项目-X")


def test_spawn_team_member_window_reuses_already_set_team_project(qapp):
    """团队级项目已设置时，新成员窗口沿用团队项目，不覆盖为源窗口项目。
    #5a-fix Plan C：按 run_id 粒度读取 get_project_for_run_id。
    """
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    inst.__dict__["_current_project"] = "源项目"

    fake_win = MagicMock()
    fake_win._window_id = "win-10"
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {"name": "t", "agents": []}
    fake_tm.get_team_run_id.return_value = "run-X"
    fake_tm.get_project_for_run_id.return_value = "团队项目"  # run_id 已有项目
    fake_tm.get_team_project.return_value = "团队项目"  # 回退接口

    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)

    with patch.object(mw.QTimer, "singleShot"):
        inst._spawn_team_member_window("build")

    fake_tm.set_project_for_run_id.assert_not_called()  # 不覆盖已有团队项目
    fake_tm.set_team_project.assert_not_called()
    fake_win._apply_team_project.assert_called_once_with("团队项目")


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


def test_spawn_team_members_activates_first_member_tab(qapp):
    """批量创建后激活 tab 切回第一个成员（修复：add_window 每次激活新窗口，
    批量创建 N 个后激活停在最后一个成员）。

    回归场景：/team --load=<模板> 加载 N 个角色 → 应自动选中第一个成员 tab，
    而非最后一个。单个成员（快速新建成员）时切回自身，行为不变。
    """
    from unittest.mock import call

    from app.widgets.tab_manager_window import TabManagerWindow

    inst = _make_main_widget_instance()

    w1, w2, w3 = MagicMock(), MagicMock(), MagicMock()
    inst._spawn_team_member_window = MagicMock(side_effect=[w1, w2, w3])
    inst._do_team_window_arrange = MagicMock()

    # 模拟 TabManagerWindow：add_window 每次激活最新窗口（idx = 该窗口在
    # _windows 中的索引），批量创建后激活停在最后一个（w3 → idx 2）。
    tm_instance = TabManagerWindow.__new__(TabManagerWindow)
    tm_instance._windows = [w1, w2, w3]
    tm_instance._window_to_index = {id(w1): 0, id(w2): 1, id(w3): 2}
    tm_instance._tab_panel = MagicMock()
    TabManagerWindow._instance = tm_instance
    try:
        n = inst._spawn_team_members(["build", "plan", "review"])
        assert n == 3
        # 批量创建后必须把激活 tab 切回第一个成员（w1 → idx 0）
        calls = tm_instance._tab_panel.set_active_index.call_args_list
        assert calls, "批量创建后未切换激活 tab"
        assert calls[-1] == call(0), f"激活应切回第一个成员 tab(idx 0)，实际: {calls[-1]}"
    finally:
        TabManagerWindow._instance = None


# ══════════════════════════════════════════════════════════
# ⑦ main_widget._handle_team_add_member：快速新建成员（可重复角色）
# ══════════════════════════════════════════════════════════


def test_handle_team_add_member_no_template_no_member_warns(qapp):
    """无模板且无成员 → InfoBar.warning 提示（F14：不再要求必须有模板）"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    inst.__dict__["_team_run_id"] = "run-A"  # ref_win 团队归属（M1'）
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
    inst.__dict__["_team_run_id"] = "run-A"
    inst.__dict__["_team_name"] = "团队A"
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
    # M1'：透传 ref_win 的 run_id / team_name，锁定目标团队（防漂移）
    inst._spawn_team_members.assert_called_once_with(["build"], run_id="run-A", team_label="团队A", team_name="团队A")
    assert "build" in actions and "plan" in actions


def test_handle_team_add_member_no_template_uses_member_roles(qapp):
    """无模板（手动 join 团队）→ 菜单列出当前成员角色（F14 兜底）"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    inst.__dict__["_team_run_id"] = "run-A"
    inst.__dict__["_team_name"] = "团队A"
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = None
    fake_tm.get_members.return_value = [{"agent_name": "build"}, {"agent_name": "build"}]  # 重复成员
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock(return_value=1)

    fake_menu_cls, actions = _setup_add_member_menu_patch("build")
    with patch("PyQt5.QtWidgets.QMenu", fake_menu_cls), patch.object(InfoBar, "success"):
        inst._handle_team_add_member()

    inst._spawn_team_members.assert_called_once_with(["build"], run_id="run-A", team_label="团队A", team_name="团队A")
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
    inst.__dict__["_team_run_id"] = "run-A"
    inst.__dict__["_team_name"] = "团队A"
    fake_tm = MagicMock()
    fake_tm.get_template_for_run_id.return_value = {
        "name": "t",
        "agents": [{"agent_name": "build"}, {"agent_name": "plan"}],
    }
    fake_tm.get_members.return_value = [{"agent_name": "build"}]
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._spawn_team_members = MagicMock(return_value=1)

    fake_menu_cls, actions = _setup_add_member_menu_patch("plan")
    with patch("PyQt5.QtWidgets.QMenu", fake_menu_cls), patch.object(InfoBar, "success") as m_success:
        inst._handle_team_add_member()

    inst._spawn_team_members.assert_called_once_with(["plan"], run_id="run-A", team_label="团队A", team_name="团队A")
    m_success.assert_called_once()


def test_handle_team_add_member_no_batch_fill_action(qapp):
    """F14：菜单不再包含"批量补齐全部角色"入口（原补齐逻辑移除）"""
    from qfluentwidgets import InfoBar

    inst = _make_main_widget_instance()
    inst.__dict__["_team_run_id"] = "run-A"
    inst.__dict__["_team_name"] = "团队A"
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

    orig_instances = list(window_registry.window_instances)
    window_registry.window_instances = [win1, win2]

    # mock TabManagerWindow.get_instance（刷新分组）
    with (
        patch("app.widgets.tab_manager_window.TabManagerWindow.get_instance") as m_tm_win,
        patch.object(InfoBar, "success") as m_success,
        # C3 链式交错：把窗间间隔压为 0，pure processEvents 即可推进全链
        # （singleShot(0) 不依赖真实时间流逝，避免 sleep 时序边界 flaky）
        patch.object(OpenAIChatToolWindow, "_TEAM_NEW_TASK_STAGGER_MS", 0),
    ):
        try:
            inst._handle_team_new_task()
            # 链式调度：step0(win1) 同步 → 0ms → step1(win2) → 0ms → step2(收尾)。
            # 每轮 processEvents 触发一个已到期 timer，多轮推进完整链。
            from PyQt5.QtCore import QCoreApplication

            for _ in range(8):
                QCoreApplication.processEvents()
        finally:
            window_registry.window_instances = orig_instances

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

    orig_instances = list(window_registry.window_instances)
    window_registry.window_instances = []  # 无 win01（inst 是 __new__ 实例不能放）
    try:
        with patch.object(InfoBar, "warning") as m_warn:
            inst._handle_team_new_task()
    finally:
        window_registry.window_instances = orig_instances

    m_warn.assert_called_once()
    assert not fake_tm.start_team_run.called


# ══════════════════════════════════════════════════════════
# ⑨ D2 标记前置 + E1/E2 幽灵窗口回收（子任务 #9）
# ══════════════════════════════════════════════════════════


def test_spawn_team_member_window_passes_team_marks_to_create_fresh(qapp):
    """D-T3：D2 根治——团队标记（agent/team_name/run_id）须作为参数传入
    _create_fresh_window（add_window 之前写入，Tab 分组直接命中团队框）"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    fake_win = MagicMock()
    fake_win._window_id = "win-99"
    fake_tm = MagicMock()
    fake_tm.get_template_for_run_id.return_value = {"name": "dev-team", "agents": []}
    fake_tm.get_team_run_id.return_value = "run-D2"
    fake_tm.get_team_project.return_value = ""
    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._join_new_window_for_template = MagicMock()
    with patch.object(mw.QTimer, "singleShot"):
        inst._spawn_team_member_window("build")
    # 团队三参数必须前置传入 _create_fresh_window
    _args, kwargs = inst._create_fresh_window.call_args
    assert kwargs.get("team_agent") == "build"
    assert kwargs.get("team_name") == "dev-team"
    assert kwargs.get("team_run_id") == "run-D2"


def test_spawn_team_member_window_aborts_on_join_failure(qapp):
    """E-T1：join_team 抛异常 → 主动回收半建窗口（_abort_team_window）+ 返回 None"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    fake_win = MagicMock()
    fake_win._window_id = "win-99"
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {"name": "dev-team", "agents": []}
    fake_tm.get_team_run_id.return_value = "run-E1"
    fake_tm.get_team_project.return_value = ""
    fake_tm.join_team.side_effect = RuntimeError("join boom")
    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    with patch.object(mw, "_abort_team_window") as m_abort:
        result = inst._spawn_team_member_window("build")
    assert result is None, "join 失败应返回 None"
    m_abort.assert_called_once_with(fake_win), "半建窗口应调用 _abort_team_window 回收"


def test_create_fresh_window_aborts_on_add_window_failure(qapp):
    """E-T2：add_window 抛异常 → _create_fresh_window 回收已构造窗口并返回 None"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    # ⚠️ __new__ 绕过 __init__ → 未初始化实例访问属性会抛
    # "super-class __init__() was never called"，需显式补 homepage
    # 使执行流通过 _create_fresh_window 首行的 sip.isdeleted 检查
    inst.homepage = MagicMock()
    fake_win = MagicMock()
    fake_win._window_id = "win-E2"
    # 模拟 OpenAIChatToolWindow 构造成功（PyQt 类实例化不走纯 Python __new__，
    # 需 patch 整个类并设 return_value 冒充构造返回值）
    with patch.object(mw, "OpenAIChatToolWindow") as m_cls, patch.object(mw, "_abort_team_window") as m_abort:
        m_cls.return_value = fake_win
        # 模拟 add_window 抛异常（TabManagerWindow 为 _create_fresh_window 内延迟 import）
        from app.widgets.tab_manager_window import TabManagerWindow

        with patch.object(TabManagerWindow, "get_instance") as m_get_tm:
            fake_tm_win = MagicMock()
            fake_tm_win.add_window.side_effect = RuntimeError("add_window boom")
            fake_tm_win.isVisible.return_value = True
            m_get_tm.return_value = fake_tm_win
            # homepage 需有效（sip.isdeleted 检查通过）
            with patch.object(mw.sip, "isdeleted", return_value=False):
                result = inst._create_fresh_window()
    assert result is None, "注册失败应返回 None"
    m_abort.assert_called_once_with(fake_win), "已构造窗口应调用 _abort_team_window 回收"


def test_abort_team_window_removes_from_tab_and_closes(qapp):
    """E1/E2 公共兜底：_abort_team_window 优先 remove_window（Tab 完整清理），
    未注册时兜底 close"""
    import app.main_widget as mw

    fake_win = MagicMock()
    fake_win._window_id = "win-A1"
    from app.widgets.tab_manager_window import TabManagerWindow

    with patch.object(TabManagerWindow, "get_instance") as m_get_tm:
        fake_tm_win = MagicMock()
        m_get_tm.return_value = fake_tm_win
        mw._abort_team_window(fake_win)
    fake_tm_win.remove_window.assert_called_once_with(fake_win), "应走 Tab 管理器完整清理"
    # 未注册（get_instance 返回 None）→ 兜底 close
    fake_win2 = MagicMock()
    with patch.object(TabManagerWindow, "get_instance", return_value=None):
        mw._abort_team_window(fake_win2)
    fake_win2.close.assert_called_once(), "未注册 Tab 时兜底 close"


# ══════════════════════════════════════════════════════════
# ⑨ 团队模型继承：构建继承构建者标签页模型 / 恢复还原成员最后使用的模型
# ══════════════════════════════════════════════════════════


def _make_builder_with_model(inst):
    """给 __new__ 实例补模型选择上下文（构建者标签页）"""
    inst._current_provider_name = "prov-1"
    inst._current_model_name = "gpt-4o"
    inst._user_manually_selected_model = True
    inst._valid_configs = {
        "prov-1": {"provider_name": "Provider A", "模型名称": "gpt-4o"},
        "prov-2": {"provider_name": "Provider B", "模型名称": "claude-3.5"},
    }
    inst._display_to_config_id = {"Provider A": "prov-1", "Provider B": "prov-2"}


def test_spawn_team_member_window_inherits_builder_model(qapp):
    """需求 1：团队构建时成员窗口继承构建者标签页当前选中的模型"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    _make_builder_with_model(inst)

    fake_win = MagicMock()
    fake_win._is_destroyed = False
    fake_win._window_id = "win-99"
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {"name": "dev-team", "agents": []}
    fake_tm.get_team_run_id.return_value = "run-ABC"
    fake_tm.get_team_project.return_value = ""
    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._join_new_window_for_template = MagicMock()
    with patch.object(mw.QTimer, "singleShot"):
        inst._spawn_team_member_window("build")

    # 成员窗口继承构建者模型选择
    assert fake_win._current_provider_name == "prov-1"
    assert fake_win._current_model_name == "gpt-4o"
    assert fake_win._user_manually_selected_model is True
    # 配置视图复制：异步 _load_model_configs 完成前即可匹配/渲染按钮
    assert fake_win._valid_configs == inst._valid_configs
    assert fake_win._display_to_config_id == {"Provider A": "prov-1", "Provider B": "prov-2"}
    fake_win._update_model_selector_btn.assert_called_once()


def test_spawn_team_member_window_without_builder_model_keeps_default(qapp):
    """构建者未选模型（__new__ 实例无模型属性）→ 不覆盖新窗口、不抛异常"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    fake_win = MagicMock()
    fake_win._is_destroyed = False
    fake_win._window_id = "win-99"
    fake_tm = MagicMock()
    fake_tm.get_template.return_value = {"name": "dev-team", "agents": []}
    fake_tm.get_team_run_id.return_value = "run-ABC"
    fake_tm.get_team_project.return_value = ""
    inst._create_fresh_window = MagicMock(return_value=fake_win)
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._join_new_window_for_template = MagicMock()
    with patch.object(mw.QTimer, "singleShot"):
        result = inst._spawn_team_member_window("build")

    assert result is fake_win
    # 未设置 provider → 不写 _current_provider_name（__new__ 实例无模型属性，
    # 方法内部 self._valid_configs 访问抛 RuntimeError 被静默兜底）
    assert "_current_provider_name" not in fake_win.__dict__


def test_apply_model_selection_restores_member_model(qapp):
    """辅助方法：按 provider_name（display_name）还原成员最后使用的模型"""
    inst = _make_main_widget_instance()
    _make_builder_with_model(inst)
    fake_win = MagicMock()
    fake_win._is_destroyed = False

    inst._apply_model_selection_to_window(fake_win, model_name="claude-3.5", provider_name="Provider B")

    assert fake_win._current_provider_name == "prov-2"
    assert fake_win._current_model_name == "claude-3.5"
    assert fake_win._user_manually_selected_model is True
    fake_win._update_model_selector_btn.assert_called_once()


def test_apply_model_selection_falls_back_to_model_name_match(qapp):
    """provider 无法匹配 → 遍历 _valid_configs 按模型名唯一匹配"""
    inst = _make_main_widget_instance()
    _make_builder_with_model(inst)
    fake_win = MagicMock()
    fake_win._is_destroyed = False

    inst._apply_model_selection_to_window(fake_win, model_name="claude-3.5", provider_name="Unknown-Provider")

    assert fake_win._current_provider_name == "prov-2"
    assert fake_win._current_model_name == "claude-3.5"


def test_apply_model_selection_no_match_falls_back_to_builder(qapp):
    """消息模型无法匹配任何配置 → 回退继承构建者标签页当前模型"""
    inst = _make_main_widget_instance()
    _make_builder_with_model(inst)
    fake_win = MagicMock()
    fake_win._is_destroyed = False

    inst._apply_model_selection_to_window(fake_win, model_name="nonexistent-model", provider_name="Nonexistent")

    assert fake_win._current_provider_name == "prov-1"
    assert fake_win._current_model_name == "gpt-4o"

