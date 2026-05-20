# -*- coding: utf-8 -*-
"""
AutoLoop Prompt 组合器 — 集中管理所有 prompt 模板
"""
import re
import time
from typing import Optional

from loguru import logger


# ========== 阶段约束常量 ==========

PLANNING_CONSTRAINT = """
🕒 当前系统时间：{current_time}

🔒 【当前阶段强制约束 - 规划阶段】
你现在 **ONLY 只允许** 做任务拆解和方案设计。
**ABSOLUTELY 禁止** 写任何实现代码，禁止使用 edit/bash/delete 工具修改代码文件！

你的任务：
1. 扫描项目理解现状
2. 将任务拆解为步骤，每个步骤格式：
   - [ ] [步骤 N] <描述> | <文件> | <验证方式>
     ✅ 需求验证：<这个步骤必须满足什么需求？输出什么结果？>
3. 将完整计划写入 SHARED_TASK_NOTES.md
4. 输出 PLANNING_COMPLETE
5. STOP！到此为止

记住：你现在只规划，不实现。代码一根都不能写！
""".strip()

EXECUTING_CONSTRAINT = """
🕒 当前系统时间：{current_time}

🔒 【当前阶段强制约束 - 执行阶段】
你现在 **ONLY 只允许** 处理 **当前步骤 {current}/{total}**。
**ABSOLUTELY 禁止** 提前执行后续步骤，禁止一次性做完多个步骤！

你必须严格遵循：
1. 读取 SHARED_TASK_NOTES.md 确认当前步骤要求和需求验证点
2. 只完成当前这一个步骤，不要碰后续步骤
3. 按照步骤要求运行验证（必须真的运行验证命令，不能假设成功）
4. 验证必须通过两层检查：
   ① 基础验证：代码能跑通吗？语法/编译/测试通过吗？
   ② 需求验证：功能真的满足原始需求吗？每个验证点都通过吗？
5. 两层验证都通过后，在 SHARED_TASK_NOTES.md 中将当前步骤改为 `[x]`
6. 在文档末尾**追加**本轮操作记录（包括改动文件、验证命令、验证结果）
7. STOP！到此为止，等待下一轮

约束来源：两阶段强制约束设计 (2026-05-16)
"""


class AutoLoopPromptComposer:
    """集中管理 AutoLoop 的所有 prompt 模板"""

    def __init__(self, engine):
        """
        Args:
            engine: AutoLoopEngine 实例，用于读取状态和笔记
        """
        self._engine = engine

    # ========== 阶段约束 ==========

    def get_stage_constraint(self) -> str:
        """获取当前阶段的强制约束提示"""
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        if self._engine.is_planning_phase():
            return PLANNING_CONSTRAINT.format(current_time=current_time)
        else:
            return EXECUTING_CONSTRAINT.format(
                current_time=current_time,
                current=self._engine.current_step,
                total=self._engine.total_steps,
            )

    # ========== 工作流上下文 ==========

    def build_workflow_context(self, iteration: int, project_path: str = "",
                               force_update: bool = False) -> str:
        """根据当前阶段构建工作流上下文"""
        is_planning = self._engine.is_planning_phase()

        if is_planning:
            lines = self._planning_context()
        else:
            lines = self._executing_context()

        if force_update:
            lines.extend([
                "",
                "## ⚠️ 【强制】接力文档未更新！",
                "",
                "你必须使用 `write` 工具更新 `SHARED_TASK_NOTES.md` 后才能继续。",
                "**不更新接力文档就继续是违规行为！**",
            ])

        if project_path:
            lines.extend([
                "",
                "## Project Root Directory",
                f"`WORKDIR`: {project_path}",
                "所有文件操作使用相对路径：",
                f"  - write(path='src/main.py', ...) → {project_path}/src/main.py",
                f"  - read(path='src/main.py')    → 读取 {project_path}/src/main.py",
            ])

        if not is_planning:
            notes = self._engine.read_shared_notes()
            if notes:
                lines.extend([
                    "",
                    "## 当前 SHARED_TASK_NOTES.md 内容",
                    "```",
                    notes[:2000],
                    "```" if len(notes) <= 2000 else "...[已截断]",
                ])

        return "\n".join(lines)

    def build_forced_update_prompt(self, iteration: int) -> str:
        """生成强制更新接力文档的提示"""
        current_step = self._engine.current_step
        total_steps = self._engine.total_steps
        notes_preview = self._engine.read_shared_notes()[:500] if self._engine else ""

        if self._engine.is_planning_phase():
            return f"""
## ⚠️ 【强制】接力文档未更新！

你（迭代 {iteration} 轮）尚未更新接力文档 `SHARED_TASK_NOTES.md`。

根据规则，你必须：
1. 使用 `write` 工具将完整的执行计划写入 SHARED_TASK_NOTES.md
2. 包含所有步骤的描述、目标文件、验证方式
3. 然后输出 `PLANNING_COMPLETE`

当前接力文档状态：
```
{notes_preview}...
```

请立即使用 `write` 工具更新接力文档，然后输出 `PLANNING_COMPLETE`。
"""
        else:
            completion_signal = self._engine.config.completion_signal
            return f"""
## ⚠️ 【强制】接力文档未更新！

你（迭代 {iteration} 轮）尚未更新接力文档 `SHARED_TASK_NOTES.md`。

根据规则，你必须：
1. 更新 SHARED_TASK_NOTES.md 中的"步骤 {current_step} 结果"章节
2. 记录本轮执行的改动、验证命令和结果
3. 然后才能继续下一步或输出 {completion_signal}

当前接力文档状态：
```
{notes_preview}...
```

请立即使用 `write` 工具更新接力文档（追加步骤结果），然后继续执行。
"""

    # ========== 组合完整消息 ==========

    def build_messages(self, task_prompt: str, iteration: int,
                       system_prompt: str, project_path: str = "",
                       force_update: bool = False) -> list:
        """构建本轮对话消息"""
        workflow_context = self.build_workflow_context(
            iteration, project_path, force_update
        )

        stage_constraint = self.get_stage_constraint()
        if stage_constraint:
            workflow_context = stage_constraint + "\n\n" + workflow_context

        incremental_summary = self._engine.get_incremental_summary()
        if incremental_summary:
            workflow_context = workflow_context + incremental_summary

        messages = [{"role": "system", "content": system_prompt}]
        messages.append({"role": "user", "content": task_prompt + "\n\n" + workflow_context})
        return messages

    # ========== 私有：阶段上下文模板 ==========

    def _planning_context(self) -> list:
        """规划阶段上下文模板"""
        return [
            "## 🚀 PHASE 1: TASK PLANNING",
            "",
            "你正处于**任务规划阶段**。你的职责是将复杂任务拆解为可验证的步骤。",
            "",
            "### 规划流程",
            "1. **扫描项目**: 使用 `scan_repo`/`glob`/`grep` 了解项目结构",
            "2. **拆解任务**: 将任务分为 N 个可验证的子步骤",
            "3. **写入笔记**: 将计划写入 SHARED_TASK_NOTES.md",
            "4. **输出信号**: 在响应末尾输出 `PLANNING_COMPLETE` 表示规划完成",
            "",
            "### 步骤格式（必须严格遵循）",
            "```",
            "[步骤 1] <简短描述> | <目标文件> | <验证方式>",
            "[步骤 2] <简短描述> | <目标文件> | <验证方式>",
            "...",
            "```",
            "",
            "### 验证方式参考",
            "| 类型 | 示例 | 说明 |",
            "|------|------|------|",
            "| 测试 | `测试: pytest tests/` | 运行测试 |",
            "| Lint | `lint: flake8` | 代码检查 |",
            "| 检查 | `检查: 文件包含 xxx` | 内容验证 |",
            "| 运行 | `运行: python main.py` | 命令执行 |",
            "",
            "### SHARED_TASK_NOTES.md 模板",
            "```markdown",
            "# SHARED_TASK_NOTES",
            "",
            "## 任务概述",
            "<一句话描述要完成的目标>",
            "",
            "## 执行计划",
            "- [步骤 1] <描述> | <文件> | <验证方式>",
            "- [步骤 2] <描述> | <文件> | <验证方式>",
            "- [步骤 3] <描述> | <文件> | <验证方式>",
            "",
            "## 当前状态",
            "等待开始执行",
            "",
            "## 下一步",
            "执行步骤 1",
            "```",
            "",
            "### ⚠️ 重要规则",
            "- **不要在规划阶段执行代码改动**！先规划，后执行",
            "- 必须输出 `PLANNING_COMPLETE` 才能进入执行阶段",
            "- 每个步骤必须有明确的验证方式，否则无法确认完成",
            "- `## 任务概述` 和 `## 执行计划` 一旦写入，进入执行阶段后将被锁定保护，**禁止修改**，执行阶段只能更新 `## 当前状态` 和追加步骤结果",
        ]

    def _executing_context(self) -> list:
        """执行阶段上下文模板"""
        current_step = self._engine.current_step
        total_steps = self._engine.total_steps
        notes = self._engine.read_shared_notes()

        lines = [
            "## ⚡ PHASE 2: EXECUTION LOOP",
            "",
            f"**当前进度**: 步骤 {current_step} / {total_steps}",
            "",
            "### 📋 文档保护规则（必须严格遵守！）",
            "",
            "✅ **允许修改**: `## 当前状态` 和 `## 下一步` 以及新增的 `步骤 X 结果` 章节",
            "❌ **禁止修改**: `## 任务概述` 和 `## 执行计划` 这两个章节一旦在规划阶段完成，**绝对不能修改或重写**，必须原样保留！",
            "❌ **禁止简化**: 不得将原详细的多步骤计划简化为少数步骤，必须保留所有原始步骤细节。",
            "",
            "### 执行规则（严格遵守）",
            "",
            "**每轮只做一件事，然后验证**。不要试图一次完成多个步骤。",
            "",
            "### 工作流程",
            "1. 读 `SHARED_TASK_NOTES.md` 确认当前步骤",
            "2. 读取相关目标文件",
            "3. 执行当前步骤（**只做一件事**）",
            "4. **必须运行验证命令**（不能跳过）",
            "5. 更新 `SHARED_TASK_NOTES.md`: **只追加/更新当前步骤结果和当前状态，不得改动执行计划部分**",
            f"6. 判断：继续当前步骤 | 前进到下一步 | 输出 STEP_{current_step}/{total_steps}_COMPLETE",
            "",
            "### 验证失败处理",
            "- 验证失败 → 分析原因 → 修复 → 重试",
            "- 连续失败 3 次 → 记录问题 → 尝试降级方案或跳过",
            "- 验证成功 → 前进到下一步",
            "",
            "### 完成信号",
            f"- **当前步骤完成** → 输出 `STEP_{current_step}/{total_steps}_COMPLETE`（独占一行）",
            f"- **全部步骤完成** → 输出 `{self._engine.config.completion_signal}`（独占一行）",
            "",
            "### 当前步骤详情",
        ]

        if notes:
            pattern = rf'- \[步骤\s*{current_step}\].*?'
            match = re.search(pattern, notes)
            if match:
                step_text = match.group(0)
                lines.append("```")
                lines.append(step_text)
                lines.append("```")
            else:
                lines.append(f"(未找到步骤 {current_step} 信息)")
        else:
            lines.append("(暂无笔记信息，请先读取 SHARED_TASK_NOTES.md)")

        return lines
