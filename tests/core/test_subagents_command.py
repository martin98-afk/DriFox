# -*- coding: utf-8 -*-
"""Subagents 命令降级回归测试。

覆盖范围：
1. `handle_command`（system-subagent 插件）必须对 `--create=` 抛
   `CommandNeedDegrade("subagents", ...)` 降级到 prompt 注入（与 /team --create 一致），
   而非落入无参数兜底分支弹 InfoBar
2. `plugins/system-subagent/commands/subagents.md` 必须声明 `--create=` 参数与对应
   prompt_sections 段（注入侧完备，select_prompt 能匹配到 section:create）

设计说明：
- 实现已从 main_widget 迁到插件（handler 走全局 FunctionCommandHandlers），
  本测试随之指向插件实现，防止收口时被无意回退
- 使用 AST 静态校验源码（与 test_team_template.py 的 TestLoadMissingDegradation 模式一致）
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def _handler_src() -> tuple[Path, str]:
    src_path = PROJECT_ROOT / "plugins" / "system-subagent" / "ui" / "command_handler.py"
    return src_path, src_path.read_text(encoding="utf-8")

def _find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None

class TestSubagentsCreateDegradation:
    """`--create=` 必须抛 CommandNeedDegrade("subagents", ...)：

    由 `_execute_command` 捕获 → select_prompt 匹配 `<!-- section:create -->` 段 →
    写 `_pending_command` → `inject_command_prompt` hook 注入 → LLM 生成智能体 md 文件。
    若落入无参数兜底分支，用户只会看到「暂无运行中的子智能体」而拿不到创建提示词。
    """

    def test_handle_command_create_raises_command_need_degrade(self):
        """`handle_command` 必须对 `--create` 抛 CommandNeedDegrade('subagents', ...)。"""
        src_path, src = _handler_src()
        tree = ast.parse(src)

        target = _find_function(tree, "handle_command")
        assert target is not None, "未找到 handle_command 方法"

        func_src = textwrap.dedent(ast.unparse(target))

        assert "CommandNeedDegrade" in func_src, "handle_command 必须抛 CommandNeedDegrade 异常"
        # 实现用 COMMAND_NAME 常量（= "subagents"），两种写法都接受
        degrade_m = re.search(
            r"raise\s+CommandNeedDegrade\(\s*(?:['\"]subagents['\"]|COMMAND_NAME)\s*,", func_src
        )
        assert degrade_m is not None, "必须抛 CommandNeedDegrade('subagents', ...)"
        assert re.search(r"^COMMAND_NAME\s*=\s*['\"]subagents['\"]", src, re.MULTILINE), (
            "模块常量 COMMAND_NAME 必须等于 subagents"
        )
        # 降级分支必须位于无参数兜底（展开运行面板）之前
        fallback_pos = func_src.index("_handle_show_panel(")
        assert degrade_m.start() < fallback_pos, "--create 降级分支必须位于无参数兜底分支之前"

    def test_subagents_md_defines_create_prompt_section(self):
        """`subagents.md` 必须声明 `--create=` 参数及 prompt_sections 映射（注入侧完备）。"""
        md_path = PROJECT_ROOT / "plugins" / "system-subagent" / "commands" / "subagents.md"
        md = md_path.read_text(encoding="utf-8")

        assert re.search(r"\[--create=\]", md), "subagents.md 必须声明 [--create=] 参数"
        assert re.search(r"prompt_sections:\s*\n\s*--create=:\s*['\"]?create", md), (
            "subagents.md 必须定义 prompt_sections --create= → create 段"
        )
        assert "section:create" in md, "subagents.md 必须包含 <!-- section:create --> 提示词模板段"
