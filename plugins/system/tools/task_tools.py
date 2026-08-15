# -*- coding: utf-8 -*-
"""
系统工具插件 — 任务与待办（自包含实现）

todowrite / todoread：待办状态维护在本模块（进程级模块单例），
ToolResult 携带 todos 字段回传 UI（主程序不读插件状态，UI 从工具结果联动）。
"""
from typing import Dict, List

from app.tools.result import ToolResult

from app.tools.task_state import get_todos, set_todos

GROUP_TODO = "任务与待办"

# 待办状态在 _task_state（下划线前缀，热重载不重置）——跨插件重载保留


def _normalize_todos(todos) -> List[Dict]:
    """归一化待办（对齐 UI 期望的键/值）"""
    normalized: List[Dict] = []
    for item in todos or []:
        if not isinstance(item, dict):
            continue
        lower_item = {str(k).lower(): v for k, v in item.items()}
        status = str(lower_item.get("status", "")).lower()
        priority = str(lower_item.get("priority", "medium")).lower()
        content = lower_item.get("content") or lower_item.get("description") or ""
        normalized.append(
            {
                "id": lower_item.get("id"),
                "content": content,
                "status": status or "pending",
                "priority": priority or "medium",
            }
        )
    return normalized


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
    normalized = _normalize_todos(kwargs.get("todos", []))
    set_todos(normalized)
    return ToolResult(True, content=f"Todo list updated: {len(normalized)} items", todos=normalized)


_TODOREAD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todoread",
        "description": "读取待办事项列表",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _todoread_impl(tool_ctx, **kwargs):
    todo_list = get_todos()
    if not todo_list:
        return ToolResult(True, content="No todos", todos=[])
    lines = []
    for i, todo in enumerate(todo_list, 1):
        status = todo.get("status", "")
        if status == "completed":
            status_icon = "✓"
        elif status == "in_progress":
            status_icon = "▶"
        else:
            status_icon = "○"
        content = todo.get("content", "")
        priority = todo.get("priority", "medium")
        lines.append(f"{i}. [{priority}] {status_icon} {content}")
    return ToolResult(True, content="\n".join(lines), todos=list(todo_list))


def _preview_todoread(tool_args: dict) -> str:
    label = "查看待办事项"
    offset = tool_args.get("offset")
    limit = tool_args.get("limit")
    if offset is not None and limit is not None and offset > 1:
        label += f" (第 {offset}-{offset + limit - 1} 行)"
    elif offset is not None and offset > 1:
        label += f" (从第 {offset} 行)"
    elif limit is not None:
        label += f" (前 {limit} 行)"
    return label


def _preview_todowrite(tool_args: dict) -> str:
    todos = tool_args.get("todos", [])
    count = len(todos) if isinstance(todos, list) else 0
    return "更新待办事项" + (f" ({count}项)" if count else "")


def _summarize_todo(tool_name, tool_args, tool_content):
    """待办工具压缩摘要（从 history_compactor 迁出）"""
    if tool_name == "todowrite":
        return "[todo] updated task list"
    content_len = len(tool_content or "")
    return f"[todoread] read todo list ({content_len:,} chars)"


def register(registry):
    registry.register(
        "todowrite", _TODOWRITE_SCHEMA, impl=_todowrite_impl,
        danger="dangerous", icon="todo", cn_name="更新待办",
        group=GROUP_TODO, description="创建/更新待办",
        aliases=["TodoWrite", "todo_write"],
        preview=_preview_todowrite,
        summarize=_summarize_todo,
        metadata={"protect": True, "ui_managed": True},  # 待办内容压缩时完整保留；UI 专属处理
    )
    registry.register(
        "todoread", _TODOREAD_SCHEMA, impl=_todoread_impl,
        danger="safe", icon="todo", cn_name="查看待办",
        group=GROUP_TODO, description="读取待办列表",
        aliases=["TodoRead", "todo_read"],
        render_mode="inline",
        preview=_preview_todoread,
        metadata={"ui_managed": True},
        summarize=_summarize_todo,
    )
