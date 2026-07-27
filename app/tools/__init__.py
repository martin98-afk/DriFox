# -*- coding: utf-8 -*-
"""
内置工具集 - 深度重构：自动聚合工具模块，消除手动委托

This module provides a dynamic tool registry that automatically discovers
and aggregates tools from separate tool modules, eliminating the need
for manual method forwarding in a shallow facade.
"""

import copy
import platform
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QObject

from app.core.lsp.lsp_manager import LspManager, get_lsp_manager
from app.core.lsp.lsp_tools import LspToolsIntegration
from app.tools.automation import AutomationTools

# Import all tool modules
from app.tools.codegraph_tools import CodeGraphTools
from app.tools.diagnostics_tools import DiagnosticsTools
from app.tools.file_tools import FileTools
from app.tools.mcp_tools import MCPClientManager
from app.tools.result import ToolResult
from app.tools.task_tools import TaskTools
from app.tools.team_tools import TeamTools
from app.tools.terminal_tools import TerminalTools
from app.tools.tool_classifier import (
    DANGEROUS_TOOLS,
    SAFE_TOOLS,
    classify_tool_danger,
    get_default_toggles,
    get_tool_counts,
)
from app.tools.web_tools import WebTools


class BuiltinTools(QObject):
    """
    Builtin tools registry - automatically aggregates methods from tool modules.

    This is a deep module: it handles dynamic dispatch to registered tools
    and manages session state, without requiring manual method forwarding
    for every tool method.
    """

    def __init__(self, homepage=None, workdir: str = None):
        super().__init__(homepage)
        self.homepage = homepage

        # 团队上下文（不依赖 homepage，由 backend 设置）
        self._team_window_id: str = ""
        self._team_agent_name: str = ""

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
        self._tools["file"] = file_tools
        self._tools["web"] = WebTools(self)
        self._tools["terminal"] = TerminalTools(self)
        self._tools["task"] = TaskTools(self)
        self._tools["diagnostics"] = DiagnosticsTools(self)
        self._tools["automation"] = AutomationTools(self)

        # LSP 工具集成
        self._lsp_tools = LspToolsIntegration(get_lsp_manager(), owner=self)
        self._tools["lsp"] = self._lsp_tools

        # CodeGraph 代码智能引擎
        self._codegraph_tools = CodeGraphTools(self)
        self._tools["codegraph"] = self._codegraph_tools

        # 团队协作工具
        self._tools["team"] = TeamTools(self)

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
                return method

        # If not found, raise AttributeError (Python default)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    # The following methods have special handling (additional logic)
    # so they are kept here instead of dynamic dispatch

    def set_team_context(self, window_id: str, agent_name: str):
        """设置团队上下文（由 ChatBackend 在初始化/切换 agent 时调用）"""
        self._team_window_id = window_id
        self._team_agent_name = agent_name

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

        # 释放 CodeGraph 实例
        if hasattr(self, "_codegraph_tools"):
            self._codegraph_tools.cleanup()

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
            "description": "读文件。返回原文，可选行号。支持文本/图片(.png/.jpg/.jpeg/.gif/.webp/.bmp)，图片返base64。记录mtime检测外部修改。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "startline": {
                        "type": "integer",
                        "description": "起始行号 (从1开始)",
                        "default": 1,
                    },
                    "endline": {
                        "type": "integer",
                        "description": "结束行号(从1开始)。不传默认 startline+499≈500行",
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
            "description": "创建/覆盖文件。自动建目录。超大文件用多次 edit 写入。",
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
            "description": "精确文本替换。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "oldString": {
                        "type": "string",
                        "description": "旧文本(精确匹配，含空白)",
                    },
                    "newString": {"type": "string", "description": "替换后的新文本"},
                    "replaceAll": {
                        "type": "boolean",
                        "description": "替换全部匹配(默认False)。oldString重复时设True",
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
            "description": "批量编辑同文件。多次替换后生成 unified diff 审查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "edits": {
                        "type": "array",
                        "description": "编辑列表。每项{oldString,newString}，按序替换首个匹配",
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
            "description": "递归搜索正则匹配内容。",
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
            "description": "列目录。",
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
            "description": "通配符递归查找。支持 **, *, ? 等glob。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "匹配模式：如 *.py, **/*.json, src/**/*.ts",
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
            "description": "执行shell命令。仅内置工具不够用时用：构建(pytest/ruff/build)、git(status/diff/log/add/commit)、进程(ps/kill/lsof)、管道(cat|grep|awk)、环境探测(which/env)。禁止替代: read/write/edit/multi_edit/list/glob/grep/get_diagnostics/lsp/bg_*/screenshot/mouse/keyboard/websearch/webfetch。调用前自检：有专用工具？有则用它。",
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
            "description": "后台启动命令，不阻塞对话。用于持续服务(如开发服务器)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
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
            "description": "停止后台任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "任务ID，格式bg_xxxxxxxx",
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
            "description": "获取后台任务日志",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务 ID"},
                    "lines": {
                        "type": "integer",
                        "description": "返回最近N行(默认100)",
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
            "description": "列出所有后台任务状态",
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
            "description": "获取文件语法检查结果(错误/警告/提示)。支持 Python(pyright/mypy/flake8)、JS/TS(tsc/eslint)、Shell(shellcheck)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "language": {
                        "type": "string",
                        "description": "语言: python/javascript/typescript/shellscript",
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
            "description": "截屏并保存PNG。支持全屏或区域截图。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "输出PNG路径(可选，空则自动生成到.drifox/screenshots/)",
                    },
                    "region": {
                        "type": "array",
                        "description": "区域(left,top,width,height)如[100,200,800,600]；空=全屏",
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
            "description": "桌面鼠标操作。支持移动/单击/双击/右键/滚动/拖拽/查位置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["move", "click", "double_click", "right_click", "scroll", "drag", "position"],
                        "description": "move=移动,click=单击,double_click=双击,right_click=右键,scroll=滚动,drag=拖到(x,y),position=查坐标+屏幕尺寸",
                    },
                    "x": {"type": "integer", "description": "目标屏幕 X 坐标（像素）"},
                    "y": {"type": "integer", "description": "目标屏幕 Y 坐标（像素）"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "鼠标按钮(默认left)",
                    },
                    "clicks": {
                        "type": "integer",
                        "description": "点击次数(默认1),double_click固定2次",
                    },
                    "dx": {"type": "integer", "description": "scroll水平滚动"},
                    "dy": {"type": "integer", "description": "scroll垂直滚动(负上正下)"},
                    "duration": {
                        "type": "number",
                        "description": "move/drag过渡秒数；move默认0瞬移，drag默认0.3",
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
            "description": "桌面键盘操作。支持打字/按单键/组合热键。需先开启桌面自动化。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["type", "press", "hotkey"],
                        "description": "type=输入文本,press=按单键,hotkey=组合热键",
                    },
                    "text": {
                        "type": "string",
                        "description": "type 操作要输入的文本（支持 Unicode）",
                    },
                    "key": {
                        "type": "string",
                        "description": "单键名: enter/f5/ctrl_l/esc/tab",
                    },
                    "keys": {
                        "type": "string",
                        "description": "组合键用+连接: ctrl+c,ctrl+shift+n",
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
                        "description": "返回格式: html/text/markdown",
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
                    "query": {"type": "string", "description": "关键词"},
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
            "description": "扫描仓库，返回结构化摘要。编码前快速建模上下文。",
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
            "description": "标记任务相关文件，聚焦后续编辑/验证。",
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
            "name": "todowrite",
            "description": "创建/更新待办事项",
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
                                "content": {"type": "string", "description": "内容"},
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
                                    "description": "上下文(可选)",
                                },
                            },
                            "required": ["agent", "description"],
                        },
                    },
                    "share_context": {
                        "type": "boolean",
                        "description": "共享主智能体上下文给子智能体(默认True)",
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
                "查询子智能体任务状态。task_ids 不传只能查一次刚完成的；指定task_id始终能查。\n\n"
                "**重要**：运行中任务勿重复查——完成后自动发[后台任务状态]通知，届时再查。轮询卡死。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "description": "任务ID列表(不传则查刚完成的，仅一次)",
                        "items": {"type": "string"},
                    },
                    "with_log": {
                        "type": "boolean",
                        "description": "含执行日志(默认False)",
                    },
                    "with_result": {
                        "type": "boolean",
                        "description": "含执行结果(默认True)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": "加载智能体技能",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能名：brainstorming/tdd/find-skills/git-commit等",
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
            "description": "列出所有可用技能(内置+用户安装)。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subagent_dag",
            "description": "子智能体DAG工作流。按拓扑排序分批并行执行，下游自动取上游结果。【同步执行】等全部完成再返回。【失败处理】失败节点→下游自动skipped。",
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
                                    "description": "节点ID(如step1/analyze/build)，供edges引用",
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
                                    "description": "额外上下文(可选)，追加到上游结果之后",
                                },
                            },
                            "required": ["id", "agent", "description"],
                        },
                    },
                    "edges": {
                        "type": "array",
                        "description": "依赖关系。如[{from:step1,to:step2}]表示step2依赖step1",
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
            "description": "向用户提问。了解偏好/需求/选择时**必用**。支持多问题，每问题可带选项列表。参数：questions(必填,数组)，每项含question(string)+options(array,每项label+description)+multiple(bool)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "description": "问题列表。每项含question+options(含label+description)+multiple",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "问题内容，尽量简洁",
                                },
                                "options": {
                                    "type": "array",
                                    "description": "选项列表(可选)。每项含label+description，最多4个",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {
                                                "type": "string",
                                                "description": "选项标题",
                                            },
                                            "description": {
                                                "type": "string",
                                                "description": "选项描述(小字显示在标题下)",
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
            "description": "列出MCP服务器连接状态和可用工具。tools字段含``mcp__{server}__``前缀，调用时用完整名，勿去前缀。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_file",
            "description": "上传文件到Gitee仓库，返回下载链接。用于gateway远程调用场景。**勿滥用**，注意敏感信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "local_path": {
                        "type": "string",
                        "description": "文件路径(绝对/相对workdir)",
                    },
                },
                "required": ["local_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lsp",
            "description": (
                "通过LSP执行代码智能操作。\n"
                "\n"
                "操作类型：\n"
                "- diagnostics: 文件诊断(错误/警告/提示)\n"
                "- documentSymbols: 符号列表(类/函数/变量)\n"
                "- goToDefinition: 跳定义\n"
                "- findReferences: 找引用\n"
                "- hover: 光标位置文档/类型\n"
                "- listServers: 列出LSP服务器状态\n"
                "\n"
                "line/column从1开始。diagnostics/listServers无需line/column。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径（listServers 操作不需要但可忽略）",
                    },
                    "operation": {
                        "type": "string",
                        "enum": [
                            "diagnostics",
                            "documentSymbols",
                            "goToDefinition",
                            "findReferences",
                            "hover",
                            "listServers",
                        ],
                        "description": "操作类型",
                    },
                    "line": {
                        "type": "integer",
                        "description": "行号（diagnostics/listServers 不需要）",
                    },
                    "column": {
                        "type": "integer",
                        "description": "列号（diagnostics/listServers/documentSymbols 不需要）",
                    },
                },
                "required": ["path", "operation"],
            },
        },
    },
    # ── 团队协作工具（精简版）─────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "team_list_members",
            "description": "列出团队中的所有成员（返回 agent_name@window_id 格式的唯一标识符，如 build@win_02）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "team_send_message",
            "description": "向团队中的另一个智能体发送任务邮件（仅团队模式下可用）。对方会串行处理任务，完成后自动回复结果到你的邮箱。",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": "目标成员标识，支持 agent_name（如 build）或 agent_name@window_id（如 build@win_02），通过 team_list_members 查看",
                    },
                    "message": {"type": "string", "description": "消息内容"},
                },
                "required": ["to_agent", "message"],
            },
        },
    },
    # ── CodeGraph 代码智能工具（只读、安全，统一入口）───
    {
        "type": "function",
        "function": {
            "name": "codegraph_explore",
            "description": (
                "统一代码探索工具。通过 mode 切换不同能力：\n"
                "  - status: 查看索引状态（文件/符号/边/待同步变更）\n"
                "  - search: 搜索符号（函数/类/方法/变量），支持按 kind 过滤\n"
                "  - callers: 查找谁调用了指定符号\n"
                "  - callees: 查找指定符号调用了谁\n"
                "  - explore: （默认）综合搜索+调用上下文，一次输出\n"
                "  - impact: 变更影响分析，评估改动波及范围\n"
                "  - sync: 同步索引与文件系统变更\n"
                "  - files: 列出已索引文件\n"
                "\n"
                "新参数（v1.4.0）:\n"
                "  substring=true  — 子串匹配，搜 Manager 也能找到 SessionManager\n"
                "  visibility=private — 只搜 _ 开头的私有符号\n"
                "  case_sensitive=true — 大小写敏感\n"
                "\n"
                "使用示例：\n"
                "  codegraph_explore(mode='status') — 看索引状态\n"
                "  codegraph_explore('ChatBackend') — 探索 ChatBackend\n"
                "  codegraph_explore('Manager', mode='search', substring=true, kind='class') — 搜所有 Manager 类\n"
                "  codegraph_explore('send_message', mode='callers') — 找调用者\n"
                "  codegraph_explore('on_click', mode='impact') — 影响分析"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索的符号名或关键词（status/sync/files 模式不需要）",
                        "default": "",
                    },
                    "mode": {
                        "type": "string",
                        "enum": [
                            "status",
                            "search",
                            "callers",
                            "callees",
                            "explore",
                            "impact",
                            "sync",
                            "files",
                        ],
                        "description": "操作模式（默认 explore）",
                        "default": "explore",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "callers/callees/impact 的遍历深度（默认 2）",
                        "default": 2,
                    },
                    "kind": {
                        "type": "string",
                        "description": "search 模式按类型过滤：function/class/method/variable/field/enum 等",
                    },
                    "max_files": {
                        "type": "integer",
                        "description": "explore 模式最大文件数（默认 50）",
                        "default": 50,
                    },
                    "directory": {
                        "type": "string",
                        "description": "files 模式按目录筛选（如 app/tools）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "search 模式最大返回数（默认 50）",
                        "default": 50,
                    },
                    "exact": {
                        "type": "boolean",
                        "description": "search 模式是否精确匹配（默认模糊）",
                        "default": False,
                    },
                    "substring": {
                        "type": "boolean",
                        "description": "search 模式使用子串匹配（搜 Manager 也可命中 SessionManager）",
                        "default": False,
                    },
                    "visibility": {
                        "type": "string",
                        "enum": ["public", "private"],
                        "description": "search 模式按可见性过滤：public（无 _ 前缀）/private（有 _ 前缀）",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "search 模式是否大小写敏感（默认不敏感）",
                        "default": False,
                    },
                },
                "required": [],
            },
        },
    },
]


# ── 短时缓存 ──────────────────────────────────────────
# get_builtin_tools_schema 在单次对话流程中被多次重复调用（上下文环刷新、
# PreSendWorker、agent 工具获取等），但子智能体列表/MCP/LSP 状态在数秒内不会变化。
# 用 5 秒 TTL 缓存避免重复 deepcopy + 动态注入，同时用 deepcopy 返回防止调用方
# 的 description 改写污染缓存。
_cache_result: Optional[List[Dict]] = None
_cache_timestamp: float = 0.0
_CACHE_TTL = 5.0  # 秒


def get_builtin_tools_schema(agent_manager=None, builtin_tools=None) -> List[Dict]:
    """获取内置工具的 schema 定义（用于给 LLM 调用）

    Args:
        agent_manager: AgentManager 实例，用于动态注入可用子智能体列表
        builtin_tools: BuiltinTools 实例，用于动态注入 MCP 工具 schema
    """
    global _cache_result, _cache_timestamp

    # 短时缓存：避免同一事件循环中多次重复 deepcopy + 动态注入
    now = time.monotonic()
    if _cache_result is not None and now - _cache_timestamp < _CACHE_TTL:
        return copy.deepcopy(_cache_result)

    # 动态获取子智能体名称列表
    subagent_names = []
    if agent_manager and hasattr(agent_manager, "list_subagent_names"):
        try:
            subagent_names = agent_manager.list_subagent_names(include_hidden=True)
        except Exception:
            pass

    # 深拷贝，避免后续对 function.description 的 `+=` 改写污染全局 TOOL_SCHEMAS。
    # ⚠️ 旧实现用 s.copy() 浅拷贝：外层 dict 是新对象，但内层的 function dict 仍与
    # 全局 TOOL_SCHEMAS 共享同一对象。get_builtin_tools_schema 每次被调用（上下文刷新
    # 频繁触发）都会对 subagent_dag / lsp 的 description 做 `+=`，导致全局工具描述被一遍
    # 遍追加，工具定义 token 随对话过程持续增长（用户实测「工具定义 token 数在增加」）。
    schemas = [copy.deepcopy(s) for s in TOOL_SCHEMAS]

    # 动态生成 subagent_para 工具描述
    subagent_para_desc = "批量分发子智能体任务(并行执行)。调完后不可等——继续调其他工具或结束本轮。完成后系统发[后台任务状态]，届时用subagent_status查。"
    if subagent_names:
        subagent_para_desc += "\n\n可用子智能体见系统提示 ## Available Subagents。"

    # Update the subagent_para schema
    for schema in schemas:
        if schema["function"]["name"] == "subagent_para":
            schema["function"]["description"] = subagent_para_desc
            break

    # 更新 subagent_dag 工具描述
    for schema in schemas:
        if schema["function"]["name"] == "subagent_dag":
            if subagent_names:
                schema["function"]["description"] += "\n\n可用子智能体见系统提示 ## Available Subagents。"
            break

    # 动态注入当前平台信息到 bash 工具描述
    current_platform = platform.system()  # Windows / Darwin / Linux
    for schema in schemas:
        if schema["function"]["name"] == "bash":
            schema["function"]["description"] += f"\n\n当前平台: {current_platform}。"
            break

    # 动态注入 MCP 工具 schema
    if builtin_tools and hasattr(builtin_tools, "_mcp_manager"):
        mcp_schemas = builtin_tools._mcp_manager.get_tool_schemas()
        if mcp_schemas:
            schemas.extend(mcp_schemas)
            logger.debug(f"[BuiltinTools] 注入 {len(mcp_schemas)} 个 MCP 工具 schema")

    # 动态注入 LSP 服务器状态到 lsp 工具描述
    try:
        from app.core.lsp.lsp_manager import get_lsp_manager

        lsp_mgr = get_lsp_manager()
        clients = lsp_mgr._clients
        if clients:
            running = [n for n, c in clients.items() if c.is_running]
            status_text = f"已启动LSP: {', '.join(running) if running else '(无)'}。"
            for schema in schemas:
                if schema["function"]["name"] == "lsp":
                    schema["function"]["description"] += f"\n\n{status_text}"
                    break
    except Exception:
        pass

    # 写入缓存
    _cache_result = schemas
    _cache_timestamp = now

    return schemas
