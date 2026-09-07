# -*- coding: utf-8 -*-
"""
系统工具插件 — 交互、技能与系统（平台服务）

question / skill / manage_skill / mcp_list_servers / upload_file：
用户提问 UI、技能库、MCP 客户端、Gitee 上传是平台能力，
impl 通过 tool_ctx["services"] 调用，工具层逻辑（参数/结果）在插件内。

渲染完全自包含：question 的完成框渲染（终端块 + 选项 + 回答解析）由
本插件的 render 闭包实现，主程序只做闭包路由，不写死任何工具名。
"""
import json
import re
from pathlib import Path

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
        "description": "加载智能体技能到当前会话。阻断要求：用户请求命中任何可用技能描述的场景（含 /斜杠命令引用，如 /visualization）时，必须在生成其他回答前先调用本工具加载对应技能；提到某技能就必须真实调用本工具，禁止只提及不加载；同一技能已加载则勿重复调用，直接遵循其内容执行。可用技能清单见系统提示「偏好技能」段，未列出时用 manage_skill 的 list 动作查看。",
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


_MANAGE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "manage_skill",
        "description": "技能生命周期管理（只读指导式）。list=返回技能清单（含来源/位置/agent_created 标记；支持 query/source 过滤，全量超 30 个自动降为精简视图）；create/modify/delete 本工具不直接改文件，返回目标路径、SKILL.md 模板与分步工作流，由你用 write/edit/bash 执行。仅 frontmatter 含 agent_created: true 且位于用户技能目录的自建技能可修改或删除，系统/用户安装技能受保护；删除前必须先征得用户确认。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "modify", "delete"], "description": "list=清单；create=新建指引；modify=编辑指引；delete=删除指引"},
                "name": {"type": "string", "description": "create/modify/delete 必填：技能名（小写字母/数字/中划线）"},
                "query": {"type": "string", "description": "list 可选：按技能名/描述子串过滤（不分大小写），如「visualization」"},
                "source": {"type": "string", "description": "list 可选：按来源过滤（system/user/插件名子串）"},
            },
            "required": ["action"],
        },
    },
}


def _agent_created_check(skill: dict) -> tuple[bool, str]:
    """双校验：技能位于用户数据技能目录 + frontmatter 标记 agent_created: true"""
    from app.utils.utils import get_app_data_dir

    p = Path(skill.get("path", "")).resolve()
    user_base = (get_app_data_dir() / "skills").resolve()
    if user_base not in p.parents:
        return False, f"技能不在用户技能目录（{user_base}）内，系统/用户安装的插件技能受保护"
    md = p / "SKILL.md"
    if not md.exists():
        md = p / "skill.md"
    if not md.exists():
        return False, "SKILL.md 不存在"
    if "agent_created: true" not in md.read_text(encoding="utf-8")[:600]:
        return False, "frontmatter 缺 agent_created: true，受保护不可修改/删除"
    return True, ""


def _skill_inventory(query: str = "", source: str = "") -> str:
    from app.utils.utils import get_local_skills, get_app_data_dir

    skills = get_local_skills()
    if not skills:
        return "当前没有任何技能。用 create 动作可创建新技能。"
    user_base = (get_app_data_dir() / "skills").resolve()
    q = (query or "").lower()
    src = (source or "").lower()

    rows = []
    for s in skills:
        p = Path(s.get("path", "")).resolve()
        tag = ""
        if user_base in p.parents:
            md = p / "SKILL.md"
            if md.exists() and "agent_created: true" in md.read_text(encoding="utf-8")[:600]:
                tag = " [agent_created]"
        src_name = s.get("plugin_name") or ("system" if s.get("is_system") else "user")
        if src and src not in src_name.lower():
            continue
        desc = (s.get("description") or "").strip().replace("\n", " ")
        if q and q not in s["qualified_name"].lower() and q not in desc.lower():
            continue
        rows.append((s, tag, src_name, desc))

    if not rows:
        return f"无匹配技能（query={query!r}, source={source!r}）。清空过滤条件查看全部 {len(skills)} 个。"

    # 无过滤且量大时降为单行精简视图，避免全量输出被截断
    brief = not q and not src and len(rows) > 30
    lines = [f"共 {len(skills)} 个技能，匹配 {len(rows)} 个："]
    for s, tag, src_name, desc in rows:
        if brief:
            lines.append(f"- {s['qualified_name']}{tag}（{src_name}）")
        else:
            lines.append(f"- {s['qualified_name']}{tag}（来源: {src_name}）\n  {desc[:100]}\n  位置: {s['path']}")
    if brief:
        lines.append("\n超过 30 个，已显示精简视图；用 query（名称/描述子串）或 source（system/user/插件名）过滤查看详情。")
    lines.append(f"\n仅 [agent_created] 技能可用 modify/delete；create 的新技能放用户技能目录：{user_base}")
    return "\n".join(lines)


def _guide_create(name: str) -> str:
    from app.utils.utils import get_app_data_dir

    if not name or not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
        return "错误：name 必填，技能名须为小写字母/数字/中划线（如 pdf-summary）。"
    base = get_app_data_dir() / "skills"
    target = base / name
    if target.exists():
        return f"错误：{target} 已存在。同名技能请用 modify。"
    return f"""技能创建指引（本工具不写文件，按以下步骤用 write 工具执行）：

目标目录：{target}

第 1 步：创建 {target}\\SKILL.md，内容模板：
---
name: {name}
description: （必填。一句话说明该技能解决什么问题、何时触发；这是模型路由的依据，务必覆盖用户会说的触发词）
agent_created: true
---

# {name}

（技能正文：工作流步骤、规则约束、示例。详细资料可拆到 references/ 子文件按需加载）

第 2 步：如需校验脚本/评测用例，放同目录 scripts/、evals/ 子目录（可选）
第 3 步：完成后用 manage_skill(action="list") 核验技能已被发现

约束：description 决定技能是否被主动加载，写清触发场景；agent_created: true 是后续 modify/delete 的凭证，勿删。"""


def _guide_modify(name: str) -> str:
    from app.utils.utils import get_skill_by_name

    skill = get_skill_by_name(name or "")
    if not skill:
        return f"错误：技能「{name}」不存在。用 list 动作查看全部技能。"
    ok, reason = _agent_created_check(skill)
    if not ok:
        return f"拒绝：{reason}"
    p = Path(skill["path"])
    return f"""技能编辑指引：

技能目录：{p}
标准结构：SKILL.md（frontmatter: name/description/agent_created）+ 可选 references/、scripts/、evals/

步骤：
1. read {p}\\SKILL.md 查看现状
2. 按需 edit 正文；改动 description 会影响触发倾向，谨慎
3. 保持 frontmatter 的 agent_created: true（删掉将失去后续修改权限）
4. 保存即热生效（技能列表按 mtime 缓存，自动失效）"""


def _guide_delete(name: str) -> str:
    from app.utils.utils import get_skill_by_name

    skill = get_skill_by_name(name or "")
    if not skill:
        return f"错误：技能「{name}」不存在。"
    ok, reason = _agent_created_check(skill)
    if not ok:
        return f"拒绝：{reason}"
    p = Path(skill["path"])
    return f"""技能删除指引：

⚠️ 执行前必须先向用户确认（question 工具或正文询问），获得明确同意后才能继续。

技能目录：{p}
步骤：
1. 获得用户明确确认
2. 用 bash 删除整个目录（含子文件，删除不可逆）
3. 用 manage_skill(action="list") 核验已消失

注意：若技能含用户数据（evals 运行记录等），先提示用户。"""


def _manage_skill_impl(tool_ctx, **kwargs):
    action = (kwargs.get("action") or "list").lower()
    name = kwargs.get("name", "")
    if action == "list":
        return ToolResult(True, content=_skill_inventory(kwargs.get("query", ""), kwargs.get("source", "")))
    if action == "create":
        return ToolResult(True, content=_guide_create(name))
    if action == "modify":
        return ToolResult(True, content=_guide_modify(name))
    if action == "delete":
        return ToolResult(True, content=_guide_delete(name))
    return ToolResult(False, error=f"未知 action: {action}（支持 list/create/modify/delete）")


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

    # 配色一律走主程序暴露的主题令牌（--panel-soft / --text / --border ...），
    # 不写死颜色、也不往主程序塞工具专属 CSS —— 换主题自动跟随。
    return f"""
    <div style="background:var(--panel-soft);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin:0;">
        <div style="padding:6px 12px;border-bottom:1px solid var(--border);color:var(--text-secondary);word-break:break-word;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;">
            <span style="color:var(--accent);">?</span> <span style="color:var(--text);">question: {escape(q_preview)}</span>
        </div>
        <pre style="margin:0;padding:10px 12px;background:transparent;color:var(--text);font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-word;max-height:520px;overflow-y:auto;">{escape(body_text)}</pre>
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


def _preview_manage_skill(tool_args: dict) -> str:
    action = (tool_args or {}).get("action") or "list"
    name = (tool_args or {}).get("name", "")
    cn = {"list": "列出技能", "create": "新建技能", "modify": "编辑技能", "delete": "删除技能"}.get(action, action)
    return f"{cn}: {name}" if name else cn


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
        keep_in_content=True,  # 提问卡常驻正文，不迁入工具折叠区
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
        "manage_skill", _MANAGE_SKILL_SCHEMA, impl=_manage_skill_impl,
        danger="safe", icon="技能", cn_name="管理技能",
        group=GROUP_INTERACTION, description="技能生命周期管理",
        aliases=["ListSkills", "listSkills", "ManageSkill", "skillmanage"],
        preview=_preview_manage_skill,
        summarize=make_summarize_from_preview(_preview_manage_skill),
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
