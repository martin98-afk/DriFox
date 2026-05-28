# -*- coding: utf-8 -*-
"""
ToolCall 解析器模块 - 从 LLM 响应中提取和修复工具调用参数

从 ChatWorker 中提取的工具调用解析逻辑，专门负责：
1. 预编译正则表达式
2. 修复模型生成的不规范 JSON
3. 智能解析 arguments

改进说明（2026-05-15）：
- 重写 try_fix_malformed_json_arguments，不再依赖脆弱的 [^"]* 正则
- 支持带转义字符的字符串提取 (\")
- 支持截断 JSON 的自动补全
- 针对 write/edit/patch/multiedit 工具做了专门的提取优化
"""

import json
import re
from typing import Dict, Optional, Tuple


# ========== 预编译正则（用于快速初筛，不再用于精确提取） ==========
RE_HAS_PATH = re.compile(r'"path"\s*:')
RE_HAS_CONTENT = re.compile(r'"content"\s*:')
RE_HAS_OLDSTRING = re.compile(r'"oldString"\s*:')
RE_HAS_NEWSTRING = re.compile(r'"newString"\s*:')


def _find_quoted_string(text: str, start_pos: int) -> Tuple[Optional[str], int]:
    """
    从指定位置开始，查找一个 JSON 格式的字符串值。
    正确处理转义引号 \\"。

    Args:
        text: 源文本
        start_pos: 从 text[start_pos] 开始查找（期望找到第一个 "）

    Returns:
        (提取的字符串值, 结束位置（闭合 " 之后）)
        如果找不到返回 (None, -1)
    """
    quote_start = text.find('"', start_pos)
    if quote_start < 0:
        return None, -1

    i = quote_start + 1
    chars = []
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text):
            next_ch = text[i + 1]
            if next_ch == '"':
                chars.append('"')
                i += 2
            elif next_ch == '\\':
                chars.append('\\')
                i += 2
            elif next_ch == 'n':
                chars.append('\n')
                i += 2
            elif next_ch == 't':
                chars.append('\t')
                i += 2
            elif next_ch == 'r':
                chars.append('\r')
                i += 2
            else:
                chars.append(ch)
                chars.append(next_ch)
                i += 2
        elif ch == '"':
            # 找到闭合引号
            return ''.join(chars), i + 1
        else:
            chars.append(ch)
            i += 1

    # 未找到闭合引号（字符串被截断）
    # 返回已收集的内容，结束位置设为文本末尾
    return ''.join(chars), len(text)


def _extract_field(text: str, field_name: str) -> Optional[str]:
    """
    从文本中提取指定 JSON 字段的字符串值。

    Args:
        text: JSON 源文本
        field_name: 字段名（如 "path", "content"）

    Returns:
        提取的字符串值，未找到返回 None
    """
    pattern = f'"{field_name}"'
    pos = text.find(pattern)
    if pos < 0:
        return None

    # 跳过字段名和冒号
    colon = text.find(':', pos + len(pattern))
    if colon < 0:
        return None

    # 跳过空白
    i = colon + 1
    while i < len(text) and text[i] in ' \t\n\r':
        i += 1

    if i >= len(text):
        return None

    # 检查值类型
    if text[i] == '"':
        # 字符串值
        value, _ = _find_quoted_string(text, i)
        return value
    elif text[i] in '{[':
        # 对象或数组值（递归处理）
        # 这个函数只处理字符串，不处理嵌套结构
        return None
    else:
        # 数字/布尔值
        end = i
        while end < len(text) and text[end] not in ',}] \t\n\r':
            end += 1
        return text[i:end]


def _complete_truncated_json(raw: str) -> Optional[str]:
    """
    尝试补全被截断的 JSON 字符串。
    在末尾添加缺失的闭合引号、花括号、方括号。

    Args:
        raw: 可能被截断的 JSON 字符串

    Returns:
        补全后的 JSON 字符串，如果无法补全返回 None
    """
    if not raw:
        return None

    result = raw.rstrip()

    # 如果以冒号或逗号结尾，移除
    while result and result[-1] in ':,':
        result = result[:-1]

    # 检查是否有未闭合的引号
    # 简单策略：如果引号数量为奇数，补一个 "
    quote_count = result.count('"')
    # 排除转义的引号
    simple_quotes = 0
    in_escape = False
    for ch in result:
        if in_escape:
            in_escape = False
            continue
        if ch == '\\':
            in_escape = True
            continue
        if ch == '"':
            simple_quotes += 1

    if simple_quotes % 2 == 1:
        result += '"'

    # 检查花括号和方括号是否平衡
    stack = []
    in_str = False
    escaped = False
    for ch in result:
        if escaped:
            escaped = False
            continue
        if ch == '\\':
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in '{[':
            stack.append(ch)
        elif ch == '}':
            if stack and stack[-1] == '{':
                stack.pop()
        elif ch == ']':
            if stack and stack[-1] == '[':
                stack.pop()

    # 补全缺失的闭合符号
    for closing in reversed(stack):
        if closing == '{':
            result += '}'
        elif closing == '[':
            result += ']'

    return result


def _escape_json_string(s: str) -> str:
    """
    对字符串进行 JSON 转义，确保可以作为 JSON 中的字符串值。
    """
    result = []
    for ch in s:
        if ch == '"':
            result.append('\\"')
        elif ch == '\\':
            result.append('\\\\')
        elif ch == '\n':
            result.append('\\n')
        elif ch == '\t':
            result.append('\\t')
        elif ch == '\r':
            result.append('\\r')
        elif ord(ch) < 0x20:
            result.append(f'\\u{ord(ch):04x}')
        else:
            result.append(ch)
    return ''.join(result)


def _try_standard_parse(raw: str) -> Optional[Dict]:
    """尝试标准 JSON 解析"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _try_parse_with_unescape(raw: str) -> Optional[Dict]:
    """
    尝试修复常见的 JSON 转义问题后解析。
    模型常见错误：字符串内的 " 没有转义、反斜杠没有转义。
    """
    if not raw:
        return None

    # 问题1：字符串内未转义的引号
    # 策略：逐个字符遍历，在字符串值内部将未转义的 " 转义
    # 但这个很难完美处理，先尝试一个简化版：逐层尝试修复

    fixed = _unescape_string_values(raw)
    if fixed != raw:
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

    return None


def _unescape_string_values(raw: str) -> str:
    """
    尝试修复字符串值中未转义的引号。
    仅在 JSON 结构清晰的场景下使用。

    这不会尝试处理所有情况，只处理常见的模型错误：
    - 字符串中的 " 没有被转义（会导致 JSON 解析在错误位置结束字符串）
    """
    result = []
    in_string = False
    escaped = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if escaped:
            result.append(ch)
            escaped = False
            i += 1
            continue
        if ch == '\\':
            result.append(ch)
            escaped = True
            i += 1
            continue
        if ch == '"':
            if in_string:
                # 检查是否是真正的字符串结束
                # 如果下一个有效字符是 , ] }，则是正常结束
                next_pos = i + 1
                while next_pos < len(raw) and raw[next_pos] in ' \t\n\r':
                    next_pos += 1
                if next_pos < len(raw) and raw[next_pos] in ',]}':
                    # 正常结束
                    result.append(ch)
                    in_string = False
                else:
                    # 这是一个字符串内部的未转义引号
                    result.append('\\"')
            else:
                result.append(ch)
                in_string = True
            i += 1
            continue
        result.append(ch)
        i += 1

    return ''.join(result)


def _rebuild_write_json(raw: str) -> Optional[Dict]:
    """
    针对 write 工具，从可能不完整的 JSON 中重建参数。
    """
    path = _extract_field(raw, "path")
    if not path:
        return None

    content = _extract_field(raw, "content")
    if content is None:
        # 尝试将 path 之后的所有内容作为 content
        # （某些 truncated 场景下 content 字段不完整）
        path_end = raw.find('"path"')
        if path_end >= 0:
            # 找到 path 值结束后的位置
            colon = raw.find(':', path_end + 6)
            if colon >= 0:
                _, val_end = _find_quoted_string(raw, colon + 1)
                if val_end > 0:
                    # 看之后是否有 content
                    rest = raw[val_end:].strip()
                    if rest.startswith(','):
                        rest = rest[1:].strip()
                    if rest.startswith('"content"'):
                        # 有 content 字段但可能截断
                        content_start = rest.find(':')
                        if content_start >= 0:
                            rest_after_colon = rest[content_start + 1:].strip()
                            if rest_after_colon.startswith('"'):
                                content_val, _ = _find_quoted_string(rest, content_start + 1)
                                if content_val:
                                    content = content_val
                                else:
                                    # 截断了，取所有剩余内容
                                    # 移除末尾的无关符号
                                    remaining = rest_after_colon
                                    if remaining.startswith('"'):
                                        remaining = remaining[1:]
                                    # 移除末尾的 } 和空白
                                    remaining = remaining.rstrip()
                                    while remaining and remaining[-1] in '},]':
                                        remaining = remaining[:-1].rstrip()
                                    content = remaining

    if content is None:
        # 既没有 path 也没有 content
        return None

    return {"path": path, "content": content}


def _rebuild_edit_json(raw: str) -> Optional[Dict]:
    """
    针对 edit 工具，从可能不完整的 JSON 中重建参数。
    尝试提取 path 和 operations 数组。
    """
    path = _extract_field(raw, "path")
    if not path:
        return None

    result = {"path": path}

    # 尝试提取 operations 数组
    ops_start = raw.find('"operations"')
    if ops_start >= 0:
        colon = raw.find(':', ops_start)
        if colon >= 0:
            arr_start = raw.find('[', colon)
            if arr_start >= 0:
                depth = 0
                arr_end = -1
                for i in range(arr_start, len(raw)):
                    if raw[i] == '[':
                        depth += 1
                    elif raw[i] == ']':
                        depth -= 1
                        if depth == 0:
                            arr_end = i + 1
                            break
                if arr_end > arr_start:
                    try:
                        import json
                        ops = json.loads(raw[arr_start:arr_end])
                        if ops:
                            result["operations"] = ops
                            return result
                    except json.JSONDecodeError:
                        pass

    return None

def _rebuild_bash_json(raw: str) -> Optional[Dict]:
    """针对 bash 工具重建参数"""
    command = _extract_field(raw, "command")
    if command is None:
        return None
    return {"command": command}


def try_fix_malformed_json_arguments(raw_args: str, tool_name: str) -> Tuple[Optional[Dict], str]:
    """
    尝试修复模型生成的不规范 JSON。

    模型有时会生成不规范的 JSON，例如：
    - content 字段包含未转义的引号
    - JSON 被 max_tokens 截断
    - 缺少闭合引号/花括号
    - 字符串值中包含多行文本未正确转义

    Args:
        raw_args: 原始参数字符串
        tool_name: 工具名称（用于特定工具的修复策略）

    Returns:
        (修复后的参数字典, 修复状态字符串)
    """
    if not raw_args or not isinstance(raw_args, str):
        return None, "empty_or_invalid_input"

    # 策略 1: 先尝试补全截断的 JSON
    completed = _complete_truncated_json(raw_args)
    if completed and completed != raw_args:
        try:
            result = json.loads(completed)
            if isinstance(result, dict) and result:
                return result, "fixed_truncated"
        except json.JSONDecodeError:
            pass

    # 策略 2: 尝试修复 unescape 问题
    unescaped = _unescape_string_values(raw_args)
    if unescaped != raw_args:
        try:
            result = json.loads(unescaped)
            if isinstance(result, dict) and result:
                return result, "fixed_unescape"
        except json.JSONDecodeError:
            pass

    # 策略 3: 针对特定工具重建 JSON
    rebuilders = {
        "write": _rebuild_write_json,
        "edit": _rebuild_edit_json,
        "bash": _rebuild_bash_json,
    }

    rebuilder = rebuilders.get(tool_name)
    if rebuilder:
        rebuilt = rebuilder(raw_args)
        if rebuilt:
            return rebuilt, f"fixed_{tool_name}"

    # 策略 4: 尝试提取常见参数（通用方案）
    common_args = {}
    for field in ("path", "filePath", "command", "url", "query", "pattern",
                  "name", "question"):
        val = _extract_field(raw_args, field)
        if val is not None:
            key = "path" if field == "filePath" else field
            common_args[key] = val

    if common_args:
        return common_args, "fixed_common"

    return None, "fix_failed"


def smart_parse_arguments(raw_args: str, tool_name: str) -> Optional[Dict]:
    """
    智能解析 arguments，多级容错机制：

    1. 标准 JSON 解析（最快路径）
    2. 补全截断 JSON 后解析
    3. 修复未转义字符后解析
    4. 工具特定参数重建
    5. 通用字段提取

    Args:
        raw_args: 原始参数字符串
        tool_name: 工具名称

    Returns:
        解析后的参数字典，全部失败返回 None（空字符串返回空字典）
    """
    if not raw_args:
        return {}

    # 级别 1: 标准 JSON 解析
    parsed = _try_standard_parse(raw_args)
    if parsed is not None:
        return parsed

    # 级别 2: 修复转义问题后解析
    parsed = _try_parse_with_unescape(raw_args)
    if parsed is not None:
        return parsed

    # 级别 3-4: 完整的容错修复
    fixed_args, status = try_fix_malformed_json_arguments(raw_args, tool_name)
    if fixed_args:
        return fixed_args

    return None
