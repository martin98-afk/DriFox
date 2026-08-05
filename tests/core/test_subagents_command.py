# -*- coding: utf-8 -*-
"""Subagents 命令降级回归测试。

覆盖范围：
1. `_handle_subagents_command` 必须对 `--create=` 抛 `CommandNeedDegrade("subagents", ...)`
   降级到 prompt 注入（与 /team --create 一致），而非落入无参数兜底分支弹 InfoBar
2. `plugins/system/commands/subagents.md` 必须声明 `--create=` 参数与对应
   prompt_sections 段（注入侧完备，select_prompt 能匹配到 section:create）

设计说明：
- 使用 AST 静态校验源码（与 test_team_template.py 的 TestLoadMissingDegradation 模式一致），
  防止修复被无意回退
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _main_widget_src() -> tuple[Path, str]:
    src_path = PROJECT_ROOT / "app" / "main_widget.py"
    return src_path, src_path.read_text(encoding="utf-8")


def _find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


class TestSubagentsCreateDegradation:
    """修复说明：原 `_handle_subagents_command` 对 `--create=` 无处理分支，落入

    "无参数"兜底分支弹 InfoBar（"暂无运行中的子智能体"），LLM 永远收不到创建提示词。
    修复为与 /team --create 一致：`--create=` 抛 `CommandNeedDegrade("subagents", ...)`，
    由 `_execute_command` 捕获 → select_prompt 匹配 `<!-- section:create -->` 段 →
    写 `_pending_command` → `inject_command_prompt` hook 注入 → LLM 生成智能体 md 文件。
    """

    def test_handle_subagents_command_create_raises_command_need_degrade(self):
        """`_handle_subagents_command` 必须对 `--create` 抛 CommandNeedDegrade('subagents', ...)。"""
        src_path, src = _main_widget_src()
        tree = ast.parse(src)

        target = _find_function(tree, "_handle_subagents_command")
        assert target is not None, "未找到 _handle_subagents_command 方法"

        func_src = textwrap.dedent(ast.unparse(target))

        # 必须抛 CommandNeedDegrade（command_name="subagents"）
        assert "CommandNeedDegrade" in func_src, "_handle_subagents_command 必须抛 CommandNeedDegrade 异常"
        assert re.search(r"CommandNeedDegrade\s*\(\s*['\"]subagents['\"]", func_src), (
            "必须抛 CommandNeedDegrade('subagents', ...)"
        )
        # 降级分支必须位于无参数兜底分支（sub_agent_mgr 读取）之前，
        # 否则 --create= 仍会落入兜底弹 InfoBar（docstring 中也有"无参数"字样，不能用文本定位）
        degrade_pos = func_src.index("raise CommandNeedDegrade('subagents', args)")
        fallback_pos = func_src.index("sub_agent_mgr = self.backend.sub_agent_manager")
        assert degrade_pos < fallback_pos, (
            "--create 降级分支必须位于无参数兜底分支之前"
        )

    def test_subagents_md_defines_create_prompt_section(self):
        """`subagents.md` 必须声明 `--create=` 参数及 prompt_sections 映射（注入侧完备）。"""
        md_path = PROJECT_ROOT / "plugins" / "system" / "commands" / "subagents.md"
        md = md_path.read_text(encoding="utf-8")

        assert re.search(r"\[--create=\]", md), "subagents.md 必须声明 [--create=] 参数"
        assert re.search(r"prompt_sections:\s*\n\s*--create=:\s*['\"]?create", md), (
            "subagents.md 必须定义 prompt_sections --create= → create 段"
        )
        assert "section:create" in md, "subagents.md 必须包含 <!-- section:create --> 提示词模板段"
