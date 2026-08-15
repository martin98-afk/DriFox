# -*- coding: utf-8 -*-
"""
系统工具插件 — 任务与待办（平台服务）

todowrite / todoread：待办状态由主程序维护（UI 待办卡片联动读取），
impl 通过 tool_ctx["services"]["todo"] 调用能力接口，工具层逻辑在插件内。
"""
from app.tools.result import ToolResult

GROUP_TODO = "任务与待办"

_TODOWRITE_SCHEMA = {
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
}


def _todowrite_impl(tool_ctx, **kwargs):
    todos = kwargs.get("todos", [])
    service = tool_ctx.get("services", {}).get("todo")
    if service is None:
        return ToolResult(False, error="待办服务不可用")
    return service.todo_write(todos)


_TODOREAD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todoread",
        "description": "读取待办事项列表",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _todoread_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("todo")
    if service is None:
        return ToolResult(False, error="待办服务不可用")
    return service.todo_read()


def register(registry):
    registry.register(
        "todowrite", _TODOWRITE_SCHEMA, impl=_todowrite_impl,
        danger="dangerous", icon="todo", cn_name="更新待办",
        group=GROUP_TODO, description="创建/更新待办",
        aliases=["TodoWrite", "todo_write"],
    )
    registry.register(
        "todoread", _TODOREAD_SCHEMA, impl=_todoread_impl,
        danger="safe", icon="todo", cn_name="查看待办",
        group=GROUP_TODO, description="读取待办列表",
        aliases=["TodoRead", "todo_read"],
    )
