# -*- coding: utf-8 -*-
"""临时对话页（temp scope）回归守卫

覆盖契约：
1. 页内会话不落库：两条保存主路径（``_save_current_session_to_history`` /
   ``_auto_save_current_session``）守卫链最前拦截 is_temp 会话 → SQLite 无记录
2. 关闭/退出清理：closeEvent 与 ``_on_app_about_to_quit`` 含临时目录清理逻辑（AST 守卫）；
   启动清扫 ``_cleanup_orphan_temp_pages`` 删除 tmp-sessions 下全部孤儿目录（行为）
3. 页内切换会话/项目后工作目录保留：tmp 目录不入 ``_current_workdir`` 实例缓存、不入 DB
4. ``_sync_working_directory`` 在 temp 页固定指向整页 tmp 目录
5. ``ChatSession.is_temp`` to_dict/from_dict 往返（旧记录无字段默认 False）
6. hook 执行 cwd 回退窗口工作目录（context.project_root；temp 页即 tmp 目录）
7. ``_resolve_project_workdir`` temp 页短路（DB 三级链会错拿源项目目录）
8. temp 页 Tab 视觉：无项目色块图标；右键菜单无「分支标签页」入口
"""
import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.conversation.chat_session import ChatSession
from app.main_widget import OpenAIChatToolWindow


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════
def _make_bare_window(**attrs):
    """裸实例（绕过 __init__，不建 UI），仅注入被测路径所需属性"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    for k, v in attrs.items():
        setattr(win, k, v)
    return win


def _make_temp_session() -> ChatSession:
    session = ChatSession(name="临时会话")
    session.is_temp = True
    session.messages = [{"role": "user", "content": "只存在于内存的问题"}]
    return session


# ═══════════════════════════════════════════════════════════════
# 1. 临时页发消息后 SQLite 无记录
# ═══════════════════════════════════════════════════════════════
class TestTempSessionNeverPersisted:
    def test_save_paths_skip_temp_session(self):
        """is_temp 会话经两条保存主路径均不产生任何落库调用"""
        session = _make_temp_session()
        sm = MagicMock()
        sm.get_current_session.return_value = session
        hm = MagicMock()
        win = _make_bare_window(
            session_manager=sm,
            history_manager=hm,
            _session_dirty=True,
            _current_session_id=session.session_id,
        )
        win._save_current_session_to_history()
        win._auto_save_current_session()
        hm.save_session.assert_not_called()
        hm.update_session.assert_not_called()

    def test_normal_session_still_persisted(self):
        """对照组：非 temp 会话守卫放行（继续执行保存逻辑，此处以走到历史管理器为证）"""
        session = ChatSession(name="普通会话")
        session.messages = [{"role": "user", "content": "会落库的问题"}]
        sm = MagicMock()
        sm.get_current_session.return_value = session
        hm = MagicMock()
        win = _make_bare_window(
            session_manager=sm,
            history_manager=hm,
            _session_dirty=True,
            _current_session_id=session.session_id,
        )
        # 裸实例缺后续依赖，放行后在 _team_run_id 属性访问处被 Qt 保护机制拦停 ——
        # 以此证明守卫未误伤普通会话（temp 会话则在守卫处直接 return，不触达该点）
        with pytest.raises(RuntimeError):
            win._save_current_session_to_history()


# ═══════════════════════════════════════════════════════════════
# 2. 关闭/退出清理 + 启动清扫
# ═══════════════════════════════════════════════════════════════
def _get_method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到方法 {cls.name}.{name}")


class TestTempPageCleanup:
    def test_close_event_cleans_temp_dir_ast(self):
        """closeEvent 必须含 _is_temp_scope 条件下的 rmtree（防清理逻辑被误删）"""
        src = (_REPO_ROOT / "app" / "main_widget.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "OpenAIChatToolWindow":
                close_event = _get_method(node, "closeEvent")
                body_src = ast.unparse(close_event)
                assert "_is_temp_scope" in body_src
                assert "tmp-sessions" in body_src
                return
        raise AssertionError("未找到 OpenAIChatToolWindow")

    def test_cleanup_orphan_temp_pages_removes_all(self, tmp_path, monkeypatch):
        """启动清扫删除 tmp-sessions 下全部孤儿目录（崩溃残留兜底）"""
        base = tmp_path / "tmp-sessions"
        for wid in ("win_01", "win_02"):
            d = base / wid
            d.mkdir(parents=True)
            (d / "workdir.txt").write_text("残留", encoding="utf-8")
        monkeypatch.setattr("app.main_widget.get_app_data_dir", lambda: tmp_path)
        OpenAIChatToolWindow._cleanup_orphan_temp_pages()
        assert not base.exists() or not any(base.iterdir())

    def test_cleanup_noop_when_missing(self, tmp_path, monkeypatch):
        """目录不存在时清扫静默跳过（不抛异常）"""
        monkeypatch.setattr("app.main_widget.get_app_data_dir", lambda: tmp_path)
        OpenAIChatToolWindow._cleanup_orphan_temp_pages()


# ═══════════════════════════════════════════════════════════════
# 3 + 4. _sync_working_directory 在 temp 页指向 tmp 目录且跨会话/项目保留
# ═══════════════════════════════════════════════════════════════
class TestTempPageWorkdir:
    @pytest.fixture
    def temp_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.main_widget.get_app_data_dir", lambda: tmp_path)
        tool_executor = MagicMock()
        tool_executor.get_workdir.return_value = ""  # 前后一致 → 不触发欢迎卡重渲染
        backend = MagicMock()
        backend.memory_manager = MagicMock()
        backend.tool_executor = tool_executor
        win = _make_bare_window(
            _is_destroyed=False,
            _is_temp_scope=True,
            _window_id="win_77",
            _current_project="默认项目",
            _current_workdir={},
            backend=backend,
        )
        # 裸实例补齐 UI 副作用方法（真实实现依赖完整窗口，此处只关心 workdir 语义）
        for m in ("_rerender_welcome_card", "_update_branch", "_push_workbench_project", "_publish_project_changed"):
            setattr(win, m, MagicMock())
        return win, tool_executor

    def test_sync_working_directory_returns_tmp_dir(self, temp_window):
        """temp 页 _sync_working_directory 固定指向整页 tmp 目录

        _ensure_temp_page_workdir 内部与公共尾部各调一次 set_workdir（幂等），
        两次参数必须一致且均为 tmp 目录。
        """
        win, tool_executor = temp_window
        win._sync_working_directory()
        calls = tool_executor.set_workdir.call_args_list
        assert len(calls) >= 1
        paths = {str(c[0][0]) for c in calls}
        assert len(paths) == 1, f"重复调用应幂等：{paths}"
        expected = paths.pop()
        assert "tmp-sessions" in expected
        assert "win_77" in expected

    def test_workdir_kept_across_session_switch(self, temp_window):
        """页内切换会话（再次同步）目录保留：同一 tmp 路径、不入实例缓存"""
        win, tool_executor = temp_window
        win._sync_working_directory()
        first = tool_executor.set_workdir.call_args[0][0]
        # 模拟页内切换会话/项目后再同步（showEvent 重入路径）
        win._current_project = "另一个项目"
        win._sync_working_directory()
        second = tool_executor.set_workdir.call_args[0][0]
        assert first == second, "临时页工作目录应跨会话/项目保留"
        assert win._current_workdir == {}, "tmp 目录不得写入实例缓存"
        win.backend.memory_manager.get_working_directory.assert_not_called()
        win.backend.memory_manager.set_working_directory.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 5. is_temp 序列化往返
# ═══════════════════════════════════════════════════════════════
class TestChatSessionIsTempRoundTrip:
    def test_to_dict_from_dict_roundtrip(self):
        session = ChatSession(name="往返", messages=[{"role": "user", "content": "hi"}])
        session.is_temp = True
        data = session.to_dict(consolidated=False)
        assert data["is_temp"] is True
        restored = ChatSession.from_dict(data)
        assert restored.is_temp is True

    def test_legacy_record_defaults_false(self):
        """旧记录无 is_temp 字段时默认 False（向后兼容）"""
        session = ChatSession(name="旧记录", messages=[])
        data = session.to_dict(consolidated=False)
        data.pop("is_temp", None)
        restored = ChatSession.from_dict(data)
        assert restored.is_temp is False

    def test_default_false(self):
        assert ChatSession(name="默认").is_temp is False


# ═══════════════════════════════════════════════════════════════
# 6. hook 执行 cwd 回退窗口工作目录
# ═══════════════════════════════════════════════════════════════
class TestHookCwdFallback:
    @pytest.fixture
    def hook_mgr(self):
        from app.core.hooks.hook_manager import HookManager

        hm = HookManager.__new__(HookManager)
        hm._cwd_resolve_cache = {}
        hm._cwd_cache_sweep_counter = 0
        return hm

    def test_no_script_falls_back_to_project_root(self, hook_mgr):
        """无脚本 hook 命令：cwd 回退 context.project_root（temp 页即 tmp 目录）"""
        from app.core.hooks.hook_manager import Hook

        hook = Hook(id="t", type="command", command="echo hi")
        cwd = hook_mgr._resolve_command_cwd(hook, {"project_root": "X:/tmp/tmp-sessions/win_77"})
        assert cwd == "X:/tmp/tmp-sessions/win_77"

    def test_no_project_root_returns_none(self, hook_mgr):
        """context 无 project_root（异常路径）：维持旧行为返回 None（子进程继承进程 cwd）"""
        from app.core.hooks.hook_manager import Hook

        hook = Hook(id="t", type="command", command="echo hi")
        assert hook_mgr._resolve_command_cwd(hook, {}) is None

    def test_explicit_cwd_still_wins(self, hook_mgr):
        """显式配置的 hook.cwd 优先级不变"""
        from app.core.hooks.hook_manager import Hook

        hook = Hook(id="t", type="command", command="echo hi", cwd="X:/explicit")
        cwd = hook_mgr._resolve_command_cwd(hook, {"project_root": "X:/tmp"})
        assert cwd == "X:/explicit"


# ═══════════════════════════════════════════════════════════════
# 7. _resolve_project_workdir temp 页短路
# ═══════════════════════════════════════════════════════════════
class TestResolveProjectWorkdirTempShortCircuit:
    def test_temp_page_skips_db_chain(self):
        """temp 页直接返回 tool_executor 工作目录，不落 DB 查询（防错拿源项目目录）"""
        tool_executor = MagicMock()
        tool_executor.get_workdir.return_value = "X:/appdata/tmp-sessions/win_77"
        backend = MagicMock()
        backend.tool_executor = tool_executor
        backend.memory_manager = MagicMock()
        win = _make_bare_window(
            _is_temp_scope=True,
            _current_project="源项目",
            _current_workdir={},
            backend=backend,
        )
        assert win._resolve_project_workdir() == "X:/appdata/tmp-sessions/win_77"
        backend.memory_manager.get_working_directory.assert_not_called()

    def test_normal_page_uses_three_level_chain(self):
        """对照：普通窗口维持三级链（实例缓存命中即返回）"""
        win = _make_bare_window(
            _is_temp_scope=False,
            _current_project="项目A",
            _current_workdir={"项目A": "X:/work/项目A"},
            backend=MagicMock(),
        )
        assert win._resolve_project_workdir() == "X:/work/项目A"


# ═══════════════════════════════════════════════════════════════
# 8. temp 页 Tab 视觉：无色块、无分支入口
# ═══════════════════════════════════════════════════════════════
def _trigger_context_menu(panel, pick: str):
    """模拟在标签页 0 上右键并选中 pick 项（与 test_tab_context_switch_session 同款）"""
    from PyQt5.QtCore import QPoint
    from PyQt5.QtWidgets import QMenu

    class _FakeMenu:
        def __init__(self):
            self.actions = []
            self._menu = QMenu()

        def addAction(self, text):
            act = MagicMock()
            act.text.return_value = text
            self.actions.append(act)
            return act

        def addSeparator(self):
            return None

        def setStyleSheet(self, *a, **kw):
            return None

        def exec_(self, *a, **kw):
            for act in self.actions:
                if act.text() == pick:
                    return act
            return None

        def close(self):
            return None

    fake = _FakeMenu()

    class _Ev:
        def globalPos(self):
            return QPoint(5, 5)

    with patch("app.widgets.tab_panel.QMenu", return_value=fake), patch.object(
        panel, "_active_list_container", return_value=panel._list_layout.parentWidget() or panel
    ), patch.object(panel, "childAt", return_value=None):
        panel.contextMenuEvent(_Ev())
    return fake


@pytest.fixture
def panel(qtbot):
    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode("list", persist=False)
    qtbot.addWidget(p)
    return p


class TestTempTabVisual:
    def test_tab_item_hides_icon_without_project_data(self, qtbot):
        """TabItem 无项目数据（temp 页）不显示色块图标"""
        from app.widgets.tab_panel import TabItem

        item = TabItem("临时 · 新对话")
        qtbot.addWidget(item)
        item.set_project("", "")
        assert item._icon_widget.isHidden()
        # 对照：有项目数据显示
        item.set_project("DR", "#4A90D9")
        assert not item._icon_widget.isHidden()

    def test_temp_tab_context_menu_hides_branch_item(self, panel):
        """temp 页右键菜单无「分支标签页」入口（分支对不落盘页无意义）"""
        panel.add_tab("临时 · 新对话")
        host = SimpleNamespace(_window_id="win_77", _windows=[SimpleNamespace(_is_temp_scope=True)])
        with patch.object(panel, "_resolve_tab_host", return_value=host):
            fake = _trigger_context_menu(panel, "分支标签页")
        labels = [a.text() for a in fake.actions]
        assert "分支标签页" not in labels
        assert "新建标签页" in labels
        assert "新建临时对话页" in labels

    def test_normal_tab_context_menu_keeps_branch_item(self, panel):
        """对照：普通页右键菜单保留「分支标签页」"""
        panel.add_tab("普通会话")
        host = SimpleNamespace(_window_id="win_01", _windows=[SimpleNamespace(_is_temp_scope=False)])
        with patch.object(panel, "_resolve_tab_host", return_value=host):
            fake = _trigger_context_menu(panel, "分支标签页")
        labels = [a.text() for a in fake.actions]
        assert "分支标签页" in labels

    def test_cancel_menu_on_temp_tab_emits_nothing(self, panel):
        """review M1：temp 页右键后 ESC 取消（exec_ 返回 None），不得误发任何请求

        根因：branch_action 预初始化为 None，menu.exec_() 取消时也返回 None，
        旧代码 None == None 为 True → 误发 tabBranchRequested。
        """
        panel.add_tab("临时 · 新对话")
        host = SimpleNamespace(_window_id="win_77", _windows=[SimpleNamespace(_is_temp_scope=True)])
        emitted: list = []
        panel.tabBranchRequested.connect(lambda i: emitted.append(("branch", i)))
        panel.tabCloseRequested.connect(lambda i: emitted.append(("close", i)))
        panel.newTabRequested.connect(lambda: emitted.append(("new", None)))
        panel.newTempTabRequested.connect(lambda: emitted.append(("new_temp", None)))
        with patch.object(panel, "_resolve_tab_host", return_value=host):
            _trigger_context_menu(panel, "__cancelled__")
        assert emitted == [], emitted


class TestHoverTooltipChildTraversal:
    """tooltip 子控件穿越修复：Leave 时光标仍在控件几何内不隐藏

    场景：「新建对话」行含子控件（icon/文案/三点按钮），鼠标移入子控件时
    行收到 Leave——旧行为直接隐藏 tooltip，表现为"显示一下又立马消失"。
    """

    def _leave_event(self):
        from PyQt5.QtCore import QEvent

        return QEvent(QEvent.Type.Leave)

    def test_row_installs_single_filter(self, panel):
        """行只挂一份 filter（三点按钮不重复安装，防同屏双 tooltip 叠加）"""
        from app.widgets.simple_hover_tooltip import get_hover_filter

        assert get_hover_filter(panel._new_chat_row) is not None
        assert get_hover_filter(panel._new_more_btn) is None

    def test_leave_with_cursor_inside_geometry_keeps_tooltip(self, panel, qtbot):
        """Leave 时光标仍在行几何内（= 移到子控件上）→ 不隐藏"""
        from PyQt5.QtCore import QPoint
        from PyQt5.QtGui import QCursor
        from app.widgets.simple_hover_tooltip import get_hover_filter

        panel.show()
        qtbot.waitExposed(panel)
        row = panel._new_chat_row
        row.resize(200, 32)
        f = get_hover_filter(row)
        hides = []
        original_hide = f._hide
        f._hide = lambda: hides.append(1)
        try:
            inside_global = row.mapToGlobal(QPoint(row.width() // 2, row.height() // 2))
            with patch.object(QCursor, "pos", return_value=inside_global):
                f.eventFilter(row, self._leave_event())
            assert hides == [], "光标仍在行几何内（子控件上）不应隐藏 tooltip"
        finally:
            f._hide = original_hide

    def test_leave_with_cursor_outside_geometry_hides_tooltip(self, panel):
        """Leave 时光标已离开行几何 → 正常隐藏（原有行为不回退）"""
        from PyQt5.QtCore import QPoint
        from PyQt5.QtGui import QCursor
        from app.widgets.simple_hover_tooltip import get_hover_filter

        row = panel._new_chat_row
        row.resize(200, 32)
        f = get_hover_filter(row)
        hides = []
        f._hide = lambda: hides.append(1)
        inside_global = row.mapToGlobal(QPoint(row.width() // 2, row.height() // 2))
        outside_global = inside_global + QPoint(10000, 10000)
        with patch.object(QCursor, "pos", return_value=outside_global):
            f.eventFilter(row, self._leave_event())
        assert len(hides) == 1, "光标真正离开几何应隐藏 tooltip"


class TestNewChatRow:
    """顶栏改版：icon + 「新建对话」行 + 三点菜单（独立临时按钮已移除）"""

    def test_row_widgets_exist_and_legacy_buttons_removed(self, panel):
        """新行存在；旧版双按钮（_new_btn/_new_temp_btn）不再创建"""
        assert getattr(panel, "_new_chat_row", None) is not None
        assert getattr(panel, "_new_more_btn", None) is not None
        assert not hasattr(panel, "_new_btn")
        assert not hasattr(panel, "_new_temp_btn")

    def test_row_click_emits_new_tab(self, panel, qtbot):
        """点击新建行 = 普通新建对话页"""
        from PyQt5.QtCore import QEvent, Qt
        from PyQt5.QtGui import QMouseEvent
        from PyQt5.QtCore import QPointF

        emitted: list = []
        panel.newTabRequested.connect(lambda: emitted.append("new"))
        event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(1, 1), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        panel._on_new_chat_row_clicked(event)
        assert emitted == ["new"]

    def test_more_menu_emits_temp_request(self, panel, qtbot):
        """三点菜单选「新建临时对话页」→ 发 newTempTabRequested；含模式切换项"""
        from unittest.mock import MagicMock as _MM

        emitted: list = []
        panel.newTempTabRequested.connect(lambda: emitted.append("temp"))

        fake_temp = _MM()
        fake_list = _MM()
        fake_tree = _MM()
        fake_sep = _MM()
        with patch("app.widgets.tab_panel.QMenu") as _menu_cls:
            menu_inst = _menu_cls.return_value
            menu_inst.addAction.side_effect = [fake_temp, fake_list, fake_tree]
            menu_inst.addSeparator.return_value = fake_sep
            menu_inst.exec_.return_value = fake_temp
            panel._show_new_chat_menu()
        labels = [c.args[0] for c in menu_inst.addAction.call_args_list]
        assert labels == ["新建临时对话页", "列表模式", "工作区树模式"]
        assert emitted == ["temp"]
        # 选「列表模式」→ set_mode(list)
        with patch("app.widgets.tab_panel.QMenu") as _menu_cls, patch.object(panel, "set_mode") as _set_mode:
            menu_inst = _menu_cls.return_value
            menu_inst.addAction.side_effect = [fake_temp, fake_list, fake_tree]
            menu_inst.exec_.return_value = fake_list
            panel._show_new_chat_menu()
        _set_mode.assert_called_once()

    def test_collapse_hides_label_and_more_btn(self, panel):
        """收起态：新建行只剩 icon（文案与三点隐藏）"""
        panel._collapsed = True
        panel._update_toggle_button(switch_ui=True)
        assert panel._new_chat_label.isHidden()
        assert panel._new_more_btn.isHidden()
        panel._collapsed = False
        panel._update_toggle_button(switch_ui=True)
        assert not panel._new_chat_label.isHidden()
        assert not panel._new_more_btn.isHidden()
