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
