# -*- coding: utf-8 -*-
"""
UI 渲染辅助函数
"""

import difflib
import hashlib
import os
import re
from html import escape

import orjson as json

from app.utils.design_tokens import Colors, _get_global_font, scale_font_size
from app.utils.utils import get_font_family_css

# ===== Pygments 语法高亮（行内 diff 代码着色，复用与 message_card 一致的 dracula 主题）=====
# 注意：render_helpers 被 message_card 反向依赖，若从 message_card 导入会形成循环导入，
# 因此在此处就地维护一套带缓存的轻量着色逻辑（与 message_card 的 lexer/formatter 模式一致）。
from pygments import highlight as _pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, get_lexer_by_name, TextLexer

# 行内 diff 专用 formatter（缓存）：按风格切换，nowrap 不包裹 <pre>，noclasses 输出内联 color 的 token <span>
_DIFF_FORMATTER_CACHE: dict = {"style": None, "formatter": None}


# 行内 diff 高亮风格（与 message_card.py 同步，由 set_diff_highlight_style 切换）
_current_diff_style = "dracula"


def set_diff_highlight_style(style_name: str):
    """设置 diff 高亮风格并清除缓存"""
    global _current_diff_style
    if style_name != _current_diff_style:
        _current_diff_style = style_name
        _DIFF_FORMATTER_CACHE["style"] = None


def _get_diff_formatter():
    """获取当前 diff 高亮 formatter，随主题风格切换重建"""
    style = _current_diff_style
    if _DIFF_FORMATTER_CACHE["style"] != style:
        _DIFF_FORMATTER_CACHE["style"] = style
        _DIFF_FORMATTER_CACHE["formatter"] = HtmlFormatter(nowrap=True, style=style, noclasses=True)
    return _DIFF_FORMATTER_CACHE["formatter"]


_TEXT_LEXER = TextLexer()
_DIFF_LEXER_CACHE: dict = {}

# 扩展名 → pygments lexer 别名（get_lexer_for_filename 找不到时的兜底）
_EXT_LEXER_MAP = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".json": "json",
    ".jsonc": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".fish": "bash",
    ".sql": "sql",
    ".xml": "xml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".lua": "lua",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".r": "r",
    ".pl": "perl",
    ".pm": "perl",
    ".dart": "dart",
    ".vue": "vue",
    ".dockerfile": "docker",
    ".mk": "makefile",
    ".cmake": "cmake",
    ".tf": "hcl",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".scala": "scala",
    ".groovy": "groovy",
    ".ps1": "powershell",
    ".bat": "batch",
}


def _get_diff_lexer(path: str):
    """根据文件路径推断 lexer，按扩展名缓存，避免重复构造（构造开销大）"""
    if not path or path == "/dev/null":
        return _TEXT_LEXER
    key = os.path.splitext(path)[1].lower() or path
    cached = _DIFF_LEXER_CACHE.get(key)
    if cached is not None:
        return cached
    lex = _TEXT_LEXER
    try:
        lex = get_lexer_for_filename(path)
    except Exception:
        alias = _EXT_LEXER_MAP.get(key)
        if alias:
            try:
                lex = get_lexer_by_name(alias)
            except Exception:
                lex = _TEXT_LEXER
    _DIFF_LEXER_CACHE[key] = lex
    return lex


def _highlight_code_line(text: str, lexer) -> str:
    """对单行代码做语法高亮，返回带内联 color 的 HTML（nowrap，无 <pre> 包裹）

    注意：Pygments 在 nowrap 模式下会在输出末尾追加一个 "\\n"。词级差异会把每个
    词段单独高亮后拼接，若保留该换行，整行会被切碎、出现多余空白与异常换行。
    这里统一剥掉末尾换行（高亮的都是单行/单词段，不含真实换行）。
    """
    if lexer is None or lexer is _TEXT_LEXER:
        return escape(text)
    try:
        return _pyg_highlight(text, lexer, _get_diff_formatter()).rstrip("\n")
    except Exception:
        return escape(text)


def _highlighted_word_diff_html(old_text: str, new_text: str, lexer) -> tuple:
    """词级差异高亮（背景叠加）+ 每段语法高亮，返回 (old_html, new_html)

    在原有词级差异（.word-del/.word-add 背景叠加）基础上，对每个词段再做
    Pygments 着色，使"改了什么"和"语法结构"同时可见。
    """
    if len(old_text) + len(new_text) > 2000:
        return _highlight_code_line(old_text, lexer), _highlight_code_line(new_text, lexer)
    old_tokens = _WORD_RE.findall(old_text) or [old_text]
    new_tokens = _WORD_RE.findall(new_text) or [new_text]
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    old_parts = []
    new_parts = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            old_parts.append(_highlight_code_line("".join(old_tokens[i1:i2]), lexer))
            new_parts.append(_highlight_code_line("".join(new_tokens[j1:j2]), lexer))
        elif tag == "delete":
            old_parts.append(f'<span class="word-del">{_highlight_code_line("".join(old_tokens[i1:i2]), lexer)}</span>')
        elif tag == "insert":
            new_parts.append(f'<span class="word-add">{_highlight_code_line("".join(new_tokens[j1:j2]), lexer)}</span>')
        elif tag == "replace":
            old_parts.append(f'<span class="word-del">{_highlight_code_line("".join(old_tokens[i1:i2]), lexer)}</span>')
            new_parts.append(f'<span class="word-add">{_highlight_code_line("".join(new_tokens[j1:j2]), lexer)}</span>')
    return "".join(old_parts), "".join(new_parts)


# 预编译正则表达式（模块级别缓存，避免重复编译）
_CODE_BLOCK_PATTERN = re.compile(r"```[\w]*\n")
_CODE_BLOCK_FINAL_PATTERN = re.compile(r"```")
# 匹配 HTML 代码块标签
_HTML_CODE_BLOCK_PATTERN = re.compile(r"<(pre|code)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
# HTML 标签清理正则（避免每次调用 re.sub）
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
# UUID 模式（用于提取 task_id）
_UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# Null 字符清理（编译一次，多次使用）
_NULL_CHAR = "\x00"  # 避免 str.replace 被重复调用


def format_tool_block(
    tool_name: str,
    tool_args: dict,
    result: str = None,
    success: bool = True,
) -> str:
    """格式化工具块为纯文本标记，用于存储"""
    args_json = json.dumps(tool_args).decode("utf-8")
    result_str = str(result) if result else ""

    return f"<tool>\nname: {tool_name}\nargs: {args_json}\nresult: {result_str}\nsuccess: {success}\n</tool>"


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
        s = json.dumps(v).decode("utf-8")
        return s[:max_len] + "..." if len(s) > max_len else s
    elif isinstance(v, list):
        s = json.dumps(v).decode("utf-8")
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


def _format_unified_table(
    tool_args: dict, result: str = None, is_sub_agent_task: bool = False, success: bool = None
) -> str:
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
                value_str = json.dumps(value).decode("utf-8")
            elif isinstance(value, list):
                value_str = json.dumps(value).decode("utf-8")
            else:
                value_str = str(value)

            value_str = _escape_text_for_plain(value_str)

            # 截断过长的值
            max_value_len = 200
            if len(value_str) > max_value_len:
                value_str = value_str[:max_value_len] + "..."

            escaped_key = escape(key)
            escaped_value = escape(value_str)

            rows.append(
                f'<div class="args-row">'
                f'<span class="args-key">{escaped_key}</span>'
                f'<span class="args-value">{escaped_value}</span>'
                f"</div>"
            )
    else:
        rows.append('<div class="args-row empty">无参数</div>')

    # 结果行（最后一行）
    result_label = "调用子智能体" if is_sub_agent_task else "结果"
    if result:
        result_text = _escape_text_for_plain(str(result))
        max_result_len = 500
        if len(result_text) > max_result_len:
            result_text = result_text[:max_result_len] + "..."
        rows.append(
            f'<div class="{row_class}">'
            f'<span class="args-key" style="color: {key_color};">{result_label}</span>'
            f'<span class="args-value">{escape(result_text)}</span>'
            f"</div>"
        )
    else:
        rows.append(
            f'<div class="{row_class}">'
            f'<span class="args-key" style="color: {key_color};">{result_label}</span>'
            f'<span class="args-value" style="color: #666; font-style: italic;">无结果</span>'
            f"</div>"
        )

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
    except json.JSONDecodeError, TypeError:
        pass

    # 尝试从文本中提取 task_id（UUID 格式）
    # 使用预编译的 _UUID_PATTERN
    matches = _UUID_PATTERN.findall(result)
    if matches:
        return ",".join(matches)

    return ""


_WORD_RE = re.compile(r"(\w+|\W+)")


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+),?\d* \+(\d+),?\d* @@(.*)")


def _summarize_diff(diff_text: str) -> dict:
    """Return lightweight stats for inline diff badges and headers."""
    added = 0
    deleted = 0
    files = []
    pending_old_path = ""

    def _clean_path(path: str) -> str:
        path = path.strip()
        if path.startswith("a/") or path.startswith("b/"):
            path = path[2:]
        return path

    for line in diff_text.splitlines():
        if line.startswith("--- "):
            pending_old_path = _clean_path(line[4:])
            continue
        if line.startswith("+++ "):
            new_path = _clean_path(line[4:])
            display = new_path or pending_old_path
            if display and display != "/dev/null" and display not in files:
                files.append(display)
            pending_old_path = ""
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1

    return {"added": added, "deleted": deleted, "files": files}


def _render_diff_preview(diff_text: str) -> str:
    """
    将 unified diff 渲染为带语法高亮、段落级差异的 HTML。

    - 相邻的增/删差异（包括被 hunk 头隔开的紧邻小改动）聚合成一个
      「差异段」，差异段同时输出两套视图：
      · 单列视图（.diff-seg-col，默认）：所有删除行先、所有新增行后，
        配对行做词级差异高亮（与双列共用同一份 _highlighted_word_diff_html
        输出，未配对行降级为整行高亮
      · 双列视图（.diff-seg-paired，split-view）：左右对照的配对行 + 词级差异高亮
    超过 500 行时截断并显示行数。
    """
    lines = diff_text.split("\n")[1:]
    # 去掉 split 产生的尾随空行（diff 文本通常以一个换行结尾），避免渲染出多余空行
    while lines and lines[-1] == "":
        lines.pop()
    MAX_LINES = 500
    truncated = False
    if len(lines) > MAX_LINES:
        truncated = True
        half = MAX_LINES // 2
        shown = len(lines) - MAX_LINES
        lines = lines[:half] + [None] + lines[-half:]

    def _clean_path(p: str) -> str:
        p = p.strip()
        if p.startswith("a/") or p.startswith("b/"):
            p = p[2:]
        return p

    # ---- 1. 解析为带类型的行对象 ----
    parsed = []
    old_ln = new_ln = 0
    i = 0
    pending_old = None
    current_lexer = _TEXT_LEXER  # 随 +++ 文件头切换，用于逐行语法高亮
    while i < len(lines):
        line = lines[i]
        if line is None:
            parsed.append({"kind": "truncated"})
            i += 1
            continue
        if line.startswith("--- "):
            pending_old = line
            i += 1
            continue
        if line.startswith("+++ "):
            new_path = _clean_path(line[4:])
            old_path = _clean_path(pending_old[4:]) if pending_old else ""
            if old_path == new_path:
                display = old_path
            elif old_path and new_path:
                display = f"{old_path} → {new_path}"
            else:
                display = new_path or old_path
            current_lexer = _get_diff_lexer(new_path or old_path)
            parsed.append({"kind": "file", "text": display, "lexer": current_lexer})
            pending_old = None
            i += 1
            continue
        if pending_old:
            # 单独的 --- 行（没有 +++ 跟随）
            parsed.append({"kind": "file", "text": _clean_path(pending_old[4:]), "lexer": current_lexer})
            pending_old = None
            continue
        if line.startswith("@@"):
            m = _HUNK_HEADER_RE.match(line)
            if m:
                old_ln = int(m.group(1))
                new_ln = int(m.group(2))
            parsed.append({"kind": "hunk", "text": line})
            i += 1
            continue
        if line.startswith("-") and not line.startswith("---"):
            parsed.append({"kind": "del", "text": line[1:], "old_ln": old_ln, "new_ln": new_ln, "lexer": current_lexer})
            old_ln += 1
            i += 1
            continue
        if line.startswith("+") and not line.startswith("+++"):
            parsed.append({"kind": "add", "text": line[1:], "old_ln": old_ln, "new_ln": new_ln, "lexer": current_lexer})
            new_ln += 1
            i += 1
            continue
        # 上下文行（unified diff 上下文带前导空格）；其余元信息行（index / \ No newline 等）跳过，不占行号
        if line.startswith(" ") or line == "":
            stripped = line[1:] if line.startswith(" ") else line
            parsed.append({"kind": "ctx", "text": stripped, "old_ln": old_ln, "new_ln": new_ln, "lexer": current_lexer})
            old_ln += 1
            new_ln += 1
        i += 1

    # ---- 2. 聚合成段落级差异段 ----
    segments = []
    cur = None  # 当前差异段（仅含 del/add）

    def _flush():
        nonlocal cur
        if cur:
            segments.append(cur)
            cur = None

    for p in parsed:
        k = p["kind"]
        if k in ("del", "add"):
            if cur is None:
                cur = []
            cur.append(p)
        elif k == "hunk":
            # hunk 头不打断相邻差异段的聚合（紧邻小改动合并为一段）
            if cur is None:
                segments.append(p)
        else:  # file / ctx / truncated
            _flush()
            segments.append(p)
    _flush()

    # ---- 3. 渲染 ----
    def _cell(kind, ln, sign, code_html, empty=False):
        if empty:
            return (
                '<div class="diff-line diff-seg-empty">'
                '<span class="line-num">&nbsp;</span>'
                '<span class="line-sign"></span>'
                '<span class="line-code">&nbsp;</span></div>'
            )
        cls = "diff-del" if kind == "del" else "diff-add"
        return (
            f'<div class="diff-line {cls}">'
            f'<span class="line-num">{ln}</span>'
            f'<span class="line-sign">{sign}</span>'
            f'<span class="line-code">{code_html}</span></div>'
        )

    rows = []
    _prev_blank = False  # 折叠连续空上下文行，只保留一条细分隔线
    for seg in segments:
        if isinstance(seg, list):
            _prev_blank = False
            dels = [p for p in seg if p["kind"] == "del"]
            adds = [p for p in seg if p["kind"] == "add"]
            pair = min(len(dels), len(adds))

            # === 双模式差异段 ===
            # .diff-seg-col（单列默认）：所有删除先、所有新增后（带词级高亮）
            # .diff-seg-paired（双列 split-view）：左右对照的配对行
            rows.append('<div class="diff-segment">')

            # ── 单列视图：所有删除行先、所有新增行后（带词级高亮） ──
            # 先把配对行的词级高亮 HTML 算好缓存到 paired_htmls，
            # 再分两段输出：先 del 全打，再 add 全打——避免 del/add 交替时
            # 既要保持 "del→add" 配对又得来回切上下文。
            # TODO(refactor): 抽出 paired-row 渲染辅助函数与双列分支共用，避免再出"忘了同步"回归
            rows.append('<div class="diff-seg-col">')
            paired_htmls = []
            for k in range(pair):
                od, oa = dels[k], adds[k]
                old_html, new_html = _highlighted_word_diff_html(od["text"], oa["text"], od["lexer"])
                paired_htmls.append((od, old_html, oa, new_html))

            # 1) 所有删除行
            for k in range(pair):
                od, old_html, _, _ = paired_htmls[k]
                rows.append(_cell("del", od["old_ln"], "-", old_html))
            for k in range(pair, len(dels)):
                od = dels[k]
                rows.append(_cell("del", od["old_ln"], "-", _highlight_code_line(od["text"], od["lexer"])))

            # 2) 所有新增行
            for k in range(pair):
                _, _, oa, new_html = paired_htmls[k]
                rows.append(_cell("add", oa["new_ln"], "+", new_html))
            for k in range(pair, len(adds)):
                oa = adds[k]
                rows.append(_cell("add", oa["new_ln"], "+", _highlight_code_line(oa["text"], oa["lexer"])))
            rows.append("</div>")

            # ── 双列视图：配对行（旧左新右），带词级高亮 ──
            rows.append('<div class="diff-seg-paired">')
            for k in range(pair):
                od, oa = dels[k], adds[k]
                old_html, new_html = _highlighted_word_diff_html(od["text"], oa["text"], od["lexer"])
                rows.append('<div class="diff-seg-row">')
                rows.append(_cell("del", od["old_ln"], "-", old_html))
                rows.append(_cell("add", oa["new_ln"], "+", new_html))
                rows.append("</div>")
            for k in range(pair, len(dels)):
                od = dels[k]
                rows.append('<div class="diff-seg-row">')
                rows.append(_cell("del", od["old_ln"], "-", _highlight_code_line(od["text"], od["lexer"])))
                rows.append(_cell("add", "", "", "", empty=True))
                rows.append("</div>")
            for k in range(pair, len(adds)):
                oa = adds[k]
                rows.append('<div class="diff-seg-row">')
                rows.append(_cell("del", "", "", "", empty=True))
                rows.append(_cell("add", oa["new_ln"], "+", _highlight_code_line(oa["text"], oa["lexer"])))
                rows.append("</div>")
            rows.append("</div>")  # /.diff-seg-paired

            rows.append("</div>")  # /.diff-segment
        elif seg["kind"] == "file":
            _prev_blank = False
            rows.append(
                f'<div class="diff-line diff-file-header diff-meta">'
                f'<span class="line-num">&nbsp;</span>'
                f'<span class="line-sign"></span>'
                f'<span class="line-code" style="color: #8b949e; font-weight: 600;">{escape(seg["text"])}</span></div>'
            )
        elif seg["kind"] == "hunk":
            _prev_blank = False
            rows.append(
                f'<div class="diff-line diff-hunk diff-meta">'
                f'<span class="line-num">&nbsp;</span>'
                f'<span class="line-sign"></span>'
                f'<span class="line-code">{escape(seg["text"])}</span></div>'
            )
        elif seg["kind"] == "truncated":
            _prev_blank = False
            rows.append(
                f'<div class="diff-line diff-truncated diff-meta">'
                f'<span class="line-num">&nbsp;</span>'
                f'<span class="line-sign"></span>'
                f'<span class="line-code">⋯ 省略 {shown} 行 ⋯</span></div>'
            )
        else:  # ctx
            # 空白上下文行（源文件里的空行）折叠成一条紧凑细分隔线，避免单列模式下
            # 段落差异之间出现 bulky 的空行。连续多个空行只保留第一条。
            if seg["text"].strip() == "":
                if _prev_blank:
                    continue
                _prev_blank = True
                rows.append(
                    '<div class="diff-line diff-ctx diff-ctx-blank">'
                    '<span class="line-num">&nbsp;</span>'
                    '<span class="line-sign"></span>'
                    '<span class="line-code">&nbsp;</span></div>'
                )
                continue
            _prev_blank = False
            rows.append(
                f'<div class="diff-line diff-ctx">'
                f'<span class="line-num">{seg["new_ln"] if seg["new_ln"] > 0 else ""}</span>'
                f'<span class="line-sign"></span>'
                f'<span class="line-code">{_highlight_code_line(seg["text"], seg["lexer"])}</span></div>'
            )

    return "".join(rows)


# 内建工具图标映射（按模块×操作类型分类 → SVG 图标文件名）
_TOOL_ICON_MAP = {
    # 文件工具 - 读取
    "read": "read",
    "todoread": "todo",
    # 文件工具 - 写入/编辑
    "write": "编辑",
    "edit": "编辑",
    "multi_edit": "编辑",
    "todowrite": "todo",
    # 文件工具 - 搜索/扫描
    "grep": "Search",
    "glob": "Search",
    "list": "folder",
    "scan_repo": "Search",
    "stage_files": "Search",
    # 终端/后台命令
    "bash": "shell",
    "bg_start": "shell",
    "bg_stop": "shell",
    "bg_logs": "shell",
    "bg_list": "shell",
    # 网络工具
    "websearch": "websearch",
    "webfetch": "websearch",
    # 子智能体任务
    "subagent_para": "设置-subagent",
    "subagent_status": "设置-subagent",
    "subagent_dag": "设置-subagent",
    # 技能工具
    "skill": "技能",
    "list_skills": "技能",
    # 提问工具
    "question": "question",
    # 诊断工具
    "get_diagnostics": "工具",
    # 截图工具
    "screenshot": "裁剪",
    "mouse": "鼠标",
    "keyboard": "233键盘-线性",
    # LSP 工具（默认 = 工具图标；具体 operation 由 _get_tool_icon 解析）
    "lsp": "工具",
    # CodeGraph 代码智能
    "codegraph_explore": "Search",
    # 上传文件
    "upload_file": "upload-file",
}

# 工具名 → 中文显示名
_TOOL_CN_NAME_MAP = {
    "read": "读取",
    "todoread": "查看待办",
    "write": "写入",
    "edit": "编辑",
    "multi_edit": "批量编辑",
    "todowrite": "更新待办",
    "grep": "搜索",
    "glob": "匹配",
    "list": "列出文件",
    "scan_repo": "扫描仓库",
    "stage_files": "标记文件",
    "bash": "执行命令",
    "bg_start": "后台启动",
    "bg_stop": "后台停止",
    "bg_logs": "后台日志",
    "bg_list": "后台列表",
    "websearch": "网页搜索",
    "webfetch": "抓取网页",
    "subagent_para": "分发任务",
    "subagent_status": "查询任务状态",
    "subagent_dag": "分发工作流",
    "skill": "加载技能",
    "list_skills": "列出技能",
    "question": "提问",
    "get_diagnostics": "诊断",
    "screenshot": "截图",
    "mouse": "鼠标",
    "keyboard": "键盘",
    "lsp": "LSP",
    "codegraph_explore": "代码探索",
    "upload_file": "上传文件",
}


# LSP 工具 operation → 图标（SVG 图标名）
_LSP_OPERATION_ICON_MAP = {
    "diagnostics": "工具",
    "documentSymbols": "Search",
    "goToDefinition": "Search",
    "findReferences": "Search",
    "hover": "question",
    "listServers": "folder",
}


def _get_tool_icon_name(tool_name: str, tool_args: dict = None) -> str:
    """根据工具名（必要时结合 tool_args）查找图标文件名（不含扩展名）

    普通工具直接查 _TOOL_ICON_MAP；
    LSP 工具按 operation 参数切换图标。
    """
    if tool_name == "lsp" and tool_args:
        operation = tool_args.get("operation", "")
        if operation in _LSP_OPERATION_ICON_MAP:
            return _LSP_OPERATION_ICON_MAP[operation]
    return _TOOL_ICON_MAP.get(tool_name, "工具")


def _get_tool_cn_name(tool_name: str) -> str:
    """获取工具的中文显示名"""
    # MCP 工具：返回服务名
    if tool_name.startswith("mcp__"):
        return "__".join(tool_name.split("__")[2:]) if len(tool_name.split("__")) > 2 else "MCP"
    if tool_name == "mcp_list_servers":
        return "MCP列表"
    return _TOOL_CN_NAME_MAP.get(tool_name, tool_name)


def _get_tool_icon_html(icon_name: str, size: int = 18) -> str:
    """生成工具图标的 HTML <img> 标签（主题感知）

    根据当前主题选择 qrc:/icons 或 qrc:/icons_light 前缀。
    """
    try:
        from app.utils.theme_manager import theme_manager

        prefix = "qrc:/icons_light" if theme_manager.is_light_theme() else "qrc:/icons"
    except Exception:
        prefix = "qrc:/icons"
    return f'<img src="{prefix}/{icon_name}.svg" style="width:{size}px;height:{size}px;pointer-events:none;" />'


def _get_tool_icon(tool_name: str, tool_args: dict = None) -> str:
    """[已弃用] 根据工具名查找图标（保留兼容，返回图标文件名）

    新代码请使用 _get_tool_icon_name + _get_tool_icon_html 组合。
    """
    return _get_tool_icon_name(tool_name, tool_args)


def _extract_screenshot_image_path(result: str) -> str:
    """从 screenshot 工具结果字符串中提取截图文件绝对路径

    result 格式类似 Python dict str():
        {'path': 'D:/...png', 'absolute_path': 'D:/...png', ...}
    """
    if not result:
        return ""

    # 策略1: ast.literal_eval 解析 Python dict 字面量
    try:
        import ast

        data = ast.literal_eval(result)
        if isinstance(data, dict):
            path = data.get("absolute_path") or data.get("path") or ""
            if path and os.path.isfile(path):
                return path
    except ValueError, SyntaxError, MemoryError:
        pass

    # 策略2: 正则提取 'absolute_path': '...' 或 'path': '...'
    for key in ("absolute_path", "path"):
        m = re.search(r"""['"]""" + key + r"""['"]\s*:\s*['"]([^'"]+\.png)['"]""", result)
        if m:
            path = m.group(1)
            if os.path.isfile(path):
                return path

    # 策略3: 直接匹配 .png 的绝对路径
    m = re.search(r"""['"]([A-Za-z]:[^'"]+\.png)['"]""", result)
    if m:
        path = m.group(1)
        if os.path.isfile(path):
            return path

    return ""


def _to_rel_path(path: str) -> str:
    """将绝对路径转为相对项目根目录的路径（便于预览展示）"""
    if not path or not os.path.isabs(path):
        return path
    try:
        cwd = os.getcwd()
        # normpath 统一分隔符后再比较
        if os.path.normpath(path).startswith(os.path.normpath(cwd)):
            rel = os.path.relpath(path, cwd)
            return rel.replace("\\", "/")
    except ValueError, OSError:
        pass
    return path


# 参数展示型工具 — 渲染为紧凑单行卡片（无折叠、无 body、无工具结果）
_INLINE_TOOLS = frozenset(
    {
        "read",
        "todoread",
        "grep",
        "glob",
        "list",
        "scan_repo",
        "stage_files",
        "get_diagnostics",
    }
)


def _format_natural_preview(tool_name: str, tool_args: dict) -> str:
    """将工具调用转为自然语言描述（用于内联卡片和折叠头的预览）"""
    # todoread 等即使无参数也应有描述
    if tool_name == "todoread":
        label = "查看待办事项"
        offset = tool_args.get("offset")
        limit = tool_args.get("limit")
        if offset is not None and limit is not None and offset > 1:
            label += f" (第 {offset}-{offset + limit - 1} 行)"
        elif offset is not None and offset > 1:
            label += f" (从第 {offset} 行)"
        elif limit is not None:
            label += f" (前 {limit} 行)"
        return label

    # ── 无参数也有描述的工具 ──
    if tool_name == "list_skills":
        return "列出可用技能"
    if tool_name == "mcp_list_servers":
        return "列出 MCP 服务器"
    if tool_name == "screenshot":
        region = tool_args.get("region")
        if region and isinstance(region, (list, tuple)) and len(region) == 4:
            return f"截取屏幕 ({region[2]}×{region[3]})"
        return "截取屏幕"

    if not tool_args:
        return ""
    desc = ""
    if tool_name == "read":
        raw = tool_args.get("path") or tool_args.get("file_path") or ""
        path = _to_rel_path(raw.rstrip("/").rstrip("\\")) if raw else ""
        if path:
            desc = f'读取 "{path}"'
        else:
            desc = "读取文件"
        startline = tool_args.get("startline")
        endline = tool_args.get("endline")
        if startline is not None and endline is not None:
            desc += f" (第 {startline}-{endline} 行)"
        elif startline is not None and startline > 1:
            desc += f" (从第 {startline} 行)"
        elif endline is not None:
            desc += f" (前 {endline} 行)"
    elif tool_name == "grep":
        pattern = tool_args.get("pattern", "")
        path = _to_rel_path(tool_args.get("path", ""))
        include = tool_args.get("include", "")
        desc = f'搜索 "{pattern}"'
        parts = []
        if path:
            parts.append(path)
        if include:
            parts.append(include)
        if parts:
            desc += " (" + ", ".join(parts) + ")"
    elif tool_name == "glob":
        pattern = tool_args.get("pattern", "")
        path = _to_rel_path(tool_args.get("path", ""))
        desc = f'匹配 "{pattern}"' if pattern else "文件匹配"
        if path:
            desc += f" ({path})"
    elif tool_name == "list":
        path = _to_rel_path(tool_args.get("path", "."))
        desc = f"{path}"
    elif tool_name == "scan_repo":
        path = _to_rel_path(tool_args.get("path", "."))
        desc = f"扫描仓库 {path}" if path != "." else "扫描仓库"
        max_depth = tool_args.get("max_depth")
        if max_depth is not None:
            desc += f" (深度 {max_depth})"
    elif tool_name == "stage_files":
        files = tool_args.get("files", [])
        if files and isinstance(files, (list, tuple)):
            names = [_to_rel_path(f) if os.path.isabs(f) else f for f in files[:3]]
            names = [os.path.basename(n) if "/" in n or "\\" in n else n for n in names]
            names = [n[:30] for n in names]
            if len(files) > 3:
                desc = "标记 " + ", ".join(names) + f" 等 {len(files)} 个"
            else:
                desc = "标记 " + ", ".join(names)
        else:
            desc = "标记文件"
    elif tool_name == "get_diagnostics":
        path = _to_rel_path(tool_args.get("path", ""))
        language = tool_args.get("language", "")
        desc = f"诊断 {path}" if path else "代码诊断"
        if language:
            desc += f" ({language})"
    # ── 折叠工具的自然预览 ──
    elif tool_name in ("write", "edit", "multi_edit"):
        raw = tool_args.get("path") or tool_args.get("file_path") or ""
        path = _to_rel_path(raw) if raw else ""
        fname = path or "文件"
        if tool_name == "write":
            desc = f'写入 "{fname}"'
        elif tool_name == "edit":
            desc = f'编辑 "{fname}"'
        else:
            edits = tool_args.get("edits", [])
            count = len(edits) if isinstance(edits, list) else 0
            desc = f'批量编辑 "{fname}"' + (f" ({count}处)" if count else "")
    elif tool_name == "todowrite":
        todos = tool_args.get("todos", [])
        count = len(todos) if isinstance(todos, list) else 0
        desc = "更新待办事项" + (f" ({count}项)" if count else "")
    elif tool_name == "skill":
        name = tool_args.get("name", "")
        desc = f'加载技能 "{name}"' if name else "加载技能"
    elif tool_name == "lsp":
        operation = tool_args.get("operation", "")
        raw = tool_args.get("path", "")
        path = _to_rel_path(raw) if raw else ""
        op_labels = {
            "diagnostics": "诊断",
            "documentSymbols": "符号列表",
            "goToDefinition": "跳转定义",
            "findReferences": "查找引用",
            "hover": "悬浮文档",
            "listServers": "服务器列表",
        }
        op_label = op_labels.get(operation, operation or "LSP")
        desc = f"LSP {op_label}"
        if path:
            desc += f' "{path}"'
    elif tool_name == "subagent_para":
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
    elif tool_name == "subagent_status":
        task_ids = tool_args.get("task_ids", [])
        if isinstance(task_ids, list) and task_ids:
            desc = f"查询子智能体状态 ({', '.join(str(t)[:12] for t in task_ids[:3])})"
        else:
            desc = "查询子智能体状态"
    elif tool_name == "subagent_dag":
        nodes = tool_args.get("nodes", [])
        count = len(nodes) if isinstance(nodes, list) else 0
        desc = "DAG 工作流" + (f" ({count}节点)" if count else "")
    elif tool_name == "websearch":
        query = tool_args.get("query", "")
        desc = f'搜索 "{query}"' if query else "网络搜索"
    elif tool_name == "webfetch":
        url = tool_args.get("url", "")
        desc = f"获取网页 {url}" if url else "获取网页"
    elif tool_name == "mouse":
        action = tool_args.get("action", "")
        x = tool_args.get("x", "")
        y = tool_args.get("y", "")
        action_labels = {
            "move": "移动",
            "click": "点击",
            "double_click": "双击",
            "right_click": "右键",
            "scroll": "滚动",
            "drag": "拖拽",
            "position": "查询位置",
        }
        action_label = action_labels.get(action, action or "操作")
        if action == "position":
            desc = "查询鼠标位置"
        elif x != "" and y != "":
            desc = f"鼠标{action_label} ({x}, {y})"
        else:
            desc = f"鼠标{action_label}"
    elif tool_name == "keyboard":
        action = tool_args.get("action", "")
        if action == "type":
            text = tool_args.get("text", "")
            preview = text[:30] + ("…" if len(text) > 30 else "")
            desc = f'键盘输入 "{preview}"' if preview else "键盘输入"
        elif action == "press":
            key = tool_args.get("key", "")
            desc = f"按键 {key}" if key else "按键"
        elif action == "hotkey":
            keys = tool_args.get("keys", "")
            desc = f"热键 {keys}" if keys else "热键"
        else:
            desc = "键盘操作"
    # ── CodeGraph 代码智能 ──
    elif tool_name == "codegraph_explore":
        mode = tool_args.get("mode", "explore")
        query = tool_args.get("query", "")
        mode_labels = {
            "status": "查看索引状态",
            "sync": "同步索引",
            "search": f'搜索 "{query}"' if query else "搜索符号",
            "callers": f'查找 "{query}" 的调用者' if query else "查找调用者",
            "callees": f'查找 "{query}" 调用了什么' if query else "查找被调用者",
            "explore": f'探索 "{query}"' if query else "代码探索",
            "impact": f'分析 "{query}" 的影响范围' if query else "影响分析",
            "files": "列出已索引文件",
        }
        desc = mode_labels.get(mode, f"CodeGraph {mode}")

        # 各 mode 特有参数的修饰语（修复：之前只显示 query/depth，其它参数全部丢失）
        extras = []
        if mode == "files":
            directory = tool_args.get("directory")
            if directory:
                extras.append(f"目录 {directory}")
        elif mode == "search":
            kind = tool_args.get("kind")
            if kind:
                extras.append(f"类型 {kind}")
            limit = tool_args.get("limit")
            if limit is not None and limit != 20:
                extras.append(f"返回 {limit}")
            if tool_args.get("exact"):
                extras.append("精确匹配")
        elif mode == "explore":
            max_files = tool_args.get("max_files")
            if max_files is not None and max_files != 12:
                extras.append(f"文件数 {max_files}")

        if mode in ("search", "callers", "callees", "explore", "impact") and query:
            extras.append(f"深度 {tool_args.get('depth', 2)}")

        if extras:
            desc += " (" + ", ".join(extras) + ")"
    return desc


def _render_tool_status_badge(success: bool) -> str:
    """生成工具执行状态的 HTML 徽章（显示在图标右上角）"""
    if success is None:
        return ""
    bg = "#4CAF50" if success else "#F44336"
    return f'<span class="tool-status-badge" style="position:absolute;top:-2px;right:-2px;width:7px;height:7px;border-radius:50%;background:{bg};z-index:2;box-shadow:0 1px 2px rgba(0,0,0,0.35);"></span>'


def _render_inline_tool(
    tool_name: str,
    tool_args: dict,
    success: bool = None,
    tool_call_id: str = None,
) -> str:
    """渲染紧凑单行卡片（无折叠、无 body、无工具结果内容）

    新设计：SVG 图标 + 状态徽章 + 中文名 + 自然语言参数描述
    """
    icon_name = _get_tool_icon_name(tool_name, tool_args)
    icon_html = _get_tool_icon_html(icon_name)
    badge_html = _render_tool_status_badge(success)
    cn_name = _get_tool_cn_name(tool_name)
    natural_preview = _format_natural_preview(tool_name, tool_args)
    # 去重：自然语言预览开头如与工具名重复则省略
    if natural_preview.startswith(cn_name):
        natural_preview = natural_preview[len(cn_name) :].lstrip()
    tc_id_attr = f' data-tool-call-id="{escape(tool_call_id)}"' if tool_call_id else ""
    return f"""<div class="tool-block" data-tool-name="{escape(tool_name)}"{tc_id_attr} style="margin: 4px 0; background: transparent; border: none; border-radius: 6px; box-shadow: none; display: flex; align-items: center; padding: 5px 10px; {get_font_family_css()}">
        <span style="display: inline-flex; align-items: center; gap: 14px; flex: 0 0 auto;">
            <span style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:0 0 auto;">
                {icon_html}
                {badge_html}
            </span>
            <span style="white-space: nowrap; flex: 0 0 auto; color: #FFA500; font-size: {scale_font_size(13)}px; font-weight: 500;">{escape(cn_name)}</span>
        </span>
        <span style="flex: 1 1 auto; min-width: 0; text-align: left; color: {Colors.TEXT_SECONDARY}; font-size: {scale_font_size(11)}px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-left: 12px;">
            {escape(natural_preview)}
        </span>
    </div>"""


def _unescape_newlines(result: str) -> str:
    """将 \\\\n 字面量还原为真实换行符（逆向 _render_tool_block_content 的转义）"""
    return result.replace("\\n", "\n")


# 文本输出渲染最大长度，防止意外长内容撑爆 DOM
_MAX_OUTPUT_CHARS = 5000


def _render_text_output(result: str, tool_name: str = "", tool_args: dict = None) -> str:
    """将工具结果以格式化 <pre> 文本块渲染（bash/read/grep/webfetch/diagnostics 等）"""
    raw = _unescape_newlines(result)[:_MAX_OUTPUT_CHARS]
    if not raw.strip():
        return ""
    tool_args = tool_args or {}
    _gf = _get_global_font()  # 用户主题全局字体

    # ── bash: 终端风格（命令头 + 输出体） ──
    if tool_name == "bash":
        cmd = tool_args.get("command", "")
        cmd_display = escape(cmd[:120]) if cmd else "(no command)"
        return f"""
        <div class="terminal-block" style="background:rgba(13,17,23,0.40);border:1px solid rgba(48,54,61,0.25);border-radius:8px;overflow:hidden;margin:0;">
            <div style="padding:6px 12px;background:rgba(22,27,34,0.40);border-bottom:1px solid rgba(48,54,61,0.25);color:#8b949e;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(12)}px;">
                $ <span style="color:#c9d1d9;">{cmd_display}</span>
            </div>
            <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;">{escape(raw)}</pre>
        </div>"""

    # ── bg_start / bg_stop: 与 bash 同样的终端风格（命令头 + 状态体） ──
    if tool_name == "bg_start":
        cmd = tool_args.get("command", "")
        header = f"bg_start command={escape(cmd[:120])}" if cmd else "bg_start"
        return f"""
        <div class="terminal-block" style="background:rgba(13,17,23,0.40);border:1px solid rgba(48,54,61,0.25);border-radius:8px;overflow:hidden;margin:0;">
            <div style="padding:6px 12px;background:rgba(22,27,34,0.40);border-bottom:1px solid rgba(48,54,61,0.25);color:#8b949e;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(12)}px;">
                $ <span style="color:#c9d1d9;">{escape(header)}</span>
            </div>
            <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;">{escape(raw)}</pre>
        </div>"""

    if tool_name == "bg_stop":
        task_id = tool_args.get("task_id", "")
        header = f"bg_stop task_id={task_id}" if task_id else "bg_stop"
        return f"""
        <div class="terminal-block" style="background:rgba(13,17,23,0.40);border:1px solid rgba(48,54,61,0.25);border-radius:8px;overflow:hidden;margin:0;">
            <div style="padding:6px 12px;background:rgba(22,27,34,0.40);border-bottom:1px solid rgba(48,54,61,0.25);color:#8b949e;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(12)}px;">
                $ <span style="color:#c9d1d9;">{escape(header)}</span>
            </div>
            <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;">{escape(raw)}</pre>
        </div>"""

    # ── bg_logs: 与 bash 同样的终端风格（任务标识头 + 日志体） ──
    if tool_name == "bg_logs":
        task_id = tool_args.get("task_id", "")
        lines = tool_args.get("lines", 100)
        header = f"bg_logs task_id={task_id} lines={lines}"
        return f"""
        <div class="terminal-block" style="background:rgba(13,17,23,0.40);border:1px solid rgba(48,54,61,0.25);border-radius:8px;overflow:hidden;margin:0;">
            <div style="padding:6px 12px;background:rgba(22,27,34,0.40);border-bottom:1px solid rgba(48,54,61,0.25);color:#8b949e;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(12)}px;">
                $ <span style="color:#c9d1d9;">{escape(header)}</span>
            </div>
            <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;">{escape(raw)}</pre>
        </div>"""

    # ── read: 代码预览（文件路径头 + 内容体） ──
    if tool_name in ("read", "todoread"):
        path_hint = tool_args.get("path") or tool_args.get("file_path") or ""
        path_display = escape(path_hint[:100]) if path_hint else "file"
        return f"""
        <div style="background:rgba(22,27,34,0.40);border:1px solid rgba(48,54,61,0.25);border-radius:8px;overflow:hidden;margin:0;">
            <div style="padding:6px 12px;background:rgba(28,33,40,0.40);border-bottom:1px solid rgba(48,54,61,0.25);color:#8b949e;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(12)}px;">
                📄 <span style="color:#c9d1d9;">{path_display}</span>
            </div>
            <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;">{escape(raw)}</pre>
        </div>"""

    # ── diagnostics: 按严重级别着色 ──
    if tool_name == "get_diagnostics":
        lines_html = []
        for line in raw.split("\n"):
            lower = line.lower()
            if "error" in lower or "[error" in lower:
                lines_html.append(f'<span style="color:#f85149;">{escape(line)}</span>')
            elif "warning" in lower or "[warning" in lower:
                lines_html.append(f'<span style="color:#d2991d;">{escape(line)}</span>')
            elif "success" in lower or " issue" in lower or "issues" in lower:
                lines_html.append(f'<span style="color:#7ee787;">{escape(line)}</span>')
            else:
                lines_html.append(escape(line))
        return f"""
        <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.55;white-space:pre-wrap;word-break:break-all;overflow-x:auto;border:1px solid rgba(48,54,61,0.25);border-radius:8px;">{"\n".join(lines_html)}</pre>"""

    # ── codegraph_explore: 结构化代码探索结果 ──
    if tool_name == "codegraph_explore":
        lines = raw.split("\n")
        html_lines = []
        for line in lines:
            escaped = escape(line)
            # 标题行: ### xxx
            if line.startswith("### "):
                html_lines.append(
                    f'<div style="color:#58a6ff;font-weight:700;font-size:{scale_font_size(14)}px;'
                    f'padding:8px 0 4px 0;">{escaped}</div>'
                )
            # 粗体文件路径: **xxx**
            elif "**" in line:
                # 简单替换 **xxx** 为带颜色的粗体
                parts = []
                in_bold = False
                buf = ""
                for ch in line:
                    if ch == "*":
                        continue
                    # Actually much simpler: just color lines starting with 📄
                # Simpler approach: check for emoji patterns
                if line.strip().startswith("📄"):
                    html_lines.append(f'<div style="color:#7ee787;font-weight:600;padding:2px 0;">{escaped}</div>')
                elif line.strip().startswith(("⬆", "⬇", "←", "→", "💥")):
                    html_lines.append(f'<div style="color:#d2a8ff;padding:1px 0 1px 12px;">{escaped}</div>')
                elif line.strip().startswith(("[", "- [")):
                    html_lines.append(f'<div style="color:#c9d1d9;padding:1px 0 1px 12px;">{escaped}</div>')
                else:
                    html_lines.append(f'<div style="padding:1px 0;">{escaped}</div>')
            elif line.strip() == "---":
                html_lines.append('<div style="border-top:1px solid rgba(48,54,61,0.25);margin:6px 0;"></div>')
            else:
                html_lines.append(f'<div style="padding:1px 0;">{escaped}</div>')

        content = "".join(html_lines)
        return f"""
        <div style="background:rgba(13,17,23,0.40);border:1px solid rgba(48,54,61,0.25);border-radius:8px;overflow:hidden;margin:0;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.55;padding:8px 12px;">
            {content}
        </div>"""

    # ── grep / glob / list / scan: 匹配/列表示结果 ──
    if tool_name in ("grep", "glob", "list", "scan_repo", "stage_files"):
        return f"""
        <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;border:1px solid rgba(48,54,61,0.25);border-radius:8px;">{escape(raw)}</pre>"""

    # ── 通用文本输出 (webfetch, websearch, mouse, keyboard 等) ──
    return f"""
    <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;border:1px solid rgba(48,54,61,0.25);border-radius:8px;">{escape(raw)}</pre>"""


def _normalize_question_item(q) -> dict:
    """将 question 条目规范化为 dict 格式

    兼容 LLM 传参的各种格式：
    - dict: {"question": "...", "options": [...], "multiple": ...}
    - str:  "问题文本"
    - 其他: 转为字符串
    """
    if isinstance(q, dict):
        return q
    elif isinstance(q, str):
        return {"question": q, "options": [], "multiple": False}
    else:
        return {"question": str(q), "options": [], "multiple": False}


def _normalize_option_item(opt) -> dict:
    """将选项条目规范化为 dict 格式

    兼容 LLM 传参的各种格式：
    - dict: {"label": "...", "description": "..."}
    - str:  "选项文本"
    - 其他: 转为字符串
    """
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
    """解析 questions 字段，兼容 list / str / None

    当 message_content 序列化截断后，questions 可能变成一个 JSON 字符串
    而非 list，此处统一还原为规范化 list[dict]。
    """
    if not questions_raw:
        return []

    if isinstance(questions_raw, list):
        return [_normalize_question_item(q) for q in questions_raw]

    if isinstance(questions_raw, str):
        # 尝试 JSON 解析（完整或截断的 JSON 字符串）
        try:
            parsed = json.loads(questions_raw)
            if isinstance(parsed, list):
                return [_normalize_question_item(q) for q in parsed]
            elif isinstance(parsed, dict):
                return [_normalize_question_item(parsed)]
        except json.JSONDecodeError, ValueError:
            pass
        # JSON 解析失败（可能被截断），作为单个问题展示原始文本
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


def _render_question_block(tool_args: dict, result: str = None) -> str:
    """将 question 工具渲染为 bash 风格的终端块

    渲染规则：
    - 终端头: ❓ question: 第一个问题文本（预览）
    - 终端体: 逐个展示问题 → 选项列表 → 用户回答
    - 选项标记: 选中 ● / 未选中 ○（多选时用 ◉ / ○）
    - 自定义回答: 单独以 ✎ 自定义: xxx 显示

    兼容多种参数格式（list / str / 旧格式单 question 字段）。
    """
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
    answer_map = _parse_question_result(result) if result else {}

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
            if result:
                lines.append(f"  ✎ 回答: {_unescape_newlines(result)}")
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


def render_tool_block(
    tool_name: str,
    tool_args: dict,
    result: str = None,
    success: bool = None,
    collapsed: bool = False,
    tool_call_id: str = None,
    diff: str = None,
    echarts: str = None,
) -> str:
    """渲染工具块，参数横向表格展示（左列参数名，右列结果值）"""

    _gf = _get_global_font()  # 用户主题全局字体

    # 检测是否为 MCP 工具（mcp__ 前缀或 mcp_list_servers）
    is_mcp_tool = tool_name.startswith("mcp__") or tool_name == "mcp_list_servers"

    # 检测是否为子智能体任务（特殊渲染逻辑）
    is_sub_agent_task = tool_name in ("subagent_para", "subagent_dag")

    # 图标与颜色按类型区分
    if is_mcp_tool:
        icon_name = "websearch"
        title_color = "#00BCD4"
    elif is_sub_agent_task:
        icon_name = "设置-subagent"
        title_color = "#9C27B0"
    else:
        icon_name = _get_tool_icon_name(tool_name, tool_args)
        title_color = "#FFA500"

    # 状态徽章（图标右上角）
    badge_html = _render_tool_status_badge(success)
    icon_html = _get_tool_icon_html(icon_name)
    cn_name = _get_tool_cn_name(tool_name)

    # 子智能体任务特殊处理
    if is_sub_agent_task:
        agent_name = tool_args.get("agent", "unknown")
        task_desc = tool_args.get("description", "")[:50]
        if tool_args.get("description"):
            task_desc = tool_args["description"][:50] + ("..." if len(tool_args["description"]) > 50 else "")

    # 参数展示型工具 → 紧凑单行卡片（无折叠、无 body）
    if tool_name in _INLINE_TOOLS:
        return _render_inline_tool(
            tool_name=tool_name,
            tool_args=tool_args,
            success=success,
            tool_call_id=tool_call_id,
        )

    # 文件编辑工具判断
    diff_summary = _summarize_diff(diff or "") if diff else {"added": 0, "deleted": 0, "files": []}

    # 差异统计（+N/-N）— 纯展示，无差异对比按钮
    diff_stats_html = ""
    if diff:
        added = diff_summary["added"]
        deleted = diff_summary["deleted"]
        if added or deleted:
            # sep 不再写内联颜色——交给 message_card.py 的 .tool-diff-stats__sep
            # CSS 类按主题自适应（浅色用 --text-muted，深色用 #6e7681）。
            # 之前写死 rgba(255,255,255,0.3) 在浅色背景下几乎不可见。
            diff_stats_html = f"""
            <span class="tool-diff-stats" style="font-size: {scale_font_size(11)}px; {get_font_family_css()}">
                <span class="tool-diff-stats__add" style="color: #39d353; font-weight: 600;">+{added}</span>
                <span class="tool-diff-stats__sep">/</span>
                <span class="tool-diff-stats__del" style="color: #f85149; font-weight: 600;">-{deleted}</span>
            </span>"""

    # 子智能体日志查看按钮
    subagent_log_btn_html = ""
    if is_sub_agent_task:
        # 解析 task_ids
        task_ids_str = _parse_subagent_task_ids(result)
        if task_ids_str:
            subagent_log_btn_html = f'''
        <span class="tool-subagent-log-btn" data-task-ids="{escape(task_ids_str)}"
            role="button" tabindex="0"
            style="display: inline-flex; align-items: center; justify-content: center; flex: 0 0 auto; background: transparent; cursor: pointer; padding: 4px; margin-left: 8px; border-radius: 4px; position: relative;"
            onclick="event.stopPropagation(); window._requestSubAgentLog(this.dataset.taskIds)"
            onkeydown="if(event.key === 'Enter' || event.key === ' '){{ event.preventDefault(); event.stopPropagation(); window._requestSubAgentLog(this.dataset.taskIds); }}"
            data-tooltip="查看子智能体执行日志">
            <img src="qrc:/icons/日志.svg" style="width: 16px; height: 16px;" />
        </span>'''

    # 生成参数预览（折叠时显示）
    # question 特殊处理：预览显示第一个问题文本
    if tool_name == "question":
        questions_raw = tool_args.get("questions", [])
        if not questions_raw and "question" in tool_args:
            questions_raw = [{"question": str(tool_args["question"])}]
        normalized = _parse_questions_field(questions_raw)
        q_text = normalized[0].get("question", "") if normalized else ""
        args_preview = q_text[:120] + ("…" if len(q_text) > 120 else "") if q_text else "(无问题)"
    else:
        # 优先使用自然语言预览，无匹配时 fallback 到 key=value 格式
        natural = _format_natural_preview(tool_name, tool_args)
        args_preview = natural if natural else _format_args_preview(tool_args)

    # 去重：自然语言预览开头如与工具名重复则省略
    if args_preview.startswith(cn_name):
        args_preview = args_preview[len(cn_name) :].lstrip()

    # ── grep/glob 结果计数 ──
    match_count_html = ""
    if success and result and tool_name in ("grep", "glob"):
        import re as _re

        if tool_name == "grep":
            # 从 meta 行解析 "# Search: ... | X matches ..."
            m = _re.search(r"\|\s*(\d+)\s+matches", result)
            if m:
                count = m.group(1)
                match_count_html = f'<span style="color: #39d353; font-weight: 600; font-size: {scale_font_size(11)}px; margin-left: 6px;">{count}项</span>'
        elif tool_name == "glob":
            # 先检查是否有 meta 头
            m = _re.search(r"\|\s*(\d+)\s+matches", result)
            if m:
                count = m.group(1)
            else:
                # 无 meta 头：每行一个文件
                count = str(len([ln for ln in result.split("\n") if ln.strip()]))
            match_count_html = f'<span style="color: #39d353; font-weight: 600; font-size: {scale_font_size(11)}px; margin-left: 6px;">{count}项</span>'

    # ── inline diff 预览区 ──
    diff_html = ""
    diff_line_count = 0
    if diff:
        diff_body = _render_diff_preview(diff)
        # 统计 diff 的行数（用于判断折叠阈值）
        diff_line_count = diff_summary["added"] + diff_summary["deleted"]
        diff_files = diff_summary["files"]
        file_label = diff_files[0] if diff_files else "文件变更"
        file_label = os.path.basename(file_label)
        if len(diff_files) > 1:
            file_label = f"{file_label} 等 {len(diff_files)} 个文件"
        added = diff_summary["added"]
        deleted = diff_summary["deleted"]
        diff_html = f"""
        <div class="tool-diff-inline">
            <div class="tool-diff-inline__header" style="{get_font_family_css()}">
                <span class="tool-diff-inline__file" title="{escape(file_label)}">{escape(file_label)}</span>
                <span class="tool-diff-inline__summary">
                    <span class="tool-diff-inline__add" style="color: #56d364;">+{added}</span>
                    <span class="tool-diff-inline__del" style="color: #ff7b72;">-{deleted}</span>
                </span>
            </div>
            <div class="tool-diff-inline__body" style="font-family: '{_gf}', Consolas, 'Courier New', monospace; font-size: {scale_font_size(12)}px;">
                {diff_body}
            </div>
        </div>"""

    # ── ECharts 图表区 ──
    echarts_html = ""
    if echarts:
        try:
            import base64 as _b64

            b64_json = _b64.b64encode(echarts.encode("utf-8")).decode("ascii")
            chart_id = "echart-tool-" + hashlib.sha1(echarts.encode("utf-8")).hexdigest()[:12]
            echarts_html = f'''
            <div id="{chart_id}" class="echarts-container" data-echarts-json="{b64_json}" style="width: 100%; height: 400px; margin: 12px 0; border-radius: 10px; overflow: hidden;"></div>'''
        except Exception:
            pass

    # ── 截图工具：提取图片路径，直接显示截图 ──
    screenshot_image_html = ""
    if tool_name == "screenshot" and success is not False:
        img_path = _extract_screenshot_image_path(result)
        if img_path:
            screenshot_image_html = f'''
            <div class="screenshot-preview" style="margin: 0; padding: 0;">
                <img src="{escape(img_path)}" style="width: 100%; height: auto; display: block; border-radius: 8px;" alt="Screenshot" />
            </div>'''

    # ── 通用文本输出工具：bash/read/grep/webfetch/websearch/diagnostics 等 ──
    _RAW_OUTPUT_TOOLS = frozenset(
        {
            "bash",
            "bg_start",
            "bg_logs",
            "bg_stop",
            "read",
            "todoread",
            "grep",
            "glob",
            "list",
            "scan_repo",
            "stage_files",
            "webfetch",
            "websearch",
            "get_diagnostics",
            "mouse",
            "keyboard",
            "codegraph_explore",
        }
    )
    raw_output_html = ""
    if tool_name in _RAW_OUTPUT_TOOLS and success is not False:
        raw_output_html = _render_text_output(result, tool_name, tool_args)

    # 有 echarts / 截图 / 文本输出 / diff 时：跳过参数表格，直接显示内容
    # 注意：调用方传入的 collapsed 代表模式偏好（简洁模式=True，非简洁=False）。
    # 内容类型逻辑仅在非简洁模式下生效；简洁模式下保持 collapsed=True 全部折叠。
    DIFF_AUTO_COLLAPSE_LINES = 10
    if echarts:
        if not collapsed:  # 非简洁模式下图表默认展开
            collapsed = False
        expanded_content = f"""
        <div class="tool-expanded-content">
            {echarts_html}
            {diff_html}
        </div>"""
    elif screenshot_image_html:
        if not collapsed:  # 非简洁模式下截图默认展开
            collapsed = False
        expanded_content = f"""
        <div class="tool-expanded-content">
            {screenshot_image_html}
        </div>"""
    elif raw_output_html:
        # 文本输出始终折叠（内容通常很长），简洁/非简洁均保持
        collapsed = True
        expanded_content = f"""
        <div class="tool-expanded-content">
            {raw_output_html}
        </div>"""
    elif diff and diff_line_count > 0:
        if not collapsed:  # 非简洁模式下按行数自动判断
            collapsed = diff_line_count > DIFF_AUTO_COLLAPSE_LINES
        expanded_content = f"""
        <div class="tool-expanded-content">
            {echarts_html}
            {diff_html}
        </div>"""
    elif tool_name == "question" and success is not False:
        collapsed = True
        question_html = _render_question_block(tool_args, result)
        expanded_content = f"""
        <div class="tool-expanded-content">
            {question_html}
        </div>"""
    else:
        # 无特殊渲染时：显示参数表格
        # collapsed 保持调用方传入值（简洁模式=True折叠，非简洁=False展开）
        unified_table_html = _format_unified_table(tool_args, result, is_sub_agent_task, success)
        expanded_content = f"""
        <div class="tool-expanded-content">
            {echarts_html}
            {unified_table_html}
            {diff_html}
        </div>"""

    # 生成哈希 key
    block_seed = "|".join(
        [
            str(tool_name or ""),
            json.dumps(tool_args or {}, option=json.OPT_SORT_KEYS).decode("utf-8"),
            str(result or ""),
            str(success),
        ]
    )
    block_key = "tool-" + hashlib.sha1(block_seed.encode("utf-8")).hexdigest()[:12]
    expanded_attr = "false" if collapsed else "true"
    body_style = "" if collapsed else ' style="height:auto; opacity:1;"'

    return f"""<div class="cm-collapsible tool-block" data-block-key="{block_key}" data-expanded="{expanded_attr}" data-tool-name="{escape(tool_name)}" data-tool-call-id="{escape(tool_call_id or "")}" style="margin: 4px 0; background: transparent; border-radius: 6px;">
    <button type="button" class="cm-collapsible__summary tool-block__summary" aria-expanded="{expanded_attr}" style="cursor: pointer; padding: 4px 8px; color: {title_color}; font-size: {scale_font_size(13)}px; font-weight: 500; display: flex; align-items: center; gap: 6px; width: 100%; background: transparent; border: none; text-align: left; box-sizing: border-box; {get_font_family_css()}">
        <span style="display: inline-flex; align-items: center; gap: 14px; flex: 0 0 auto;">
            <span style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:0 0 auto;">
                {icon_html}
                {badge_html}
            </span>
            <span style="white-space: nowrap; flex: 0 0 auto; {get_font_family_css()}">{escape(cn_name)}</span>
        </span>
        <span style="display: flex; align-items: center; gap: 8px; margin-left: 10px; min-width: 0; flex: 1 1 auto; justify-content: flex-start; overflow: hidden;">
            <span style="color: {Colors.TEXT_SECONDARY}; font-size: {scale_font_size(11)}px; text-align: left; word-break: break-all; white-space: normal; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;">
                {escape(args_preview)}
            </span>
            {match_count_html}
            {diff_stats_html}
            {subagent_log_btn_html}
        </span>
        <span class="cm-collapsible__chevron" aria-hidden="true" style="flex: 0 0 auto; margin-left: auto;"></span>
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
