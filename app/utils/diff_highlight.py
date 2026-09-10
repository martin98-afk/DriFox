# -*- coding: utf-8 -*-
"""
行内 diff 高亮子系统（独立模块，零 Qt / 零 widgets 依赖）。

原位于 app/widgets/render_helpers.py；抽取到 utils 以消除
utils → widgets 反向依赖（diff_viewer 等 utils 模块需要 diff 高亮，
不应反向 import widgets 层）。render_helpers 通过 re-export 保持
存量引用兼容，tests 无需改动。主题感知仅通过函数内对 theme_manager
的 lazy import（try/except 兜底），导入期不触发任何 Qt 依赖。
"""

import difflib
import os
import re
from html import escape

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


def _sync_diff_style_to_theme() -> None:
    """根据当前主题同步 _current_diff_style，确保 _get_diff_formatter 返回正确风格。

    🐛 工具渲染管线（render_helpers.format_tool_block → _render_edit_diff_body）
    调用 _render_diff_preview 时不会经过 message_card._render_markdown_to_html
    的 set_diff_highlight_style 入口，因此主题切换后 _current_diff_style 可能
    仍是旧值，formatter 仍用 dracula 风格的前景色渲染到浅色主题背景上
    → 白字白底不可见（"偶尔出现"是因为切主题后第一次 markdown 渲染恰好
    把 _current_diff_style 同步过去才看起来正常）。
    此处按当前主题即时同步，避免渲染管线入口漏同步导致颜色错位。
    """
    try:
        from app.utils.theme_manager import theme_manager

        target = "friendly" if theme_manager.is_light_theme() else "dracula"
        if target != _current_diff_style:
            set_diff_highlight_style(target)
    except Exception:
        # 主题管理器尚未初始化（如单元测试/导入期）→ 保持当前风格，不破坏渲染
        pass


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
# 防御上限：扩展名种类有限（<64），超限整体清空防膨胀
_DIFF_LEXER_CACHE_MAX = 64

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
    if len(_DIFF_LEXER_CACHE) >= _DIFF_LEXER_CACHE_MAX:
        _DIFF_LEXER_CACHE.clear()  # 防御膨胀：超限整体清空
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


# 词级差异分词正则（模块级别缓存，避免重复编译）
_WORD_RE = re.compile(r"(\w+|\W+)")
