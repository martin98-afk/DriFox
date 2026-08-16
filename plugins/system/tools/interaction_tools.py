# -*- coding: utf-8 -*-
"""
系统工具插件 — 交互、技能与系统（平台服务）

question / skill / list_skills / mcp_list_servers / upload_file：
用户提问 UI、技能库、MCP 客户端、Gitee 上传是平台能力，
impl 通过 tool_ctx["services"] 调用，工具层逻辑（参数/结果）在插件内。

渲染完全自包含：question 的完成框渲染（终端块 + 选项 + 回答解析）由
本插件的 render 闭包实现，主程序只做闭包路由，不写死任何工具名。
"""
import json
import re

from app.tools.result import ToolResult
from app.tools.registry import make_summarize_from_preview

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


def _render_question_body(result, tool_name, tool_args, success):
    """question 完成框渲染闭包：终端块（问题 → 选项 → 回答）

    渲染逻辑从主程序 render_helpers._render_question_block 完整迁出，
    主程序不再包含 question 任何写死分支。
    """
    from app.widgets.render_helpers import (
        _get_global_font,
        _unescape_newlines,
        escape,
        scale_font_size,
    )

    tool_args = tool_args or {}
    result_str = getattr(result, "content", "") or ""
    _gf = _get_global_font()

    # 获取问题列表（兼容新旧格式 + 字符串类型）
    questions_raw = tool_args.get("questions", [])
    if not questions_raw and "question" in tool_args:
        questions_raw = [
            {
                "question": str(tool_args.get("question", "")),
                "options": tool_args.get("options", []),
                "multiple": tool_args.get("multiple", False),
            }
        ]
    normalized = _parse_questions_field(questions_raw)
    if not normalized:
        return ""

    # 解析用户回答
    answer_map = _parse_question_result(result_str) if result_str else {}

    # 构建终端体文本
    lines = []
    for q in normalized:
        q_text = q.get("question", "")
        options = q.get("options", [])
        multiple = q.get("multiple", False)
        suffix = " (多选)" if multiple else ""
        answer_info = answer_map.get(q_text, {})
        selected_labels = answer_info.get("selected", [])
        custom_text = answer_info.get("custom")

        # 问题文本行
        lines.append(f"❓ {q_text}{suffix}")

        # 选项列表
        if options:
            lines.append("")
            unselected_marker = "○"
            selected_marker = "◉" if multiple else "●"
            for opt in options:
                opt = _normalize_option_item(opt)
                label = opt.get("label", "")
                desc = opt.get("description", "")
                is_selected = label in selected_labels
                marker = selected_marker if is_selected else unselected_marker
                if desc:
                    lines.append(f"  {marker} {label}  —  {desc}")
                else:
                    lines.append(f"  {marker} {label}")
            lines.append("")

        # 用户回答：只有自定义输入才单独显示（选中的选项已用实心标记）
        if custom_text:
            lines.append(f"  ✎ 自定义: {custom_text}")
        if not selected_labels and not custom_text and not options:
            # 无选项且无回答时，显示原始 result
            if result_str:
                lines.append(f"  ✎ 回答: {_unescape_newlines(result_str)}")
        lines.append("")

    body_text = "\n".join(lines).rstrip()

    # 终端头预览（第一个问题文本）
    first_q = normalized[0].get("question", "")
    q_preview = first_q[:80] + ("…" if len(first_q) > 80 else "")

    return f"""
    <div class="terminal-block" style="background:rgba(13,17,23,0.40);border:1px solid rgba(48,54,61,0.25);border-radius:8px;overflow:hidden;margin:0;">
        <div style="padding:6px 12px;background:rgba(22,27,34,0.40);border-bottom:1px solid rgba(48,54,61,0.25);color:#8b949e;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(12)}px;">
            <span style="color:#FFA500;">❓</span> <span style="color:#c9d1d9;">question: {escape(q_preview)}</span>
        </div>
        <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;">{escape(body_text)}</pre>
    </div>"""


# ========== question 辅助解析（随插件迁移，主程序不再保留） ==========

def _normalize_question_item(q) -> dict:
    """将 question 条目规范化为 dict 格式（兼容 str/dict/其他）"""
    if isinstance(q, dict):
        return q
    elif isinstance(q, str):
        return {"question": q, "options": [], "multiple": False}
    else:
        return {"question": str(q), "options": [], "multiple": False}


def _normalize_option_item(opt) -> dict:
    """将选项条目规范化为 dict 格式（兼容 str/dict/其他）"""
    if isinstance(opt, dict):
        label = opt.get("label", "")
        if not label:
            for key in ("name", "text", "value", "title"):
                label = opt.get(key, "")
                if label:
                    break
        if not label:
            for v in opt.values():
                if isinstance(v, str) and v:
                    label = v
                    break
            if not label:
                label = str(opt)
        return {"label": label, "description": opt.get("description", "")}
    elif isinstance(opt, str):
        return {"label": opt, "description": ""}
    else:
        return {"label": str(opt), "description": ""}


def _parse_questions_field(questions_raw) -> list:
    """解析 questions 字段，兼容 list / str / None（JSON 字符串还原）"""
    if not questions_raw:
        return []

    if isinstance(questions_raw, list):
        return [_normalize_question_item(q) for q in questions_raw]

    if isinstance(questions_raw, str):
        try:
            parsed = json.loads(questions_raw)
            if isinstance(parsed, list):
                return [_normalize_question_item(q) for q in parsed]
            elif isinstance(parsed, dict):
                return [_normalize_question_item(parsed)]
        except (json.JSONDecodeError, ValueError):
            pass
        return [{"question": questions_raw, "options": [], "multiple": False}]

    return [{"question": str(questions_raw), "options": [], "multiple": False}]


# 预编译：匹配 "问题「xxx」的回答：" 格式
_QRESULT_SECTION_RE = re.compile(r"问题「(.+?)」的回答：\n?(.*)", re.DOTALL)
# 预编译：匹配 【label】 格式的选中项
_QRESULT_SELECTED_RE = re.compile(r"【(.+?)】")


def _parse_question_result(result: str) -> dict:
    """解析 question 工具的回答字符串，提取每个问题的选中选项和自定义回答

    回答格式（来自 question_floating_widget._build_and_emit_answer）:
        问题「问题文本」的回答：
        【选项1】；【选项2】；自定义文本
        ---
        问题「问题2」的回答：
        【选项A】

    返回: {question_text: {"selected": [label, ...], "custom": str or None}}
    """
    if not result:
        return {}

    from app.widgets.render_helpers import _unescape_newlines

    text = _unescape_newlines(result)
    answers = {}

    for section in re.split(r"\n---\n", text):
        m = _QRESULT_SECTION_RE.match(section.strip())
        if not m:
            continue
        q_text = m.group(1).strip()
        answer_text = m.group(2).strip()

        # 提取 【label】 格式的选中选项
        selected = _QRESULT_SELECTED_RE.findall(answer_text)

        # 自定义文本 = 移除 【label】 和分隔符后的剩余文本
        custom_text = _QRESULT_SELECTED_RE.sub("", answer_text)
        # 移除中英文分号分隔符
        custom_text = custom_text.replace("；", "").replace(";", "").strip()
        # 过滤掉仅含省略号或空白的假自定义文本
        if custom_text and custom_text != "...":
            custom = custom_text
        else:
            custom = None

        answers[q_text] = {"selected": selected, "custom": custom}

    return answers


# ========== preview 闭包（inline 卡/折叠头自然语言预览，随插件定义） ==========

def _preview_question(tool_args: dict) -> str:
    questions_raw = tool_args.get("questions", [])
    if not questions_raw and "question" in tool_args:
        questions_raw = [{"question": str(tool_args["question"])}]
    normalized = _parse_questions_field(questions_raw)
    q_text = normalized[0].get("question", "") if normalized else ""
    return q_text[:120] + ("…" if len(q_text) > 120 else "") if q_text else "(无问题)"


def _preview_skill(tool_args: dict) -> str:
    name = tool_args.get("name", "")
    return f'加载技能 "{name}"' if name else "加载技能"


def _preview_list_skills(tool_args: dict) -> str:
    return "列出可用技能"


def _preview_mcp_list_servers(tool_args: dict) -> str:
    return "列出 MCP 服务器"


def _summarize_question(tool_name, tool_args, tool_content):
    """question 压缩摘要（从 history_compactor 迁出）"""
    return "[clarify] asked user a question"


def _summarize_skill(tool_name, tool_args, tool_content):
    """skill 压缩摘要（从 history_compactor 迁出）"""
    name = (tool_args or {}).get("name", "?")
    content_len = len(tool_content or "")
    return f"[{tool_name}] name={name} ({content_len:,} chars)"


def register(registry):
    registry.register(
        "question", _QUESTION_SCHEMA, impl=_question_impl,
        danger="safe", icon="question", cn_name="提问",
        group=GROUP_INTERACTION, description="向用户提问确认",
        aliases=["Question", "AskUser", "ask_user", "AskUserQuestion"],
        render=_render_question_body,
        preview=_preview_question,
        summarize=_summarize_question,
        metadata={"interactive": True, "ui_managed": True},  # UI 弹窗交互，非纯工具执行
    )
    registry.register(
        "skill", _SKILL_SCHEMA, impl=_skill_impl,
        danger="safe", icon="技能", cn_name="加载技能",
        group=GROUP_INTERACTION, description="加载指定技能",
        aliases=["Skill"],
        preview=_preview_skill,
        summarize=_summarize_skill,
        metadata={"protect": True, "permission_arg": "name"},  # 技能内容完整保留；权限按技能名
    )
    registry.register(
        "list_skills", _LIST_SKILLS_SCHEMA, impl=_list_skills_impl,
        danger="safe", icon="技能", cn_name="列出技能",
        group=GROUP_INTERACTION, description="列出可用技能",
        aliases=["ListSkills", "listSkills"],
        preview=_preview_list_skills,
        summarize=make_summarize_from_preview(_preview_list_skills),
    )
    registry.register(
        "mcp_list_servers", _MCP_LIST_SCHEMA, impl=_mcp_list_impl,
        danger="safe", icon="工具", cn_name="MCP列表",
        group=GROUP_INTERACTION, description="列出MCP服务器",
        aliases=["McpListServers", "mcp_list_servers"],
        preview=_preview_mcp_list_servers,
        summarize=make_summarize_from_preview(_preview_mcp_list_servers),
    )
    registry.register(
        "upload_file", _UPLOAD_FILE_SCHEMA, impl=_upload_file_impl,
        danger="dangerous", icon="upload-file", cn_name="上传文件",
        group=GROUP_SYSTEM, description="上传本地文件到Gitee",
    )
