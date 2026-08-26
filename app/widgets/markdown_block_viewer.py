# -*- coding: utf-8 -*-
"""纯 Qt 块级 Markdown 渲染器原型（P0/P1 预研）。

目标：验证用原生 Qt 控件替换 CodeWebViewer (QWebEngineView) 的视觉还原度。
覆盖块类型：标题/段落(行内格式/链接/行内code)/列表/表格/引用/分隔线/
代码块(Pygments 高亮+行号+复制)/think 折叠卡/<tool> 工具卡/
「⚙ 工具与思考」折叠分区/任务列表(update_todo_list)。
不含：ECharts(P2)、图片异步加载(P2)、diff 内嵌高亮(P2)。

设计：
- 解析层：markdown 文本 → 有序块列表（think/tool/code/html），复用项目现有的
  python-markdown 实例与 Pygments formatter，保证高亮配色与 WebEngine 版一致。
- 控件层：每个块映射一个 QWidget。think/tool 归入「工具与思考」区（对齐
  WebEngine 版 reorganizeContent 行为）；正文块在下方按文档顺序排列。
- 高度由原生 layout 协商，无需 JS 上报（对应 pywebview_height 链路整体删除）。
"""

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QPropertyAnimation, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QTextOption
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import html as _html_mod

from app.utils.design_tokens import Colors, get_unified_scrollbar_style, scale_font_size
from app.utils.utils import get_font_family_css, get_icon

try:  # pygments 独立包，顶层安全导入
    from pygments import highlight

    _HAS_PYGMENTS = True
except Exception:  # pragma: no cover - demo 环境兜底
    _HAS_PYGMENTS = False


def _md_components():
    """延迟获取 message_card 的 markdown/Pygments 共享组件。

    必须延迟：message_card 顶层 import 本模块（灰度接入），顶层反向导入会
    形成循环，导致拿到未初始化的半成品模块（组件缺失被 except 吞掉，
    正文退化为纯文本渲染——曾致 `##`/`**` 字面显示）。
    """
    from app.widgets.message_card import _get_formatter_cached, _get_lexer_cached, get_markdown_instance

    return get_markdown_instance, _get_lexer_cached, _get_formatter_cached


def _tool_cn_name_safe(name: str) -> str:
    try:
        from app.widgets.render_helpers import _get_tool_cn_name

        return _get_tool_cn_name(name)
    except Exception:
        return name


def _is_light_theme() -> bool:
    try:
        from app.utils.theme_manager import theme_manager

        return theme_manager.is_light_theme()
    except Exception:
        return False


# ── 解析常量 ──────────────────────────────────────────────────────
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_TOOL_OPEN = "<tool>"
_TOOL_CLOSE = "</tool>"
_CODE_FONT_SIZE = scale_font_size(13)
_CODE_BORDER = "rgba(58, 63, 71, 0.6)"
_LANG_COLOR = "#FFA500"
_BODY_MAX_HEIGHT = 400  # 代码区内部滚动上限(px)

_FENCE_RE = re.compile(r"^([ \t]*)```([\w+#.-]*)[ \t]*\n", re.M)


# ── 通用标记段切分 ────────────────────────────────────────────────
def _split_tag_segments(md_text: str, open_tag: str, close_tag: str) -> List[Tuple[str, str, bool]]:
    """切分 <tag>...</tag> 段。返回 [("text"|"tag", 内容, 是否闭合)]。

    与 CodeWebViewer._inject_think_cards 同策略：open 到「下一个 open 前
    的最后一个 close」为一个块；未闭合标记 closed=False（流式态）。
    """
    segments: List[Tuple[str, str, bool]] = []
    i = 0
    while i < len(md_text):
        start = md_text.find(open_tag, i)
        if start == -1:
            rest = md_text[i:].replace(close_tag, "")
            if rest.strip():
                segments.append(("text", rest, True))
            break
        pre = md_text[i:start]
        if pre.strip():
            segments.append(("text", pre, True))
        t0 = start + len(open_tag)
        nxt = md_text.find(open_tag, t0)
        search_end = nxt if nxt != -1 else len(md_text)
        close = md_text.rfind(close_tag, t0, search_end)
        if close != -1:
            content = md_text[t0:close]
            if content.strip():
                segments.append(("tag", content, True))
            i = close + len(close_tag)
        else:
            content = md_text[t0:search_end]
            if content.strip():
                segments.append(("tag", content, False))
            i = search_end
    return segments


def _split_code_fences(text: str) -> List[Tuple[str, str, str, bool]]:
    """在文本段内切分 fenced code。返回 [("text"|"code", 内容, lang, closed)]。"""
    out: List[Tuple[str, str, str, bool]] = []
    pos = 0
    while pos < len(text):
        m = _FENCE_RE.search(text, pos)
        if not m:
            seg = text[pos:]
            if seg.strip():
                out.append(("text", seg, "", True))
            break
        head = text[pos : m.start()]
        if head.strip():
            out.append(("text", head, "", True))
        lang = (m.group(2) or "").strip()
        body_start = m.end()
        indent = m.group(1) or ""
        closer = re.compile(r"^" + re.escape(indent) + r"```[ \t]*$", re.M)
        cm = closer.search(text, body_start)
        if cm is not None:
            code_body = text[body_start : cm.start()]
            out.append(("code", code_body, lang, True))
            pos = cm.end()
        else:  # 未闭合（流式中）
            code_body = text[body_start:]
            out.append(("code", code_body, lang, False))
            pos = len(text)
    return out


# ── <tool> 字段解析（简化版，对齐 message_card 规则） ─────────────
_TOOL_NAME_RE = re.compile(r"^name:\s*(.+)$", re.M)
_TOOL_SUCCESS_RE = re.compile(r"^success:\s*(true|false)\s*$", re.M | re.I)


def _extract_balanced_braces(text: str, start: int) -> Optional[str]:
    """从 start 处的 { 提取平衡 JSON 对象（跳过字符串内括号）。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if in_str:
            if c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _is_edit_tool_name(name: str) -> bool:
    """延迟判断编辑类工具（结果展示在正文而非工具折叠区，对齐 WebEngine 版规则）。"""
    if not name:
        return False
    try:
        from app.widgets.message_card import _edit_tools

        return name in _edit_tools()
    except Exception:
        return False


def _parse_tool_block(content: str) -> Dict[str, Any]:
    """解析 <tool> 内部字段 → {name, cn_name, args_json, args_raw, result, success}"""
    content = content.strip()
    name_m = _TOOL_NAME_RE.search(content)
    name = name_m.group(1).strip() if name_m else ""
    success_m = None
    for success_m in _TOOL_SUCCESS_RE.finditer(content):  # 取最后一个行首匹配
        pass
    success = (success_m.group(1).lower() == "true") if success_m else True

    # args：name 行之后的第一个平衡 {}
    args_raw = ""
    if name_m:
        brace = content.find("{", name_m.end())
        if brace != -1:
            extracted = _extract_balanced_braces(content, brace)
            if extracted:
                args_raw = extracted

    # result：行首 "result:" 之后，终点 = 各元字段（diff/success/tool_call_id/echarts）
    # 最后行首匹配位置的最小值（对齐 message_card._render_tool_block_content 规则）
    meta_positions = [m.start() for m in re.finditer(r"^(?:diff|success|tool_call_id|echarts):", content, re.M)]
    result = ""
    diff_content = ""
    rm = None
    for rm in re.finditer(r"^result:\s?", content, re.M):
        pass
    if rm is not None:
        result_end = len(content)
        for pos in meta_positions:
            if pos > rm.end():
                result_end = min(result_end, pos)
        result = content[rm.end() : result_end].strip()

    # diff：行首 "diff:" 之后到下一个元字段或结尾（仅 edit/write 类工具携带）
    dm = None
    for dm in re.finditer(r"^diff:\s?", content, re.M):
        pass
    if dm is not None:
        diff_after = content[dm.end() :]
        nxt = re.search(r"^(?:success|tool_call_id|echarts):", diff_after, re.M)
        diff_content = (diff_after[: nxt.start()] if nxt else diff_after).strip()

    cn_name = name
    try:
        cn_name = _tool_cn_name_safe(name) if name else name
    except Exception:
        pass
    return {
        "name": name,
        "cn_name": cn_name or name,
        "args_raw": args_raw,
        "result": result,
        "diff": diff_content,
        "success": success,
        "is_edit": _is_edit_tool_name(name),
    }


def parse_blocks(md_text: str) -> List[Dict[str, Any]]:
    """markdown 全文 → 块列表。type ∈ think/tool/code/html。"""
    blocks: List[Dict[str, Any]] = []
    for kind, content, closed in _split_tag_segments(md_text, _THINK_OPEN, _THINK_CLOSE):
        if kind == "tag":
            blocks.append({"type": "think", "content": content, "completed": closed})
            continue
        for tkind, tbody, tclosed in _split_tag_segments(content, _TOOL_OPEN, _TOOL_CLOSE):
            if tkind == "tag":
                fields = _parse_tool_block(tbody)
                blocks.append({"type": "tool", **fields, "closed": tclosed})
                continue
            for ckind, cbody, lang, fence_closed in _split_code_fences(tbody):
                if ckind == "code":
                    blocks.append({"type": "code", "lang": lang, "code": cbody, "closed": fence_closed})
                else:
                    blocks.append({"type": "html", "html": _md_to_html(cbody)})
    return blocks


SIDE_TYPES = ("think", "tool")


def _block_key(b: Dict[str, Any]) -> str:
    """块内容指纹：reconcile 时跳过未变化的块。"""
    if b["type"] == "tool":
        raw = "tool:" + b.get("name", "") + "|" + b.get("args_raw", "") + "|" + b.get("result", "")
    else:
        raw = b.get("html") or b.get("code") or b.get("content") or ""
    tail = f"{b.get('lang', '')}|{b.get('completed', b.get('closed', ''))}"
    return b["type"] + ":" + hashlib.sha1((raw + str(tail)).encode("utf-8")).hexdigest()[:12]


def _md_to_html(md_fragment: str) -> str:
    """markdown 片段 → 注入行内样式的 HTML（Qt 富文本兼容）。"""
    try:
        get_markdown_instance, _, _ = _md_components()
        md = get_markdown_instance()
        md.reset()
        html = md.convert(md_fragment)
    except Exception:
        html = "<p>" + _html_mod.escape(md_fragment) + "</p>"
    return _inject_qt_rich_styles(html)


_INLINE_CODE_STYLE_RE = re.compile(r"<code>(?!style)")


def _inline_code_style() -> str:
    bg = "#ececf1" if _is_light_theme() else "rgba(255,255,255,0.09)"
    fg = "#7c3aed" if _is_light_theme() else "#e8b4f8"
    return f"background-color:{bg}; font-family:'Consolas',monospace; color:{fg};"


def _inject_qt_rich_styles(html: str) -> str:
    """为 Qt 富文本补齐浏览器默认样式：行内 code 底色、表格边框。"""
    html = _INLINE_CODE_STYLE_RE.sub(lambda _: f'<code style="{_inline_code_style()}">', html)
    if "<table" in html:
        html = html.replace("<table>", '<table border="1" cellspacing="0" cellpadding="5">', 1)
    return html


# ── 控件层：正文块 ────────────────────────────────────────────────
class RichTextLabel(QLabel):
    """段落/标题/列表/表格：QLabel 富文本。"""

    def __init__(self, html: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setOpenExternalLinks(True)
        self.setTextInteractionFlags(Qt.TextBrowserInteraction | Qt.TextSelectableByMouse)
        self.set_html(html)

    def set_html(self, html: str) -> None:
        self.setStyleSheet(
            f"QLabel {{ {get_font_family_css()} font-size:{scale_font_size(14)}px;"
            f" color:{Colors.ASSISTANT_CARD_TEXT}; background:transparent; }}"
        )
        self.setText(html)


class CodeBlockWidget(QFrame):
    """代码块：语言标签栏 + 行号列(主题色) + Pygments 高亮正文 + 复制按钮。"""

    saveFileRequested = pyqtSignal(str, str)  # (code, lang)

    @staticmethod
    def _code_bg() -> str:
        """代码区实色背景：QTextEdit 透明背景 + 富文本 HTML 在 Qt 下会产生重绘残影，
        必须用不透明底色（实验验证，见 tests/debug/_ab_matrix.py）。"""
        return "#f6f6f7" if _is_light_theme() else "#26272e"

    def __init__(self, code: str, lang: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._raw_code = code
        self._lang = lang
        self.setObjectName("codeBlock")
        # 高度自适应定时器（先于 _apply_highlight 创建）
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.setInterval(0)
        self._height_timer.timeout.connect(self._update_height)
        self.setObjectName("codeBlock")
        self.setStyleSheet(
            f"""
            QFrame#codeBlock {{
                background: {CodeBlockWidget._code_bg()};
                border: 1px solid {_CODE_BORDER};
                border-radius: 10px;
            }}
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 顶部工具栏 ──
        bar = QFrame(self)
        bar.setStyleSheet(
            "QFrame { background: rgba(255,255,255,0.03); border:none;"
            f" border-bottom:1px solid {_CODE_BORDER}; border-top-left-radius:10px; border-top-right-radius:10px; }}"
        )
        bar.setFixedHeight(30)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 0, 6, 0)
        lang_label = QLabel(lang if lang else "Plain Text", bar)
        lang_label.setStyleSheet(
            f"{get_font_family_css()} color:{_LANG_COLOR if lang else '#888888'};"
            f" font-size:{_CODE_FONT_SIZE}px; font-weight:bold; background:transparent; border:none;"
        )
        bl.addWidget(lang_label)
        bl.addStretch()
        save_btn = QToolButton(bar)
        save_btn.setIcon(get_icon("导入"))
        save_btn.setAutoRaise(True)
        save_btn.setToolTip("保存本地文件")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.clicked.connect(lambda: self.saveFileRequested.emit(self._raw_code, self._lang))
        copy_btn = QToolButton(bar)
        copy_btn.setIcon(get_icon("复制"))
        copy_btn.setAutoRaise(True)
        copy_btn.setToolTip("复制代码")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.clicked.connect(self._copy_code)
        bl.addWidget(save_btn)
        bl.addWidget(copy_btn)
        root.addWidget(bar)

        # ── 正文区：行号列 + 代码 ──
        body = QHBoxLayout()
        body.setContentsMargins(0, 6, 0, 6)
        body.setSpacing(0)
        self._line_numbers = QLabel("", self)
        self._line_numbers.setAlignment(Qt.AlignTop | Qt.AlignRight)
        self._line_numbers.setStyleSheet(
            f"QLabel {{ color:{self._line_color()}; background:transparent; border:none;"
            f" font-family:'Consolas',monospace; font-size:{_CODE_FONT_SIZE}px;"
            " padding:0 8px 0 12px; }"
        )
        body.addWidget(self._line_numbers)

        self.text_edit = QTextEdit(self)
        self.text_edit.setReadOnly(True)
        self.text_edit.setFrameShape(QTextEdit.NoFrame)
        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_edit.setWordWrapMode(QTextOption.NoWrap)
        self.text_edit.setStyleSheet(
            "QTextEdit { background:" + self._code_bg() + "; border:none;"
            f" font-family:'Consolas',monospace; font-size:{_CODE_FONT_SIZE}px; }}\n"
            + ("" if os.environ.get("AB_NOSCROLLBAR") else get_unified_scrollbar_style(6))
        )
        body.addWidget(self.text_edit, 1)
        root.addLayout(body)

        self._apply_highlight(code)

    @staticmethod
    def _line_color() -> str:
        """行号颜色跟随主题：深色主题白系半透明，浅色主题黑系半透明。"""
        return "rgba(0,0,0,0.38)" if _is_light_theme() else "rgba(255,255,255,0.35)"

    def _apply_highlight(self, code: str) -> None:
        inner = None
        if _HAS_PYGMENTS:
            try:
                # 与 WebEngine 版同步：按主题切换 Pygments 风格（浅色 friendly / 深色 dracula），
                # 否则浅色主题下 dracula 浅灰文字在浅色底上不可见
                try:
                    from app.widgets.message_card import set_pygments_style

                    set_pygments_style("friendly" if _is_light_theme() else "dracula")
                except Exception:
                    pass
                _, get_lexer_cached, get_formatter_cached = _md_components()
                lexer = get_lexer_cached(self._lang)
                fmt = get_formatter_cached()
                highlighted = highlight(code, lexer, fmt)
                m = re.search(r"<pre[^>]*>(.*?)</pre>", highlighted, re.S)
                inner = m.group(1) if m else None
            except Exception:
                inner = None
        if inner is None:
            inner = _html_mod.escape(code)
        self.text_edit.document().setDefaultStyleSheet("pre { margin:0; padding:0 0 0 4px; }")
        self.text_edit.setHtml(f"<pre style='margin:0; white-space:pre;'>{inner}</pre>")
        self._line_numbers.setText("\n".join(str(i + 1) for i in range(max(1, len(code.splitlines())))))
        self._height_timer.start()

    def update_content(self, code: str, lang: str) -> None:
        """流式原地更新（不重建控件）。"""
        self._raw_code = code
        self._lang = lang
        self._apply_highlight(code)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._height_timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._update_height)

    def _update_height(self) -> None:
        if self.text_edit.viewport().width() <= 0:
            return
        doc_h = int(self.text_edit.document().size().height()) + 12
        self.text_edit.setFixedHeight(min(doc_h, _BODY_MAX_HEIGHT))

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        doc_h = min(int(self.text_edit.document().size().height()) + 44, _BODY_MAX_HEIGHT + 44)
        return QSize(hint.width(), max(doc_h, 64))

    def _copy_code(self) -> None:
        QApplication.clipboard().setText(self._raw_code)


class ThinkCard(QFrame):
    """思考折叠卡：头部(图标+标签+预览+chevron) + 可动画展开的正文区。"""

    CHEVRON_DOWN = "▾"
    CHEVRON_RIGHT = "▸"

    def __init__(self, content: str, completed: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._expanded = False
        self._anim: Optional[QPropertyAnimation] = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(0)

        header = QPushButton(self)
        header.setCursor(Qt.PointingHandCursor)
        header.setStyleSheet(
            f"QPushButton {{ text-align:left; background:rgba(127,127,127,0.06);"
            f" border:1px solid rgba(127,127,127,0.15); border-radius:8px;"
            f" padding:5px 10px; {get_font_family_css()} }}"
            f"QPushButton:hover {{ background:rgba(127,127,127,0.12); }}"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(2, 0, 6, 0)
        icon_label = QLabel("💡", header)
        self._title_label = QLabel("深度思考", header)
        self._title_label.setStyleSheet(
            f"{get_font_family_css()} color:{Colors.ASSISTANT_CARD_MUTED};"
            f" font-size:{scale_font_size(13)}px; font-weight:600; background:transparent;"
        )
        self._preview_label = QLabel("", header)
        self._preview_label.setStyleSheet(
            f"{get_font_family_css()} color:{Colors.ASSISTANT_CARD_MUTED};"
            f" font-size:{scale_font_size(11)}px; background:transparent;"
        )
        self._chevron = QLabel(self.CHEVRON_RIGHT, header)
        self._chevron.setStyleSheet(
            f"color:{Colors.ASSISTANT_CARD_MUTED}; font-size:{scale_font_size(12)}px; background:transparent;"
        )
        for w in (icon_label, self._title_label, self._preview_label):
            hl.addWidget(w)
        hl.addStretch()
        hl.addWidget(self._chevron)
        header.clicked.connect(self.toggle)
        root.addWidget(header)

        # 正文包裹层：maximumHeight 动画目标
        self._body_wrap = QFrame(self)
        self._body_wrap.setMaximumHeight(0)
        bw = QVBoxLayout(self._body_wrap)
        bw.setContentsMargins(12, 8, 8, 4)
        plain = content.replace("```", "").strip()
        self._body_label = QLabel(plain, self._body_wrap)
        self._body_label.setWordWrap(True)
        self._body_label.setTextFormat(Qt.PlainText)
        self._body_label.setStyleSheet(
            f"{get_font_family_css()} color:{Colors.ASSISTANT_CARD_MUTED};"
            f" font-size:{scale_font_size(13)}px; background:transparent;"
        )
        bw.addWidget(self._body_label)
        root.addWidget(self._body_wrap)

    def set_streaming(self) -> None:
        """流式态：单行提示，无折叠。"""
        self.apply_streaming(True)

    def apply_streaming(self, active: bool) -> None:
        """校正流式态标记（reconcile 复用控件时内容指纹不变但状态可能翻转）。"""
        if active:
            self._title_label.setText("深度思考中...")
            self._preview_label.setText("")
            self._chevron.hide()
        else:
            self._title_label.setText("深度思考")
            self._chevron.show()

    def toggle(self) -> None:
        target = self._body_wrap.sizeHint().height() if not self._expanded else 0
        self._expanded = not self._expanded
        self._chevron.setText(self.CHEVRON_DOWN if self._expanded else self.CHEVRON_RIGHT)
        self._anim = QPropertyAnimation(self._body_wrap, b"maximumHeight", self)
        self._anim.setDuration(180)
        self._anim.setStartValue(self._body_wrap.maximumHeight())
        self._anim.setEndValue(max(target, 1))
        self._anim.finished.connect(self._on_anim_done)
        self._anim.start()

    def _on_anim_done(self) -> None:
        if self._expanded:
            self._body_wrap.setMaximumHeight(16777215)


class DiffViewWidget(QTextEdit):
    """内嵌 diff 视图：行级 +/- 着色 + hunk/文件头样式（对齐 WebEngine 版 tool-diff-inline）。"""

    _ADD_BG, _ADD_FG = "rgba(63,185,80,0.15)", ("#22863a", "#3fb950")
    _DEL_BG, _DEL_FG = "rgba(248,81,73,0.15)", ("#b31d28", "#f85149")
    AUTO_COLLAPSE_LINES = 10
    MAX_HEIGHT = 320

    def __init__(self, diff_text: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QTextEdit.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setWordWrapMode(QTextOption.NoWrap)
        self.setStyleSheet(
            "QTextEdit { background:rgba(127,127,127,0.06); border:none; border-radius:6px;"
            " font-family:'Consolas',monospace; font-size:"
            f"{scale_font_size(12)}px; }}\n{get_unified_scrollbar_style(6)}"
        )
        light = _is_light_theme()
        rows = []
        for line in diff_text.splitlines():
            esc = _html_mod.escape(line)
            if line.startswith(("+++", "---")):
                rows.append(f'<div style="font-weight:700; color:{Colors.ASSISTANT_CARD_MUTED};">{esc}</div>')
            elif line.startswith("@@"):
                rows.append(f'<div style="color:#539bf5;">{esc}</div>')
            elif line.startswith("+"):
                fg = self._ADD_FG[0] if light else self._ADD_FG[1]
                rows.append(f'<div style="background:{self._ADD_BG}; color:{fg};">{esc}</div>')
            elif line.startswith("-"):
                fg = self._DEL_FG[0] if light else self._DEL_FG[1]
                rows.append(f'<div style="background:{self._DEL_BG}; color:{fg};">{esc}</div>')
            else:
                rows.append(f"<div>{esc or '&nbsp;'}</div>")
        self.setHtml(
            f"<div style=\"white-space:pre-wrap; font-family:'Consolas',monospace; line-height:1.5;\">{''.join(rows)}</div>"
        )
        line_count = len(diff_text.splitlines())
        if line_count > self.AUTO_COLLAPSE_LINES:
            self.setFixedHeight(self.MAX_HEIGHT)
        else:
            # 行数估算高度：setHtml 后立即取 document().size() 会得到未完成布局的偏小值
            line_h = scale_font_size(12) * 1.55
            self.setFixedHeight(min(int(line_count * line_h) + 16, self.MAX_HEIGHT))


def _unified_table_html(block: Dict[str, Any]) -> str:
    """参数+结果合并两列表格（对齐 WebEngine 版 _format_unified_table 语义）。"""
    muted = Colors.ASSISTANT_CARD_MUTED
    text = Colors.ASSISTANT_CARD_TEXT
    rows = []
    try:
        args = json.loads(block.get("args_raw") or "{}")
        if not isinstance(args, dict):
            args = {}
    except Exception:
        args = {}
    for k, v in args.items():
        v_str = _html_mod.escape(_truncate_val(v))
        rows.append(
            f'<tr><td style="color:{muted}; padding-right:10px; vertical-align:top;">{_html_mod.escape(str(k))}</td>'
            f'<td style="color:{text};">{v_str}</td></tr>'
        )
    result_text = (block.get("result") or "").strip()
    if result_text:
        rows.append(
            f'<tr><td style="color:{muted}; padding-right:10px; vertical-align:top;">result</td>'
            f'<td style="color:{text};">{_html_mod.escape(result_text[:400])}</td></tr>'
        )
    if not rows:
        return ""
    return '<table border="1" cellspacing="0" cellpadding="4" width="100%">' + "".join(rows) + "</table>"


def _truncate_val(v: Any, max_len: int = 80) -> str:
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    s = s.replace("\n", "\\n")
    return s[:max_len] + "..." if len(s) > max_len else s


class ToolCardWidget(QFrame):
    """工具调用卡：头部(名称+状态徽章+chevron)，展开显示 args/result。"""

    CHEVRON_DOWN = "▾"
    CHEVRON_RIGHT = "▸"

    def __init__(self, block: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._expanded = False
        self._anim: Optional[QPropertyAnimation] = None
        self._block = block
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(0)

        header = QPushButton(self)
        header.setCursor(Qt.PointingHandCursor)
        header.setStyleSheet(
            f"QPushButton {{ text-align:left; background:rgba(127,127,127,0.05);"
            f" border:none; border-radius:6px; padding:5px 10px; {get_font_family_css()} }}"
            f"QPushButton:hover {{ background:rgba(127,127,127,0.10); }}"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(2, 0, 6, 0)
        gear = QLabel("⚙", header)
        self._name_label = QLabel(block.get("cn_name") or block.get("name") or "工具调用", header)
        # 标题色对齐 WebEngine 版 title_color=#FFA500
        self._name_label.setStyleSheet(
            f"{get_font_family_css()} color:{_LANG_COLOR};"
            f" font-size:{_CODE_FONT_SIZE}px; font-weight:600; background:transparent;"
        )
        self._status_label = QLabel("", header)
        self._status_label.setStyleSheet(f"font-size:{scale_font_size(11)}px; background:transparent;")
        # 差异统计胶囊（+N/-N，对齐 WebEngine 版 tool-diff-stats）
        self._diff_stats_label = QLabel("", header)
        self._diff_stats_label.setStyleSheet(
            f"QLabel {{ font-size:{scale_font_size(11)}px; font-weight:600;"
            f" background:rgba(127,127,127,0.10); border-radius:8px; padding:1px 7px; }}"
        )
        self._chevron = QLabel(self.CHEVRON_RIGHT, header)
        self._chevron.setStyleSheet(
            f"color:{Colors.ASSISTANT_CARD_MUTED}; font-size:{scale_font_size(12)}px; background:transparent;"
        )
        for w in (gear, self._name_label, self._status_label):
            hl.addWidget(w)
        self._update_diff_stats()
        hl.addStretch()
        hl.addWidget(self._chevron)
        header.clicked.connect(self.toggle)
        root.addWidget(header)

        # 展开区：diff 优先 → 参数+结果表格 → args JSON → 纯 result 文本（对齐 WebEngine 版分支）
        self._body_wrap = QFrame(self)
        self._body_wrap.setMaximumHeight(0)
        bw = QVBoxLayout(self._body_wrap)
        bw.setContentsMargins(20, 4, 8, 6)
        bw.setSpacing(6)
        muted = Colors.ASSISTANT_CARD_MUTED
        diff_text = block.get("diff") or ""
        result_text = (block.get("result") or "").strip()
        table_html = "" if diff_text else _unified_table_html(block)
        if diff_text:
            bw.addWidget(DiffViewWidget(diff_text, self._body_wrap))
        elif table_html:
            table_label = QLabel(table_html, self._body_wrap)
            table_label.setWordWrap(True)
            table_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            table_label.setStyleSheet(
                f"QLabel {{ {get_font_family_css()} font-size:{scale_font_size(12)}px;"
                f" background:rgba(127,127,127,0.05); border-radius:6px; padding:4px 6px; }}"
            )
            table_label.setMaximumHeight(240)
            bw.addWidget(table_label)
        else:
            args_raw = block.get("args_raw") or ""
            if args_raw:
                args_label = QLabel(self._pretty_args(args_raw), self._body_wrap)
                args_label.setWordWrap(True)
                args_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                args_label.setStyleSheet(
                    f"QLabel {{ font-family:'Consolas',monospace; font-size:{scale_font_size(12)}px;"
                    f" color:{muted}; background:rgba(127,127,127,0.07); border-radius:6px; padding:6px 8px; }}"
                )
                args_label.setMaximumHeight(160)
                bw.addWidget(args_label)
        # 纯 result 文本：仅当未被表格/diff 覆盖时显示（避免重复）
        if result_text and not diff_text and not table_html:
            result_label = QLabel(_html_mod.escape(result_text), self._body_wrap)
            result_label.setWordWrap(True)
            result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            result_label.setStyleSheet(
                f"QLabel {{ {get_font_family_css()} font-size:{scale_font_size(12)}px;"
                f" color:{muted}; background:transparent; }}"
            )
            result_label.setMaximumHeight(220)
            bw.addWidget(result_label)
        root.addWidget(self._body_wrap)

        self._apply_status()

    def _update_diff_stats(self) -> None:
        """从 diff 文本统计 +/- 行数（排除 +++/--- 文件头），更新头部胶囊。"""
        diff_text = self._block.get("diff") or ""
        if not diff_text:
            self._diff_stats_label.hide()
            return
        added = sum(1 for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        deleted = sum(1 for ln in diff_text.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        if not added and not deleted:
            self._diff_stats_label.hide()
            return
        self._diff_stats_label.setText(
            f"<span style='color:#39d353;'>+{added}</span>"
            "<span style='color:rgba(127,127,127,0.6);'>/</span>"
            f"<span style='color:#f85149;'>-{deleted}</span>"
        )
        self._diff_stats_label.show()

    @staticmethod
    def _pretty_args(raw: str) -> str:
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except Exception:
            return raw

    def _apply_status(self) -> None:
        b = self._block
        if not b.get("closed"):
            self._status_label.setText("<span style='color:#f59e0b;font-weight:600;'>⟳ 运行中</span>")
            if not self._expanded:
                self.toggle()
        elif b.get("success"):
            self._status_label.setText("<span style='color:#3fb950;font-weight:700;'>✓</span>")
        else:
            self._status_label.setText("<span style='color:#ff7b72;font-weight:700;'>✗ 失败</span>")

    def set_streaming(self) -> None:
        self.apply_streaming(True)

    def apply_streaming(self, active: bool) -> None:
        """校正运行态：未闭合→运行中；闭合→按 success 显示徽章（幂等）。"""
        b = self._block
        if active and not b.get("closed"):
            self._status_label.setText("<span style='color:#f59e0b;font-weight:600;'>⟳ 运行中</span>")
            if not self._expanded:
                self.toggle()
        else:
            self._status_label.setText(
                "<span style='color:#3fb950;font-weight:700;'>✓</span>"
                if b.get("success")
                else "<span style='color:#ff7b72;font-weight:700;'>✗ 失败</span>"
            )

    def toggle(self) -> None:
        target = self._body_wrap.sizeHint().height() if not self._expanded else 0
        self._expanded = not self._expanded
        self._chevron.setText(self.CHEVRON_DOWN if self._expanded else self.CHEVRON_RIGHT)
        self._anim = QPropertyAnimation(self._body_wrap, b"maximumHeight", self)
        self._anim.setDuration(180)
        self._anim.setStartValue(self._body_wrap.maximumHeight())
        self._anim.setEndValue(max(target, 1))
        self._anim.finished.connect(self._on_anim_done)
        self._anim.start()

    def _on_anim_done(self) -> None:
        if self._expanded:
            self._body_wrap.setMaximumHeight(16777215)


# ── 「工具与思考」分区 + 任务列表面板 ─────────────────────────────
class _SeparatorRow(QFrame):
    """可点击折叠的分隔条（对照 WebEngine 版 tool-separator/todo-separator）。"""

    clicked = pyqtSignal()

    def __init__(self, text: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(6)
        self._chevron = QLabel("▾", self)
        label = QLabel(text, self)
        label.setStyleSheet(
            f"{get_font_family_css()} color:{Colors.ASSISTANT_CARD_MUTED};"
            f" font-size:{scale_font_size(12)}px; font-weight:600; background:transparent;"
        )
        self._extra = QLabel("", self)
        self._extra.setStyleSheet(
            f"{get_font_family_css()} color:{Colors.ASSISTANT_CARD_MUTED};"
            f" font-size:{scale_font_size(11)}px; background:transparent;"
        )
        for w in (self._chevron, label, self._extra):
            lay.addWidget(w)
        lay.addStretch()
        self.setStyleSheet(
            "_SeparatorRow { background:rgba(127,127,127,0.05); border-radius:6px; padding:1px 6px; }"
            "_SeparatorRow:hover { background:rgba(127,127,127,0.10); }"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)

    def set_collapsed(self, collapsed: bool) -> None:
        self._chevron.setText("▸" if collapsed else "▾")

    def set_extra(self, text: str) -> None:
        self._extra.setText(text)


def reconcile_widgets(
    layout: QVBoxLayout,
    widgets: List[QWidget],
    keys: List[str],
    items: List[Tuple[str, Dict[str, Any]]],
    builder,
    streaming: bool,
) -> Tuple[List[QWidget], List[str]]:
    """差量对齐：只重建分歧点之后的控件。返回新的 (widgets, keys)。"""
    new_keys = [k for k, _ in items]
    common = 0
    while common < len(new_keys) and common < len(keys) and new_keys[common] == keys[common]:
        common += 1
    while len(widgets) > common:
        w = widgets.pop()
        layout.removeWidget(w)
        w.deleteLater()
    del keys[common:]
    for _, b in items[common:]:
        w = builder(b)
        widgets.append(w)
        keys.append(_block_key(b))
        layout.addWidget(w)
    # 统一校正流式态标记：复用控件的内容指纹不变但闭合状态可能已翻转
    # （think/tool 在流式期间闭合且内容不再变化时，需原地恢复正常态）
    if streaming:
        for i, (_, b) in enumerate(items[: len(widgets)]):
            w = widgets[i]
            if isinstance(w, ThinkCard):
                w.apply_streaming(not b.get("completed", True))
            elif isinstance(w, ToolCardWidget):
                w.apply_streaming(not b.get("closed", True))
    return widgets, keys


class ToolSectionWidget(QWidget):
    """「⚙ 工具与思考」折叠分区：think/tool 卡统一收纳，分隔条可整体折叠。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._collapsed = False
        self._anim: Optional[QPropertyAnimation] = None
        self._keys: List[str] = []
        self._widgets: List[QWidget] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(2)
        self._separator = _SeparatorRow("⚙ 工具与思考", self)
        self._separator.clicked.connect(self.toggle)
        root.addWidget(self._separator)

        self._content_wrap = QFrame(self)
        cv = QVBoxLayout(self._content_wrap)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(2)
        self._cards_layout = QVBoxLayout()
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(2)
        cv.addLayout(self._cards_layout)
        # 任务列表面板固定在卡片之后（对齐 WebEngine 版 DOM：#tool-content → #todo-panel）
        self._panel_holder = QVBoxLayout()
        self._panel_holder.setContentsMargins(0, 0, 0, 0)
        cv.addLayout(self._panel_holder)
        root.addWidget(self._content_wrap)
        self.hide()

    def reconcile(self, items: List[Tuple[str, Dict[str, Any]]], streaming: bool) -> None:
        self._widgets, self._keys = reconcile_widgets(
            self._cards_layout, self._widgets, self._keys, items, self._build, streaming
        )
        self.setVisible(bool(items))

    @staticmethod
    def _build(b: Dict[str, Any]) -> QWidget:
        if b["type"] == "think":
            return ThinkCard(b["content"], b["completed"])
        return ToolCardWidget(b)

    def attach_todo_panel(self, panel: QWidget) -> None:
        self._panel_holder.addWidget(panel)

    def toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._separator.set_collapsed(self._collapsed)
        self._anim = QPropertyAnimation(self._content_wrap, b"maximumHeight", self)
        self._anim.setDuration(180)
        self._anim.setStartValue(self._content_wrap.maximumHeight())
        target = 0 if self._collapsed else max(self._content_wrap.sizeHint().height(), 1)
        self._anim.setEndValue(target)
        self._anim.finished.connect(self._on_done)
        self._anim.start()

    def _on_done(self) -> None:
        if not self._collapsed:
            self._content_wrap.setMaximumHeight(16777215)


class TodoPanel(QWidget):
    """任务列表面板：📋 分隔条 + 进度 + 状态条目（对齐 WebEngine 版 .todo-item 视觉）。"""

    _PRI_COLORS = {"high": "#ef4444", "medium": "#f59e0b", "low": "#3b82f6"}

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(2)
        self._separator = _SeparatorRow("📋 任务列表", self)
        self._separator.clicked.connect(self._toggle)
        root.addWidget(self._separator)
        self._list_wrap = QFrame(self)
        lw = QVBoxLayout(self._list_wrap)
        lw.setContentsMargins(6, 2, 6, 2)
        lw.setSpacing(1)
        self._list_layout = QVBoxLayout()
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(1)
        lw.addLayout(self._list_layout)
        root.addWidget(self._list_wrap)
        self._collapsed = False
        self.hide()

    def update_todos(self, todos: List[Dict[str, Any]]) -> None:
        todos = list(todos or [])
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        done = 0
        for t in todos:
            status = (t.get("status") or "pending") if isinstance(t, dict) else "pending"
            content = (t.get("content") or "") if isinstance(t, dict) else str(t)
            priority = ((t.get("priority") or "medium") if isinstance(t, dict) else "medium") or "medium"
            if status == "completed":
                done += 1
            self._list_layout.addWidget(QLabel(self._render_item(status, content, priority)))
        total = len(todos)
        self._separator.set_extra(f"{done}/{total} 已完成" if total else "")
        self.setVisible(bool(todos))

    def _render_item(self, status: str, content: str, priority: str) -> str:
        esc = _html_mod.escape(content)
        base_css = f"{get_font_family_css()} font-size:{scale_font_size(13)}px; background:transparent;"
        if status == "completed":
            return (
                f'<div style="{base_css}"><span style="color:#3fb950;font-weight:700;">✓</span> '
                f'<span style="color:{Colors.ASSISTANT_CARD_MUTED};text-decoration:line-through;">{esc}</span></div>'
            )
        if status == "in_progress":
            warm = "#d97706" if _is_light_theme() else "#f59e0b"
            return (
                f'<div style="{base_css}"><span style="color:{warm};">⟳</span> <b style="color:{warm};">{esc}</b></div>'
            )
        pri = self._PRI_COLORS.get(priority, self._PRI_COLORS["medium"])
        return f'<div style="{base_css}"><span style="color:{pri};">○</span> <span style="color:{Colors.ASSISTANT_CARD_TEXT};">{esc}</span></div>'

    def _toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._separator.set_collapsed(self._collapsed)
        self._list_wrap.setVisible(not self._collapsed)


# ── 主渲染器 ──────────────────────────────────────────────────────
class _NullPage:
    """CodeWebViewer.page() 的 no-op 替身：吞掉所有 JS 调用（灰度兼容层）。"""

    def runJavaScript(self, *args, **kwargs):  # noqa: N802
        pass

    def toHtml(self, *args, **kwargs):  # noqa: N802
        return ""


class MarkdownBlockViewer(QWidget):
    """纯 Qt 块级消息正文渲染器（CodeWebViewer 替换原型）。

    对外接口与 CodeWebViewer 对齐：append_chunk / finish_streaming /
    refresh_theme / get_plain_text / contentHeightChanged；
    任务列表：update_todo_list(todos)（签名同 MessageCard.update_todo_list）。
    """

    contentHeightChanged = pyqtSignal(int)
    saveFileRequested = pyqtSignal(str, str)  # (code, lang) — 兼容 MessageCard 工厂连接

    def __init__(self, parent: Optional[QWidget] = None, light: bool = False):
        super().__init__(parent)
        self._light = light
        self._md = ""
        self._streaming = True
        self._keys: List[str] = []
        self._widgets: List[QWidget] = []
        # ── CodeWebViewer 兼容属性（MessageCard 动态读写） ──
        self._is_js_ready = True  # _push_todo_list 判断：Qt 渲染器始终"就绪"
        self._tool_compact_mode = True  # 简洁模式：工具卡默认折叠
        self._needs_full_render = True
        self._stable_html = ""
        self._stable_md_len = 0
        self._tool_md_cache: Dict[str, str] = {}  # MessageCard 直接 .clear()；Qt 渲染不消费
        self._tool_target_id = "tool-content"  # 非编辑工具的分区目标（对齐 WebEngine DOM id）
        self._thinking_finalized = False
        self._think_text_streaming_started = False
        self._reasoning_streaming_started = False
        self._vbox = QVBoxLayout(self)
        self._vbox.setContentsMargins(4, 4, 8, 4)
        self._vbox.setSpacing(2)
        Colors.refresh()

        # 「工具与思考」区在正文之上（对齐 WebEngine 版 DOM 顺序），
        # todo panel 挂在其内部（对齐 #todo-panel 位于 #tool-section 内）
        self._tool_section = ToolSectionWidget(self)
        self._todo_panel = TodoPanel(self)
        self._tool_section.attach_todo_panel(self._todo_panel)
        self._vbox.addWidget(self._tool_section)

    # ── 公开接口 ──
    def set_markdown(self, text: str) -> None:
        self._md = text or ""
        self._reconcile(force=True)

    def append_chunk(self, text: str) -> None:
        if not text:
            return
        self._md += text
        self._reconcile()

    def finish_streaming(self, keep_dock: bool = False) -> None:
        self._streaming = False
        self._reconcile(force=True)

    def get_plain_text(self) -> str:
        return self._md

    def update_todo_list(self, todos: List[Dict[str, Any]]) -> None:
        """更新任务列表（签名同 MessageCard.update_todo_list）。空列表隐藏。"""
        self._todo_panel.update_todos(list(todos or []))

    def refresh_theme(self) -> None:
        Colors.refresh()
        self._reconcile(force=True)

    def cleanup(self) -> None:  # 兼容 MessageCard 调用签名
        pass

    # ── MessageCard 灰度兼容层（duck-typing CodeWebViewer 调用面） ──
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 高度自治：向 MessageCard 报告自身高度（父容器滚底依赖 heightChanged 链路）
        self.contentHeightChanged.emit(self.height())

    def page(self) -> "_NullPage":
        """兼容 `viewer.page().runJavaScript(...)` 调用点：全部 no-op。

        Qt 渲染器无 JS 层；工具流式注入等 DOM 操作依赖 markdown 全量
        reconcile（`<tool>` 块解析）达成同等效果。
        """
        return _NullPage()

    def _install_dialog_filter(self) -> None:
        """no-op：对话框 HWND 穿透防护是 WebEngine 原生子窗口专属问题，
        Qt 控件无原生 HWND，无需隐藏/恢复逻辑（ui_helpers 等外部调用点兼容）。"""

    def _refresh_viewer_font(self) -> None:
        """no-op：Qt 控件字体由 QSS 统一管理（refresh_theme 时自动生效）。"""

    def _sync_streaming_dock(self, active: bool) -> None:
        """no-op：工具区「坞态沉底」是 WebEngine DOM 分区状态机，Qt 版由原生 layout 天然排布。"""

    def _copy_to_clipboard(self, copy_selection: bool = False) -> None:
        """兼容导出路径：复制全文 markdown（Qt 渲染器无选区概念，忽略 copy_selection）。"""
        QApplication.clipboard().setText(self._md)

    @property
    def _markdown_text(self) -> str:
        return self._md

    @_markdown_text.setter
    def _markdown_text(self, value: str) -> None:
        self.set_markdown(value or "")

    def _schedule_render(self, immediate: bool = False) -> None:
        """兼容 MessageCard 渲染调度：消费懒回调后全量重解析。"""
        cb = getattr(self, "_lazy_markdown_cb", None)
        if cb is not None:
            self._lazy_markdown_cb = None
            self.set_markdown(cb())
        else:
            self._reconcile(force=immediate)

    def _perform_update(self) -> None:
        """兼容 set_html_direct：按当前 markdown 全量渲染。"""
        self.set_markdown(self._md)

    def _append_text_incremental(self, text: str) -> None:
        """兼容 CodeWebViewer 流式差量注入：Qt 渲染器无 DOM 注入层，
        用 80ms 节流的全量刷新代替（key 指纹 reconcile 只重建尾部变化块）。"""
        if not hasattr(self, "_incr_timer"):
            self._incr_timer = QTimer(self)
            self._incr_timer.setSingleShot(True)
            self._incr_timer.setInterval(80)
            self._incr_timer.timeout.connect(self._do_incremental_render)
        if not self._incr_timer.isActive():
            self._incr_timer.start()

    def _do_incremental_render(self) -> None:
        cb = getattr(self, "_lazy_markdown_cb", None)
        if cb is not None:
            self._lazy_markdown_cb = None
            self.set_markdown(cb())

    @staticmethod
    def _has_reached_clean_boundary(md_text: str) -> bool:
        """兼容 MessageCard.append_text：自然边界检测（简化复刻 CodeWebViewer 同名逻辑）。"""
        if not md_text:
            return False
        if md_text.endswith("\n\n"):
            return True
        stripped = md_text.rstrip()
        return stripped.endswith("</think>") or stripped.endswith("```")

    @staticmethod
    def _has_reached_soft_boundary(md_text: str) -> bool:
        """兼容 MessageCard.append_text：句号类软边界检测。"""
        stripped = (md_text or "").rstrip()
        return bool(stripped) and stripped[-1] in "。！？!?.；;"

    # ── 内部 ──
    def _reconcile(self, force: bool = False) -> None:
        blocks = parse_blocks(self._md)
        # 分区规则对齐 WebEngine 版：think + 非编辑工具进「工具与思考」折叠区；
        # 编辑类工具结果始终展示在正文之中（按文档位置）
        side_items = [
            (_block_key(b), b) for b in blocks if b["type"] == "think" or (b["type"] == "tool" and not b.get("is_edit"))
        ]
        body_items = [
            (_block_key(b), b)
            for b in blocks
            if b["type"] in ("html", "code") or (b["type"] == "tool" and b.get("is_edit"))
        ]
        self._widgets, self._keys = reconcile_widgets(
            self._vbox, self._widgets, self._keys, body_items, self._build_block_widget, self._streaming
        )
        self._tool_section.reconcile(side_items, self._streaming)

    @staticmethod
    def _build_block_widget(b: Dict[str, Any]) -> QWidget:
        if b["type"] == "code":
            return CodeBlockWidget(b["code"], b["lang"])
        if b["type"] == "tool":
            return ToolCardWidget(b)  # 编辑类工具：结果展示在正文之中
        return RichTextLabel(b["html"])
