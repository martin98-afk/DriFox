# -*- coding: utf-8 -*-
"""回归：/team --load 开新团队必须重置 team_workdir 为源标签页工作目录。

根因：team_workdir 与 team_project 同为 team.json 顶层持久化字段，
用户切换工作目录 / git worktree 时由 set_team_workdir 写入。
_handle_team_load 开新团队时只重置了 team_project（commit fddca28e），
遗漏了 team_workdir，导致下次开新团队沿用上次构建残留的旧工作目录，
新成员窗口左上角分支标签显示旧工作目录（其他项目 / worktree）的分支，
而非当前团队项目。

修复：在 _handle_team_load 重置 team_project 处对称重置 team_workdir 为
源标签页当前工作目录（self._resolve_project_workdir()）。

覆盖：
- 注入点静态校验：_handle_team_load 调用 set_team_workdir 且读取源工作目录
- 行为回归：残留 team_workdir 在 /team --load 时被重置为源标签页 workdir
"""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core import team_manager as tm_mod

_ORIG_GET_INSTANCE = tm_mod.TeamManager.__dict__["get_instance"]
_MAIN_WIDGET = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"


@pytest.fixture
def fresh_tm(tmp_path, monkeypatch):
    """指向 tmp_path 的全新 TeamManager 实例（隔离，不污染真实 ~/.drifox/）。"""
    monkeypatch.setattr(tm_mod.TeamManager, "get_instance", _ORIG_GET_INSTANCE)
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path))
    tm_mod.TeamManager._instance = None
    tm = tm_mod.TeamManager.get_instance()
    yield tm
    tm_mod.TeamManager._instance = None


def _method_calls(method_name: str) -> set:
    """AST 解析 main_widget 指定方法体内的函数调用名集合。"""
    tree = ast.parse(_MAIN_WIDGET.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            calls = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    if isinstance(sub.func, ast.Name):
                        calls.add(sub.func.id)
                    elif isinstance(sub.func, ast.Attribute):
                        calls.add(sub.func.attr)
            return calls
    return set()


def _method_body(method_name: str) -> str:
    """提取 main_widget 中指定方法的源码文本。"""
    text = _MAIN_WIDGET.read_text(encoding="utf-8")
    start = text.find(f"    def {method_name}(")
    assert start >= 0, f"main_widget 缺少方法 {method_name}"
    body_end = len(text)
    for probe in ("\n    def ", "\n    class "):
        idx = text.find(probe, start + 10)
        if idx >= 0:
            body_end = min(body_end, idx)
    return text[start:body_end]


class TestTeamWorkdirResetOnLoad:
    """回归：/team --load 开新团队须重置 team_workdir，避免沿用残留旧工作目录。"""

    def test_team_load_calls_set_team_workdir(self):
        """注入点静态校验：_handle_team_load 必须调用 set_team_workdir。

        注：本测试经 AST 解析 main_widget 源码文本，属静态耦合，无行为变更的
        良性重构（抽取 helper / 改名 / 调缩进）可能误报失败；与端到端行为测试
        test_residual_team_workdir_reset_on_load 互补，重构时若失败需同步更新。
        """
        calls = _method_calls("_handle_team_load")
        assert "set_team_workdir" in calls, "_handle_team_load 未重置团队工作目录（team_workdir 残留根因）"

    def test_team_load_reads_source_workdir(self):
        """注入点静态校验：set_team_workdir 的参数来自源标签页 _resolve_project_workdir。

        注：同上，解析 main_widget 源码文本，属静态耦合；与端到端行为测试互补，
        重构时若失败需同步更新。
        """
        body = _method_body("_handle_team_load")
        assert "set_team_workdir" in body, "应重置团队工作目录"
        assert "_resolve_project_workdir" in body, "应读取源标签页工作目录作为团队工作目录"

    def test_residual_team_workdir_reset_on_load(self, fresh_tm):
        """端到端回归：残留 team_workdir 在 /team --load 时被重置为源标签页 workdir。

        #5a-fix Plan C：项目按 run_id 粒度存储（projects_by_run_id[run_id]），
        不再写顶层 project 字段。残留的旧 team_project 仍保留，但本次新团队
        按 new_run_id 写入新值——断言改为按 run_id 读取。
        """
        # 模拟上次团队遗留的旧工作目录（例如曾切换过的 git worktree）
        fresh_tm.set_team_workdir("D:/stale/old-worktree")
        assert fresh_tm.get_team_workdir() == "D:/stale/old-worktree"
        fresh_tm.set_team_project("旧项目")
        assert fresh_tm.get_team_project() == "旧项目"

        # 构造发起构建的源标签页轻量实例
        from app.main_widget import OpenAIChatToolWindow

        win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
        win._is_destroyed = False
        win._current_project = "项目A"
        src_workdir = "D:/work/DriFox"
        win._resolve_project_workdir = lambda: src_workdir
        win._get_team_manager = lambda: fresh_tm
        win._spawn_team_members = Mock()  # 阻断真实窗口创建，仅验证 team.json 重置
        win.window = Mock()

        # mock 团队模板加载（仅 build 一个角色）
        template = SimpleNamespace(
            template_name="demo",
            description="d",
            agents=[SimpleNamespace(agent_name="build", description="b")],
        )
        template.validate_agent_names = Mock(return_value=[])
        fake_tm = Mock()
        fake_tm.get_instance = Mock(return_value=fake_tm)
        fake_tm.load = Mock(return_value=template)

        # mock 确认弹窗：exec_ 时触发 confirmed 回调（用户点确认）
        captured = {}

        def fake_confirm(*a, **k):
            d = Mock()
            d.confirmed = Mock()

            def _connect(cb):
                captured["on_confirm"] = cb

            d.confirmed.connect = _connect
            d.exec = lambda: captured["on_confirm"]()
            return d

        with (
            patch("app.core.team.template_manager.TemplateManager", fake_tm),
            patch("app.widgets.common_dialogs.ConfirmDialog", fake_confirm),
        ):
            win.backend = SimpleNamespace(agent_manager=Mock(list_agents=Mock(return_value=[])))
            win._handle_team_load("demo")

        # 残留工作目录被重置为源标签页工作目录（run_id 粒度：workdirs_by_run_id）。
        # 🐛 workdir 已迁移为 run_id 粒度存储（与 projects_by_run_id 对齐），
        # 顶层 workdir 单槽废弃——新团队按 new_run_id 写入，不再读顶层残留值
        # （顶层单槽是"标签页工作路径与当前项目不匹配"bug 的污染源）。
        team_data = fresh_tm._read_json(fresh_tm._team_file(fresh_tm.DEFAULT_TEAM))
        wd_mapping = team_data.get("workdirs_by_run_id") or {}
        assert len(wd_mapping) >= 1, "新团队应按 run_id 写入 workdirs_by_run_id"
        assert src_workdir in wd_mapping.values(), "新团队应继承源标签页工作目录，而非沿用上次构建残留的旧工作目录"
        # 旧团队残留顶层 workdir 不应被新团队写入污染（隔离保护）
        assert team_data.get("workdir") == "D:/stale/old-worktree", "旧团队残留顶层 workdir 字段不应丢失"
        # #5a-fix Plan C：新团队按 run_id 写入 projects_by_run_id[run_id]，
        # 旧 set_team_project("旧项目") 写顶层 project 字段保持不变（隔离）。
        # 通过 mock 拦截到的 start_team_run 返回值找到新 run_id，按 run_id 读取新项目。
        # fake_tm 是 Mock，start_team_run 默认返回 MagicMock；为端到端验证，这里
        # 退化为通过 team.json 顶层 projects_by_run_id 字段长度判断（写入动作已发生）
        # + 新项目确实落地（用临时 new_run_id 探测）。
        team_data = fresh_tm._read_json(fresh_tm._team_file(fresh_tm.DEFAULT_TEAM))
        mapping = team_data.get("projects_by_run_id") or {}
        # 新 run_id（force=True 生成的 uuid4 hex）应写入 projects_by_run_id
        assert len(mapping) >= 1, "新团队应按 run_id 写入 projects_by_run_id"
        new_run_id, new_project = next(iter(mapping.items()))
        assert new_project == "项目A", f"新团队项目应为源标签页项目，实际={new_project}"
        # 旧团队残留项目不应丢失（隔离保护）
        assert fresh_tm.get_team_project() == "旧项目", "旧团队残留顶层 project 字段不应丢失"
