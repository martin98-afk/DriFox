# -*- coding: utf-8 -*-
"""语法高亮 — 按文件类型做词法着色（pygments 驱动，缺失时自动降级）

为什么全文 lex 再按行分桶，而不是在 QSyntaxHighlighter 回调里逐块分析：
- QSyntaxHighlighter 是逐 block 回调的，把 pygments 的状态机拆进回调要自己维护
  lexer 状态栈，跨行结构（三引号、模板字符串、嵌套注释）极易错位
- 预览本身受 TEXT_MAX_LINES 限流（≤2000 行），一次性 lex 开销可控；
  超过 HIGHLIGHT_MAX_BYTES / HIGHLIGHT_MAX_LINES 直接不高亮，避免同步卡 UI

配色：暗色取 One Dark，亮色取 Solarized Light，跟随卡片主题切换。
"""

import os
from typing import Dict, List, Optional, Tuple

from PyQt5.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument

# ── 限流阈值 ──────────────────────────────────────────────

HIGHLIGHT_MAX_BYTES = 256 * 1024  # 超过纯文本预览（全文 lex 是同步 CPU 活）
HIGHLIGHT_MAX_LINES = 4000

# ── 配色表 ────────────────────────────────────────────────

# 样式键 → (暗色, 亮色, 粗体, 斜体)
_PALETTE: Dict[str, Tuple[str, str, bool, bool]] = {
    "keyword": ("#c678dd", "#a626a4", False, False),
    "string": ("#98c379", "#50a14f", False, False),
    "comment": ("#7f848e", "#a0a1a7", False, True),
    "number": ("#d19a66", "#986801", False, False),
    "function": ("#61afef", "#4078f2", False, False),
    "class": ("#e5c07b", "#c18401", False, False),
    "builtin": ("#56b6c2", "#0184bc", False, False),
    "operator": ("#56b6c2", "#0184bc", False, False),
    "error": ("#e06c75", "#e45649", True, False),
}

# ── token → 样式键 ────────────────────────────────────────


def _style_of(ttype) -> str:
    """pygments token 类型 → 样式键（空串表示沿用默认文字色）"""
    try:
        from pygments.token import Token
    except ImportError:
        return ""
    try:
        if ttype in Token.Comment:
            return "comment"
        # Decorator / Attribute / Class / Namespace / Tag 归一类（黄），
        # 语义上都是「名字」，不需要四套颜色
        if (
            ttype in Token.Name.Class
            or ttype in Token.Name.Namespace
            or ttype in Token.Name.Decorator
            or ttype in Token.Name.Attribute
        ):
            return "class"
        if ttype in Token.Name.Tag:
            return "keyword"
        if ttype in Token.Name.Function:
            return "function"
        if ttype in Token.Name.Builtin:
            return "builtin"
        if ttype in Token.Literal.String:
            return "string"
        if ttype in Token.Literal.Number:
            return "number"
        if ttype in Token.Keyword:
            return "keyword"
        if ttype in Token.Operator:
            return "operator"
        if ttype in Token.Error:
            return "error"
    except TypeError:  # 非 pygments token（理论上不会出现）
        return ""
    return ""


# ── 词法器探测 ────────────────────────────────────────────


def find_lexer(path: str):
    """按文件名找 pygments 词法器，返回 ``(lexer, 语言名)``；找不到返回 ``(None, "")``"""
    try:
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:
        return None, ""
    name = os.path.basename(path or "")
    if not name:
        return None, ""
    try:
        lexer = get_lexer_for_filename(name)
    except ClassNotFound:
        return None, ""
    except Exception:  # 词法器内部正则冲突等，一律当「没有」
        return None, ""
    try:
        label = lexer.name or (lexer.aliases[0] if lexer.aliases else "")
    except Exception:
        label = ""
    return lexer, label


# ── 分桶 ──────────────────────────────────────────────────


def build_spans(text: str, lexer) -> List[List[Tuple[int, int, str]]]:
    """把整段文本 lex 成 ``spans[行号] = [(起, 止, 样式键), ...]``（字符下标，不含换行）"""
    line_count = text.count("\n") + 1
    spans: List[List[Tuple[int, int, str]]] = [[] for _ in range(line_count)]
    line = 0
    col = 0
    for ttype, value in lexer.get_tokens(text):
        if not value:
            continue
        style = _style_of(ttype)
        parts = value.split("\n")
        for i, part in enumerate(parts):
            if i:
                line += 1
                col = 0
            if part and style and 0 <= line < line_count:
                spans[line].append((col, col + len(part), style))
            col += len(part)
    return spans


def analyze(path: str, text: str) -> Tuple[List[List[Tuple[int, int, str]]], str, str]:
    """分析文本，返回 ``(spans, 语言名, 跳过原因)``；跳过原因为空串表示高亮可用"""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = len(text.encode("utf-8", "ignore"))
    if size > HIGHLIGHT_MAX_BYTES:
        return [], "", "too_large"
    if text.count("\n") + 1 > HIGHLIGHT_MAX_LINES:
        return [], "", "too_many_lines"
    lexer, label = find_lexer(path)
    if lexer is None:
        return [], "", "unknown"
    try:
        return build_spans(text, lexer), label, ""
    except Exception:
        return [], "", "failed"


# ── 高亮器 ────────────────────────────────────────────────


class CodeHighlighter(QSyntaxHighlighter):
    """按预计算 spans 上色的高亮器（不改文本，只 setFormat）"""

    def __init__(self, document: QTextDocument):
        super().__init__(document)
        self._spans: List[List[Tuple[int, int, str]]] = []
        self._formats: Dict[str, QTextCharFormat] = {}
        self._is_dark = True
        self.set_theme(is_dark=True)

    # ── 主题 ──

    def set_theme(self, is_dark: bool = True) -> None:
        self._is_dark = bool(is_dark)
        self._formats = {}
        for key, (dark, light, bold, italic) in _PALETTE.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(dark if self._is_dark else light))
            if bold:
                fmt.setFontWeight(QFont.Bold)
            if italic:
                fmt.setFontItalic(True)
            self._formats[key] = fmt
        self.rehighlight()

    # ── 内容 ──

    def clear(self) -> None:
        self._spans = []
        self.rehighlight()

    def set_spans(self, spans: List[List[Tuple[int, int, str]]]) -> None:
        self._spans = spans or []
        self.rehighlight()

    def has_spans(self) -> bool:
        return bool(self._spans)

    # ── 回调 ──

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt 命名)
        if not self._spans:
            return
        block = self.currentBlock()
        if block is None:
            return
        number = block.blockNumber()
        if number < 0 or number >= len(self._spans):
            return
        length_limit = len(text)
        for start, end, style in self._spans[number]:
            fmt = self._formats.get(style)
            if fmt is None or start >= length_limit:
                continue
            length = min(end, length_limit) - start
            if length > 0:
                self.setFormat(start, length, fmt)
