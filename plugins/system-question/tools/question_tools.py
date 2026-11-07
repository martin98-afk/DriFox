# -*- coding: utf-8 -*-
"""
系统提问插件 — question 工具（用户交互）

从 system-tools 拆出：question 是交互式工具（metadata interactive），
宿主弹出提问卡并阻塞等待用户回答。本插件提供 schema / 参数规范化 /
完成框渲染 / 预览与摘要；超时策略由本插件的 ui 组件经能力池驱动。
"""
import json
import re

from app.plugins.sdk import ToolResult

GROUP_INTERACTION = "交互与技能"


_QUESTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "question",
        "description": "向用户提问。需求/偏好/选择不明时**必用**。支持多问题，每题可带选项。",
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "问题列表。每项 question+options(label+description)+multiple",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "description": "问题内容，尽量简洁"},
                            "options": {
                                "type": "array",
                                "description": "选项列表(可选)。每项 label+description，最多 4 个",
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


def _preview_question(tool_args: dict) -> str:
    questions_raw = tool_args.get("questions", [])
    if not questions_raw and "question" in tool_args:
        questions_raw = [{"question": str(tool_args["question"])}]
    normalized = _parse_questions_field(questions_raw)
    q_text = normalized[0].get("question", "") if normalized else ""
    return q_text[:120] + ("…" if len(q_text) > 120 else "") if q_text else "(无问题)"


def _summarize_question(tool_name, tool_args, tool_content):
    """question 压缩摘要（从 history_compactor 迁出）"""
    return "[clarify] asked user a question"


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
