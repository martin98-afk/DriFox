# -*- coding: utf-8 -*-
"""
内置工具集 - 深度重构：自动聚合工具模块，消除手动委托

This module provides a dynamic tool registry that automatically discovers
and aggregates tools from separate tool modules, eliminating the need
for manual method forwarding in a shallow facade.
"""

from pathlib import Path
from typing import Dict, List, Any
import re
import difflib
import hashlib

from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger

# Import all tool modules
from app.tools.diagnostics_tools import DiagnosticsTools
from app.tools.file_tools import FileTools
from app.tools.mcp_tools import MCPClientManager
from app.tools.result import ToolResult
from app.tools.task_tools import TaskTools
from app.tools.terminal_tools import TerminalTools
from app.tools.web_tools import WebTools


# 导入 hashline 核心函数
from app.tools.file_tools import (
    _line_hash, _parse_anchor, _format_hashline, _clean_hashline_from_lines,
)

class BuiltinTools(QObject):
    """
    Builtin tools registry - automatically aggregates methods from tool modules.
    
    This is a deep module: it handles dynamic dispatch to registered tools,
    manages session state, and emits file change events without requiring
    manual method forwarding for every tool method.
    """

    fileModified = pyqtSignal(str)

    def __init__(self, homepage=None, workdir: str = None):
        super().__init__(homepage)
        self.homepage = homepage

        if workdir:
            self.workdir = Path(workdir)
        else:
            try:
                from app.utils.utils import resource_path
                self.workdir = Path(resource_path("/"))
            except Exception:
                self.workdir = Path.cwd()

        # Initialize all tool instances
        self._tools: Dict[str, Any] = {}
        self._register_tools()

        # Session-scoped state
        self._todo_list = []
        self._loaded_skills = {}
        self._skill_workspaces = {}
        
        # MCP 客户端管理器（全局单例，多窗口共享连接）
        self._mcp_manager = MCPClientManager.get_instance()
        self._mcp_manager.acquire()
        
        # Dependencies injected later
        self._sub_agent_manager = None
        self._agent_manager = None
        self._set_stage_callback = None
        self._memory_manager = None
        self._get_llm_config = None
        self._get_session_messages = None
        self._current_project = "默认项目"  # 当前项目（由 set_current_project() 设置）



        logger.info(f"[BuiltinTools] Workdir: {self.workdir}, loaded {len(self._tools)} tool modules")

    def _register_tools(self):
        """Register all tool modules - add new tools here"""
        # 传入 self（BuiltinTools 实例），各工具通过 workdir 属性动态获取最新 workdir
        file_tools = FileTools(self)
        self._tools['file'] = file_tools
        self._tools['web'] = WebTools(self)
        self._tools['terminal'] = TerminalTools(self)
        self._tools['task'] = TaskTools(self)
        self._tools['diagnostics'] = DiagnosticsTools(self)

        # Expose properties for backward compatibility
        self._file_tools = file_tools
        self._web_tools = self._tools['web']
        self._terminal_tools = self._tools['terminal']
        self._task_tools = self._tools['task']
        self._diagnostics_tools = self._tools['diagnostics']

    @property
    def file_tools(self):
        return self._file_tools

    @property
    def web_tools(self):
        return self._web_tools

    @property
    def terminal_tools(self):
        return self._terminal_tools

    @property
    def task_tools(self):
        return self._task_tools

    @property
    def diagnostics_tools(self):
        return self._diagnostics_tools

    @property
    def mcp_manager(self):
        return self._mcp_manager

    def __getattr__(self, name: str):
        """
        Dynamic dispatch: look for method on tool modules.
        
        This eliminates the need for manual method forwarding.
        If a method isn't found on this class, it searches all
        registered tool modules and dispatches to the first match.
        """
        # Search all tool modules for the method
        for tool in self._tools.values():
            if hasattr(tool, name):
                method = getattr(tool, name)
                
                # Wrap the method to handle fileModified emission after write operations
                if name in ['write_file', 'edit_file']:
                    def wrapped_method(*args, **kwargs):
                        result = method(*args, **kwargs)
                        if isinstance(result, ToolResult) and result.success:
                            # Get path from first argument
                            path = args[0] if args else kwargs.get('path')
                            if path:
                                resolved_path = self._file_tools._resolve_path(path)
                                logger.info(
                                    f"[BuiltinTools] {name} success, emitting fileModified: {resolved_path}"
                                )
                                self.fileModified.emit(str(resolved_path))
                        return result
                    return wrapped_method
                
                return method
        
        # If not found, raise AttributeError (Python default)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    # The following methods have special handling (additional logic)
    # so they are kept here instead of dynamic dispatch

    def get_todos(self):
        """获取待办事项列表（返回副本，防止外部直接修改内部状态）"""
        return list(self._task_tools._todo_list)

    def todo_write(self, todos: List[Dict]):
        result = self._task_tools.todo_write(todos)
        self._todo_list = list(self._task_tools._todo_list)
        return result

    def todo_clear(self):
        self._task_tools.todo_clear()
        self._todo_list = []

    def reset_session_state(self):
        """Reset session-scoped state when switching sessions"""
        self._todo_list = []
        self._task_tools.reset_session_state()

    def cleanup(self):
        """
        彻底清理 BuiltinTools 的所有缓存，防止内存泄漏。
        应该在对话结束后或切换会话时调用。
        """
        # 清理待办事项
        self._todo_list = []
        if hasattr(self._task_tools, 'cleanup'):
            self._task_tools.cleanup()

        # 清理加载的技能
        self._loaded_skills = {}
        self._skill_workspaces = {}

        # 清理子智能体管理器
        self._sub_agent_manager = None

        # 清理文件工具的缓存
        if hasattr(self._file_tools, 'cleanup'):
            self._file_tools.cleanup()

        # 释放 MCP 引用（引用计数归零时才真正断开）
        self._mcp_manager.release()

    def summarize_changes(self, text: str = "", limit: int = 1200) -> ToolResult:
        text = (text or "").strip()
        if not text:
            return ToolResult(False, error="No text provided for summarization")

        clean_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if clean_lines and clean_lines[-1] == stripped:
                continue
            clean_lines.append(stripped)

        summary = "\n".join(clean_lines)
        if len(summary) > limit:
            head = summary[: int(limit * 0.75)].rstrip()
            tail = summary[-int(limit * 0.15):].lstrip()
            summary = f"{head}\n\n[... 已省略 {len(summary) - len(head) - len(tail)} 个字符 ...]\n\n{tail}"
        return ToolResult(True, content=summary)

    def set_memory_manager(self, memory_manager):
        self._memory_manager = memory_manager

    def set_llm_config_getter(self, getter):
        self._get_llm_config = getter

    def set_session_messages_getter(self, getter):
        self._get_session_messages = getter

    def set_agent_manager(self, agent_manager):
        """设置 AgentManager 实例，用于动态生成工具 schema"""
        self._agent_manager = agent_manager
        # 同时设置给 task_tools
        if hasattr(self._task_tools, '_agent_manager'):
            self._task_tools._agent_manager = agent_manager

    def set_current_project(self, project: str):
        """设置当前项目（供更新项目笔记时使用）"""
        self._current_project = project

    def set_workdir(self, workdir: str):
        """动态更新工作目录（用于 AutoLoop 自定义项目路径）
        
        各工具模块通过 workdir 属性动态获取最新值，无需逐个传播。
        """
        from pathlib import Path
        self.workdir = Path(workdir)
        logger.info(f"[BuiltinTools] Workdir updated to: {self.workdir}")

    def _build_hashline_rejection(self, mismatches, all_lines):
        """构建 hashline 拒绝信息（与 file_tools._build_hashline_rejection 一致）。"""
        lines = []
        noun = "anchor does" if len(mismatches) == 1 else "anchors do"
        lines.append(f"Edit rejected: {len(mismatches)} {noun} not match the current file.")
        lines.append("The edit was NOT applied. Please re-read the file and issue another edit tool-call.")
        lines.append("")

        mismatch_lines = {m["line"] for m in mismatches}
        show_lines = set()
        for m in mismatches:
            show_lines.add(m["line"])
            for offset in range(1, 3):
                if m["line"] - offset >= 1:
                    show_lines.add(m["line"] - offset)
                if m["line"] + offset <= len(all_lines):
                    show_lines.add(m["line"] + offset)

        prev = -1
        for ln in sorted(show_lines):
            if prev != -1 and ln > prev + 1:
                lines.append("...")
            prev = ln
            content = all_lines[ln - 1].rstrip('\n')
            h = _line_hash(content)
            marker = "**" if ln in mismatch_lines else "  "
            lines.append(f"{marker}{ln}{h}|{content}")

        return ToolResult(False, error="\n".join(lines))

    def edit_project_note(
        self,
        operations: List[Dict],
    ) -> ToolResult:
        if not self._memory_manager:
            return ToolResult(False, error="Memory manager not available")

        project = getattr(self, '_current_project', '默认项目') or '默认项目'
        note = self._memory_manager.get_project_note(project)
        old_text = note.get("content", "") if note else ""

        if not old_text:
            return ToolResult(False, error="项目笔记为空，无法编辑")

        all_lines = [l + '\n' for l in old_text.splitlines()]
        if not all_lines:
            all_lines = ['\n']
        if all_lines and not all_lines[-1].endswith('\n'):
            all_lines[-1] = all_lines[-1] + '\n'

        _VALID_OP_TYPES = frozenset({"replace", "insert_after", "insert_before", "delete"})

        # ── 第一遍：严格校验所有锚点，不自动修正 ──
        resolved_ops = []

        for idx, op in enumerate(operations):
            if not isinstance(op, dict):
                return ToolResult(
                    False,
                    error=f"编辑拒绝：操作 {idx} 不是有效对象（类型为 {type(op).__name__}）。每个操作需要 'anchor' 和 'op' 字段。"
                )

            op_type = op.get("op", "replace")
            if op_type not in _VALID_OP_TYPES:
                return ToolResult(
                    False,
                    error=f"编辑拒绝：未知操作类型 '{op_type}'（索引 {idx}）。有效类型：{', '.join(sorted(_VALID_OP_TYPES))}"
                )

            anchor_str = op.get("anchor", "")
            try:
                orig_line, expected_hash = _parse_anchor(anchor_str)
            except ValueError as e:
                return ToolResult(False, error=str(e))

            if not (1 <= orig_line <= len(all_lines)):
                return ToolResult(
                    False,
                    error=f"Edit rejected: line {orig_line} does not exist "
                          f"(file has {len(all_lines)} lines). "
                          f"Please re-read the file to get current anchors."
                )

            actual_content = all_lines[orig_line - 1].rstrip('\n')
            actual_hash = _line_hash(actual_content)
            if actual_hash != expected_hash:
                return self._build_hashline_rejection(
                    [{"line": orig_line, "expected": expected_hash, "actual": actual_hash}],
                    all_lines
                )

            actual_line = orig_line

            anchor_end_str = op.get("anchor_end")
            actual_end_line = actual_line
            if anchor_end_str:
                try:
                    orig_end_line, end_hash = _parse_anchor(anchor_end_str)
                except ValueError as e:
                    return ToolResult(False, error=str(e))

                if not (1 <= orig_end_line <= len(all_lines)):
                    return ToolResult(
                        False,
                        error=f"Edit rejected: end line {orig_end_line} does not exist "
                              f"(file has {len(all_lines)} lines). "
                              f"Please re-read the file to get current anchors."
                    )

                end_content = all_lines[orig_end_line - 1].rstrip('\n')
                end_actual_hash = _line_hash(end_content)
                if end_actual_hash != end_hash:
                    return self._build_hashline_rejection(
                        [{"line": orig_end_line, "expected": end_hash, "actual": end_actual_hash}],
                        all_lines
                    )

                actual_end_line = orig_end_line
                if actual_end_line < actual_line:
                    return ToolResult(False, error=f"End line {actual_end_line} is before start line {actual_line}")

            resolved_ops.append((actual_line, actual_end_line, op, idx))

        # ── 清洗 operations 中 lines 的 hashline 前缀 ──
        for _, _, op, _ in resolved_ops:
            if "lines" in op and op["lines"]:
                op["lines"] = _clean_hashline_from_lines(op["lines"])

        # ── 第二遍：应用操作（从底部向上，同锚点按原顺序倒序处理） ──
        sorted_ops = sorted(resolved_ops, key=lambda x: (-x[1], -x[0], -x[3]))
        new_lines = list(all_lines)
        applied_count = 0

        for actual_line, actual_end_line, op, _ in sorted_ops:
            op_type = op.get("op", "replace")
            anchor_end = op.get("anchor_end")
            if op_type == "replace":
                if anchor_end:
                    insert_lines = [l + ("\n" if not l.endswith("\n") else "") for l in op.get("lines", [])]
                    new_lines[actual_line - 1:actual_end_line] = insert_lines
                else:
                    insert_lines = op.get("lines", [])
                    if insert_lines:
                        first = insert_lines[0]
                        new_lines[actual_line - 1] = first + ("\n" if not first.endswith("\n") else "")
                        if len(insert_lines) > 1:
                            extra = [l + ("\n" if not l.endswith("\n") else "") for l in insert_lines[1:]]
                            new_lines[actual_line:actual_line] = extra
                    else:
                        del new_lines[actual_line - 1]
            elif op_type == "delete":
                if anchor_end:
                    del new_lines[actual_line - 1:actual_end_line]
                else:
                    del new_lines[actual_line - 1]
            elif op_type == "insert_after":
                insert_lines = [l + ("\n" if not l.endswith("\n") else "") for l in op.get("lines", [])]
                new_lines[actual_line:actual_line] = insert_lines
            elif op_type == "insert_before":
                insert_lines = [l + ("\n" if not l.endswith("\n") else "") for l in op.get("lines", [])]
                new_lines[actual_line - 1:actual_line - 1] = insert_lines
            else:
                return ToolResult(False, error=f"未知的操作类型: {op_type}")
            applied_count += 1

        new_text = "".join(new_lines).rstrip('\n')
        success = self._memory_manager.save_project_note(project, new_text)
        if not success:
            return ToolResult(False, error="保存项目笔记失败")

        diff_lines = list(difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile="project_note", tofile="project_note", lineterm=''
        ))
        diff_str = "\n".join(diff_lines) if diff_lines else ""

        first_changed = None
        last_changed = None
        _HUNK_RE = re.compile(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')
        for line in diff_lines:
            m = _HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                end_line = start + count - 1
                if first_changed is None or start < first_changed:
                    first_changed = start
                if last_changed is None or end_line > last_changed:
                    last_changed = end_line

        result_parts = [f"Applied {applied_count} hashline edit(s) to project note."]
        if first_changed is not None and last_changed is not None:
            anchor_start = max(1, first_changed - 2)
            anchor_end_num = min(len(new_text.splitlines()), last_changed + 2)
            new_lines_list = [l + '\n' for l in new_text.splitlines()]
            if new_lines_list:
                anchor_slice = new_lines_list[anchor_start - 1:anchor_end_num]
                anchors = _format_hashline(anchor_slice, anchor_start)
                result_parts.append("")
                result_parts.append(f"--- Anchors {anchor_start}-{anchor_end_num} ---")
                result_parts.append(anchors)

        return ToolResult(
            True,
            content="\n".join(result_parts),
            diff=diff_str,
        )

    def read_project_note(
        self,
        offset: int = 1,
        limit: int = 500,
    ) -> ToolResult:
        """
        读取当前项目笔记内容，返回 hashline 格式（每行标注 LINE:HASH|content）。
        与普通 read 工具用法一致，每行带有 2-字符内容哈希锚点，
        编辑时通过 edit_project_note 的 LINE:HASH 定位，无需精确匹配旧文本。
        
        **使用时机**：
        需要查看或引用当前项目笔记内容时使用。
        内容很长时可以通过 offset/limit 分页读取。
        
        Args:
            offset: 起始行号（从 1 开始），默认 1
            limit: 读取行数，默认 500
        """
        if not self._memory_manager:
            return ToolResult(False, error="Memory manager not available")
        
        project = getattr(self, '_current_project', '默认项目') or '默认项目'
        note = self._memory_manager.get_project_note(project)
        full_content = note.get("content", "") if note else ""
        
        if not full_content:
            full_content = "(项目笔记为空)"
        
        lines = full_content.splitlines()
        total_lines = len(lines)
        if offset < 1:
            offset = 1
        
        start = offset - 1
        end = min(total_lines, start + limit) if limit > 0 else total_lines
        selected_lines = lines[start:end]
        hashline_content = _format_hashline(selected_lines, start + 1)
        
        return ToolResult(True, content={
            "project": project,
            "content": hashline_content,
            "total_lines": total_lines,
            "offset": offset,
            "limit": limit if limit > 0 else total_lines,
            "returned_lines": len(selected_lines),
            "total_length": len(full_content),
        })


def create_builtin_tools(homepage=None, workdir: str = None) -> BuiltinTools:
    """创建内置工具实例"""
    return BuiltinTools(homepage, workdir)


# Tool schema definitions - keep separate from class
# Each tool module can provide its own schema in the future
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "读取文件内容，返回 hashline 格式（每行标注 LINE+HASH|content）。每行带有 2-字符 BPE bigram 内容哈希锚点，编辑时通过 LINE+HASH 定位，无需精确匹配旧文本。如果哈希不匹配编辑会被拒绝，需要重新读取文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径"},
                    "offset": {
                        "type": "integer",
                        "description": "起始行号 (从1开始)",
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "读取的行数",
                        "default": 500,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "创建新文件或覆盖现有文件,会自动创建不存在的目录，避免一次性写入超大型文件，超大型文件采用多次工具编辑实现。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径"},
                    "content": {"type": "string", "description": "完整的文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "通过 hashline LINE+HASH 锚点编辑文件。使用 read 工具返回的 LINE+HASH 标记（如 12sr）作为锚点定位编辑位置，无需精确匹配旧文本。哈希不匹配时编辑会被拒绝，需重新读取文件。支持多种操作类型。批量操作从文件底部向上执行以避免行号偏移。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "operations": {
                        "type": "array",
                        "description": "编辑操作列表。支持的操作类型：\n- replace: 替换单行或范围 (anchor + 可选 anchor_end + lines)\n- insert_after: 在锚点后插入 (anchor + lines)\n- insert_before: 在锚点前插入 (anchor + lines)\n- delete: 删除单行或范围 (anchor + 可选 anchor_end)\n\n每条操作的 anchor 格式为 read 返回的 LINE+HASH，如 '12sr'。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "description": "操作类型: replace, insert_after, insert_before, delete",
                                    "enum": ["replace", "insert_after", "insert_before", "delete"]
                                },
                                "anchor": {
                                    "type": "string",
                                    "description": "LINE+HASH 锚点，如 '12sr'"
                                },
                                "anchor_end": {
                                    "type": "string",
                                    "description": "范围操作的结束锚点，如 '15th'（可选）"
                                },
                                "lines": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "新内容行列表（delete 操作不需要）"
                                }
                            },
                            "required": ["op", "anchor"]
                        }
                    }
                },
                "required": ["path", "operations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "在指定目录下递归搜索匹配正则表达式的内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {
                        "type": "string",
                        "description": "起始搜索目录 (默认当前目录)",
                        "default": ".",
                    },
                    "include": {
                        "type": "string",
                        "description": "文件过滤模式 (如 '*.py')",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list",
            "description": "列出目录下的文件和文件夹。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径",
                        "default": ".",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "通过通配符模式递归查找文件，支持 **, *, ? 等glob语法。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "文件匹配模式 (如 '*.py', '**/*.json', 'src/**/*.ts')"
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索起始路径 (默认当前目录)",
                        "default": ".",
                    },
                },
                "required": ["pattern"],
            },
        },
    },


    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行shell命令",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "命令"},
                    "timeout": {"type": "integer", "description": "超时秒数"},
                },
                "required": ["command"],
            },
        },
    },
    # 后台任务管理工具
    {
        "type": "function",
        "function": {
            "name": "bg_start",
            "description": "启动后台命令，不阻塞当前对话。用于启动需要持续运行的服务（如开发服务器）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "cwd": {"type": "string", "description": "工作目录（可选，默认为项目根目录）"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bg_stop",
            "description": "停止指定的后台任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID，格式为 bg_xxxxxxxx"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bg_logs",
            "description": "获取后台任务的输出日志",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID"},
                    "lines": {"type": "integer", "description": "返回最近 N 行（默认 100）"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bg_list",
            "description": "列出所有后台任务的状态",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diagnostics",
            "description": "获取文件的语法检查结果（错误、警告、提示）。支持 Python (pyright/mypy/flake8)、JavaScript/TypeScript (tsc/eslint)、Shell (shellcheck)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                    "language": {
                        "type": "string",
                        "description": "语言类型，可选: python, javascript, typescript, shellscript",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "webfetch",
            "description": "获取网页内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "网页URL"},
                    "format": {
                        "type": "string",
                        "description": "返回格式, 支持:html, text, markdown",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "websearch",
            "description": "网络搜索",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "num_results": {"type": "integer", "description": "结果数量"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_repo",
            "description": "扫描仓库目录并返回结构化摘要，适合编码任务前快速建模上下文",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "扫描路径"},
                    "max_depth": {"type": "integer", "description": "最大扫描深度"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stage_files",
            "description": "标记当前任务相关文件，帮助后续聚焦编辑和验证",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "文件路径列表",
                    },
                },
                "required": ["files"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_project_note",
            "description": "通过 hashline LINE+HASH 锚点编辑项目笔记。使用 read_project_note 返回的 LINE+HASH 标记作为锚点定位编辑位置，无需精确匹配旧文本。哈希不匹配时编辑会被拒绝，需重新读取。支持多种操作类型。批量操作从文件底部向上执行以避免行号偏移。",
             "parameters": {
                 "type": "object",
                 "properties": {
                     "operations": {
                         "type": "array",
                         "description": "编辑操作列表。支持的操作类型：\n- replace: 替换单行或范围 (anchor + 可选 anchor_end + lines)\n- insert_after: 在锚点后插入 (anchor + lines)\n- insert_before: 在锚点前插入 (anchor + lines)\n- delete: 删除单行或范围 (anchor + 可选 anchor_end)\n\n每条操作的 anchor 格式为 read_project_note 返回的 LINE+HASH，如 '12sr'。",
                         "items": {
                             "type": "object",
                             "properties": {
                                 "op": {
                                     "type": "string",
                                     "description": "操作类型: replace, insert_after, insert_before, delete",
                                     "enum": ["replace", "insert_after", "insert_before", "delete"]
                                 },
                                 "anchor": {
                                     "type": "string",
                                     "description": "LINE+HASH 锚点，如 '12sr'"
                                 },
                                 "anchor_end": {
                                     "type": "string",
                                     "description": "范围操作的结束锚点，如 '15th'（可选）"
                                },
                                "lines": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "新内容行列表（delete 操作不需要）"
                                }
                            },
                            "required": ["op", "anchor"]
                        }
                    }
                },
                "required": ["operations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_project_note",
            "description": "读取当前项目笔记内容，返回 hashline 格式（每行标注 LINE+HASH|content）。需要查看或引用当前项目笔记内容时使用。内容很长时可以通过 offset/limit 分页读取，和普通 read 工具用法一致。",
            "parameters": {
                "type": "object",
                "properties": {
                    "offset": {"type": "integer", "description": "起始行号（从 1 开始），默认 1"},
                    "limit": {"type": "integer", "description": "读取行数，默认 500"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todowrite",
            "description": "创建和更新待办事项列表",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "待办列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "序号"},
                                "content": {"type": "string", "description": "待办事项内容"},
                                "status": {"type": "string", "description": "状态: pending/in_progress/completed"},
                                "priority": {"type": "string", "description": "优先级: high/medium/low"},
                            },
                            "required": ["content"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todoread",
            "description": "读取待办事项列表",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_batch",
            "description": "",  # filled dynamically below
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "",  # filled dynamically below
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {
                                    "type": "string",
                                    "description": "子智能体名称。",
                                },
                                "description": {"type": "string", "description": "任务描述"},
                                "context": {"type": "string", "description": "详细上下文信息（可选）"},
                            },
                            "required": ["agent", "description"],
                        },
                    },
                    "share_context": {
                        "type": "boolean",
                        "description": "是否共享主智能体上下文给子智能体（默认 True）。启用后子智能体将获得主智能体的完整上下文信息。",
                        "default": True,
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_status",
            "description": "查询子智能体任务状态。task_ids 不传时只能查一次刚完成的任务；指定 task_id 始终能查到。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "description": "任务ID列表（不传则查刚完成的任务，只能查一次）",
                        "items": {"type": "string"},
                    },
                    "with_log": {
                        "type": "boolean",
                        "description": "是否包含执行日志（默认 False）",
                    },
                    "with_result": {
                        "type": "boolean",
                        "description": "是否包含执行结果（默认 True）",
                    },
                }
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": "【强制触发】当用户消息中出现 `@技能名` 格式时，必须立即调用此工具加载技能，**不得以任何理由延迟或绕过**。禁止：先理解上下文、先做其他事、先问问题。正确流程：检测到 @技能 → 立即调用 skill 工具 → 按技能指引执行。\n\nArgs:\n    name: 技能名称（如 brainstorming, tdd, debugging 等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能名称，如 brainstorming, tdd, find-skills, git-commit 等",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "列出所有可用的本地技能，包括内置技能和用户安装的技能。当需要了解有哪些技能可用时可调用此工具。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "question",
            "description": "向用户提问并获取回答。当需要了解用户偏好、需求或让用户做选择时，**必须**使用此工具，不要自行生成问卷或选项。\n\n## 使用原则\n1. **优先使用选项而非文本输入**: 总是通过 options 参数提供选项列表，让用户直接点击选择。选项可以是功能点、技术方案、确认操作等。\n2. **每个问题独立提问**: 当有多个独立问题时，应分多次调用 question 工具，每次只问一个核心问题。避免在一个 question 调用中塞入多个问题要求用户手动输入文本。\n3. **优先多次选择**: 如果需要用户做多个决定或确认多个点，使用多次 question 调用（每次设置 multiple=true 或提供选项），让用户通过选择完成，而非让他们自行组织文本回复。\n\n## 参数说明\n- question: 问题内容，尽量简洁\n- options: 选项列表（**推荐始终提供**），每个选项应该是完整的、可直接选择的\n- multiple: 是否允许多选",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "问题内容及描述，尽量简洁，不要包含选项内容"},
                    "options": {"type": "array",
                                "description": "选项列表。当有多个可选方案或需要用户确认时，**必须提供选项列表**，不要留空让用户文本输入。"},
                    "multiple": {
                        "type": "boolean",
                        "description": "是否允许多选，默认false",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_list_servers",
            "description": "列出所有 MCP 服务器的连接状态和可用工具。当需要了解当前有哪些 MCP 服务器可用时可调用此工具。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def get_builtin_tools_schema(agent_manager=None, builtin_tools=None) -> List[Dict]:
    """获取内置工具的 schema 定义（用于给 LLM 调用）

    Args:
        agent_manager: AgentManager 实例，用于动态注入可用子智能体列表
        builtin_tools: BuiltinTools 实例，用于动态注入 MCP 工具 schema
    """
    # 动态获取子智能体名称列表
    subagent_names = []
    if agent_manager and hasattr(agent_manager, 'list_subagent_names'):
        try:
            subagent_names = agent_manager.list_subagent_names(include_hidden=True)
        except Exception:
            pass
    
    # Make a copy to avoid modifying the original
    schemas = [s.copy() for s in TOOL_SCHEMAS]
    
    # 动态生成 task_batch 工具描述
    task_batch_desc = (
        f"批量分发多个子智能体任务（并行执行）。无需等待子智能体结果，任务完成后系统会自动发送 `[后台任务状态]` 消息通知，发布完任务后可以继续自身任务。"
        f"收到通知后使用 task_status 获取结果。"
    )
    if subagent_names:
        subagent_list = ", ".join(subagent_names)
        task_batch_desc += f"\n\n可用子智能体: {subagent_list}"
    
    # Update the task_batch schema with dynamic content
    for schema in schemas:
        if schema['function']['name'] == 'task_batch':
            schema['function']['description'] = task_batch_desc
            if subagent_names:
                schema['function']['parameters']['properties']['tasks']['items']['properties']['agent'][
                    'description'
                ] += f" (可选：{', '.join(subagent_names)})"
            break
    
    # 动态注入 MCP 工具 schema
    if builtin_tools and hasattr(builtin_tools, '_mcp_manager'):
        mcp_schemas = builtin_tools._mcp_manager.get_tool_schemas()
        if mcp_schemas:
            schemas.extend(mcp_schemas)
            logger.info(f"[BuiltinTools] 注入 {len(mcp_schemas)} 个 MCP 工具 schema")
    
    return schemas
