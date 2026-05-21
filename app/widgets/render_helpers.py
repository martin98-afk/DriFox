# -*- coding: utf-8 -*-
"""
UI 渲染辅助函数
"""

import hashlib
import orjson as json
import re
from html import escape

from app.utils.design_tokens import scale_font_size, _get_global_font, Colors
from app.utils.utils import get_font_family_css

# 预编译正则表达式（模块级别缓存，避免重复编译）
_CODE_BLOCK_PATTERN = re.compile(r"```[\w]*\n")
_CODE_BLOCK_FINAL_PATTERN = re.compile(r"```")
# 匹配 HTML 代码块标签
_HTML_CODE_BLOCK_PATTERN = re.compile(r"<(pre|code)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
# HTML 标签清理正则（避免每次调用 re.sub）
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
# UUID 模式（用于提取 task_id）
_UUID_PATTERN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)
# Null 字符清理（编译一次，多次使用）
_NULL_CHAR = '\x00'  # 避免 str.replace 被重复调用


def format_tool_block(
    tool_name: str,
    tool_args: dict,
    result: str = None,
    success: bool = True,
) -> str:
    """格式化工具块为纯文本标记，用于存储"""
    args_json = json.dumps(tool_args).decode('utf-8')
    result_str = str(result) if result else ""

    return (
        f"<tool>\nname: {tool_name}\nargs: {args_json}\n"
        f"result: {result_str}\nsuccess: {success}\n</tool>"
    )


def _escape_text_for_plain(text: str) -> str:
    """
    清理文本中的特殊字符，避免纯文本渲染错误。
    移除：
    - HTML 标签 <...>
    - Markdown 代码块标记 ```language, ```
    - 独立反引号 `
    - 思考标签 <think>、
    - 其他可能导致渲染问题的特殊字符
    """
    if not text:
        return ""
    # 0. 清理思考标签（避免渲染时被误识别）
    text = text.replace("<think>", "").replace("", "")
    # 1. 先移除 HTML 代码块标签 <pre>...</pre> <code>...</code>
    text = _HTML_CODE_BLOCK_PATTERN.sub("", text)
    # 2. 移除 markdown 代码块标记 ```language 和 ```
    text = _CODE_BLOCK_PATTERN.sub("", text)
    text = _CODE_BLOCK_FINAL_PATTERN.sub("", text)
    # 3. 移除独立的反引号
    text = text.replace("`", "")
    # 4. 移除 HTML 标签（使用预编译正则）
    text = _HTML_TAG_PATTERN.sub("", text)
    # 5. 移除可能造成渲染问题的特殊空白字符
    text = text.replace(_NULL_CHAR, "")  # 移除 null 字符
    # 6. 规范化换行符并转义为字面量（用于不支持多行的显示）
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "\\n")  # 换行符转为字面量 \n
    return text.strip()


def _truncate_value(v, max_len: int = 80) -> str:
    """截断单个参数值"""
    if isinstance(v, dict):
        s = json.dumps(v).decode('utf-8')
        return s[:max_len] + "..." if len(s) > max_len else s
    elif isinstance(v, list):
        s = json.dumps(v).decode('utf-8')
        return s[:max_len] + "..." if len(s) > max_len else s
    elif isinstance(v, str):
        return v[:max_len] + "..." if len(v) > max_len else v
    else:
        s = str(v)
        return s[:max_len] + "..." if len(s) > max_len else s


def _format_args_preview(tool_args: dict, max_total_len: int = 80) -> str:
    """
    格式化参数预览为 '参数1=值1; 参数2=值2' 格式。
    限制总字数，超过则截断并添加 '...'。
    
    优化：优先显示简短的参数值，长内容进行截断。
    """
    if not tool_args:
        return ""
    
    # 按值的长度排序（短的优先），确保重要的简短参数优先显示
    sorted_args = sorted(tool_args.items(), key=lambda x: len(str(x[1])))
    
    parts = []
    total_len = 0
    
    for key, value in sorted_args:
        # 清理值中的特殊字符
        value_str = _truncate_value(value)
        value_str = _escape_text_for_plain(value_str)
        # 参数预览也不支持多行，确保换行符被转义
        value_str = value_str.replace("\n", "\\n")
        # 构建参数片段
        part = f"{key}={value_str}"
        
        # 检查加上分隔符后是否会超过限制
        if parts:
            next_len = total_len + len(part) + 2  # +2 for "; "
            if next_len > max_total_len:
                # 检查当前是否已经超过限制
                if total_len >= max_total_len:
                    break
                # 添加当前部分（如果还没超过）
                remaining = max_total_len - total_len - 3  # space for "..."
                if remaining > 0:
                    parts.append(part[:remaining] + "...")
                else:
                    parts.append("...")
                break
        
        parts.append(part)
        total_len += len(part) + 2
        
        # 再次检查是否超过总长度
        if total_len > max_total_len:
            break
    
    result = "; ".join(parts)
    if len(result) > max_total_len:
        result = result[:max_total_len] + "..."
    
    return result


def _format_unified_table(tool_args: dict, result: str = None, is_sub_agent_task: bool = False, success: bool = None) -> str:
    """
    将参数字典和结果合并为一个表格。
    前几行是参数（key=value 形式），最后一行是结果。
    """
    rows = []
    
    # 根据成功/失败状态确定颜色
    if success is False:
        row_class = "args-row result-row result-fail"
        key_color = "#F44336"
    elif success is True:
        row_class = "args-row result-row result-success"
        key_color = "#5FD18C"
    else:
        row_class = "args-row result-row"
        key_color = "#9C9C9C"
    
    # 参数行
    if tool_args:
        for key, value in tool_args.items():
            if isinstance(value, dict):
                value_str = json.dumps(value).decode('utf-8')
            elif isinstance(value, list):
                value_str = json.dumps(value).decode('utf-8')
            else:
                value_str = str(value)
            
            value_str = _escape_text_for_plain(value_str)
            
            # 截断过长的值
            max_value_len = 200
            if len(value_str) > max_value_len:
                value_str = value_str[:max_value_len] + "..."
            
            escaped_key = escape(key)
            escaped_value = escape(value_str)
            
            rows.append(f'<div class="args-row">'
                        f'<span class="args-key">{escaped_key}</span>'
                        f'<span class="args-value">{escaped_value}</span>'
                        f'</div>')
    else:
        rows.append('<div class="args-row empty">无参数</div>')
    
    # 结果行（最后一行）
    result_label = "调用子智能体" if is_sub_agent_task else "结果"
    if result:
        result_text = _escape_text_for_plain(str(result))
        max_result_len = 500
        if len(result_text) > max_result_len:
            result_text = result_text[:max_result_len] + "..."
        rows.append(f'<div class="{row_class}">'
                    f'<span class="args-key" style="color: {key_color};">{result_label}</span>'
                    f'<span class="args-value">{escape(result_text)}</span>'
                    f'</div>')
    else:
        rows.append(f'<div class="{row_class}">'
                    f'<span class="args-key" style="color: {key_color};">{result_label}</span>'
                    f'<span class="args-value" style="color: #666; font-style: italic;">无结果</span>'
                    f'</div>')
    
    return f'<div class="args-table">{"".join(rows)}</div>'


def _parse_subagent_task_ids(result: str) -> str:
    """
    解析 result 中的 task_ids，返回逗号分隔的字符串。
    """
    if not result:
        return ""
    
    # 尝试解析 JSON
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            task_ids = data.get("task_ids", [])
            if task_ids:
                return ",".join(task_ids)
        elif isinstance(data, list):
            return ",".join(data)
    except (json.JSONDecodeError, TypeError):
        pass
    
    # 尝试从文本中提取 task_id（UUID 格式）
    # 使用预编译的 _UUID_PATTERN
    matches = _UUID_PATTERN.findall(result)
    if matches:
        return ",".join(matches)
    
    return ""


def render_tool_block(
    tool_name: str,
    tool_args: dict,
    result: str = None,
    success: bool = None,
    collapsed: bool = False,
    tool_call_id: str = None,
    diff: str = None,
) -> str:
    """渲染工具块，参数横向表格展示（左列参数名，右列结果值）"""

    # 检测是否为 MCP 工具（mcp__server__tool）
    is_mcp_tool = tool_name.startswith("mcp__")

    # 检测是否为子智能体任务（包括旧的 task 和新的 task_batch）
    is_sub_agent_task = tool_name in ("task", "task_batch")

    # 状态图标
    status_html = ""
    if success is not None:
        status_color = "#4CAF50" if success else "#F44336"
        status_text = "✓" if success else "✗"
        status_html = (
            f'<span style="color: {status_color}; font-weight: bold; '
            f'margin-left: 6px;">{status_text}</span>'
        )

    if is_mcp_tool:
        icon = "🌐"
        title_color = "#00BCD4"
        tool_name = "__".join(tool_name.split("__")[2:])
    elif is_sub_agent_task:
        icon = "🤖"
        title_color = "#9C27B0"
    else:
        icon = "🔧"
        title_color = "#FFA500"

    # 子智能体任务特殊处理
    if is_sub_agent_task:
        agent_name = tool_args.get("agent", "unknown")
        task_desc = tool_args.get("description", "")[:50]
        if tool_args.get("description"):
            task_desc = tool_args["description"][:50] + ("..." if len(tool_args["description"]) > 50 else "")

    # 文件编辑工具判断
    file_edit_tools = {"write", "edit"}
    is_file_edit = tool_name in file_edit_tools

    # 差异对比按钮
    diff_icon_html = ""
    if is_file_edit and tool_call_id:
        diff_icon_html = f'''
        <span class="tool-diff-icon-btn" data-tool-call-id="{escape(tool_call_id)}"
            role="button" tabindex="0"
            style="display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; background: transparent; cursor: pointer; padding: 4px; margin-left: 8px; border-radius: 4px;"
            onclick="event.stopPropagation(); window._requestToolDiff(this.dataset.toolCallId)"
            onkeydown="if(event.key === 'Enter' || event.key === ' '){{ event.preventDefault(); event.stopPropagation(); window._requestToolDiff(this.dataset.toolCallId); }}"
            title="查看文件差异">
            <img src="qrc:/icons/差异对比.svg" style="width: 16px; height: 16px;" />
        </span>'''

    # 子智能体日志查看按钮
    subagent_log_btn_html = ""
    if is_sub_agent_task:
        # 解析 task_ids
        task_ids_str = _parse_subagent_task_ids(result)
        if task_ids_str:
            subagent_log_btn_html = f'''
        <span class="tool-subagent-log-btn" data-task-ids="{escape(task_ids_str)}"
            role="button" tabindex="0"
            style="display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; background: transparent; cursor: pointer; padding: 4px; margin-left: 8px; border-radius: 4px;"
            onclick="event.stopPropagation(); window._requestSubAgentLog(this.dataset.taskIds)"
            onkeydown="if(event.key === 'Enter' || event.key === ' '){{ event.preventDefault(); event.stopPropagation(); window._requestSubAgentLog(this.dataset.taskIds); }}"
            title="查看子智能体执行日志">
            <img src="qrc:/icons/日志.svg" style="width: 16px; height: 16px;" />
        </span>'''

    # 生成参数预览（折叠时显示）
    args_preview = _format_args_preview(tool_args)
    
    # 生成统一表格：参数行 + 结果行（最后一行）
    unified_table_html = _format_unified_table(tool_args, result, is_sub_agent_task, success)
    
    # ── inline diff 预览区 ──
    diff_html = ""
    if diff:
        diff_lines = diff.split("\n")
        # 限制显示行数，过长时截断
        MAX_DIFF_LINES = 60
        truncated = False
        if len(diff_lines) > MAX_DIFF_LINES:
            truncated = True
            half = MAX_DIFF_LINES // 2
            diff_lines = diff_lines[:half] + ["... 中间省略 %d 行 ..." % (len(diff_lines) - MAX_DIFF_LINES)] + diff_lines[-half:]
        diff_rows = []
        for line in diff_lines:
            if line.startswith("+") and not line.startswith("+++"):
                cls = "diff-add"
            elif line.startswith("-") and not line.startswith("---"):
                cls = "diff-del"
            elif line.startswith("@@"):
                cls = "diff-hunk"
            else:
                cls = "diff-ctx"
            escaped = escape(line)
            diff_rows.append(f'<div class="diff-line {cls}">{escaped}</div>')
        diff_body = "".join(diff_rows)
        diff_html = f"""
        <div class="tool-diff-inline" style="margin-top: 8px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; overflow: hidden;">
            <div style="padding: 6px 12px; background: #161b22; border-bottom: 1px solid #30363d; color: #8b949e; font-size: {scale_font_size(11)}px; font-weight: 500; {get_font_family_css()}">差异预览</div>
            <div style="padding: 4px 0; font-family: Consolas, 'Courier New', monospace; font-size: {scale_font_size(12)}px; line-height: 1.5; overflow-x: auto;">
                {diff_body}
            </div>
        </div>"""
    
    # 完整的展开内容（合并的表格 + diff）
    expanded_content = f"""
    <div class="tool-expanded-content">
        {unified_table_html}
        {diff_html}
    </div>"""

    # 生成哈希 key
    block_seed = "|".join([
        str(tool_name or ""),
        json.dumps(tool_args or {}, option=json.OPT_SORT_KEYS).decode('utf-8'),
        str(result or ""),
        str(success),
    ])
    block_key = "tool-" + hashlib.sha1(block_seed.encode("utf-8")).hexdigest()[:12]
    expanded_attr = "false" if collapsed else "true"
    body_style = "" if collapsed else ' style="height:auto; opacity:1;"'

    return f"""<div class="cm-collapsible tool-block" data-block-key="{block_key}" data-expanded="{expanded_attr}" data-tool-call-id="{escape(tool_call_id or '')}" style="margin: 8px 0; background: transparent; border-radius: 6px;">
    <button type="button" class="cm-collapsible__summary tool-block__summary" aria-expanded="{expanded_attr}" style="cursor: pointer; padding: 6px 10px; color: {title_color}; font-size: {scale_font_size(13)}px; font-weight: 500; display: flex; align-items: center; gap: 10px; width: 100%; background: transparent; border: none; text-align: left; box-sizing: border-box; {get_font_family_css()}">
        <span style="display: inline-flex; align-items: center; gap: 4px; min-width: 80px; flex: 0 0 auto;">
            <span class="cm-collapsible__chevron" aria-hidden="true"></span>
            <span style="flex: 0 0 auto; {get_font_family_css()}">{icon}</span>
            <span style="white-space: nowrap; flex: 0 0 auto; {get_font_family_css()}">{escape(tool_name)}</span>
            {status_html}
        </span>
        <span style="display: flex; align-items: flex-end; gap: 8px; margin-left: 10px; min-width: 0; flex: 1 1 auto; justify-content: flex-end; overflow: hidden;">
            <span style="color: {Colors.TEXT_SECONDARY}; font-size: {scale_font_size(11)}px; text-align: right; word-break: break-all; white-space: normal; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;">
                {escape(args_preview)}
            </span>
        </span>
        <span style="display: flex; align-items: center; flex: 0 0 auto; margin-left: 8px;">
            {diff_icon_html}
            {subagent_log_btn_html}
        </span>
    </button>
    <div class="cm-collapsible__body"{body_style}>
        {expanded_content}
    </div>
</div>"""


def render_hook_block(event_name: str, content: str, collapsed: bool = True) -> str:
    """渲染 Hook 输出块（折叠样式）"""
    icon = "⚡"
    title_color = "#00BCD4"
    
    # 事件名称格式化
    event_display = event_name.replace("Pre", "Pre ").replace("Post", "Post ")
    
    # 预览文本
    max_preview = 50
    if len(content) > max_preview:
        content_preview = content[:max_preview].replace("\n", " ") + "..."
    else:
        content_preview = content.replace("\n", " ")
    
    # 生成唯一 block_key
    block_key = "hook-" + hashlib.md5(f"{event_name}:{content[:50]}".encode()).hexdigest()[:8]
    
    expanded_attr = "false" if collapsed else "true"
    body_style = "" if collapsed else ' style="height:auto; opacity:1;"'
    
    expanded_content = f"""
    <div class="hook-content" style="padding: 10px 12px; font-family: {_get_global_font()}, Consolas, monospace; font-size: {scale_font_size(12)}px; color: #e0e0e0; white-space: pre-wrap; word-break: break-word; line-height: 1.5; {get_font_family_css()}">
        {escape(content)}
    </div>
    """
    
    return f"""<div class="cm-collapsible hook-block" data-block-key="{block_key}" data-expanded="{expanded_attr}" data-hook-event="{escape(event_name)}" style="margin: 8px 0; background: transparent; border: 1px solid rgba(0, 188, 212, 0.2); border-left: 3px solid {title_color}; border-radius: 6px;">
    <button type="button" class="cm-collapsible__summary hook-block__summary" aria-expanded="{expanded_attr}" style="cursor: pointer; padding: 8px 12px; color: {title_color}; font-size: {scale_font_size(13)}px; font-weight: 500; display: flex; align-items: center; gap: 10px; width: 100%; background: transparent; border: none; text-align: left; box-sizing: border-box; {get_font_family_css()}">
        <span style="display: inline-flex; align-items: center; gap: 4px; min-width: 100px; flex: 0 0 auto;">
            <span class="cm-collapsible__chevron" aria-hidden="true"></span>
            <span style="flex: 0 0 auto; {get_font_family_css()}">{icon}</span>
            <span style="white-space: nowrap; flex: 0 0 auto; {get_font_family_css()}">{escape(event_display)}</span>
        </span>
        <span style="display: flex; align-items: flex-end; gap: 8px; margin-left: 10px; min-width: 0; flex: 1 1 auto; justify-content: flex-end; overflow: hidden;">
            <span style="color: {Colors.TEXT_SECONDARY}; font-size: {scale_font_size(11)}px; text-align: right; word-break: break-all; white-space: normal; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;">
                {escape(content_preview)}
            </span>
        </span>
    </button>
    <div class="cm-collapsible__body"{body_style}>
        {expanded_content}
    </div>
</div>"""


def format_timestamp(ts: str) -> str:
    """格式化时间戳"""
    if not ts:
        return ""
    if len(ts) > 5:
        return ts[-5:]
    return ts