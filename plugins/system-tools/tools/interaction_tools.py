# -*- coding: utf-8 -*-
"""
系统工具插件 — 交互、技能与系统（平台服务）

question / skill / manage_skill / mcp_manage / upload_file：
用户提问 UI、技能库、MCP 客户端、Gitee 上传是平台能力，
impl 通过 tool_ctx["services"] 调用，工具层逻辑（参数/结果）在插件内。

渲染完全自包含：question 的完成框渲染（终端块 + 选项 + 回答解析）由
本插件的 render 闭包实现，主程序只做闭包路由，不写死任何工具名。
"""
import json
import re
from pathlib import Path

from app.plugins.sdk import ToolResult
from app.plugins.sdk import make_summarize_from_preview

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
    from app.plugins.sdk import load_skill

    name = kwargs.get("name", "")
    workdir = (tool_ctx or {}).get("workdir") or None
    success, content, workspace = load_skill(name, project_dir=workdir)
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
    from app.plugins.sdk import get_app_data_dir

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


def _skill_inventory(query: str = "", source: str = "", workdir: str = "") -> str:
    from app.plugins.sdk import get_app_data_dir, get_local_skills

    skills = get_local_skills(project_dir=workdir or None)
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
        src_name = s.get("plugin_name") or s.get("source") or ("system" if s.get("is_system") else "user")
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
    from app.plugins.sdk import get_app_data_dir

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
    from app.plugins.sdk import get_skill_by_name

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
    from app.plugins.sdk import get_skill_by_name

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
        wd = (tool_ctx or {}).get("workdir") or ""
        return ToolResult(True, content=_skill_inventory(kwargs.get("query", ""), kwargs.get("source", ""), workdir=wd))
    if action == "create":
        return ToolResult(True, content=_guide_create(name))
    if action == "modify":
        return ToolResult(True, content=_guide_modify(name))
    if action == "delete":
        return ToolResult(True, content=_guide_delete(name))
    return ToolResult(False, error=f"未知 action: {action}（支持 list/create/modify/delete）")


_MCP_MANAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mcp_manage",
        "description": (
            "管理 MCP 服务器。action=\"list\"（默认）列出全部服务器（含已关闭的）与连接状态；"
            "action=\"enable\" 临时开启一台已关闭的服务器，返回它的工具定义（名字+描述+参数 schema），"
            "随后用 tool_execute(tool_name=\"mcp__服务__工具\", arguments=...) 中转调用；"
            "action=\"disable\" 关闭临时开启的服务器。"
            "\n\n省 token 纪律：只有常驻开启的服务器会把工具注入工具列表；临时的不会。"
            "所以临时开启的服务器用完必须调 action=\"disable\" 回收（不会自动超时关闭）。"
            "\n\n能临时开启的仅限配置里已存在但被关闭的服务器；不存在的新服务器需要用户在设置里添加。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "enable", "disable"],
                    "description": "list=列出全部（默认）；enable=临时开启；disable=关闭临时开启的",
                },
                "name": {
                    "type": "string",
                    "description": "服务器名（enable / disable 必填，取 list 返回里的 name）",
                },
            },
        },
    },
}

# 与 app/tools/mcp_tools.py 的 MCPState 常量一致；插件侧用字面量，
# 避免顶层导入主程序内部模块（plugins 加载到 app 重依赖是历史坑）。
_MCP_STATE_CN = {
    "connecting": "启动中",
    "connected": "已连接",
    "failed": "连接失败",
    "disabled": "已关闭",
}


def _mcp_render_list(status: list) -> str:
    """把 get_status() 的结构渲染成模型可读文本（避免裸 dict 白烧 token）"""
    if not status:
        return (
            "没有任何 MCP 服务器配置。需要新增请让用户在「设置 → MCP 连接器」中配置。"
        )
    lines = [f"MCP 服务器共 {len(status)} 个："]
    for item in status:
        name = item.get("name", "?")
        if item.get("ephemeral"):
            tag = "临时开启"
        elif item.get("enabled"):
            tag = "常驻开启"
        else:
            tag = "关闭"
        state = _MCP_STATE_CN.get(item.get("state", ""), item.get("state", ""))
        tools = item.get("tools") or []
        line = f"- {name}（{tag}·{state}，{len(tools)} 个工具）"
        if tag == "关闭":
            line += f" → 可临时开启：action=\"enable\", name=\"{name}\""
        elif item.get("ephemeral"):
            line += f" → 用完请关闭：action=\"disable\", name=\"{name}\""
        lines.append(line)
        if item.get("error"):
            lines.append(f"    错误：{item['error']}")
        if tools:
            lines.append(f"    工具：{', '.join(tools)}")
    lines.append("")
    lines.append(
        "说明：只有「常驻开启」的服务器工具在常驻工具列表里，可直接调用；"
        "「临时开启」的工具需经 tool_execute(tool_name=..., arguments={...}) 中转调用。"
    )
    return "\n".join(lines)


def _mcp_render_enabled(name: str, schemas: list) -> str:
    """临时开启成功 → 只渲染工具名录（名 + 一句话描述），不含参数。

    为什么不渲染完整 schema：一台服务器的全部工具参数动辄数万字符，灌
    一次就吃掉大块上下文，而模型当轮通常只用一两个工具。参数详情让模型
    用 tool_search 的 tool_names 按需查（tool_search 的搜索面已含临时
    开启的服务器），用时才查、查了就调。
    """
    if not schemas:
        return f"MCP 服务器 '{name}' 已临时开启，但未发现任何工具。"
    lines = [
        f"MCP 服务器 '{name}' 已临时开启，共 {len(schemas)} 个工具。",
        "它们的定义**不在**常驻工具列表里，调用须用 tool_execute(tool_name=工具名, arguments=参数对象)。",
        f"参数详情用 tool_search(tool_names=[\"工具名\"]) 按需查询；用完请 mcp_manage(action=\"disable\", name=\"{name}\") 回收。",
        "",
    ]
    for schema in schemas:
        fn = schema.get("function", {}) or {}
        desc_lines = (fn.get("description") or "（无描述）").strip().splitlines()
        desc = desc_lines[0].strip() if desc_lines else "（无描述）"
        if len(desc) > 100:
            desc = desc[:100] + "…"
        lines.append(f"- {fn.get('name', '?')}：{desc}")
    return "\n".join(lines)


def _mcp_manage_impl(tool_ctx, **kwargs):
    service = (tool_ctx or {}).get("services", {}).get("mcp")
    if service is None:
        return ToolResult(False, error="MCP 服务不可用")
    action = (kwargs.get("action") or "list").lower()
    name = (kwargs.get("name") or "").strip()

    if action == "list":
        return ToolResult(True, content=_mcp_render_list(service.get_status()))

    if action not in ("enable", "disable"):
        return ToolResult(False, error=f"未知 action: {action}（支持 list/enable/disable）")
    if not name:
        return ToolResult(False, error=f"action=\"{action}\" 需要提供 name（服务器名）")

    if action == "enable":
        ok, payload = service.enable_ephemeral(name)
        if not ok:
            return ToolResult(False, error=str(payload))
        return ToolResult(True, content=_mcp_render_enabled(name, payload or []))

    ok, error = service.disable_ephemeral(name)
    if not ok:
        return ToolResult(False, error=error)
    return ToolResult(True, content=f"MCP 服务器 '{name}' 的临时开启已关闭，子进程已回收。")


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
    # 经能力池取上传能力（manifest 声明裁剪 + 审计），不走 services 旧键
    upload = (tool_ctx or {}).get("capabilities", {}).get("net.upload")
    if upload is None:
        return ToolResult(False, error="上传服务不可用（net.upload 能力未授权或未注册）")
    return upload(local_path=kwargs.get("local_path", ""))


def _render_question_body(result, tool_name, tool_args, success):
    """question 完成框渲染闭包：终端块（问题 → 选项 → 回答）

    渲染逻辑从主程序 render_helpers._render_question_block 完整迁出，
    主程序不再包含 question 任何写死分支。
    """
    from app.plugins.sdk import global_font_family, html_escape, scale_font_size, unescape_newlines

    tool_args = tool_args or {}
    result_str = getattr(result, "content", "") or ""
    _gf = global_font_family()

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
                lines.append(f"  ✎ 回答: {unescape_newlines(result_str)}")
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
            <span style="color:var(--accent);">?</span> <span style="color:var(--text);">question: {html_escape(q_preview)}</span>
        </div>
        <pre style="margin:0;padding:10px 12px;background:transparent;color:var(--text);font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-word;max-height:520px;overflow-y:auto;">{html_escape(body_text)}</pre>
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

    from app.plugins.sdk import unescape_newlines

    text = unescape_newlines(result)
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


def _preview_mcp_manage(tool_args: dict) -> str:
    tool_args = tool_args or {}
    action = (tool_args.get("action") or "list").lower()
    name = tool_args.get("name", "")
    cn = {"list": "查看 MCP 服务器", "enable": "临时开启 MCP", "disable": "关闭临时 MCP"}.get(
        action, action
    )
    return f"{cn}: {name}" if name else cn


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
        metadata={"interactive": True, "ui_managed": True, "no_prune": True, "no_offload": True},  # UI 弹窗交互，非纯工具执行；结果小且语义完整
    )
    registry.register(
        "skill", _SKILL_SCHEMA, impl=_skill_impl,
        danger="safe", icon="技能", cn_name="加载技能",
        group=GROUP_INTERACTION, description="加载指定技能",
        aliases=["Skill"],
        preview=_preview_skill,
        summarize=_summarize_skill,
        metadata={"protect": True, "permission_arg": "name", "no_prune": True, "no_offload": True},  # 技能内容完整保留；权限按技能名；结果小且语义完整
    )
    registry.register(
        "manage_skill", _MANAGE_SKILL_SCHEMA, impl=_manage_skill_impl,
        danger="safe", icon="技能", cn_name="管理技能",
        group=GROUP_INTERACTION, description="技能生命周期管理",
        aliases=["ListSkills", "listSkills", "ManageSkill", "skillmanage"],
        preview=_preview_manage_skill,
        summarize=make_summarize_from_preview(_preview_manage_skill),
        metadata={"no_offload": True},  # 服务器列表小且结构化，落盘无收益
    )
    registry.register(
        "mcp_manage", _MCP_MANAGE_SCHEMA, impl=_mcp_manage_impl,
        danger="safe", icon="工具", cn_name="管理MCP",
        group=GROUP_INTERACTION, description="管理MCP服务器：列出/临时开启/关闭",
        aliases=["mcp_list_servers", "McpListServers", "McpManage", "mcp_list"],
        preview=_preview_mcp_manage,
        summarize=make_summarize_from_preview(_preview_mcp_manage),
        metadata={"no_offload": True},  # 服务器列表小且结构化，落盘无收益
    )
    registry.register(
        "upload_file", _UPLOAD_FILE_SCHEMA, impl=_upload_file_impl,
        danger="dangerous", icon="upload-file", cn_name="上传文件",
        group=GROUP_SYSTEM, description="上传本地文件到Gitee",
    )
