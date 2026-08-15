# -*- coding: utf-8 -*-
"""
系统工具插件 — 子智能体与团队协作（自包含实现）

subagent_*：直接使用 tool_ctx["sub_agent_manager"]（ToolExecutor 注入的主程序
子智能体调度器），不依赖主程序服务转发。
team_*：直接调用主程序 TeamManager（插件 import 主程序，正常方向），
窗口团队上下文从 tool_ctx["team_window_id"]/["team_agent_name"] 获取。
团队专用工具注册 team_only=True → 非团队成员从 schema 定义中过滤。
"""
import uuid

import orjson

from app.tools.result import ToolResult
from app.tools.registry import make_summarize_from_preview

GROUP_SUBAGENT = "子智能体"
GROUP_TEAM = "团队协作"


# ============================================================
# 团队服务（内联实现：TeamManager 为主程序平台能力，插件 import 调用）
# ============================================================

_STATUS_LABELS = {
    "busy": "🟡 执行任务中",
    "idle": "🟢 空闲",
    "running": "🟡 执行任务中",
    "pending": "⏳ 等待处理",
}


def _get_team_manager():
    from app.core.team_manager import TeamManager

    return TeamManager.get_instance()


def _format_capability(cap: dict) -> str:
    """格式化能力摘要：如 `写✓/编✓/bash✓ | 标签: implement`。"""
    mark = lambda ok: "✓" if ok else "✗"
    parts = [
        f"写{mark(bool(cap.get('can_write')))}",
        f"bash{mark(bool(cap.get('can_bash')))}",
        # T16：团队成员恒具团队工具 → 恒 ✓（不读 can_team，防御老快照）
        "团队✓",
    ]
    tags = cap.get("task_tags") or []
    if tags:
        parts.append("标签: " + ",".join(tags))
    return " / ".join(parts)


def _team_list_members(tool_ctx) -> ToolResult:
    """列出团队所有成员（含 agent_name@window_id 标识符、状态、角色、权限）"""
    window_id = tool_ctx.get("team_window_id", "")
    agent_name = tool_ctx.get("team_agent_name", "")
    if not window_id:
        return ToolResult(False, error="当前不在团队上下文中，请先执行 /team --join=<agent> 加入团队")

    tm = _get_team_manager()
    members = tm.get_members()
    if not members:
        return ToolResult(True, content="当前没有团队成员。使用 /team --join=<agent> 加入团队。")

    role_descs = {}
    try:
        template = tm.get_template()
        for item in (template or {}).get("agents") or []:
            if isinstance(item, dict) and item.get("agent_name"):
                desc = str(item.get("description") or "").strip()
                if desc:
                    role_descs[item["agent_name"]] = desc
    except Exception:
        role_descs = {}

    lines = [f"团队成员 ({len(members)} 人):"]
    for m in members:
        suffix = ""
        if m["window_id"] == window_id and m["agent_name"] == agent_name:
            suffix = " ← 你"
        status = tm.get_member_busy_status(m["window_id"])
        running_tasks = tm.get_running_tasks(m["window_id"])
        pending_tasks = tm.get_pending_tasks(m["window_id"])
        if running_tasks:
            status_label = _STATUS_LABELS.get("running", "🟡 执行任务中")
            task_summary = running_tasks[0].get("subject", "")[:40]
            if task_summary:
                status_label += f" 「{task_summary}」"
        elif pending_tasks:
            status_label = _STATUS_LABELS.get("pending", "⏳ 等待处理")
            task_summary = pending_tasks[0].get("subject", "")[:40]
            if task_summary:
                status_label += f" 「{task_summary}」"
        else:
            status_label = _STATUS_LABELS.get(status, "🟢 空闲")
        line = f"  - {m['agent_name']}@{m['window_id']}  {status_label}{suffix}"
        desc = role_descs.get(m["agent_name"], "")
        if desc:
            line += f"\n      角色: {desc}"
        cap = None
        try:
            cap = tm.get_member_capability(m["agent_name"])
        except Exception:
            cap = None
        if cap:
            line += f"\n      权限: {_format_capability(cap)}"
        lines.append(line)
    return ToolResult(True, content="\n".join(lines))


def _team_send_message(tool_ctx, to_agent: str, message: str) -> ToolResult:
    """向团队成员发送任务邮件（支持 agent_name 或 agent_name@window_id）"""
    window_id = tool_ctx.get("team_window_id", "")
    from_agent = tool_ctx.get("team_agent_name", "")
    if not window_id or not from_agent:
        return ToolResult(False, error="当前不在团队上下文中，请先执行 /team --join=<agent> 加入团队")

    tm = _get_team_manager()
    mail_id = tm.send_task(
        from_window=window_id,
        from_agent=from_agent,
        to_identifier=to_agent,
        task_description=message,
    )
    if mail_id:
        target = tm.find_member(to_agent)
        target_label = to_agent
        if target:
            target_label = f"{target['agent_name']}@{target['window_id']}"
        cap_hint = ""
        target_agent_name = (target or {}).get("agent_name", "")
        try:
            cap = tm.get_member_capability(target_agent_name) if target_agent_name else None
            if cap:
                cap_hint = f"\n      目标能力: {_format_capability(cap)}"
        except Exception:
            cap_hint = ""
        return ToolResult(True, content=f"任务邮件已发送给「{target_label}」(#{mail_id}){cap_hint}")
    else:
        members = tm.get_members()
        available = ", ".join(f"{m['agent_name']}@{m['window_id']}" for m in members)
        return ToolResult(False, error=f"未找到目标「{to_agent}」。当前团队成员: {available}")


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
                "share_context": {"type": "boolean", "description": "共享主智能体上下文给子智能体(默认True)", "default": True},
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
                "task_ids": {"type": "array", "description": "任务ID列表(不传则查刚完成的，仅一次)", "items": {"type": "string"}},
                "with_log": {"type": "boolean", "description": "含执行日志(默认False)"},
                "with_result": {"type": "boolean", "description": "含执行结果(默认True)"},
            },
        },
    },
}

_SUBAGENT_DAG_SCHEMA = {
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
                            "id": {"type": "string", "description": "节点ID(如step1/analyze/build)，供edges引用"},
                            "agent": {"type": "string", "description": "子智能体名称"},
                            "description": {"type": "string", "description": "该节点的任务描述"},
                            "context": {"type": "string", "description": "额外上下文(可选)，追加到上游结果之后"},
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
                            "from": {"type": "string", "description": "上游节点 ID"},
                            "to": {"type": "string", "description": "下游节点 ID"},
                        },
                        "required": ["from", "to"],
                    },
                },
            },
            "required": ["nodes", "edges"],
        },
    },
}

_TEAM_SEND_SCHEMA = {
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
}

_TEAM_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "team_list_members",
        "description": "列出团队中的所有成员（返回 agent_name@window_id 格式的唯一标识符，如 build@win_02）。",
        "parameters": {"type": "object", "properties": {}, "required": []},
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


def _subagent_dag_impl(tool_ctx, **kwargs):
    manager = tool_ctx.get("sub_agent_manager")
    if not manager:
        return ToolResult(False, error="子智能体管理器未初始化")
    nodes = kwargs.get("nodes", [])
    edges = kwargs.get("edges", [])
    if not nodes:
        return ToolResult(False, error="节点列表为空")
    session_id = tool_ctx.get("session_id", "")
    return manager.execute_dag(nodes=nodes, edges=edges, session_id=session_id)


def _team_send_impl(tool_ctx, **kwargs):
    return _team_send_message(tool_ctx, kwargs.get("to_agent", ""), kwargs.get("message", ""))


def _team_list_impl(tool_ctx, **kwargs):
    return _team_list_members(tool_ctx)


def _render_dag_body(result, tool_name, tool_args, success):
    """subagent_dag 完成框渲染闭包：ECharts DAG 节点图（从主程序 render_helpers 迁出）"""
    import base64
    import hashlib

    echarts = getattr(result, "echarts", None) or ""
    if not echarts:
        return None  # 无图表 → 回退默认渲染
    b64_json = base64.b64encode(echarts.encode("utf-8")).decode("ascii")
    chart_id = "echart-tool-" + hashlib.sha1(echarts.encode("utf-8")).hexdigest()[:12]
    return f"""
    <div id="{chart_id}" class="echarts-container" data-echarts-json="{b64_json}"
        style="width: 100%; height: 400px; margin: 12px 0; border-radius: 10px; overflow: hidden;"></div>"""


def _preview_subagent_para(tool_args: dict) -> str:
    tasks = tool_args.get("tasks", [])
    count = len(tasks) if isinstance(tasks, list) else 0
    if count:
        agents = set()
        for t in tasks:
            if isinstance(t, dict):
                agents.add(t.get("agent", "?"))
        agent_names = ", ".join(sorted(agents)) if agents else ""
        desc = f"分发 {count} 个子任务" + (f" → {agent_names}" if agent_names else "")
    else:
        desc = "分发子智能体任务"
    return desc


def _preview_subagent_status(tool_args: dict) -> str:
    task_ids = tool_args.get("task_ids", [])
    if isinstance(task_ids, list) and task_ids:
        return f"查询子智能体状态 ({', '.join(str(t)[:12] for t in task_ids[:3])})"
    return "查询子智能体状态"


def _preview_subagent_dag(tool_args: dict) -> str:
    nodes = tool_args.get("nodes", [])
    count = len(nodes) if isinstance(nodes, list) else 0
    return "DAG 工作流" + (f" ({count}节点)" if count else "")


def register(registry):
    registry.register(
        "subagent_para", _SUBAGENT_PARA_SCHEMA, impl=_subagent_para_impl,
        danger="dangerous", icon="设置-subagent", cn_name="分发任务",
        group=GROUP_SUBAGENT, description="并行启动子智能体",
        aliases=["subagents-para", "subagent-para", "TaskBatch", "Batch", "task", "Task"],
        preview=_preview_subagent_para,
        summarize=make_summarize_from_preview(_preview_subagent_para),
        metadata={"subagent_task": True, "permission_task": True},
    )
    registry.register(
        "subagent_status", _SUBAGENT_STATUS_SCHEMA, impl=_subagent_status_impl,
        danger="safe", icon="设置-subagent", cn_name="查询任务状态",
        group=GROUP_SUBAGENT, description="查询子智能体状态",
        aliases=["subagent-status", "TaskStatus", "Status"],
        preview=_preview_subagent_status,
        summarize=make_summarize_from_preview(_preview_subagent_status),
    )
    registry.register(
        "subagent_dag", _SUBAGENT_DAG_SCHEMA, impl=_subagent_dag_impl,
        danger="dangerous", icon="设置-subagent", cn_name="分发工作流",
        group=GROUP_SUBAGENT, description="DAG工作流子智能体",
        aliases=["subagent-dag", "subagent-teams", "SubagentDag", "Dag"],
        render=_render_dag_body,
        preview=_preview_subagent_dag,
        summarize=make_summarize_from_preview(_preview_subagent_dag),
        metadata={"subagent_task": True},
    )
    # 团队专用工具：team_only=True → 非团队成员从 schema 定义中过滤（LLM 不可见）
    registry.register(
        "team_send_message", _TEAM_SEND_SCHEMA, impl=_team_send_impl,
        danger="safe", icon="邮件-发送", cn_name="发送邮件",
        group=GROUP_TEAM, description="向团队成员发送任务", team_only=True,
    )
    registry.register(
        "team_list_members", _TEAM_LIST_SCHEMA, impl=_team_list_impl,
        danger="safe", icon="团队", cn_name="团队成员",
        group=GROUP_TEAM, description="列出团队成员", team_only=True,
    )
