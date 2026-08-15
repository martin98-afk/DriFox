# -*- coding: utf-8 -*-
"""
系统工具插件 — 终端与后台任务（平台服务）

bash / bg_*：命令安全拦截、内联脚本重写、Job Object 进程树管理、pty 会话
是平台基础设施，impl 通过 tool_ctx["services"]["terminal"] 调用能力接口。
"""
from app.tools.result import ToolResult

GROUP_TERMINAL = "终端与后台"

_BASH_SCHEMA = {
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
}


def _bash_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("terminal")
    if service is None:
        return ToolResult(False, error="终端服务不可用")
    return service.execute_bash(kwargs.get("command", ""), kwargs.get("timeout", 120))


_BG_START_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bg_start",
        "description": "后台启动命令，不阻塞对话。用于持续服务(如开发服务器)。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "cwd": {"type": "string", "description": "工作目录（可选，默认为项目根目录）"},
            },
            "required": ["command"],
        },
    },
}


def _bg_start_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("terminal")
    if service is None:
        return ToolResult(False, error="终端服务不可用")
    return service.bg_start(kwargs.get("command", ""), kwargs.get("cwd"))


_BG_STOP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bg_stop",
        "description": "停止后台任务",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务ID，格式bg_xxxxxxxx"},
            },
            "required": ["task_id"],
        },
    },
}


def _bg_stop_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("terminal")
    if service is None:
        return ToolResult(False, error="终端服务不可用")
    return service.bg_stop(kwargs.get("task_id", ""))


_BG_LOGS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bg_logs",
        "description": "获取后台任务日志",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "lines": {"type": "integer", "description": "返回最近N行(默认100)"},
            },
            "required": ["task_id"],
        },
    },
}


def _bg_logs_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("terminal")
    if service is None:
        return ToolResult(False, error="终端服务不可用")
    return service.bg_logs(kwargs.get("task_id", ""), kwargs.get("lines", 100))


_BG_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bg_list",
        "description": "列出所有后台任务状态",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _bg_list_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("terminal")
    if service is None:
        return ToolResult(False, error="终端服务不可用")
    return service.bg_list()


def register(registry):
    registry.register(
        "bash", _BASH_SCHEMA, impl=_bash_impl,
        danger="dangerous", icon="shell", cn_name="执行命令",
        group=GROUP_TERMINAL, description="执行shell命令",
        aliases=["Bash", "Terminal", "RunCommand", "execute_command", "shell", "Command"],
    )
    registry.register(
        "bg_start", _BG_START_SCHEMA, impl=_bg_start_impl,
        danger="dangerous", icon="shell", cn_name="后台启动",
        group=GROUP_TERMINAL, description="启动后台命令",
        aliases=["BgStart", "bg_start"],
    )
    registry.register(
        "bg_stop", _BG_STOP_SCHEMA, impl=_bg_stop_impl,
        danger="dangerous", icon="shell", cn_name="后台停止",
        group=GROUP_TERMINAL, description="停止后台任务",
        aliases=["BgStop", "bg_stop"],
    )
    registry.register(
        "bg_logs", _BG_LOGS_SCHEMA, impl=_bg_logs_impl,
        danger="safe", icon="shell", cn_name="后台日志",
        group=GROUP_TERMINAL, description="查看后台任务日志",
        aliases=["BgLogs", "bg_logs"],
    )
    registry.register(
        "bg_list", _BG_LIST_SCHEMA, impl=_bg_list_impl,
        danger="safe", icon="shell", cn_name="后台列表",
        group=GROUP_TERMINAL, description="列出后台任务状态",
        aliases=["BgList", "bg_list"],
    )
