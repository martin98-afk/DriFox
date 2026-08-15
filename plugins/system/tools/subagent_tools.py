# -*- coding: utf-8 -*-
"""
系统工具插件 — 子智能体与团队协作（平台服务）

subagent_* / team_*：子智能体调度（SubAgentManager）与团队协作（TeamManager）
是平台能力，impl 通过 tool_ctx["services"] 调用，工具层逻辑（参数/结果）在插件内。
"""
import orjson

from app.tools.result import ToolResult

GROUP_SUBAGENT = "子智能体"
GROUP_TEAM = "团队协作"

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


def _subagent_para_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("subagent")
    if service is None:
        return ToolResult(False, error="子智能体服务不可用")
    tasks_val = kwargs.get("tasks", [])
    tasks = orjson.loads(tasks_val) if isinstance(tasks_val, str) else (tasks_val or [])
    return service.subagent_para_execute(
        tasks,
        kwargs.get("share_context", False),
        session_id=tool_ctx.get("session_id", ""),
        sub_agent_manager=tool_ctx.get("sub_agent_manager"),
    )


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


def _subagent_status_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("subagent")
    if service is None:
        return ToolResult(False, error="子智能体服务不可用")
    return service.subagent_status(
        kwargs.get("task_ids"),
        kwargs.get("with_log", False),
        kwargs.get("with_result", True),
        session_id=tool_ctx.get("session_id", ""),
        sub_agent_manager=tool_ctx.get("sub_agent_manager"),
    )


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


def _subagent_dag_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("subagent")
    if service is None:
        return ToolResult(False, error="子智能体服务不可用")
    return service.subagent_dag_execute(
        kwargs.get("nodes", []),
        kwargs.get("edges", []),
        session_id=tool_ctx.get("session_id", ""),
        sub_agent_manager=tool_ctx.get("sub_agent_manager"),
    )


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


def _team_send_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("team")
    if service is None:
        return ToolResult(False, error="团队服务不可用")
    return service.team_send_message(
        to_agent=kwargs.get("to_agent", ""),
        message=kwargs.get("message", ""),
    )


_TEAM_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "team_list_members",
        "description": "列出团队中的所有成员（返回 agent_name@window_id 格式的唯一标识符，如 build@win_02）。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _team_list_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("team")
    if service is None:
        return ToolResult(False, error="团队服务不可用")
    return service.team_list_members()


def register(registry):
    registry.register(
        "subagent_para", _SUBAGENT_PARA_SCHEMA, impl=_subagent_para_impl,
        danger="dangerous", icon="设置-subagent", cn_name="分发任务",
        group=GROUP_SUBAGENT, description="并行启动子智能体",
        aliases=["subagents-para", "subagent-para", "TaskBatch", "Batch", "task", "Task"],
    )
    registry.register(
        "subagent_status", _SUBAGENT_STATUS_SCHEMA, impl=_subagent_status_impl,
        danger="safe", icon="设置-subagent", cn_name="查询任务状态",
        group=GROUP_SUBAGENT, description="查询子智能体状态",
        aliases=["subagent-status", "TaskStatus", "Status"],
    )
    registry.register(
        "subagent_dag", _SUBAGENT_DAG_SCHEMA, impl=_subagent_dag_impl,
        danger="dangerous", icon="设置-subagent", cn_name="分发工作流",
        group=GROUP_SUBAGENT, description="DAG工作流子智能体",
        aliases=["subagent-dag", "subagent-teams", "SubagentDag", "Dag"],
    )
    registry.register(
        "team_send_message", _TEAM_SEND_SCHEMA, impl=_team_send_impl,
        danger="dangerous", icon="邮件-发送", cn_name="发送邮件",
        group=GROUP_TEAM, description="向团队成员发送任务",
    )
    registry.register(
        "team_list_members", _TEAM_LIST_SCHEMA, impl=_team_list_impl,
        danger="safe", icon="团队", cn_name="团队成员",
        group=GROUP_TEAM, description="列出团队成员",
    )
