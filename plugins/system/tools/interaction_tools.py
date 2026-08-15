# -*- coding: utf-8 -*-
"""
系统工具插件 — 交互、技能与系统（平台服务）

question / skill / list_skills / mcp_list_servers / upload_file：
用户提问 UI、技能库、MCP 客户端、Gitee 上传是平台能力，
impl 通过 tool_ctx["services"] 调用，工具层逻辑（参数/结果）在插件内。
"""
from app.tools.result import ToolResult

GROUP_INTERACTION = "交互与技能"
GROUP_SYSTEM = "系统与上传"


_QUESTION_SCHEMA = {
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
                            "question": {"type": "string", "description": "问题内容，尽量简洁"},
                            "options": {
                                "type": "array",
                                "description": "选项列表(可选)。每项含label+description，最多4个",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "选项标题"},
                                        "description": {"type": "string", "description": "选项描述(小字显示在标题下)"},
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multiple": {"type": "boolean", "description": "是否允许多选（可选，默认 false）"},
                        },
                        "required": ["question"],
                    },
                },
            },
            "required": ["questions"],
        },
    },
}


def _question_impl(tool_ctx, **kwargs):
    """向用户提问：返回 question 类型结果（UI 弹窗渲染，主程序不读插件状态）"""
    questions = kwargs.get("questions", [])
    # 兼容旧格式：单问题参数
    if not questions and "question" in kwargs:
        questions = [{
            "question": kwargs["question"],
            "options": kwargs.get("options", []),
            "multiple": kwargs.get("multiple", False),
        }]
    return ToolResult(True, content={"questions": questions or [], "type": "question"})


_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": "加载智能体技能",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名：brainstorming/tdd/find-skills/git-commit等"},
            },
            "required": ["name"],
        },
    },
}


def _skill_impl(tool_ctx, **kwargs):
    from app.utils.utils import load_skill

    name = kwargs.get("name", "")
    success, content, workspace = load_skill(name)
    if success:
        return ToolResult(
            True,
            content=f"Skill loaded: {name}\n\nSkill workspace: {workspace}\n\n{content}",
        )
    return ToolResult(False, error=content)


_LIST_SKILLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_skills",
        "description": "列出所有可用技能(内置+用户安装)。",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _list_skills_impl(tool_ctx, **kwargs):
    from app.utils.utils import list_skills_with_intro

    return ToolResult(True, content=list_skills_with_intro())


_MCP_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mcp_list_servers",
        "description": "列出MCP服务器连接状态和可用工具。tools字段含``mcp__{server}__``前缀，调用时用完整名，勿去前缀。",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _mcp_list_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("mcp")
    if service is None:
        return ToolResult(False, error="MCP 服务不可用")
    return ToolResult(True, content=service.get_status())


_UPLOAD_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "upload_file",
        "description": "上传文件到Gitee仓库，返回下载链接。用于gateway远程调用场景。**勿滥用**，注意敏感信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "local_path": {"type": "string", "description": "文件路径(绝对/相对workdir)"},
            },
            "required": ["local_path"],
        },
    },
}


def _upload_file_impl(tool_ctx, **kwargs):
    service = tool_ctx.get("services", {}).get("gitee")
    if service is None:
        return ToolResult(False, error="上传服务不可用")
    return service(local_path=kwargs.get("local_path", ""))


def register(registry):
    registry.register(
        "question", _QUESTION_SCHEMA, impl=_question_impl,
        danger="safe", icon="question", cn_name="提问",
        group=GROUP_INTERACTION, description="向用户提问确认",
        aliases=["Question", "AskUser", "ask_user", "AskUserQuestion"],
    )
    registry.register(
        "skill", _SKILL_SCHEMA, impl=_skill_impl,
        danger="safe", icon="技能", cn_name="加载技能",
        group=GROUP_INTERACTION, description="加载指定技能",
        aliases=["Skill"],
    )
    registry.register(
        "list_skills", _LIST_SKILLS_SCHEMA, impl=_list_skills_impl,
        danger="safe", icon="技能", cn_name="列出技能",
        group=GROUP_INTERACTION, description="列出可用技能",
        aliases=["ListSkills", "listSkills"],
    )
    registry.register(
        "mcp_list_servers", _MCP_LIST_SCHEMA, impl=_mcp_list_impl,
        danger="safe", icon="工具", cn_name="MCP列表",
        group=GROUP_SYSTEM, description="列出MCP服务器",
        aliases=["McpListServers", "mcp_list_servers"],
    )
    registry.register(
        "upload_file", _UPLOAD_FILE_SCHEMA, impl=_upload_file_impl,
        danger="dangerous", icon="upload-file", cn_name="上传文件",
        group=GROUP_SYSTEM, description="上传本地文件到Gitee",
    )
