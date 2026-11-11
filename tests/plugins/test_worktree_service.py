# -*- coding: utf-8 -*-
"""WorktreeService 单测：缓存污染 / 竞态写槽 / 降级语义。

背景：分支标签消失缺陷 1、2 的回归锚点。
加载方式与生产 load_plugin 同构（spec_from_file_location + ui 目录插 sys.path）。
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_PLUGIN_UI = Path(__file__).resolve().parents[2] / "plugins" / "worktree-manager" / "ui"
_MODULE_NAME = "ui_plugin_worktree_manager"


@pytest.fixture(scope="module")
def plugin_module():
    """按生产同款机制加载插件模块（相对导入可用），返回 ui/__init__ 模块对象。"""
    assert (_PLUGIN_UI / "__init__.py").exists(), "插件 ui/__init__.py 缺失"
    if str(_PLUGIN_UI) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_UI))
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _PLUGIN_UI / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def svc(plugin_module):
    s = plugin_module.WorktreeService()
    s._branch_cache.clear()
    s._branch_cache_ts.clear()
    return s


def _mw(project="P", workdir="D:/repo"):
    mw = SimpleNamespace(
        _current_project=project,
        _current_workdir={project: workdir},
        _resolve_project_workdir=lambda: workdir,
    )
    mw._branch_widget = SimpleNamespace(
        setVisible=Mock(),
        setText=Mock(),
        setToolTip=Mock(),
        isVisible=Mock(return_value=False),
        text=Mock(return_value=""),
        toolTip=Mock(return_value=""),
        _detect_request_id=1,  # 真实时序：update_branch 发起后自增到 1，回调携带 1
        on_branch_detected=Mock(),  # 真实 BranchChip 的信号槽
    )
    mw._project_avatar = SimpleNamespace(setToolTip=Mock())
    return mw


class TestBranchCache:
    def test_failed_detect_not_cached(self, svc):
        """缺陷1回归：git 失败（ok=False）不得写入缓存。"""
        mw = _mw()
        svc.on_branch_detected(mw, 1, "D:/repo", "", ok=False)
        assert "D:/repo" not in svc._branch_cache
        mw._branch_widget.setText.assert_not_called()

    def test_empty_branch_on_success_is_cached(self, svc):
        """detached HEAD 合法空分支（ok=True）可缓存。"""
        mw = _mw()
        svc.on_branch_detected(mw, 1, "D:/repo", "", ok=True)
        assert svc._branch_cache["D:/repo"] == ""

    def test_result_written_to_task_workdir_slot(self, svc):
        """缺陷2回归：回调用任务携带的 workdir 写缓存，而非当前解析值。"""
        mw = _mw()
        mw._resolve_project_workdir = lambda: "D:/other"  # 检测期间已切走
        svc.on_branch_detected(mw, 1, "D:/repo", "feat-x", ok=True)
        assert svc._branch_cache.get("D:/repo") == "feat-x"
        assert "D:/other" not in svc._branch_cache

    def test_stale_workdir_skips_ui_apply(self, svc):
        """缺陷2回归：任务 workdir ≠ 当前 workdir 时不应用 UI（防串显）。"""
        mw = _mw()
        mw._resolve_project_workdir = lambda: "D:/other"
        svc.on_branch_detected(mw, 1, "D:/repo", "feat-x", ok=True)
        mw._branch_widget.setText.assert_not_called()

    def test_stale_request_id_discards(self, svc):
        """过期 request_id：新任务已发起，旧结果丢弃（不缓存不上屏）。"""
        mw = _mw()
        mw._branch_widget._detect_request_id = 2  # 已发起更新的检测
        svc.on_branch_detected(mw, 1, "D:/repo", "feat-x", ok=True)
        assert "D:/repo" not in svc._branch_cache
        mw._branch_widget.setText.assert_not_called()

    def test_apply_writes_correct_ui_when_current(self, svc):
        """正常路径：当前 workdir 匹配时正常应用 UI。"""
        mw = _mw()
        svc.on_branch_detected(mw, 1, "D:/repo", "dev", ok=True)
        mw._branch_widget.setText.assert_called_once_with("dev")
        mw._branch_widget.setVisible.assert_called_with(True)


class TestSessionKwargs:
    def test_kwargs_shape_main_repo(self, svc):
        """主仓库 → worktree_path 空串（与旧实现语义一致）。"""
        mw = _mw(workdir="D:/definitely-not-a-repo-xyz")
        assert svc.get_session_worktree_kwargs(mw) == {"worktree_path": ""}


class TestFirstWindowRetrofit:
    """首窗补装链路静态校验：build 早于 UI 插件注册 → 延迟加载后必须补装。"""

    def test_deferred_init_calls_install(self):
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "app" / "main_widget.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        bodies = {
            node.name: ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "_install_titlebar_widgets" in bodies, "main_widget 缺少补装方法"
        assert "_install_titlebar_widgets()" in bodies.get("_init_ui_plugins_deferred", ""), (
            "_init_ui_plugins_deferred 未调用补装（首窗分支标签将缺失）"
        )
        install_body = bodies["_install_titlebar_widgets"]
        assert "get_titlebar_widget" in install_body, "补装应从注册表查询 slot"
        assert "_branch_widget" in install_body and "return" in install_body, (
            "补装应幂等（已有 widget 时提前返回）"
        )


class _FakePool:
    """替身线程池：只记录被明星任务，不真跑线程（单元测试不需要真 git）。"""

    def __init__(self, sink):
        self._sink = sink

    def start(self, task):
        self._sink.append(task)


class TestBranchCacheTtl:
    """回归锚：chip 显示与所在目录分支不符。

    缓存键是「工作目录」，同一目录内 git switch/checkout 不换键。没有 TTL 时
    缓存永不失效，chip 会一直显示切走前的分支（与用户实际所在工作树不符）。
    """

    def test_within_ttl_hit_skips_redetect(self, svc, tmp_path):
        """TTL 内仍走缓存（保留去重语义，不重复跑 git）。"""
        wd = str(tmp_path)
        mw = _mw(workdir=wd)
        svc._branch_cache_set(wd, "main")
        rid = mw._branch_widget._detect_request_id
        svc.update_branch(mw)
        assert mw._branch_widget._detect_request_id == rid, "TTL 内不应重新发起检测"
        mw._branch_widget.setText.assert_called_once_with("main")

    def test_expired_cache_triggers_redetect(self, svc, tmp_path, monkeypatch):
        """TTL 过期后必须重新检测，否则同目录切分支后 chip 永久陈旧。"""
        import PySide6.QtCore as QtCore

        wd = str(tmp_path)
        mw = _mw(workdir=wd)
        svc._branch_cache_set(wd, "main")
        svc._branch_cache_ts[wd] -= svc._BRANCH_CACHE_TTL + 1
        started = []
        monkeypatch.setattr(QtCore, "QThreadPool", SimpleNamespace(globalInstance=lambda: _FakePool(started)))
        rid = mw._branch_widget._detect_request_id
        svc.update_branch(mw)
        assert mw._branch_widget._detect_request_id == rid + 1, "过期缓存必须重新发起检测"
        assert len(started) == 1, "应启动一个后台检测任务"

    def test_invalidate_forces_redetect(self, svc, tmp_path, monkeypatch):
        """工具侧写操作后主动失效：不靠 TTL 也能立即重检。"""
        import PySide6.QtCore as QtCore

        wd = str(tmp_path)
        mw = _mw(workdir=wd)
        svc._branch_cache_set(wd, "main")
        svc.invalidate_branch_cache()
        started = []
        monkeypatch.setattr(QtCore, "QThreadPool", SimpleNamespace(globalInstance=lambda: _FakePool(started)))
        rid = mw._branch_widget._detect_request_id
        svc.update_branch(mw)
        assert mw._branch_widget._detect_request_id == rid + 1
        assert len(started) == 1

    def test_same_workdir_refresh_does_not_hide_chip(self, svc, tmp_path):
        """同目录刷新（轮次结束/切会话）不得隐藏 chip：每轮闪一下比陈旧更恼人。"""
        wd = str(tmp_path)
        mw = _mw(workdir=wd)
        svc._branch_cache_set(wd, "main")
        svc._apply_branch_to_ui(mw, wd, "main")
        mw._branch_widget.setVisible.reset_mock()
        svc.update_branch(mw)
        hidden = [c for c in mw._branch_widget.setVisible.call_args_list if c.args == (False,)]
        assert not hidden, "同一目录刷新不应隐藏标签"


class TestSwitchNoopRefresh:
    def test_noop_switch_still_refreshes_chip(self, svc, tmp_path, monkeypatch):
        """已在该工作树时仍刷一次 chip：不然陈旧标签永远等不到修正时机。"""
        from app.plugins.sdk import GitWorktreeDetector

        target = str(tmp_path)
        mw = _mw(workdir=target)
        monkeypatch.setattr(GitWorktreeDetector, "is_valid_worktree_link", staticmethod(lambda p: True))
        refreshed = []
        monkeypatch.setattr(svc, "update_branch", lambda win: refreshed.append(win))
        svc.switch_to_worktree(mw, target)
        assert refreshed == [mw], "幂等短路前必须刷新分支标签"


class TestToolChangeRefresh:
    def test_changed_event_invalidates_and_refreshes_all_windows(self, plugin_module, monkeypatch):
        """工具侧 create/remove/merge 只发 changed，chip 必须借这个时机失效重检。"""
        service_mod = sys.modules["ui_plugin_worktree_manager.service"]
        svc = service_mod.WorktreeService()
        svc._branch_cache_set("D:/repo", "main")
        mw1, mw2 = _mw(), _mw()
        monkeypatch.setattr(
            service_mod, "_resolve_tab_manager", lambda: SimpleNamespace(_windows=[mw1, mw2])
        )
        refreshed = []
        monkeypatch.setattr(svc, "update_branch", lambda win: refreshed.append(win))
        receiver = service_mod._SwitchReceiver(svc)
        receiver.on_worktree_changed("created D:/repo/.worktrees/x")
        assert not svc._branch_cache, "工具侧写操作后分支缓存必须失效"
        assert refreshed == [mw1, mw2], "所有打开窗口都要重检，不只活跃窗口"

