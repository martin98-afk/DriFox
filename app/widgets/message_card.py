# -*- coding: utf-8 -*-
"""
MessageCard - 消息卡片组件

负责渲染和显示对话消息，支持：
- Markdown 内容渲染（使用 WebEngineView）
- 代码高亮（使用 Pygments）
- 工具调用结果显示
- 流式内容追加
- 用户/助手消息区分

消息结构：
- role: "user" | "assistant" | "system" | "tool"
- content: str | List[Dict]  # 支持多内容块
- tool_calls: List[Dict]     # 工具调用
- tool_call_id: str         # 工具结果关联 ID
"""
import base64
import hashlib
import math
import random
import re
import time
import urllib.parse
from datetime import datetime
from functools import lru_cache
from html import escape
from typing import List, Dict, Any, Optional

import orjson as json
from PyQt5.QtCore import (
    Qt,
    QTimer,
    QTimerEvent,
    pyqtSignal,
    QUrl,
    QVariantAnimation,
    QEasingCurve,
)
from PyQt5.QtGui import (
    QWheelEvent,
    QPainter,
    QPen,
    QColor,
    QBrush,
    QLinearGradient,
    QPainterPath,
)
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QSizePolicy,
    QTextEdit,
)
from markdown import Markdown
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, TextLexer
from qfluentwidgets import (
    FluentIcon,
    ToolTipFilter,
    TransparentToolButton,
)
from qfluentwidgets.components.widgets.card_widget import (
    CardSeparator,
    SimpleCardWidget,
)

from app.core import (
    append_text_block,
    content_to_markdown,
    content_to_text,
    ensure_content_blocks,
)
from app.core.message_content import make_tool_result_block
from app.utils.utils import get_font_family_css, get_icon
from app.utils.design_tokens import current_theme, scale_font_size, Colors, font_size_css, _get_global_font
from app.widgets.render_helpers import (
    render_tool_block,
)

# ======== Markdown 实例 ========
_md_instance = None
ACTION_COLOR_MAP = {
    "jump": "#FFA500",
    "create": "#9370DB",
    "generate": "#32CD32",
    "ask": "#FF6347",
    "view": "#4169E1",
}
DEFAULT_COLOR = "#888888"

# ======== 预编译的正则表达式（提升到模块级别，避免重复编译）=======
_CODE_BLOCK_PATTERN = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_CODE_BLOCK_WITH_LANG_PATTERN = re.compile(r"<pre><code(?:\s+class=\"([^\"]*)\")?>(.*?)</code></pre>", re.DOTALL)
_CONTEXT_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((ask|jump|create|generate|view|session)(?:\|([^)]*))?\)")
_CODE_BLOCK_CODE_PATTERN = re.compile(r"```[\w]*\n")
_CODE_BLOCK_END_PATTERN = re.compile(r"```\n")
_CODE_BLOCK_FINAL_PATTERN = re.compile(r"```")
# 预编译常用正则
_LINK_DETECTION_PATTERN = re.compile(r"\[[^\[\]]+\]\([^)\s]+\)")
_CODE_BLOCK_REMOVE_PATTERN = re.compile(r"```[\s\S]*?```", re.DOTALL)
_MULTIPLE_SPACES_PATTERN = re.compile(r" +")
_PRE_CONTENT_PATTERN = re.compile(r"<pre[^>]*>(.*?)</pre>", re.DOTALL)
_TOOL_NAME_PATTERN = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)
_TOOL_ARGS_LINE_PATTERN = re.compile(r"args:\s*(\{[^}]*\})")
_TOOL_SUCCESS_PATTERN = re.compile(r"^success:\s*(.+?)\s*$", re.MULTILINE)
_TOOL_ID_PATTERN = re.compile(r"^tool_call_id:\s*(.+?)\s*$", re.MULTILINE)
_TOOL_RESULT_PATTERN = re.compile(r"^result:\s*(.*)$", re.MULTILINE)
_NEXT_FIELD_PATTERN = re.compile(r"\n\w+:")
# 性能优化：正则提取后备方案使用的预编译模式
_EXTRACT_KEY_VALUE_PATTERN = re.compile(r'"([^"\\]+)"\s*:\s*"([^"]*)"', re.DOTALL)


def get_markdown_instance():
    global _md_instance
    if _md_instance is None:
        _md_instance = Markdown(
            extensions=["fenced_code", "nl2br", "tables"],
            output_format="html5",
            safe=False,
        )
    return _md_instance


def _unwrap_code_blocks_with_context_links(md_text: str) -> str:
    def replacer(match):
        lang_part = match.group(1) or ""
        code_content = match.group(2)
        if _LINK_DETECTION_PATTERN.search(code_content) and lang_part not in (
                "python"
        ):
            return code_content
        else:
            return (
                f"```{lang_part}\n{code_content}```"
                if lang_part
                else f"```\n{code_content}```"
            )

    return _CODE_BLOCK_PATTERN.sub(replacer, md_text)


def _strip_code_blocks(text: str) -> str:
    """
    移除 markdown 代码块标记和代码内容。
    思考框内不需要代码编辑框，直接显示纯文本。
    """
    # 匹配完整的代码块，包括内容
    text = _CODE_BLOCK_REMOVE_PATTERN.sub("", text)
    # 移除剩余的反引号
    text = text.replace("`", "")
    # 将换行符替换为空格，让内容自然填充，避免多余空行
    text = text.replace("\r\n", " ").replace("\n", " ")
    # 合并多余空格
    text = _MULTIPLE_SPACES_PATTERN.sub(" ", text)
    return text.strip()


# ======== 核心逻辑：保留你的原始代码块样式 ========
def _wrap_code_blocks_with_copy_button_web(html: str) -> str:
    def replacer(match):
        lang = (match.group(1) or "").replace("language-", "").strip()
        code_content_raw = match.group(2) or ""
        # --- 优化后的代码块逻辑 ---
        try:
            copy_text = (
                code_content_raw.replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&amp;", "&")
                .replace("&#39;", "'")
                .replace("&quot;", '"')
            )
        except:
            copy_text = code_content_raw

        b64_copy = base64.b64encode(copy_text.encode("utf-8")).decode("ascii")

        lines = copy_text.splitlines() or [""]
        line_count = len(lines)

        # 高亮代码（获取 <pre> 内部 HTML）
        try:
            lexer = get_lexer_by_name(lang, stripall=False) if lang else TextLexer()
            formatter = HtmlFormatter(
                style="dracula",
                linenos=False,
                noclasses=True,
                cssclass="code-block",
                prestyles="margin:0; padding:0; background:transparent; font-family: Consolas, monospace; font-size:{scale_font_size(13)}px; color:#D4D4D4;",
            )
            highlighted = highlight(copy_text, lexer, formatter)
            # 提取 <pre> 内部内容
            pre_match = _PRE_CONTENT_PATTERN.search(highlighted)
            if pre_match:
                inner_code_html = pre_match.group(1)
            else:
                inner_code_html = escape(copy_text)
        except Exception:
            inner_code_html = escape(copy_text)

        # 生成行号（纯文本，每行一个数字）
        line_numbers_text = "\n".join(str(i + 1) for i in range(line_count))

        # 构建新的代码容器（行号固定 + 代码可横向滚动）
        code_block_html = f"""
        <div class="code-container">
            <div class="line-numbers">{escape(line_numbers_text)}</div>
            <div class="code-content">
                <pre>{inner_code_html}</pre>
            </div>
        </div>
        """

        return f'''
        <div style="
            position: relative;
            margin: 12px 0;
            background: transparent;
            border: 1px solid var(--code-border, rgba(58, 63, 71, 0.6));
            border-radius: 10px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.18), 0 1px 3px rgba(0,0,0,0.2);
            backdrop-filter: blur(8px);
            font-family: Consolas, monospace;
            font-size: {scale_font_size(13)}px;
        ">
            <!-- 顶部工具栏区域 -->
            <div style="
                display: flex; justify-content: space-between; align-items: center;
                padding: 6px 10px; height: 30px; background: rgba(255, 255, 255, 0.03);
                border-bottom: 1px solid var(--code-border, rgba(45, 45, 57, 0.5)); border-radius: 10px 10px 0 0;
            ">
                {f'<span style="color: #FFA500; font-size: {scale_font_size(13)}px; font-weight: bold;">{lang}</span>' if lang else '<span style="color: #888;">Plain Text</span>'}
                <div style="display: flex; gap: 12px; align-items: center; padding-right: 4px;">
                    <button type="button" data-action="save_file" data-lang="{lang}" data-copy="{b64_copy}" class="code-btn" data-tooltip="保存本地文件" style="width: 30px; height: 30px; background: transparent; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0; border-radius: 6px;">
                        <img src="qrc:/icons/导入.svg" style="width:22px; height:22px; pointer-events: none;" />
                    </button>
                    <button type="button" data-action="copy" data-copy="{b64_copy}" class="code-btn" data-tooltip="复制代码" style="width: 30px; height: 30px; background: transparent; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0; border-radius: 6px;">
                        <img src="qrc:/icons/复制.svg" style="width:22px; height:22px; pointer-events: none;" />
                    </button>
                </div>
            </div>
            <!-- 可横向滚动的代码区域 -->
            <div style="
                padding: 8px 0 0 0;
                border-radius: 0 0 10px 10px;
            ">
                {code_block_html}
            </div>
        </div>
        '''

    return _CODE_BLOCK_WITH_LANG_PATTERN.sub(replacer, html)


def _sanitize_incomplete_markdown(md_text: str) -> str:
    if not md_text:
        return ""
    # 只处理 markdown 代码块的不完整情况
    # 不再删除尾随的 <，因为它可能是 HTML/工具标签的一部分
    if md_text.count("```") % 2 == 1:
        md_text += "\n```"
    return md_text


def _get_think_block_styles() -> str:
    """获取思考块的全局字体样式"""
    return f"{get_font_family_css()} font-size: {scale_font_size(13)}px;"


def _render_think_block(content: str, completed: bool = True) -> str:
    status_text = "💡 思考过程" if completed else "🧠 正在思考..."
    expanded = not completed

    max_preview = 40
    content_preview = content.strip().replace("\n", " ")[:max_preview]
    if len(content.strip().replace("\n", " ")) > max_preview:
        content_preview += "..."

    block_seed = f"{content}|{completed}"
    block_key = "think-" + hashlib.sha1(block_seed.encode("utf-8")).hexdigest()[:12]
    expanded_attr = "true" if expanded else "false"
    body_style = ' style="height:auto; opacity:1;"' if expanded else ""

    # 思考内容不需要渲染代码编辑框，移除代码块标记
    content = _strip_code_blocks(content)
    content = escape(content)

    # 获取全局字体样式
    font_style = _get_think_block_styles()

    return f"""<div class="cm-collapsible think-block" data-block-key="{block_key}" data-expanded="{expanded_attr}">
    <button type="button" class="cm-collapsible__summary think-block__summary" aria-expanded="{expanded_attr}" style="{font_style}">
        <span class="cm-collapsible__chevron" aria-hidden="true"></span>
        <span style="white-space: nowrap;">{status_text}</span>
        <span style="color: {Colors.TEXT_SECONDARY}; font-weight: normal; margin-left: 12px; font-size: {scale_font_size(11)}px;">{escape(content_preview)}</span>
    </button>
    <div class="cm-collapsible__body"{body_style}>
        <div class="think-content loading" style="white-space: normal; word-break: break-word; line-height: 1.6; {font_style}">{content}</div>
    </div>
</div>"""


def _render_think_block_lightweight(content: str, completed: bool = True) -> str:
    """轻量级思考块渲染（用于超长思考内容）

    与 _render_think_block 的区别：
    1. 不执行代码块处理（_strip_code_blocks），直接转义
    2. 不生成 block_key hash（节省计算）
    3. 预览文本截取更简单
    """
    status_text = "💡 思考过程" if completed else "🧠 正在思考..."
    expanded = not completed

    # 预览文本：简单截取前50字符
    max_preview = 50
    if len(content) > max_preview:
        content_preview = content[:max_preview].replace("\n", " ") + "..."
    else:
        content_preview = content.replace("\n", " ")

    expanded_attr = "true" if expanded else "false"
    body_style = ' style="height:auto; opacity:1;"' if expanded else ""

    # 轻量级处理：只做转义，不处理代码块
    content_escaped = escape(content)

    # 获取全局字体样式
    font_style = _get_think_block_styles()

    return f"""<div class="cm-collapsible think-block" data-block-key="think-light" data-expanded="{expanded_attr}">
    <button type="button" class="cm-collapsible__summary think-block__summary" aria-expanded="{expanded_attr}" style="{font_style}">
        <span class="cm-collapsible__chevron" aria-hidden="true"></span>
        <span style="white-space: nowrap;">{status_text}</span>
        <span style="color: {Colors.TEXT_SECONDARY}; font-weight: normal; margin-left: 12px; font-size: {scale_font_size(11)}px;">{escape(content_preview)}</span>
    </button>
    <div class="cm-collapsible__body"{body_style}>
        <div class="think-content loading" style="white-space: normal; word-break: break-word; line-height: 1.6; {font_style}">{content_escaped}</div>
    </div>
</div>"""


def _inject_think_cards(md_text: str, completed: bool = True) -> str:
    """注入思考框HTML。

    关键逻辑：<think> 匹配到下一个 <think> 之前的最后一个 </think>，
    避免流式输出时多个 </think> 导致内容泄露。
    """
    parts = []
    i = 0
    while i < len(md_text):
        start_idx = md_text.find("<think>", i)
        if start_idx == -1:
            parts.append(md_text[i:])
            break
        parts.append(md_text[i:start_idx])

        think_start = start_idx + len("<think>")

        # 确定搜索边界：到下一个 <think> 或文本结尾
        next_think = md_text.find("<think>", think_start)
        search_end = next_think if next_think != -1 else len(md_text)

        # 在边界内查找最后一个 </think>（处理多个 </think> 的情况）
        end_idx = md_text.rfind("</think>", think_start, search_end)

        if end_idx != -1:
            content = md_text[think_start:end_idx]
            if content.strip():
                parts.append(_render_think_block(content, completed=True))
            # 空思考块跳过渲染，避免页面末尾遗留空折叠框
            i = end_idx + len("</think>")
        else:
            # 未闭合：内容截取到边界处，避免吞掉后续 <think>
            content = md_text[think_start:search_end]
            if content.strip():
                parts.append(_render_think_block(content, completed=False))
            # 空且未闭合也跳过
            i = search_end
    return "".join(parts)


def _render_tool_block_content(content: str) -> str:
    """
    渲染工具块内容为HTML。

    解析格式：
    <tool>
    name: xxx
    args: {JSON}  <- 可能跨行，需要正确处理嵌套 JSON
    result: xxx   <- 可能跨行
    success: true
    tool_call_id: xxx
    </tool>
    """
    tool_name = ""
    tool_args_str = ""
    tool_result = ""
    tool_success = True
    tool_call_id = None

    content = content.strip()

    # ========== 解析 name ==========
    name_match = _TOOL_NAME_PATTERN.search(content)
    if name_match:
        tool_name = name_match.group(1).strip()

    # ========== 解析 args（需要正确处理嵌套 JSON 和数组）==========
    args_start = content.find("args:")
    result_search_start = 0  # 默认值
    tool_args_str = ""

    if args_start != -1:
        brace_start = content.find("{", args_start)
        if brace_start != -1:
            # 找到最外层的 } 或 ]（结束 JSON/数组）
            depth = 0
            i = brace_start
            in_string = False
            
            while i < len(content):
                c = content[i]
                
                # 字符串内不计入深度
                if in_string:
                    if c == '\\':
                        i += 2
                        continue
                    elif c == '"':
                        in_string = False
                    i += 1
                    continue
                
                if c == '"':
                    in_string = True
                    i += 1
                    continue
                
                if c == '{' or c == '[':
                    depth += 1
                elif c == '}' or c == ']':
                    depth -= 1
                    if depth == 0:
                        tool_args_str = content[brace_start:i + 1]
                        result_search_start = i + 1
                        break
                i += 1
            
            # 如果没有找到闭合（JSON 不完整），取已接收的部分
            if not tool_args_str and brace_start >= 0:
                tool_args_str = content[brace_start:]
                result_search_start = i
        else:
            line = content[args_start:].split("\n")[0]
            tool_args_str = line[args_start + 5:].strip()
            result_search_start = args_start + len(line)
    else:
        # 没有找到 args:，尝试直接解析整个 JSON 对象
        brace_start = content.find("{")
        if brace_start >= 0:
            tool_args_str = content[brace_start:]
    
    # ========== 解析 success ==========
    success_match = _TOOL_SUCCESS_PATTERN.search(content)
    if success_match:
        tool_success = success_match.group(1).strip().lower() == "true"

    # ========== 解析 tool_call_id ==========
    id_match = _TOOL_ID_PATTERN.search(content)
    if id_match:
        tool_call_id = id_match.group(1).strip()

    # ========== 解析 result ==========
    # 关键：从 result: 之后开始搜索，而不是从 result_search_start
    result_start = content.find("result:")
    if result_start >= 0:
        result_after = content[result_start + 7:]  # 跳过 "result:"
        # 找到 result 内容的结束位置（下一个字段之前）
        next_field = _NEXT_FIELD_PATTERN.search(result_after)
        if next_field:
            tool_result = result_after[:next_field.start()].strip()
        else:
            tool_result = result_after.strip()
    else:
        tool_result = ""

    # 转义 result 中的换行符（参数预览和表格不支持多行显示）
    tool_result = tool_result.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")

    # ========== 解析 args JSON 为字典 ==========
    args_dict = {}
    if tool_args_str:
        # 1. 尝试完整 JSON 解析
        try:
            args_dict = json.loads(tool_args_str)
            if not isinstance(args_dict, dict):
                args_dict = {}
        except json.JSONDecodeError:
            # JSON 解析失败，可能是因为不完整，尝试智能修复
            fixed_args_str = tool_args_str.strip()
            # 如果是未闭合，尝试补全括号
            if fixed_args_str.startswith('{') and not fixed_args_str.endswith('}'):
                fixed_args_str += '}'
                try:
                    args_dict = json.loads(fixed_args_str)
                    if not isinstance(args_dict, dict):
                        args_dict = {}
                except json.JSONDecodeError:
                    # 补全后还是失败，再尝试正则提取
                    args_dict = _extract_args_by_regex(tool_args_str)
            else:
                # JSON 解析失败，尝试使用正则提取参数
                args_dict = _extract_args_by_regex(tool_args_str)
    else:
        # 没有 args，尝试从整个 content 中提取参数
        args_dict = _extract_args_by_regex(content)

    # 转义参数中的换行符（参数预览和表格不支持多行显示）
    for key in args_dict:
        if isinstance(args_dict[key], str):
            args_dict[key] = args_dict[key].replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return render_tool_block(
        tool_name, args_dict, tool_result, tool_success, collapsed=True,
        tool_call_id=tool_call_id
    )


def _find_string_end(s, start):
    """从 start 位置开始，找到字符串真正结束的位置
    
    规则：只有当引号后面紧跟 , 或 } 或 ] 或 : 时，才认为是字符串结束
    这避免了把字符串内容中的引号误认为是结束
    """
    i = start
    n = len(s)
    while i < n:
        c = s[i]
        if c == '\\':
            # 转义序列，跳过下一个字符
            i += 2
        elif c == '"':
            # 检查后面是否是真正的分隔符
            next_i = i + 1
            # 跳过空白
            while next_i < n and s[next_i] in ' \t\n\r':
                next_i += 1
            if next_i < n:
                next_c = s[next_i]
                # 只有后面是这些字符才是真正结束：, } ] 或 : (key后面的值结束时)
                if next_c in ',}:]':
                    return i
            i += 1
        else:
            i += 1
    return i


def _parse_json_partial(json_str: str) -> dict:
    """部分 JSON 解析 - 在 JSON 不完整时尽可能提取参数"""
    args = {}
    i = 0
    n = len(json_str)

    while i < n:
        c = json_str[i]

        # 跳过空白
        if c in ' \t\n\r':
            i += 1
            continue

        # 期待 "key"
        if c != '"':
            i += 1
            continue

        # 解析 key
        key_end = _find_string_end(json_str, i + 1)
        key = json_str[i+1:key_end]
        i = key_end + 1

        # 跳过空白和冒号
        while i < n and json_str[i] in ' \t\n\r:':
            i += 1
        if i >= n:
            break

        c = json_str[i]

        # 解析 value
        if c == '"':
            value_end = _find_string_end(json_str, i + 1)
            value = json_str[i+1:value_end]
            i = value_end + 1
            # 处理转义（简化处理）
            value = value.replace('\\"', '"').replace('\\\\', '\\')
            args[key] = value
        elif c == '{':
            obj_start = i
            depth = 1
            i += 1
            while i < n and depth > 0:
                ch = json_str[i]
                if ch == '"':
                    str_end = _find_string_end(json_str, i + 1)
                    i = str_end + 1
                elif ch in '{[':
                    depth += 1
                elif ch in '}]':
                    depth -= 1
                i += 1
            obj_str = json_str[obj_start:i]
            try:
                args[key] = json.loads(obj_str)
            except:
                args[key] = obj_str
        elif c == '[':
            arr_start = i
            depth = 1
            i += 1
            while i < n and depth > 0:
                ch = json_str[i]
                if ch == '"':
                    str_end = _find_string_end(json_str, i + 1)
                    i = str_end + 1
                elif ch in '{[':
                    depth += 1
                elif ch in '}]':
                    depth -= 1
                i += 1
            arr_str = json_str[arr_start:i]
            try:
                args[key] = json.loads(arr_str)
            except:
                args[key] = arr_str
        elif c.isdigit() or c == '-':
            num_str = c
            i += 1
            while i < n and json_str[i].isdigit() or json_str[i] in '.eE+-':
                num_str += json_str[i]
                i += 1
            try:
                args[key] = float(num_str) if '.' in num_str else int(num_str)
            except:
                args[key] = num_str
        elif i + 4 <= n and json_str[i:i+4] == 'true':
            args[key] = True
            i += 4
        elif i + 5 <= n and json_str[i:i+5] == 'false':
            args[key] = False
            i += 5
        elif i + 4 <= n and json_str[i:i+4] == 'null':
            args[key] = None
            i += 4
        else:
            i += 1

        # 跳过空白和逗号
        while i < n and json_str[i] in ' \t\n\r,':
            i += 1

    return args


def _find_json_bounds(content: str) -> tuple:
    """找到 JSON 对象的起始和结束位置"""
    start = content.find('{')
    if start == -1:
        return -1, -1
    
    depth = 0
    i = start
    in_string = False
    escape_next = False
    
    while i < len(content):
        c = content[i]
        
        if escape_next:
            escape_next = False
            i += 1
            continue
        if c == '\\':
            escape_next = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            i += 1
            continue
        if not in_string:
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    
    return start, -1


def _extract_args_by_regex(content: str) -> dict:
    """
    当 JSON 解析失败时，使用状态机解析任意参数。
    处理包含复杂代码内容的场景（代码中有引号、括号等）。
    """
    if not content:
        return {}
    
    # 方法1: 尝试直接解析整个内容
    content = content.strip()
    try:
        result = json.loads(content)
        if isinstance(result, dict):
            return result
    except:
        pass
    
    # 方法2: 找到 JSON 边界，尝试解析
    start, end = _find_json_bounds(content)
    if start >= 0:
        end_pos = end if end > 0 else len(content)
        json_str = content[start:end_pos]
        try:
            result = json.loads(json_str)
            if isinstance(result, dict):
                return result
        except:
            if end < 0:  # JSON 未闭合，尝试部分解析
                args = _parse_json_partial(json_str)
                if args:
                    return args
    
    # 方法3: 直接部分解析
    args = _parse_json_partial(content)
    return args if args else {}


def _extract_by_regex_fallback(content: str) -> dict:
    """正则提取后备方案 - 很少使用（使用预编译正则）"""
    args = {}
    for match in _EXTRACT_KEY_VALUE_PATTERN.finditer(content):
        key = match.group(1)
        value = match.group(2)
        quote_count = value.count('"')
        if quote_count % 2 != 0:
            continue
        args[key] = value
    return args


def _inject_tool_blocks(md_text: str, completed: bool = True) -> str:
    """注入工具块HTML，类似think块"""
    if not md_text:
        return md_text

    parts = []
    i = 0
    while i < len(md_text):
        start_idx = md_text.find("<tool>", i)
        if start_idx == -1:
            parts.append(md_text[i:])
            break
        parts.append(md_text[i:start_idx])
        end_idx = md_text.find("</tool>", start_idx + len("<tool>"))
        if end_idx != -1:
            content = md_text[start_idx + len("<tool>"): end_idx]
            parts.append(_render_tool_block_content(content))
            i = end_idx + len("</tool>")
        else:
            parts.append(md_text[start_idx:])
            break
    return "".join(parts)


def _inject_hook_blocks(md_text: str, completed: bool = True) -> str:
    """注入 Hook 块 HTML，类似 think 块"""
    if not md_text:
        return md_text

    parts = []
    i = 0
    while i < len(md_text):
        start_idx = md_text.find("<hook ", i)
        if start_idx == -1:
            parts.append(md_text[i:])
            break
        parts.append(md_text[i:start_idx])
        
        # 找到 event 属性
        event_start = md_text.find('event="', start_idx)
        if event_start == -1 or event_start > start_idx + 10:
            # 没有 event 属性，跳过这个位置，继续往后找
            i = start_idx + 6
            continue
        
        event_end = md_text.find('"', event_start + len('event="'))
        if event_end == -1:
            parts.append(md_text[start_idx:])
            break
        
        event_name = md_text[event_start + len('event="'):event_end]
        
        # 找到闭合标签
        end_idx = md_text.find("</hook>", start_idx + len("<hook "))
        if end_idx != -1:
            content = md_text[start_idx + len('<hook '): end_idx]
            # 解析内容（event_name 后面的内容）
            content_start = content.find('>')
            if content_start != -1:
                hook_content = content[content_start + 1:].strip()
            else:
                hook_content = content.strip()
            
            # 使用 render_hook_block 渲染
            from app.widgets.render_helpers import render_hook_block
            parts.append(render_hook_block(event_name, hook_content, collapsed=not completed))
            i = end_idx + len("</hook>")
        else:
            # 未闭合的 hook，跳过
            parts.append(md_text[start_idx:])
            break
    return "".join(parts)


# 缓存大小阈值（KB）：超过此大小的文本不缓存，防止内存膨胀
_LRU_CACHE_SIZE_THRESHOLD = 50 * 1024  # 50KB


@lru_cache(maxsize=256)
def _render_markdown_to_html_cached_impl(raw_md: str, reasoning: str) -> str:
    """
    Markdown 转 HTML 的核心渲染函数（带 LRU 缓存）。
    """
    safe_md = _sanitize_incomplete_markdown(raw_md)
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    processed_md = _inject_think_cards(safe_md, True)
    processed_md = _inject_tool_blocks(processed_md, True)
    processed_md = _inject_hook_blocks(processed_md, True)

    try:
        md = get_markdown_instance()
        md.reset()
        html_content = md.convert(processed_md)
        return _wrap_code_blocks_with_copy_button_web(html_content)
    except Exception:
        return f"<pre>{escape(raw_md)}</pre>"


def _render_markdown_to_html_cached(raw_md: str, reasoning: str) -> str:
    """
    带内存保护的 Markdown 渲染函数。
    - 对于超过阈值的文本，跳过缓存直接渲染
    - 保持 LRU 缓存以提高重复内容的性能
    """
    # 添加思考块内容
    if reasoning:
        raw_md = _render_think_block(reasoning, completed=True) + raw_md

    # 大文本跳过缓存，防止内存膨胀
    text_size = len(raw_md.encode('utf-8'))
    if text_size > _LRU_CACHE_SIZE_THRESHOLD:
        # 大文本直接渲染，绕过缓存
        # 临时禁用缓存
        original_cache_info = _render_markdown_to_html_cached_impl.cache_info()
        _render_markdown_to_html_cached_impl.cache_clear()
        try:
            return _render_markdown_to_html_cached_impl(raw_md, reasoning)
        finally:
            # 恢复缓存状态
            pass

    return _render_markdown_to_html_cached_impl(raw_md, reasoning)

    try:
        md = get_markdown_instance()
        md.reset()
        html_content = md.convert(processed_md)
        return _wrap_code_blocks_with_copy_button_web(html_content)
    except Exception:
        return f"<pre>{escape(raw_md)}</pre>"


# ======== 欢迎卡片随机 Tips ========
WELCOME_TIPS = [
    # ===== 文件与输入 =====
    "💡 拖拽文件到输入框即可快速分析",
    "💡 Shift+Enter 换行，Enter 发送消息",
    "💡 按 ↑/↓ 键可浏览历史输入记录",

    # ===== 会话管理 =====
    "💡 Ctrl+N 快速新建对话，Ctrl+L 清空当前会话",
    "💡 历史会话自动保存，关闭窗口也不丢失",
    "💡 长对话会自动启用「上下文压缩」优化 Token",

    # ===== 项目功能 =====
    "💡 点击顶部项目名称可切换/新建/归档项目，不同项目数据隔离",
    "💡 项目笔记自动关联当前项目，切换项目自动切换笔记内容",
    "💡 关键文档中添加文件夹可作为工具的工作目录，相对路径以此为准",

    # ===== 模型与参数 =====
    "💡 点击模型名称可快速切换大模型",
    "💡 模型参数影响回复风格（温度/最大Token），多试试找到你的风格",
    "💡 不同智能体擅长不同任务：Plan 规划、Build 构建、Explore 探索",

    # ===== 技能系统 =====
    "💡 输入 @ 可快速选择技能，触发 AI 专项能力",
    "💡 @brainstorming 集思广益，@writing-plans 制定计划",
    "💡 @git-commit 自动分析改动生成规范提交信息",
    "💡 @skill-creator 创建新的自定义技能扩展",
    "💡 @minimax-image-understanding 分析图片内容",

    # ===== 代码与文件 =====
    "💡 代码块右上角有复制和保存按钮，点击即可",
    "💡 工具执行结果可点击「查看差异」对比文件修改",
    "💡 工具悬浮框会显示正在执行的工具，点击可查看详情",
    "💡 用户卡片的撤销按钮可以单独撤销单个编辑操作",
    "💡 用户卡片的撤销按钮会将会话重置到对应卡片之前",

    # ===== 窗口与布局 =====
    "💡 右上角「新建窗口」按钮可创建并发会话，多任务同时进行",
    "💡 右上角「分支」按钮可复制当前会话到新窗口",
    "💡 右下角可展开历史会话卡片，点击继续历史对话",

    # ===== 高级功能 =====
    "💡 记忆管理让 AI 更懂你的偏好和习惯",
    "💡 点击上下文指示器可查看 Token 使用详情",
    "💡 子智能体可协助处理复杂任务，观察其工作过程",

    # ===== MCP 系统 =====
    "💡 在系统设置中配置 MCP Server，可扩展 AI 的工具能力",
    "💡 MCP 工具自动获取工具信息，连接后即可直接调用",
    "💡 通过 npx -y @modelcontextprotocol/server-filesystem 可让 AI 读写本地文件",
]

# ======== 欢迎卡片欢迎语 ========
WELCOME_GREETINGS = [
    "你好！我是 Drifox 飘狐 🦊",
    "嗨！有什么我可以帮你的吗？",
    "欢迎回来！今天想聊点什么？",
    "你好！随时可以问我问题或让我帮忙处理任务",
    "嗨！准备好一起探索了吗？",
    "欢迎！需要帮忙分析什么吗？",
    "你好！可以帮你总结、分析、生成内容哦！",
    "Drifox 为你准备了最近的会话记录，点击即可继续之前的对话 👇",
    "欢迎使用 Drifox 飘狐！我是你的智能助手 🚀",
    "嗨！我是你的 AI 搭档，有问题尽管问 🤖",
]


def get_random_tip() -> str:
    """获取随机 Tips"""
    return random.choice(WELCOME_TIPS)


def get_random_greeting() -> str:
    """获取随机欢迎语"""
    return random.choice(WELCOME_GREETINGS)


def _inject_context_links(md_text: str) -> str:
    """将 [文本](ask/jump/create/generate/view/session) 转换为胶囊样式的追问标签

    session 类型格式：[文本](session|session_id|last_time)
    last_time 如果为空则不显示
    """

    def replacer(match):
        content = match.group(1)
        action = match.group(2)
        extra = match.group(3) or ""

        if action == "session":
            # session 格式：session_id|last_time
            parts = extra.split("|")
            session_id = parts[0].strip() if parts else ""
            last_time = parts[1].strip() if len(parts) > 1 else ""

            # 如果有 last_time，追加显示
            if last_time:
                display_content = f'{content}<span class="session-time">{last_time}</span>'
            else:
                display_content = content

            attrs = f'data-type="session" data-session-id="{escape(session_id)}" data-action="session"'
            if last_time:
                attrs += f' data-last-time="{escape(last_time)}"'
            return f'<span class="context-tag session-tag" {attrs}>{display_content}</span>'

        return f'<span class="context-tag" data-type="{action}" data-content="{escape(content)}" data-action="{action}">{content}</span>'

    return _CONTEXT_LINK_PATTERN.sub(replacer, md_text)


# ======== WebViewer ========
class ConsoleMonitorPage(QWebEnginePage):
    codeActionRequested = pyqtSignal(str, str)
    contextActionRequested = pyqtSignal(str, str)
    heightReported = pyqtSignal(int)
    contentReady = pyqtSignal()
    toolDiffRequested = pyqtSignal(str)  # tool_call_id
    subAgentLogRequested = pyqtSignal(str)  # task_ids (comma-separated)
    saveFileRequested = pyqtSignal(str, str)  # code, lang

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        msg = message.strip()
        if msg == "pywebview_ready":
            self.contentReady.emit()
        elif msg.startswith("pywebview_height:"):
            try:
                self.heightReported.emit(int(float(msg.split(":")[1])))
            except:
                pass
        elif msg.startswith("pywebview_action:"):
            if "context|||" in msg:
                try:
                    parts = msg.split("|||")
                    self.contextActionRequested.emit(
                        urllib.parse.unquote(parts[1]), urllib.parse.unquote(parts[2])
                    )
                except:
                    pass
            elif "context_lost" in msg:
                self._handle_context_lost()
            elif "open_url:" in msg:
                try:
                    url_str = msg.split("open_url:", 1)[1]
                    from PyQt5.QtGui import QDesktopServices
                    from PyQt5.QtCore import QUrl

                    QDesktopServices.openUrl(QUrl(url_str))
                except:
                    pass
            elif "tool_diff:" in msg:
                # 处理工具差异对比请求
                try:
                    tool_call_id = msg.split("tool_diff:", 1)[1]
                    self.toolDiffRequested.emit(tool_call_id)
                except Exception:
                    pass
            elif "subagent_log:" in msg:
                # 处理子智能体日志查看请求
                try:
                    task_ids = msg.split("subagent_log:", 1)[1]
                    self.subAgentLogRequested.emit(task_ids)
                except Exception:
                    pass
            elif "save_file:" in msg:
                # 处理保存文件请求
                try:
                    parts = msg.split("save_file:", 1)[1]
                    # 格式: b64_code:lang
                    sub_parts = parts.rsplit(":", 1)
                    if len(sub_parts) == 2:
                        b64_code, lang = sub_parts
                        code = base64.b64decode(b64_code).decode("utf-8")
                        self.saveFileRequested.emit(code, lang)
                except Exception:
                    pass
            else:
                try:
                    p = msg.split(":")
                    self.codeActionRequested.emit(
                        base64.b64decode(p[2]).decode("utf-8"), p[1]
                    )
                except:
                    pass

    def _handle_context_lost(self):
        self.contentReady.emit()


class CodeWebViewer(QWebEngineView):
    contentHeightChanged = pyqtSignal(int)
    codeActionRequested = pyqtSignal(str, str)
    contextActionRequested = pyqtSignal(str, str)
    toolDiffRequested = pyqtSignal(str)  # tool_call_id
    subAgentLogRequested = pyqtSignal(str)  # task_ids (comma-separated)
    saveFileRequested = pyqtSignal(str, str)  # code, lang
    # WebEngine 上下文丢失信号
    contextLost = pyqtSignal()
    contextRestored = pyqtSignal()
    needRecreate = pyqtSignal()  # 需要完全重建控件（恢复失败时）

    # WebEngine 最大尺寸限制，防止 GPU 内存溢出
    # macOS GPU 对过大离屏缓冲分配失败，所以需要保守限制
    MAX_WIDTH = 1800
    MAX_HEIGHT = 4000

    def __init__(self, parent=None):
        super().__init__(parent)
        from typing import List
        self._markdown_text = ""
        self._streaming = True
        self._is_js_ready = False
        self._last_rendered_html = ""
        self._last_rendered_markdown = ""
        self._lazy_markdown_cb = None  # 懒回调：渲染时才生成 markdown，避免高频 content_to_markdown
        self._min_render_interval = 50
        self._height_report_pending = False
        self._context_lost = False  # 上下文丢失标志
        self._context_lost_count = 0  # 上下文丢失次数统计
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(100)
        self._resize_debounce_timer.timeout.connect(self._do_resize_check)
        # 性能优化：resize 锁，防止 resize 期间频繁报告高度
        self._resize_locked = False
        self._resize_unlock_timer = QTimer(self)
        self._resize_unlock_timer.setSingleShot(True)
        self._resize_unlock_timer.setInterval(150)  # resize 结束后 150ms 再报告高度
        self._resize_unlock_timer.timeout.connect(self._on_resize_unlock)

        # 1. 渲染定时器
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._perform_update)

        # 2. Resize 定时器 (修复 Crash 的关键：作为成员变量，随 self 销毁)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(50)
        self._resize_timer.timeout.connect(self._safe_report_height)

        self._page = ConsoleMonitorPage(self)
        self.setPage(self._page)
        # 透明背景
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.page().setBackgroundColor(Qt.transparent)
        self.setContextMenuPolicy(Qt.NoContextMenu)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(40)

        self._page.codeActionRequested.connect(self.codeActionRequested.emit)
        self._page.contextActionRequested.connect(self.contextActionRequested.emit)
        self._page.heightReported.connect(self._on_height_reported)
        self._page.contentReady.connect(self._on_js_ready)
        self._page.toolDiffRequested.connect(self.toolDiffRequested.emit)
        self._page.subAgentLogRequested.connect(self.subAgentLogRequested.emit)
        self._page.saveFileRequested.connect(self.saveFileRequested.emit)

        self._load_skeleton()

    def _handle_context_lost(self):
        """JavaScript 报告上下文丢失"""
        if not self._context_lost:
            self._context_lost = True
            self._context_lost_count += 1
            self.contextLost.emit()
            
            # 如果已经丢失超过1次，直接请求重建
            if self._context_lost_count > 1:
                self.needRecreate.emit()
                return
            
            # 尝试恢复上下文
            self._schedule_context_restore()

    def _schedule_context_restore(self):
        """延迟恢复 WebEngine 上下文"""
        QTimer.singleShot(500, self._try_restore_context)

    def _try_restore_context(self):
        """尝试恢复 WebEngine 上下文"""
        try:
            # 重新加载骨架 HTML
            self._is_js_ready = False
            self._load_skeleton()
            self._context_lost = False
            self.contextRestored.emit()
            # 重新渲染内容
            if self._markdown_text:
                self._schedule_render(immediate=True)
        except Exception as e:
            print(f"Context restore failed: {e}")
            # 恢复失败，请求重建
            self.needRecreate.emit()

    def event(self, event):
        """拦截 WebEngine 事件"""
        # 处理上下文丢失
        if event.type() == QTimerEvent and hasattr(self, '_context_lost_timer'):
            pass
        return super().event(event)

    def wheelEvent(self, event: QWheelEvent):
        # 获取滚动条（向上递归找 QScrollArea chat_scroll_area）
        try:
            widget = self
            # 一直向上遍历父控件直到找到 chat_scroll_area
            for _ in range(5):  # 最多找5层
                if hasattr(widget, 'chat_scroll_area'):
                    break
                parent_widget = widget.parent()
                if parent_widget is None:
                    break
                widget = parent_widget
            
            if hasattr(widget, 'chat_scroll_area'):
                scroll_area = getattr(widget, 'chat_scroll_area')
                if scroll_area:
                    vbar = scroll_area.verticalScrollBar()
                    if vbar and vbar.minimum() != vbar.maximum():
                        # 让外部 ScrollArea 滚动
                        delta = event.angleDelta().y()
                        vbar.setValue(vbar.value() - delta // 2)
                        event.accept()  # 标记事件已处理
                        return
        except Exception:
            pass

        super().wheelEvent(event)

    def setFixedSize(self, *args, **kwargs):
        """限制最大尺寸，防止 GPU 内存溢出"""
        # 计算安全尺寸
        w = args[0] if len(args) > 0 else kwargs.get('width', self.MAX_WIDTH)
        h = args[1] if len(args) > 1 else kwargs.get('height', self.MAX_HEIGHT)

        # 限制最大尺寸
        safe_w = min(w, self.MAX_WIDTH) if isinstance(w, int) else w
        safe_h = min(h, self.MAX_HEIGHT) if isinstance(h, int) else h

        super().setFixedSize(safe_w, safe_h)

    def resize(self, *args, **kwargs):
        """限制 resize 尺寸，防止过大导致 GPU 内存溢出"""
        w = args[0] if len(args) > 0 else kwargs.get('width', self.MAX_WIDTH)
        h = args[1] if len(args) > 1 else kwargs.get('height', self.MAX_HEIGHT)

        # 限制最大尺寸
        safe_w = min(w, self.MAX_WIDTH) if isinstance(w, int) else w
        safe_h = min(h, self.MAX_HEIGHT) if isinstance(h, int) else h

        super().resize(safe_w, safe_h)
    
    def setFixedHeight(self, height):
        """限制最大高度，防止 GPU 内存溢出"""
        safe_h = min(height, self.MAX_HEIGHT)
        super().setFixedHeight(safe_h)
    
    def setFixedWidth(self, width):
        """限制最大宽度，防止 GPU 内存溢出"""
        safe_w = min(width, self.MAX_WIDTH)
        super().setFixedWidth(safe_w)

    def _install_dialog_filter(self):
        """安装事件过滤器，监听对话框显示"""
        from PyQt5.QtWidgets import QApplication

        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        # 监听对话框显示/激活事件
        event_type = event.type()
        if event_type == 24 or event_type == 9:  # QEvent.Show = 24, QEvent.FocusIn = 9
            obj_class = obj.__class__.__name__
            popup_keywords = [
                "Dialog",
                "Popup",
                "Flyout",
                "InfoBar",
                "Toast",
                "ComboBox",
                "Menu",
                "ToolTip",
            ]
            if any(kw in obj_class for kw in popup_keywords):
                # 降低当前WebView及其父组件的层级
                self.lower()
                parent = self.parent()
                while parent:
                    parent.lower()
                    # 找到 MessageCard 或聊天容器为止
                    if (
                            hasattr(parent, "chat_layout")
                            or parent.__class__.__name__ == "MessageCard"
                    ):
                        break
                    parent = parent.parent()
                # 同时将弹窗提升到最顶层
                if hasattr(obj, "raise_"):
                    obj.raise_()
        return super().eventFilter(obj, event)

    def lower_for_popup(self):
        """降低控件层级，让弹出窗口可以显示在前面"""
        self.lower()
        # 降低父级
        parent_card = self.parent()
        if parent_card:
            parent_card.lower()

    # 安全的高度上报函数
    def _safe_report_height(self):
        try:
            # 再次检查 page 是否存在，避免 C++ 对象已删除错误
            if self.page():
                self._height_report_pending = False
                self.page().runJavaScript("reportHeight();")
        except RuntimeError:
            # 捕获可能的 "wrapped C/C++ object has been deleted"
            pass

    def _do_resize_check(self):
        # 如果处于 resize 锁定状态，跳过 height 报告
        if self._resize_locked:
            return
        try:
            if self.page():
                self.page().runJavaScript("reportHeight();")
        except RuntimeError:
            pass

    def _on_resize_unlock(self):
        """resize 结束后触发高度报告"""
        self._resize_locked = False
        self._do_resize_check()

    def _on_height_reported(self, h):
        self._height_report_pending = False
        final_h = h + 2
        if abs(self.height() - final_h) > 2:
            self.contentHeightChanged.emit(final_h)

    def _on_js_ready(self):
        self._is_js_ready = True
        if self._markdown_text:
            self._schedule_render(immediate=True)

    def _load_skeleton(self):
        # 获取系统字体
        font_family = "Segoe UI, sans-serif"
        try:
            from app.utils.config import Settings
            settings = Settings.get_instance()
            font_family = settings.llm_font_family.value
            if not font_family:
                font_family = settings.canvas_font_selected.value or "Segoe UI, sans-serif"
        except Exception:
            pass

        self._viewer_font_family = font_family
        self._viewer_font_css = f"{get_font_family_css()} font-family: {font_family}, sans-serif; font-size: {scale_font_size(14)}px;"

        tag_css = []
        for act, col in ACTION_COLOR_MAP.items():
            tag_css.append(
                f'.context-tag[data-type="{act}"] {{ background: {col}15; border-color: {col}60; color: {col}; }}'
            )
            tag_css.append(
                f'.context-tag[data-type="{act}"]:hover {{ background: {col}30; border-color: {col}; }}'
            )

        cdn_libs = """
        <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
        """

        scrollbar_css = """
            /* 统一滚动条样式 - 深色模式适配 */
            ::-webkit-scrollbar {
                width: 6px;
                height: 6px;
            }
            ::-webkit-scrollbar-track {
                background: #1a1f2e;
                border-radius: 3px;
                margin: 2px 0;
            }
            ::-webkit-scrollbar-track:hover {
                background: #1e2435;
            }
            ::-webkit-scrollbar-thumb {
                background: #3a3f50;
                border-radius: 3px;
                min-height: 24px;
            }
            ::-webkit-scrollbar-thumb:hover {
                background: #4a4f62;
            }
            ::-webkit-scrollbar-thumb:active {
                background: #5a5f72;
            }
            ::-webkit-scrollbar-corner {
                background: #1a1f2e;
            }
            /* Firefox 滚动条 */
            * {
                scrollbar-width: thin;
                scrollbar-color: #3a3f50 #1a1f2e;
            }
        """
        theme = current_theme()
        body_font_size = scale_font_size(14)
        code_font_size = scale_font_size(13)
        tag_font_size = scale_font_size(12)
        small_font_size = scale_font_size(11)
        tiny_font_size = scale_font_size(10)
        mono_font = f"{_get_global_font()}, Consolas, monospace"
        font_family = _get_global_font()

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            {cdn_libs}
            <style>
                :root {{
                    --bg: transparent;
                    --panel: {theme["card_bg_solid"]};
                    --panel-elevated: {theme["card_bg_solid"]};
                    --panel-soft: {theme["content_bg"]};
                    --border: {theme["border"]};
                    --border-strong: {theme["border_accent"]};
                    --text: {theme["text_primary"]};
                    --text-secondary: {theme["text_secondary"]};
                    --text-muted: {theme["text_muted"]};
                    --accent: {theme["accent"]};
                    --accent-warm: {theme["accent_warm"]};
                    --code-bg: transparent;
                    --code-toolbar: rgba(255, 255, 255, 0.03);
                    --code-border: #2a3447;
                    --success: #5fd18c;
                    --danger: #ff7b7b;
                }}
                html {{ overflow: hidden; }}
                body {{
                    background: var(--bg) !important;
                    color: var(--text);
                    {self._viewer_font_css}
                    margin: 0; 
                    padding: 6px 14px; 
                    max-height: {self.MAX_HEIGHT}px;
                    overflow-x: hidden;
                    overflow-y: auto;
                }}
                {scrollbar_css}

                #content-placeholder {{ color: var(--text); }}
                #content-placeholder * {{ color: inherit; }}
                h1, h2, h3, h4, h5, h6 {{ color: #FFFFFF !important; font-weight: 700; letter-spacing: 0.01em; }}
                h1 {{ font-size: 1.45em; margin: 12px 0 8px; }}
                h2 {{ font-size: 1.25em; margin: 10px 0 6px; }}
                h3 {{ font-size: 1.1em; margin: 8px 0 4px; }}
                p {{ margin: 8px 0; color: var(--text-secondary); }}
                a {{ color: var(--accent) !important; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
                ul, ol {{ margin: 8px 0; padding-left: 24px; }}
                li {{ margin: 4px 0; color: var(--text-secondary); }}
                strong {{ color: #FFFFFF !important; font-weight: 600; }}
                em {{ color: #c4cedd !important; font-style: italic; }}
                code:not(.code-content *):not(pre code) {{ 
                    background: rgba(102, 198, 255, 0.12) !important; 
                    color: #9bddff !important;
                    padding: 2px 6px; 
                    border-radius: 5px; 
                    font-family: {mono_font};
                    font-size: {code_font_size}px;
                }}
                hr {{ border: none; border-top: 1px solid var(--border); margin: 14px 0; }}

                /* 优化：移除首尾元素的边距，彻底消除多余空白 */
                #content-placeholder > :first-child {{ margin-top: 0 !important; }}
                #content-placeholder > :last-child {{ margin-bottom: 0 !important; }}

                /* 优化：紧凑的段落间距 */
                p {{ margin: 8px 0; }}

                table:not(.code-table) {{
                    width: 100%;
                    border-collapse: collapse;
                    margin: 10px 0;
                    background: transparent;
                    border: 1px solid var(--border);
                    border-radius: 10px;
                    overflow: hidden;
                    font-family: '{font_family}', sans-serif;
                    font-size: {body_font_size}px;
                }}
                table:not(.code-table) th {{
                    background: rgba(255, 255, 255, 0.04);
                    padding: 8px 12px;
                    text-align: left;
                    font-weight: 600;
                    color: #fff !important;
                    border-bottom: 1px solid var(--border-strong);
                }}
                table:not(.code-table) td {{
                    padding: 8px 12px;
                    border-bottom: 1px solid var(--border);
                    color: var(--text-secondary) !important;
                }}
                table:not(.code-table) tr:nth-child(even) {{ background: rgba(255, 255, 255, 0.02); }}
                table:not(.code-table) tr:hover {{ background: rgba(255, 255, 255, 0.05); }}

                .context-tag {{
                    display: inline-block;
                    padding: 2px 8px;
                    margin: 0 2px;
                    border: 1px solid transparent;
                    border-radius: 999px;
                    font-size: {tag_font_size}px;
                    font-weight: 700;
                    cursor: pointer;
                    transition: 0.18s ease;
                    vertical-align: middle;
                }}
                {"".join(tag_css)}

                /* session 历史会话标签样式 */
                .session-tag {{
                    background: rgba(100, 198, 255, 0.12);
                    border-color: rgba(100, 198, 255, 0.5);
                    color: #66c6ff;
                    margin: 4px 4px;
                    min-width: 120px;
                }}
                .session-tag:hover {{
                    background: rgba(100, 198, 255, 0.25);
                    border-color: rgba(100, 198, 255, 0.8);
                }}
                /* session 时间显示在标题下方 */
                .session-tag .session-time {{
                    display: block;
                    font-size: {tiny_font_size}px;
                    font-weight: normal;
                    opacity: 0.6;
                    margin-top: 4px;
                    color: #88d4ff;
                }}

                /* Markdown 表格样式 */
                .session-table {{
                    border-collapse: collapse;
                    width: 100%;
                    margin: 8px 0;
                }}
                .session-table th, .session-table td {{
                    border: 1px solid rgba(100, 198, 255, 0.3);
                    padding: 8px 12px;
                    text-align: left;
                    font-family: '{font_family}', sans-serif;
                    font-size: {body_font_size}px;
                }}
                .session-table th {{
                    background: rgba(100, 198, 255, 0.06);
                    color: #66c6ff;
                    font-weight: 600;
                }}
                .session-table td {{
                    background: transparent;
                    vertical-align: middle;
                }}
                .session-table tr:hover td {{
                    background: rgba(100, 198, 255, 0.04);
                }}

                /* 代码块通用样式 */
                .code-table {{ width: 100%; border-collapse: collapse; }}
                .code-table td {{ padding: 0; vertical-align: top; }}
                .lineno {{ width: 32px; text-align: right; padding-right: 8px !important; color: #606060; border-right: 1px solid #404040; user-select: none; font-size: {small_font_size}px; line-height: 1.5; }}
                /* 优化后的代码块布局：行号固定，代码可横向滚动 */
                .code-container {{
                    display: flex;
                    overflow-x: auto;
                    overflow-y: hidden;
                    background: transparent;
                    font-family: {mono_font};
                    font-size: {code_font_size}px;
                    line-height: 1.5;
                    padding: 0 10px 8px 0;
                    margin: 0;
                }}
                .line-numbers {{
                    flex: 0 0 auto;
                    text-align: right;
                    padding-right: 12px;
                    color: #5b6578;
                    border-right: 1px solid var(--code-border);
                    user-select: none; /* 关键：禁止复制行号 */
                    white-space: pre;
                    min-width: 32px;
                    overflow: hidden;
                }}
                .code-content {{
                    flex: 1;
                    overflow-x: auto;
                    overflow-y: hidden;
                    padding-left: 12px;
                }}
                .code-content pre {{
                    margin: 0 !important;
                    white-space: pre;
                    word-wrap: normal;
                    overflow: visible;
                    background: transparent !important;
                    font-family: {mono_font} !important;
                    font-size: {code_font_size}px !important;
                    line-height: 1.5 !important;
                }}
                .code-line {{ padding-left: 12px !important; white-space: pre; font-family: {mono_font}; }}

                .code-btn:hover {{ background: rgba(255,255,255,0.08) !important; }}

                .cm-collapsible {{
                    overflow: hidden;
                    transform: translateZ(0);
                    backface-visibility: hidden;
                }}
                .cm-collapsible__summary {{
                    width: 100%;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    background: transparent;
                    border: none;
                    text-align: left;
                    cursor: pointer;
                    outline: none;
                    -webkit-tap-highlight-color: transparent;
                }}
                .cm-collapsible__summary:focus-visible {{
                    box-shadow: inset 0 0 0 1px rgba(102, 198, 255, 0.28);
                }}
                .cm-collapsible__chevron {{
                    flex: 0 0 auto;
                    width: 8px;
                    height: 8px;
                    border-right: 1.5px solid currentColor;
                    border-bottom: 1.5px solid currentColor;
                    transform: rotate(45deg);
                    transform-origin: center;
                    transition: transform 180ms ease;
                    margin-left: 2px;
                    opacity: 0.85;
                }}
                .cm-collapsible[data-expanded="true"] .cm-collapsible__chevron {{
                    transform: rotate(225deg);
                }}
                .cm-collapsible__body {{
                    height: 0;
                    opacity: 0;
                    overflow: hidden;
                    will-change: height, opacity;
                    transition: height 250ms cubic-bezier(0.4, 0, 0.2, 1), opacity 200ms ease;
                }}
                .cm-collapsible[data-expanded="true"] .cm-collapsible__body {{
                    opacity: 1;
                }}

                .think-block {{
                    margin: 8px 0;
                    background: transparent;
                    border: 1px solid var(--border);
                    border-radius: 10px;
                    transition: border-color 220ms ease;
                }}
                .think-block[data-expanded="true"] {{
                    border-color: rgba(102, 198, 255, 0.4);
                }}
                .think-block__summary {{
                    padding: 8px 12px;
                    color: var(--text-secondary);
                    font-weight: 600;
                }}
                .think-content {{
                    padding: 10px 12px;
                    border-top: 1px solid var(--border);
                    background: transparent;
                    color: var(--text-muted) !important;
                    font-style: italic;
                    font-size: {code_font_size}px;
                    font-family: '{font_family}', sans-serif;
                    line-height: 1.6;
                }}
                /* 思考内容加载骨架屏动画 */
                .think-content.loading {{
                    background-image: linear-gradient(
                        90deg,
                        rgba(255, 255, 255, 0.02) 25%,
                        rgba(255, 255, 255, 0.05) 50%,
                        rgba(255, 255, 255, 0.02) 75%
                    );
                    background-size: 200% 100%;
                    animation: think-shimmer 1.5s ease-in-out infinite;
                }}
                @keyframes think-shimmer {{
                    0% {{ background-position: 200% 0; }}
                    100% {{ background-position: -200% 0; }}
                }}

                .tool-block {{
                    margin: 8px 0;
                    background: transparent;
                    border: 1px solid var(--border);
                    border-radius: 10px;
                    box-shadow: none;
                    transition: border-color 220ms ease;
                }}
                .tool-block[data-expanded="true"] {{
                    border-color: rgba(95, 209, 140, 0.5);
                }}
                .tool-block__summary {{
                    padding: 8px 12px;
                    color: var(--accent);
                    font-weight: 600;
                    font-size: {code_font_size}px;
                    font-family: '{font_family}', sans-serif;
                    white-space: normal;
                }}
                .tool-expanded-content {{
                    padding: 0;
                }}
                .tool-params-section,
                .tool-result-section {{
                    padding: 0;
                }}
                .tool-section-label {{
                    color: #888;
                    font-size: {small_font_size}px;
                    font-weight: 500;
                    padding: 8px 12px 4px;
                    text-transform: uppercase;
                    letter-spacing: 0.5px;
                }}
                .args-table {{
                    display: flex;
                    flex-direction: column;
                    gap: 0;
                    margin: 0;
                }}
                .args-row {{
                    display: flex;
                    align-items: flex-start;
                    padding: 6px 12px;
                    border-bottom: 1px solid rgba(58, 63, 71, 0.4);
                    font-size: {tag_font_size}px;
                }}
                .args-row:last-child {{
                    border-bottom: none;
                }}
                .args-row.empty {{
                    color: #666;
                    font-style: italic;
                    padding: 8px 12px;
                }}
                .args-key {{
                    flex: 0 0 auto;
                    min-width: 80px;
                    max-width: 120px;
                    color: #9C9C9C;
                    font-weight: 500;
                    margin-right: 12px;
                    word-break: break-word;
                }}
                .args-row.result-success {{
                    border-top: 1px solid rgba(95, 209, 140, 0.3);
                    background: rgba(95, 209, 140, 0.05);
                }}
                .args-row.result-fail {{
                    border-top: 1px solid rgba(244, 67, 54, 0.3);
                    background: rgba(244, 67, 54, 0.05);
                }}
                .args-value {{
                    flex: 1 1 auto;
                    color: #d4d4d4;
                    word-break: break-all;
                    font-family: {mono_font};
                    font-size: {small_font_size}px;
                }}
                .result-content {{
                    padding: 6px 12px 10px;
                    color: #d4d4d4;
                    font-size: {tag_font_size}px;
                    line-height: 1.5;
                    word-break: break-word;
                    font-family: {mono_font};
                    max-height: 400px;
                    overflow-y: auto;
                }}
                .result-empty {{
                    padding: 6px 12px 10px;
                    color: #666;
                    font-style: italic;
                    font-size: {tag_font_size}px;
                }}
                .tool-content {{
                    padding: 10px 12px;
                    border-top: 1px solid var(--border);
                    background: transparent;
                }}
                .tool-content pre {{
                    margin: 0;
                    color: #d8b68d;
                    font-size: {tag_font_size}px;
                    font-family: {mono_font};
                    white-space: pre-wrap;
                    word-break: break-word;
                }}

                .hook-block {{
                    margin: 8px 0;
                    background: transparent;
                    border: 1px solid rgba(0, 188, 212, 0.2);
                    border-left: 3px solid #00BCD4;
                    border-radius: 10px;
                    box-shadow: none;
                    transition: border-color 220ms ease;
                }}
                .hook-block[data-expanded="true"] {{
                    border-color: rgba(0, 188, 212, 0.5);
                }}
                .hook-block__summary {{
                    padding: 8px 12px;
                    color: #00BCD4;
                    font-weight: 600;
                    font-size: {code_font_size}px;
                    font-family: '{font_family}', sans-serif;
                    white-space: normal;
                }}
                .hook-content {{
                    padding: 10px 12px;
                    border-top: 1px solid rgba(0, 188, 212, 0.2);
                    background: transparent;
                    font-family: {mono_font};
                    font-size: {tag_font_size}px;
                    color: #e0e0e0;
                    white-space: pre-wrap;
                    word-break: break-word;
                    line-height: 1.5;
                }}

                blockquote {{
                    border-left: 3px solid var(--accent-warm);
                    background: rgba(255,182,92,0.08);
                    margin: 10px 0;
                    padding: 8px 12px;
                    border-radius: 0 10px 10px 0;
                    color: var(--text-secondary) !important;
                }}
            </style>
        </head>
        <body>
            <div id="content-placeholder"></div>
            <script>
                const collapsibleState = new Map();

                function syncExpandedAttrs(block, expanded) {{
                    block.dataset.expanded = expanded ? 'true' : 'false';
                    const summary = block.querySelector('.cm-collapsible__summary');
                    if (summary) summary.setAttribute('aria-expanded', expanded ? 'true' : 'false');
                    const key = block.dataset.blockKey;
                    if (key) collapsibleState.set(key, expanded);
                }}

                function animateCollapsible(block, expand) {{
                    const body = block.querySelector('.cm-collapsible__body');
                    if (!body) return;

                    const ANIM_DURATION = 220;
                    const startTime = performance.now();
                    const startHeight = body.getBoundingClientRect().height;
                    const startOpacity = expand ? 0 : 1;
                    const endHeight = expand ? body.scrollHeight : 0;
                    const endOpacity = expand ? 1 : 0;

                    // 立即更新展开状态
                    syncExpandedAttrs(block, expand);

                    // 阻止 CSS transition 干扰
                    const isCollapsing = !expand;
                    body.style.transition = 'none';
                    body.style.height = startHeight + 'px';
                    body.style.opacity = startOpacity;
                    // 立即设置 overflow 防止内容泄漏
                    body.style.overflow = 'hidden';
                    // 折叠时立即设置高度，防止视觉抖动
                    if (isCollapsing) body.style.height = '0px';

                    // 强制重绘
                    void body.offsetHeight;

                    // 取消之前的动画
                    if (window._collapsibleAnimId) {{
                        cancelAnimationFrame(window._collapsibleAnimId);
                    }}

                    function tick(now) {{
                        const elapsed = now - startTime;
                        const progress = Math.min(elapsed / ANIM_DURATION, 1);
                        // 使用 easeOutQuad 缓动
                        const eased = 1 - (1 - progress) * (1 - progress);

                        // 折叠时 startHeight 已经是0，currentHeight 计算应该从0开始
                        const currentHeight = isCollapsing 
                            ? startHeight * (1 - eased)  // 从 startHeight 减少到 0
                            : startHeight + (endHeight - startHeight) * eased;
                        const currentOpacity = startOpacity + (endOpacity - startOpacity) * eased;

                        body.style.height = currentHeight + 'px';
                        body.style.opacity = currentOpacity;

                        if (progress < 1) {{
                            window._collapsibleAnimId = requestAnimationFrame(tick);
                        }} else {{
                            // 动画结束：设置最终状态
                            body.style.height = expand ? 'auto' : '0px';
                            body.style.opacity = endOpacity;
                            body.style.overflow = '';
                            // 动画结束后重置高度报告标志
                            _collapsibleHeightReporting = false;
                            // 动画结束后延迟报告高度，确保 CSS transition 完成
                            setTimeout(() => reportHeight(), 80);
                        }}
                    }}

                    window._collapsibleAnimId = requestAnimationFrame(tick);
                }}

                // 折叠动画期间暂停高度报告，避免卡片抖动
                let _collapsibleHeightReporting = false;
                function startCollapsibleAnimation() {{
                    _collapsibleHeightReporting = true;
                }}

                function restoreCollapsibleStates(root) {{
                    root.querySelectorAll('.cm-collapsible').forEach(block => {{
                        const key = block.dataset.blockKey;
                        const expanded = key && collapsibleState.has(key)
                            ? collapsibleState.get(key)
                            : block.dataset.expanded === 'true';
                        const body = block.querySelector('.cm-collapsible__body');
                        syncExpandedAttrs(block, !!expanded);
                        if (body) {{
                            body.style.transition = 'none';
                            if (expanded) {{
                                body.style.height = 'auto';
                                body.style.opacity = '1';
                            }} else {{
                                body.style.height = '0px';
                                body.style.opacity = '0';
                            }}
                            body.offsetHeight;
                            body.style.transition = '';
                        }}
                    }});
                }}

                function updateContent(newHtml) {{
                    const container = document.getElementById('content-placeholder');
                    if (container.innerHTML !== newHtml) {{
                        // 记录当前展开状态的思考块
                        const expandedStates = new Map();
                        container.querySelectorAll('.think-block').forEach(block => {{
                            expandedStates.set(block.dataset.blockKey, block.dataset.expanded === 'true');
                        }});

                        container.innerHTML = newHtml;

                        // 恢复展开状态并移除骨架屏动画
                        container.querySelectorAll('.think-content').forEach(content => {{
                            content.classList.remove('loading');
                        }});

                        restoreCollapsibleStates(container);

                        // 恢复展开状态
                        container.querySelectorAll('.think-block').forEach(block => {{
                            const savedState = expandedStates.get(block.dataset.blockKey);
                            if (savedState !== undefined) {{
                                block.dataset.expanded = savedState ? 'true' : 'false';
                                const body = block.querySelector('.cm-collapsible__body');
                                if (body) {{
                                    body.style.height = savedState ? 'auto' : '0px';
                                    body.style.opacity = savedState ? '1' : '0';
                                }}
                            }}
                        }});

                        if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise();
                        
                        // 自动滚动到 body 底部（流式时新内容在底部）
                        document.body.scrollTop = document.body.scrollHeight;
                        
                        // 使用延迟报告，确保折叠框高度设为 auto 后浏览器布局完成
                        setTimeout(() => reportHeight(), 50);
                    }}
                }}
                function reportHeight() {{
                    const h = document.documentElement.getBoundingClientRect().height;
                    console.log('pywebview_height:' + h);
                }}
                // 防抖报告高度：动画期间暂停报告，只在动画结束后报告最终值
                let _heightReportPending = false;
                function reportHeightDebounced() {{
                    if (_collapsibleHeightReporting) return;  // 动画期间暂停
                    if (_heightReportPending) return;
                    _heightReportPending = true;
                    requestAnimationFrame(() => {{
                        reportHeight();
                        _heightReportPending = false;
                    }});
                }}
                document.addEventListener('click', e => {{
                    const btn = e.target.closest('button[data-action]');
                    if (btn) {{
                        const act = btn.getAttribute('data-action');
                        const b64 = btn.getAttribute('data-copy');
                        const lang = btn.getAttribute('data-lang') || '';
                        if (act === 'copy' && navigator.clipboard) navigator.clipboard.writeText(atob(b64));
                        console.log('pywebview_action:' + act + ':' + b64 + ':' + lang);
                        return;
                    }}
                    const summary = e.target.closest('.cm-collapsible__summary');
                    if (summary) {{
                        const block = summary.closest('.cm-collapsible');
                        if (block) {{
                            // 动画开始前暂停高度报告
                            startCollapsibleAnimation();
                            animateCollapsible(block, block.dataset.expanded !== 'true');
                        }}
                        return;
                    }}
                    const tag = e.target.closest('.context-tag');
                    if (tag) {{
                        var tagType = tag.getAttribute('data-type') || tag.getAttribute('data-action') || '';
                        var sessionId = tag.getAttribute('data-session-id') || '';
                        var tagContent = sessionId || tag.getAttribute('data-content') || tag.getAttribute('data-title') || '';
                        e.stopPropagation();
                        e.preventDefault();
                        console.log('pywebview_action:context|||' + tagContent + '|||' + tagType);
                        return;
                    }}
                    const link = e.target.closest('a');
                    if (link) {{
                        console.log('pywebview_action:link_found:' + link.href);
                    }}
                    if (link && link.href) {{
                        e.preventDefault();
                        console.log('pywebview_action:open_url:' + link.href);
                    }}
                }});
                document.addEventListener('DOMContentLoaded', () => {{
                    console.log('pywebview_ready');
                    reportHeight();
                    // 使用防抖的 ResizeObserver，避免频繁触发高度更新
                    let resizeTimeout = null;
                    new ResizeObserver(() => {{
                        // 动画期间跳过高度报告
                        if (_collapsibleHeightReporting) return;
                        if (resizeTimeout) clearTimeout(resizeTimeout);
                        resizeTimeout = setTimeout(() => requestAnimationFrame(reportHeight), 50);
                    }}).observe(document.body);
                }});
                window.addEventListener('load', () => {{
                    reportHeight();
                }});
                window.addEventListener('webglcontextlost', (e) => {{
                    e.preventDefault();
                    console.log('pywebview_action:context_lost');
                }}, false);
                window.addEventListener('webglcontextrestored', () => {{
                    console.log('pywebview_ready');
                    reportHeight();
                }}, false);
                window.pywebview = {{ reportHeight: reportHeight }};

                // 工具差异对比请求函数
                window._requestToolDiff = function(toolCallId) {{
                    console.log('pywebview_action:tool_diff:' + toolCallId);
                }};

                // 子智能体日志查看请求函数
                window._requestSubAgentLog = function(taskIds) {{
                    console.log('pywebview_action:subagent_log:' + taskIds);
                }};
            </script>
        </body>
        </html>
        """
        self.setHtml(html, QUrl(""))

    def append_chunk(self, text: str):
        if not text:
            return

        self._markdown_text += text

        if not self._is_js_ready:
            return
        if self._streaming and len(text) > 3:
            self._schedule_render(immediate=True)
        else:
            self._schedule_render()

    def _append_text_incremental(self, text: str):
        """增量追加纯文本到 DOM（流式模式），让用户立即看到文字，不等全量渲染。

        在全量渲染（updateContent）到达前先推送纯文本内容，
        避免渲染延迟导致的"卡高先涨、文字后显"问题。
        """
        if not self._is_js_ready or not self.page():
            return
        try:
            # 防御：过滤掉可能出现在正文 chunk 中的 <think> / </think> 标签
            # （防止增量显示标签，全量渲染会正确处理）
            text_clean = text.replace("<think>", "").replace("</think>", "")
            if not text_clean:
                return
            escaped = escape(text_clean)
            js = f"""
            (function() {{
                var c = document.getElementById('content-placeholder');
                if (!c) return;
                var last = c.lastElementChild;
                if (last && last.tagName === 'P') {{
                    last.textContent += {json.dumps(escaped)};
                }} else if (last && last.classList.contains('think-block')) {{
                    // 最后是思考块：追加到思考块之后的新段落
                    var p = document.createElement('p');
                    p.textContent = {json.dumps(escaped)};
                    c.appendChild(p);
                }} else {{
                    var p = document.createElement('p');
                    p.textContent = {json.dumps(escaped)};
                    c.appendChild(p);
                }}
                // 流式增量追加时，让 body 内部滚动到最底部
                document.body.scrollTop = document.body.scrollHeight;
            }})();
            """
            self.page().runJavaScript(js)
        except RuntimeError:
            pass

    def _render_markdown_to_html(self, raw_md: str) -> str:
        """渲染 markdown 到 HTML。
        
        reasoning 现在作为 <think> 标签嵌入在 raw_md 中（由 content_to_markdown 生成），
        与文本、工具结果按实际顺序交错排列，不再需要单独的 _reasoning_blocks 逻辑。
        """
        # 刷新字体（响应系统字体设置变化）
        self._refresh_viewer_font_css()
        
        if not self._streaming:
            # 非流式模式：直接渲染，所有 <think> 都是已完成的
            return _render_markdown_to_html_cached(
                raw_md,
                "",
            )

        # 流式模式：仅在最后一个块是 reasoning 时，去掉其闭合标签
        # 判断标准：markdown 以 </think> 结尾（说明最后一个块恰好是 reasoning）
        streaming_md = raw_md.rstrip()
        if self._streaming and streaming_md.endswith("</think>"):
            # 末尾正好是 reasoning 块的闭合标签，去掉它表示该块尚未完成
            streaming_md = streaming_md[:-len("</think>")].rstrip()

        safe_md = _sanitize_incomplete_markdown(streaming_md)
        safe_md = _unwrap_code_blocks_with_context_links(safe_md)
        safe_md = _inject_context_links(safe_md)
        processed_md = _inject_think_cards(safe_md, self._streaming is False)
        processed_md = _inject_tool_blocks(processed_md, self._streaming is False)
        processed_md = _inject_hook_blocks(processed_md, self._streaming is False)

        try:
            md = get_markdown_instance()
            md.reset()
            html_content = md.convert(processed_md)
            return _wrap_code_blocks_with_copy_button_web(html_content)
        except Exception:
            return f"<pre>{escape(raw_md)}</pre>"

    def _schedule_render(self, immediate: bool = False):
        if not self._is_js_ready:
            return
        if immediate:
            if self._render_timer.isActive():
                self._render_timer.stop()
            self._perform_update()
            return

        # 动态渲染间隔：内容越大渲染越稀疏，减轻 UI 压力
        if self._streaming:
            content_len = len(self._markdown_text)
            if content_len > 50000:
                interval = 200
            elif content_len > 20000:
                interval = 100
            elif content_len > 5000:
                interval = 60
            else:
                interval = 30
        else:
            interval = 40

        if self._render_timer.isActive():
            return
        self._render_timer.start(interval)

    def _refresh_viewer_font(self):
        """刷新 viewer 字体样式，响应系统字体设置变化"""
        if not hasattr(self, '_viewer_font_family'):
            return
        self._refresh_viewer_font_css()
        self._schedule_render(immediate=True)

    def _refresh_viewer_font_css(self):
        """刷新字体 CSS 变量，供 render 使用"""
        if not hasattr(self, '_viewer_font_family'):
            return
        font_family = self._viewer_font_family
        font_css = get_font_family_css()
        body_font_size = scale_font_size(14)
        self._viewer_font_css = f"{font_css} font-family: {font_family}, sans-serif; font-size: {body_font_size}px;"

    def _perform_update(self):
        try:
            if not self.page():
                return
            import time as _t
            _t0 = _t.time()
            # 懒加载：通过回调获取最新 markdown（避免每次 reasoning chunk 都调用 content_to_markdown）
            if self._lazy_markdown_cb:
                _tcb0 = _t.time()
                fresh_md = self._lazy_markdown_cb()
                _tcb = (_t.time() - _tcb0) * 1000
                self._lazy_markdown_cb = None  # 清除回调，避免后续 set_content 重复转换
                if fresh_md == self._last_rendered_markdown:
                    if not self._height_report_pending:
                        self._height_report_pending = True
                        self._resize_timer.start()
                    return
                self._markdown_text = fresh_md
            elif self._markdown_text == self._last_rendered_markdown:
                if not self._height_report_pending:
                    self._height_report_pending = True
                    self._resize_timer.start()
                return

            # 刷新字体 CSS var
            self._refresh_viewer_font_css()

            _tr0 = _t.time()
            html_content = self._render_markdown_to_html(self._markdown_text)
            _tr = (_t.time() - _tr0) * 1000
            self._last_rendered_markdown = self._markdown_text
            if html_content == self._last_rendered_html:
                if not self._height_report_pending:
                    self._height_report_pending = True
                    self._resize_timer.start()
                return

            self._last_rendered_html = html_content
            self._height_report_pending = True
            js_code = f"updateContent({json.dumps(html_content).decode('utf-8')});"
            _tjs0 = _t.time()
            self.page().runJavaScript(js_code)
            _tjs = (_t.time() - _tjs0) * 1000
            _total = (_t.time() - _t0) * 1000
        except RuntimeError:
            pass

    def finish_streaming(self):
        self._streaming = False
        self._schedule_render(immediate=True)

    def get_plain_text(self) -> str:
        return self._markdown_text

    def get_html(self) -> str:
        return self._markdown_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._streaming:
            return

        # 性能优化：使用 resize 锁，阻止 resize 期间频繁报告高度
        if not self._resize_locked:
            self._resize_locked = True
            self._resize_unlock_timer.stop()
            self._resize_unlock_timer.start()

    def wheelEvent(self, event: QWheelEvent):
        # 获取滚动条（向上找 QScrollArea）
        scroll_area = self.parent().parent().parent.chat_scroll_area
        if scroll_area:
            vbar = scroll_area.verticalScrollBar()
            if vbar and vbar.minimum() != vbar.maximum():
                # 让外部 ScrollArea 滚动
                delta = event.angleDelta().y()
                vbar.setValue(vbar.value() - delta // 2)
                event.accept()  # 标记事件已处理
                return

        super().wheelEvent(event)

    def cleanup(self):
        """
        清理 CodeWebViewer 持有的资源，防止内存泄漏。
        应该在删除 viewer 前调用，或者在 deleteLater 中自动调用。
        """
        # 停止所有定时器
        timers_to_stop = [
            self._render_timer,
            self._resize_timer,
            self._resize_debounce_timer,
            self._resize_unlock_timer,
        ]
        for timer in timers_to_stop:
            try:
                timer.stop()
                timer.deleteLater()
            except RuntimeError:
                pass

        # 断开所有信号连接
        try:
            if hasattr(self._page, 'codeActionRequested'):
                self._page.codeActionRequested.disconnect()
            if hasattr(self._page, 'contextActionRequested'):
                self._page.contextActionRequested.disconnect()
            if hasattr(self._page, 'heightReported'):
                self._page.heightReported.disconnect()
            if hasattr(self._page, 'contentReady'):
                self._page.contentReady.disconnect()
            if hasattr(self._page, 'toolDiffRequested'):
                self._page.toolDiffRequested.disconnect()
            if hasattr(self._page, 'subAgentLogRequested'):
                self._page.subAgentLogRequested.disconnect()
            if hasattr(self._page, 'saveFileRequested'):
                self._page.saveFileRequested.disconnect()
        except Exception:
            pass

        # 清理流式输出和渲染缓存
        self._streaming = False
        self._markdown_text = ""
        self._last_rendered_html = ""
        self._last_rendered_markdown = ""
        self._is_js_ready = False

        # 清理上下文状态
        self._context_lost = False
        self._height_report_pending = False
        self._resize_locked = False

        # 清理页面：先加载空白页释放资源
        try:
            self.setHtml("")
        except RuntimeError:
            pass
        
        # 清理页面对象
        try:
            if hasattr(self, '_page'):
                self._page.deleteLater()
                del self._page
        except (RuntimeError, AttributeError):
            pass
        
        # 清理代码块缓存
        if hasattr(self, '_code_block_cache'):
            self._code_block_cache.clear()
            self._code_block_cache = None
        
        # 清理滚动位置
        self._last_scroll_position = 0

    def deleteLater(self):
        self.cleanup()
        super().deleteLater()


class PlainTextViewer(QWidget):
    contentHeightChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._init_ui()
        # 性能优化：添加 resize 防抖定时器
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(50)  # 50ms 防抖
        self._resize_debounce_timer.timeout.connect(self._do_resize_update)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)

        self.text_edit = QTextEdit(self)
        self.text_edit.setReadOnly(True)
        self.text_edit.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_edit.setFrameShape(QTextEdit.NoFrame)
        font_css = get_font_family_css()
        self.text_edit.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                border: none;
                {font_css}
                color: #F5F7FB;
                font-size: {scale_font_size(14)}px;
                line-height: 1.5;
                selection-background-color: rgba(102, 198, 255, 0.28);
            }}
        """)
        layout.addWidget(self.text_edit)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(40)

    def append_chunk(self, text: str):
        self._text += text
        self.text_edit.setPlainText(self._text)
        # 设置文档宽度以确保正确计算换行
        vp_width = self.text_edit.viewport().width()
        if vp_width > 0:
            self.text_edit.document().setTextWidth(vp_width)
        QTimer.singleShot(10, self._update_height)

    def finish_streaming(self):
        QTimer.singleShot(10, self._update_height)

    def get_plain_text(self) -> str:
        return self._text

    def set_text(self, text: str):
        self._text = text
        self.text_edit.setPlainText(text)
        # 设置文档宽度以确保正确计算换行
        vp_width = self.text_edit.viewport().width()
        if vp_width > 0:
            self.text_edit.document().setTextWidth(vp_width)
        QTimer.singleShot(10, self._update_height)

    def _update_height(self):
        """强制 QTextEdit 重新布局后再计算高度"""
        # 先让 QTextEdit 重新布局
        self.text_edit.update()
        self.text_edit.document().markContentsDirty(0, self.text_edit.document().characterCount())

        # 强制更新几何信息
        self.text_edit.ensurePolished()

        doc = self.text_edit.document()
        h = int(doc.size().height()) + 16  # padding

        h = max(40, h)

        if abs(self.height() - h) > 2:
            self.setFixedHeight(h)
            self.contentHeightChanged.emit(h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 性能优化：使用防抖定时器，避免每次 resize 都触发高度计算
        self._resize_debounce_timer.stop()
        self._resize_debounce_timer.start()

    def _do_resize_update(self):
        """防抖后执行高度更新"""
        self._update_height()

    def update_height(self):
        """公开方法，用于外部触发高度重算（跳过防抖，直接更新）"""
        self._resize_debounce_timer.stop()  # 取消待执行的防抖
        self._update_height()

    def cleanup(self):
        """
        清理 PlainTextViewer 持有的资源，防止内存泄漏。
        """
        try:
            self._resize_debounce_timer.stop()
            self._resize_debounce_timer.deleteLater()
        except RuntimeError:
            pass

        # 清理文本缓存
        self._text = ""

        # 清理 QTextEdit（关键修复：先清空内容，再释放文档）
        if hasattr(self, 'text_edit') and self.text_edit:
            try:
                self.text_edit.clear()
                # 释放文档以释放内存
                doc = self.text_edit.document()
                doc.setPlainText("")
                # 清空undo/redo历史
                doc.setUndoRedoEnabled(False)
            except RuntimeError:
                pass
        
        # 清理引用
        self.text_edit = None


class MessageCard(SimpleCardWidget):
    heightChanged = pyqtSignal(int)
    deleteRequested = pyqtSignal()
    undoRequested = pyqtSignal()
    actionRequested = pyqtSignal(str, str)
    contextActionRequested = pyqtSignal(str, str)
    optionSelected = pyqtSignal(dict)
    interventionRequested = pyqtSignal(dict)
    toolDiffRequested = pyqtSignal(str)  # tool_call_id
    subAgentLogRequested = pyqtSignal(str)  # task_ids (comma-separated)
    cardDiffRequested = pyqtSignal(int, int)  # round_index, message_index（消息在 _message_batch 中的索引）
    saveFileRequested = pyqtSignal(str, str)  # code, lang
    lazyRenderCompleted = pyqtSignal()  # 懒渲染完成信号，用于通知滚动保持

    def __init__(
            self,
            role: str,
            timestamp: str = None,
            parent=None,
            error: bool = False,
            reasoning_content: str = "",
    ):
        super().__init__(parent)
        self.parent = parent
        self.role = role
        self.timestamp = timestamp or datetime.now().strftime("%m-%d %H:%M")
        # 历史数据 timestamp 格式为 %Y-%m-%d %H:%M:%S，转为 %m-%d %H:%M
        if self.timestamp and len(self.timestamp) >= 19:
            try:
                dt = datetime.strptime(self.timestamp[:19], "%Y-%m-%d %H:%M:%S")
                self.timestamp = dt.strftime("%m-%d %H:%M")
            except ValueError:
                self.timestamp = self.timestamp[:14]
        # 助手卡片初始不显示时间，流完成后再设持续时长
        if role == "assistant" and not timestamp:
            self.timestamp = ""
        self.error = error
        self._interactive_options: List[dict] = []
        self._content_data: Any = [] if role == "assistant" else ""
        # 将 reasoning_content 转为 _content_data 的 reasoning block
        if role == "assistant" and reasoning_content:
            self._content_data.append({"type": "reasoning", "content": reasoning_content})
        self._streaming = False
        self._retrying = False  # 重试模式标志
        self._retry_error_type = ""  # 重试错误类型
        self._retry_attempt = 0  # 当前重试次数
        self._retry_max = 15  # 最大重试次数
        self._retry_wait_time = 0.0  # 等待时间
        self._round_index: Optional[int] = None  # 用于卡片差异功能
        self._message_index: Optional[int] = None  # 用于卡片差异和撤销功能：消息在 session.messages 中的索引
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._update_anim)
        self._pulse_phase = 0.0
        self._height_anim = QVariantAnimation(self)
        self._height_anim.setDuration(180)
        self._height_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._height_anim.valueChanged.connect(self._apply_viewer_height)
        self._height_anim.stateChanged.connect(self._on_height_anim_state_changed)
        self._is_height_animating = False  # 动画期间抑制重复报告
        # 禁用 Python 端的动画，依赖 JS 动画控制高度
        self._height_anim.setDuration(0)  # 设置为0相当于禁用插值
        self._target_viewer_height = 40
        self._last_applied_viewer_height = 40
        self._theme = self._build_theme(role, error)
        self._base_bg = self._theme["bg"]
        self._base_border = self._theme["border"]
        # 性能优化：缓存上次宽度值，避免不必要的更新
        self._last_synced_width = 0
        self._resize_preview_mode = False
        self._resize_preview_height = 0
        self._options_were_visible_before_resize = False
        # WebEngine 上下文恢复标志
        self._webengine_needs_restore = False
        # 懒渲染标志：未进入可视区域前不创建QWebEngine
        self._lazy_rendered = False
        # 标记：内容刚加载到viewer，首次heightChanged后滚动并清除
        self._content_just_loaded = False
        self._pending_content: Optional[str] = None
        self._reasoning_total_len = 0  # reasoning 内容总长度计数器，避免每次遍历
        self._viewer_container = QWidget(self)
        self._viewer_layout = QVBoxLayout(self._viewer_container)
        self._viewer_layout.setContentsMargins(0, 0, 0, 0)
        self._setup_ui()

    def _build_theme(self, role: str, error: bool = False) -> Dict[str, str]:
        Colors.refresh()
        themes = {
            "assistant": {
                "avatar": "AI",
                "title": "Drifox",
                "subtitle": "Assistant",
                "bg": Colors.ASSISTANT_CARD_BG,
                "border": "none",
                "accent": Colors.ASSISTANT_CARD_ACCENT,
                "text": Colors.ASSISTANT_CARD_TEXT,
                "muted": Colors.ASSISTANT_CARD_MUTED,
                "side": "left",
            },
            "welcome": {
                "avatar": "DX",
                "title": "Drifox",
                "subtitle": "AI Copilot",
                "bg": Colors.ASSISTANT_CARD_BG,
                "border": "none",
                "accent": Colors.ASSISTANT_CARD_ACCENT,
                "text": Colors.ASSISTANT_CARD_TEXT,
                "muted": Colors.ASSISTANT_CARD_MUTED,
                "side": "left",
            },
            "user": {
                "avatar": "你",
                "title": "你",
                "subtitle": "Prompt",
                "bg": Colors.USER_CARD_BG,
                "border": "none",
                "accent": Colors.USER_CARD_ACCENT,
                "text": Colors.USER_CARD_TEXT,
                "muted": Colors.USER_CARD_MUTED,
                "side": "right",
            },
        }
        theme = dict(themes.get(role, themes["assistant"]))
        if error:
            bg = Colors.ERROR  # 使用语义色
            theme["bg"] = "#2A1F1F"
            theme["border"] = "#A94444"
            theme["accent"] = "#FF7B7B"
        return theme

    def refresh_theme(self):
        """刷新主题颜色，响应全局主题切换"""
        self._theme = self._build_theme(self.role, self.error)
        self._base_bg = self._theme["bg"]
        self._base_border = self._theme["border"]
        self._apply_card_style()
        # 更新头像
        if hasattr(self, '_av_label'):
            self._av_label.setStyleSheet( self._build_avatar_style())
        # 更新标题
        if hasattr(self, '_name_label'):
            font_css = get_font_family_css()
            self._name_label.setStyleSheet(
                f"{font_css} font-size:{scale_font_size(14)}px;color:{self._theme['text']};font-weight:700;"
            )
        # 更新副标题
        if hasattr(self, '_subtitle_label'):
            font_css = get_font_family_css()
            self._subtitle_label.setStyleSheet(
                f"{font_css} font-size:{scale_font_size(11)}px;color:{self._theme['muted']};font-weight:500;letter-spacing:0.02em;"
            )
        # 更新时间戳
        if hasattr(self, '_ts_label'):
            self._ts_label.setStyleSheet(
                f"""
                QLabel {{
                    {get_font_family_css()} font-size: {scale_font_size(11)}px;
                    color: {self._theme["muted"]};
                    background: rgba(255,255,255,0.03);
                    border: 1px solid rgba(255,255,255,0.06);
                    border-radius: 9px;
                    padding: 2px 8px;
                }}
                """
            )
        # 刷新富文本视图字体
        if hasattr(self, 'viewer') and self.viewer and hasattr(self.viewer, '_refresh_viewer_font'):
            self.viewer._refresh_viewer_font()

    def set_duration(self, duration_seconds: int):
        """设置执行持续时间显示（用于 assistant 卡片）"""
        if self.role != "assistant":
            return
        if duration_seconds <= 0:
            return
        # 格式化为 mm:ss 或 h:mm:ss
        h, rem = divmod(duration_seconds, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            duration_text = f"{h}:{m:02d}:{s:02d}"
        else:
            duration_text = f"{m:02d}:{s:02d}"
        # 更新时间戳显示为持续时间
        if hasattr(self, '_ts_label'):
            self._ts_label.setText(duration_text)
            self._ts_label.setToolTip(f"执行持续时间: {duration_text}")
            self._ts_label.setVisible(True)
            self._ts_label.setStyleSheet(
                f"""
                QLabel {{
                    {get_font_family_css()} font-size: {scale_font_size(11)}px;
                    color: {self._theme["muted"]};
                    background: rgba(255,255,255,0.03);
                    border: 1px solid rgba(255,255,255,0.06);
                    border-radius: 9px;
                    padding: 2px 8px;
                }}
                """
            )

    def _build_avatar_style(self):
        font_css = get_font_family_css()
        if self.role in ("welcome", "assistant"):
            return ""
        return f"""
            QLabel {{
                {font_css} font-size: {scale_font_size(12)}px;
                color: #FFFFFF;
                font-weight: 700;
                background: {self._theme["accent"]};
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 15px;
            }}
        """

    def _setup_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(6, 6, 6, 6)
        main.setSpacing(8)
        top = QHBoxLayout()
        top.setContentsMargins(8, 2, 8, 2)
        top.setSpacing(10)

        av = QLabel(self)
        self._av_label = av
        if self.role in ("welcome", "assistant"):
            # 品牌图标头像
            av_icon = get_icon("drifox")
            pixmap = av_icon.pixmap(28, 28)
            av.setPixmap(pixmap)
            av.setFixedSize(30, 30)
            av.setAlignment(Qt.AlignCenter)
        else:
            # user 和其他：圆形文字头像
            av_icon = get_icon("用户")
            pixmap = av_icon.pixmap(28, 28)
            av.setPixmap(pixmap)
            av.setFixedSize(30, 30)
            av.setAlignment(Qt.AlignCenter)

        title_wrap = QWidget(self)
        title_layout = QVBoxLayout(title_wrap)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(1)

        font_css = get_font_family_css()
        nm_l = QLabel(self._theme["title"], self)
        self._name_label = nm_l
        nm_l.setStyleSheet(
            f"{font_css} font-size:{scale_font_size(14)}px;color:{self._theme['text']};font-weight:700;"
        )
        sub_l = QLabel(self._theme["subtitle"], self)
        self._subtitle_label = sub_l
        sub_l.setStyleSheet(
            f"{font_css} font-size:{scale_font_size(11)}px;color:{self._theme['muted']};font-weight:500;letter-spacing:0.02em;"
        )
        title_layout.addWidget(nm_l)
        title_layout.addWidget(sub_l)

        top.addWidget(av)
        top.addWidget(title_wrap)
        # 所有卡片都显示时间（用户卡片显示时间戳，助手卡片显示持续时间）
        ts = QLabel(self.timestamp, self)
        self._ts_label = ts
        ts.setVisible(bool(self.timestamp))
        ts.setStyleSheet(
            f"""
            QLabel {{
                {get_font_family_css()} font-size: {scale_font_size(11)}px;
                color: {self._theme["muted"]};
                background: rgba(255,255,255,0.03);
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 9px;
                padding: 2px 8px;
            }}
            """
        )
        top.addWidget(ts)
        top.addStretch()

        btns = QWidget(self)
        bl = QHBoxLayout(btns)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        specs = []
        if self.role == "assistant":
            specs = [
                (
                    get_icon("差异对比"),
                    "文档差异对比",
                    lambda: self._emit_card_diff_requested(),
                ),
                (
                    get_icon("复制"),
                    "复制",
                    lambda: self.actionRequested.emit(self.get_plain_text(), "copy"),
                ),
            ]
        elif self.role == "user":
            specs = [
                (
                    get_icon("复制"),
                    "复制",
                    lambda: self.actionRequested.emit(self.get_plain_text(), "copy"),
                ),
                (get_icon("撤销"), "撤销到这里", self.undoRequested.emit),
                (FluentIcon.DELETE, "删除", self.deleteRequested.emit),
            ]
        for ic, tp, cb in specs:
            b = TransparentToolButton(ic, self)
            b.setToolTip(tp)
            b.clicked.connect(cb)
            b.setFixedSize(32, 32)
            b.installEventFilter(ToolTipFilter(b))
            bl.addWidget(b)
        top.addWidget(btns)
        main.addLayout(top)
        main.addWidget(CardSeparator(self))

        if self.role == "user":
            self.viewer = PlainTextViewer(self)
            self.viewer.contentHeightChanged.connect(self._update_height)
            self._viewer_layout.addWidget(self.viewer)
            main.addWidget(self._viewer_container)
            self._lazy_rendered = True
        elif self.role == "welcome":
            # 欢迎卡片一开始就在可视区域，直接创建viewer不需要懒加载
            self.viewer = CodeWebViewer(self)
            self.viewer._lazy_markdown_cb = lambda: content_to_markdown(self._content_data)
            self.viewer.codeActionRequested.connect(self.actionRequested.emit)
            self.viewer.contextActionRequested.connect(self.contextActionRequested.emit)
            self.viewer.contentHeightChanged.connect(self._update_height)
            self.viewer.toolDiffRequested.connect(self.toolDiffRequested.emit)
            self.viewer.subAgentLogRequested.connect(self.subAgentLogRequested.emit)
            self.viewer.saveFileRequested.connect(self.saveFileRequested.emit)
            # WebEngine 上下文丢失处理
            self.viewer.contextLost.connect(self._on_webengine_context_lost)
            self.viewer.contextRestored.connect(self._on_webengine_context_restored)
            self.viewer.needRecreate.connect(self._on_webengine_need_recreate)
            self.viewer._install_dialog_filter()
            self._viewer_layout.addWidget(self.viewer)
            main.addWidget(self._viewer_container)
            self._lazy_rendered = True
        else:
            # 懒渲染：占位符，不立即创建QWebEngine，进入可视区域再创建
            placeholder = QLabel("加载中...", self)
            placeholder.setStyleSheet(f"color: #888888; font-size: {scale_font_size(14)}px; padding: 8px; {get_font_family_css()}")
            placeholder.setAlignment(Qt.AlignCenter)
            self._viewer_layout.addWidget(placeholder)
            main.addWidget(self._viewer_container)
            self._lazy_rendered = False
            self.viewer = None  # 懒加载，延后创建
            self.resize_placeholder = QFrame(self)
            self.resize_placeholder.setVisible(False)
            self.resize_placeholder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.resize_placeholder.setStyleSheet(
                """
                QFrame {
                    background: rgba(255,255,255,0.035);
                    border: 1px dashed rgba(255,255,255,0.08);
                    border-radius: 12px;
                }
                """
            )
            main.addWidget(self.resize_placeholder)

        self.options_widget = QWidget(self)
        self.options_layout = QVBoxLayout(self.options_widget)
        self.options_layout.setContentsMargins(0, 8, 0, 0)
        self.options_layout.setSpacing(8)
        self.options_widget.setVisible(False)
        main.addWidget(self.options_widget)

        # 重试状态栏（默认隐藏）
        self._retry_status_widget = QWidget(self)
        self._retry_status_widget.setVisible(False)
        retry_layout = QHBoxLayout(self._retry_status_widget)
        retry_layout.setContentsMargins(12, 6, 12, 6)
        retry_layout.setSpacing(8)
        self._retry_status_widget.setStyleSheet(
            """
            QWidget {
                background: rgba(255, 40, 40, 0.08);
                border-top: 1px solid rgba(255, 60, 60, 0.2);
                border-radius: 0px;
            }
            """
        )
        # 旋转图标（CSS动画模拟）
        self._retry_spinner = QLabel("⟳", self)
        self._retry_spinner.setStyleSheet(
            f"""
            QLabel {{
                color: rgba(255, 80, 80, 0.8);
                font-size: {scale_font_size(14)}px;
                font-weight: bold;
            }}
            """
        )
        retry_layout.addWidget(self._retry_spinner)
        # 错误类型
        self._retry_type_label = QLabel("", self)
        self._retry_type_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ff6b6b;
                font-size: {scale_font_size(12)}px;
                font-weight: 600;
            }}
            """
        )
        retry_layout.addWidget(self._retry_type_label)
        # 重试次数
        self._retry_attempt_label = QLabel("", self)
        self._retry_attempt_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ffaa44;
                font-size: {scale_font_size(12)}px;
            }}
            """
        )
        retry_layout.addWidget(self._retry_attempt_label)
        retry_layout.addStretch()
        # 等待倒计时
        self._retry_wait_label = QLabel("", self)
        self._retry_wait_label.setStyleSheet(
            f"""
            QLabel {{
                color: #888;
                font-size: {scale_font_size(11)}px;
            }}
            """
        )
        retry_layout.addWidget(self._retry_wait_label)
        main.addWidget(self._retry_status_widget)

        main.addWidget(CardSeparator(self))
        self.setStyleSheet(
            f"""
            CardWidget {{
                background-color: {self._theme["bg"]};
                border: 1px solid {self._theme["border"]};
                border-radius: 16px;
            }}
            """
        )

    def start_streaming_anim(self):
        if self._streaming:
            return
        self._streaming = True
        self._pulse_phase = 0.0
        try:
            self._anim_timer.start(50)  # 80→50ms，帧率从12.5fps提升到20fps
        except RuntimeError:
            return
        self.update()

    def _update_anim(self):
        self._pulse_phase = (self._pulse_phase + 0.035) % (math.pi * 2)
        # 重试状态栏降频更新（每200ms一次，避免和paintEvent双重刷新导致卡顿）
        if self._retrying:
            if not hasattr(self, '_retry_status_tick'):
                self._retry_status_tick = 0
            self._retry_status_tick += 1
            if self._retry_status_tick >= 4:  # 50ms * 4 = 200ms
                self._retry_status_tick = 0
                self._update_retry_status_bar()
        self.update()

    def _apply_card_style(self, border: str = None, bg: str = None):
        self.setStyleSheet(
            f"""
            CardWidget {{
                background-color: {bg or self._base_bg};
                border: 1px solid {border or self._base_border};
                border-radius: 16px;
            }}
            """
        )

    def stop_streaming_anim(self):
        self._streaming = False
        self._retrying = False
        self.error = False  # 重试成功后清除错误状态
        try:
            self._anim_timer.stop()
        except RuntimeError:
            return
        self._apply_card_style()
        self._retry_status_widget.setVisible(False)
        self.update()
        self.repaint()

    def start_retry_anim(self, error_type: str, attempt: int, max_retries: int, wait_time: float):
        """切换到重试边框模式（红色流动+白光点）"""
        self._retrying = True
        self._retry_error_type = error_type
        self._retry_attempt = attempt
        self._retry_max = max_retries
        self._retry_wait_time = wait_time
        # 确保动画定时器运行
        if not self._streaming:
            self._streaming = True
            self._pulse_phase = 0.0
            try:
                self._anim_timer.start(50)
            except RuntimeError:
                return
        # 更新状态栏
        self._update_retry_status_bar()
        self._retry_status_widget.setVisible(True)
        self.update()

    def update_retry_status(self, error_type: str, attempt: int, max_retries: int, wait_time: float):
        """更新重试状态信息"""
        self._retry_error_type = error_type
        self._retry_attempt = attempt
        self._retry_max = max_retries
        self._retry_wait_time = wait_time
        self._update_retry_status_bar()
        self.update()

    def stop_retry_anim(self):
        """停止重试动画，恢复正常边框"""
        self._retrying = False
        self._retry_status_widget.setVisible(False)
        if not self._streaming:
            return
        # 继续正常的流式动画（彩虹边框）
        self.update()

    def _update_retry_status_bar(self):
        """更新重试状态栏的文本内容"""
        # 重试时恢复标准重试样式
        self._retry_status_widget.setStyleSheet(
            """
            QWidget {
                background: rgba(255, 40, 40, 0.08);
                border-top: 1px solid rgba(255, 60, 60, 0.2);
                border-radius: 0px;
            }
            """
        )
        # 旋转图标动画
        spin_chars = ["◜", "◝", "◞", "◟"]
        idx = int(self._pulse_phase * 2) % 4
        self._retry_spinner.setText(spin_chars[idx])
        # 错误类型
        self._retry_type_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ff6b6b;
                font-size: {scale_font_size(12)}px;
                font-weight: 600;
            }}
            """
        )
        self._retry_type_label.setText(self._retry_error_type)
        # 重试次数
        self._retry_attempt_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ffaa44;
                font-size: {scale_font_size(12)}px;
            }}
            """
        )
        self._retry_attempt_label.setText(f"第 {self._retry_attempt}/{self._retry_max} 次重试")
        # 等待时间
        self._retry_wait_label.setStyleSheet(
            f"""
            QLabel {{
                color: #888;
                font-size: {scale_font_size(11)}px;
            }}
            """
        )
        self._retry_wait_label.setText(f"等待 {self._retry_wait_time:.0f}s")

    def _on_webengine_context_lost(self):
        """WebEngine 上下文丢失时显示恢复提示"""
        # 设置卡片为错误状态样式
        self._apply_card_style(border="#A94444")
        # 标记需要恢复
        self._webengine_needs_restore = True

    def _on_webengine_context_restored(self):
        """WebEngine 上下文恢复后恢复正常样式"""
        self._apply_card_style()
        self._webengine_needs_restore = False
        # 重新同步宽度
        self.sync_width(force=True)

    def _on_webengine_need_recreate(self):
        """需要完全重建 WebEngine 视图（GPU上下文丢失无法恢复时）"""
        if not self._lazy_rendered or self.viewer is None:
            return
        
        # 保存当前内容
        markdown_text = None
        if hasattr(self.viewer, '_markdown_text'):
            markdown_text = self.viewer._markdown_text
        
        # 销毁旧viewer
        self.viewer.deleteLater()
        
        # 重新创建viewer
        for i in reversed(range(self._viewer_layout.count())):
            item = self._viewer_layout.itemAt(i)
            if item and item.widget():
                item.widget().deleteLater()
        
        self.viewer = CodeWebViewer(self)
        self.viewer._lazy_markdown_cb = lambda: content_to_markdown(self._content_data)
        self.viewer.codeActionRequested.connect(self.actionRequested.emit)
        self.viewer.contextActionRequested.connect(self.contextActionRequested.emit)
        self.viewer.contentHeightChanged.connect(self._update_height)
        self.viewer.toolDiffRequested.connect(self.toolDiffRequested.emit)
        self.viewer.subAgentLogRequested.connect(self.subAgentLogRequested.emit)
        self.viewer.saveFileRequested.connect(self.saveFileRequested.emit)
        self.viewer.contextLost.connect(self._on_webengine_context_lost)
        self.viewer.contextRestored.connect(self._on_webengine_context_restored)
        self.viewer.needRecreate.connect(self._on_webengine_need_recreate)
        self.viewer._install_dialog_filter()
        
        self._viewer_layout.addWidget(self.viewer)
        
        # 恢复内容
        if markdown_text:
            self.viewer._markdown_text = markdown_text
            self.viewer._schedule_render(immediate=True)
        
        # 恢复正常样式
        self._apply_card_style()
        self._webengine_needs_restore = False
        
        # 同步宽度
        self.sync_width(force=True)

    def paintEvent(self, event):
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        radius = 16

        accent = QColor(self._theme["accent"])
        accent.setAlpha(95 if self.role == "user" else 75)
        stripe_width = 4
        stripe_x = w - stripe_width - 2 if self._theme.get("side") == "right" else 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(stripe_x, 10, stripe_width, max(18, h - 20), 3, 3)

        if not self._streaming:
            return

        # ══════════════════════════════════════════════════════
        #  辅助：准备色板 + 流光相位
        # ══════════════════════════════════════════════════════
        if self.role == "assistant":
            # 呼吸：极缓慢脉动
            breathe = 0.55 + 0.45 * (math.sin(self._pulse_phase * 0.3) + 1) / 2
            # 流光闪烁：柔和放缓
            shimmer = 0.6 + 0.4 * (math.sin(self._pulse_phase * 1.8) + 1) / 2

            def lerp_color(a: QColor, b: QColor, t: float) -> QColor:
                """线性插值两颜色"""
                r = int(a.red() + (b.red() - a.red()) * t)
                g = int(a.green() + (b.green() - a.green()) * t)
                bl = int(a.blue() + (b.blue() - a.blue()) * t)
                return QColor(r, g, bl)

            if self._retrying:
                # ── 重试模式：红色流动渐变 ──
                rainbow = [
                    QColor("#ff2222"),  # 鲜红
                    QColor("#aa0000"),  # 暗红
                    QColor("#ff3333"),  # 亮红
                    QColor("#880000"),  # 深红
                    QColor("#ff1111"),  # 火红
                    QColor("#bb0000"),  # 酒红
                    QColor("#ff4444"),  # 浅红
                    QColor("#990000"),  # 暗深红
                ]
            else:
                # ── 正常模式：10 色精细彩虹 ──
                rainbow = [
                    QColor("#60D4FF"),  # 天蓝
                    QColor("#40C8FF"),  # 青蓝
                    QColor("#4DA6FF"),  # 柔蓝
                    QColor("#8B7BFF"),  # 薰衣草
                    QColor("#C084FC"),  # 紫罗兰
                    QColor("#F472B6"),  # 玫瑰粉
                    QColor("#FB7185"),  # 珊瑚红
                    QColor("#F59E0B"),  # 琥珀金
                    QColor("#34D399"),  # 翠绿
                    QColor("#22D3EE"),  # 青色
                ]
            N = len(rainbow)
            # 主边框连续相位
            shift_main = (self._pulse_phase / (math.pi * 2)) * N
            # 发光层更慢
            shift_glow = shift_main * 0.5
            # 流光带相位
            shift_shimmer = shift_main * 1.15

            def build_gradient(shift: float, stops: list, alpha_base: float) -> QLinearGradient:
                """用连续相位生成平滑渐变：每个 stop 点用前后两色插值"""
                grad = QLinearGradient(0, 0, w, h)
                for pos in stops:
                    raw = (shift + pos * N) % N
                    idx = int(raw) % N
                    frac = raw - int(raw)
                    c = lerp_color(rainbow[idx], rainbow[(idx + 1) % N], frac)
                    c.setAlpha(int(alpha_base * breathe))
                    grad.setColorAt(pos, c)
                return grad

            main_stops = [0.0, 0.12, 0.24, 0.36, 0.50, 0.64, 0.76, 0.88, 1.0]
            inner_stops = [0.0, 0.12, 0.24, 0.36, 0.48, 0.60, 0.72, 0.84, 0.92, 1.0]
            glow_stops = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
            shimmer_stops = [0.0, 0.5, 1.0]
        else:
            rainbow = None
            pulse = QColor(self._theme["accent"])
            breathe = 0.55 + 0.45 * (math.sin(self._pulse_phase * 0.3) + 1) / 2
            shimmer = 0.6 + 0.4 * (math.sin(self._pulse_phase * 1.8) + 1) / 2


        # ══════════════════════════════════════════════════════
        #  层1：内壁漫射（极柔和的边缘渗光）
        # ══════════════════════════════════════════════════════
        inner_clip = QPainterPath()
        inner_clip.addRoundedRect(3, 3, w - 6, h - 6, radius - 2, radius - 2)
        painter.setClipPath(inner_clip)
        if self.role == "assistant":
            inner_gradient = build_gradient(shift_glow, inner_stops, 12)
        else:
            inner_gradient = QLinearGradient(0, 0, w, h)
            c = QColor(pulse.lighter(150))
            c.setAlpha(int(18 * breathe))
            inner_gradient.setColorAt(0.0, c)
            inner_gradient.setColorAt(1.0, QColor(pulse.darker(110).name()))
        painter.fillRect(0, 0, w, h, inner_gradient)

        # ══════════════════════════════════════════════════════
        #  层2：外发光（霓虹光晕，7px宽，比主边框更宽更柔和）
        # ══════════════════════════════════════════════════════
        outer_clip = QPainterPath()
        outer_clip.addRoundedRect(-2, -2, w + 4, h + 4, radius + 3, radius + 3)
        inner_edge_clip = QPainterPath()
        inner_edge_clip.addRoundedRect(0, 0, w, h, radius + 1, radius + 1)
        glow_region = outer_clip - inner_edge_clip
        painter.setClipPath(glow_region)
        if self.role == "assistant":
            glow_gradient = build_gradient(shift_glow, glow_stops, 48)
        else:
            glow_gradient = QLinearGradient(0, 0, w, h)
            glow_gradient.setColorAt(0.0, QColor(pulse.lighter(130).name()))
            glow_gradient.setColorAt(0.5, QColor(pulse.name()))
            glow_gradient.setColorAt(1.0, QColor(pulse.darker(140).name()))
        glow_pen = QPen(glow_gradient, 7)
        painter.setPen(glow_pen)
        painter.setBrush(QBrush(Qt.NoBrush))
        painter.drawRoundedRect(-2, -2, w + 4, h + 4, radius + 3, radius + 3)

        # ══════════════════════════════════════════════════════
        #  层3：主彩色边框（4px，饱和鲜艳）
        # ══════════════════════════════════════════════════════
        border_clip = QPainterPath()
        border_clip.addRoundedRect(0, 0, w, h, radius + 1, radius + 1)
        inner_border_clip = QPainterPath()
        inner_border_clip.addRoundedRect(2, 2, w - 4, h - 4, radius - 1, radius - 1)
        border_region = border_clip - inner_border_clip
        painter.setClipPath(border_region)
        if self.role == "assistant":
            main_gradient = build_gradient(shift_main, main_stops, 215)
        else:
            main_gradient = QLinearGradient(0, 0, w, h)
            glow_a = int((90 + 45 * (math.sin(self._pulse_phase * 1.5) + 1) / 2) * breathe)
            pulse2 = QColor(pulse.name())
            pulse2.setAlpha(glow_a)
            main_gradient.setColorAt(0.0, QColor(pulse.lighter(120).name()))
            main_gradient.setColorAt(0.5, pulse2)
            main_gradient.setColorAt(1.0, QColor(pulse.darker(130).name()))
        main_pen = QPen(main_gradient, 4)
        painter.setPen(main_pen)
        painter.setBrush(QBrush(Qt.NoBrush))
        painter.drawRoundedRect(0, 0, w, h, radius + 1, radius + 1)

        # ══════════════════════════════════════════════════════
        #  层4：流光高光带（白色细光条快速划过）
        # ══════════════════════════════════════════════════════
        if self.role == "assistant":
            shimmer_clip = QPainterPath()
            shimmer_clip.addRoundedRect(1, 1, w - 2, h - 2, radius, radius)
            painter.setClipPath(shimmer_clip)
            # 流光位置：连续小数，避免跳变
            shimmer_pos = (shift_shimmer % N) / N
            shimmer_band_gradient = QLinearGradient(0, 0, w, h)
            shimmer_band_gradient.setColorAt(max(0.0, shimmer_pos - 0.07), QColor(0, 0, 0, 0))
            shimmer_band_gradient.setColorAt(max(0.0, shimmer_pos - 0.03), QColor(255, 255, 255, int(80 * shimmer)))
            shimmer_band_gradient.setColorAt(shimmer_pos, QColor(255, 255, 255, int(150 * shimmer)))
            shimmer_band_gradient.setColorAt(min(1.0, shimmer_pos + 0.03), QColor(255, 255, 255, int(80 * shimmer)))
            shimmer_band_gradient.setColorAt(min(1.0, shimmer_pos + 0.07), QColor(0, 0, 0, 0))
            shimmer_pen = QPen(shimmer_band_gradient, 3)
            painter.setPen(shimmer_pen)
            painter.setBrush(QBrush(Qt.NoBrush))
            painter.drawRoundedRect(1, 1, w - 2, h - 2, radius, radius)

        # ══════════════════════════════════════════════════════
        #  层5：顶部高光条（柔和的光泽）
        # ══════════════════════════════════════════════════════
        top_clip = QPainterPath()
        top_clip.addRoundedRect(0, 0, w, h, radius, radius)
        painter.setClipPath(top_clip)
        if self.role == "assistant":
            if self._retrying or self.error:
                top_color = QColor("#ff2222")
            else:
                top_color = QColor("#60D4FF")
            top_color.setAlpha(int(22 * breathe))
        else:
            top_color = QColor(self._theme["accent"])
            top_color.setAlpha(int(30 * breathe))
        painter.fillRect(0, 0, w, 5, top_color)

    def set_error_state(self, is_error: bool, error_message: str = ""):
        """设置错误状态
        
        Args:
            is_error: 是否为错误状态
            error_message: 错误信息（错误状态时显示在状态栏）
        """
        self.error = is_error
        if is_error:
            self._retrying = False
            # 显示错误状态栏（而不是隐藏）
            self._show_error_status(error_message)
            bd, bg = "#ff4d4d", "#2a1f1f"
        else:
            self._retry_status_widget.setVisible(False)
            bd, bg = self._base_border, self._base_bg
        self._apply_card_style(border=bd, bg=bg)

    def _show_error_status(self, error_message: str):
        """显示错误状态信息（复用重试状态栏UI，但显示错误信息）"""
        self._retry_error_type = "错误"
        self._retry_type_label.setText("❌")
        self._retry_attempt_label.setText(error_message if error_message else "请求失败")
        self._retry_wait_label.setText("")
        self._retry_spinner.setText("⚠")
        # 改变状态栏样式为错误风格
        self._retry_status_widget.setStyleSheet(
            """
            QWidget {
                background: rgba(255, 40, 40, 0.08);
                border-top: 1px solid rgba(255, 60, 60, 0.2);
                border-radius: 0px;
            }
            """
        )
        self._retry_type_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ff6b6b;
                font-size: {scale_font_size(14)}px;
                font-weight: bold;
            }}
            """
        )
        self._retry_attempt_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ff9999;
                font-size: {scale_font_size(12)}px;
            }}
            """
        )
        self._retry_status_widget.setVisible(True)

    def _emit_card_diff_requested(self):
        """发射卡片差异请求信号"""
        round_idx = self._round_index if self._round_index is not None else -1
        msg_idx = self._message_index if self._message_index is not None else -1
        self.cardDiffRequested.emit(round_idx, msg_idx)

    def _update_height(self, h):
        target_height = max(40, h)
        current_height = self.viewer.height() or self.viewer.minimumHeight() or 40
        self._target_viewer_height = target_height

        # 关键优化：高度变化完全由 CSS transition 驱动
        # PyQt 只设置最终值，不做 QVariantAnimation 插值动画
        # 因为 CSS transition 已经提供了平滑动画
        if self._streaming or abs(target_height - current_height) < 10:
            if self._height_anim.state() == QVariantAnimation.Running:
                self._height_anim.stop()
            self._apply_viewer_height(target_height)
            return

        # 停止任何正在进行的动画，直接跳到目标值
        # CSS transition 会负责平滑过渡
        self._height_anim.stop()
        self._apply_viewer_height(target_height)

    def _on_height_anim_state_changed(self, state):
        self._is_height_animating = (state == QVariantAnimation.Running)
        # 动画结束时触发一次高度变化信号，让父容器更新
        if state == QVariantAnimation.Stopped:
            self.heightChanged.emit(self._last_applied_viewer_height)
            layout = self.layout()
            if layout:
                layout.invalidate()

    def _apply_viewer_height(self, value):
        height = max(40, int(value))
        if height == self._last_applied_viewer_height:
            return
        self._last_applied_viewer_height = height
        self.viewer.setFixedHeight(height)
        self.heightChanged.emit(height)
        # 简化：直接触发布局更新，不再区分动画状态

    def sync_width(self, force: bool = False):
        """同步卡片宽度

        Args:
            force: 是否强制更新，即使宽度没变化
        """
        parent = self.parentWidget()
        if not parent:
            return
        parent_width = parent.width()
        if self.role == "welcome":
            horizontal_margin = 20
        elif self.role == "user":
            horizontal_margin = 180
        else:
            horizontal_margin = 72

        target_width = max(320, parent_width - horizontal_margin)

        # 性能优化：只有宽度真正变化时才更新
        if not force and target_width == self._last_synced_width:
            return

        self._last_synced_width = target_width
        if self.minimumWidth() != target_width or self.maximumWidth() != target_width:
            self.blockSignals(True)
            self.setMinimumWidth(target_width)
            self.setMaximumWidth(target_width)
            self.blockSignals(False)

        # 宽度同步后触发 viewer 高度重算（用于 user 卡片的 PlainTextViewer）
        if not self._resize_preview_mode and hasattr(self.viewer, 'update_height'):
            self.viewer.update_height()

    def set_resize_preview_mode(self, enabled: bool):
        """在窗口 resize 期间切换到轻量占位模式，减少复杂子控件重绘。

        只有使用 CodeWebViewer 的卡片需要 placeholder 优化，
        PlainTextViewer（user 卡片）weight 很轻，不需要。
        """
        if enabled == self._resize_preview_mode:
            return

        self._resize_preview_mode = enabled

        # user 卡片使用 PlainTextViewer，weight 很轻，不需要 placeholder
        if self.role == "user":
            return

        # welcome 卡片不需要 resize placeholder
        if self.role == "welcome":
            return

        # 懒渲染还没创建viewer，跳过
        if self.viewer is None:
            return

        if enabled:
            viewer_height = max(self.viewer.height(), self.viewer.minimumHeight(), 40)
            options_height = self.options_widget.sizeHint().height() if self.options_widget.isVisible() else 0
            self._resize_preview_height = max(40, viewer_height + options_height)
            self.resize_placeholder.setFixedHeight(self._resize_preview_height)
            self.resize_placeholder.show()
            self.viewer.setUpdatesEnabled(False)
            self.viewer.hide()
            self._options_were_visible_before_resize = self.options_widget.isVisible()
            if self._options_were_visible_before_resize:
                self.options_widget.setUpdatesEnabled(False)
                self.options_widget.hide()
            return

        self.viewer.show()
        self.viewer.setUpdatesEnabled(True)
        if self._options_were_visible_before_resize:
            self.options_widget.show()
            self.options_widget.setUpdatesEnabled(True)
        self.resize_placeholder.hide()
        self.resize_placeholder.setFixedHeight(0)
        self._resize_preview_height = 0
        self._options_were_visible_before_resize = False

        if hasattr(self.viewer, "update_height"):
            self.viewer.update_height()

    def wheelEvent(self, event: QWheelEvent):
        try:
            scroll_area = self.parent.chat_scroll_area
            if scroll_area:
                vbar = scroll_area.verticalScrollBar()
                if (
                        vbar
                        and vbar.minimum() != vbar.maximum()
                        and event.angleDelta().y() != 0
                ):
                    vbar.setValue(vbar.value() - event.angleDelta().y() // 2)
                    event.accept()
                    return
        except:
            pass
        super().wheelEvent(event)

    def update_content(self, txt):
        if self.role == "assistant" and not self._streaming:
            self.start_streaming_anim()
        if isinstance(txt, list):
            self.set_content(txt)
            return
        self.append_text(txt)

    def ensure_rendered(self, delay_ms: int = 0):
        """如果还没渲染，懒加载创建QWebViewer并渲染内容
        
        Args:
            delay_ms: 延迟加载毫秒数。默认0立即加载，>0则延迟加载并发送信号
        """
        if self._lazy_rendered or self.role == "user":
            return

        def _do_ensure_rendered():
            # 移除占位符，创建真正的viewer
            for i in reversed(range(self._viewer_layout.count())):
                item = self._viewer_layout.itemAt(i)
                if item and item.widget():
                    item.widget().deleteLater()

            self.viewer = CodeWebViewer(self)
            self.viewer._lazy_markdown_cb = lambda: content_to_markdown(self._content_data)
            self.viewer.codeActionRequested.connect(self.actionRequested.emit)
            self.viewer.contextActionRequested.connect(self.contextActionRequested.emit)
            self.viewer.contentHeightChanged.connect(self._update_height)
            self.viewer.toolDiffRequested.connect(self.toolDiffRequested.emit)
            self.viewer.subAgentLogRequested.connect(self.subAgentLogRequested.emit)
            self.viewer.saveFileRequested.connect(self.saveFileRequested.emit)
            # WebEngine 上下文丢失处理
            self.viewer.contextLost.connect(self._on_webengine_context_lost)
            self.viewer.contextRestored.connect(self._on_webengine_context_restored)
            self.viewer.needRecreate.connect(self._on_webengine_need_recreate)
            # 安装对话框过滤
            self.viewer._install_dialog_filter()

            self._viewer_layout.addWidget(self.viewer)
            self._lazy_rendered = True

            # 如果有等待渲染的内容，现在渲染
            if self._pending_content is not None:
                self.set_content(self._pending_content)
                self._pending_content = None
            
            # 通知懒渲染完成，让父组件可以修正滚动位置
            self.lazyRenderCompleted.emit()

        if delay_ms > 0:
            # 延迟加载，批量处理减少卡顿
            QTimer.singleShot(delay_ms, _do_ensure_rendered)
        else:
            _do_ensure_rendered()

    def set_content(self, content: Any):
        if self.role == "assistant":
            self._content_data = ensure_content_blocks(content)
            rendered = content_to_markdown(self._content_data)
        else:
            self._content_data = str(content or "")
            rendered = self._content_data

        if not self._lazy_rendered:
            # 懒渲染阶段，保存内容等待进入可视区域
            self._pending_content = content
            return

        if hasattr(self.viewer, "_markdown_text"):
            self.viewer._markdown_text = rendered
            self.viewer._schedule_render(immediate=True)
        elif hasattr(self.viewer, "set_text"):
            self.viewer.set_text(rendered)
        self._content_just_loaded = True

    def append_text(self, text: str):
        if self.role == "assistant":
            self._content_data = append_text_block(self._content_data, text)
            # 优化：懒渲染模式下直接跳过 markdown 渲染，避免不必要的计算
            if not self._lazy_rendered or not self.viewer:
                self._pending_content = self._content_data
                return
            # 性能优化：不立即执行 content_to_markdown，设懒回调让 _perform_update
            # 在渲染定时器到期时执行（多个 chunk 在窗口期内只转换一次，避免白费）
            self.viewer._lazy_markdown_cb = lambda: content_to_markdown(self._content_data)
            # 流式模式下增量追加纯文本到 DOM，让用户立即看到文字
            if self._streaming:
                self.viewer._append_text_incremental(text)
            self.viewer._schedule_render(immediate=False)
            self._content_just_loaded = True
            return

        self._content_data = str(self._content_data or "") + str(text or "")
        if self.viewer:
            self.viewer.append_chunk(str(text or ""))
            self._content_just_loaded = True

    def append_tool_result(
            self,
            tool_name: str,
            arguments: Dict[str, Any] = None,
            result: Any = None,
            success: bool = True,
            tool_call_id: str = None,
    ):
        self._content_data.append(
            make_tool_result_block(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                success=success,
                tool_call_id=tool_call_id,
            )
        )
        # 优化：懒渲染模式下直接跳过 markdown 渲染，避免不必要的计算
        if not self._lazy_rendered or not self.viewer:
            self._pending_content = self._content_data
            return
        # 性能优化：通过 _lazy_markdown_cb 延迟到 _perform_update 执行
        # 工具结果不必须立即渲染，用 immediate=False 合并到下一次渲染批次
        self.viewer._lazy_markdown_cb = lambda: content_to_markdown(self._content_data)
        self.viewer._schedule_render(immediate=False)

    def get_plain_text(self) -> str:
        if self.role == "assistant":
            return content_to_text(self._content_data, include_tool_results=True)
        return str(self._content_data or "")

    def run_js(self, js_code: str):
        """运行 JavaScript 代码"""
        try:
            if self.viewer and hasattr(self.viewer, "page"):
                self.viewer.page().runJavaScript(js_code)
        except RuntimeError:
            pass

    def set_reasoning_content(self, content: str):
        """设置思考内容（用于 DeepSeek 思考模式）- 作为 reasoning block 写入 _content_data"""
        self._content_data.insert(0, {"type": "reasoning", "content": content})
        if content and hasattr(self.viewer, "_markdown_text"):
            self.viewer._markdown_text = content_to_markdown(self._content_data)
            self.viewer._schedule_render(immediate=True)

    def set_html_direct(self, html: str):
        """直接设置 HTML，绕过打字机效果"""
        try:
            if self.viewer:
                self.viewer._markdown_text = html
                self.viewer._streaming = False
                self.viewer._perform_update()
        except RuntimeError:
            pass

    def start_new_thinking_block(self):
        """开始一个新的思考块（每轮工具迭代调用一次）
        
        将 reasoning 作为 _content_data 的一个 block，
        与文本、工具结果自然交错排列。
        """
        self._content_data.append({"type": "reasoning", "content": ""})
        
    def append_reasoning(self, text: str):
        """追加思考内容到当前最后一个思考块（流式模式）

        将 reasoning 直接写入 _content_data 的 reasoning block，
        使其与文本、工具结果按实际发生顺序交错渲染。
        """
        t0 = time.time()
        # 查找最后一个 reasoning block（不管是否在末尾，避免 content 先到导致新增到末尾）
        last_reasoning_idx = -1
        for i in reversed(range(len(self._content_data))):
            if self._content_data[i].get("type") == "reasoning":
                last_reasoning_idx = i
                break
        
        if last_reasoning_idx >= 0:
            # 找到已有的最后一个 reasoning 块，追加内容
            self._content_data[last_reasoning_idx]["content"] = (self._content_data[last_reasoning_idx].get("content", "") or "") + text
        else:
            # 未找到，新增 reasoning 块
            self._content_data.append({"type": "reasoning", "content": text})
        self._reasoning_total_len += len(text)

        LARGE_THINKING_THRESHOLD = 50 * 1024  # 50KB

        if not self._lazy_rendered or not self.viewer:
            self._pending_content = self._content_data
            return

        # 标记内容已加载，高度变化时触发 _on_message_card_height_changed 滚底
        self._content_just_loaded = True

        if self._reasoning_total_len > LARGE_THINKING_THRESHOLD:
            # 超长思考：增量更新提供即时文字，同时定期全量渲染保持 DOM 结构正确
            self._update_thinking_incremental(text)
        # 性能优化：通过 _lazy_markdown_cb 将 content_to_markdown 延迟到
        # _perform_update 执行（渲染定时器自带防抖，多 chunk 合并转换一次）
        # 这同时修复了旧代码的 bug：渲染定时器激活时跳过 markdown 更新，
        # 导致最后几个 chunk 内容丢失
        self.viewer._lazy_markdown_cb = lambda: content_to_markdown(self._content_data)
        self.viewer._schedule_render(immediate=False)

    def _update_thinking_incremental(self, new_text: str):
        """增量更新思考内容（用于超长思考）

        直接通过 JavaScript 更新最后一个思考块内容，避免完整重渲染。
        """
        if not hasattr(self.viewer, 'page'):
            return

        try:
            # 对新内容进行转义和代码块清理
            escaped = escape(new_text)
            escaped = _CODE_BLOCK_REMOVE_PATTERN.sub("", escaped)
            escaped = escaped.replace("`", "").replace("\r\n", " ").replace("\n", " ")

            # 直接更新最后一个思考块内容（通过 JS）
            js_code = f"""
            (function() {{
                const thinkContents = document.querySelectorAll('.think-content');
                if (thinkContents.length > 0) {{
                    const lastThink = thinkContents[thinkContents.length - 1];
                    lastThink.textContent += {json.dumps(escaped)};
                    lastThink.classList.remove('loading');
                }}
                reportHeight();
            }})();
            """
            self.viewer.page().runJavaScript(js_code)
        except RuntimeError:
            pass

    def add_interactive_option(self, option: Dict[str, Any]):
        """添加交互选项"""
        self._interactive_options.append(option)

        option_widget = QWidget(self.options_widget)
        option_layout = QHBoxLayout(option_widget)
        option_layout.setContentsMargins(0, 0, 0, 0)
        option_layout.setSpacing(8)

        label = QLabel(f"• {option.get('label', '选项')}", self)
        label.setStyleSheet(f"color: #4a9eff; {get_font_family_css()} font-size: 13px; cursor: pointer;")
        label.setCursor(Qt.PointingHandCursor)
        label.option_data = option
        label.mousePressEvent = lambda e, opt=option: self._on_option_clicked(opt)

        option_layout.addWidget(label)
        option_layout.addStretch()

        self.options_layout.addWidget(option_widget)
        self.options_widget.setVisible(True)

    def add_interactive_options(self, options: List[Dict[str, Any]]):
        """批量添加交互选项"""
        if not options:
            return

        title_label = QLabel("👉 请选择：", self)
        title_label.setStyleSheet(f"color: #888; {get_font_family_css()} font-size: 12px; margin-top: 8px;")
        self.options_layout.addWidget(title_label)

        for option in options:
            self.add_interactive_option(option)

    def _on_option_clicked(self, option: Dict[str, Any]):
        """选项被点击"""
        self.optionSelected.emit(option)

    def set_intervention_mode(self, enabled: bool):
        """设置人工干预模式"""
        if enabled:
            self.interventionRequested.emit(
                {"card_id": id(self), "message": "请求人工干预"}
            )

    def finish_streaming(self):
        try:
            if self.viewer is not None and hasattr(self.viewer, 'finish_streaming'):
                self.viewer.finish_streaming()
        except RuntimeError:
            pass
        self.stop_streaming_anim()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 宽度同步由外层聊天窗口统一调度，避免卡片自身 resize 再次触发全量重算

    def cleanup(self):
        """
        清理 MessageCard 持有的资源，防止内存泄漏。
        应该在删除卡片前调用，或者在 closeEvent 中自动调用。
        """
        # 停止所有定时器
        timers_to_stop = [
            self._anim_timer,
            self._height_anim,
        ]
        for timer in timers_to_stop:
            try:
                if isinstance(timer, QTimer):
                    timer.stop()
                elif isinstance(timer, QVariantAnimation):
                    timer.stop()
            except RuntimeError:
                pass

        # 调用 viewer 的清理方法（先清理后释放引用）
        if hasattr(self.viewer, 'cleanup'):
            try:
                self.viewer.cleanup()
            except RuntimeError:
                pass
        self.viewer = None  # 释放 viewer 引用，允许 GC

        # 清理大数据缓存
        self._content_data = None
        self._interactive_options = []
        self._markdown_text = None  # 大 markdown 文本
        self._last_rendered_html = None  # 大 HTML 字符串
        self._last_rendered_markdown = None  # 可能很大的 markdown
        self._rendered_code_blocks = []  # 代码块缓存

        # 清理 markdown_cache 如果存在
        if hasattr(self, '_markdown_cache') and self._markdown_cache:
            self._markdown_cache.clear()
            self._markdown_cache = None

    def closeEvent(self, e):
        self.cleanup()
        super().closeEvent(e)


def create_welcome_card(
        parent=None, agent_name: str = "", agent_description: str = "",
        recent_sessions: list = None, top_by_count: list = None
) -> MessageCard:
    """创建欢迎卡片

    Args:
        parent: 父控件
        agent_name: 当前智能体名称
        agent_description: 智能体描述
        recent_sessions: 最近的历史会话列表，每项包含 title, last_time, session_id, message_count
        top_by_count: 消息最多的会话列表，每项包含 title, last_time, session_id, message_count
    """
    agent_tendency = ""
    if agent_name:
        agent_tendency = f"""
---

### 🤖 当前智能体：{agent_name}

{agent_description}

"""

    # 随机选择欢迎语和 Tips
    greeting = get_random_greeting()
    tip = get_random_tip()

    # 构建历史会话链接（两列表格：最近会话 | 最多消息）
    history_section = ""
    if recent_sessions or top_by_count:
        # 生成表格 HTML（使用纯 HTML 确保胶囊样式正确显示）
        table_rows = ""
        for i in range(3):
            # 左边：最近会话
            recent = recent_sessions[i] if recent_sessions and i < len(recent_sessions) else None
            # 右边：消息最多
            top = top_by_count[i] if top_by_count and i < len(top_by_count) else None

            if recent:
                title = escape(recent.get("title", "未命名会话"))
                session_id = escape(recent.get("session_id", ""))
                last_time = escape(recent.get("last_time") or "")
                left_cell = f'<span class="context-tag session-tag" data-type="session" data-session-id="{session_id}" data-action="session">{title}<span class="session-time">{last_time}</span></span>'
            else:
                left_cell = "-"

            if top:
                title = escape(top.get("title", "未命名会话"))
                session_id = escape(top.get("session_id", ""))
                msg_count = top.get("message_count", 0)
                right_cell = f'<span class="context-tag session-tag" data-type="session" data-session-id="{session_id}" data-action="session">{title}<span class="session-time">{msg_count}条消息</span></span>'
            else:
                right_cell = "-"

            table_rows += f'<tr><td>{left_cell}</td><td>{right_cell}</td></tr>'

        history_section = f"""
<table class="session-table">
<tr><th>最近会话</th><th>最活跃会话</th></tr>
{table_rows}
</table>
"""

    welcome_md = f"""### 👋 {greeting}

---

**{tip}**

{history_section}
"""

    card = MessageCard(role="welcome", timestamp="就绪", parent=parent)
    card.update_content(welcome_md)
    card.finish_streaming()
    return card
