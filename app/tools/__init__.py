# -*- coding: utf-8 -*-
"""
内置工具集 - 深度重构：自动聚合工具模块，消除手动委托

This module provides a dynamic tool registry that automatically discovers
and aggregates tools from separate tool modules, eliminating the need
for manual method forwarding in a shallow facade.
"""

import difflib
from pathlib import Path
from typing import Dict, List, Any

from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger

from app.tools.automation import AutomationTools
# Import all tool modules
from app.tools.diagnostics_tools import DiagnosticsTools
from app.tools.file_tools import FileTools
from app.tools.mcp_tools import MCPClientManager
from app.tools.result import ToolResult
from app.tools.task_tools import TaskTools
from app.tools.terminal_tools import TerminalTools
from app.tools.web_tools import WebTools


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

        logger.info(
            f"[BuiltinTools] Workdir: {self.workdir}, loaded {len(self._tools)} tool modules"
        )

    def _register_tools(self):
        """Register all tool modules - add new tools here"""
        # 传入 self（BuiltinTools 实例），各工具通过 workdir 属性动态获取最新 workdir
        file_tools = FileTools(self)
        self._tools["file"] = file_tools
        self._tools["web"] = WebTools(self)
        self._tools["terminal"] = TerminalTools(self)
        self._tools["task"] = TaskTools(self)
        self._tools["diagnostics"] = DiagnosticsTools(self)
        self._tools["automation"] = AutomationTools(self)

        # Expose properties for backward compatibility
        self._file_tools = file_tools
        self._web_tools = self._tools["web"]
        self._terminal_tools = self._tools["terminal"]
        self._task_tools = self._tools["task"]
        self._diagnostics_tools = self._tools["diagnostics"]
        self._automation_tools = self._tools["automation"]

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
                if name in ["write_file", "edit_file", "multi_edit"]:

                    def wrapped_method(*args, **kwargs):
                        result = method(*args, **kwargs)
                        if isinstance(result, ToolResult) and result.success:
                            # Get path from first argument
                            path = args[0] if args else kwargs.get("path")
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
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )

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
        if hasattr(self._task_tools, "cleanup"):
            self._task_tools.cleanup()

        # 清理加载的技能
        self._loaded_skills = {}
        self._skill_workspaces = {}

        # 清理子智能体管理器
        self._sub_agent_manager = None

        # 清理文件工具的缓存
        if hasattr(self._file_tools, "cleanup"):
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
            tail = summary[-int(limit * 0.15) :].lstrip()
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
        if hasattr(self._task_tools, "_agent_manager"):
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

    def edit_project_note(
        self,
        oldString: str,
        newString: str,
    ) -> ToolResult:
        if not self._memory_manager:
            return ToolResult(False, error="Memory manager not available")

        project = getattr(self, "_current_project", "默认项目") or "默认项目"
        workdir = str(self.workdir) if self.workdir else None
        note = self._memory_manager.get_project_note(project, workdir=workdir)
        old_text = note.get("content", "") if note else ""

        if not old_text:
            return ToolResult(False, error="项目笔记为空，无法编辑")

        if oldString not in old_text:
            return ToolResult(
                False,
                error="The specified 'oldString' was not found in the project note. Ensure exact match.",
            )

        new_text = old_text.replace(oldString, newString, 1)
        success = self._memory_manager.save_project_note(project, new_text, workdir=workdir)
        if not success:
            return ToolResult(False, error="保存项目笔记失败")

        diff_lines = list(
            difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile="project_note",
                tofile="project_note",
                lineterm="",
            )
        )
        diff_str = "\n".join(diff_lines) if diff_lines else ""

        return ToolResult(
            True,
            content="Successfully edited project note.",
            diff=diff_str,
        )

    def read_project_note(
        self,
        offset: int = 1,
        limit: int = 500,
    ) -> ToolResult:
        """
        读取当前项目笔记内容。

        Args:
            offset: 起始行号（从 1 开始），默认 1
            limit: 读取行数，默认 500
        """
        if not self._memory_manager:
            return ToolResult(False, error="Memory manager not available")

        project = getattr(self, "_current_project", "默认项目") or "默认项目"
        workdir = str(self.workdir) if self.workdir else None
        note = self._memory_manager.get_project_note(project, workdir=workdir)
        full_content = note.get("content", "") if note else ""

        if not full_content:
            full_content = "(项目笔记为空)"

        line_list = full_content.splitlines()
        total_lines = len(line_list)
        if offset < 1:
            offset = 1

        start = offset - 1
        end = min(total_lines, start + limit) if limit > 0 else total_lines
        selected_lines = line_list[start:end]

        return ToolResult(
            True,
            content={
                "project": project,
                "content": "\n".join(selected_lines),
                "total_lines": total_lines,
                "offset": offset,
                "limit": limit if limit > 0 else total_lines,
                "returned_lines": len(selected_lines),
                "total_length": len(full_content),
            },
        )


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
            "description": "读取文件内容。返回原文，可选带行号。支持文本和图片文件（.png/.jpg/.jpeg/.gif/.webp/.bmp）。读取图片时返回 base64 编码数据，由系统自动处理。读取时记录文件修改时间，用于后续编辑时检测外部修改。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
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
                    "show_line_numbers": {
                        "type": "boolean",
                        "description": "是否显示行号，默认 False",
                        "default": False,
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
            "description": "精确文本替换编辑。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "oldString": {
                        "type": "string",
                        "description": "要替换的旧文本（精确匹配，包含空白字符）",
                    },
                    "newString": {"type": "string", "description": "替换后的新文本"},
                    "replaceAll": {
                        "type": "boolean",
                        "description": "是否替换所有匹配项（默认 False，只替换第一个）。当 oldString 出现多次时需设置为 True",
                        "default": False,
                    },
                },
                "required": ["path", "oldString", "newString"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": "批量编辑同一文件，支持多次 oldString/newString 替换。所有替换完成后生成 unified diff 用于审查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "edits": {
                        "type": "array",
                        "description": '编辑操作列表，每项为 {"oldString": "...", "newString": "..."}，按顺序逐条执行替换（仅替换第一个匹配项）',
                        "items": {
                            "type": "object",
                            "properties": {
                                "oldString": {
                                    "type": "string",
                                    "description": "要替换的旧文本",
                                },
                                "newString": {
                                    "type": "string",
                                    "description": "替换后的新文本",
                                },
                            },
                            "required": ["oldString", "newString"],
                        },
                    },
                },
                "required": ["path", "edits"],
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
                        "description": "文件匹配模式 (如 '*.py', '**/*.json', 'src/**/*.ts')",
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
                    "cwd": {
                        "type": "string",
                        "description": "工作目录（可选，默认为项目根目录）",
                    },
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
                    "task_id": {
                        "type": "string",
                        "description": "任务 ID，格式为 bg_xxxxxxxx",
                    },
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
                    "lines": {
                        "type": "integer",
                        "description": "返回最近 N 行（默认 100）",
                    },
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
                    "path": {"type": "string", "description": "文件路径"},
                    "language": {
                        "type": "string",
                        "description": "语言类型，可选: python, javascript, typescript, shellscript",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "截取屏幕并保存为 PNG 文件。支持全屏截图或指定区域截图。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "输出 PNG 文件路径（可选，为空时自动生成到 .drifox/screenshots/ 目录）",
                    },
                    "region": {
                        "type": "array",
                        "description": "截取区域 (left, top, width, height)，如 [100, 200, 800, 600]；为空时截取主显示器全屏",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse",
            "description": "桌面鼠标操作。支持移动、单击、双击、右键、滚动、拖拽、查询当前位置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["move", "click", "double_click", "right_click", "scroll", "drag", "position"],
                        "description": "操作类型: move=移动, click=单击, double_click=双击, right_click=右键, scroll=滚动, drag=拖拽(从当前位置拖到 x,y), position=返回当前鼠标坐标及屏幕尺寸(无需 x,y; content 为 {x,y,screen_width,screen_height})",
                    },
                    "x": {"type": "integer", "description": "目标屏幕 X 坐标（像素）"},
                    "y": {"type": "integer", "description": "目标屏幕 Y 坐标（像素）"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "鼠标按钮（默认 left）",
                    },
                    "clicks": {
                        "type": "integer",
                        "description": "点击次数（默认 1），double_click 固定为 2 次",
                    },
                    "dx": {"type": "integer", "description": "scroll 水平滚动量"},
                    "dy": {"type": "integer", "description": "scroll 垂直滚动量（负值向上，正值向下）"},
                    "duration": {
                        "type": "number",
                        "description": "move/drag 过渡时长（秒）；move 默认 0 瞬移，drag 默认 0.3",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keyboard",
            "description": "桌面键盘操作。支持打字、按单键、组合热键。需先在设置中开启桌面自动化。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["type", "press", "hotkey"],
                        "description": "操作类型: type=输入文本, press=按单键, hotkey=组合热键",
                    },
                    "text": {
                        "type": "string",
                        "description": "type 操作要输入的文本（支持 Unicode）",
                    },
                    "key": {
                        "type": "string",
                        "description": "press 操作的单键名，如 enter, f5, ctrl_l, esc, tab",
                    },
                    "keys": {
                        "type": "string",
                        "description": "hotkey 操作的组合键，用 + 连接，如 ctrl+c, ctrl+shift+n",
                    },
                },
                "required": ["action"],
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
            "description": "精确文本替换编辑项目笔记。编辑前检查文件是否被外部修改。生成 unified diff 用于审查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "oldString": {
                        "type": "string",
                        "description": "要替换的旧文本（精确匹配）",
                    },
                    "newString": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["oldString", "newString"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_project_note",
            "description": "读取当前项目笔记内容。返回原文，可选通过 offset/limit 分页读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "offset": {
                        "type": "integer",
                        "description": "起始行号（从 1 开始），默认 1",
                    },
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
                                "content": {
                                    "type": "string",
                                    "description": "待办事项内容",
                                },
                                "status": {
                                    "type": "string",
                                    "description": "状态: pending/in_progress/completed",
                                },
                                "priority": {
                                    "type": "string",
                                    "description": "优先级: high/medium/low",
                                },
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
            "name": "subagent_para",
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
                                "description": {
                                    "type": "string",
                                    "description": "任务描述",
                                },
                                "context": {
                                    "type": "string",
                                    "description": "详细上下文信息（可选）",
                                },
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
            "name": "subagent_status",
            "description": (
                "查询子智能体任务状态。task_ids 不传时只能查一次刚完成的任务；指定 task_id 始终能查到。\n\n"
                "**重要**：如果任务还在运行中（返回的 status 为 running/finishing 并附带 _hint），"
                "**请勿重复调用本工具等待结果**——任务完成后系统会自动通过 `[后台任务状态]` 用户消息通知，"
                "届时再调用本工具获取详细结果。轮询会导致 LLM 主流程卡死。"
            ),
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
                },
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
            "name": "subagent_dag",
            "description": "批量分发子智能体任务组成 DAG 工作流（有向无环图）。支持节点间依赖关系定义，系统自动按拓扑排序分批并行执行，下游节点自动获取上游节点结果。\n\n【同步执行】调用本工具后会等待所有节点执行完成再返回结果，不需要额外查询。\n\n【依赖解析】系统根据 edges 计算拓扑排序，入度为0的节点并行执行，完成后自动将结果注入下游节点的 context。\n\n【失败处理】如果某个节点执行失败，依赖它的下游节点自动标记为 skipped，不会执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "nodes": {
                        "type": "array",
                        "description": "工作流节点列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "节点唯一标识（如 'step1', 'analyze', 'build'），供 edges 引用",
                                },
                                "agent": {
                                    "type": "string",
                                    "description": "子智能体名称",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "该节点的任务描述",
                                },
                                "context": {
                                    "type": "string",
                                    "description": "额外上下文信息（可选），将追加到自动注入的上游结果之后",
                                },
                            },
                            "required": ["id", "agent", "description"],
                        },
                    },
                    "edges": {
                        "type": "array",
                        "description": "节点间的依赖关系。例如 [{\"from\": \"step1\", \"to\": \"step2\"}] 表示 step2 依赖 step1",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {
                                    "type": "string",
                                    "description": "上游节点 ID",
                                },
                                "to": {
                                    "type": "string",
                                    "description": "下游节点 ID",
                                },
                            },
                            "required": ["from", "to"],
                        },
                    },
                },
                "required": ["nodes", "edges"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "question",
            "description": "向用户提问并获取回答。当需要了解用户偏好、需求或让用户做选择时，**必须**使用此工具。\n\n支持一次性提多个问题，每个问题可以有自己的选项列表。\n\n## 参数说明\n- questions: 问题列表（**必填**），列表里每个元素是一个独立的问题对象\n  - question: 问题内容（字符串，必填）\n  - options: 选项列表（数组，可选）。每个选项是一个对象：\n    - label: 选项标题（字符串，必填）\n    - description: 选项描述（字符串，可选，小字显示在标题下方）\n  - multiple: 是否允许多选（布尔值，可选，默认 false）",
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "description": "问题列表。列表里每个元素是一个独立的问题对象，包含 question（问题内容）、options（选项列表，每个选项有 label+description）、multiple（是否多选）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "问题内容，尽量简洁",
                                },
                                "options": {
                                    "type": "array",
                                    "description": "选项列表（可选）。每个选项包含 label（标题）和 description（描述）,一个问题最多提出4个选项",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {
                                                "type": "string",
                                                "description": "选项标题",
                                            },
                                            "description": {
                                                "type": "string",
                                                "description": "选项描述，小字显示在标题下方",
                                            },
                                        },
                                        "required": ["label"],
                                    },
                                },
                                "multiple": {
                                    "type": "boolean",
                                    "description": "是否允许多选（可选，默认 false）",
                                },
                            },
                            "required": ["question"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_list_servers",
            "description": "列出所有 MCP 服务器的连接状态和可用工具。当需要了解当前有哪些 MCP 服务器可用时可调用此工具。**返回的 tools 字段为完整工具名（含 ``mcp__{server}__`` 前缀），调用时必须原样使用完整名**，不得自行去掉前缀。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_file",
            "description": "将本地文件上传至 Gitee 仓库，返回公开下载链接。适用于需要 LLM 访问本地文件但又无法直接读取的场景（如 gateway 远程调用场景）。**请勿滥用**，仅在确实需要时使用，并注意保护敏感信息，避免上传包含敏感数据的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "local_path": {
                        "type": "string",
                        "description": "本地文件路径（绝对路径或相对 workdir 的路径）",
                    },
                },
                "required": ["local_path"],
            },
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
    if agent_manager and hasattr(agent_manager, "list_subagent_names"):
        try:
            subagent_names = agent_manager.list_subagent_names(include_hidden=True)
        except Exception:
            pass

    # Make a copy to avoid modifying the original
    schemas = [s.copy() for s in TOOL_SCHEMAS]

    # 动态生成 subagent_para 工具描述
    subagent_para_desc = (
        f"批量分发多个子智能体任务（并行执行）。**调用本工具后绝对不能主动停下来等待结果**——子智能体在后台异步运行，任务完成后系统会自动发送 `[后台任务状态]` 消息通知，届时再使用 subagent_status 获取结果。\n\n"
        f"【强制行为】调用 subagent_para 返回后，你必须二选一：\n"
        f"1. 继续调用其他工具执行下一步工作（例如启动其他并行子任务、处理剩余工作）；\n"
        f"2. 如果所有任务已派发完毕、不再有工具可调，直接停止调用工具并输出总结，结束本轮循环。\n\n"
        f"【禁止】只调用一次 subagent_para 后便输出文本结束对话并等待——这是错误行为，会让主流程卡死。**绝对不要**为了让用户「先看到任务派发」而主动停下。"
    )
    if subagent_names:
        subagent_para_desc += (
            "\n\n【可用子智能体】参见系统提示词中的 `## Available Subagents` 节，含完整描述。"
        )

    # Update the subagent_para schema
    for schema in schemas:
        if schema["function"]["name"] == "subagent_para":
            schema["function"]["description"] = subagent_para_desc
            break

    # 更新 subagent_dag 工具描述
    for schema in schemas:
        if schema["function"]["name"] == "subagent_dag":
            if subagent_names:
                schema["function"]["description"] += (
                    "\n\n【可用子智能体】参见系统提示词中的 `## Available Subagents` 节，含完整描述。"
                )
            break

    # 动态注入 MCP 工具 schema
    if builtin_tools and hasattr(builtin_tools, "_mcp_manager"):
        mcp_schemas = builtin_tools._mcp_manager.get_tool_schemas()
        if mcp_schemas:
            schemas.extend(mcp_schemas)
            logger.info(f"[BuiltinTools] 注入 {len(mcp_schemas)} 个 MCP 工具 schema")

    return schemas
