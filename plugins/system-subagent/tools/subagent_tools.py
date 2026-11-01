# -*- coding: utf-8 -*-
"""子智能体工具（system-subagent 插件所有物）

两个工具 + 一个结果注入器：

- ``subagent_para``：并行派发子智能体任务。管理器经
  ``tool_ctx["sub_agent_manager"]`` 取（ToolExecutor 从 ``subagent.run`` 能力
  注入的窗口门面，未就绪为 None）。
- ``subagent_status``：查询任务状态（运行中/已完成/插件表落库三种来源）。
- ``_inject_subagent_result``：任务结果注入器，注册进工具 metadata 的
  ``subagent_result_injector``；管理器完成链经 ToolRegistry 发现并调用，
  文案构建与通道选择都归本插件。

从 ``plugins/system-tools/tools/subagent_tools.py`` 迁出：团队协作工具留在
system-tools，子智能体工具随管理器一起归本插件。
"""

import uuid

import orjson
from loguru import logger

from app.plugins.sdk import ToolResult
from app.plugins.sdk import make_summarize_from_preview

GROUP_SUBAGENT = "子智能体"

# ============================================================
# schema
# ============================================================

_SUBAGENT_PARA_SCHEMA = {
    "type": "function",
    "function": {
        "name": "subagent_para",
        "description": "",  # 运行时由 get_builtin_tools_schema 动态填充子智能体列表
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent": {"type": "string", "description": "子智能体名称。"},
                            "description": {"type": "string", "description": "任务描述"},
                            "context": {"type": "string", "description": "上下文(可选)"},
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
}

_SUBAGENT_STATUS_SCHEMA = {
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
                "with_log": {"type": "boolean", "description": "含执行日志(默认False)"},
                "with_result": {"type": "boolean", "description": "含执行结果(默认True)"},
            },
        },
    },
}

# ============================================================
# impl
# ============================================================


def _subagent_para_impl(tool_ctx, **kwargs):
    manager = tool_ctx.get("sub_agent_manager")
    if not manager:
        return ToolResult(False, error="子智能体管理器未初始化")
    tasks_val = kwargs.get("tasks", [])
    tasks = orjson.loads(tasks_val) if isinstance(tasks_val, str) else (tasks_val or [])
    if not tasks:
        return ToolResult(False, error="任务列表为空")
    share_context = kwargs.get("share_context", False)
    session_id = tool_ctx.get("session_id", "")

    task_ids = []
    for task_item in tasks:
        agent = task_item.get("agent", "")
        description = task_item.get("description", "")
        context = task_item.get("context", "")
        if not agent or not description:
            continue
        task_id = str(uuid.uuid4())
        manager.execute_task(
            task_id=task_id,
            agent_name=agent,
            task_description=description,
            parent_context=context or "",
            on_finished=None,
            on_error=None,
            executor_ref=None,
            share_context=share_context,
            session_id=session_id,
        )
        task_ids.append(task_id)

    return ToolResult(
        True,
        content={
            "task_ids": task_ids,
            "status": "running",
            "count": len(task_ids),
        },
    )


def _subagent_status_impl(tool_ctx, **kwargs):
    manager = tool_ctx.get("sub_agent_manager")
    if not manager:
        return ToolResult(False, error="子智能体管理器未初始化")
    task_ids = kwargs.get("task_ids")
    with_log = kwargs.get("with_log", False)
    with_result = kwargs.get("with_result", True)
    session_id = tool_ctx.get("session_id", "")
    if task_ids:
        if isinstance(task_ids, list):
            id_list = [str(tid).strip() for tid in task_ids if tid]
        else:
            id_list = [tid.strip() for tid in str(task_ids).split(",") if tid.strip()]
        return manager.get_tasks_status_with_details(id_list, with_log, with_result, session_id=session_id)
    return manager.get_all_active_tasks_with_details(with_log, with_result, session_id=session_id)


def _preview_subagent_para(tool_args: dict) -> str:
    """分发任务预览：优先展示子任务各自的 description（比"分发 N 个子任务"更有信息量）"""
    tasks = tool_args.get("tasks", [])
    count = len(tasks) if isinstance(tasks, list) else 0
    if not count:
        return "分发子智能体任务"
    descs = []
    for t in tasks:
        if isinstance(t, dict):
            d = str(t.get("description") or "").strip()
            if d:
                descs.append(d)
    if not descs:
        return f"分发 {count} 个子任务"
    if count == 1:
        return descs[0]
    if len(descs) == 2:
        return " / ".join(d[:40] for d in descs)
    return f"{descs[0][:40]} 等 {count} 个子任务"


def _preview_subagent_status(tool_args: dict) -> str:
    task_ids = tool_args.get("task_ids", [])
    if isinstance(task_ids, list) and task_ids:
        return f"查询子智能体状态 ({', '.join(str(t)[:12] for t in task_ids[:3])})"
    return "查询子智能体状态"


def _inject_subagent_result(
    send,
    task_id: str,
    result: str,
    success: bool,
    agent_name: str,
    task_description: str,
    mode: str = "hook",
) -> bool:
    """子智能体结果注入（插件所有物）：构建文案并经 session.send 发送。

    由管理器完成链经 ToolRegistry metadata 发现并调用（send 为 session.send
    裸 callable）。流式中经 hook 通道静默注入当前对话上下文（worker 下一轮
    消费，不打断）；空闲时 session.send 自动降级为普通发送开新轮。
    mode 可选 queue/skip/hook。
    """
    if task_id == "__batch_summary__":
        content = str(result or "")
    else:
        status_icon = "✅" if success else "❌"
        agent_label = f"[{agent_name}]" if agent_name else ""
        MAX_RESULT_CHARS = 4096
        if result and len(result) > MAX_RESULT_CHARS:
            result = (
                result[:MAX_RESULT_CHARS] + "\n\n[... 已省略 " + str(len(result) - MAX_RESULT_CHARS) + " 个字符 ...]"
            )
        else:
            result = result or "(无输出)"
        content = (
            f"子智能体任务完成: {status_icon} {agent_label} {task_description} (id: {task_id})\n执行结果：\n{result}"
        )
    return bool(send(content, mode=mode, hook_event="SubAgentFinished"))


def register(registry):
    registry.register(
        "subagent_para",
        _SUBAGENT_PARA_SCHEMA,
        impl=_subagent_para_impl,
        danger="dangerous",
        icon="设置-subagent",
        cn_name="分发任务",
        group=GROUP_SUBAGENT,
        description="并行启动子智能体",
        aliases=["subagents-para", "subagent-para", "TaskBatch", "Batch", "task", "Task"],
        preview=_preview_subagent_para,
        summarize=make_summarize_from_preview(_preview_subagent_para),
        keep_in_content=True,  # 任务表常驻正文，不迁入工具折叠区
        metadata={
            "subagent_task": True,
            "permission_task": True,
            # 子智能体结果注入器：管理器任务完成时经 ToolRegistry 发现并调用
            "subagent_result_injector": _inject_subagent_result,
        },
    )
    registry.register(
        "subagent_status",
        _SUBAGENT_STATUS_SCHEMA,
        impl=_subagent_status_impl,
        danger="safe",
        icon="设置-subagent",
        cn_name="查询任务状态",
        group=GROUP_SUBAGENT,
        description="查询子智能体状态",
        aliases=["subagent-status", "TaskStatus", "Status"],
        preview=_preview_subagent_status,
        summarize=make_summarize_from_preview(_preview_subagent_status),
    )
    logger.debug("[system-subagent] 子智能体工具已注册")
