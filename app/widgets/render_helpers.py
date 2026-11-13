# -*- coding: utf-8 -*-
"""
UI 渲染辅助函数
"""

import hashlib
import os
import re
from html import escape

import orjson as json
from loguru import logger

from app.tools.registry import DEFAULT_FALLBACK_ICON
from app.utils.design_tokens import Colors, _get_global_font, scale_font_size
from app.utils.utils import get_font_family_css

# 行内 diff 高亮子系统已抽取至 app/utils/diff_highlight（消除 utils→widgets 反向依赖）。
# 此处 re-export 供内部渲染管线（format_tool_block 等）与存量引用使用，tests 无需改动。
# invalidate_render_caches 所清的 diff formatter/lexer 缓存现居新模块，一并 re-export。
from app.utils.diff_highlight import (
    _DIFF_FORMATTER_CACHE,
    _DIFF_LEXER_CACHE,
    _TEXT_LEXER,
    _get_diff_lexer,
    _highlight_code_line,
    _highlighted_word_diff_html,
    _sync_diff_style_to_theme,
    set_diff_highlight_style,
)


# ===== 主题感知的 qrc 图标前缀（单一来源，替代 render_helpers / message_card 散落的硬编码） =====
# 主题判断失败默认走深色，与 history 行为一致；调用方必须接受 fallback。
def get_tool_qrc_prefix() -> str:
    """获取当前主题对应的工具图标 qrc 前缀（qrc:/icons_light 或 qrc:/icons）"""
    try:
        from app.utils.theme_manager import theme_manager

        return "qrc:/icons_light" if theme_manager.is_light_theme() else "qrc:/icons"
    except Exception:
        return "qrc:/icons"

# ===== 主程序 qrc 资源存在性缓存（避免每条消息都查 QFile） =====
# icon 在插件目录里查不到时，渲染层会用 DEFAULT_FALLBACK_ICON ("工具") 兜底——
# 该常量已知存在于 icons.qrc + icons_light.qrc，避免破图。
_QRC_ICON_EXISTS_CACHE: dict = {}
_QRC_ICON_CACHE_MAX = 256


def _qrc_icon_exists(prefix: str, icon_name: str) -> bool:
    """检查主程序 qrc 资源是否存在指定 icon（带缓存，避免每次都 QFile.exists）

    prefix 形如 "qrc:/icons"，QFile 接受 ":icons/..."（去 qrc 前缀加冒号）。
    主题管理器/Qt 未初始化时按"找不到"处理，让上层走 DEFAULT_FALLBACK_ICON 兜底。
    """
    key = f"{prefix}/{icon_name}.svg"
    cached = _QRC_ICON_EXISTS_CACHE.get(key)
    if cached is not None:
        return cached
    exists = False
    try:
        from PyQt5.QtCore import QFile
        qrc_path = ":" + prefix[len("qrc:"):] if prefix.startswith("qrc:") else prefix
        exists = QFile.exists(f"{qrc_path}/{icon_name}.svg")
    except Exception:
        exists = False
    if len(_QRC_ICON_EXISTS_CACHE) >= _QRC_ICON_CACHE_MAX:
        _QRC_ICON_EXISTS_CACHE.clear()
    _QRC_ICON_EXISTS_CACHE[key] = exists
    return exists


# 预编译正则表达式（模块级别缓存，避免重复编译）
_CODE_BLOCK_PATTERN = re.compile(r"```[\w]*\n")
_CODE_BLOCK_FINAL_PATTERN = re.compile(r"```")
# 匹配 HTML 代码块标签
_HTML_CODE_BLOCK_PATTERN = re.compile(r"<(pre|code)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
# HTML 标签清理正则（避免每次调用 re.sub）
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
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


def _shorten_path(path: str, max_len: int = 56) -> str:
    """折叠头右侧的路径缩短：保留开头目录与末尾文件名，中间省略。
    绝对路径动辄上百字符，原样塞进单行会被 CSS 从尾部裁掉——文件名正好是
    最该看见的部分，所以在 Python 侧先做首尾截断。
    """
    if not path or len(path) <= max_len:
        return path
    half = (max_len - 1) // 2
    return f"{path[:half]}…{path[-half:]}"


# ===== 工具卡路径可点（点击 → 宿主用系统程序打开该文件/目录） =====
# 只覆盖工具完成框上显示的路径：header 右侧地址、inline 卡（read/grep/list/glob）
# 自然语言预览里的路径片段。显示文本可能被 _shorten_path 截断，data-open-path
# 始终携带原始完整路径；相对路径由宿主按会话 workdir 解析。
def _tool_path_arg(tool_args) -> str:
    """取工具参数里的主路径（单目标工具：read/write/edit/grep/list/glob 等）"""
    if not isinstance(tool_args, dict):
        return ""
    for key in ("file_path", "path"):
        value = tool_args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""

def _render_path_link(display: str, path: str) -> str:
    """可点路径元素：显示 display（可为截断文本），携带完整原始路径"""
    return (
        f'<span class="tool-path-link" data-open-path="{escape(path)}" '
        f'title="{escape(path)}">{escape(display)}</span>'
    )

def _linkify_path(text: str, path: str) -> str:
    """把纯文本里的路径片段替换成可点元素，属性始终携带完整原始 ``path``

    插件 preview 会用 to_rel_path 把绝对路径相对化后再显示（如
    ``D:/work/DriFox/app/main.py`` 显示成 ``app/main.py``），且 Windows 下
    分隔符可能混用。此时精确子串匹配必然失配，完成框里的路径就变纯文本、
    点击无反应。回退策略：取原始路径的**尾部若干段**（含末段目录名），
    归一化分隔符后在文本里找最长命中，命中片段可点但仍指向完整原路径。
    未命中返回空串（调用方自行 escape 整段文本），不猜、不编造路径。
    """
    if not text or not path:
        return ""

    def _norm(s: str) -> str:
        return s.replace("\\", "/")

    idx = text.find(path)
    if idx >= 0:
        return escape(text[:idx]) + _render_path_link(path, path) + escape(text[idx + len(path) :])

    # 精确未命中：按「原始路径的尾部片段」在归一化文本里找最长匹配。
    # 从最长后缀逐段缩短（含末段）；目录型工具（grep/list/glob）的 path 相对化后
    # 只剩 "app" 这类单段，也必须可点。长尾优先命中，单段仅在前缀都不在文本里
    # 时才发生，此时文本里本就只有这一段，不会把更长路径截半。
    parts = [p for p in _norm(path).split("/") if p]
    if not parts:
        return ""
    norm_text = _norm(text)
    for start in range(0, len(parts)):  # start 越小 tail 越长：最长后缀优先
        tail = "/".join(parts[start:])
        hit = norm_text.find(tail)
        if hit >= 0:
            # 命中区间按归一化坐标定位；text 中的反斜杠与归一串等长（1:1 替换）
            return (
                escape(text[:hit])
                + _render_path_link(text[hit : hit + len(tail)], path)
                + escape(text[hit + len(tail) :])
            )
    return ""

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

    状态色一律走主题变量（--success / --danger / --text-muted）：早先写死
    #F44336 / #5FD18C / #666，跟主题脱节。结果超长时给「展开全部」而不是直接砍断
    （用原生 <details>，展开时折叠体已 height:auto，不会被裁掉）。
    """
    max_result_len = 500
    rows = []

    # 根据成功/失败状态确定颜色
    if success is False:
        row_class = "args-row result-row result-fail"
        key_color = "var(--danger)"
    elif success is True:
        row_class = "args-row result-row result-success"
        key_color = "var(--success)"
    else:
        row_class = "args-row result-row"
        key_color = "var(--text-muted)"

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

    # 结果行（最后一行）：结果通常是一大段文本，超长时给「展开全部」
    result_label = "调用子智能体" if is_sub_agent_task else "结果"
    if result:
        full_text = _escape_text_for_plain(str(result))
        if len(full_text) > max_result_len:
            result_inner = (
                f"{escape(full_text[:max_result_len])}…"
                f'<details style="display:block;margin-top:6px;">'
                f'<summary style="cursor:pointer;color:var(--accent);'
                f'font-size:{scale_font_size(11)}px;">展开全部（共 {len(full_text)} 字符）</summary>'
                f'<div style="margin-top:6px;max-height:420px;overflow-y:auto;">{escape(full_text)}</div>'
                f"</details>"
            )
        else:
            result_inner = escape(full_text)
        rows.append(
            f'<div class="{row_class}">'
            f'<span class="args-key" style="color: {key_color};">{result_label}</span>'
            f'<span class="args-value">{result_inner}</span>'
            f"</div>"
        )
    else:
        rows.append(
            f'<div class="{row_class}">'
            f'<span class="args-key" style="color: {key_color};">{result_label}</span>'
            f'<span class="args-value" style="color: var(--text-muted); font-style: italic;">无结果</span>'
            f"</div>"
        )

    return f'<div class="args-table">{"".join(rows)}</div>'


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

    渲染入口先同步主题（见 _sync_diff_style_to_theme 注释），避免主题切换
    后 formatter 仍是旧主题的前景色导致浅色背景下白字白底不可见。
    - 相邻的增/删差异（包括被 hunk 头隔开的紧邻小改动）聚合成一个
      「差异段」，差异段同时输出两套视图：
      · 单列视图（.diff-seg-col，默认）：所有删除行先、所有新增行后，
        配对行做词级差异高亮（与双列共用同一份 _highlighted_word_diff_html
        输出，未配对行降级为整行高亮
      · 双列视图（.diff-seg-paired，split-view）：左右对照的配对行 + 词级差异高亮
    超过 500 行时截断并显示行数。
    """
    # 渲染前按当前主题同步 diff 风格，避免工具渲染管线漏同步导致浅色背景白字
    _sync_diff_style_to_theme()
    lines = diff_text.split("\n")[1:]
    # 去掉 split 产生的尾随空行（diff 文本通常以一个换行结尾），避免渲染出多余空行
    while lines and lines[-1] == "":
        lines.pop()
    MAX_LINES = 500
    if len(lines) > MAX_LINES:
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

            # 配对行的词级高亮 HTML 只计算一次，单列/双列两视图共用同一份
            # 输出（_highlighted_word_diff_html 含 SequenceMatcher + Pygments 着色，
            # 重复计算代价高；共用也保证两视图字节级一致）。
            paired_htmls = []
            for k in range(pair):
                od, oa = dels[k], adds[k]
                old_html, new_html = _highlighted_word_diff_html(od["text"], oa["text"], od["lexer"])
                paired_htmls.append((od, old_html, oa, new_html))

            # ── 单列视图：所有删除行先、所有新增行后（带词级高亮） ──
            # 再分两段输出：先 del 全打，再 add 全打——避免 del/add 交替时
            # 既要保持 "del→add" 配对又得来回切上下文。
            rows.append('<div class="diff-seg-col">')

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
                od, old_html, oa, new_html = paired_htmls[k]
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


def _get_tool_icon_name(tool_name: str, tool_args: dict = None) -> str:
    """根据工具名（必要时结合 tool_args）查找图标文件名（不含扩展名）

    数据源：ToolRegistry（工具插件注册时声明的 icon）。
    插件可通过 metadata["operation_icons"] 声明按参数切换的图标（如 lsp 按 operation）。
    """
    try:
        from app.tools.registry import ToolRegistry

        reg = ToolRegistry.get_instance().get(tool_name)
        if reg is not None and tool_args:
            op_icons = reg.metadata.get("operation_icons") if reg.metadata else None
            # 类型守门：坏元数据（非 dict）回退静态图标，不抛异常
            if isinstance(op_icons, dict):
                operation = tool_args.get("operation", "")
                if operation in op_icons:
                    return op_icons[operation]
        return ToolRegistry.get_instance().get_icon(tool_name)
    except Exception:
        return DEFAULT_FALLBACK_ICON


def _get_tool_cn_name(tool_name: str) -> str:
    """获取工具的中文显示名（registry 驱动）"""
    # MCP 工具：返回服务名（动态发现，不进 registry）
    if tool_name.startswith("mcp__"):
        return "__".join(tool_name.split("__")[2:]) if len(tool_name.split("__")) > 2 else "MCP"
    try:
        from app.tools.registry import ToolRegistry

        cn = ToolRegistry.get_instance().get_cn_name(tool_name)
        if cn:
            return cn
    except Exception:
        pass
    return tool_name


def _get_tool_icon_html(icon_name: str, size: int = 18, tool_name: str = None) -> str:
    """生成工具图标的 HTML <img> 标签（主题感知）

    图标来源优先级：
    1. 工具插件自带图标（<插件>/tools/icons/ + icons_light/，icon 自包含）
       - 浅色主题 → icons_light/；深色主题 → icons/
       - 缺浅色版 → 回退深色版 → 回退主程序 qrc
    2. 主程序 qrc 资源（qrc:/icons 或 qrc:/icons_light，主题感知）
    """
    if tool_name:
        try:
            from pathlib import Path

            from app.tools.registry import ToolRegistry

            reg = ToolRegistry.get_instance().get(tool_name)
            if reg is not None:
                # 主题感知：浅色优先 icons_light，深色用 icons
                is_light = False
                try:
                    from app.utils.theme_manager import theme_manager

                    is_light = theme_manager.is_light_theme()
                except Exception:
                    pass
                icon_dirs = [reg.icon_dir_light if is_light else reg.icon_dir]
                if is_light and not icon_dirs[0]:
                    icon_dirs.append(reg.icon_dir)  # 浅色缺失 → 回退深色
                for d in icon_dirs:
                    if not d:
                        continue
                    svg_path = Path(d) / f"{icon_name}.svg"
                    if svg_path.exists():
                        import base64

                        svg_bytes = svg_path.read_bytes()
                        b64 = base64.b64encode(svg_bytes).decode("ascii")
                        return (
                            f'<img src="data:image/svg+xml;base64,{b64}" '
                            f'style="width:{size}px;height:{size}px;pointer-events:none;" />'
                        )
                    logger.debug(
                        f"[icon] 插件目录 {d} 找不到 {icon_name}.svg（tool={tool_name}），跳过"
                    )
        except Exception as e:
            logger.debug(
                f"[icon] tool={tool_name} icon={icon_name} 插件目录查询异常：{e}"
            )
    # 2) 主程序 qrc 资源（主题感知；单一来源 get_tool_qrc_prefix）
    prefix = get_tool_qrc_prefix()
    # 插件目录与主程序 qrc 都查不到时（插件声明了一个 qrc 未收录的文件名），
    # 强制回退到 DEFAULT_FALLBACK_ICON，避免 WebEngineView 显示破图。
    if not _qrc_icon_exists(prefix, icon_name):
        logger.debug(
            f"[icon] qrc {prefix}/{icon_name}.svg 不存在，回退到 {DEFAULT_FALLBACK_ICON}"
        )
        icon_name = DEFAULT_FALLBACK_ICON
    return f'<img src="{prefix}/{icon_name}.svg" style="width:{size}px;height:{size}px;pointer-events:none;" />'
def _get_tool_icon(tool_name: str, tool_args: dict = None) -> str:
    """[已弃用] 根据工具名查找图标（保留兼容，返回图标文件名）

    新代码请使用 _get_tool_icon_name + _get_tool_icon_html 组合。
    """
    return _get_tool_icon_name(tool_name, tool_args)




# 参数展示型工具（render_mode="inline"）→ 紧凑单行卡片（无折叠、无 body、无工具结果）
# 注意：不再有工具名白名单 —— 是否 inline 完全由插件注册的 render_mode 决定。


def _format_natural_preview(tool_name: str, tool_args: dict) -> str:
    """将工具调用转为自然语言描述（用于内联卡片和折叠头的预览）

    渲染完全由工具插件决定：优先调用插件注册的 preview 闭包
    （preview(tool_args) -> str），未注册时返回空串（渲染层 fallback key=value）。
    """
    try:
        from app.tools.registry import ToolRegistry

        preview_fn = ToolRegistry.get_instance().get_preview(tool_name)
        if preview_fn is not None:
            return preview_fn(tool_args or {}) or ""
    except Exception:
        pass
    return ""


def _render_tool_status_badge(success: bool) -> str:
    """生成工具执行状态的 HTML 徽章（显示在图标右上角）"""
    if success is None:
        return ""
    bg = "#4CAF50" if success else "#F44336"
    return f'<span class="tool-status-badge" style="position:absolute;top:-2px;right:-2px;width:7px;height:7px;border-radius:50%;background:{bg};z-index:2;box-shadow:0 1px 2px rgba(0,0,0,0.35);"></span>'


def _tool_success_attr(success) -> str:
    """失败标记：JS「工具与思考」头部按此统计「失败操作」数。

    只在 success is False 时输出；None（运行中/未知）与 True 都不输出，
    避免运行中块被算成失败。
    """
    return ' data-tool-success="false"' if success is False else ""

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
    icon_html = _get_tool_icon_html(icon_name, tool_name=tool_name)
    badge_html = _render_tool_status_badge(success)
    cn_name = _get_tool_cn_name(tool_name)
    natural_preview = _format_natural_preview(tool_name, tool_args)
    # 去重：自然语言预览开头如与工具名重复则省略
    if natural_preview.startswith(cn_name):
        natural_preview = natural_preview[len(cn_name) :].lstrip()
    # 预览里的路径片段可点（read/grep/list/glob 这类单行卡只有 preview 承载路径）
    preview_html = _linkify_path(natural_preview, _tool_path_arg(tool_args)) or escape(natural_preview)
    tc_id_attr = f' data-tool-call-id="{escape(tool_call_id)}"' if tool_call_id else ""
    # 编辑/子智能体/提问类工具标记 data-keep-in-content：JS 正文分区据此保留在正文（registry 派生）
    _keep_attr = ' data-keep-in-content="true"' if tool_name in _keep_in_content_tools() else ""
    _succ_attr = _tool_success_attr(success)
    return f"""<div class="tool-block" data-tool-name="{escape(tool_name)}"{tc_id_attr}{_keep_attr}{_succ_attr} style="margin: 4px 0; background: transparent; border: none; border-radius: 6px; box-shadow: none; display: flex; align-items: center; padding: 5px 10px; {get_font_family_css()}">
        <span style="display: inline-flex; align-items: center; gap: 14px; flex: 0 0 auto;">
            <span style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:0 0 auto;">
                {icon_html}
                {badge_html}
            </span>
            <span style="white-space: nowrap; flex: 0 0 auto; color: {Colors.TOOL_TITLE_MCP if tool_name.startswith('mcp__') else Colors.TOOL_TITLE}; font-size: {scale_font_size(13)}px; font-weight: 500;">{escape(cn_name)}</span>
        </span>
        <span style="flex: 1 1 auto; min-width: 0; text-align: left; color: var(--text-secondary); font-size: {scale_font_size(11)}px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-left: 12px;">
            {preview_html}
        </span>
    </div>"""


def _unescape_newlines(result: str) -> str:
    """将 \\\\n 字面量还原为真实换行符（逆向 _render_tool_block_content 的转义）"""
    return result.replace("\\n", "\n")


# 文本输出渲染最大长度，防止意外长内容撑爆 DOM
_MAX_OUTPUT_CHARS = 5000


def _render_text_output(result: str, tool_name: str = "", tool_args: dict = None) -> str:
    """将工具结果以格式化 <pre> 文本块渲染

    渲染完全由工具插件决定：优先调用插件注册的 render 闭包
    （如 bash 终端风格 / question 弹窗 / screenshot 图片 / codegraph 结构化），
    未注册闭包时回退通用等宽 <pre> 兜底。主程序不写死任何工具名。
    """
    if not isinstance(result, str):
        result = str(result)
    full = _unescape_newlines(result)
    raw = full[:_MAX_OUTPUT_CHARS]
    if not raw.strip():
        return ""
    tool_args = tool_args or {}
    _gf = _get_global_font()  # 用户主题全局字体

    # ── 插件自定义渲染闭包优先（工具完成框渲染插件化） ──
    try:
        from app.tools.registry import ToolRegistry

        render_fn = ToolRegistry.get_instance().get_render(tool_name)
        if render_fn is not None:
            # 闭包签名：render(result, tool_name, tool_args, success)，result 为 ToolResult 对象
            from app.tools.result import ToolResult as _TR

            body = render_fn(_TR(True, content=result), tool_name, tool_args, True)
            if body:
                return body
    except Exception as e:
        logger.warning(f"[render] 工具 {tool_name} render 闭包异常，回退默认渲染: {e}")

    # ── 通用文本输出兜底 (webfetch, websearch, mouse, keyboard 等) ──
    # 颜色一律走主题变量：早先写死深色底 + 浅色字（rgba(13,17,23,.40) / #c9d1d9），
    # 浅色主题下就是一块突兀的深色盒子。
    # word-break 用 break-word 而非 break-all：搜索摘要这类散文会被 break-all 切碎。
    # max-height + 内部滚动：兜底块最长 5000 字符，不设限会把正文顶掉整屏。
    note = ""
    if len(full) > _MAX_OUTPUT_CHARS:
        note = (
            f'<div style="margin-top:6px;color:var(--text-muted);'
            f'font-size:{scale_font_size(11)}px;font-style:italic;">'
            f"共 {len(full)} 字符，已截断显示前 {_MAX_OUTPUT_CHARS} 字符</div>"
        )
    # 复制按钮：骨架里已有 document 级委托（button[data-action=copy] → clipboard），
    # 复用即可，不用改骨架 JS。复制的是当前显示的内容（截断后的 raw）。
    copy_btn = ""
    if raw.strip():
        try:
            import base64 as _b64

            copy_btn = (
                f'<button type="button" data-action="copy" class="code-btn" data-tooltip="复制结果" '
                f'data-copy="{_b64.b64encode(raw.encode("utf-8")).decode("ascii")}" '
                f'style="position:absolute;top:6px;right:6px;height:22px;padding:0 8px;'
                f'background:var(--panel-soft);border:1px solid var(--border);border-radius:6px;'
                f'cursor:pointer;color:var(--text-secondary);font-size:{scale_font_size(11)}px;">复制</button>'
            )
        except Exception:  # base64 失败不影响展示
            copy_btn = ""
    return f"""
    <div style="position:relative;">
    <pre style="margin:0;padding:10px 12px;background:var(--panel-soft);color:var(--text);font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-word;max-height:520px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;">{escape(raw)}</pre>{copy_btn}</div>{note}"""


def _keep_in_content_tools() -> frozenset:
    """始终展示在正文的工具集合（registry 派生，与 message_card._edit_tools 同源）。

    规则：注册时显式声明 keep_in_content=True（write/edit/multi_edit、
    subagent_para、question 等）。用于工具块渲染 data-keep-in-content 属性。
    """
    try:
        from app.tools.registry import ToolRegistry

        return ToolRegistry.get_instance().keep_in_content_tools()
    except Exception:
        return frozenset()


def _reg_metadata_flag(tool_name: str, key: str) -> bool:
    """查询工具注册声明的 metadata 布尔标记（未注册/异常返回 False）"""
    try:
        from app.tools.registry import ToolRegistry
        from app.tools.tool_name_mapper import ToolNameMapper

        reg = ToolRegistry.get_instance().get(ToolNameMapper.to_native(tool_name))
        return bool(reg and reg.metadata and reg.metadata.get(key))
    except Exception:
        return False


def render_tool_block(
    tool_name: str,
    tool_args: dict,
    result: str = None,
    success: bool = None,
    collapsed: bool = False,
    tool_call_id: str = None,
    diff: str = None,
    echarts: str = None,
    lazy_body: bool = False,
    body_only: bool = False,
) -> str:
    """渲染工具块，参数横向表格展示（左列参数名，右列结果值）

    lazy_body：折叠态 body 只留空占位，内容完全不构造。折叠只是
        `.cm-collapsible__body{height:0}` 的视觉隐藏，节点照样进 DOM，而工具块
        body 是 DOM 内存大头（pygments 高亮实测膨胀 12.7 倍：12KB diff → 154KB
        HTML + 3200 个 span）。展开时由 viewer 从 markdown 反查该块重渲染后注入
        （见 card_viewers 的 tool_body 通道 / window._fillToolBody）。
    body_only：只返回 body 内部 HTML（不含外层块与 header），供懒注入取内容。
    """

    _gf = _get_global_font()  # 用户主题全局字体

    # 检测是否为 MCP 工具（mcp__ 前缀，动态发现工具；固定工具走 registry 图标）
    is_mcp_tool = tool_name.startswith("mcp__")

    # 子智能体任务检测：插件注册 metadata["subagent_task"]=True 声明（表格语义 + 图标配色）
    # 历史消息中的旧名 task（现为 subagent_para 的 alias）经 to_native 归一化后命中
    try:
        from app.tools.registry import ToolRegistry
        from app.tools.tool_name_mapper import ToolNameMapper

        _native = ToolNameMapper.to_native(tool_name)
        _sub_reg = ToolRegistry.get_instance().get(_native)
        is_sub_agent_task = bool(_sub_reg and _sub_reg.metadata and _sub_reg.metadata.get("subagent_task"))
    except Exception:
        is_sub_agent_task = False

    # 图标与颜色按类型区分（颜色走主题 token，禁止再写死）
    if is_mcp_tool:
        icon_name = "websearch"
        title_color = Colors.TOOL_TITLE_MCP
    elif is_sub_agent_task:
        icon_name = "设置-subagent"
        title_color = Colors.TOOL_TITLE_SUBAGENT
    else:
        icon_name = _get_tool_icon_name(tool_name, tool_args)
        title_color = Colors.TOOL_TITLE

    # 状态徽章（图标右上角）
    badge_html = _render_tool_status_badge(success)
    _succ_attr = _tool_success_attr(success)
    icon_html = _get_tool_icon_html(icon_name, tool_name=tool_name)
    cn_name = _get_tool_cn_name(tool_name)

    # 参数展示型工具 → 紧凑单行卡片（无折叠、无 body；render_mode="inline"，read 风格）
    try:
        from app.tools.registry import ToolRegistry

        render_mode = ToolRegistry.get_instance().get_render_mode(tool_name)
    except Exception:
        render_mode = ""
    # render_mode="expand"：禁用折叠框 — 完整卡但无 cm-collapsible 折叠交互，body 始终展开
    no_collapse = render_mode == "expand"
    if render_mode == "none":
        # 不渲染工具完成框
        return ""
    if render_mode == "inline":
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

    # 生成参数预览（折叠时显示）
    # 优先使用插件 preview 闭包（自然语言预览），无匹配时 fallback 到 key=value 格式
    natural = _format_natural_preview(tool_name, tool_args)
    args_preview = natural if natural else _format_args_preview(tool_args)

    # 去重：自然语言预览开头如与工具名重复则省略
    if args_preview.startswith(cn_name):
        args_preview = args_preview[len(cn_name) :].lstrip()

    # 有 description（模型填的意图说明）时：描述进标题位替换工具名，
    # 右侧继续显示被它挤掉的事实信息（文件路径）。描述与路径挤在同一行
    # 会把两者都压扁，故分居两侧；路径缺了不知道这一步改的是哪个文件。
    desc_text = ""
    if isinstance(tool_args, dict):
        desc_text = str(tool_args.get("description") or "").strip()
    title_text = desc_text or cn_name
    # 描述不截断：宁可右侧路径被挤掉，也要把「这一步做什么」完整摆出来
    path_arg = _tool_path_arg(tool_args)
    preview_text = args_preview
    preview_is_path = False
    if desc_text:
        # 有描述时右侧专放文件路径（事实信息），整段可点
        preview_text = _shorten_path(path_arg)
        preview_is_path = bool(path_arg)
    elif len(preview_text) > 80:
        # 无描述时保持原预览，只对超长串做首尾缩短（尾部通常是文件名）
        preview_text = _shorten_path(preview_text, 80)
    preview_span = ""
    if preview_text:
        # 右侧必须单行：路径换行会撑破 header 行高，和下一块工具卡叠在一起
        if preview_is_path:
            preview_inner = _render_path_link(preview_text, path_arg)
        else:
            preview_inner = _linkify_path(preview_text, path_arg) or escape(preview_text)
        preview_span = f'''<span style="color: var(--text-secondary); font-size: {scale_font_size(11)}px; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                {preview_inner}
            </span>'''

    # 生成哈希 key（折叠块懒注入的早返回也要用，故提前到内容构造之前）
    block_seed = "|".join(
        [
            str(tool_name or ""),
            json.dumps(tool_args or {}, option=json.OPT_SORT_KEYS).decode("utf-8"),
            str(result or ""),
            str(success),
        ]
    )
    block_key = "tool-" + hashlib.sha1(block_seed.encode("utf-8")).hexdigest()[:12]

    # ── 不可见工具块懒注入：内容一律不构造，body 只留空占位 ──
    # 两种形态都懒：
    #   折叠态（cm-collapsible，body 是 height:0 的隐藏层）→ 点击 summary 时注入；
    #   无折叠态（render_mode="expand"，body 本应常展开）→ 工具区整体折叠时
    #     用户同样看不到，展开工具区时批量注入（历史卡典型场景）。
    # 无 tool_call_id 的块无法反查源文本，不懒化（否则展开后永久空白）。
    if lazy_body and tool_call_id and (collapsed or no_collapse):
        _keep_attr = ' data-keep-in-content="true"' if tool_name in _keep_in_content_tools() else ""
        _head_spans = f"""<span style="display: inline-flex; align-items: center; gap: 14px; flex: 0 0 auto;">
            <span style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:0 0 auto;">
                {icon_html}
                {badge_html}
            </span>
            <span style="white-space: nowrap; flex: 0 0 auto; {get_font_family_css()}" title="{escape(title_text)}">{escape(title_text)}</span>
        </span>
        <span style="display: flex; align-items: center; gap: 8px; margin-left: 10px; min-width: 0; flex: 1 1 auto; justify-content: flex-start; overflow: hidden;">
            {preview_span}{diff_stats_html}
        </span>"""
        if no_collapse:
            return f"""<div class="tool-block tool-block--no-collapse" data-block-key="{block_key}" data-lazy-body="1" data-tool-name="{escape(tool_name)}" data-tool-call-id="{escape(tool_call_id or "")}"{_keep_attr}{_succ_attr} style="margin: 4px 0; background: transparent; border-radius: 6px;">
    <div class="tool-block__header" style="cursor: default; padding: 4px 8px; color: {title_color}; font-size: {scale_font_size(13)}px; font-weight: 500; display: flex; align-items: center; gap: 6px; width: 100%; overflow: hidden; background: transparent; border: none; text-align: left; box-sizing: border-box; {get_font_family_css()}">
        {_head_spans}
    </div>
    <div class="tool-block__body" style="padding: 0 8px 4px;"></div>
</div>"""
        return f"""<div class="cm-collapsible tool-block" data-block-key="{block_key}" data-expanded="false" data-lazy-body="1" data-tool-name="{escape(tool_name)}" data-tool-call-id="{escape(tool_call_id or "")}"{_keep_attr}{_succ_attr} style="margin: 4px 0; background: transparent; border-radius: 6px;">
    <button type="button" class="cm-collapsible__summary tool-block__summary" aria-expanded="false" style="cursor: pointer; padding: 4px 8px; color: {title_color}; font-size: {scale_font_size(13)}px; font-weight: 500; display: flex; align-items: center; gap: 6px; width: 100%; overflow: hidden; background: transparent; border: none; text-align: left; box-sizing: border-box; {get_font_family_css()}">
        {_head_spans}
        <span class="cm-collapsible__chevron" aria-hidden="true" style="flex: 0 0 auto; margin-left: auto;"></span>
    </button>
    <div class="cm-collapsible__body" style="height:0;opacity:0;overflow:hidden;"></div>
</div>"""

    # ── inline diff 预览区（编辑类工具：全部由插件 render 闭包渲染） ──
    # 主程序不做任何 diff 渲染兜底：工具有 diff 时插件闭包负责输出（如 edit/
    # multi_edit/write 注册的 _render_edit_diff_body），闭包返回 None/异常时
    # 回退通用文本/表格渲染。diff 行数统计仅用于折叠阈值判断。
    diff_html = ""
    diff_line_count = 0
    if diff:
        try:
            from app.tools.registry import ToolRegistry
            from app.tools.result import ToolResult as _TR

            diff_render_fn = ToolRegistry.get_instance().get_render(tool_name)
            if diff_render_fn is not None:
                body = diff_render_fn(_TR(True, content=result or "", diff=diff), tool_name, tool_args, success)
                if body:
                    diff_html = body
        except Exception as e:
            logger.warning(f"[render] 工具 {tool_name} diff 渲染闭包异常: {e}")
            diff_html = ""
        if diff_html:
            diff_line_count = diff_summary["added"] + diff_summary["deleted"]

    # ── ECharts 图表区 ──
    echarts_html = ""
    if echarts:
        try:
            # 工具自定义渲染闭包优先（注册时声明 render 的工具）
            from app.tools.registry import ToolRegistry
            from app.tools.result import ToolResult as _TR

            render_fn = ToolRegistry.get_instance().get_render(tool_name)
            if render_fn is not None:
                body = render_fn(_TR(True, content=result or "", echarts=echarts), tool_name, tool_args, success)
                if body:
                    echarts_html = body
            if not echarts_html:
                import base64 as _b64

                b64_json = _b64.b64encode(echarts.encode("utf-8")).decode("ascii")
                chart_id = "echart-tool-" + hashlib.sha1(echarts.encode("utf-8")).hexdigest()[:12]
                echarts_html = f'''
                <div id="{chart_id}" class="echarts-container" data-echarts-json="{b64_json}" style="width: 100%; height: 400px; margin: 12px 0; border-radius: 10px; overflow: hidden;"></div>'''
        except Exception as e:
            logger.warning(f"[render] 工具 {tool_name} echarts 渲染闭包异常，回退默认图表: {e}")

    # ── 文本输出：所有成功且有结果文本的工具均渲染（闭包路由 / 通用 pre） ──
    # 不再有工具名白名单：任何工具的结果渲染都优先走插件 render 闭包，
    # 未注册闭包则回退通用 <pre>；子智能体任务保持表格渲染。
    raw_output_html = ""
    if result and success is not False and not is_sub_agent_task:
        raw_output_html = _render_text_output(result, tool_name, tool_args)

    # 有 echarts / 截图 / 文本输出 / diff 时：跳过参数表格，直接显示内容
    # 注意：调用方传入的 collapsed 代表模式偏好（简洁模式=True，非简洁=False）。
    # 内容类型逻辑仅在非简洁模式下生效；简洁模式下保持 collapsed=True 全部折叠。
    DIFF_AUTO_COLLAPSE_LINES = 10
    if no_collapse:
        # 禁用折叠框：无论简洁模式偏好，内容始终展开
        collapsed = False
    if echarts:
        if not collapsed:  # 非简洁模式下图表默认展开
            collapsed = False
        expanded_content = f"""
        <div class="tool-expanded-content">
            {echarts_html}
            {diff_html}
        </div>"""
    elif diff and (diff_html or diff_line_count > 0):
        # 编辑类工具：diff 优先于文本输出（diff 是核心结果）。
        # 闭包成功（diff_html 非空）或主程序兜底（diff_line_count > 0）都走此分支
        if not collapsed:  # 非简洁模式下按行数自动判断
            collapsed = diff_line_count > DIFF_AUTO_COLLAPSE_LINES
        expanded_content = f"""
        <div class="tool-expanded-content">
            {echarts_html}
            {diff_html}
        </div>"""
    elif raw_output_html:
        # 文本输出始终折叠（内容通常很长），简洁/非简洁均保持
        collapsed = True
        expanded_content = f"""
        <div class="tool-expanded-content">
            {raw_output_html}
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

    expanded_attr = "false" if collapsed else "true"
    body_style = "" if collapsed else ' style="height:auto; opacity:1;"'

    # 只取 body 内容（懒注入通道用）：不含外层块与 header
    if body_only:
        return expanded_content

    if no_collapse:
        # 禁用折叠框渲染：无 cm-collapsible 交互，标题栏 + body 直接展示
        _keep_attr = ' data-keep-in-content="true"' if tool_name in _keep_in_content_tools() else ""
        return f"""<div class="tool-block tool-block--no-collapse" data-tool-name="{escape(tool_name)}" data-tool-call-id="{escape(tool_call_id or "")}"{_keep_attr}{_succ_attr} style="margin: 4px 0; background: transparent; border-radius: 6px;">
    <div class="tool-block__header" style="cursor: default; padding: 4px 8px; color: {title_color}; font-size: {scale_font_size(13)}px; font-weight: 500; display: flex; align-items: center; gap: 6px; width: 100%; overflow: hidden; background: transparent; border: none; text-align: left; box-sizing: border-box; {get_font_family_css()}">
        <span style="display: inline-flex; align-items: center; gap: 14px; flex: 0 0 auto;">
            <span style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:0 0 auto;">
                {icon_html}
                {badge_html}
            </span>
            <span style="white-space: nowrap; flex: 0 0 auto; {get_font_family_css()}" title="{escape(title_text)}">{escape(title_text)}</span>
        </span>
        <span style="display: flex; align-items: center; gap: 8px; margin-left: 10px; min-width: 0; flex: 1 1 auto; justify-content: flex-start; overflow: hidden;">
            {preview_span}{diff_stats_html}
        </span>
    </div>
    <div class="tool-block__body" style="padding: 0 8px 4px;">
        {expanded_content}
    </div>
</div>"""

    _keep_attr = ' data-keep-in-content="true"' if tool_name in _keep_in_content_tools() else ""
    return f"""<div class="cm-collapsible tool-block" data-block-key="{block_key}" data-expanded="{expanded_attr}" data-tool-name="{escape(tool_name)}" data-tool-call-id="{escape(tool_call_id or "")}"{_keep_attr}{_succ_attr} style="margin: 4px 0; background: transparent; border-radius: 6px;">
    <button type="button" class="cm-collapsible__summary tool-block__summary" aria-expanded="{expanded_attr}" style="cursor: pointer; padding: 4px 8px; color: {title_color}; font-size: {scale_font_size(13)}px; font-weight: 500; display: flex; align-items: center; gap: 6px; width: 100%; overflow: hidden; background: transparent; border: none; text-align: left; box-sizing: border-box; {get_font_family_css()}">
        <span style="display: inline-flex; align-items: center; gap: 14px; flex: 0 0 auto;">
            <span style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:0 0 auto;">
                {icon_html}
                {badge_html}
            </span>
            <span style="white-space: nowrap; flex: 0 0 auto; {get_font_family_css()}" title="{escape(title_text)}">{escape(title_text)}</span>
        </span>
        <span style="display: flex; align-items: center; gap: 8px; margin-left: 10px; min-width: 0; flex: 1 1 auto; justify-content: flex-start; overflow: hidden;">
            {preview_span}{diff_stats_html}
        </span>
        <span class="cm-collapsible__chevron" aria-hidden="true" style="flex: 0 0 auto; margin-left: auto;"></span>
    </button>
    <div class="cm-collapsible__body"{body_style}>
        {expanded_content}
    </div>
</div>"""




def invalidate_render_caches():
    """清除 render_helpers 模块级缓存（qrc icon 存在性 + diff formatter + lexer）。

    在新建/切换会话或主题切换时调用，避免缓存的 qrc 查询结果、diff formatter
    实例和 pygments lexer 实例累积。每个缓存内部已有 max 防御上限，本函数
    用于主动一次性释放。
    """
    _QRC_ICON_EXISTS_CACHE.clear()
    _DIFF_FORMATTER_CACHE.clear()
    _DIFF_LEXER_CACHE.clear()
