# -*- coding: utf-8 -*-
"""worktree 工具切换请求路由回归测试。

背景缺陷：manage_worktree(create/switch) 的自动切目录经 bridge 投递主线程后，
接收端用「当前活跃窗口」执行切换。用户浏览其他项目页面时，DriFox 的工作树
路径被持久化写入其他项目的 project_workdir 表（跨项目污染）。

修复语义：信号必须携带发起会话 session_id；接收端按会话定位窗口并复用
WorktreeService.switch_to_worktree（host 绑定该窗口）；定位不到窗口则放弃
切换——宁可不切，不能切错。
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "plugins" / "worktree-manager" / "tools" / "worktree_tools.py"
_UI = Path(__file__).resolve().parents[2] / "plugins" / "worktree-manager" / "ui"


def _load_tools_module():
    """与生产 tool loader 同构：裸模块加载（无 __package__）。"""
    name = "_worktree_tools_under_test"
    spec = importlib.util.spec_from_file_location(name, _TOOLS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tools(monkeypatch):
    """加载 tools 模块并替换 bridge 为记录型假桥。"""
    mod = _load_tools_module()
    fake = SimpleNamespace(switchRequested=SimpleNamespace(emit=Mock()), changed=SimpleNamespace(emit=Mock()))
    monkeypatch.setattr(mod, "_load_get_bridge", lambda: (lambda: fake))
    return mod, fake


class TestRequestSwitchCarriesSession:
    def test_emit_has_session_id(self, tools):
        """_request_switch 必须把 session_id 一起 emit（信号两参）。"""
        mod, fake = tools
        mod._request_switch("D:/wt", "s-abc")
        fake.switchRequested.emit.assert_called_once_with(os.path.abspath("D:/wt"), "s-abc")


class TestActionSwitchCarriesCtxSession:
    @pytest.fixture()
    def repo_with_worktree(self, tmp_path):
        """真 git 主仓库 + 一个登记工作树（行为化跑通 _action_switch 的校验链）。"""
        root = tmp_path / "repo"
        root.mkdir()
        def git(*args):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        git("init", "-b", "main")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (root / "a.txt").write_text("x", encoding="utf-8")
        git("add", "a.txt")
        git("commit", "-m", "init")
        wt = tmp_path / "repo-wt"
        git("worktree", "add", "-b", "feat-x", str(wt))
        return root, wt

    def test_switch_passes_session_id(self, tools, repo_with_worktree):
        mod, fake = tools
        root, wt = repo_with_worktree
        ctx = {"workdir": str(root), "session_id": "s-42"}
        result = mod._worktree_manage_impl(ctx, action="switch", path=str(wt))
        assert result.success, getattr(result, "error", "")
        args = fake.switchRequested.emit.call_args[0]
        assert args == (os.path.abspath(str(wt)), "s-42")

    def test_create_passes_session_id(self, tools, repo_with_worktree):
        mod, fake = tools
        root, _wt = repo_with_worktree
        ctx = {"workdir": str(root), "session_id": "s-7"}
        result = mod._worktree_manage_impl(ctx, action="create", branch_name="t/br", description="测试")
        assert result.success, getattr(result, "error", "")
        args = fake.switchRequested.emit.call_args[0]
        created = root / ".worktrees" / "t-br"
        assert args == (os.path.abspath(str(created)), "s-7")

    def test_switch_noop_emits_changed(self, tools, repo_with_worktree):
        """已在该工作树时也要通知变更：主线程靠这个时机清分支缓存、校准 chip。

        直接返回会让同一目录内切过分支的旧标签永远得不到修正。
        """
        mod, fake = tools
        _root, wt = repo_with_worktree
        ctx = {"workdir": str(wt), "session_id": "s-1"}
        result = mod._worktree_manage_impl(ctx, action="switch", path=str(wt))
        assert result.success, getattr(result, "error", "")
        fake.changed.emit.assert_called_once()
        fake.switchRequested.emit.assert_not_called()


class TestReceiverRoutesBySession:
    """主线程接收端：按 session_id 定位窗口，禁止回落活跃窗口。"""

    @pytest.fixture()
    def ui_module(self):
        assert (_UI / "__init__.py").exists()
        if str(_UI) not in sys.path:
            sys.path.insert(0, str(_UI))
        name = "ui_plugin_worktree_manager"
        spec = importlib.util.spec_from_file_location(name, _UI / "__init__.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        # _resolve_tab_manager / _SwitchReceiver 定义在 service 子模块，
        # __init__ 只是转发导入：patch 必须打在 service 模块本体上
        return sys.modules["ui_plugin_worktree_manager.service"]

    def test_routes_to_session_window(self, ui_module, monkeypatch):
        win1 = SimpleNamespace(_window_id="w1", _current_session_id="s1")
        win2 = SimpleNamespace(_window_id="w2", _current_session_id="s2")
        tm = SimpleNamespace(_windows=[win1, win2])
        monkeypatch.setattr(ui_module, "_resolve_tab_manager", lambda: tm)
        svc = Mock()
        ui_module._SwitchReceiver(svc).on_switch_requested("D:/wt", "s2")
        svc.switch_to_worktree.assert_called_once_with(win2, "D:/wt")

    def test_unknown_session_aborts(self, ui_module, monkeypatch):
        """会话定位不到窗口（如用户已切走/子智能体）→ 放弃切换，不碰活跃窗口。"""
        tm = SimpleNamespace(_windows=[SimpleNamespace(_window_id="w1", _current_session_id="s1")])
        monkeypatch.setattr(ui_module, "_resolve_tab_manager", lambda: tm)
        svc = Mock()
        ui_module._SwitchReceiver(svc).on_switch_requested("D:/wt", "s-other")
        svc.switch_to_worktree.assert_not_called()

    def test_empty_session_aborts(self, ui_module, monkeypatch):
        tm = SimpleNamespace(_windows=[SimpleNamespace(_window_id="w1", _current_session_id="s1")])
        monkeypatch.setattr(ui_module, "_resolve_tab_manager", lambda: tm)
        svc = Mock()
        ui_module._SwitchReceiver(svc).on_switch_requested("D:/wt", "")
        svc.switch_to_worktree.assert_not_called()

    def test_bridge_signal_two_params(self):
        """行为断言：单参 emit 必须失败，双参通过——单参信号是本次缺陷根因之一。"""
        import importlib.util as _imp
        p = _UI / "change_bridge.py"
        spec = _imp.spec_from_file_location("_wt_bridge_t", p)
        mod = _imp.module_from_spec(spec)
        spec.loader.exec_module(mod)
        bridge = mod.WorktreeChangeBridge()
        with pytest.raises(TypeError):
            bridge.switchRequested.emit("only-path")
        bridge.switchRequested.emit("path", "session")  # 不抛即通过
