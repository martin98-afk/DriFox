# -*- coding: utf-8 -*-
"""新建项目时的流式保护回归测试

背景：流式输出中新建项目（标题栏输入 / 项目选择卡片 / 导入文件夹三入口都走
_on_new_project_created）。修复前该方法直接改当前窗口的 _current_project /
backend._current_project / tool_executor / 工作目录，然后 _create_new_session
内部检测到流式才 spawn_tab 新标签页——新标签页拿到了新项目，但原流式窗口的
项目也被一起切走，流式对话的项目归属与工具写入全部错位。

修复后：与 _on_project_selected 的流式保护对齐，流式中新建项目一律
spawn_tab(project=新项目) 开新标签页，项目上下文只落到新窗口，
当前流式窗口保持旧项目，不强行停流。
"""

import ast
from pathlib import Path
from types import MethodType
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAIN_WIDGET_PATH = REPO_ROOT / "app" / "main_widget.py"


def _get_method_src(method_name: str) -> ast.FunctionDef:
    """读取 main_widget.py 中指定方法的方法体 AST"""
    src = MAIN_WIDGET_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src, filename="main_widget.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OpenAIChatToolWindow":
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == method_name:
                    return stmt
    raise AssertionError(f"未找到方法 OpenAIChatToolWindow.{method_name}")


def _find_streaming_if(method: ast.FunctionDef) -> ast.If:
    """找到方法体内判断 _is_streaming 的 if 分支（返回第一个）"""
    for node in ast.walk(method):
        if isinstance(node, ast.If):
            for sub in ast.walk(node.test):
                if isinstance(sub, ast.Attribute) and sub.attr == "_is_streaming":
                    return node
    return None


def _body_assigns_project(node) -> bool:
    """node 子树内是否存在对 _current_project 的赋值（含 self.backend._current_project）"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                seen = target
                while isinstance(seen, (ast.Attribute, ast.Subscript, ast.Tuple, ast.List)):
                    if isinstance(seen, ast.Attribute) and seen.attr == "_current_project":
                        return True
                    seen = seen.value
    return False


class TestNewProjectStreamingGuardStructure:
    """AST 结构检查：流式保护分支必须存在且先于项目切换"""

    def test_streaming_guard_branch_exists(self):
        """_on_new_project_created 必须包含判断 _is_streaming 的流式保护分支"""
        method = _get_method_src("_on_new_project_created")
        assert _find_streaming_if(method) is not None, (
            "_on_new_project_created 缺少流式保护：流式中新建项目会污染当前对话的项目归属"
        )

    def test_streaming_guard_spawns_tab_with_project(self):
        """流式保护分支内必须 spawn_tab 且携带 project 参数（项目上下文只给新窗口）"""
        method = _get_method_src("_on_new_project_created")
        guard = _find_streaming_if(method)
        assert guard is not None
        found = False
        for node in ast.walk(guard):
            if isinstance(node, ast.Call):
                func = node.func
                is_spawn = (isinstance(func, ast.Name) and func.id == "spawn_tab") or (
                    isinstance(func, ast.Attribute) and func.attr == "spawn_tab"
                )
                if not is_spawn:
                    continue
                if any(kw.arg == "project" for kw in node.keywords):
                    found = True
                    break
        assert found, "流式保护分支内 spawn_tab 必须带 project 关键字参数"

    def test_streaming_guard_never_assigns_current_project(self):
        """流式保护分支内禁止改写 _current_project（含 backend），原窗口项目必须保持"""
        method = _get_method_src("_on_new_project_created")
        guard = _find_streaming_if(method)
        assert guard is not None
        assert not _body_assigns_project(guard), (
            "流式保护分支内不得对 _current_project 赋值：项目切换必须发生在新窗口上"
        )


# ═══════════════════════════════════════════════════════════════
# 行为测试（stub 窗口 + mock TabManagerWindow）
# ═══════════════════════════════════════════════════════════════


def _make_stub(streaming: bool):
    """构造最小窗口 stub，真实执行 _on_new_project_created 的流式保护分支"""
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)

    inst._is_streaming = streaming
    inst._is_destroyed = False
    inst._window_id = "test-window-001"
    inst._current_project = "旧项目"
    inst._current_workdir = {}
    inst.backend = MagicMock()
    inst.backend._current_project = "旧项目"
    inst._project_label = MagicMock()
    inst._refresh_project_branch_style = MagicMock()
    inst._update_branch = MagicMock()
    inst._sync_working_directory = MagicMock()
    inst._history_popup_card = MagicMock()
    inst._create_new_session = MagicMock()
    inst._broadcast_team_project = MagicMock()
    inst._card_manager = MagicMock()
    inst.cfg = MagicMock()
    inst._on_new_project_created = MethodType(OpenAIChatToolWindow._on_new_project_created, inst)
    return inst


def _call_with_tm(stub, project, tm_cls_mock, **kwargs):
    """patch 掉 TabManagerWindow.get_instance 并调用真实方法"""
    fake_tm = MagicMock()
    fake_new = MagicMock()
    fake_new._current_workdir = {}
    fake_tm.spawn_tab.return_value = fake_new
    tm_cls_mock.get_instance.return_value = fake_tm
    stub._on_new_project_created(project, **kwargs)
    return fake_tm, fake_new


class TestNewProjectStreamingBehavior:
    """行为验证：流式中新建项目，项目上下文只落到新窗口"""

    @patch("app.main_widget.TabManagerWindow")
    def test_streaming_spawns_tab_with_new_project(self, tm_cls_mock):
        """流式中新建项目 → spawn_tab(project=新项目)，且携带 new_session=True"""
        stub = _make_stub(streaming=True)
        fake_tm, fake_new = _call_with_tm(stub, "新项目", tm_cls_mock)
        fake_tm.spawn_tab.assert_called_once_with(stub, new_session=True, project="新项目")

    @patch("app.main_widget.TabManagerWindow")
    def test_streaming_keeps_source_window_project(self, tm_cls_mock):
        """流式中新建项目 → 原窗口 _current_project / backend._current_project 保持不变"""
        stub = _make_stub(streaming=True)
        fake_tm, fake_new = _call_with_tm(stub, "新项目", tm_cls_mock)
        assert stub._current_project == "旧项目", "原窗口项目被污染"
        assert stub.backend._current_project == "旧项目", "原窗口 backend 项目被污染"
        stub._project_label.setText.assert_not_called(), "原窗口项目标签不得变更"
        stub._create_new_session.assert_not_called(), "原窗口不得触发新建会话"

    @patch("app.main_widget.TabManagerWindow")
    def test_streaming_registers_workdir_on_new_window(self, tm_cls_mock):
        """流式中新建项目 → 新窗口必须同步工作目录（含临时目录创建 + DB 注册）"""
        stub = _make_stub(streaming=True)
        fake_tm, fake_new = _call_with_tm(stub, "新项目", tm_cls_mock)
        fake_new._sync_working_directory.assert_called_once(), "新窗口未同步工作目录"
        stub._sync_working_directory.assert_not_called(), "原窗口不得同步工作目录"

    @patch("app.main_widget.TabManagerWindow")
    def test_streaming_with_root_dir_binds_on_new_window(self, tm_cls_mock):
        """流式 + 导入文件夹建项目 → 根目录只绑定到新窗口（instance 缓存 + 关键文档）"""
        stub = _make_stub(streaming=True)
        folder = "D:/workspace/proj"
        fake_tm, fake_new = _call_with_tm(stub, "新项目", tm_cls_mock, root_dir=folder, suppress_memory_card=True)
        assert fake_new._current_workdir["新项目"] == folder
        fake_new.backend.memory_manager.add_key_document.assert_called_once_with("新项目", folder, added_by="manual")
        fake_new.backend.memory_manager.set_working_directory.assert_called_once_with("新项目", folder)
        fake_tm.open_workbench_memory.assert_not_called()  # suppress_memory_card=True

    @patch("app.main_widget.TabManagerWindow")
    def test_streaming_broadcasts_team_with_prev_project(self, tm_cls_mock):
        """流式中新建项目 → 团队广播以旧项目为 prev（发送方保持旧项目，接收方校验一致）"""
        stub = _make_stub(streaming=True)
        fake_tm, fake_new = _call_with_tm(stub, "新项目", tm_cls_mock)
        stub._broadcast_team_project.assert_called_once_with("新项目", "旧项目")

    @patch("app.main_widget.TabManagerWindow")
    def test_not_streaming_keeps_original_behavior(self, tm_cls_mock):
        """非流式中新建项目 → 原地切项目（原行为保留），不 spawn_tab"""
        stub = _make_stub(streaming=False)
        fake_tm = MagicMock()
        tm_cls_mock.get_instance.return_value = fake_tm
        stub._on_new_project_created("新项目")
        assert stub._current_project == "新项目", "非流式必须原地切项目"
        fake_tm.spawn_tab.assert_not_called(), "非流式不得开新标签页"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
