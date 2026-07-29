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
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime
from functools import lru_cache
from html import escape, unescape
from typing import Any, Dict, List, Optional

import orjson as json
import sip
from loguru import logger
from markdown import Markdown
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from PyQt5.QtCore import (
    QEasingCurve,
    QPointF,
    Qt,
    QTimer,
    QTimerEvent,
    QUrl,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineSettings, QWebEngineView
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
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
from app.core.webengine_profile import get_shared_web_profile
from app.utils.design_tokens import (
    Colors,
    _get_global_font,
    current_theme,
    fade_in_widget,
    font_size_css,
    get_unified_scrollbar_style,
    scale_font_size,
)
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.render_helpers import (
    _format_natural_preview,
    _get_tool_cn_name,
    _get_tool_icon,
    _get_tool_icon_html,
    _get_tool_icon_name,
    render_tool_block,
)
from app.widgets.simple_hover_tooltip import install_hover_tooltip

# ======== Markdown 实例 ========
_md_instance = None
ACTION_COLOR_MAP = {
    "ask": "#FF6347",
}
DEFAULT_COLOR = "#888888"

# ======== 预编译的正则表达式（提升到模块级别，避免重复编译）=======
_CODE_BLOCK_PATTERN = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_CODE_BLOCK_WITH_LANG_PATTERN = re.compile(r"<pre><code(?:\s+class=\"([^\"]*)\")?>(.*?)</code></pre>", re.DOTALL)
_CONTEXT_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((ask)(?:\|([^)]*))?\)")
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
# 只匹配实际字段名，避免日志内容中的“状态: running”等屏蔽结果
_NEXT_FIELD_PATTERN = re.compile(r"\n(?:success|tool_call_id|diff|echarts):")
# 性能优化：正则提取后备方案使用的预编译模式
_EXTRACT_KEY_VALUE_PATTERN = re.compile(r'"([^"\\]+)"\s*:\s*"([^"]*)"', re.DOTALL)

# ===== Pygments lexer/formatter 缓存（避免每个代码块每周期重建） =====
_LEXER_CACHE: dict = {}
_TEXT_LEXER = TextLexer()
# formatter 含动态字号，缓存当前字号对应的实例
_FORMATTER_CACHE: dict = {"font_size": None, "formatter": None}

# ===== 性能缓存：图标前缀和字号（避免每块代码都查主题和计算字号） =====
_ICON_PREFIX_CACHE: str = "qrc:/icons"
_CODE_FONT_SIZE: int = scale_font_size(13)


def _update_icon_prefix():
    """主题切换时更新图标前缀缓存"""
    global _ICON_PREFIX_CACHE
    try:
        from app.utils.theme_manager import theme_manager

        _ICON_PREFIX_CACHE = "qrc:/icons_light" if theme_manager.is_light_theme() else "qrc:/icons"
    except Exception:
        _ICON_PREFIX_CACHE = "qrc:/icons"


# HTML 实体解码函数（str.maketrans 只能做单字符→单字符，无法解码 &quot; 等多字符实体）
_unescape_html = unescape  # 别名，保持语义清晰


def _get_lexer_cached(lang: str):
    """按语言名缓存 lexer 实例（lexer 构造开销大，含完整词法分析器初始化）"""
    if not lang:
        return _TEXT_LEXER
    lex = _LEXER_CACHE.get(lang)
    if lex is None:
        try:
            lex = get_lexer_by_name(lang, stripall=False)
        except Exception:
            lex = _TEXT_LEXER
        _LEXER_CACHE[lang] = lex
    return lex


# Pygments 高亮风格切换（深色→dracula，浅色→friendly）
_current_pygments_style = "dracula"


def set_pygments_style(style_name: str):
    """设置 Pygments 高亮风格并清除缓存"""
    global _current_pygments_style
    if style_name != _current_pygments_style:
        _current_pygments_style = style_name
        _FORMATTER_CACHE["font_size"] = None  # 强制重建


def _get_formatter_cached():
    """HtmlFormatter 单例，字号或风格变化时重建"""
    fs = scale_font_size(13)
    style = _current_pygments_style
    cache_key = (fs, style)
    if _FORMATTER_CACHE.get("cache_key") != cache_key:
        # 浅色风格用深色默认文字
        pre_color = "#1a1a1a" if style != "dracula" else "#D4D4D4"
        _FORMATTER_CACHE["cache_key"] = cache_key
        _FORMATTER_CACHE["font_size"] = fs
        _FORMATTER_CACHE["formatter"] = HtmlFormatter(
            style=style,
            linenos=False,
            noclasses=True,
            cssclass="code-block",
            prestyles=f"margin:0; padding:0; background:transparent; font-family: Consolas, monospace; font-size:{fs}px; color:{pre_color};",
        )
    return _FORMATTER_CACHE["formatter"]


# ======== 滚动行为常量 ========
SCROLL_BOUNDARY_TOLERANCE = 5.0  # 滚动边界判定容差(px)，用于判断是否到达顶部/底部
AUTO_SCROLL_THRESHOLD = 1000  # "接近底部"判定阈值(px)，用户在此范围内视为"在底部"

# 编辑类工具/子智能体/提问类工具：无论简洁模式与否，这些工具的结果始终展示在正文中
# 子智能体和提问工具（subagent_para/subagent_dag/question）涉及 AI 与用户的直接交互，
# 留在正文中比收到工具区更符合直觉，体验更连贯。
_EDIT_TOOLS = frozenset({"write", "edit", "multi_edit", "subagent_para", "subagent_dag", "question"})
# =============================

# ======== 欢迎卡片欢迎语（已退役：欢迎卡片不再显示 tips，已迁移至输入框 placeholder 轮播）========
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
        if _LINK_DETECTION_PATTERN.search(code_content) and lang_part not in ("python"):
            return code_content
        else:
            return f"```{lang_part}\n{code_content}```" if lang_part else f"```\n{code_content}```"

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
    _icon_prefix = _ICON_PREFIX_CACHE
    _font_size = _CODE_FONT_SIZE

    def replacer(match):
        lang = (match.group(1) or "").replace("language-", "").strip()
        code_content_raw = match.group(2) or ""

        # ===== ECharts 代码块：渲染为交互式图表 =====
        if lang == "echarts":
            try:
                # 解码 HTML 实体（&quot; → " 等），确保 JSON 可解析
                json_text = _unescape_html(code_content_raw)
                # 验证 JSON 合法性（json.loads 内部会解析，无需提前 decode）
                # base64 编码防止 HTML 属性转义问题
                b64_json = base64.b64encode(json_text.encode("utf-8")).decode("ascii")
                chart_id = "echart-" + hashlib.sha1(json_text.encode("utf-8")).hexdigest()[:12]
                return f'''
                <div id="{chart_id}" class="echarts-container" data-echarts-json="{b64_json}" style="width: 100%; height: 400px; margin: 12px 0; border-radius: 10px; overflow: hidden;"></div>
                '''
            except Exception:
                # JSON 解析失败，降级为普通代码块
                pass

        # --- 普通代码块处理 ---
        try:
            copy_text = _unescape_html(code_content_raw)
        except Exception:
            copy_text = code_content_raw

        b64_copy = base64.b64encode(copy_text.encode("utf-8")).decode("ascii")

        lines = copy_text.splitlines() or [""]
        line_count = len(lines)

        # 高亮代码（获取 <pre> 内部 HTML）
        try:
            lexer = _get_lexer_cached(lang)
            formatter = _get_formatter_cached()
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
            font-size: {_font_size}px;
        ">
            <!-- 顶部工具栏区域 -->
            <div style="
                display: flex; justify-content: space-between; align-items: center;
                padding: 6px 10px; height: 30px; background: rgba(255, 255, 255, 0.03);
                border-bottom: 1px solid var(--code-border, rgba(45, 45, 57, 0.5)); border-radius: 10px 10px 0 0;
            ">
                {f'<span style="color: #FFA500; font-size: {_font_size}px; font-weight: bold;">{lang}</span>' if lang else '<span style="color: #888;">Plain Text</span>'}
                <div style="display: flex; gap: 12px; align-items: center; padding-right: 4px;">
                    <button type="button" data-action="save_file" data-lang="{lang}" data-copy="{b64_copy}" class="code-btn" data-tooltip="保存本地文件" style="width: 30px; height: 30px; background: transparent; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0; border-radius: 6px;">
                        <img src="{_icon_prefix}/导入.svg" style="width:22px; height:22px; pointer-events: none;" />
                    </button>
                    <button type="button" data-action="copy" data-copy="{b64_copy}" class="code-btn" data-tooltip="复制代码" style="width: 30px; height: 30px; background: transparent; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0; border-radius: 6px;">
                        <img src="{_icon_prefix}/复制.svg" style="width:22px; height:22px; pointer-events: none;" />
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


def _get_think_icon_html(size: int = 18) -> str:
    """生成思考过程图标的 HTML <img> 标签（主题感知，自动适配深色/浅色模式）"""
    try:
        from app.utils.theme_manager import theme_manager

        prefix = "qrc:/icons_light" if theme_manager.is_light_theme() else "qrc:/icons"
    except Exception:
        prefix = "qrc:/icons"
    style = f"width:{size}px;height:{size}px;vertical-align:middle;pointer-events:none;"
    return f'<img src="{prefix}/思考过程.svg" style="{style}" />'


def _get_think_block_styles() -> str:
    """获取思考块的全局字体样式"""
    return f"{get_font_family_css()} font-size: {scale_font_size(13)}px;"


def _get_think_preview(content: str, max_length: int = 160) -> str:
    """智能生成思考内容折叠框的预览文本

    新策略（结论优先）：
      1. 检测结论标记 → 优先展示结论后的内容
      2. 无结论时三段式采样：
         - 首句（跳过过短 <10 字的）
         - 中间代表性句（~40% 位置）
         - 尾句（往往含总结性内容）
      3. 保证最少 40 字，不够时向后扩展
      4. 未到文本结尾时追加省略号
    """
    if not content:
        return ""

    text = content.strip()
    if not text:
        return ""

    def _is_full(preview_len: int) -> bool:
        """预览长度是否已覆盖完整内容（忽略空白、换行差异）"""
        norm_text = len(text.replace(" ", "").replace("\n", ""))
        return preview_len >= norm_text

    # ── 策略1: 优先检测结论，展示结论内容 ──
    for marker in _CONCLUSION_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            conclusion_text = text[idx:].replace("\n", " ").strip()
            if len(conclusion_text) <= max_length:
                return conclusion_text if _is_full(len(conclusion_text)) else conclusion_text + "..."
            # 结论太长，截取到 max_length
            for i in range(min(max_length, len(conclusion_text)), 0, -1):
                if conclusion_text[i - 1] in "。！？.!?；;":
                    return conclusion_text[:i]
            return conclusion_text[:max_length] + "..."

    # ── 策略2: 三段式采样 ──
    # 展平文本
    flat = text.replace("\n", " ")

    # ── 检测是否英文为主（中文占比 < 30%） ──
    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    is_english_heavy = cjk_count < len(text) * 0.3

    if is_english_heavy:
        # 英文为主的策略：按句尾标点+空格+大写字母分句
        # 避免 1. / 2. / U.S. / v2.5 等被误判为句子边界
        raw_sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat)
        # 清理空串和过短片段（纯粹的数字编号如 "1." 直接丢弃）
        raw_sentences = [s.strip() for s in raw_sentences if s.strip() and len(s.strip()) >= 4]
    else:
        raw_sentences: List[str] = []
        current = ""
        for ch in flat:
            current += ch
            if ch in "。！？.!?；;":
                s = current.strip()
                if s:
                    raw_sentences.append(s)
                current = ""
        if current.strip():
            raw_sentences.append(current.strip())

    # 合并连续短句（<8 字）到前一句或后一句
    sentences: List[str] = []
    buf = ""
    for s in raw_sentences:
        if not s:
            continue
        if len(s) < 8:
            buf += s
        else:
            if buf:
                sentences.append(buf + s)
                buf = ""
            else:
                sentences.append(s)
    if buf:
        if sentences:
            sentences[-1] += buf
        else:
            sentences.append(buf)

    if not sentences:
        # 没有有效句子，回退到简单截断
        if len(flat) <= max_length:
            return flat
        for i in range(max_length, 0, -1):
            if flat[i - 1] in " ，,、；;：:.":
                return flat[:i].rstrip(" ，,、.") + "..."
        return flat[:max_length] + "..."

    # 选首句 + 中间句(~40%) + 尾句（相邻句子直接拼接，不加 ...）
    selected_indices: List[int] = []
    n = len(sentences)

    # 首句
    selected_indices.append(0)

    # 中间句（40% 位置，确保不与首尾重复）
    mid_idx = max(1, int(n * 0.4))
    if mid_idx < n - 1:  # 不在最后一句话
        selected_indices.append(mid_idx)

    # 尾句
    last_idx = n - 1
    if n > 1 and last_idx not in selected_indices:
        selected_indices.append(last_idx)

    # 按原始顺序排序
    selected_indices.sort()

    # 构建预览：相邻句子直接拼接，非相邻用 ...
    preview_groups: List[str] = []
    current_group = sentences[selected_indices[0]]
    for i in range(1, len(selected_indices)):
        idx = selected_indices[i]
        prev_idx = selected_indices[i - 1]
        if idx == prev_idx + 1:
            # 与上一个句子相邻，直接拼接
            current_group += sentences[idx]
        else:
            preview_groups.append(current_group)
            current_group = sentences[idx]
    preview_groups.append(current_group)

    preview = " ... ".join(preview_groups)

    # ── 保证最少 40 字 ──
    if len(preview) < 40:
        # 只有一句时直接展示全部（或截断到 max_length）
        if n == 1:
            full = sentences[0]
            if len(full) <= max_length:
                return full if _is_full(len(full)) else full + "..."
            else:
                preview = full[:max_length] + "..."
        else:
            # 向后扩展：直接取连续句子直到 ≥40 字（不插入 ...）
            extended = ""
            for s in sentences:
                if len(extended) >= 40:
                    break
                extended += s
            preview = extended

    # 截断到 max_length
    if len(preview) > max_length:
        for i in range(max_length, 0, -1):
            if preview[i - 1] in "。！？.!?；;":
                preview = preview[:i]
                break
        else:
            preview = preview[:max_length]

    if _is_full(len(preview)):
        return preview
    return preview + "..."


# ── 思考折叠框标签分类系统（加权） ──
# 标签定义：tag=显示名, priority=平局优先级, cn=中文模式, en=英文模式
# 权重规则：短语(≥4中字/含空格)权值3, 3字权值2, 常见普通词权值0.5, 其他1
_THINK_TAGS = [
    {
        "tag": "分析",
        "priority": 3,
        "cn": (
            "问题出在",
            "原因在于",
            "关键问题",
            "核心问题",
            "需要分析",
            "需要理解",
            "需要考虑",
            "问题",
            "分析",
            "理解",
            "排查",
        ),
        "en": ("problem", "issue", "analyze", "understand", "root cause", "what went wrong", "why"),
    },
    {
        "tag": "设计",
        "priority": 2,
        "cn": ("设计方案", "实现方案", "架构设计", "方案", "设计", "架构", "策略", "规划"),
        "en": ("solution", "design", "approach", "strategy", "architecture", "plan to", "propose to"),
    },
    {
        "tag": "探索",
        "priority": 2,
        "cn": (
            "探索",
            "研究",
            "了解",
            "学习",
            "查阅",
            "参考",
            "知识",
            "概念",
            "原理",
            "定义",
            "资料",
            "文献",
            "查询",
            "搜索",
            "调查",
        ),
        "en": (
            "explore",
            "research",
            "learn",
            "study",
            "concept",
            "definition",
            "principle",
            "reference",
            "knowledge",
            "investigate",
        ),
    },
    {
        "tag": "验证",
        "priority": 2,
        "cn": (
            "验证",
            "测试",
            "检测",
            "调试",
            "断言",
            "校验",
            "用例",
            "覆盖",
            "回归",
            "边界条件",
            "测试用例",
            "单元测试",
            "集成测试",
        ),
        "en": (
            "test",
            "verify",
            "validate",
            "debug",
            "assert",
            "coverage",
            "regression",
            "unit test",
            "integration test",
            "test case",
        ),
    },
    {
        "tag": "版本",
        "priority": 3,
        "cn": (
            "版本控制",
            "仓库",
            "回滚",
            "PR",
            "rebase",
            "stash",
            "cherry-pick",
            "git bisect",
            "git blame",
            "git log",
        ),
        "en": (
            "rebase",
            "cherry-pick",
            "checkout",
            "git bisect",
            "git blame",
            "git log",
            "version control",
            "source control",
        ),
    },
    {
        "tag": "实现",
        "priority": 2,
        "cn": ("具体实现", "代码片段", "接口定义", "类型定义", "模块结构", "类设计", "方法签名", "API设计"),
        "en": (
            "implement the",
            "define the",
            "interface",
            "method signature",
            "API design",
            "class definition",
            "module structure",
        ),
    },
    {
        "tag": "修复",
        "priority": 2,
        "cn": ("错误", "异常", "报错", "修复", "崩溃", "排查错误", "错误原因", "调试日志"),
        "en": ("bug", "crash", "fix the", "broken", "stack trace", "traceback", "debugging the error"),
    },
    {
        "tag": "优化",
        "priority": 2,
        "cn": ("性能", "优化", "速度", "效率", "延迟", "瓶颈"),
        "en": ("performance", "optimize", "speed", "efficiency", "latency", "bottleneck", "slow"),
    },
    {
        "tag": "安全",
        "priority": 2,
        "cn": ("安全", "权限", "漏洞", "风险", "加密", "认证"),
        "en": ("security", "permission", "auth", "vulnerability", "encrypt", "risk", "compliance"),
    },
    {
        "tag": "重构",
        "priority": 2,
        "cn": ("重构", "重写", "清理代码", "消除重复", "简化代码", "代码整理", "提取方法", "模块拆分", "内联函数"),
        "en": ("refactor", "cleanup", "simplify", "extract method", "inline", "split into", "restructure"),
    },
    {
        "tag": "配置",
        "priority": 2,
        "cn": (
            "配置",
            "参数设置",
            "环境变量",
            "开关",
            "配置文件",
            "config",
            "设置项",
            "调整参数",
            "初始化配置",
            "dotenv",
        ),
        "en": (
            "configuration",
            "env var",
            "environment variable",
            "setting",
            "parameter",
            "config file",
            "dotenv",
            ".env",
        ),
    },
    {
        "tag": "审查",
        "priority": 2,
        "cn": ("审查", "review代码", "代码检查", "风格检查", "lint", "代码质量", "检查规范", "静态分析"),
        "en": ("code review", "lint", "code quality", "inspect", "check style", "static analysis"),
    },
]
# 结论标记（中英文，优先级最高）
_CONCLUSION_MARKERS = (
    "因此",
    "所以",
    "综上",
    "综上所述",
    "总而言之",
    "总的来说",
    "建议",
    "推荐",
    "结论是",
    "答案是",
    "总结一下",
    "也就是说",
    "最终",
    "therefore",
    "in conclusion",
    "overall",
    "the answer is",
    "the solution is",
    "i recommend",
    "i suggest",
    "so the answer",
)
# 常见高频词（权值0.5，避免误触）
_COMMON_WORDS = frozenset(
    (
        "问题",
        "分析",
        "代码",
        "方案",
        "设计",
        "安全",
        "性能",
        "实现",
        "错误",
        "优化",
        "检查",
        "考虑",
        "需要",
        "处理",
        "解决",
        "使用",
        "支持",
        "提供",
        "操作",
    )
)


def _pattern_weight(p: str, position: float = 0.5) -> float:
    """计算模式的权重：越长越具体→权重越高，结尾区加权

    Args:
        p: 匹配到的模式字符串
        position: 关键词在全文中的相对位置 (0.0=开头, 1.0=结尾)
                  结尾 30% 区域 (≥0.7) 权重 ×1.5
    """
    base = 1.0

    if " " in p:  # 多词短语（英文短语或中文带空格）
        base = 3.0
    else:
        has_cjk = any("\u4e00" <= c <= "\u9fff" for c in p)
        if has_cjk:
            if len(p) >= 4:
                base = 3.0
            elif len(p) == 3:
                base = 2.0
            elif p in _COMMON_WORDS:
                base = 0.5
        else:
            # 英文权重
            if len(p) >= 6:
                base = 2.0
            elif len(p) >= 4:
                base = 1.5

    # 位置加权：结尾 30% 区域权重 ×1.5
    if position >= 0.7:
        base *= 1.5

    return base


def _classify_think_tag(content: str) -> str:
    """对思考内容进行分类，返回预定义标签名，空=不显示

    改进要点：
    - 分析窗口扩展为前 1500 + 尾部 500（覆盖结论区）
    - 位置加权：结尾 30% 区域关键词权重 ×1.5
    - 排他性：同词命中多标签时权重减半
    - 阈值 3.0（关键词已清洗，阈值可提高）
    """
    content = content.strip()
    if not content:
        return ""

    # ── 扩展分析窗口：前 1500 + 尾部 500 ──
    head = content[:1500]
    tail = content[-500:] if len(content) > 1500 else ""
    # 合并去重：尾部可能与前部重叠
    if tail and len(content) > 1500:
        window = head + "\n" + tail
    else:
        window = head
    window_lower = window.lower()

    # ── 结论优先检测（全文中搜索） ──
    full_lower = content.lower()
    for marker in _CONCLUSION_MARKERS:
        if marker in content or marker in full_lower:
            return "结论"

    # ── 计算每个关键词在窗口中的最佳位置 ──
    def _find_position(pattern: str, text: str, text_lower: str) -> float:
        """返回关键词在文本中的相对位置 (0~1)，找不到返回 -1"""
        if any("\u4e00" <= c <= "\u9fff" for c in pattern):
            idx = text.find(pattern)
        else:
            idx = text_lower.find(pattern)
        if idx == -1:
            return -1.0
        total = max(len(text), 1)
        return idx / total

    # ── 统计所有标签命中（用于排他性计算） ──
    all_matches: Dict[str, List[tuple]] = {}  # pattern -> [(tag_index, position)]

    for ti, tag_def in enumerate(_THINK_TAGS):
        for p in tag_def["cn"]:
            pos = _find_position(p, window, window_lower)
            if pos >= 0:
                if p not in all_matches:
                    all_matches[p] = []
                all_matches[p].append((ti, pos))
        for p in tag_def["en"]:
            pos = _find_position(p, window, window_lower)
            if pos >= 0:
                if p not in all_matches:
                    all_matches[p] = []
                all_matches[p].append((ti, pos))

    # ── 计算排他性权重 ──
    def _exclusivity_multiplier(pattern: str) -> float:
        """同词被多个标签匹配时减半"""
        tags_hit = all_matches.get(pattern, [])
        unique_tags = len(set(t for t, _ in tags_hit))
        return 0.5 if unique_tags > 1 else 1.0

    # ── 标签加权计分（去重 + 排他性） ──
    best_tag = ""
    best_score = 0.0
    best_priority = -1

    for tag_def in _THINK_TAGS:
        matches_cn = [(p, _find_position(p, window, window_lower)) for p in tag_def["cn"]]
        matches_en = [(p, _find_position(p, window, window_lower)) for p in tag_def["en"]]
        matches_cn = [(p, pos) for p, pos in matches_cn if pos >= 0]
        matches_en = [(p, pos) for p, pos in matches_en if pos >= 0]
        all_hits = matches_cn + matches_en

        if not all_hits:
            continue

        # 去重：长模式优先，子串不计；每个关键词取最佳位置
        uniq: Dict[str, float] = {}
        for p, pos in sorted(all_hits, key=lambda x: len(x[0]), reverse=True):
            if not any(p in u for u in uniq):
                if p not in uniq or pos > uniq[p]:
                    uniq[p] = pos

        # 加权计分：权重 × 排他性
        score = sum(_pattern_weight(p, position=pos) * _exclusivity_multiplier(p) for p, pos in uniq.items())

        if score > best_score or (score == best_score and tag_def["priority"] > best_priority):
            best_score = score
            best_tag = tag_def["tag"]
            best_priority = tag_def["priority"]

    return best_tag if best_score >= 3.0 else ""


_THINK_SNAKE_SVG = (
    '<svg class="think-snake" width="18" height="18" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="8" fill="none" stroke="rgba(255,200,50,0.06)" stroke-width="2.5" />'
    '<circle cx="12" cy="12" r="8" fill="none" stroke="rgba(255,200,50,0.2)" stroke-width="2.5"'
    ' stroke-linecap="round" stroke-dasharray="20 30" class="think-snake-arc" />'
    '<circle cx="12" cy="12" r="8" fill="none" stroke="rgba(255,200,50,0.55)" stroke-width="2.5"'
    ' stroke-linecap="round" stroke-dasharray="12 38" class="think-snake-arc think-snake-body" />'
    '<circle cx="12" cy="12" r="8" fill="none" stroke="rgba(255,200,50,1)" stroke-width="2.5"'
    ' stroke-linecap="round" stroke-dasharray="6 44" class="think-snake-arc think-snake-head" />'
    "</svg>"
)


# 页脚 Review 按钮 SVG：放大镜 + 对勾，象征"审查"。
# 使用 currentColor 让 QPainter 在渲染时统一着色以匹配主题。
_REVIEW_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="11" cy="11" r="6.5"/>'
    '<line x1="20" y1="20" x2="15.8" y2="15.8"/>'
    '<polyline points="8.2 11.2 10.4 13.4 13.8 9.6"/>'
    "</svg>"
)


def _render_svg_pixmap(
    svg_str: str,
    size: int,
    color: str,
    dpr: float = 1.0,
) -> "QPixmap":
    """把内嵌 SVG 渲染为指定颜色的 QPixmap（用于 QLabel 显示）。

    Args:
        svg_str: 完整 SVG 字符串（内部使用 stroke="currentColor"）
        size: 逻辑像素尺寸（正方形）
        color: 应用颜色（与 _theme["accent"] 保持一致）
        dpr: devicePixelRatio（HiDPI 屏上 >1.0）。默认 1.0。

    Returns:
        已着色的 QPixmap，背景透明。物理像素 = size*dpr，已 setDevicePixelRatio(dpr)。
    """
    dpr = dpr if dpr > 0 else 1.0
    physical = max(1, int(round(size * dpr)))
    pixmap = QPixmap(physical, physical)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.transparent)
    renderer = QSvgRenderer(svg_str.encode("utf-8"))
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        renderer.render(painter)
        # 用 SourceIn 模式把 currentColor 替换为目标颜色
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(color))
    finally:
        painter.end()
    return pixmap


def _render_tool_streaming_block(
    tool_call_id: str,
    tool_name: str,
    preview: str,
    char_count: int = 0,
    completed: bool = False,
) -> str:
    """渲染工具流式调用块 HTML — 无折叠 inline 卡片。

    布局：[icon] [tool_name] [spinner] | [预览文本 (N字符)]

    与 _render_inline_tool 风格一致，无折叠/无 body/无可展开内容。
    工具执行完成后由 _append_tool_result_block 原地替换为 render_tool_block。

    Args:
        tool_call_id: 工具调用 ID
        tool_name: 原始工具名（如 read、mcp__playwright__browser_navigate）
        preview: 预览文本
        char_count: 已接收参数字符数（追加到预览文本后）
        completed: True=参数接收完成（隐藏蛇形动画），False=流式中
    """
    # MCP 工具名清理
    is_mcp = tool_name.startswith("mcp__")
    # 子智能体任务
    is_sub_agent_task = tool_name in ("task", "subagent_para", "subagent_dag")
    display_name = tool_name or ""
    if is_mcp:
        display_name = "__".join(display_name.split("__")[2:])
    if not display_name:
        display_name = "工具调用中"

    # 图标与颜色：与 render_tool_block 保持一致
    if is_mcp:
        icon_name = "websearch"
        title_color = "#00BCD4"
    elif is_sub_agent_task:
        icon_name = "设置-subagent"
        title_color = "#9C27B0"
    else:
        icon_name = _get_tool_icon(tool_name)
        title_color = "#FFA500"

    icon_html = _get_tool_icon_html(icon_name)
    cn_name = _get_tool_cn_name(tool_name) if not is_mcp else display_name

    # spinner
    spinner_html = f'<span class="tool-streaming-spinner">{_THINK_SNAKE_SVG}</span>'

    # 合并预览文本 + 字符数进度（放在同一个 span 里，JS 更新 innerHTML 时一起走）
    preview_display = escape(preview) if preview else "准备中..."
    if not completed and char_count > 0:
        preview_display += f'<span style="color: var(--text); font-size: {scale_font_size(10)}px; margin-left: 4px;">({char_count}字符)</span>'

    streaming_state = "false" if completed else "true"

    return f"""<div class="tool-block tool-streaming-block" data-tool-name="{escape(tool_name)}" data-tool-call-id="{tool_call_id}" data-streaming="{streaming_state}" style="margin: 4px 0; background: transparent; border: none; border-radius: 6px; box-shadow: none; display: flex; align-items: center; padding: 5px 10px; {get_font_family_css()}">
        <span style="display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto;">
            <span style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:0 0 auto;">
                {icon_html}
            </span>
            <span style="white-space: nowrap; flex: 0 0 auto; color: {title_color}; font-size: {scale_font_size(13)}px; font-weight: 500;">{escape(cn_name)}</span>
            {spinner_html}
        </span>
        <span class="tool-streaming-preview" style="flex: 1 1 auto; min-width: 0; text-align: left; color: var(--text-secondary); font-size: {scale_font_size(11)}px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-left: 12px;">
            {preview_display}
        </span>
    </div>"""


def _render_think_block(content: str, completed: bool = True, compact: bool = False) -> str:
    if completed:
        # ── 完成态 ──
        tag = _classify_think_tag(content)
        think_icon = _get_think_icon_html()
        bulb = f'<span class="think-bulb">{think_icon}</span>'
        status_text = f"{bulb} {escape(tag)}" if tag else bulb
        preview = _get_think_preview(content)
        font_style = _get_think_block_styles()

        # ── 简洁模式：纯文本行，不走折叠框（避免 save/restore 导致的消失→重现闪烁）──
        if compact:
            block_seed = f"{content}|1"
            block_key = "think-" + hashlib.sha1(block_seed.encode("utf-8")).hexdigest()[:12]
            preview_right = f'<span style="color: var(--text-secondary); font-weight: normal; margin-left: 8px; font-size: {scale_font_size(11)}px;">{escape(preview)}</span>'
            return f"""<div class="think-compact" data-block-key="{block_key}" style="margin: 2px 0; padding: 4px 8px; {font_style} display: flex; align-items: baseline; gap: 6px; border-radius: 4px;">
    <span style="white-space: nowrap; flex-shrink: 0;">{status_text}</span>
    {preview_right}
</div>"""

        # ── 非简洁模式：完整折叠框UI（图标标签 + 预览 + 可展开全文）──
        content_escaped = escape(_strip_code_blocks(content))
        block_seed = f"{content}|1"
        block_key = "think-" + hashlib.sha1(block_seed.encode("utf-8")).hexdigest()[:12]
        summary_right = f'<span style="color: var(--text-secondary); font-weight: normal; margin-left: 12px; font-size: {scale_font_size(11)}px;">{escape(preview)}</span>'
        body_html = f'<div class="think-content loading" style="white-space: normal; word-break: break-word; line-height: 1.6; {font_style}">{content_escaped}</div>'
        return f"""<div class="cm-collapsible think-block" data-block-key="{block_key}" data-expanded="false" style="margin: 4px 0;">
    <button type="button" class="cm-collapsible__summary think-block__summary" aria-expanded="false" style="{font_style}">
        <span style="white-space: nowrap;">{status_text}</span>
        {summary_right}
        <span class="cm-collapsible__chevron" aria-hidden="true" style="flex: 0 0 auto; margin-left: auto;"></span>
    </button>
    <div class="cm-collapsible__body">
        {body_html}
    </div>
</div>"""

    # ── 流式态：无折叠UI，显示金色圆环 + "深度思考中"文字 ──
    spinner_html = f'<span class="tool-streaming-spinner">{_THINK_SNAKE_SVG}</span>'
    return f"""<div class="think-streaming" data-streaming="true" style="margin: 4px 0; padding: 6px 10px; border: none; border-radius: 6px;">
    <span style="display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 15px;">
        {spinner_html}
        <span>深度思考中...</span>
    </span>
</div>"""


def _render_think_block_lightweight(content: str, completed: bool = True) -> str:
    """轻量级思考块渲染（用于超长思考内容）

    与 _render_think_block 的区别：
    1. 不执行代码块处理（_strip_code_blocks），直接转义
    2. 不生成 block_key hash（节省计算）
    """
    if completed:
        # ── 完成态：可折叠UI（图标标签 + 预览 + 可展开全文） ──
        tag = _classify_think_tag(content)
        think_icon = _get_think_icon_html()
        bulb = f'<span class="think-bulb">{think_icon}</span>'
        status_text = f"{bulb} {escape(tag)}" if tag else bulb
        content_escaped = escape(content)
        font_style = _get_think_block_styles()
        preview = _get_think_preview(content)
        summary_right = f'<span style="color: var(--text-secondary); font-weight: normal; margin-left: 12px; font-size: {scale_font_size(11)}px;">{escape(preview)}</span>'
        body_html = f'<div class="think-content loading" style="white-space: normal; word-break: break-word; line-height: 1.6; {font_style}">{content_escaped}</div>'
        return f"""<div class="cm-collapsible think-block" data-block-key="think-light" data-expanded="false" style="margin: 4px 0;">
    <button type="button" class="cm-collapsible__summary think-block__summary" aria-expanded="false" style="{font_style}">
        <span style="white-space: nowrap;">{status_text}</span>
        {summary_right}
        <span class="cm-collapsible__chevron" aria-hidden="true" style="flex: 0 0 auto; margin-left: auto;"></span>
    </button>
    <div class="cm-collapsible__body">
        {body_html}
    </div>
</div>"""

    # ── 流式态：无折叠UI，显示金色圆环 + "深度思考中"文字 ──
    spinner_html = f'<span class="tool-streaming-spinner">{_THINK_SNAKE_SVG}</span>'
    return f"""<div class="think-streaming" data-streaming="true" style="margin: 4px 0; padding: 6px 10px; border: none; border-radius: 6px;">
    <span style="display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 15px;">
        {spinner_html}
        <span>深度思考中...</span>
    </span>
</div>"""


def _has_unclosed_think(text: str) -> bool:
    """检测文本中是否存在未闭合的 <think> 标签（最后一个 <think> 之后无 </think>）。"""
    if not text:
        return False
    last_open = text.rfind("<think>")
    if last_open == -1:
        return False
    last_close = text.rfind("</think>", last_open)
    return last_close == -1


def _inject_think_cards(md_text: str, completed: bool = True, compact: bool = False) -> str:
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
                parts.append(_render_think_block(content, completed=True, compact=compact))
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


@lru_cache(maxsize=128)
def _render_tool_block_content(content: str, compact: bool = False) -> str:
    """
    渲染工具块内容为HTML。

    Args:
        content: 原始 tool 块标记文本
        compact: 简洁模式标志，True 则工具块默认折叠，False 默认展开

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
                    if c == "\\":
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

                if c == "{" or c == "[":
                    depth += 1
                elif c == "}" or c == "]":
                    depth -= 1
                    if depth == 0:
                        tool_args_str = content[brace_start : i + 1]
                        result_search_start = i + 1
                        break
                i += 1

            # 如果没有找到闭合（JSON 不完整），取已接收的部分
            if not tool_args_str and brace_start >= 0:
                tool_args_str = content[brace_start:]
                result_search_start = i
        else:
            line = content[args_start:].split("\n")[0]
            tool_args_str = line[args_start + 5 :].strip()
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

    # ========== 解析 diff（可选字段，仅 edit/write 工具有）==========
    diff_content = ""
    diff_start = content.find("\ndiff:")
    if diff_start != -1:
        diff_after = content[diff_start + 6 :]  # skip "\ndiff:"
        # diff 内容持续到下一个字段（\nsuccess:）或末尾
        diff_next = _NEXT_FIELD_PATTERN.search(diff_after)
        if diff_next:
            diff_content = diff_after[: diff_next.start()].strip()
        else:
            diff_content = diff_after.strip()

    # ========== 解析 echarts（可选字段，仅 subagent_dag 有）==========
    echarts_content = ""
    echarts_start = content.find("\necharts:")
    if echarts_start != -1:
        echarts_after = content[echarts_start + 9 :]
        # echarts JSON 持续到末尾或下一个字段
        echarts_next = _NEXT_FIELD_PATTERN.search(echarts_after)
        if echarts_next:
            echarts_content = echarts_after[: echarts_next.start()].strip()
        else:
            echarts_content = echarts_after.strip()
    if result_start >= 0:
        result_after = content[result_start + 7 :]  # 跳过 "result:"
        # 找到 result 内容的结束位置（下一个字段之前）
        next_field = _NEXT_FIELD_PATTERN.search(result_after)
        if next_field:
            tool_result = result_after[: next_field.start()].strip()
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
            if fixed_args_str.startswith("{") and not fixed_args_str.endswith("}"):
                fixed_args_str += "}"
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

    # 历史工具 diff 缺失时的 fallback（从参数重建）
    if not diff_content and tool_name == "edit":
        fpath = args_dict.get("file_path") or args_dict.get("path") or ""
        if fpath:
            ops = args_dict.get("operations", [])
            if ops and isinstance(ops, list):
                pseudo = [f"--- {fpath}", f"+++ {fpath}"]
                for op in ops:
                    if isinstance(op, dict):
                        t = op.get("op", "replace")
                        a = op.get("anchor", "")
                        ln = op.get("lines")
                        if t == "delete":
                            pseudo.append(f"@@ -1 +1 @@ delete at {a}")
                            pseudo.append("- <deleted>")
                        elif ln:
                            pseudo.append(f"@@ -1 +1 @@ {t} at {a}")
                            for l in ln:
                                pseudo.append(f"+{l}")
                    elif isinstance(op, str):
                        pseudo.append("@@ -1 +1 @@")
                        pseudo.append(f"+{op}")
                diff_content = "\n".join(pseudo)

    # 转义参数中的换行符（参数预览和表格不支持多行显示）
    for key in args_dict:
        if isinstance(args_dict[key], str):
            args_dict[key] = args_dict[key].replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return render_tool_block(
        tool_name,
        args_dict,
        tool_result,
        tool_success,
        collapsed=compact,
        tool_call_id=tool_call_id,
        diff=diff_content,
        echarts=echarts_content,
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
        if c == "\\":
            # 转义序列，跳过下一个字符
            i += 2
        elif c == '"':
            # 检查后面是否是真正的分隔符
            next_i = i + 1
            # 跳过空白
            while next_i < n and s[next_i] in " \t\n\r":
                next_i += 1
            if next_i < n:
                next_c = s[next_i]
                # 只有后面是这些字符才是真正结束：, } ] 或 : (key后面的值结束时)
                if next_c in ",}:]":
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
        if c in " \t\n\r":
            i += 1
            continue

        # 期待 "key"
        if c != '"':
            i += 1
            continue

        # 解析 key
        key_end = _find_string_end(json_str, i + 1)
        key = json_str[i + 1 : key_end]
        i = key_end + 1

        # 跳过空白和冒号
        while i < n and json_str[i] in " \t\n\r:":
            i += 1
        if i >= n:
            break

        c = json_str[i]

        # 解析 value
        if c == '"':
            value_end = _find_string_end(json_str, i + 1)
            value = json_str[i + 1 : value_end]
            i = value_end + 1
            # 处理转义（简化处理）
            value = value.replace('\\"', '"').replace("\\\\", "\\")
            args[key] = value
        elif c == "{":
            obj_start = i
            depth = 1
            i += 1
            while i < n and depth > 0:
                ch = json_str[i]
                if ch == '"':
                    str_end = _find_string_end(json_str, i + 1)
                    i = str_end + 1
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                i += 1
            obj_str = json_str[obj_start:i]
            try:
                args[key] = json.loads(obj_str)
            except Exception:
                args[key] = obj_str
        elif c == "[":
            arr_start = i
            depth = 1
            i += 1
            while i < n and depth > 0:
                ch = json_str[i]
                if ch == '"':
                    str_end = _find_string_end(json_str, i + 1)
                    i = str_end + 1
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                i += 1
            arr_str = json_str[arr_start:i]
            try:
                args[key] = json.loads(arr_str)
            except Exception:
                args[key] = arr_str
        elif c.isdigit() or c == "-":
            num_str = c
            i += 1
            while i < n and json_str[i].isdigit() or json_str[i] in ".eE+-":
                num_str += json_str[i]
                i += 1
            try:
                args[key] = float(num_str) if "." in num_str else int(num_str)
            except Exception:
                args[key] = num_str
        elif i + 4 <= n and json_str[i : i + 4] == "true":
            args[key] = True
            i += 4
        elif i + 5 <= n and json_str[i : i + 5] == "false":
            args[key] = False
            i += 5
        elif i + 4 <= n and json_str[i : i + 4] == "null":
            args[key] = None
            i += 4
        else:
            i += 1

        # 跳过空白和逗号
        while i < n and json_str[i] in " \t\n\r,":
            i += 1

    return args


def _find_json_bounds(content: str) -> tuple:
    """找到 JSON 对象的起始和结束位置"""
    start = content.find("{")
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
        if c == "\\":
            escape_next = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            i += 1
            continue
        if not in_string:
            if c == "{":
                depth += 1
            elif c == "}":
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
    except Exception:
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
        except Exception:
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


def _inject_tool_blocks(md_text: str, completed: bool = True, compact: bool = False) -> str:
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
            content = md_text[start_idx + len("<tool>") : end_idx]
            parts.append(_render_tool_block_content(content, compact=compact))
            i = end_idx + len("</tool>")
        else:
            parts.append(md_text[start_idx:])
            break
    return "".join(parts)


# ===== _inject_hook_blocks 预编译正则（流式时每周期调用，避免重复编译） =====
_RE_SYS_REMINDER_FULL = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_RE_SYS_REMINDER_HALF = re.compile(r"<system-reminder>")
_RE_HOOK_TAG_FULL = re.compile(r"<([a-z0-9-]+-hook)>.*?</\1>", re.DOTALL)
_RE_HOOK_TAG_HALF = re.compile(r"<[a-z0-9-]+-hook>")
_RE_HOOK_EVENT_FULL = re.compile(r'<hook\s+event="[^"]+">.*?</hook>', re.DOTALL)
_RE_HOOK_EVENT_HALF = re.compile(r'<hook\s+event="[^"]+">')


def _inject_hook_blocks(md_text: str, completed: bool = True) -> str:
    """彻底丢弃所有 hook 输出（不再渲染折叠框）。

    历史背景：早期版本会把 hook 输出渲染成 UI 折叠框，但这种内容是 LLM 上下文
    注入，不应暴露给用户。该函数现在不再渲染任何 hook 块，只做"剥壳清空"：

    1. <system-reminder>...</system-reminder> 整段丢
    2. 半截 <system-reminder> 标签丢（流式中间态防线，仅删标签本身不吞下文）
    3. <xxx-hook>...</xxx-hook> 兜底丢
    4. 半截 <xxx-hook> 标签丢（仅删标签本身不吞下文）
    5. <hook event="Xxx">...</hook> 旧格式丢
    6. 半截 <hook event=...> 标签丢（仅删标签本身不吞下文）

    注意：半截标签不匹配后续内容（仅删标签名），避免误吞用户正文中的 <system-reminder>。
    核心 bug 修复：旧版用 .* (re.DOTALL) 会从用户正文中出现的 <system-reminder> 一路吞到末尾。

    Args:
        md_text: 原始 markdown 文本
        completed: 已废弃参数，保留仅为兼容旧调用方

    Returns:
        不含任何 hook/system-reminder 内容的 markdown 文本
    """
    if not md_text:
        return md_text

    # 1) 完整 <system-reminder>...</system-reminder> 整段丢
    md_text = _RE_SYS_REMINDER_FULL.sub("", md_text)
    # 2) 半截 <system-reminder>...</字符串末尾> 也要丢（流式中间态）
    md_text = _RE_SYS_REMINDER_HALF.sub("", md_text)

    # 3) 完整 <xxx-hook>...</xxx-hook> 整段丢（兼容早期无 system-reminder 包裹的消息）
    md_text = _RE_HOOK_TAG_FULL.sub("", md_text)
    # 4) 半截 <xxx-hook>...</末尾> 也要丢
    md_text = _RE_HOOK_TAG_HALF.sub("", md_text)

    # 5) 完整 <hook event="Xxx">...</hook> 整段丢（兼容最早旧格式）
    md_text = _RE_HOOK_EVENT_FULL.sub("", md_text)
    # 6) 半截 <hook event=...>...</末尾> 也要丢
    md_text = _RE_HOOK_EVENT_HALF.sub("", md_text)

    return md_text


# 缓存大小阈值（KB）：超过此大小的文本不缓存，防止内存膨胀
_LRU_CACHE_SIZE_THRESHOLD = 200 * 1024  # 200KB


@lru_cache(maxsize=64)  # 256→64：实际唯一渲染内容通常 < 32 条，64 覆盖 2 个会话绰绰有余
def _render_markdown_to_html_cached_impl(raw_md: str, reasoning: str, compact: bool = False) -> str:
    """
    Markdown 转 HTML 的核心渲染函数（带 LRU 缓存）。
    """
    safe_md = _sanitize_incomplete_markdown(raw_md)
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    processed_md = _inject_think_cards(safe_md, True, compact=compact)
    processed_md = _inject_tool_blocks(processed_md, True, compact=compact)
    processed_md = _inject_hook_blocks(processed_md, True)

    try:
        md = get_markdown_instance()
        md.reset()
        html_content = md.convert(processed_md)
        html_content = _wrap_code_blocks_with_copy_button_web(html_content)
        return html_content
    except Exception:
        return f"<pre>{escape(raw_md)}</pre>"


def _render_markdown_to_html_cached(raw_md: str, reasoning: str, compact: bool = False) -> str:
    """
    带内存保护的 Markdown 渲染函数。
    - 对于超过阈值的文本，跳过缓存直接渲染
    - 保持 LRU 缓存以提高重复内容的性能
    """
    # 添加思考块内容
    if reasoning:
        raw_md = _render_think_block(reasoning, completed=True) + raw_md

    # 大文本跳过缓存，防止内存膨胀 — 用 __wrapped__ 绕过 LRU，不清空缓存
    text_size = len(raw_md.encode("utf-8"))
    if text_size > _LRU_CACHE_SIZE_THRESHOLD:
        return _render_markdown_to_html_cached_impl.__wrapped__(raw_md, reasoning, compact=compact)

    return _render_markdown_to_html_cached_impl(raw_md, reasoning, compact=compact)


# ── Skeleton 全局缓存：_load_skeleton 返回的 HTML 字符串（~54KB）在
# 多张卡片间共享，避免每张卡片独立构造大段 CSS/JS 模板。
# 缓存键：(is_light, theme_fingerprint, font_family)
_skeleton_cache: Dict[tuple, str] = {}


# 流式模式追加的字符统计 HTML 标记，用于 finish_streaming 时移除
_CHAR_COUNT_HTML = '<div id="char-count" style="color: var(--text-muted); font-size: 11px; margin-top: 12px; text-align: right; opacity: 0.7;"></div>'

# ── 流式活动坞（Streaming Dock）骨架资产 ──
# 简洁模式下流式期间：#tool-section 从卡片顶部沉到底部并限高 ~3-4 行，
# 让用户实时看到正在执行的工具/思考；流式结束后归位顶部恢复现状。
# 由 Python 在流式开始/结束时注入 _setStreamingDock(true/false) 切换。
_STREAMING_DOCK_CSS = """
                /* ── 流式活动坞：简洁模式流式期间工具区沉底 + 限高 ──
                   纯 CSS order 调换，不搬移 DOM，避免闪烁。 */
                body.streaming-dock {
                    display: flex;
                    flex-direction: column;
                    overflow-anchor: auto;
                }
                body.streaming-dock #content-placeholder {
                    order: 1;
                }
                body.streaming-dock #tool-section {
                    order: 2;
                    margin: 8px 0 0 0;
                }
                /* 坞态限高：≈3-4 行条目（行高约 26px + 上下 padding） */
                body.streaming-dock #tool-content {
                    max-height: 110px;
                }
"""

_STREAMING_DOCK_JS = """
                // ===== 流式活动坞（Streaming Dock）=====
                window._streamingActive = false;
                function _setStreamingDock(active) {
                    // 仅简洁模式启用坞态
                    var on = !!active && !!window._toolCompactMode;
                    var wasOn = document.body.classList.contains('streaming-dock');
                    window._streamingActive = !!active;
                    if (on === wasOn) return;
                    var ts = document.getElementById('tool-section');
                    // 切换前记录工具区高度与用户是否在底部，用于阅读位置补偿
                    var _dockH = ts ? ts.offsetHeight : 0;
                    var _atBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < 40;
                    document.body.classList.toggle('streaming-dock', on);
                    if (!on && wasOn) {
                        // 坞态 → 归位顶部：正文整体下移 ≈ 工具区高度，
                        // 用户上滚阅读时补偿 scrollTop，避免阅读位置跳动
                        if (!_atBottom && _dockH > 0) {
                            document.body.scrollTop = document.body.scrollTop + _dockH;
                        }
                        // 恢复完整高度后滚到底部，展示最新条目
                        var tc = document.getElementById('tool-content');
                        if (tc) { tc._userScrolledUp = false; tc.scrollTop = tc.scrollHeight; }
                    } else if (on && !wasOn) {
                        // 顶部 → 坞态：正文上移，做对称补偿
                        if (!_atBottom && _dockH > 0) {
                            document.body.scrollTop = Math.max(0, document.body.scrollTop - _dockH);
                        }
                    }
                    // 高度变化（110px ↔ 600px max-height）后重新报告文档高度
                    if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                }
"""


def clear_global_render_cache():
    """清理全局 Markdown 渲染 LRU 缓存 + 骨架 HTML 缓存

    应在会话切换、清空聊天区域时调用，释放缓存的 HTML 字符串。
    """
    _render_markdown_to_html_cached_impl.cache_clear()
    _skeleton_cache.clear()


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


# ===== _resolve_image_src 模块级常量（避免每次渲染重编译正则+重算路径） =====
_IMG_SRC_PATTERN = re.compile(r'(<img\s[^>]*?src\s*=\s*["\'])([^"\']+)(["\'][^>]*?>)', re.IGNORECASE)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve_image_src(html_content: str) -> str:
    """
    将 HTML 中的图片 src 相对路径转为绝对 file:/// 路径。

    检测 <img src="相对路径"> 中的 src，如果路径是相对路径且本地文件存在，
    则转换为 file:/// 绝对路径，确保 QWebEngineView 能正常加载。
    已存在的绝对路径（http/https/file/data/qrc）跳过处理。
    """

    def _replacer(match):
        prefix = match.group(1)
        src = match.group(2)
        suffix = match.group(3)

        # 跳过已经是绝对 URL 或 data URI 的 src
        if src.startswith(("http://", "https://", "file://", "data:", "qrc:/", "#", "blob:")):
            return match.group(0)

        # 尝试解析为绝对路径
        if os.path.isabs(src):
            # 已经是绝对路径，直接检查文件是否存在
            candidate = os.path.normpath(src)
        else:
            # 相对路径：以项目根目录为基准拼接
            candidate = os.path.normpath(os.path.join(_PROJECT_ROOT, src))

        if os.path.isfile(candidate):
            # 本地文件存在，转为 file:/// 路径
            file_url = QUrl.fromLocalFile(candidate).toString()
            return f"{prefix}{file_url}{suffix}"

        return match.group(0)

    return _IMG_SRC_PATTERN.sub(_replacer, html_content)


# ======== 本地 Vendor JS 脚本（离线优先，CDN 降级） ========
_vendor_script_tags_cache: Optional[str] = None


def _get_vendor_script_tags() -> str:
    """构建本地 vendor JS 脚本标签（离线优先，CDN 降级）。

    优先引用本地 app/resources/web/vendor/ 下的 JS 库（离线可用），
    本地文件缺失时降级为 CDN，确保离线环境 echarts 可用。
    结果做模块级缓存，避免每次 _load_skeleton 都做文件系统检查。
    """
    global _vendor_script_tags_cache
    if _vendor_script_tags_cache is not None:
        return _vendor_script_tags_cache

    # PyInstaller 打包后资源可能在 _MEIPASS 下
    base_dirs = [_PROJECT_ROOT]
    if hasattr(sys, "_MEIPASS"):
        base_dirs.append(sys._MEIPASS)

    vendor_libs = [
        ("app/resources/web/vendor/echarts.min.js", "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"),
        (
            "app/resources/web/vendor/echarts-wordcloud.min.js",
            "https://cdn.jsdelivr.net/npm/echarts-wordcloud@2/dist/echarts-wordcloud.min.js",
        ),
    ]

    tags = []
    for rel_path, cdn_url in vendor_libs:
        local_found = False
        for base in base_dirs:
            candidate = os.path.join(base, rel_path)
            if os.path.isfile(candidate):
                # 用绝对 file:/// URL，确保任何 baseUrl 下都能加载
                local_url = QUrl.fromLocalFile(candidate).toString()
                tags.append(f'<script src="{local_url}"></script>')
                local_found = True
                break
        if not local_found:
            tags.append(f'<script src="{cdn_url}"></script>')

    _vendor_script_tags_cache = "\n        ".join(tags)
    return _vendor_script_tags_cache


# ======== WebViewer ========
class ConsoleMonitorPage(QWebEnginePage):
    codeActionRequested = pyqtSignal(str, str)
    contextActionRequested = pyqtSignal(str, str)
    heightReported = pyqtSignal(int)
    contentReady = pyqtSignal()
    toolDiffRequested = pyqtSignal(str)  # tool_call_id
    subAgentLogRequested = pyqtSignal(str)  # task_ids (comma-separated)
    saveFileRequested = pyqtSignal(str, str)  # code, lang

    def __init__(self, profile=None, parent=None):
        """创建一个 ConsoleMonitorPage。

        Args:
            profile: QWebEngineProfile 实例。传入 None 则使用默认 profile。
            parent: 父 QObject。
        """
        if profile is not None:
            super().__init__(profile, parent)
        else:
            super().__init__(parent)

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        msg = message.strip()
        # [PERF] pywebview_height 是最高频信号（流式时每周期多次触发），
        # 放在首位快速短路，避免对每条 height 消息都做 startswith("pywebview_ready") 等冗余判断
        if msg.startswith("pywebview_height:"):
            try:
                self.heightReported.emit(int(float(msg.split(":")[1])))
            except Exception:
                pass
        elif msg == "pywebview_ready":
            self.contentReady.emit()
        elif msg.startswith("pywebview_action:"):
            if "context|||" in msg:
                try:
                    parts = msg.split("|||")
                    self.contextActionRequested.emit(urllib.parse.unquote(parts[1]), urllib.parse.unquote(parts[2]))
                except Exception:
                    pass
            elif "context_lost" in msg:
                self._handle_context_lost()
            elif "open_url:" in msg:
                try:
                    url_str = msg.split("open_url:", 1)[1]
                    from PyQt5.QtCore import QUrl
                    from PyQt5.QtGui import QDesktopServices

                    QDesktopServices.openUrl(QUrl(url_str))
                except Exception:
                    pass
            elif "open_file:" in msg:
                # 处理打开文件/文件夹请求
                try:
                    file_path = msg.split("open_file:", 1)[1]

                    import os
                    import subprocess

                    if os.name == "nt":
                        if os.path.isdir(file_path):
                            # 文件夹：直接在资源管理器中打开
                            subprocess.Popen(["explorer", file_path])
                        else:
                            # 文件：使用系统默认程序打开
                            os.startfile(file_path)
                    else:
                        # macOS/Linux
                        cmd = "open" if os.uname().sysname == "Darwin" else "xdg-open"
                        subprocess.Popen([cmd, file_path])
                except Exception:
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
                    self.codeActionRequested.emit(base64.b64decode(p[2]).decode("utf-8"), p[1])
                except Exception:
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
    # 降低 MAX_HEIGHT 可大幅减少每个 Chromium 实例的离屏渲染缓冲区
    # 4000→2000 将单视图 GPU 缓冲区从 ~28.8MB 降至 ~14.4MB
    # 标准消息卡片在正常宽度(400~700px)下，1500px 高度已覆盖绝大多数内容
    MAX_WIDTH = 1800
    MAX_HEIGHT = 3000

    def __init__(self, parent=None, light=False):
        super().__init__(parent)
        self._markdown_text = ""
        self._streaming = True
        self._is_history = False  # 历史会话标志（非流式加载的历史消息）
        self._is_js_ready = False
        self._last_rendered_html = ""
        self._last_rendered_markdown = ""
        # 流式渲染哈希缓存：避免对相同 processed_md 重复跑 6 轮正则 + md.convert()
        self._processed_md_hash = None
        self._cached_streaming_html = None
        self._cached_raw_md_hash = 0  # hash(self._markdown_text) 在缓存时的快照，供 finish_streaming 验证缓存有效性
        self._lazy_markdown_cb = None  # 懒回调：渲染时才生成 markdown，避免高频 content_to_markdown
        # [PERF] 工具结果 markdown 缓存：tool_call_id → <tool>...</tool> markdown 字符串
        # 已完成的工具块的 markdown 只計算一次，後續增量渲染跳過昂貴的
        # _sanitize_result + sorted() + JSON 序列化，直接拼接緩存結果。
        self._tool_md_cache: Dict[str, str] = {}
        self._light_skeleton = light  # 轻量骨架标志（去掉 echarts CDN 等）
        # [PERF] 最小渲染间隔 80ms：降低 WebEngine setHtml 调用频率
        # 每 80ms 合并一次渲染比 50ms 减少 37.5% 的 Chromium 重排版次数，
        # 对用户感知的流式流畅度影响极小（人眼无法分辨 50ms 与 80ms 的渲染间隔差异）
        self._min_render_interval = 80
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

        # 思考已完成标志：工具调用开始时置 True，阻止 _render_markdown_to_html 继续剥离 </think>
        self._thinking_finalized = False
        # 流式思考首 chunk 标志：首 chunk 渲染"深度思考中..." spinner，后续静默累积不更新 DOM
        self._reasoning_streaming_started = False
        # <think> 标签文本流式思考标志：与 _reasoning_streaming_started 对应，
        # 用于 text 块中包含 <think> 标签时的静默累积策略
        self._think_text_streaming_started = False

        # [PERF] 流式速度跟踪：用于自适应安全渲染间隔
        self._last_chunk_time = 0.0  # 上次 append_chunk 的时间戳（monotonic ns）
        self._current_adaptive_interval = self._SAFETY_RENDER_INTERVAL  # 当前自适应间隔

        # 内部文档高度跟踪（用于 wheelEvent 判断内部是否可滚动）
        self._document_height = 0

        # 1. 渲染定时器
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._perform_update)

        # 2. Resize 定时器 (修复 Crash 的关键：作为成员变量，随 self 销毁)
        # [PERF] 100ms 比 50ms 减少 50% 的 height report 触发，
        # 降低父容器和 scrollarea 的 layout 重算频率
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self._safe_report_height)

        # 共享全局 profile：所有消息卡片复用同一 Chromium 进程池，
        # 避免每个卡片独立匿名 profile 触发独立进程组初始化（加载慢的根因）。
        self._profile = get_shared_web_profile()
        self._page = ConsoleMonitorPage(self._profile, self)
        self.setPage(self._page)

        # 启用本地文件访问，支持 markdown 图片显示
        ws = self.settings()
        ws.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        ws.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)

        # 透明背景
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.page().setBackgroundColor(Qt.transparent)
        # 使用自定义右键菜单（不是浏览器默认的）
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

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

    @property
    def _tool_compact_mode(self) -> bool:
        try:
            from app.utils.config import Settings

            return Settings.get_instance().ui_compact_tool_area.value
        except Exception:
            return True

    @property
    def _tool_target_id(self) -> str:
        return "tool-content" if self._tool_compact_mode else "content-placeholder"

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
            logger.warning(f"Context restore failed: {e}")
            # 恢复失败，请求重建
            self.needRecreate.emit()

    def event(self, event):
        """拦截 WebEngine 事件"""
        # 处理上下文丢失
        if event.type() == QTimerEvent and hasattr(self, "_context_lost_timer"):
            pass
        return super().event(event)

    def wheelEvent(self, event: QWheelEvent):
        # 策略：只有当内部 WebView 无法继续滚动时，才将滚动事件转发到外部 chat_scroll_area。
        # 如果内部有可滚动内容且未到达边界，让内部 WebView 自己处理滚动。
        try:
            widget = self
            for _ in range(5):
                if hasattr(widget, "chat_scroll_area"):
                    break
                parent_widget = widget.parent()
                if parent_widget is None:
                    break
                widget = parent_widget

            outer_area = getattr(widget, "chat_scroll_area", None) if hasattr(widget, "chat_scroll_area") else None
            if not outer_area:
                super().wheelEvent(event)
                return

            outer_vbar = outer_area.verticalScrollBar()
            if not outer_vbar or outer_vbar.minimum() == outer_vbar.maximum():
                # 外部没有可滚动范围 → 直接内部处理
                super().wheelEvent(event)
                return

            # 检查内部 WebView 是否有可滚动内容（文档高度 > 视口高度）
            viewport_h = self.height()
            # 🐛 修复：用 page().contentsSize() 获取更实时的文档高度，
            # 替代仅靠 JS 异步上报的 _document_height（流式期间滞后 ~100-200ms）。
            # contentsSize 由 Chromium 在每帧渲染后更新，滞后 < 1 帧（~16ms）。
            doc_h = 0
            page = self.page()
            if page:
                contents_size = page.contentsSize()
                if contents_size and contents_size.height() > 0:
                    doc_h = contents_size.height()
            if doc_h <= 0:
                doc_h = self._document_height

            # 🐛 修复竞态：_document_height 通过 JS 异步上报，初始值为 0，
            # 此时保守处理——让内部先处理（super().wheelEvent），
            # 等 _on_height_reported 上报实际高度后再启用边界转发逻辑。
            if doc_h <= 0:
                super().wheelEvent(event)
                return

            inner_has_overflow = doc_h > viewport_h and viewport_h >= 40

            if not inner_has_overflow:
                # 内部没有溢出 → 转发到外部
                delta = event.angleDelta().y()
                outer_vbar.setValue(outer_vbar.value() - delta // 2)
                event.accept()
                return

            # 内部有溢出：检查当前滚动位置是否在边界
            scroll_pos = self.page().scrollPosition() if self.page() else QPointF(0, 0)
            scroll_y = scroll_pos.y()

            scrolling_down = event.angleDelta().y() < 0
            scrolling_up = event.angleDelta().y() > 0

            # 判断是否到达滚动边界
            at_top = scroll_y <= SCROLL_BOUNDARY_TOLERANCE
            at_bottom = scroll_y >= (doc_h - viewport_h - SCROLL_BOUNDARY_TOLERANCE)

            if (scrolling_down and at_bottom) or (scrolling_up and at_top):
                # 内部已到达边界 → 转发到外部
                delta = event.angleDelta().y()
                outer_vbar.setValue(outer_vbar.value() - delta // 2)
                event.accept()
                return

            # 内部还有可滚动空间 → 让内部处理
            super().wheelEvent(event)
        except Exception:
            super().wheelEvent(event)

    def setFixedSize(self, *args, **kwargs):
        """限制最大尺寸，防止 GPU 内存溢出"""
        # 计算安全尺寸
        w = args[0] if len(args) > 0 else kwargs.get("width", self.MAX_WIDTH)
        h = args[1] if len(args) > 1 else kwargs.get("height", self.MAX_HEIGHT)

        # 限制最大尺寸
        safe_w = min(w, self.MAX_WIDTH) if isinstance(w, int) else w
        safe_h = min(h, self.MAX_HEIGHT) if isinstance(h, int) else h

        super().setFixedSize(safe_w, safe_h)

    def resize(self, *args, **kwargs):
        """限制 resize 尺寸，防止过大导致 GPU 内存溢出"""
        w = args[0] if len(args) > 0 else kwargs.get("width", self.MAX_WIDTH)
        h = args[1] if len(args) > 1 else kwargs.get("height", self.MAX_HEIGHT)

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
                    if hasattr(parent, "chat_layout") or parent.__class__.__name__ == "MessageCard":
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
        self._document_height = h  # 跟踪文档高度用于 wheelEvent 边界判断
        final_h = h + 2
        if abs(self.height() - final_h) > 2:
            self.contentHeightChanged.emit(final_h)

    def _on_js_ready(self):
        self._is_js_ready = True
        # 同步简洁模式标志到 JS
        try:
            from app.utils.config import Settings

            compact = "true" if Settings.get_instance().ui_compact_tool_area.value else "false"
            self.page().runJavaScript(f"window._toolCompactMode = {compact};")
            # 历史会话：先折叠工具区（设置 data-collapsed="true"，dock sync 需要读取此值）
            if getattr(self, "_is_history", False):
                self.page().runJavaScript(
                    "var _ts=document.getElementById('tool-section');"
                    "var _sep=document.getElementById('tool-separator');"
                    "if(_ts)_ts.setAttribute('data-collapsed','true');"
                    "if(_sep)_sep.setAttribute('aria-expanded','false');"
                )
            # 坞态同步：由 JS 读取 tool-section 的 data-collapsed 属性判断
            # collapsed="true"（历史会话/已完成）→ dock off
            # collapsed="false"（流式会话默认）→ dock on（受 _toolCompactMode 守卫）
            # 此 JS 在上方 collapse 之后执行，保证 data-collapsed 已更新到正确值
            self.page().runJavaScript(
                "var _ts2=document.getElementById('tool-section');"
                "var _co=_ts2&&_ts2.getAttribute('data-collapsed')==='true';"
                "if(typeof _setStreamingDock==='function')_setStreamingDock(!_co);"
            )
        except RuntimeError:
            pass
        # 🐛 修复：流式内容可能在 JS 就绪前通过 _lazy_markdown_cb 缓存，
        # 仅检查 _markdown_text 会遗漏这些内容，导致卡片永久空白。
        # 当 _lazy_markdown_cb 存在时也触发渲染，_perform_update 会消费它。
        if self._markdown_text or self._lazy_markdown_cb:
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
        self._viewer_font_css = (
            f"{get_font_family_css()} font-family: {font_family}, sans-serif; font-size: {scale_font_size(14)}px;"
        )

        # ── 骨架全局缓存：多张卡片共享同一份 HTML 模板 ──
        # 缓存键由主题色 + 字体 + light 模式组成，最多 ~8 条 × 54KB ≈ 432KB
        theme = current_theme()
        body_font_size = scale_font_size(14)
        code_font_size = scale_font_size(13)
        tag_font_size = scale_font_size(12)
        small_font_size = scale_font_size(11)
        tiny_font_size = scale_font_size(10)
        font_family_global = _get_global_font()

        theme_fp = json.dumps({k: theme[k] for k in sorted(theme)}, option=json.OPT_SORT_KEYS).decode("utf-8")
        cache_key = (
            self._light_skeleton,
            theme_fp,
            font_family,
            font_family_global,
            body_font_size,
            code_font_size,
            tag_font_size,
            small_font_size,
            tiny_font_size,
        )
        cached = _skeleton_cache.get(cache_key)
        if cached is not None:
            self.setHtml(cached, QUrl.fromLocalFile(_PROJECT_ROOT + "/"))
            return

        tag_css = []
        for act, col in ACTION_COLOR_MAP.items():
            tag_css.append(
                f'.context-tag[data-type="{act}"] {{ background: {col}15; border-color: {col}60; color: {col}; }}'
            )
            tag_css.append(f'.context-tag[data-type="{act}"]:hover {{ background: {col}30; border-color: {col}; }}')

        if self._light_skeleton:
            cdn_libs = ""
        else:
            # 离线优先：本地 vendor JS（app/resources/web/vendor/），缺失时降级 CDN
            cdn_libs = _get_vendor_script_tags()

        # 检测浅色/深色模式，用于滚动条和行内差异框主题适配
        try:
            from app.utils.theme_manager import theme_manager

            _is_light = theme_manager.is_light_theme()
        except Exception:
            _is_light = False

        if _is_light:
            # 浅色模式滚动条 — 半透明灰色，在不同浅色主題背景上都自然
            scrollbar_css = """
                ::-webkit-scrollbar {
                    width: 6px;
                    height: 6px;
                }
                ::-webkit-scrollbar-track {
                    background: transparent;
                    border-radius: 3px;
                    margin: 2px 0;
                }
                ::-webkit-scrollbar-track:hover {
                    background: rgba(0, 0, 0, 0.04);
                }
                ::-webkit-scrollbar-thumb {
                    background: rgba(0, 0, 0, 0.18);
                    border-radius: 3px;
                    min-height: 24px;
                }
                ::-webkit-scrollbar-thumb:hover {
                    background: rgba(0, 0, 0, 0.28);
                }
                ::-webkit-scrollbar-thumb:active {
                    background: rgba(0, 0, 0, 0.35);
                }
                ::-webkit-scrollbar-corner {
                    background: transparent;
                }
                /* Firefox 滚动条 */
                * {
                    scrollbar-width: thin;
                    scrollbar-color: rgba(0, 0, 0, 0.18) transparent;
                }
            """
        else:
            # 深色模式滚动条 — 保留原有的精致深色风格
            scrollbar_css = """
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
        _is_light_diff = _is_light
        mono_font = f"{font_family_global}, Consolas, monospace"

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
                    --code-bg: {"var(--panel-soft)" if _is_light_diff else "transparent"};
                    --code-toolbar: {"rgba(0,0,0,0.03)" if _is_light_diff else "rgba(255, 255, 255, 0.03)"};
                    --code-border: {"var(--border)" if _is_light_diff else "#2a3447"};
                    --success: #5fd18c;
                    --danger: #ff7b7b;
                }}
                html {{
                    overflow: hidden;
                    /* 🛡️ 阻止 Chromium 视口创建滚动条。
                       卡片内容完全展开，由父级滚动容器处理滚动。
                       流式渲染期间内容可能暂时超出视口，
                       但 opacity transition 遮盖了短暂裁剪。 */
                }}
                html, body {{
                    background: var(--bg) !important;
                    color: var(--text);
                    {self._viewer_font_css}
                    margin: 0; 
                    padding: 0;
                }}
                body {{
                    padding: 6px 14px 0 14px; 
                    max-height: {self.MAX_HEIGHT}px;
                    overflow-y: auto;
                    overflow-x: hidden;
                    overflow-anchor: auto;
                }}
                {scrollbar_css}

                #content-placeholder {{
                    color: var(--text);
                    /* 平滑过渡：全量渲染时内容以轻微透明度淡入替代生硬闪烁 */
                    transition: opacity 150ms ease;
                    will-change: opacity;
                }}
                #content-placeholder * {{ color: inherit; }}
                /* 图片自适应卡片宽度 */
                #content-placeholder img {{
                    max-width: 100%;
                    height: auto;
                    border-radius: 8px;
                    display: block;
                    margin: 8px 0;
                    object-fit: contain;
                }}
                /* 工具/思考块内的图标小图不应用圆角裁剪，保持原样显示 */
                #content-placeholder .tool-block img,
                #content-placeholder .think-block img,
                #content-placeholder .think-compact img,
                #content-placeholder .think-streaming img {{
                    border-radius: 0;
                    display: inline;
                    margin: 0;
                    max-width: none;
                }}
                h1, h2, h3, h4, h5, h6 {{ color: var(--text) !important; font-weight: 700; letter-spacing: 0.01em; }}
                h1 {{ font-size: 1.45em; margin: 12px 0 8px; }}
                h2 {{ font-size: 1.25em; margin: 10px 0 6px; }}
                h3 {{ font-size: 1.1em; margin: 8px 0 4px; }}
                p {{ margin: 8px 0; color: var(--text-secondary); }}
                a {{ color: var(--accent) !important; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
                ul, ol {{ margin: 8px 0; padding-left: 24px; }}
                li {{ margin: 4px 0; color: var(--text-secondary); }}
                strong {{ color: var(--text) !important; font-weight: 600; }}
                em {{ color: var(--text-secondary) !important; font-style: italic; }}
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
                /* 解决 Chromium 滚动容器 padding-bottom 不生效的 bug */
                #content-placeholder::after {{
                    content: '';
                    display: block;
                    height: 5px;
                }}

                /* 优化：紧凑的段落间距 */
                p {{ margin: 8px 0; }}

                /* ── 原生 <table> 样式（保留 display:table，自动拉伸填满） ── */
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
                    color: var(--text) !important;
                    border-bottom: 1px solid var(--border-strong);
                }}
                table:not(.code-table) td {{
                    padding: 8px 12px;
                    border-bottom: 1px solid var(--border);
                    color: var(--text-secondary) !important;
                }}
                table:not(.code-table) tr:nth-child(even) {{ background: rgba(255, 255, 255, 0.02); }}
                table:not(.code-table) tr:hover {{ background: rgba(255, 255, 255, 0.05); }}

                /* ── 表格滚动容器（JS 在 updateContent 中自动包裹每个 <table>） ── */
                .table-scroll-wrapper {{
                    overflow-x: auto;
                    overflow-y: hidden;
                    margin: 10px 0;
                    border: 1px solid var(--border);
                    border-radius: 10px;
                }}
                .table-scroll-wrapper::-webkit-scrollbar {{
                    height: 8px;
                }}
                .table-scroll-wrapper::-webkit-scrollbar-thumb {{
                    background: var(--border);
                    border-radius: 4px;
                }}
                .table-scroll-wrapper::-webkit-scrollbar-thumb:hover {{
                    background: var(--border-strong);
                }}
                .table-scroll-wrapper::-webkit-scrollbar-track {{
                    background: transparent;
                }}
                .table-scroll-wrapper > table {{
                    width: 100%;
                    border-collapse: collapse;
                    background: transparent;
                    font-family: '{font_family}', sans-serif;
                    font-size: {body_font_size}px;
                    margin: 0;
                    border: none !important;
                    border-radius: 0 !important;
                }}
                .table-scroll-wrapper > table th,
                .table-scroll-wrapper > table td {{
                    white-space: normal;
                    word-break: break-word;
                }}
                /* 继承 wrapper 内部表格的行样式 */
                .table-scroll-wrapper > table th {{
                    background: rgba(255, 255, 255, 0.04);
                    padding: 8px 12px;
                    text-align: left;
                    font-weight: 600;
                    color: var(--text) !important;
                    border-bottom: 1px solid var(--border-strong);
                }}
                .table-scroll-wrapper > table td {{
                    padding: 8px 12px;
                    border-bottom: 1px solid var(--border);
                    color: var(--text-secondary) !important;
                    max-height: 3.8em;
                    overflow-y: auto;
                    vertical-align: top;
                }}
                .table-scroll-wrapper > table tr:nth-child(even) {{ background: rgba(255, 255, 255, 0.02); }}
                .table-scroll-wrapper > table tr:hover {{ background: rgba(255, 255, 255, 0.05); }}

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
                .lineno {{ width: 32px; text-align: right; padding-right: 8px !important; color: #606060; border-right: 1px solid #404040; user-select: none; font-size: {
            small_font_size
        }px; line-height: 1.5; }}
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
                    contain: layout style;
                }}
                .cm-collapsible__summary {{
                    width: 100%;
                    display: flex;
                    align-items: center;
                    gap: 6px;
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
                    width: 6px;
                    height: 6px;
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
                    margin: 4px 0;
                    background: transparent;
                    border: none;
                    border-radius: 6px;
                }}
                .think-compact {{
                    background: transparent;
                    border: none;
                }}
                .think-block[data-expanded="true"] {{
                    border: none;
                }}
                .think-block__summary {{
                    padding: 5px 10px;
                    color: var(--text-secondary);
                    font-weight: 600;
                }}
                /* 流式思考纯文本块（无折叠UI）— 金色圆环 + 背景 */
                .think-streaming {{
                    margin: 4px 0;
                    background: transparent;
                    border: none;
                    border-radius: 6px;
                    padding: 8px 10px;
                    color: var(--text-secondary);
                    font-style: italic;
                    transition: border-color 220ms ease, background 220ms ease;
                }}
                .think-streaming[data-streaming="true"] {{
                    background: transparent;
                }}
                /* 思考轮播提示文字 — 从左到右脉冲渐变色动画 */
                .think-streaming-tip {{
                    background: linear-gradient(
                        90deg,
                        var(--text-secondary) 0%,
                        var(--accent) 45%,
                        var(--accent-warm) 55%,
                        var(--text-secondary) 100%
                    );
                    background-size: 200% 100%;
                    background-clip: text;
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    animation: think-tip-sweep 2.5s ease-in-out infinite;
                }}
                @keyframes think-tip-sweep {{
                    0% {{ background-position: 200% 0; }}
                    100% {{ background-position: -200% 0; }}
                }}
                /* 工具运行卡片的参数预览 — 流式态脉冲渐变色动画 */
                .tool-streaming-block[data-streaming="true"] .tool-streaming-preview {{
                    background: linear-gradient(
                        90deg,
                        var(--text-secondary) 0%,
                        var(--accent) 45%,
                        var(--accent-warm) 55%,
                        var(--text-secondary) 100%
                    );
                    background-size: 200% 100%;
                    background-clip: text;
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    animation: think-tip-sweep 2.5s ease-in-out infinite;
                }}
                .think-content {{
                    padding: 8px 10px;
                    border-top: 1px solid var(--border);
                    background: transparent;
                    color: var(--text-secondary) !important;
                    font-style: italic;
                    font-size: {code_font_size + 2}px;
                    font-family: '{font_family}', sans-serif;
                    line-height: 1.6;
                    max-height: 500px;
                    overflow-y: auto;
                    transition: opacity 200ms ease;
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
                /* 思考流式预览 — 默认静态色 */
                .think-streaming-preview {{
                    position: relative;
                    color: var(--text-secondary);
                }}
                /* 流式状态：::after 伪元素叠加流动光效，不触碰文字层 */
                .think-block[data-streaming="true"] .think-streaming-preview::after {{
                    content: '';
                    position: absolute;
                    inset: 0;
                    pointer-events: none;
                    background: linear-gradient(
                        90deg,
                        transparent 0%,
                        rgba(255, 200, 50, 0.05) 45%,
                        rgba(255, 200, 50, 0.10) 50%,
                        rgba(255, 200, 50, 0.05) 55%,
                        transparent 100%
                    );
                    background-size: 250% 100%;
                    animation: think-shimmer 3s ease-in-out infinite;
                }}
                /* 思考中蛇形爬行动画 */
                .think-block .think-block__summary {{
                    transition: background-color 220ms ease;
                }}
                .think-block[data-streaming="true"] .think-block__summary {{
                    background: rgba(255, 255, 255, 0.04);
                }}
                .think-snake {{
                    display: inline-block;
                    vertical-align: middle;
                    margin-right: 2px;
                }}
                .think-snake-arc {{
                    transform-origin: 12px 12px;
                }}

                /* 工具流式调用块 — 金色圆环动画背景 */
                .tool-streaming-block .tool-block__summary {{
                    transition: background-color 220ms ease;
                }}
                .tool-streaming-block[data-streaming="true"] .tool-block__summary {{
                    background: rgba(255, 200, 50, 0.05);
                }}
                /* spinner 和状态文字的平滑过渡 */
                .tool-streaming-spinner {{
                    transition: opacity 220ms ease, transform 220ms ease;
                }}
                .tool-streaming-block[data-streaming="false"] .tool-streaming-spinner {{
                    opacity: 0;
                    transform: scale(0.7);
                }}
                .tool-streaming-block[data-streaming="true"] .tool-streaming-spinner {{
                    opacity: 1;
                    transform: scale(1);
                }}

                .tool-block {{
                    margin: 4px 0;
                    background: transparent;
                    border: none;
                    border-radius: 6px;
                    box-shadow: none;
                }}
                .tool-block[data-expanded="true"] {{
                    border: none;
                }}
                .tool-block__summary {{
                    padding: 5px 10px;
                    color: var(--accent);
                    font-weight: 600;
                    font-size: {code_font_size}px;
                    font-family: '{font_family}', sans-serif;
                    white-space: normal;
                }}
                .tool-expanded-content {{
                    padding: 0;
                }}
                .tool-diff-stats {{
                    display: inline-flex;
                    align-items: center;
                    gap: 3px;
                    margin-left: 4px;
                    padding: 1px 6px;
                    border: 1px solid rgba(139, 148, 158, 0.2);
                    border-radius: 999px;
                    background: rgba(139, 148, 158, 0.08);
                    font-weight: 700;
                    white-space: nowrap;
                }}
                .tool-diff-stats__add {{
                    color: #3fb950;
                }}
                .tool-diff-stats__del {{
                    color: #ff7b72;
                }}
                .tool-diff-stats__sep {{
                    color: {"var(--text-muted)" if _is_light_diff else "#6e7681"};
                }}
                .tool-diff-inline {{
                    margin: 0;
                    background: {"var(--panel-soft)" if _is_light_diff else "rgba(13,17,23,0.40)"};
                    border: 1px solid {"var(--border)" if _is_light_diff else "rgba(48,54,61,0.25)"};
                    border-radius: 8px;
                    overflow: hidden;
                }}
                .tool-diff-inline__header {{
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    min-width: 0;
                    padding: 4px 10px;
                    background: {"rgba(0,0,0,0.03)" if _is_light_diff else "rgba(22,27,34,0.40)"};
                    border-bottom: 1px solid {"var(--border)" if _is_light_diff else "rgba(48,54,61,0.25)"};
                    color: {"var(--text-secondary)" if _is_light_diff else "#8b949e"};
                    font-size: {small_font_size}px;
                    font-weight: 600;
                }}
                .tool-diff-inline__title {{
                    flex: 0 0 auto;
                    color: {"var(--text)" if _is_light_diff else "#d0d7de"};
                    letter-spacing: 0;
                }}
                .tool-diff-inline__file {{
                    flex: 1 1 auto;
                    min-width: 0;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                    color: {"var(--text-secondary)" if _is_light_diff else "#8b949e"};
                    font-weight: 500;
                }}
                .tool-diff-inline__summary {{
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    flex: 0 0 auto;
                    padding: 2px 7px;
                    border-radius: 999px;
                    background: rgba(13,17,23,0.42);
                    border: 1px solid rgba(139, 148, 158, 0.18);
                    font-weight: 800;
                }}
                .tool-diff-inline__add {{
                    color: #56d364;
                }}
                .tool-diff-inline__del {{
                    color: #ff7b72;
                }}
                .tool-diff-inline__body {{
                    line-height: 1.55;
                    overflow-x: auto;
                }}
                .tool-diff-inline .diff-line {{
                    display: flex;
                    align-items: stretch;
                    min-height: 23px;
                    font-size: {tag_font_size}px;
                    line-height: 1.55;
                    border-bottom: 1px solid transparent;
                }}
                .tool-diff-inline .diff-ctx:hover {{
                    background: {"rgba(0,0,0,0.04)" if _is_light_diff else "rgba(255,255,255,0.035)"};
                }}
                .tool-diff-inline .diff-add:hover {{
                    background-color: {"rgba(63, 185, 80, 0.15)" if _is_light_diff else "rgba(63, 185, 80, 0.18)"};
                }}
                .tool-diff-inline .diff-del:hover {{
                    background-color: {"rgba(248, 81, 73, 0.15)" if _is_light_diff else "rgba(248, 81, 73, 0.18)"};
                }}
                .tool-diff-inline .line-num {{
                    flex: none;
                    min-width: 38px;
                    padding: 0 8px;
                    text-align: right;
                    color: {"var(--text-muted)" if _is_light_diff else "#6e7681"};
                    user-select: none;
                    font-size: {tag_font_size - 1}px;
                    box-sizing: border-box;
                    background: {"rgba(0,0,0,0.03)" if _is_light_diff else "rgba(13,17,23,0.18)"};
                    border-right: 1px solid {"var(--border)" if _is_light_diff else "rgba(139,148,158,0.16)"};
                }}
                .tool-diff-inline .line-sign {{
                    flex: none;
                    width: 20px;
                    text-align: center;
                    color: #6e7681;
                    user-select: none;
                    font-weight: 700;
                }}
                .tool-diff-inline .line-code {{
                    flex: 1;
                    padding: 0 10px;
                    white-space: pre-wrap;
                    min-width: 0;
                }}
                .tool-diff-inline .diff-add {{
                    background-color: rgba(63, 185, 80, 0.095);
                    box-shadow: inset 3px 0 0 rgba(63, 185, 80, 0.65);
                }}
                .tool-diff-inline .diff-add .line-sign {{
                    color: #56d364;
                }}
                .tool-diff-inline .diff-add .line-code {{
                    color: {"#1a7f37" if _is_light_diff else "#aff5b4"};
                }}
                .tool-diff-inline .diff-del {{
                    background-color: rgba(248, 81, 73, 0.095);
                    box-shadow: inset 3px 0 0 rgba(248, 81, 73, 0.62);
                }}
                .tool-diff-inline .diff-del .line-sign {{
                    color: #ff7b72;
                }}
                .tool-diff-inline .diff-del .line-code {{
                    color: {"#cf222e" if _is_light_diff else "#ffdcd7"};
                }}
                .tool-diff-inline .diff-ctx {{
                    color: {"var(--text-secondary)" if _is_light_diff else "#adbac7"};
                }}
                .tool-diff-inline .diff-hunk {{
                    color: {"var(--text)" if _is_light_diff else "#79c0ff"};
                    background: {"rgba(37, 99, 235, 0.06)" if _is_light_diff else "rgba(56, 139, 253, 0.075)"};
                }}
                .tool-diff-inline .diff-hunk .line-code {{
                    color: {"var(--text)" if _is_light_diff else "#79c0ff"};
                }}
                .tool-diff-inline .diff-file-header .line-code {{
                    color: {"var(--text)" if _is_light_diff else "#c9d1d9"};
                    font-weight: 600;
                }}
                .tool-diff-inline .diff-truncated {{
                    color: {"var(--text-muted)" if _is_light_diff else "#6e7681"};
                    background: {"rgba(0,0,0,0.03)" if _is_light_diff else "rgba(139, 148, 158, 0.055)"};
                }}
                .tool-diff-inline .diff-truncated .line-code {{
                    text-align: center;
                }}
                /* 空白上下文行（源文件空行）：折叠为紧凑空隙，避免单列模式下
                   段落差异之间出现 bulky 空行；连续空行只渲染一条。 */
                .tool-diff-inline .diff-line.diff-ctx-blank {{
                    min-height: 0;
                    height: 9px;
                }}
                .tool-diff-inline .diff-line.diff-ctx-blank .line-code {{
                    color: transparent;
                }}
                /* === 差异段：单列默认为"删除→新增"分组，双列(split-view)为配对行左右对照 === */
                .tool-diff-inline .diff-segment {{
                    display: block;
                }}
                /* 单列模式（默认）：所有删除先、所有新增后，连续堆叠 */
                .tool-diff-inline .diff-seg-col {{
                    display: block;
                }}
                .tool-diff-inline .diff-seg-col > .diff-line {{
                    border-bottom: 1px solid transparent;
                }}
                /* 配对行视图（双列模式用）：默认隐藏 */
                .tool-diff-inline .diff-seg-paired {{
                    display: none;
                }}
                /* 双列模式：隐藏单列视图，显示配对视图 */
                .tool-diff-inline.split-view .diff-seg-col {{
                    display: none;
                }}
                .tool-diff-inline.split-view .diff-seg-paired {{
                    display: block;
                }}
                /* 配对行：左右对照（始终 row，因为仅在 split-view 中出现） */
                .tool-diff-inline .diff-seg-row {{
                    display: flex;
                    flex-direction: row;
                    align-items: stretch;
                }}
                .tool-diff-inline .diff-seg-row > .diff-line {{
                    flex: 1 1 50%;
                    width: auto;
                    border-bottom: 1px solid transparent;
                }}
                /* 空栏占位（纯删/纯增段在双列模式中的空白占位列） */
                .tool-diff-inline .diff-seg-empty {{
                    display: flex;
                    background: transparent !important;
                    box-shadow: none !important;
                }}
                .tool-diff-inline .diff-seg-empty .line-code {{
                    color: transparent;
                }}

                /* 元信息行（文件头/hunk头/截断）：行号列与符号列隐形，避免空列割裂视觉 */
                .tool-diff-inline .diff-meta .line-num {{
                    background: transparent;
                    border-right-color: transparent;
                    min-width: 0;
                    padding: 0;
                }}
                .tool-diff-inline .diff-meta .line-sign {{
                    width: 0;
                }}
                .tool-diff-inline .word-add {{
                    background: rgba(63, 185, 80, 0.28);
                    border-radius: 3px;
                    box-shadow: inset 0 -1px 0 rgba(63, 185, 80, 0.65);
                }}
                .tool-diff-inline .word-del {{
                    background: rgba(248, 81, 73, 0.28);
                    border-radius: 3px;
                    box-shadow: inset 0 -1px 0 rgba(248, 81, 73, 0.65);
                }}
                .tool-params-section,
                .tool-result-section {{
                    padding: 0;
                }}
                .tool-section-label {{
                    color: var(--text-muted);
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
                    border-bottom: 1px solid var(--border);
                    font-size: {tag_font_size}px;
                }}
                .args-row:last-child {{
                    border-bottom: none;
                }}
                .args-row.empty {{
                    color: var(--text-muted);
                    font-style: italic;
                    padding: 8px 12px;
                }}
                .args-key {{
                    flex: 0 0 auto;
                    min-width: 80px;
                    max-width: 120px;
                    color: var(--text-secondary);
                    font-weight: 500;
                    margin-right: 12px;
                    word-break: break-word;
                }}
                .args-row.result-success {{
                    border-top: 1px solid {"rgba(34, 197, 94, 0.3)" if _is_light_diff else "rgba(95, 209, 140, 0.3)"};
                    background: {"rgba(34, 197, 94, 0.05)" if _is_light_diff else "rgba(95, 209, 140, 0.05)"};
                }}
                .args-row.result-fail {{
                    border-top: 1px solid rgba(244, 67, 54, 0.3);
                    background: rgba(244, 67, 54, 0.05);
                }}
                .args-value {{
                    flex: 1 1 auto;
                    color: var(--text);
                    word-break: break-all;
                    font-family: {mono_font};
                    font-size: {small_font_size}px;
                }}
                .result-content {{
                    padding: 6px 12px 10px;
                    color: var(--text);
                    font-size: {tag_font_size}px;
                    line-height: 1.5;
                    word-break: break-word;
                    font-family: {mono_font};
                    max-height: 400px;
                    overflow-y: auto;
                }}
                .result-empty {{
                    padding: 6px 12px 10px;
                    color: var(--text-muted);
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

                {
            ""
            if self._light_skeleton
            else '''
                /* ===== ECharts 图表容器 ===== */
                .echarts-container {{
                    width: 100%;
                    min-height: 300px;
                    height: auto;
                    margin: 12px 0;
                    border-radius: 10px;
                    background: rgba(22, 27, 34, 0.6);
                    border: 1px solid var(--code-border, rgba(58, 63, 71, 0.6));
                }}
                '''
        }

                /* 内容区图片可点击打开 */
                #content-placeholder img {{
                    cursor: pointer;
                }}
                /* 工具/思考块内的图标小图不应用圆角裁剪和指针样式 */
                #content-placeholder .tool-block img,
                #content-placeholder .think-block img,
                #content-placeholder .think-compact img,
                #content-placeholder .think-streaming img {{
                    border-radius: 0;
                    display: inline;
                    margin: 0;
                    max-width: none;
                    cursor: default;
                }}

                /* 工具/思考区域 - 高度自适应 + 可折叠（正文上方，背景+边框区分） */
                /* ── 性能优化：contain: layout paint 让浏览器把此容器视为独立渲染作用域，
                   父布局变化不会让其子树重排 ── */
                #tool-section {{
                    margin: 0 0 8px 0;
                    contain: layout paint;
                }}
                #tool-separator {{
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    font-size: 11px;
                    color: var(--text-muted);
                    user-select: none;
                    padding: 2px 2px 6px 2px;
                    cursor: pointer;
                    border-radius: 4px;
                    transition: background-color 120ms ease;
                }}
                #tool-separator:hover {{
                    background: var(--panel-soft);
                }}
                /* 自绘 tooltip：hover 时在分隔条下方显示说明 */
                #tool-separator {{
                    position: relative;
                }}
                .tool-separator-tooltip {{
                    position: absolute;
                    left: 50%;
                    top: 100%;
                    transform: translateX(-50%);
                    margin-top: 6px;
                    white-space: nowrap;
                    background: var(--panel, rgba(30,30,32,250));
                    color: var(--text, #ffffff);
                    font-size: 11px;
                    padding: 4px 8px;
                    border-radius: 6px;
                    border: 1px solid var(--border, rgba(128,128,128,0.15));
                    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
                    pointer-events: none;
                    z-index: 100;
                    line-height: 1.4;
                    opacity: 0;
                    transition: opacity 140ms ease;
                }}
                #tool-separator:hover .tool-separator-tooltip {{
                    opacity: 1;
                }}
                /* 子智能体日志按钮自绘 tooltip（代替 HTML title，避免 Chromium 原生 tooltip 在深色模式下显示为黑块） */
                .tool-subagent-log-btn::after {{
                    content: attr(data-tooltip);
                    position: absolute;
                    bottom: calc(100% + 6px);
                    left: 50%;
                    transform: translateX(-50%);
                    white-space: nowrap;
                    background: var(--panel, rgba(30,30,32,250));
                    color: var(--text, #ffffff);
                    font-size: 11px;
                    padding: 4px 8px;
                    border-radius: 6px;
                    border: 1px solid var(--border, rgba(128,128,128,0.15));
                    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
                    pointer-events: none;
                    z-index: 100;
                    line-height: 1.4;
                    opacity: 0;
                    transition: opacity 140ms ease;
                }}
                .tool-subagent-log-btn:hover::after {{
                    opacity: 1;
                }}
                /* 折叠时让 chevron 旋转 */
                #tool-section[data-collapsed="true"] #tool-separator .chevron {{
                    transform: rotate(-90deg);
                }}
                #tool-separator::before,
                #tool-separator::after {{
                    content: '';
                    flex: 1;
                    height: 1px;
                    background: var(--border);
                    opacity: 0.6;
                }}
                #tool-separator .chevron {{
                    display: inline-block;
                    transition: transform 160ms ease;
                    font-size: 9px;
                    opacity: 0.7;
                }}

                @keyframes _streamingPulse {{
                    0%, 100% {{ opacity: 0.3; transform: scale(0.85); }}
                    50% {{ opacity: 1; transform: scale(1.1); }}
                }}
                #tool-content {{
                    /* 固定最大高度，超出时显示滚动条。
                       不设动态大小（不依赖 body 高度比例）。 */
                    max-height: 600px;
                    overflow-y: auto;
                    background: transparent;
                    border: none;
                    border-radius: 6px;
                    padding: 2px 4px;
                    /* 折叠过渡：高度 0 时禁用滚动，避免用户看到残留滚动条 */
                    transition: max-height 200ms ease, opacity 160ms ease;
                }}
                #tool-section[data-collapsed="true"] #tool-content {{
                    max-height: 0;
                    opacity: 0;
                    padding-top: 0;
                    padding-bottom: 0;
                    overflow: hidden;
                }}
                /* 新工具块入场动效 — 仅对"真正新"的块生效
                   （无 data-tool-call-id 且非 restore 的块）。
                   流式/恢复的块已有 data-tool-call-id 或 data-restored，跳过动画避免闪烁。 */
                @keyframes _toolBlockEnter {{
                    from {{ opacity: 0; transform: translateY(4px); }}
                    to {{ opacity: 1; transform: translateY(0); }}
                }}
                #tool-content > .tool-block:not([data-tool-call-id]):not([data-restored]),
                #tool-content > .think-block:not([data-restored]),
                #tool-content > .think-streaming:not([data-restored]) {{
                    animation: _toolBlockEnter 160ms ease-out;
                }}
                #tool-content > .tool-block:first-child,
                #tool-content > .think-block:first-child,
                #tool-content > .think-streaming:first-child {{
                    margin-top: 0;
                }}
                #tool-content > .tool-block:last-child,
                #tool-content > .think-block:last-child,
                #tool-content > .think-streaming:last-child {{
                    margin-bottom: 0;
                }}
                {_STREAMING_DOCK_CSS}
            </style>
        </head>
        <body>
            <div id="tool-section" style="display: none;" data-collapsed="false">
              <div id="tool-separator" role="button" tabindex="0" aria-expanded="true">
                <span class="chevron">▾</span>
                <span>⚙ 工具与思考</span>
                <span class="tool-separator-tooltip">点击折叠/展开工具与思考区</span>
              </div>
              <div id="tool-content"></div>
            </div>
            <div id="content-placeholder"></div>
            <script>
                const collapsibleState = new Map();
                // 简洁模式标志：由 Python 在 _load_skeleton 后通过 JS 同步更新
                window._toolCompactMode = true;

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

                    // 强制重绘，确保第一帧从正确的 startHeight 开始
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
                            // 折叠后保持 overflow hidden，防止内容溢出导致文档高度波动
                            if (!expand) body.style.overflow = 'hidden';
                            else body.style.overflow = '';
                            // 强制重排确保布局已稳定，然后立即报告最终高度
                            // 先 void body.offsetHeight 强制同步布局，再 reportHeight
                            void body.offsetHeight;
                            reportHeight();
                            // ⚠️ 最后释放高度报告抑制，避免 ResizeObserver 在布局计算期间
                            // 被 overflow 等属性变化触发二次报告（50ms 后 viewer 再跳一次）
                            _collapsibleHeightReporting = false;
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

                // ===== Mermaid 渲染（已移除） =====
                // mermaid 图表渲染功能因 QtWebEngine Chromium 版本兼容问题已整体移除。
                // mermaid 代码块（```mermaid）降级为普通代码块，由 Pygments 高亮显示。

                function updateContent(newHtml) {{
                    const container = document.getElementById('content-placeholder');
                    if (container.innerHTML !== newHtml) {{
                        // 记录当前展开状态的思考块
                        const expandedStates = new Map();
                        container.querySelectorAll('.think-block').forEach(block => {{
                            expandedStates.set(block.dataset.blockKey, block.dataset.expanded === 'true');
                        }});

                        // ── 冻结折叠框 CSS transition 避免 DOM 重建时边框闪烁 ──
                        // container.innerHTML = newHtml 会销毁所有已有 DOM 节点，
                        // 重建后 restoreCollapsibleStates 设置 data-expanded 会触发
                        // 220ms 的 border-color transition（灰色→蓝色），导致可见闪烁。
                        // 用 getElementById 复用已有元素，避免多次 updateContent 时残留重复 <style>
                        const _freezeEl = document.getElementById('_fz') || (function(){{
                            var _el = document.createElement('style');
                            _el.id = '_fz';
                            document.head.appendChild(_el);
                            return _el;
                        }})();
                        _freezeEl.textContent = '.cm-collapsible,.cm-collapsible *,.think-block,.think-block *,.tool-block,.tool-block *,.think-streaming,.think-streaming *,.tool-streaming-block,.tool-streaming-block *,.think-compact,.think-compact *{{transition:none!important}}';

                        // 🐛 修复：innerHTML 替换会重置 scrollTop=0 并触发 scroll 事件，
                        // 导致"置顶闪烁"和用户滚动后永久卡顶的问题。
                        // 解决方案：保存 scrollTop 前置位 + _userScrolledWithin 快照，
                        // innerHTML 后立即恢复滚动位置，避免 paint 间隙闪烁。
                        var _scrollThreshold = {AUTO_SCROLL_THRESHOLD};
                        var _prevScrollTop = document.body.scrollTop;
                        var _wasUserScrolled = window._userScrolledWithin;
                        // ── 平滑过渡：新内容以轻微透明度淡入，替代生硬闪烁 ──
                        // 在全量 DOM 替换前设 opacity 略低，替换后在 rAF 中恢复全透明，
                        // CSS transition 驱动平滑淡入效果，减轻 innerHTML 重建的视觉突兀感。
                        // 🛡️ 竞态防护：取消上轮残留的清理定时器
                        if (window._fadeCleanupTimer) {{
                            clearTimeout(window._fadeCleanupTimer);
                        }}
                        // 🐛 修复闪烁：有流式块或折叠框时跳过淡入淡出过渡。
                        // 原因：updateContent 全量重建 DOM 后，think-block / tool-block
                        // 短暂出现在 #content-placeholder（reorganizeContent 迁移前），
                        // opacity 0.88→1 的淡入会放大这个视觉跳变，产生闪烁。
                        // 任何"已有折叠框/工具块"的场景都应跳过淡入，保持视觉稳定。
                        var _hasStreaming = document.querySelector(
                            '#tool-content [data-streaming="true"], ' +
                            '#content-placeholder [data-streaming="true"]'
                        ) !== null;
                        var _hasCollapsible = document.querySelector(
                            '#tool-content .think-block, ' +
                            '#tool-content .tool-block, ' +
                            '#tool-content .think-streaming, ' +
                            '#tool-content .think-compact, ' +
                            '#content-placeholder .think-block, ' +
                            '#content-placeholder .tool-block, ' +
                            '#content-placeholder .think-streaming, ' +
                            '#content-placeholder .think-compact'
                        ) !== null;
                        if (_hasStreaming || _hasCollapsible) {{
                            container.style.opacity = '1';
                            container.style.transition = '';
                        }} else {{
                            // 先切 transition=none，强制 opacity 跳变到 0.88（避免上一轮 transition
                            // 未清理时产生 1→0.88 的淡出动画），再立即恢复 transition 用于后续淡入。
                            container.style.transition = 'none';
                            container.style.opacity = '0.88';
                            void container.offsetHeight;  // 强制同步样式，使跳变立即生效
                            container.style.transition = 'opacity 120ms ease';
                        }}
                        window._suppressScrollEvent = true;
                        try {{
                            container.innerHTML = newHtml;
                        }} catch(e) {{
                            // innerHTML 替换异常时恢复透明度，避免永久半透明残影
                            container.style.opacity = '1';
                            container.style.transition = '';
                            console.error('updateContent innerHTML failed:', e);
                            throw e;
                        }}
                        // 立即恢复滚动位置，防止浏览器在下一次 paint 时呈现 scrollTop=0
                        var _maxScroll = Math.max(0, document.body.scrollHeight - document.body.clientHeight);
                        document.body.scrollTop = Math.min(_prevScrollTop, _maxScroll);

                        // 包裹所有 <table>（不含 .code-table）到可横向滚动的容器中
                        container.querySelectorAll('table:not(.code-table)').forEach(function(table) {{
                            // 已被包裹则跳过（如多次调用 updateContent）
                            if (table.parentNode && table.parentNode.classList.contains('table-scroll-wrapper')) return;
                            var wrapper = document.createElement('div');
                            wrapper.className = 'table-scroll-wrapper';
                            table.parentNode.insertBefore(wrapper, table);
                            wrapper.appendChild(table);
                        }});

                        // 恢复展开状态并移除骨架屏动画
                        container.querySelectorAll('.think-content, .think-streaming-preview').forEach(content => {{
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

                        // ── 恢复 CSS transition（requestAnimationFrame 使浏览器在下一次
                        // 重绘前已发现元素处于 target 状态，不会触发过渡动画） ──
                        requestAnimationFrame(function() {{
                            var _fe = document.getElementById('_fz');
                            if (_fe) _fe.remove();
                        }});

                        // 初始化 ECharts 图表
                        if (window.echarts) {{
                            document.querySelectorAll('.echarts-container').forEach(function(el) {{
                                try {{
                                    var jsonB64 = el.getAttribute('data-echarts-json');
                                    if (!jsonB64 || el._echartInited) return;
                                    // atob() 默认按 ISO-8859-1 解码字节串，会破坏 UTF-8 中文。
                                    // 用 TextDecoder('utf-8') 还原为正确字符串后再 JSON.parse，避免 mojibake。
                                    var _bytes = Uint8Array.from(atob(jsonB64), function(c) {{ return c.charCodeAt(0); }});
                                    var option = JSON.parse(new TextDecoder('utf-8').decode(_bytes));
                                    var chart = echarts.init(el, 'dark');
                                    chart.setOption(option);
                                    el._echartInited = true;
                                    // 卡片 resize 时自适应
                                    var _ro = new ResizeObserver(function() {{ chart.resize(); }});
                                    _ro.observe(el);
                                }} catch(e) {{
                                    console.error('ECharts init error:', e);
                                }}
                            }});
                        }}

                        // 将工具/思考块分流到独立滚动容器（仅简洁模式）
                        // 必须在 _suppressScrollEvent=false 之前执行，
                        // 否则移动 DOM 触发的 scroll 事件会错误标记 _userScrolledWithin=true
                        if (window._toolCompactMode) reorganizeContent();

                        // 🐛 修复：auto-scroll 延后到所有 DOM 操作（table 包裹、折叠框状态恢复、
                        // think-block 展开、ECharts 初始化、reorganizeContent）之后执行，
                        // 确保 scrollHeight 值反映最终渲染结果，避免因 collapsible 展开 /
                        // tool-block restore 等操作在 auto-scroll 后增加高度而导致的
                        // "滚不到底部"问题。
                        // 附加修复：打 auto-scroll 时间戳，让 scroll 事件回调识别
                        // 程序触发的滚动事件（解决 suppress=false 之后异步派发 scroll 的 race）。
                        // 此时 _suppressScrollEvent 仍为 true，所有 scroll 事件仍被抑制。
                        if (!_wasUserScrolled) {{
                            document.body.scrollTop = document.body.scrollHeight;
                            window._userScrolledWithin = false;
                        }} else {{
                            var _wasAtBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < _scrollThreshold;
                            if (_wasAtBottom) {{
                                document.body.scrollTop = document.body.scrollHeight;
                                window._userScrolledWithin = false;
                            }}
                        }}
                        // 同步 _prevScrollTop，让 delta 检测有正确基线
                        window._prevScrollTop = document.body.scrollTop;
                        window._autoScrollTime = performance.now();
                        window._suppressScrollEvent = false;

                        // ── 恢复全透明度：在下一帧前 fade in，CSS transition 驱动平滑淡入 ──
                        // 🛡️ 竞态防护：递增 token + 定时器引用，防止连续 updateContent 时
                        // 上轮清理误清本轮 transition，或清理定时器残留导致 transition 提前消失。
                        // 🐛 修复闪烁：有流式块时跳过 fade-in transition（已在上述同步代码中跳过）
                        if (!_hasStreaming) {{
                            window._fadeToken = (window._fadeToken || 0) + 1;
                            var _thisFadeToken = window._fadeToken;
                            requestAnimationFrame(function() {{
                                container.style.opacity = '1';
                                // 动画完成后清理 transition，避免影响后续 resize 等操作
                                window._fadeCleanupTimer = setTimeout(function() {{
                                    if (window._fadeToken === _thisFadeToken) {{
                                        container.style.transition = '';
                                    }}
                                    window._fadeCleanupTimer = null;
                                }}, 130);
                                // 本轮清理定时器已注册，若下一轮 updateContent 在 130ms 内到达，
                                // 会在开头 clearTimeout 取消此定时器，同时 _fadeToken 递增使回调跳过。
                            }});
                        }}

                        // 使用延迟报告，确保浏览器布局完成
                        setTimeout(() => reportHeight(), 50);
                    }}
                }}
                function reportHeight() {{
                    // 用 body.scrollHeight 获取完整内容高度。
                    // getBoundingClientRect 在 html{{overflow:hidden}} 下
                    // 返回视口高度而非内容高度，导致卡片无法完全展开。
                    const h = document.body.scrollHeight;
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

                // ===== 正文/非正文分区：将工具块/思考块从内容区移到独立可滚动容器 =====
                // 编辑类工具（write/edit/multi_edit）保留在正文中，不迁移到"工具与思考"区域
                // 子智能体/提问类工具（subagent_para/subagent_dag/question）与编辑工具类似，
                // 属于 AI 与用户之间的直接交互结果，保留在正文中体验更连贯。
                var _EDIT_TOOLS_SELECTOR = ':not([data-tool-name="write"]):not([data-tool-name="edit"]):not([data-tool-name="multi_edit"]):not([data-tool-name="subagent_para"]):not([data-tool-name="subagent_dag"]):not([data-tool-name="question"])';

                // 更新"工具与思考"标题（总项数，无勾叉 badge）
                function _updateToolSectionHeader() {{
                    var toolContent = document.getElementById('tool-content');
                    var separator = document.getElementById('tool-separator');
                    if (!separator) return;
                    var total = toolContent ? toolContent.children.length : 0;
                    var titleSpan = separator.querySelector(':scope > span:not(.chevron)');
                    if (titleSpan) {{
                        titleSpan.textContent = total > 0 ? '⚙ 工具与思考 · ' + total + ' 项' : '⚙ 工具与思考';
                    }}
                    // ── 自动展开：流式时有新工具且当前折叠 → 展开 ──
                    var _hasStreaming = document.querySelector('#tool-content [data-streaming="true"]');
                    var _tsEl = document.getElementById('tool-section');
                    if (_hasStreaming && _tsEl && _tsEl.getAttribute('data-collapsed') === 'true') {{
                        _tsEl.setAttribute('data-collapsed', 'false');
                        separator.setAttribute('aria-expanded', 'true');
                    }}
                }}

                function reorganizeContent() {{
                    var container = document.getElementById('content-placeholder');
                    var toolSection = document.getElementById('tool-section');
                    var toolContent = document.getElementById('tool-content');
                    if (!container || !toolContent || !toolSection) return;
                    // 找出容器内所有需要迁移到工具区的块（编辑类工具保留在正文）
                    // 🆕 .think-compact：简洁模式下的思考纯文本行，非折叠框
                    var blocks = container.querySelectorAll(
                        '.tool-block' + _EDIT_TOOLS_SELECTOR + ', ' +
                        '.think-block, .think-streaming, .think-compact, ' +
                        '[data-tool-call-id]' + _EDIT_TOOLS_SELECTOR
                    );
                    if (blocks.length === 0) {{
                        // 容器没有需要迁移的块 —— 若 tool-content 也空就隐藏整个区
                        if (toolContent.children.length === 0) {{
                            toolSection.style.display = 'none';
                            return;
                        }}
                        // tool-content 仍有 data-tool-injected 流式块 / 旧搬移块
                        // （markdown 被缩短、块被删除），仍需刷新 header
                        toolSection.style.display = '';
                        _updateToolSectionHeader();
                        // 坞态（流式中）：自动滚底显示最新活动
                        if (window._streamingActive && window._toolCompactMode) _scrollToolContentToBottom();
                        return;
                    }}
                    // ── 增量搬移：用稳定标识（data-block-key / data-tool-call-id）
                    // 做精确去重 —— 比"count 比对"可靠，因为 updateContent 重写
                    // content-placeholder 后，里面的块全是新 DOM 节点，单纯比数量会
                    // 误判"已搬移过"而漏搬新块，导致工具/思考在正文里也出现。
                    // 🐛 修复吞内容 + 闪烁：think-streaming 用 replaceChild 原地替换。
                    // 原实现 remove+append 导致闪烁；保留旧的导致内容过期被吞。
                    // replaceChild 保持 DOM 位置不变，内容更新为新版，无闪烁无吞内容。
                    var _oldThinkStreaming = toolContent.querySelector('.think-streaming');
                    var _hasNewThinkStreaming = false;
                    Array.prototype.forEach.call(blocks, function(el) {{
                        if (el.classList.contains('think-streaming')) _hasNewThinkStreaming = true;
                    }});
                    if (!_hasNewThinkStreaming && _oldThinkStreaming) {{
                        // reasoning 已完成，#content-placeholder 没有 think-streaming
                        // 安全移除 #tool-content 中残留的旧 think-streaming
                        _oldThinkStreaming.remove();
                    }}
                    // 🐛 FIX: 清理 tool-content 中不再匹配当前内容的已完成 think-block / think-compact
                    // 多轮思考场景：旧完成的折叠框持续堆积在 tool-content 底部不清理。
                    // 收集 content-placeholder 中当前 think 块的 block-key 集合，
                    // 移除 tool-content 中不在集合内的旧 think 块。
                    var _currentThinkKeys = new Set();
                    Array.prototype.forEach.call(blocks, function(el) {{
                        var _bk = el.getAttribute('data-block-key');
                        if (_bk && (el.classList.contains('think-block') || el.classList.contains('think-streaming') || el.classList.contains('think-compact'))) {{
                            _currentThinkKeys.add(_bk);
                        }}
                    }});
                    Array.prototype.forEach.call(toolContent.querySelectorAll('.think-block, .think-compact'), function(el) {{
                        var _bk = el.getAttribute('data-block-key');
                        if (_bk && !_currentThinkKeys.has(_bk) && !el.getAttribute('data-tool-call-id')) {{
                            el.remove();
                        }}
                    }});
                    // 🐛 FIX: 清理 tool-content 中不再匹配当前内容的已完成 tool-block
                    // 多轮工具调用场景：旧已完成 tool-block 堆积在 tool-content 底部不清理。
                    // 收集 content-placeholder 中当前 tool 块的 tool-call-id 集合，
                    // 移除 tool-content 中不在集合内的已完成 tool 块（保留流式进行中的块）。
                    var _currentToolIds = new Set();
                    Array.prototype.forEach.call(blocks, function(el) {{
                        var _tid = el.getAttribute('data-tool-call-id');
                        if (_tid) {{
                            _currentToolIds.add(_tid);
                        }}
                    }});
                    Array.prototype.forEach.call(toolContent.querySelectorAll('.tool-block'), function(el) {{
                        var _tid = el.getAttribute('data-tool-call-id');
                        var _streaming = el.getAttribute('data-streaming');
                        if (_tid && !_currentToolIds.has(_tid) && _streaming !== 'true') {{
                            el.remove();
                        }}
                    }});
                    // ── 排序法保证工具区顺序与 content-placeholder 一致 ──
                    // 建立位置映射：content-placeholder 中每个 block 的序号（所有块都有位置）
                    var posMap = Object.create(null);
                    Array.prototype.forEach.call(blocks, function(el, idx) {{
                        var bk = el.getAttribute('data-block-key');
                        var tid = el.getAttribute('data-tool-call-id');
                        if (bk) posMap['bk:' + bk] = idx;
                        if (tid) posMap['tcid:' + tid] = idx;
                        // 无稳定标识的块（.think-streaming）用其在 blocks 中的序号
                        if (!bk && !tid) el._posIdx = idx;
                    }});
                    // 从正文移除已有稳定标识的重叠块，其余搬移到工具区
                    // 🐛 修复吞内容 + 闪烁：think-streaming 用 replaceChild 原地替换。
                    // 原实现 remove+append 闪烁；保留旧的导致 reasoning 累积内容被吞。
                    // replaceChild 保持 DOM 位置不变，内容更新为新版，无闪烁无吞内容。
                    var moved = false;
                    Array.prototype.forEach.call(blocks, function(el) {{
                        var bk = el.getAttribute('data-block-key');
                        var tid = el.getAttribute('data-tool-call-id');
                        var dup = (bk && toolContent.querySelector('[data-block-key="' + bk + '"]'))
                               || (tid && toolContent.querySelector('[data-tool-call-id="' + tid + '"]'));
                        if (dup) {{
                            if (el.parentNode === container) el.remove();
                        }} else if (el.classList.contains('think-streaming') && _oldThinkStreaming) {{
                            // think-streaming 原地替换：保持 DOM 位置，更新内容
                            if (el.parentNode === container) {{
                                if (_oldThinkStreaming.parentNode) {{
                                    _oldThinkStreaming.parentNode.replaceChild(el, _oldThinkStreaming);
                                }}
                                _oldThinkStreaming = el;  // 更新引用，供后续排序使用
                            }}
                        }} else if (el.parentNode === container) {{
                            toolContent.appendChild(el);
                            moved = true;
                        }}
                    }});
                    // 整体排序：使工具区所有子元素顺序与 content-placeholder 匹配
                    // 🐛 修复闪烁：只在顺序确实变化时才 appendChild 重排。
                    // 原实现无脑 allChildren.forEach(appendChild) 即使顺序未变
                    // 也会移动 DOM 节点，触发浏览器重绘导致已稳定显示的块闪烁。
                    var allChildren = Array.prototype.slice.call(toolContent.children);
                    var _needsReorder = false;
                    var _sortedChildren = allChildren.slice().sort(function(a, b) {{
                        function getPos(el) {{
                            var bk = el.getAttribute('data-block-key');
                            var tid = el.getAttribute('data-tool-call-id');
                            if (bk && posMap['bk:' + bk] !== undefined) return posMap['bk:' + bk];
                            if (tid && posMap['tcid:' + tid] !== undefined) return posMap['tcid:' + tid];
                            if (el._posIdx !== undefined) return el._posIdx;
                            return 1e9;
                        }}
                        return getPos(a) - getPos(b);
                    }});
                    // 检查排序后是否有元素位置变化
                    for (var _i = 0; _i < _sortedChildren.length; _i++) {{
                        if (_sortedChildren[_i] !== allChildren[_i]) {{
                            _needsReorder = true;
                            break;
                        }}
                    }}
                    if (_needsReorder) {{
                        _sortedChildren.forEach(function(el) {{ toolContent.appendChild(el); }});
                    }}
                    toolSection.style.display = toolContent.children.length > 0 ? '' : 'none';
                    if (moved || toolContent.children.length > 0) _updateToolSectionHeader();
                    // 坞态（流式中）：新条目进入后自动滚底
                    if (window._streamingActive && window._toolCompactMode) _scrollToolContentToBottom();
                }}
                // 工具与思考区头部折叠/展开：用 transitionend 精确监听动画结束，
                // 替代不可靠的 setTimeout(220) —— 动画时长若被 CSS 改动会失准
                function _toggleToolSection(sep, evt) {{
                    var toolSection = document.getElementById('tool-section');
                    if (!sep || !toolSection) return;
                    if (evt) {{ evt.stopPropagation(); evt.preventDefault(); }}
                    var collapsed = toolSection.getAttribute('data-collapsed') === 'true';
                    toolSection.setAttribute('data-collapsed', collapsed ? 'false' : 'true');
                    sep.setAttribute('aria-expanded', collapsed ? 'true' : 'false');
                    try {{ sessionStorage.setItem('_toolSectionCollapsed', collapsed ? '0' : '1'); }} catch(_err) {{}}
                    var tc = document.getElementById('tool-content');
                    if (tc) {{
                        var onEnd = function() {{
                            tc.removeEventListener('transitionend', onEnd);
                            reportHeight();
                        }};
                        tc.addEventListener('transitionend', onEnd);
                        // 兜底：transitionend 在 display:none 时可能不触发
                        setTimeout(function() {{ tc.removeEventListener('transitionend', onEnd); reportHeight(); }}, 260);
                    }}
                }}
                document.addEventListener('click', e => {{
                    const sep = e.target.closest('#tool-separator');
                    if (sep) {{
                        _toggleToolSection(sep, e);
                        return;
                    }}
                    const btn = e.target.closest('button[data-action]');
                    if (btn) {{
                        const act = btn.getAttribute('data-action');
                        const b64 = btn.getAttribute('data-copy');
                        const lang = btn.getAttribute('data-lang') || '';
                        if (act === 'copy') try {{ navigator.clipboard.writeText(atob(b64)); }} catch(e) {{}}
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
                    // 图片点击 → 系统默认程序打开
                    const img = e.target.closest('#content-placeholder img');
                    if (img) {{
                        e.stopPropagation();
                        e.preventDefault();
                        console.log('pywebview_action:open_url:' + img.src);
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
                    // 历史会话折叠状态由 _on_js_ready 中的 Python 侧设置
                    // （避免 DOMContentLoaded 时 window._isHistoryCard 尚未就绪的时序问题）。
                    // 此处仅恢复流式会话的 sessionStorage 折叠偏好。
                    try {{
                        var _stored = sessionStorage.getItem('_toolSectionCollapsed');
                        if (_stored === '1') {{
                            var _ts = document.getElementById('tool-section');
                            var _sep = document.getElementById('tool-separator');
                            if (_ts) _ts.setAttribute('data-collapsed', 'true');
                            if (_sep) _sep.setAttribute('aria-expanded', 'false');
                        }}
                    }} catch(_e) {{}}
                    // 无障碍：键盘 Enter / Space 触发折叠切换（WCAG button 模式）
                    var _sepEl = document.getElementById('tool-separator');
                    if (_sepEl) {{
                        _sepEl.addEventListener('keydown', function(kEvt) {{
                            if (kEvt.key === 'Enter' || kEvt.key === ' ') {{
                                _toggleToolSection(_sepEl, kEvt);
                            }}
                        }});
                    }}
                    reportHeight();
                    // 使用防抖的 ResizeObserver，避免频繁触发高度更新
                    let resizeTimeout = null;
                    new ResizeObserver(() => {{
                        // 动画期间跳过高度报告
                        if (_collapsibleHeightReporting) return;
                        if (resizeTimeout) clearTimeout(resizeTimeout);
                        resizeTimeout = setTimeout(() => requestAnimationFrame(reportHeight), 50);
                    }}).observe(document.body);

                    // 简洁模式：工具区不再设动态 max-height，
                    // 内容完全展开，由父级卡片统一处理滚动。
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

                // ===== 用户滚动跟踪：判断用户是否主动滚动卡片内部内容 =====
                // 🐛 修复：当卡片内容超出 MAX_HEIGHT 时，body 出现内部滚动条。
                // 初始状态 scrollTop=0 导致 wasAtBottom 判断失败，auto-scroll 不触发。
                // 跟踪用户主动滚动行为，未滚动时强制 auto-scroll 到底部。
                window._userScrolledWithin = false;
                window._suppressScrollEvent = false;
                // 🐛 修复 race condition：用 scrollTop 差值区分用户滚动 vs 程序自动滚动。
                // 原实现用 200ms 时间窗抑制 auto-scroll 事件，但快速流式时 auto-scroll
                // 频繁触发导致时间窗永不过期，用户所有滚轮事件被永久忽略（卡在底部）。
                // 新方案：用户滚轮单次增量 < 300px，auto-scroll（scrollTop=scrollHeight）
                // 跳变 > 300px。通过 delta 判断替代时间窗，不受流式频率影响。
                window._prevScrollTop = 0;
                document.body.addEventListener('scroll', function() {{
                    var _st = document.body.scrollTop;
                    // 即使被抑制也保持 _prevScrollTop 同步，避免 auto-scroll 后首次
                    // 用户滚动因 _prevScrollTop 陈旧而导致 delta 误判（大跳变）。
                    if (window._suppressScrollEvent) {{
                        window._prevScrollTop = _st;
                        return;
                    }}
                    var delta = Math.abs(_st - window._prevScrollTop);
                    window._prevScrollTop = _st;
                    // 用户滚轮单步增量 ~35-120px（取决于滚轮设置和滚动速度）。
                    // auto-scroll 跳变 > 500px（内容显著增长）。
                    // Page Down / 键盘滚动 增量可能更大（~视口高度），
                    // 但用户触发的也应该标记为主动滚动——将阈值设为 2000px，
                    // 仅过滤 auto-scroll 直接跳到底部的大跳变。
                    if (delta > 0 && delta < 2000) {{
                        window._userScrolledWithin = true;
                    }}
                }});
                // ======================================================

                // ===== JS驱动的蛇形思考动画（替代CSS animation）=====
                // 使用 requestAnimationFrame 持续更新 stroke-dashoffset，
                // 即使 updateContent 重建DOM，新SVG元素在下一帧立即获得正确偏移，
                // 不再因 CSS animation 重启而导致视觉跳跃。
                let _snakeStartTime = null;
                function _animateThinkSnake() {{
                    if (_snakeStartTime === null) _snakeStartTime = performance.now();
                    const elapsed = performance.now() - _snakeStartTime;
                    // 周期 1.5s，完整一圈对应 stroke-dashoffset: 0→-50.265（周长 2π×8 ≈ 50.265）
                    document.querySelectorAll('.think-snake-arc').forEach(el => {{
                        let extraDelay = 0;
                        if (el.classList.contains('think-snake-head')) extraDelay = 350;
                        else if (el.classList.contains('think-snake-body')) extraDelay = 180;
                        const phase = (elapsed + extraDelay) % 1500;
                        const offset = -(phase / 1500) * 50.265;
                        el.setAttribute('stroke-dashoffset', offset);
                    }});
                    requestAnimationFrame(_animateThinkSnake);
                }}
                _animateThinkSnake();

                // ===== 工具区（#tool-content）自动滚底 =====
                // 当工具/思考区有新内容时，自动滚动到底部，让用户始终看到最新状态。
                function _scrollToolContentToBottom() {{
                    var tc = document.getElementById('tool-content');
                    if (!tc) return;
                    // 用户主动向上滚动了工具区则不自动滚底
                    if (tc._userScrolledUp) return;
                    tc.scrollTop = tc.scrollHeight;
                }}
                // 工具区滚动跟踪：用户主动向上滚动时标记，滚到底部时取消标记
                document.getElementById('tool-content')?.addEventListener('scroll', function() {{
                    var tc = this;
                    var atBottom = Math.abs(tc.scrollHeight - tc.scrollTop - tc.clientHeight) < 30;
                    tc._userScrolledUp = !atBottom;
                    if (atBottom) tc._userScrolledUp = false;
                }});
                {_STREAMING_DOCK_JS}

                // ===== 流式工具块：移除超时自动标记 ====
                // 原 _cleanupStuckTools 会在 30 秒后标记工具为"超时未返回结果"，
                // 但工具可能仍在执行中，不应急于标记失败。硬等即可。

                // ===== 深度思考轮播提示（减少等待焦虑，类似 CodeBuddy 设计理念）=====
                // 当 .think-streaming[data-streaming="true"] 存在时，定时轮换显示
                // 说明信息，让用户在等待期间能获取有用提示，而不是只盯着转圈。
                const _thinkTips = [
                    "正在深度思考中...",
                    "分析上下文关联...",
                    "检索相关知识库...",
                    "正在综合推理...",
                    "组织回答结构...",
                    "即将输出结果...",
                    "梳理关键信息...",
                    "对比多个方案...",
                    "校验逻辑完整性...",
                    "回溯历史消息...",
                    "推理最佳路径...",
                    "整合分析结果...",
                    "审查边缘场景...",
                    "串联上下文线索...",
                    "构建最终输出...",
                    "准备呈现答案..."
                ];
                let _tipIndex = 0;
                let _tipTimer = null;

                function _startTipRotation() {{
                    _stopTipRotation();
                    // 首次启动时给文字 span 加上脉冲渐变色 class
                    const el0 = document.querySelector('.think-streaming[data-streaming="true"]');
                    if (el0) {{
                        const s0 = el0.querySelector('span > span:last-child');
                        if (s0) s0.classList.add('think-streaming-tip');
                    }}
                    _tipTimer = setInterval(() => {{
                        const el = document.querySelector('.think-streaming[data-streaming="true"]');
                        if (!el) {{ _stopTipRotation(); return; }}
                        // 🐛 修复：不能用 span:last-child — 外层 span（唯一子元素）也会命中，
                        // 导致 textContent 替换时清掉 spinner SVG。改为精确选择内层文字 span。
                        const tipSpan = el.querySelector('span > span:last-child');
                        if (tipSpan) {{
                            _tipIndex = (_tipIndex + 1) % _thinkTips.length;
                            tipSpan.textContent = _thinkTips[_tipIndex];
                        }}
                    }}, 3500);
                }}

                function _stopTipRotation() {{
                    if (_tipTimer) {{
                        clearInterval(_tipTimer);
                        _tipTimer = null;
                    }}
                }}

                // 通过 MutationObserver 监听 content-placeholder 变化，
                // 自动启停轮播（兼容 updateContent 全量重建 DOM 的场景）。
                const _tipObserver = new MutationObserver(() => {{
                    const hasStreaming = !!document.querySelector('.think-streaming[data-streaming="true"]');
                    if (hasStreaming && !_tipTimer) {{
                        _startTipRotation();
                    }} else if (!hasStreaming && _tipTimer) {{
                        _stopTipRotation();
                    }}
                }});
                const _tipTarget = document.getElementById('content-placeholder');
                if (_tipTarget) {{
                    _tipObserver.observe(_tipTarget, {{ childList: true, subtree: true }});
                }}
                // 也监听 tool-content（思考块被移动到此处）
                const _tipToolContent = document.getElementById('tool-content');
                if (_tipToolContent) {{
                    _tipObserver.observe(_tipToolContent, {{ childList: true, subtree: true }});
                }}
            </script>
        </body>
        </html>
        """
        # 存入全局骨架缓存，避免后续卡片重复构造同一 HTML 模板
        _skeleton_cache[cache_key] = html
        # 以项目根目录为基础 URL，使相对路径图片（如 images/xxx.png）可正确解析
        self.setHtml(html, QUrl.fromLocalFile(_PROJECT_ROOT + "/"))

    # ========== 差量渲染常量 ==========
    # 安全兜底渲染间隔（ms）：无自然边界到达时强制全量渲染
    # 🔧 300ms 基础值，实际值根据流式速度在 150-500ms 间自适应
    _SAFETY_RENDER_INTERVAL = 300
    # 自适应安全渲染间隔参数：
    # - 快速流式（chunk 间隔 < 200ms）：用 150ms，响应更及时
    # - 慢速流式（chunk 间隔 > 500ms）：用 500ms，减少冗余渲染
    # - 默认：300ms
    _ADAPTIVE_INTERVAL_FAST = 150
    _ADAPTIVE_INTERVAL_SLOW = 500
    _ADAPTIVE_THRESHOLD_FAST = 200  # ms
    _ADAPTIVE_THRESHOLD_SLOW = 500  # ms
    # 预编译代码块闭合检测
    _CLEAN_BOUNDARY_CODE_BLOCK_RE = re.compile(r"```[\s]*$")

    @staticmethod
    def _has_reached_clean_boundary(md_text: str) -> bool:
        """检测 markdown 文本是否在自然边界结束

        自然边界 = 段落结束 / think 块闭合 / 代码块闭合。
        在此边界做全量 HTML 渲染可得稳定结果，无需后续重算。

        Returns:
            True: 文本在自然边界结束，适合触发全量渲染
        """
        if not md_text:
            return False
        # 段落结束（双换行）：用原文本检测，因 rstrip 会移除尾部换行
        if md_text.endswith("\n\n"):
            return True
        # think 块 / 代码块闭合：用 rstrip 处理尾部空白
        stripped = md_text.rstrip()
        return stripped.endswith("</think>") or CodeWebViewer._CLEAN_BOUNDARY_CODE_BLOCK_RE.search(stripped) is not None

    def append_chunk(self, text: str):
        if not text:
            return

        self._markdown_text += text

        # [PERF] 更新流式速度跟踪
        now = time.monotonic_ns()
        if self._last_chunk_time > 0:
            elapsed_ms = (now - self._last_chunk_time) / 1_000_000
            if elapsed_ms < self._ADAPTIVE_THRESHOLD_FAST:
                self._current_adaptive_interval = self._ADAPTIVE_INTERVAL_FAST
            elif elapsed_ms > self._ADAPTIVE_THRESHOLD_SLOW:
                self._current_adaptive_interval = self._ADAPTIVE_INTERVAL_SLOW
            else:
                self._current_adaptive_interval = self._SAFETY_RENDER_INTERVAL
        self._last_chunk_time = now

        if not self._is_js_ready:
            return
        if self._streaming and len(text) > 3:
            # 差量渲染：仅在自然边界触发全量渲染，否则靠增量文本 + 安全兜底
            if self._has_reached_clean_boundary(self._markdown_text):
                self._schedule_render(immediate=True)
            else:
                self._schedule_render(immediate=False)
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
            # 内存优化：超长 chunk 截断增量推送，避免单次 JS 调用传输过大数据
            # 全量渲染最终会提供完整格式化后的内容
            if len(text_clean) > 2000:
                text_clean = text_clean[:2000] + "\n\n..."
            js = f"""
            (function() {{
                var text = {json.dumps(text_clean)};
                var c = document.getElementById('content-placeholder');
                if (!c || !text) return;
                // ── 智能段落处理 ──
                // 检测 chunk 是否以换行开头（对应 Markdown 段落分隔），
                // 让增量文本的段落结构与最终 Markdown 渲染对齐，
                // 减少全量渲染时因段落重组引起的视觉跳跃。
                var startsWithNewline = text.length > 0 && (text[0] === '\\n' || text[0] === '\\r');
                if (startsWithNewline) {{
                    // 新段落：去掉前导换行，创建独立 <p>（即使内容为空也创建空段落，
                    // 保持与全量 Markdown 渲染的段落结构一致，避免段落计数偏移）
                    var clean = text.replace(/^[\\n\\r]+/, '');
                    var p = document.createElement('p');
                    p.textContent = clean;
                    c.appendChild(p);
                }} else {{
                    var last = c.lastElementChild;
                    if (last && last.tagName === 'P') {{
                        last.textContent += text;
                    }} else if (last && last.classList.contains('think-block')) {{
                        // 最后是思考块：追加到思考块之后的新段落
                        var p = document.createElement('p');
                        p.textContent = text;
                        c.appendChild(p);
                    }} else {{
                        var p = document.createElement('p');
                        p.textContent = text;
                        c.appendChild(p);
                    }}
                }}
                // 🐛 修复：同步 auto-scroll（无 setTimeout 渲染间隙），
                // 避免浏览器在异步间隙中 paint 出滚动位置不一致的画面。
                // 附加修复：auto-scroll 成功后复位 _userScrolledWithin，
                // 防止用户一次滚轮操作后永久丧失粘性滚底能力。
                // 用 scrollTop 差值识别用户滚动（替代原 200ms 时间窗，避免
                // 快速流式时时间窗永不过期导致用户滚轮被永久忽略）。
                window._suppressScrollEvent = true;
                if (!window._userScrolledWithin) {{
                    document.body.scrollTop = document.body.scrollHeight;
                }} else {{
                    var wasAtBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < {AUTO_SCROLL_THRESHOLD};
                    if (wasAtBottom) {{
                        document.body.scrollTop = document.body.scrollHeight;
                        window._userScrolledWithin = false;
                    }}
                }}
                // 同步 _prevScrollTop，使 delta 检测有正确的基线
                window._prevScrollTop = document.body.scrollTop;
                window._autoScrollTime = performance.now();
                window._suppressScrollEvent = false;
                reportHeightDebounced();
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
        # 根据主题切换代码高亮风格（通用代码块 + 行内 diff）
        try:
            from app.utils.theme_manager import theme_manager
            from app.widgets.render_helpers import set_diff_highlight_style

            _style = "friendly" if theme_manager.is_light_theme() else "dracula"
            set_pygments_style(_style)
            set_diff_highlight_style(_style)
            # 同步缓存图标前缀，避免每次渲染都重新检测主题
            _update_icon_prefix()
            global _CODE_FONT_SIZE
            _CODE_FONT_SIZE = scale_font_size(13)
        except Exception:
            pass

        if not self._streaming:
            # 非流式模式：直接渲染，所有 <think> 都是已完成的
            html_content = _render_markdown_to_html_cached(
                raw_md,
                "",
                compact=self._tool_compact_mode,
            )
            # 将图片相对路径转为绝对 file:/// 路径
            html_content = _resolve_image_src(html_content)
            return html_content

        # 流式模式：仅在最后一个块是 reasoning 且思考尚未被工具调用标记为完成时，去掉其闭合标签
        # 判断标准：markdown 以 </think> 结尾（说明最后一个块恰好是 reasoning）
        streaming_md = raw_md.rstrip()
        if self._streaming and streaming_md.endswith("</think>") and not self._thinking_finalized:
            # 末尾正好是 reasoning 块的闭合标签，去掉它表示该块尚未完成
            streaming_md = streaming_md[: -len("</think>")].rstrip()

        safe_md = _sanitize_incomplete_markdown(streaming_md)
        safe_md = _unwrap_code_blocks_with_context_links(safe_md)
        safe_md = _inject_context_links(safe_md)
        processed_md = _inject_think_cards(safe_md, self._streaming is False, compact=self._tool_compact_mode)
        processed_md = _inject_tool_blocks(processed_md, self._streaming is False, compact=self._tool_compact_mode)
        processed_md = _inject_hook_blocks(processed_md, self._streaming is False)

        # [PERF] 实例级哈希缓存：processed_md 未变时直接返回缓存的 HTML，
        # 跳过 md.convert() + _wrap_code_blocks（最昂贵的步骤）。
        # 命中场景：resize 触发重复渲染、thinking_finalized 状态切换但内容未变、
        # finish_streaming 后的 immediate render 与随后 render 定时器重叠
        processed_hash = hash(processed_md)
        if self._processed_md_hash == processed_hash and self._cached_streaming_html is not None:
            return self._cached_streaming_html

        try:
            md = get_markdown_instance()
            md.reset()
            html_content = md.convert(processed_md)
            html_content = _wrap_code_blocks_with_copy_button_web(html_content)

            # 将图片相对路径转为绝对 file:/// 路径
            html_content = _resolve_image_src(html_content)

            # 流式模式：追加字数统计显示
            if self._streaming:
                html_content = html_content + _CHAR_COUNT_HTML

            # 缓存渲染结果（只存一份，内存开销小）
            self._processed_md_hash = processed_hash
            self._cached_streaming_html = html_content
            self._cached_raw_md_hash = hash(str(self._markdown_text))
            return html_content
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

        # ── 差量渲染策略 ──
        # 增量纯文本已由 _append_text_incremental 即时显示到 DOM，
        # 全量 HTML 渲染仅在以下时机触发，避免 O(n) 逐帧重排：
        # 1. 自然边界触发（由 append_chunk 检测到并传 immediate=True）
        # 2. 安全兜底：2s 内无边界到达，强制渲染确保格式最终正确
        if self._streaming:
            # 流式模式下检查自然边界
            if self._has_reached_clean_boundary(self._markdown_text):
                self._perform_update()
                return
            # 无边界：启安全定时器（仅当未激活时）
            # [PERF] 使用自适应间隔：快速流式用 150ms，慢速用 500ms，默认 300ms
            if not self._render_timer.isActive():
                self._render_timer.start(self._current_adaptive_interval)
        else:
            # 非流式模式（历史加载）：40ms 防抖后渲染
            if not self._render_timer.isActive():
                self._render_timer.start(40)

    def _refresh_viewer_font(self):
        """刷新 viewer 字体样式，响应系统字体设置变化"""
        if not hasattr(self, "_viewer_font_family"):
            return
        self._refresh_viewer_font_css()
        self._schedule_render(immediate=True)

    def _refresh_viewer_font_css(self):
        """刷新字体 CSS 变量，供 render 使用"""
        if not hasattr(self, "_viewer_font_family"):
            return
        font_family = self._viewer_font_family
        font_css = get_font_family_css()
        body_font_size = scale_font_size(14)
        self._viewer_font_css = f"{font_css} font-family: {font_family}, sans-serif; font-size: {body_font_size}px;"

    def refresh_theme(self):
        """刷新主题颜色，响应全局主题切换

        优化：使用 ThemeRefreshCoordinator 全局缓存 JS 字符串。
        同一主题版本内所有 MessageCard 共享同一份 JS 代码，
        避免逐卡重复构建字符串。
        """
        from app.utils.theme_refresh import ThemeRefreshCoordinator

        try:
            from app.utils.theme_manager import theme_manager

            _is_light = theme_manager.is_light_theme()
        except Exception:
            _is_light = False

        # 版本号检查：同一主题版本内跳过 JS 注入
        v = ThemeRefreshCoordinator.get_version()
        if getattr(self, "_last_theme_version", -1) == v:
            return
        self._last_theme_version = v

        theme = current_theme()
        js_code = ThemeRefreshCoordinator.get_or_build_js(theme, _is_light)

        try:
            if self.page():
                self.page().runJavaScript(js_code)
        except RuntimeError:
            pass

    def _perform_update(self):
        try:
            if not self.page():
                return

            # 已完成（结果已到达）的工具 id 集合，供下方 restore 逻辑判断运行框是否可复活
            _finished_ids = list(getattr(self, "_restore_finished_ids", set()) or set())
            _safe_finished = json.dumps(_finished_ids).decode("utf-8")

            # ── 非流式模式（历史加载 / 流式结束）：直接渲染，跳过所有增量比较逻辑 ──
            if not self._streaming:
                self._refresh_viewer_font_css()
                # 如果有懒回调，执行一次获取最终 markdown
                if self._lazy_markdown_cb:
                    self._markdown_text = self._lazy_markdown_cb()
                    self._lazy_markdown_cb = None
                # 🚀 [PERF] 流式结束优化：复用 _cached_streaming_html 跳过重渲染
                # finish_streaming() 触发此非流式分支时，_cached_streaming_html
                # 已有完整的渲染结果（由流式模式的最后一次 _render_markdown_to_html
                # 缓存）。直接复用可避免重复的 markdown→HTML 转换（sanitize +
                # inject_think + inject_tool + md.convert），节省 20-80ms 主线程阻塞。
                # ⚡ 哈希验证：确认 _markdown_text 自缓存以来未改变
                # （防止 _lazy_markdown_cb 在缓存后更新了 _markdown_text）。
                # 移除流式模式追加的字符统计 <div>，它只在流式期间有用。
                if (
                    self._cached_streaming_html is not None
                    and hash(str(self._markdown_text)) == self._cached_raw_md_hash
                ):
                    if _CHAR_COUNT_HTML in self._cached_streaming_html:
                        html_content = self._cached_streaming_html[
                            : self._cached_streaming_html.rfind(_CHAR_COUNT_HTML)
                        ]
                    else:
                        html_content = self._cached_streaming_html
                else:
                    html_content = self._render_markdown_to_html(self._markdown_text)
                self._last_rendered_markdown = self._markdown_text
                self._height_report_pending = True
                # 🐛 修复：非流式路径也会在"流式结束但工具仍在并行执行"时触发
                # （finish_streaming 将 _streaming 置 False 后走此分支）。
                # 此时 DOM 中存在 JS 增量注入的"工具运行折叠框"（data-tool-call-id），
                # 它不在 _content_data 中、也不会被 markdown 重新生成。
                # 若直接 updateContent 会整体替换 content-placeholder 的 innerHTML，
                # 把所有运行框连同已完成的工具结果块一并抹掉，导致
                # "一堆运行框出现后又立马消失，只剩个别框" 的闪灭现象。
                # 因此与流式分支保持一致：先 save 活跃+已完成运行块，updateContent 后用 restore 还原。
                # ♻️ 修复：保存所有 [data-tool-call-id] 块，不仅 data-streaming="true"。
                # 因为 finish_tool_streaming 注入的已完成块 (data-streaming="false")
                # 不在 _content_data 中，不会被 markdown 重新生成，若不保存也会被抹掉。
                # 非流式分支：使用共享的 _build_save_and_restore_js 模板
                # 🚀 [PERF] 使用异步 runJavaScript（带 callback）避免主线程阻塞
                # 等待 WebEngine 处理 DOM。同步版本会卡 30-120ms。
                # 异步后主线程立即释放，WebEngine 在后台解析 HTML 和替换 DOM。
                self.page().runJavaScript(self._build_save_and_restore_js(html_content), lambda _result: None)
                self._last_rendered_html = None
                return

            # ── 以下为流式模式（增量渲染） ──
            # 懒加载：通过回调获取最新 markdown（避免每次 reasoning chunk 都调用 content_to_markdown）
            if self._lazy_markdown_cb:
                fresh_md = self._lazy_markdown_cb()
                self._lazy_markdown_cb = None  # 清除回调，避免后续 set_content 重复转换
                self._markdown_text = fresh_md
            elif self._markdown_text:
                # 🐛 修复：_markdown_text 已通过 set_content（来自 ensure_rendered）
                # 预填充了内容，但 _lazy_markdown_cb 从未被 append_text 设置过。
                # 不应跳过渲染，否则内容永远不显示。
                pass
            else:
                # [PERF-opt] 无新内容：流式模式下跳过全量渲染
                # 工具块/思考块的状态切换已通过增量 JS（_inject_tool_streaming_html /
                # _maybe_finish_thinking_for_tool）处理完毕，无需全量 updateContent
                # 覆盖 DOM，避免"闪灭→再现"闪烁和重复工作。
                return

            # [PERF-opt] 内容变化检测：markdown 未变化时跳过全量渲染
            # 避免定时器空转、回调无变化等场景下的冗余 innerHTML 替换
            if self._markdown_text == self._last_rendered_markdown:
                return

            # 刷新字体 CSS var
            self._refresh_viewer_font_css()

            html_content = self._render_markdown_to_html(self._markdown_text)
            self._last_rendered_markdown = self._markdown_text
            self._last_rendered_html = html_content
            self._height_report_pending = True
            # 🐛 修复：全量渲染后卡住不滚底。流式增量文本（_append_text_incremental）
            # 先触发 reportHeight 消费了 _content_just_loaded 标记，导致 50ms 后
            # updateContent 的 height report 到达时 _content_just_loaded 已为 False，
            # _on_message_card_height_changed 跳过外部滚底。
            # 这里在推 JS 前还原标记，确保全量渲染后的 height report 能触发外部滚底。
            _card = self.parent()
            if _card is not None and _card.__class__.__name__ == "MessageCard":
                _card._content_just_loaded = True
            # 流式分支：复用共享的 save+restore 模板，末尾追加 auto-scroll 逻辑
            # （工具块 restore 后 scrollHeight 可能增加，需要重新判断滚到底）
            auto_scroll_js = (
                "window._suppressScrollEvent=true;"
                "if(!window._userScrolledWithin){"
                "document.body.scrollTop=document.body.scrollHeight;"
                "}else{"
                f"var _prd=Math.abs(document.body.scrollHeight-document.body.scrollTop-document.body.clientHeight);"
                f"if(_prd<{AUTO_SCROLL_THRESHOLD}){{"
                "document.body.scrollTop=document.body.scrollHeight;"
                "window._userScrolledWithin=false;"
                "}}"
                "window._prevScrollTop=document.body.scrollTop;"
                "window._autoScrollTime=performance.now();"
                "window._suppressScrollEvent=false;"
            )
            js_code = self._build_save_and_restore_js(html_content).replace("})();", auto_scroll_js + "})();")
            self.page().runJavaScript(js_code)
            # 释放缓存：HTML 已推送到 WebEngine，Python 端不再保留减少内存占用
            self._last_rendered_html = None

        except RuntimeError:
            pass

    def _build_save_and_restore_js(self, html_content: str) -> str:
        """生成"保存工具块 → 重写内容 → 还原工具块"的 JS 模板（流式/非流式共享）

        为什么需要这个三步流程？
        - 流式期间 `_inject_tool_streaming_html` 会把运行中的工具块直接 append 到
          #tool-content（带 data-tool-call-id 标记），不在 _content_data 中。
        - updateContent() 重写 #content-placeholder 的 innerHTML **不会**影响 #tool-content
          里的块，但**已完成且由 JS 注入**的工具结果块（data-tool-call-id）也不会被 markdown
          重新生成（它们是 JS 端瞬时数据）。若不保存就 updateContent，这些块会被 JS 视为
          "应被保留"，从而产生一闪而没或重复出现的"闪灭"现象。
        - 解决：保存 #tool-content 内所有 data-tool-call-id 块 → updateContent → 按原 idx
          位置还原。已完成块会被"复活"为静态折叠框（移除 data-streaming、标记 data-expanded=false）。

        调用方：流式分支需要在末尾额外追加 auto-scroll 逻辑；非流式分支直接 runJavaScript。

        🐛 修复"残留思考框累积"：
        旧实现同时保存 think-block（用 data-block-key），但 reorganizeContent 在
        updateContent 中已正确处理 think-block 从 markdown 的迁移和清理。save/restore
        把旧 think-block 加回来后，reorganizeContent 的清理被完全撤销，导致多轮思考后
        旧思考框持续堆积在 #tool-content 底部。

        【新策略——谁的孩子谁抱走】
        - think-block / think-streaming：完全交给 reorganizeContent 处理（来自 markdown），
          不参与 save/restore。
        - tool-block with data-streaming="true"（流式进行中）：必须 save/restore，
          因为它们由 JS 注入，不在 markdown 中。
        - tool-block with data-streaming="false"（已完成）：来自 markdown，
          不再 save/restore，由 reorganizeContent 从 #content-placeholder 迁移。
        - 恢复时只做"追加回去"，不再做 streaming→completed 转换（因为已完成块已由
          markdown 渲染 + reorganizeContent 处理）。
        """
        _target_id = self._tool_target_id
        return (
            "(function(){"
            f"var _tc=document.getElementById('{_target_id}');"
            "var _saved=[];"
            # 🐛 修复：保存所有 data-tool-call-id 块（含已完成态），并从 DOM 移除。
            # 【根因】原实现只读取 outerHTML 不移除旧块，导致 reorganizeContent
            # 在 updateContent 内部迁移 markdown 新块时，发现 #tool-content 已有
            # 同 data-tool-call-id 的旧块，误判为重复并移除新块。最终 #tool-content
            # 保留旧块（流式态 tool-streaming-block），append_tool_result 的增量
            # 更新找不到 .cm-collapsible__summary / __body，原地转换失败，
            # 运行框卡在"运行中"。
            # 【修复】保存后立即 el.remove()，让 reorganizeContent 干净迁移新块。
            # restore 时只恢复 data-streaming="true" 的流式块（不在 markdown 中），
            # 已完成块由 markdown 重新生成 + reorganizeContent 迁移。
            # [PERF] 快速路径：_tc 无子元素时跳过 save 循环，减少 JS 执行开销
            "if(_tc&&_tc.children.length){"
            "Array.prototype.forEach.call(Array.prototype.slice.call(_tc.children),function(el,i){"
            "if(el.hasAttribute&&el.hasAttribute('data-tool-call-id')){"
            "_saved.push({id:el.getAttribute('data-tool-call-id'),"
            "html:el.outerHTML,kind:'tool',"
            "streaming:el.getAttribute('data-streaming')||''});"
            "el.remove();}"
            "});}"
            "document.querySelectorAll('[data-tool-injected]').forEach(function(el){el.remove()});"
            f"updateContent({json.dumps(html_content).decode('utf-8')});"
            # 🐛 修复：只恢复流式进行中的块（data-streaming="true"）。
            # 已完成块已由 markdown 重新生成 + reorganizeContent 迁移到 #tool-content。
            # 恢复流式块时检查同 ID 是否已存在（避免与 reorganizeContent 迁移的块重复）。
            # [PERF] _saved 为空时跳过 restore，这是最常见场景（无活跃工具块）
            f"if(_saved.length){{_tc=document.getElementById('{_target_id}');if(_tc){{"
            "_saved.forEach(function(b){"
            "if(b.streaming==='true'&&!document.querySelector('[data-tool-call-id=\"'+b.id+'\"]')){"
            "var _t=document.createElement('div');_t.innerHTML=b.html;"
            "var _bk=_t.firstElementChild;if(_bk){"
            "_bk.removeAttribute('data-tool-injected');"
            "_bk.setAttribute('data-restored','true');"
            "_tc.appendChild(_bk);"
            "}}})"
            "}}"
            # 🐛 修复：save-restore 恢复块后工具区自动滚底
            "if(typeof _scrollToolContentToBottom==='function')_scrollToolContentToBottom();"
            "if(window._toolCompactMode){"
            "var _ts2=document.getElementById('tool-section');"
            "if(_ts2){_ts2.style.display=(_tc&&_tc.children.length>0)?'':'none';_updateToolSectionHeader();}"
            "}"
            "})();"
        )

    def finish_streaming(self):
        self._streaming = False
        # 流式结束：坞态归位（简洁模式下工具区从底部回到顶部）
        self._sync_streaming_dock(False)
        # 🐛 FIX: 流式结束时清除 tool_md_cache，防止缓存过期导致
        # 后续非流式渲染拿到缺内容的旧 <tool> markdown，造成 tool-block
        # 在 reorganizeContent 中因不匹配而被清除或生成重复。
        if hasattr(self, "_tool_md_cache"):
            self._tool_md_cache.clear()
        # 重置思考文本流式标志，防止下一轮对话误判
        self._think_text_streaming_started = False
        self._reasoning_streaming_started = False
        # 流式结束：触发一次最终全量渲染，完成所有未完成的内容
        # 注意：不强制清除 _last_rendered_markdown —— 流式对话期间
        # think-streaming（展开）应保持，只有历史会话加载走非流式分支
        # 才会渲染为 think-block（折叠）。强制重渲染会把流式期间的
        # 展开态误转为折叠态，违背"流式展开 / 历史折叠"的产品预期。
        self._schedule_render(immediate=True)
        # 🚀 [PERF] 延迟工具区折叠，让 WebEngine 先完成 _schedule_render 的
        # 布局/绘制后再执行 DOM 属性操作，分离连续 runJavaScript 阻塞。
        QTimer.singleShot(0, self._auto_collapse_tool_section)

    def _auto_collapse_tool_section(self):
        """流式结束时自动折叠工具与思考区

        在 dock 归位 + 最终渲染完成后折叠工具区，减少"弹到抬头"的视觉跳跃。
        检测到仍有流式进行中的块时跳过折叠，等后续工具结果到达再自然收敛。
        """
        try:
            if self._is_js_ready and self.page():
                self.page().runJavaScript(
                    "(function(){"
                    "var _ts=document.getElementById('tool-section');"
                    "var _sep=document.getElementById('tool-separator');"
                    "if(_ts&&!_ts.querySelector('[data-streaming=\"true\"]')){"
                    "  _ts.setAttribute('data-collapsed','true');"
                    "  if(_sep)_sep.setAttribute('aria-expanded','false');"
                    "  if(typeof reportHeightDebounced==='function')reportHeightDebounced();"
                    "}"
                    "})();"
                )
        except RuntimeError:
            pass

    def _sync_streaming_dock(self, active: bool):
        """同步流式活动坞状态到 JS 端。

        仅简洁模式下 JS 侧 _setStreamingDock 会真正切换 body.streaming-dock，
        非简洁模式注入为空操作。JS 未就绪时跳过——_on_js_ready 会按当前
        _streaming / _is_history 状态兜底同步。
        """
        try:
            if self._is_js_ready and self.page():
                flag = "true" if active else "false"
                self.page().runJavaScript(f"if(typeof _setStreamingDock==='function')_setStreamingDock({flag});")
        except RuntimeError:
            pass

    def _cleanup_render_cache(self):
        """清理渲染缓存，降低内存占用（流式完成后调用）

        流式结束后清空 Python 端缓存字段，但保留 _lazy_markdown_cb 回调，
        以便主题切换或卡片复用时能从 MessageCard._content_data 按需重新生成
        _markdown_text，避免常驻两份等价的文本数据。

        🐛 修复：JS 未就绪时不清除 _lazy_markdown_cb，防止流式完成早于
        _on_js_ready 时丢失内容引用，导致卡片永久空白。
        """
        self._last_rendered_html = None
        self._last_rendered_markdown = ""
        self._markdown_text = ""
        # 不再清除 _lazy_markdown_cb——主题切换 / 卡片复用需要按需从
        # MessageCard._content_data 重新生成 markdown，避免 2 份等价文本常驻。
        # 真正释放卡片时才由 cleanup() 统一置 None。

    @staticmethod
    def clear_global_cache():
        """类方法：清理模块级 LRU 渲染缓存"""
        clear_global_render_cache()

    def get_plain_text(self) -> str:
        """获取消息纯文本内容

        优先返回缓存的 _markdown_text（性能最优），
        若已被 _cleanup_render_cache 清空，则尝试从 _lazy_markdown_cb 重新生成，
        最后兜底从父级 MessageCard 获取 content_to_text 纯文本。
        """
        if self._markdown_text:
            return self._markdown_text
        # _markdown_text 被 _cleanup_render_cache 清空后的兜底
        if self._lazy_markdown_cb:
            try:
                fresh = self._lazy_markdown_cb()
                if fresh:
                    self._markdown_text = fresh
                    return fresh
            except Exception:
                pass
        # 从父 MessageCard 兜底
        p = self.parent()
        while p:
            if hasattr(p, "get_plain_text") and not isinstance(p, CodeWebViewer):
                try:
                    return p.get_plain_text()
                except Exception:
                    pass
                break
            p = p.parent()
        return ""

    def get_html(self) -> str:
        """获取消息的完整 HTML 页面（非流式/导出用）

        优先返回已缓存的 _last_rendered_html（含工具块等全量 DOM 等效 HTML），
        否则从 _markdown_text 或 _lazy_markdown_cb 重新生成。

        注意：_last_rendered_html 在流式渲染注入 JS 后会被清空以节省内存，
        因此导出时多数走 markdown→HTML 路径。
        """
        # 优先：已缓存的完整 HTML 直接返回（含工具展开块等，最完整）
        if self._last_rendered_html:
            return self._last_rendered_html
        # 次优：从 _markdown_text 转换
        md = self._markdown_text
        if not md and self._lazy_markdown_cb:
            try:
                md = self._lazy_markdown_cb()
                if md:
                    self._markdown_text = md
            except Exception:
                pass
        if md:
            return self._convert_md_to_html(md)
        return ""

    def _show_context_menu(self, pos):
        """显示大模型卡片右键菜单：查看差异、复制"""
        from app.utils.design_tokens import Colors

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {Colors.CARD_BG_SOLID};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 8px 32px 8px 12px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {scale_font_size(13)}px;
                {get_font_family_css()}
            }}
            QMenu::item:selected {{
                background-color: {Colors.HOVER_BG};
                border-radius: 4px;
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {Colors.BORDER};
                margin: 4px 8px;
            }}
        """)

        # 查看差异
        diff_action = menu.addAction(get_icon("差异对比"), "查看差异")
        diff_action.triggered.connect(self._request_view_diff)

        menu.addSeparator()

        # 复制
        copy_action = menu.addAction(get_icon("复制"), "复制")
        copy_action.triggered.connect(self._copy_to_clipboard)

        # 导出
        export_action = menu.addAction(get_icon("导入"), "导出")
        export_action.triggered.connect(self._export_message)

        menu.exec_(self.mapToGlobal(pos))

    def _request_view_diff(self):
        """请求查看差异 - 向上查找 MessageCard 并发出 cardDiffRequested 信号"""
        parent = self.parent()
        while parent:
            if hasattr(parent, "cardDiffRequested"):
                # 通知父组件显示卡片差异
                if parent._round_index is not None and parent._message_index is not None:
                    parent.cardDiffRequested.emit(parent._round_index, parent._message_index)
                break
            parent = parent.parent()

    def _copy_to_clipboard(self):
        """复制内容到剪贴板（使用系统原生 API）

        🐛 修复：使用 get_plain_text() 替代直接读 _markdown_text，
        因为 _cleanup_render_cache 会将 _markdown_text 清空。
        get_plain_text() 会通过 _lazy_markdown_cb 或父 MessageCard 自动兜底。
        """
        text = self.get_plain_text()
        if not text:
            return
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
        except Exception:
            # 兜底：使用 PyQt5 剪贴板
            from PyQt5.QtWidgets import QApplication

            clipboard = QApplication.clipboard()
            clipboard.setText(text)

    def _get_default_filename(self) -> str:
        """生成默认导出文件名：会话名_时间戳"""
        from datetime import datetime

        session_name = "消息"
        try:
            # 沿父链向上查找主窗口（self.window() 返回 ToolPopupDialog，没有 session_manager）
            parent_widget = self.parent()
            while parent_widget is not None:
                if hasattr(parent_widget, "session_manager"):
                    session = parent_widget.session_manager.get_current_session()
                    if session:
                        name = (session.topic_summary or session.name or "").strip()
                        if name:
                            session_name = name
                    break
                parent_widget = parent_widget.parent()
        except Exception:
            pass
        # 移除文件名非法字符
        invalid_chars = r'<>:"/\|?*'
        for c in invalid_chars:
            session_name = session_name.replace(c, "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{session_name}_{ts}"

    def _export_message(self):
        """导出消息为 Markdown、HTML 或 PNG 图片文件

        🐛 修复：使用 get_plain_text()/get_html() 替代直接读 _markdown_text，
        因为 _cleanup_render_cache 会将 _markdown_text 清空。
        get_plain_text() 会通过 _lazy_markdown_cb 或父 MessageCard 自动兜底。
        """
        from PyQt5.QtWidgets import QFileDialog

        default_name = self._get_default_filename()
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self, "导出消息", default_name, "PNG 图片 (*.png);;Markdown (*.md);;HTML (*.html)"
        )

        if not file_path:
            return

        try:
            is_png = "PNG" in selected_filter or file_path.lower().endswith(".png")
            is_html = "HTML" in selected_filter or file_path.lower().endswith(".html")
            if is_png:
                if not file_path.lower().endswith(".png"):
                    file_path += ".png"
                self._export_as_image(file_path)
            elif is_html:
                if not file_path.lower().endswith(".html"):
                    file_path += ".html"
                html_content = self.get_html()
                if not html_content:
                    logger.warning("导出 HTML 失败：无法获取消息内容")
                    self._show_save_error("无法获取消息内容")
                    return
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(html_content)
                logger.info(f"消息已导出到: {file_path}")
                self._show_save_success(file_path)
            else:
                if not file_path.lower().endswith(".md"):
                    file_path += ".md"
                md_content = self.get_plain_text()
                if not md_content:
                    logger.warning("导出 Markdown 失败：无法获取消息内容")
                    self._show_save_error("无法获取消息内容")
                    return
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(md_content)
                logger.info(f"消息已导出到: {file_path}")
                self._show_save_success(file_path)
        except Exception as e:
            logger.error(f"导出失败: {e}")
            self._show_save_error(str(e))

    def _run_js_sync(self, js_code: str, timeout_ms: int = 2000) -> str:
        """同步执行 JavaScript 并返回结果"""
        from PyQt5.QtCore import QEventLoop, QTimer

        page = self.page()
        if not page:
            return ""

        result = [None]
        loop = QEventLoop()

        def callback(val):
            result[0] = val
            if loop.isRunning():
                loop.quit()

        page.runJavaScript(js_code, callback)
        QTimer.singleShot(timeout_ms, lambda: loop.quit() if loop.isRunning() else None)
        loop.exec_()

        return result[0] or ""

    def _get_card_bg_color(self) -> "QColor":
        """沿父链查找 MessageCard，获取卡片背景色（强制实心化）

        PyQt5 的 QColor() 字符串构造不支持 "rgba(r, g, b, a)" 格式
        (isValid()=False)，需要手动解析提取 r/g/b 后用 QColor(r, g, b) 构造。
        """
        import re

        from PyQt5.QtGui import QColor

        parent = self.parent()
        while parent:
            if hasattr(parent, "_theme") and isinstance(parent._theme, dict) and "bg" in parent._theme:
                bg = parent._theme["bg"]
                # 1. 先试标准颜色字符串（#hex、named color 等）
                color = QColor(bg)
                if color.isValid():
                    color.setAlpha(255)
                    return color
                # 2. 兜底：手动解析 rgba(r, g, b[, a]) / rgb(r, g, b) 字符串
                m = re.match(
                    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d.]+\s*)?\)",
                    bg,
                )
                if m:
                    return QColor(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                # 3. 主题色字符串无效且无法解析，跳出用兜底
                break
            parent = parent.parent()
        # 兜底：暗色主题背景
        return QColor("#2B2B2B")

    def _compose_with_solid_bg(self, source: "QPixmap", width: int, height: int) -> "QPixmap":
        """在 QPixmap 上填充实心卡片背景，再合成 source

        Args:
            source:  从 widget.grab() 拿到的 pixmap（可能含透明区）
            width:   目标宽度
            height:  目标高度

        Returns:
            填充实心卡片背景 + 绘制 source 的合成 pixmap
        """
        from PyQt5.QtGui import QPainter, QPixmap

        if width <= 0 or height <= 0:
            return source
        result = QPixmap(width, height)
        result.fill(self._get_card_bg_color())
        if not source.isNull():
            painter = QPainter(result)
            painter.drawPixmap(0, 0, source)
            painter.end()
        return result

    def _capture_full_content(self) -> "QPixmap":
        """截取消息的完整内容为一张大图（实心背景 + 内容合成）

        策略：临时解除 body max-height 限制并撑大视图到完整内容高度，
        让全部内容一次性渲染可见，然后通过一次 grab() 截取整张图片，
        最后用 QPixmap 主动填充实心卡片背景 + 合成 grab 结果。

        相比原实现：
        - 主动 QPixmap.fill 卡片色（实心），避免半透明 rgba 在 PNG 中呈现为黑
        - 单次 200ms 等待 + processEvents() 强制布局（替代 400ms×2）
        - grab(QRect) 显式指定区域，避免 setFixedHeight 后未生效导致漏抓
        """
        import json as json_mod

        from PyQt5.QtCore import QEventLoop, QPoint, QRect, QTimer
        from PyQt5.QtWidgets import QApplication

        page = self.page()
        view_w = self.width()
        cur_h = self.height()

        # 1. 获取完整内容高度
        dims_raw = self._run_js_sync("JSON.stringify({sh: document.body.scrollHeight})")
        if not dims_raw:
            # 拿不到高度 → 兜底：直接 grab + 强制实心背景
            return self._compose_with_solid_bg(self.grab(), view_w, cur_h)

        try:
            scroll_h = json_mod.loads(dims_raw).get("sh", 0)
        except Exception:
            scroll_h = 0

        # 2. 短消息：内容不超出 → 不展开
        if scroll_h <= cur_h or scroll_h <= 0:
            grabbed = self.grab()
            return self._compose_with_solid_bg(
                grabbed,
                view_w,
                max(cur_h, grabbed.height() if not grabbed.isNull() else cur_h),
            )

        # 3. 长消息：临时展开
        old_styles = self._run_js_sync("""
            var s = document.body.style;
            JSON.stringify({maxHeight: s.maxHeight, overflowY: s.overflowY})
        """)
        self._run_js_sync("""
            document.body.style.maxHeight = 'none';
            document.body.style.overflowY = 'hidden';
        """)

        orig_height = self.height()
        target_h = scroll_h + 20
        self.setFixedHeight(target_h)
        self.update()
        # ★ 强制布局：让 setFixedHeight 真的撑大 widget
        QApplication.processEvents()

        self._run_js_sync("window.scrollTo(0, 0);")

        # ★ 单次 200ms 等待（替代 400ms×2）
        stable_loop = QEventLoop()
        QTimer.singleShot(200, stable_loop.quit)
        stable_loop.exec_()

        # 4. 显式 grab 整个目标区域
        full_pix = self.grab(QRect(QPoint(0, 0), self.size()))

        # 5. 合成：实心背景 + grab 内容
        final_w = full_pix.width() if not full_pix.isNull() else view_w
        final_h = max(target_h, full_pix.height() if not full_pix.isNull() else 0)
        result = self._compose_with_solid_bg(full_pix, final_w, final_h)

        # 6. 恢复视图和样式
        self.setFixedHeight(orig_height)
        if old_styles:
            try:
                prev = json_mod.loads(old_styles)
                js_restore = f"""
                    document.body.style.maxHeight = {json_mod.dumps(prev.get("maxHeight", ""))};
                    document.body.style.overflowY = {json_mod.dumps(prev.get("overflowY", "auto"))};
                    window.scrollTo(0, 0);
                """
                self._run_js_sync(js_restore)
            except Exception:
                self._run_js_sync("window.scrollTo(0, 0);")

        if result.isNull() or result.width() <= 0 or result.height() <= 0:
            return self.grab()
        return result

    def _split_and_stitch(self, pixmap: "QPixmap", max_cols: int = 6) -> "QPixmap":
        """将纵向长图均匀分段后水平拼接为宽高合理的矩形图

        把 pixmap 按高度均匀切成 N 段，从左到右水平拼接。
        N 的选择使最终拼接图的宽高比尽量接近 3:2。
        """
        from PyQt5.QtGui import QPainter, QPixmap

        w = pixmap.width()
        h = pixmap.height()
        if w <= 0 or h <= 0:
            return pixmap

        # 计算最佳列数：使拼接后的宽高比接近目标比例
        target_ratio = 1.5  # 3:2
        best_cols = 1
        best_diff = float("inf")

        for cols in range(2, min(max_cols + 1, (h + w - 1) // w + 1)):
            strip_h = h / cols
            ratio = (cols * w) / strip_h
            diff = abs(ratio - target_ratio)
            if diff < best_diff:
                best_diff = diff
                best_cols = cols

        if best_cols <= 1:
            return pixmap

        # 均匀切分（最后一段包含余量）
        strip_h = h // best_cols
        segments = []
        for i in range(best_cols):
            y = i * strip_h
            if i == best_cols - 1:
                seg = pixmap.copy(0, y, w, h - y)
            else:
                seg = pixmap.copy(0, y, w, strip_h)
            if not seg.isNull():
                segments.append(seg)

        if len(segments) <= 1:
            return pixmap

        # 水平拼接
        total_w = sum(s.width() for s in segments)
        max_h = max(s.height() for s in segments)
        result = QPixmap(total_w, max_h)
        painter = QPainter(result)
        x = 0
        for seg in segments:
            painter.drawPixmap(x, 0, seg)
            x += seg.width()
        painter.end()

        return result

    def _export_as_image(self, file_path: str):
        """将当前消息内容导出为 PNG 图片（全内容截取 + 智能拼接）"""

        # 1. 截取全内容大图
        full = self._capture_full_content()
        if full.isNull():
            raise RuntimeError("截图生成失败，无法获取渲染内容")

        # 2. 若内容超出视图高度，均匀分段后水平拼接为矩形图
        if full.height() > full.width() * 1.5:
            result = self._split_and_stitch(full)
        else:
            result = full

        result.save(file_path, "PNG")
        logger.info(f"消息已导出为图片: {file_path}")
        self._show_save_success(file_path)

    def _convert_md_to_html(self, markdown_text: str) -> str:
        """将 Markdown 文本转换为独立 HTML 页面"""
        from markdown import Markdown

        md = Markdown(extensions=["fenced_code", "codehilite", "tables"])
        body_html = md.convert(markdown_text)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>消息导出</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; color: #333; }}
        pre {{ background: #f5f5f5; padding: 12px; border-radius: 6px; overflow-x: auto; }}
        code {{ background: #f0f0f0; padding: 2px 4px; border-radius: 3px; font-size: 0.9em; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; }}
        img {{ max-width: 100%; }}
        blockquote {{ border-left: 4px solid #ddd; margin-left: 0; padding-left: 16px; color: #666; }}
        h1, h2, h3, h4 {{ margin-top: 24px; }}
    </style>
</head>
<body>
{body_html}
</body>
</html>"""

    def _show_save_success(self, file_path: str):
        """显示保存成功提示"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition

            main_window = self.window()
            if main_window:
                InfoBar.success(
                    "文件已导出",
                    file_path,
                    duration=3000,
                    parent=main_window,
                    position=InfoBarPosition.BOTTOM,
                )
        except Exception:
            pass

    def _show_save_error(self, error_msg: str):
        """显示保存失败提示"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition

            main_window = self.window()
            if main_window:
                InfoBar.error(
                    "导出失败",
                    error_msg,
                    duration=3000,
                    parent=main_window,
                    position=InfoBarPosition.BOTTOM,
                )
        except Exception:
            pass

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
        # 内部 PlainTextViewer(QWidget) 本身不可滚动，始终转发到外部
        try:
            scroll_area = self.parent().parent()._parent.chat_scroll_area
            if scroll_area:
                vbar = scroll_area.verticalScrollBar()
                if vbar and vbar.minimum() != vbar.maximum():
                    delta = event.angleDelta().y()
                    vbar.setValue(vbar.value() - delta // 2)
                    event.accept()
                    return
        except Exception:
            pass
        super().wheelEvent(event)

    def cleanup(self):
        """
        清理 CodeWebViewer 持有的资源，防止内存泄漏。
        应该在删除 viewer 前调用，或者在 deleteLater 中自动调用。
        """
        # 🔧 内存修复：移除全局事件过滤器，防止 QApplication 持有对已销毁
        # CodeWebViewer 实例的引用，导致 GC 无法回收且事件循环误调用已释放对象
        try:
            from PyQt5.QtWidgets import QApplication

            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass

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
            if hasattr(self._page, "codeActionRequested"):
                self._page.codeActionRequested.disconnect()
            if hasattr(self._page, "contextActionRequested"):
                self._page.contextActionRequested.disconnect()
            if hasattr(self._page, "heightReported"):
                self._page.heightReported.disconnect()
            if hasattr(self._page, "contentReady"):
                self._page.contentReady.disconnect()
            if hasattr(self._page, "toolDiffRequested"):
                self._page.toolDiffRequested.disconnect()
            if hasattr(self._page, "subAgentLogRequested"):
                self._page.subAgentLogRequested.disconnect()
            if hasattr(self._page, "saveFileRequested"):
                self._page.saveFileRequested.disconnect()
        except Exception:
            pass

        # 清理流式输出和渲染缓存
        self._streaming = False
        self._markdown_text = ""
        self._last_rendered_html = ""
        self._last_rendered_markdown = ""
        self._processed_md_hash = None
        self._cached_streaming_html = None
        self._cached_raw_md_hash = 0
        self._lazy_markdown_cb = None  # 清理懒回调引用，释放 content_data
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
            if hasattr(self, "_page"):
                self._page.deleteLater()
                del self._page
        except (RuntimeError, AttributeError):
            pass

        # 共享 profile 为全局单例，不可销毁；仅解除引用。
        # page 已在上方单独 deleteLater 释放渲染资源（DOM/JS heap/图层）。
        if hasattr(self, "_profile"):
            self._profile = None

        # 清理代码块缓存
        if hasattr(self, "_code_block_cache"):
            self._code_block_cache.clear()
            self._code_block_cache = None

        # 清理滚动位置
        self._last_scroll_position = 0

    def deleteLater(self):
        self.cleanup()
        super().deleteLater()


class PlainTextViewer(QWidget):
    contentHeightChanged = pyqtSignal(int)

    # 用户消息卡片最大高度（px）：超过此高度启用 QTextEdit 内部滚动条
    # 约可容纳 13 行 14px 文本，平衡阅读完整性与卡片视觉占位
    MAX_HEIGHT = 300

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
        self.text_edit.setContextMenuPolicy(Qt.CustomContextMenu)
        self.text_edit.customContextMenuRequested.connect(self._show_context_menu)
        # 显式声明：超出可视区域时自动显示垂直滚动条
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._apply_text_style()
        layout.addWidget(self.text_edit)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(40)
        # 用户消息卡片最大高度：超出后由 QTextEdit 内部滚动条处理滚动
        self.setMaximumHeight(self.MAX_HEIGHT)

    def _apply_text_style(self):
        """应用文本样式（从 Colors token 读取颜色）

        滚动条复用项目统一的 get_unified_scrollbar_style，与
        tab_panel / project_selector / settings 等列表的视觉风格保持一致。
        """
        font_css = get_font_family_css()
        text_color = Colors.USER_CARD_TEXT
        self.text_edit.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                border: none;
                {font_css}
                color: {text_color};
                font-size: {scale_font_size(14)}px;
                line-height: 1.5;
                selection-background-color: rgba(102, 198, 255, 0.28);
            }}
            {get_unified_scrollbar_style(6)}
        """)

    def refresh_theme(self):
        """主题切换后刷新文本颜色"""
        self._apply_text_style()

    def append_chunk(self, text: str):
        self._text += text
        self.text_edit.setPlainText(self._text)
        # 设置文档宽度以确保正确计算换行
        vp_width = self.text_edit.viewport().width()
        if vp_width > 0:
            self.text_edit.document().setTextWidth(vp_width)
        self._schedule_update_height()

    def finish_streaming(self):
        self._schedule_update_height()

    def _schedule_update_height(self):
        """🛡️ 安全的延迟高度更新

        使用 lambda 包装 + try/except 保护，防止 PlainTextViewer 被 deleteLater()
        销毁后定时器回调仍访问已释放的 C++ 对象（text_edit）导致段错误。
        """
        QTimer.singleShot(10, lambda: self._safe_update_height())

    def _safe_update_height(self):
        """带存活性检查的 _update_height"""
        try:
            # 检查 C++ 对象是否已被销毁
            if sip.isdeleted(self.text_edit):
                return
            self._update_height()
        except RuntimeError:
            pass

    def get_plain_text(self) -> str:
        return self._text

    def set_text(self, text: str):
        self._text = text
        self.text_edit.setPlainText(text)
        # 设置文档宽度以确保正确计算换行
        vp_width = self.text_edit.viewport().width()
        if vp_width > 0:
            self.text_edit.document().setTextWidth(vp_width)
        self._schedule_update_height()

    def _update_height(self):
        """强制 QTextEdit 重新布局后再计算高度"""
        # 先让 QTextEdit 重新布局
        self.text_edit.update()
        self.text_edit.document().markContentsDirty(0, self.text_edit.document().characterCount())

        # 强制更新几何信息
        self.text_edit.ensurePolished()

        doc = self.text_edit.document()
        h = int(math.ceil(doc.size().height())) + 16  # padding

        # 限制最大高度：内容超出 MAX_HEIGHT 后由 QTextEdit 内部滚动条处理滚动
        h = max(40, min(h, self.MAX_HEIGHT))

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
        if hasattr(self, "text_edit") and self.text_edit:
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

    def _show_context_menu(self, pos):
        """显示用户卡片右键菜单：复制、撤销、删除"""
        from app.utils.design_tokens import Colors

        menu = QMenu(self.text_edit)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {Colors.CARD_BG_SOLID};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 8px 32px 8px 12px;
                color: {Colors.TEXT_PRIMARY};
                font-size: {scale_font_size(13)}px;
                {get_font_family_css()}
            }}
            QMenu::item:selected {{
                background-color: {Colors.HOVER_BG};
                border-radius: 4px;
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {Colors.BORDER};
                margin: 4px 8px;
            }}
        """)

        # 复制
        copy_action = menu.addAction(get_icon("复制"), "复制")
        copy_action.triggered.connect(lambda: self._copy_to_clipboard())

        menu.addSeparator()
        # 撤销
        undo_action = menu.addAction(get_icon("撤销"), "撤销到这里")
        undo_action.triggered.connect(lambda: self._request_undo())

        menu.addSeparator()

        # 删除
        delete_action = menu.addAction(get_icon("删除"), "删除")
        delete_action.triggered.connect(lambda: self._request_delete())

        menu.exec_(self.text_edit.mapToGlobal(pos))

    def _copy_to_clipboard(self, copy_selection: bool = True):
        """复制内容到剪贴板

        Args:
            copy_selection: 为 True 时优先复制选中文本（上下文菜单标准行为），
                            无选中时降级复制全文。
                            为 False 时直接复制全文（工具栏按钮行为）。
        """
        from PyQt5.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if copy_selection:
            cursor = self.text_edit.textCursor()
            selected = cursor.selectedText()
            if selected:
                clipboard.setText(selected)
                return
        clipboard.setText(self._text)

    def _convert_text_to_html(self, text: str) -> str:
        """将纯文本转换为独立 HTML 页面"""
        import html as html_mod

        escaped = html_mod.escape(text)
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>消息导出</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; color: #333; }}
        pre {{ background: #f5f5f5; padding: 16px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }}
    </style>
</head>
<body>
<pre>{escaped}</pre>
</body>
</html>"""

    def _show_save_success(self, file_path: str):
        """显示保存成功提示"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition

            main_window = self.window()
            if main_window:
                InfoBar.success(
                    "文件已导出",
                    file_path,
                    duration=3000,
                    parent=main_window,
                    position=InfoBarPosition.BOTTOM,
                )
        except Exception:
            pass

    def _show_save_error(self, error_msg: str):
        """显示保存失败提示"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition

            main_window = self.window()
            if main_window:
                InfoBar.error(
                    "导出失败",
                    error_msg,
                    duration=3000,
                    parent=main_window,
                    position=InfoBarPosition.BOTTOM,
                )
        except Exception:
            pass

    def _request_undo(self):
        """请求撤销 - 通知父组件"""
        # 向上查找 MessageCard 并发出 undoRequested 信号
        parent = self.parent()
        while parent:
            if hasattr(parent, "undoRequested"):
                parent.undoRequested.emit()
                break
            parent = parent.parent()

    def _request_delete(self):
        """请求删除 - 通知父组件"""
        # 向上查找 MessageCard 并发出 deleteRequested 信号
        parent = self.parent()
        while parent:
            if hasattr(parent, "deleteRequested"):
                parent.deleteRequested.emit()
                break
            parent = parent.parent()


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
    reviewRequested = pyqtSignal(int, int)  # round_index, message_index — 用户点击页脚 Review 按钮时触发
    saveFileRequested = pyqtSignal(str, str)  # code, lang
    lazyRenderCompleted = pyqtSignal()  # 懒渲染完成信号，用于通知滚动保持
    modelLabelClicked = pyqtSignal(str, str)  # model_name, config_id — 用户点击页脚模型标签时触发

    def __init__(
        self,
        role: str,
        timestamp: str = None,
        parent=None,
        error: bool = False,
        reasoning_content: str = "",
        model_name: str = None,
        provider_name: str = None,
        config_id: str = None,
    ):
        super().__init__(parent)
        self._parent = parent
        self.role = role
        self.model_name = model_name
        self.provider_name = provider_name
        self._provider_config_id = config_id  # UUID key in _valid_configs, for precise provider lookup
        self.timestamp = timestamp or datetime.now().strftime("%m-%d %H:%M")
        # 历史数据 timestamp 格式为 %Y-%m-%d %H:%M:%S，转为 %m-%d %H:%M
        if self.timestamp and len(self.timestamp) >= 19:
            try:
                dt = datetime.strptime(self.timestamp[:19], "%Y-%m-%d %H:%M:%S")
                self.timestamp = dt.strftime("%m-%d %H:%M")
            except ValueError:
                self.timestamp = self.timestamp[:14]
        # 助手卡片初始不显示时间，流完成后再设模型名称或时间
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
        # 底部元信息栏（助手卡片）
        self._footer_bar: Optional[QWidget] = None
        self._footer_model_label: Optional[QLabel] = None
        self._footer_elapsed_label: Optional[QLabel] = None
        self._footer_tokens_label: Optional[QLabel] = None
        self._footer_diff_stats_label: Optional[QLabel] = None
        self._footer_review_btn: Optional[QLabel] = None
        self._footer_sep1: Optional[QLabel] = None
        self._footer_sep2: Optional[QLabel] = None
        # 耗时实时计时器
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed_display)
        self._elapsed_start_time: Optional[float] = None
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
        # 🆕 流式高度防抖：减少频繁 height report 导致的 viewer resize 抖动
        self._stream_height_timer = QTimer(self)
        self._stream_height_timer.setSingleShot(True)
        self._stream_height_timer.setInterval(80)
        self._stream_height_timer.timeout.connect(self._apply_debounced_height)
        self._debounced_target_height = 40
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
        self._finished_streaming_ids: set = set()  # 防止 streaming 状态回退
        # 工具参数首次到达跟踪：每个 tool_call_id 第一次 update_tool_streaming 时
        # 触发"标记当前思考块为完成"，避免 reasoning→tool_call 切换时思考块残留"思考中"
        self._tool_args_first_seen_ids: set = set()
        self._pending_content: Optional[str] = None
        self._reasoning_total_len = 0  # reasoning 内容总长度计数器，避免每次遍历
        self._viewer_container = QWidget(self)
        self._viewer_layout = QVBoxLayout(self._viewer_container)
        self._viewer_layout.setContentsMargins(0, 0, 0, 0)
        self._setup_ui()

    def _build_theme(self, role: str, error: bool = False) -> Dict[str, str]:
        Colors.refresh()

        # 获取当前窗口透明度（OpacitySlider 控制），用于调整卡片背景色 alpha
        try:
            win = self.window()
            if win is not None:
                _win_opacity = win.windowOpacity()
            else:
                _win_opacity = 1.0
        except Exception:
            _win_opacity = 1.0

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
                "avatar": "User",
                "title": "User",
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

        # 按窗口透明度调整背景色 alpha
        if _win_opacity < 1.0 and theme["bg"].startswith("rgba("):
            import re

            m = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)", theme["bg"])
            if m:
                r, g, b, a = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                new_a = max(0, min(255, int(a * _win_opacity)))
                theme["bg"] = f"rgba({r}, {g}, {b}, {new_a})"

        if error:
            # 检测深浅色模式，选择合适的错误配色
            try:
                from app.utils.theme_manager import theme_manager

                _is_light = theme_manager.is_light_theme()
            except Exception:
                _is_light = False
            if _is_light:
                theme["bg"] = "#FFF5F5"  # 浅粉底
                theme["border"] = "#FCA5A5"  # 浅红边框
                theme["accent"] = "#DC2626"  # 深红强调
            else:
                theme["bg"] = "#2A1F1F"  # 暗红褐底
                theme["border"] = "#A94444"  # 暗红边框
                theme["accent"] = "#FF7B7B"  # 亮红强调
        return theme

    def refresh_theme(self):
        """刷新主题颜色，响应全局主题切换"""
        # 🐛 清空 LRU 渲染缓存 + 骨架 HTML 缓存，强制下次渲染使用新主题颜色。
        # 否则 _render_markdown_to_html_cached 的 @lru_cache 会返回旧主题的 HTML
        # （旧 pygments 代码高亮 + 旧图标路径），导致代码块颜色与背景混淆而"消失"。
        clear_global_render_cache()
        # 同步全局性能缓存（图标前缀和字号），确保下次渲染使用新主题
        _update_icon_prefix()
        global _CODE_FONT_SIZE
        _CODE_FONT_SIZE = scale_font_size(13)
        # 刷新主题颜色
        self._theme = self._build_theme(self.role, self.error)
        self._base_bg = self._theme["bg"]
        self._base_border = self._theme["border"]
        self._apply_card_style()
        # 更新头像
        if hasattr(self, "_av_label"):
            self._av_label.setStyleSheet(self._build_avatar_style())
        # 更新标题
        if hasattr(self, "_name_label"):
            font_css = get_font_family_css()
            self._name_label.setStyleSheet(
                f"{font_css} font-size:{scale_font_size(14)}px;color:{self._theme['text']};font-weight:700;"
            )
        # 更新副标题
        if hasattr(self, "_subtitle_label"):
            font_css = get_font_family_css()
            self._subtitle_label.setStyleSheet(
                f"{font_css} font-size:{scale_font_size(11)}px;color:{self._theme['muted']};font-weight:500;letter-spacing:0.02em;"
            )
        # 更新时间戳
        if hasattr(self, "_ts_label"):
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
        if hasattr(self, "viewer") and self.viewer and hasattr(self.viewer, "_refresh_viewer_font"):
            self.viewer._refresh_viewer_font()
        # 刷新用户卡片纯文本视图颜色（PlainTextViewer 没有 _refresh_viewer_font）
        if hasattr(self, "viewer") and self.viewer and hasattr(self.viewer, "refresh_theme"):
            self.viewer.refresh_theme()

    # ── 卡片背景色覆盖（替代 qfluentwidgets CardWidget 的固定白色覆盖层）──
    # 背景色完全由 _apply_card_style() 通过 CSS 控制，无需动态解析

    def _normalBackgroundColor(self):
        """返回透明色，让 CSS background-color 透出"""
        from PyQt5.QtGui import QColor

        return QColor(0, 0, 0, 0)

    def _hoverBackgroundColor(self):
        """返回透明色，让 CSS background-color 透出"""
        return QColor(0, 0, 0, 0)

    def _pressedBackgroundColor(self):
        """返回透明色，让 CSS background-color 透出"""
        return QColor(0, 0, 0, 0)

    def _get_footer_model_text(self) -> str:
        """根据 model_name 生成页脚显示文本（服务商名已隐藏，仅显示模型名）"""
        return self.model_name or ""

    def set_model_name(self, model_name: str, provider_name: str = None, config_id: str = None):
        """设置模型名称显示（用于助手卡片）

        Args:
            model_name: 模型名称
            provider_name: 服务商显示名（可选）
            config_id: 服务商配置 UUID（可选，用于精确导航到对应配置）
        """
        if self.role != "assistant":
            return
        if not model_name:
            return
        self.model_name = model_name
        if provider_name is not None:
            self.provider_name = provider_name
        if config_id is not None:
            self._provider_config_id = config_id
        footer_text = self._get_footer_model_text()
        if hasattr(self, "_ts_label"):
            self._ts_label.setText(model_name)
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
        # 同步到底部元信息栏
        if self._footer_model_label:
            self._footer_model_label.setText(footer_text)
            self._footer_model_label.setVisible(True)
            self._refresh_footer_separators()

    def _build_footer_bar(self, main: QVBoxLayout):
        """构建助手卡片底部极简元信息栏：差异统计（左） | token | 耗时 | 模型（右）"""
        bar = QWidget(self)
        self._footer_bar = bar
        bar.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(bar)
        # 水平 8 让两端对称，垂直 0 配合统一字号后整体更紧凑
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(0)

        accent = self._theme["accent"]
        font_css = get_font_family_css()
        # 统一所有 footer 元素字号为 10px（原 9px 文字 + 11px emoji 混用 → 基线错位）
        label_style = (
            f"{font_css} font-size: {scale_font_size(10)}px; "
            f"color: {accent}; font-weight: 400; padding: 0px; margin: 0px;"
        )

        # 差异统计（左对齐，极简风格，点击弹出差异弹窗）
        diff_l = QLabel("", self)
        diff_l.setStyleSheet(label_style)
        diff_l.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        diff_l.setVisible(False)
        diff_l.setCursor(Qt.PointingHandCursor)
        diff_l.mousePressEvent = lambda e: self._emit_card_diff_requested()
        install_hover_tooltip(diff_l, "点击查看当条消息的文件差异详情")
        self._footer_diff_stats_label = diff_l
        layout.addWidget(diff_l)

        # Review 按钮（使用 Search 图标），点击触发 code-reviewer 子智能体
        icon_size = scale_font_size(10)
        review_btn = QLabel(self)
        review_btn.setObjectName("footer_review_btn")
        review_btn.setPixmap(get_icon("Search").pixmap(icon_size, icon_size))
        review_btn.setFixedSize(icon_size + 4, icon_size + 4)
        review_btn.setScaledContents(True)
        review_btn.setStyleSheet(
            "QLabel {"
            " background: transparent; padding: 2px; margin: 0px;"
            " border-radius: 3px;"
            " }"
            "QLabel:hover { background: rgba(128,128,128,0.18); }"
        )
        review_btn.setAlignment(Qt.AlignCenter)
        review_btn.setCursor(Qt.PointingHandCursor)
        review_btn.setVisible(False)
        review_btn.mousePressEvent = lambda e: self._emit_review_requested()
        install_hover_tooltip(review_btn, "用 code-reviewer 子智能体快速审查本次修改")
        self._footer_review_btn = review_btn
        layout.addWidget(review_btn)

        layout.addStretch()

        # Token 消耗
        tokens_l = QLabel("", self)
        tokens_l.setStyleSheet(label_style)
        tokens_l.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        tokens_l.setVisible(False)
        self._footer_tokens_label = tokens_l
        layout.addWidget(tokens_l)

        # 分隔点 1（token ↔ 耗时）
        sep1 = QLabel("·", self)
        sep1.setStyleSheet(label_style)
        sep1.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        sep1.setVisible(False)
        self._footer_sep1 = sep1
        layout.addWidget(sep1)

        # 耗时
        elapsed_l = QLabel("", self)
        elapsed_l.setStyleSheet(label_style)
        elapsed_l.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        elapsed_l.setVisible(False)
        self._footer_elapsed_label = elapsed_l
        layout.addWidget(elapsed_l)

        # 分隔点 2（耗时 ↔ 模型）
        sep2 = QLabel("·", self)
        sep2.setStyleSheet(label_style)
        sep2.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        sep2.setVisible(False)
        self._footer_sep2 = sep2
        layout.addWidget(sep2)

        # 模型名称（可点击，仅显示模型名，服务商名已隐藏但保留用于跳转）
        footer_text = self._get_footer_model_text()
        model_l = QLabel(footer_text, self)
        model_l.setStyleSheet(f"{label_style}")
        model_l.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        model_l.setVisible(bool(footer_text))
        model_l.setCursor(Qt.PointingHandCursor)
        model_l.mousePressEvent = lambda e: self._on_footer_model_clicked(e)
        install_hover_tooltip(model_l, "点击切换到目标模型配置")
        self._footer_model_label = model_l
        layout.addWidget(model_l)

        main.addWidget(bar)

    def set_meta_info(self, elapsed: float = None, token_usage: dict = None):
        """设置助手卡片的元信息（耗时和 token 消耗）

        Args:
            elapsed: 响应耗时（秒），如 3.2。传入后停止实时计时。
            token_usage: 如 {"input": 1234, "output": 567, "total": 1801}
        """
        if self.role != "assistant":
            return
        # 耗时
        if elapsed is not None and self._footer_elapsed_label:
            self._elapsed_timer.stop()
            self._elapsed_start_time = None
            self._footer_elapsed_label.setText(f"⏱ {elapsed:.0f}s")
            self._footer_elapsed_label.setVisible(True)
        # Token
        if token_usage is not None and self._footer_tokens_label:
            total = token_usage.get("total", 0)
            if total >= 1000:
                text = f"{total / 1000:.1f}K tokens"
            else:
                text = f"{total} tokens"
            self._footer_tokens_label.setText(text)
            self._footer_tokens_label.setVisible(True)
        # 刷新分隔点（用自己的状态判断，不依赖 isVisible()）
        self._refresh_footer_separators()

    def set_diff_stats(self, files_count: int = 0, additions: int = 0, deletions: int = 0):
        """设置左对齐差异统计：📄N | +N | -N（点击弹出差异弹窗）

        Args:
            files_count: 修改的文件数
            additions: 新增行数
            deletions: 删除行数
        """
        if self.role != "assistant":
            return
        if not self._footer_diff_stats_label:
            return
        if files_count == 0 and additions == 0 and deletions == 0:
            self._footer_diff_stats_label.setVisible(False)
            # 同步隐藏 Review 按钮（没有 diff 时审查无意义）
            if self._footer_review_btn:
                self._footer_review_btn.setVisible(False)
            return

        accent = self._theme.get("accent", "#888888")
        html = f'<span style="color:{accent};">📄{files_count}</span>'

        add_del = []
        if additions > 0:
            add_del.append('<span style="color:#2ea043;">+{}</span>'.format(additions))
        if deletions > 0:
            add_del.append('<span style="color:#f85149;">-{}</span>'.format(deletions))
        if add_del:
            html += "&nbsp;" + "/".join(add_del)

        self._footer_diff_stats_label.setText(html)
        self._footer_diff_stats_label.setTextFormat(Qt.RichText)
        self._footer_diff_stats_label.setVisible(True)

        # 同步显示 Review 按钮（紧贴差异统计右侧）
        if self._footer_review_btn:
            self._footer_review_btn.setVisible(True)

    def add_diff_stats(self, files_count: int = 0, additions: int = 0, deletions: int = 0, seen_files: set = None):
        """增量累加差异统计（工具执行时实时调用，文件级去重避免多次编辑同一文件重复计数）

        Args:
            files_count: 本次新增的文件数
            additions: 本次新增的行数
            deletions: 本次删除的行数
            seen_files: 本次操作涉及的文件路径集合（用于去重）
        """
        if self.role != "assistant":
            return
        if not self._footer_diff_stats_label:
            return

        # 懒初始化累积计数器
        if not hasattr(self, "_diff_seen_files"):
            self._diff_seen_files = set()
        if not hasattr(self, "_diff_files_total"):
            self._diff_files_total = 0
        if not hasattr(self, "_diff_additions_total"):
            self._diff_additions_total = 0
        if not hasattr(self, "_diff_deletions_total"):
            self._diff_deletions_total = 0

        if seen_files:
            new_files = seen_files - self._diff_seen_files
            self._diff_seen_files.update(seen_files)
        else:
            new_files = set()

        self._diff_files_total += len(new_files) if seen_files else files_count
        self._diff_additions_total += additions
        self._diff_deletions_total += deletions

        self.set_diff_stats(
            files_count=self._diff_files_total,
            additions=self._diff_additions_total,
            deletions=self._diff_deletions_total,
        )

    def _on_footer_model_clicked(self, event):
        """用户点击页脚模型标签时，发出 modelLabelClicked(model_name, config_id)"""
        if self.model_name:
            self.modelLabelClicked.emit(
                self.model_name,
                getattr(self, "_provider_config_id", "") or "",
            )

    def _refresh_footer_separators(self):
        """根据标签文本非空判断分隔点可见性（比 isVisible 更可靠）"""
        has_tokens = bool(self._footer_tokens_label and self._footer_tokens_label.text())
        has_elapsed = bool(self._footer_elapsed_label and self._footer_elapsed_label.text())
        has_model = bool(self._footer_model_label and self._footer_model_label.text())
        if self._footer_sep1:
            self._footer_sep1.setVisible(has_tokens and has_elapsed)
        if self._footer_sep2:
            self._footer_sep2.setVisible(has_elapsed and has_model)

    def start_elapsed_tracking(self):
        """开始实时计时（流式输出时调用）"""
        if self.role != "assistant":
            return
        if not self._footer_elapsed_label:
            return
        self._elapsed_start_time = time.time()
        self._footer_elapsed_label.setText("⏱ 0s")
        self._footer_elapsed_label.setVisible(True)
        self._refresh_footer_separators()
        self._elapsed_timer.start(1000)  # 每秒更新

    def _update_elapsed_display(self):
        """实时更新耗时显示"""
        if self._elapsed_start_time is None:
            self._elapsed_timer.stop()
            return
        elapsed = time.time() - self._elapsed_start_time
        self._footer_elapsed_label.setText(f"⏱ {elapsed:.0f}s")

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
        main.setContentsMargins(4, 4, 4, 4)
        main.setSpacing(4)
        top = QHBoxLayout()
        top.setContentsMargins(4, 0, 4, 0)
        top.setSpacing(6)

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
        nm_l.setStyleSheet(f"{font_css} font-size:{scale_font_size(14)}px;color:{self._theme['text']};font-weight:700;")
        sub_l = QLabel(self._theme["subtitle"], self)
        self._subtitle_label = sub_l
        sub_l.setStyleSheet(
            f"{font_css} font-size:{scale_font_size(11)}px;color:{self._theme['muted']};font-weight:500;letter-spacing:0.02em;"
        )
        title_layout.addWidget(nm_l)
        title_layout.addWidget(sub_l)

        top.addWidget(av)
        top.addWidget(title_wrap)
        # 用户卡片显示时间戳，助手卡片显示模型名称
        if self.role == "assistant" and self.model_name:
            label_text = self.model_name
        else:
            label_text = self.timestamp
        ts = QLabel(label_text, self)
        self._ts_label = ts
        ts.setVisible(bool(label_text))
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

        # 顶部操作按钮
        btns = QWidget(self)
        bl = QHBoxLayout(btns)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
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
                (get_icon("复制"), "复制", lambda: self._copy_user_message()),
                (get_icon("撤销"), "撤销到这里", self.undoRequested.emit),
                (get_icon("删除"), "删除", self.deleteRequested.emit),
            ]
        else:
            specs = []
        for ic, tp, cb in specs:
            b = TransparentToolButton(ic, self)
            b.setToolTip(tp)
            b.clicked.connect(cb)
            b.setFixedSize(32, 32)
            install_hover_tooltip(b, delay_ms=200)
            bl.addWidget(b)
        if specs:
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
            # 欢迎卡片使用懒渲染：占位符，不立即创建 QWebEngine
            # 避免首帧 Chromium 进程创建阻塞主线程（优化前首帧卡顿 200-500ms 的根因）
            placeholder = QLabel("加载中...", self)
            placeholder.setStyleSheet(
                f"color: #888888; font-size: {scale_font_size(14)}px; padding: 8px; {get_font_family_css()}"
            )
            placeholder.setAlignment(Qt.AlignCenter)
            self._viewer_layout.addWidget(placeholder)
            main.addWidget(self._viewer_container)
            self._lazy_rendered = False
            self.viewer = None  # 懒加载，延后创建
        else:
            # 懒渲染：占位符，不立即创建QWebEngine，进入可视区域再创建
            placeholder = QLabel("加载中...", self)
            placeholder.setStyleSheet(
                f"color: #888888; font-size: {scale_font_size(14)}px; padding: 8px; {get_font_family_css()}"
            )
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
        self.options_layout.setContentsMargins(0, 4, 0, 0)
        self.options_layout.setSpacing(4)
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

        # ===== 助手卡片底部元信息栏（分割线下方） =====
        if self.role == "assistant":
            self._build_footer_bar(main)
        self.setStyleSheet(
            f"""
            CardWidget {{
                background-color: {self._theme["bg"]};
                border: 1px solid {self._theme["border"]};
                border-radius: 10px;
            }}
            """
        )

        # 淡入动画：新消息微妙出现（200ms，仅透明度）
        fade_in_widget(self, 200)

    def start_streaming_anim(self):
        if self._streaming:
            return
        self._streaming = True
        # 🐛 修复"工具结果冒出又消失"：新轮流式开始时恢复 viewer 流式模式，
        # 避免 finish_streaming 后 viewer._streaming=False 导致工具结果到达时
        # append_tool_result 跳过 callback 更新，被后续 _perform_update 覆盖。
        if self.viewer and hasattr(self.viewer, "_streaming") and not self.viewer._streaming:
            self.viewer._streaming = True
        # 修正 viewer 初始化时的 _is_history 标记（可能因 viewer 创建早于
        # start_streaming_anim 而为 True，导致 _on_js_ready 误折叠工具区）
        if self.viewer and hasattr(self.viewer, "_is_history") and self.viewer._is_history:
            self.viewer._is_history = False
        # 新轮流式开始：恢复简洁模式坞态（工具区沉底跟随最新活动）
        if self.viewer and hasattr(self.viewer, "_sync_streaming_dock"):
            self.viewer._sync_streaming_dock(True)
        self._pulse_phase = 0.0
        try:
            self._anim_timer.start(50)  # 80→50ms，帧率从12.5fps提升到20fps
        except RuntimeError:
            return
        self.update()

    def _update_anim(self):
        # 拖拽期间暂停重绘：原生拖拽时主线程在 DefWindowProc 模态循环里，
        # 每 50ms 触发一次 update() 会强制 DWM 对整窗重新合成 → 拖拽卡顿。
        # 直接跳过 update() 让窗口保持静止，DWM 仅平移已有纹理，拖拽顺滑；
        # 松手后 _any_window_dragging 复位，下一拍定时器自然恢复动画。
        from app.tool_popup import ToolPopupDialog

        if ToolPopupDialog._any_window_dragging:
            return
        self._pulse_phase = (self._pulse_phase + 0.035) % (math.pi * 2)
        # 重试状态栏降频更新（每200ms一次，避免和paintEvent双重刷新导致卡顿）
        if self._retrying:
            if not hasattr(self, "_retry_status_tick"):
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
                border-radius: 10px;
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
        self.error = False
        self._retry_status_widget.setVisible(False)
        self._apply_card_style()
        if not self._streaming:
            return
        # 继续正常的流式动画（彩虹边框）
        self.update()
        self.repaint()

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
        # 设置卡片为错误状态样式（根据深浅模式选择边框色）
        try:
            from app.utils.theme_manager import theme_manager

            _is_light = theme_manager.is_light_theme()
        except Exception:
            _is_light = False
        _border = "#FCA5A5" if _is_light else "#A94444"
        self._apply_card_style(border=_border)
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
        if hasattr(self.viewer, "_markdown_text"):
            markdown_text = self.viewer._markdown_text

        # 销毁旧viewer
        self.viewer.deleteLater()

        # 重新创建viewer
        for i in reversed(range(self._viewer_layout.count())):
            item = self._viewer_layout.itemAt(i)
            if item and item.widget():
                item.widget().deleteLater()

        self.viewer = CodeWebViewer(self)
        self.viewer._lazy_markdown_cb = self._build_incremental_md
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
            painter.end()
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
        painter.end()

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
            # 检测深浅色模式，选择合适背景
            try:
                from app.utils.theme_manager import theme_manager

                _is_light = theme_manager.is_light_theme()
            except Exception:
                _is_light = False
            bd = "#ff4d4d"
            bg = "#FFF5F5" if _is_light else "#2a1f1f"
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
        # 检测深浅色模式，选择合适的错误文字颜色
        try:
            from app.utils.theme_manager import theme_manager

            _is_light = theme_manager.is_light_theme()
        except Exception:
            _is_light = False
        _err_text_color = "#DC2626" if _is_light else "#ff6b6b"
        _err_sub_color = "#B91C1C" if _is_light else "#ff9999"
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
                color: {_err_text_color};
                font-size: {scale_font_size(14)}px;
                font-weight: bold;
            }}
            """
        )
        self._retry_attempt_label.setStyleSheet(
            f"""
            QLabel {{
                color: {_err_sub_color};
                font-size: {scale_font_size(12)}px;
            }}
            """
        )
        self._retry_status_widget.setVisible(True)

    def _emit_card_diff_requested(self):
        """发射卡片差异请求信号

        Signal:
            cardDiffRequested(int round_index, int message_index)
        """
        round_idx = self._round_index if self._round_index is not None else -1
        msg_idx = self._message_index if self._message_index is not None else -1
        self.cardDiffRequested.emit(round_idx, msg_idx)

    def _emit_review_requested(self):
        """发射页脚 Review 按钮点击信号（触发 code-reviewer 子智能体快速审查）

        Signal:
            reviewRequested(int round_index, int message_index)
        """
        round_idx = self._round_index if self._round_index is not None else -1
        msg_idx = self._message_index if self._message_index is not None else -1
        self.reviewRequested.emit(round_idx, msg_idx)

    def _update_height(self, h):
        target_height = max(40, h)
        current_height = self.viewer.height() or self.viewer.minimumHeight() or 40
        self._target_viewer_height = target_height

        # 🆕 流式中防抖：累积高度变化，定时器到期才应用 viewer 高度。
        # 流式期间每个 text chunk 都会触发 height report（~60fps），
        # 若每次立即 resize viewer 会导致卡片高度持续跳动、主滚动区不稳定。
        # 防抖后只有最后一次高度在 80ms 窗口到期后被应用，大幅减少 resize 频率。
        if self._streaming:
            if self._height_anim.state() == QVariantAnimation.Running:
                self._height_anim.stop()
            self._debounced_target_height = target_height
            if not self._stream_height_timer.isActive():
                self._stream_height_timer.start()
            return

        # 非流式小变化（<10px）→ 立即跳转避免闪烁
        if abs(target_height - current_height) < 10:
            if self._height_anim.state() == QVariantAnimation.Running:
                self._height_anim.stop()
            self._apply_viewer_height(target_height)
            return

        # 非流式大高度变化（折叠/展开）：一次性设 viewer 最终高度，
        # 但跳过容器的 200ms maximumHeight 动画，避免 AlignBottom 布局中
        # 卡片位置因容器动画与 viewer 高度变化不同步而"闪现"。
        # card_container 检查 NO_ANIMATION_PROP → 直接 snap 到目标高度。
        self.setProperty("noContainerAnimation", True)
        self._apply_viewer_height(target_height)
        # 延迟清除标志，等容器 snap 完成
        QTimer.singleShot(50, lambda: self.setProperty("noContainerAnimation", False))

    def _on_height_anim_state_changed(self, state):
        self._is_height_animating = state == QVariantAnimation.Running
        # 动画结束时触发一次高度变化信号，让父容器更新
        if state == QVariantAnimation.Stopped:
            self.heightChanged.emit(self._last_applied_viewer_height)
            layout = self.layout()
            if layout:
                layout.invalidate()

    def _apply_debounced_height(self):
        """应用防抖后的流式高度（_stream_height_timer 到期回调）"""
        h = self._debounced_target_height
        # 流式已结束则跳过（由 finish_streaming 后的非流式 _update_height 接管）
        if not self._streaming:
            return
        current_height = self.viewer.height() or self.viewer.minimumHeight() or 40
        if abs(h - current_height) > 2:
            self._apply_viewer_height(h)

    def _apply_viewer_height(self, value):
        height = max(40, int(value))
        if height == self._last_applied_viewer_height:
            return
        self._last_applied_viewer_height = height
        self.viewer.setFixedHeight(height)
        self.heightChanged.emit(height)
        # viewer 高度变化后 body 视口可能改变，仅在用户已处于底部时重新滚动到底部
        # 🐛 修复：当 MAX_HEIGHT 限制导致 body 首次出现溢出时，scrollTop=0，
        # wasAtBottom 永远为 false，auto-scroll 不触发。跟踪用户主动滚动行为，
        # 未滚动时强制 auto-scroll 到底部。
        if self._streaming and hasattr(self.viewer, "page") and self.viewer.page():
            try:
                # 🐛 修复：同步 auto-scroll 取代 setTimeout(0)，避免渲染间隙置顶闪烁
                # 🐛 修复：auto-scroll 成功后复位 _userScrolledWithin，
                # 防止用户一次滚轮操作后永久丧失粘性滚底能力。
                # 🐛 修复 race condition：打 auto-scroll 时间戳，防止异步派发的
                # scroll 事件在 _suppressScrollEvent=false 后被误判为用户主动滚动，
                # 导致后续流式输出卡顶部。
                self.viewer.page().runJavaScript(
                    "(function(){"
                    "  window._suppressScrollEvent = true;"
                    "  if (!window._userScrolledWithin) {"
                    "    document.body.scrollTop = document.body.scrollHeight;"
                    "  } else {"
                    "    var wasAtBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < "
                    + str(AUTO_SCROLL_THRESHOLD)
                    + ";"
                    "    if (wasAtBottom) {"
                    "      document.body.scrollTop = document.body.scrollHeight;"
                    "      window._userScrolledWithin = false;"
                    "    }"
                    "  }"
                    "  window._prevScrollTop = document.body.scrollTop;"
                    "  window._autoScrollTime = performance.now();"
                    "  window._suppressScrollEvent = false;"
                    "})();"
                )
            except RuntimeError:
                pass

    def sync_width(self, force: bool = False, target_width: int | None = None):
        """同步卡片宽度

        Args:
            force: 是否强制更新，即使宽度没变化
            target_width: 显式指定目标宽度（用于 resize 期间绕过循环依赖）。
                          传入时直接使用此宽度，不再从 parent 推算。
        """
        if target_width is None:
            parent = self.parentWidget()
            if not parent:
                return
            parent_width = parent.width()
            if self.role == "welcome":
                horizontal_margin = 20
            elif self.role == "user":
                horizontal_margin = 180
            else:
                horizontal_margin = 20

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
        if not self._resize_preview_mode and hasattr(self.viewer, "update_height"):
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
        # MessageCard 的 wheelEvent 仅在子 widget（viewer）未消费事件时被调用。
        # 此时说明内部没有可滚动内容，或内部已达边界 → 直接转发到外部。
        try:
            scroll_area = self._parent.chat_scroll_area
            if scroll_area:
                vbar = scroll_area.verticalScrollBar()
                if vbar and vbar.minimum() != vbar.maximum() and event.angleDelta().y() != 0:
                    vbar.setValue(vbar.value() - event.angleDelta().y() // 2)
                    event.accept()
                    return
        except Exception:
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

            # welcome 卡片使用轻量骨架（无 echarts CDN）
            is_welcome = self.role == "welcome"
            self.viewer = CodeWebViewer(self, light=is_welcome)
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            if not is_welcome:
                # 标记是否为历史会话：非流式加载的历史消息自动折叠工具区
                self.viewer._is_history = not self._streaming
                # 让 viewer 的 restore 逻辑知道哪些工具结果已到达，
                # 避免全量重渲染时把已完成的运行框以“运行中”状态复活。
                self.viewer._restore_finished_ids = self._finished_streaming_ids
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
            # [PERF] 內容已整體替換，失效舊工具塊 markdown 緩存
            if self.viewer and hasattr(self.viewer, "_tool_md_cache"):
                self.viewer._tool_md_cache.clear()
        else:
            # 用户消息支持 multimodal 内容（含图片块的列表）
            if isinstance(content, list):
                # 使用 content_to_text 正确提取文本，图片块转为 [图片] 占位符
                self._content_data = content
                rendered = content_to_text(content)
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

    # ── 增量 markdown 構建（性能優化）───────────────
    def _build_incremental_md(self) -> str:
        """增量構建 markdown：已完成的 tool_result 塊從緩存讀取，跳過昂貴的全量重建

        Profile 實測：
        - 純文本 10 block: 1.7 μs（極快，不緩存）
        - 5 個工具結果: 319 μs → 首次 ~64 μs/塊，之後每次渲染 0 μs
        - 30 個工具結果: 2,428 μs → 首次 ~81 μs/塊，之後每次渲染 0 μs

        對 tool_streaming / custom 等未知類型自動回退 content_to_markdown。
        """
        content = self._content_data
        if isinstance(content, str) or not isinstance(content, list):
            return content_to_markdown(content)

        cache = getattr(self.viewer, "_tool_md_cache", {}) if self.viewer else {}
        parts: List[str] = []

        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue

            bt = block.get("type")

            if bt == "tool_result":
                tid = block.get("tool_call_id", "")
                cached = cache.get(tid)
                if cached is not None:
                    parts.append(cached)
                    continue
                # 懶緩存：首次遇到未緩存的工具塊，轉換後快取
                single_md = content_to_markdown([block])
                if tid:
                    cache[tid] = single_md
                parts.append(single_md)

            elif bt == "text":
                text = str(block.get("text", ""))
                if text:
                    parts.append(text)

            elif bt == "reasoning":
                reasoning_content = str(block.get("content", "") or "")
                if reasoning_content:
                    parts.append(f"<think>{reasoning_content}</think>")

            elif bt in ("image_url", "input_image", "image"):
                image_url = ""
                if bt == "image_url":
                    image_data = block.get("image_url", {}) or {}
                    image_url = str(image_data.get("url", ""))
                if image_url and image_url.startswith("data:image"):
                    parts.append("![image](uploaded_image)")
                elif image_url:
                    parts.append(f"![image]({image_url})")
                else:
                    parts.append("[图片]")

            else:
                # 未知類型（含 tool_streaming / custom）→ 全量回退
                return content_to_markdown(content)

        return "\n\n".join(part for part in parts if part).strip()

    def append_text(self, text: str):
        if self.role == "assistant":
            self._content_data = append_text_block(self._content_data, text)
            # 优化：懒渲染模式下直接跳过 markdown 渲染，避免不必要的计算
            if not self._lazy_rendered or not self.viewer:
                self._pending_content = self._content_data
                return
            # [PERF] 增量 markdown 構建：已完成的 tool_result 塊走緩存，只有文本塊即時轉換
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            # 流式模式下增量追加纯文本到 DOM，让用户立即看到文字
            if self._streaming:
                self.viewer._append_text_incremental(text)
            # 🆕 检测未闭合 <think> 标签：静默累积不触发渲染，与 append_reasoning 策略一致
            # 避免每个思考文本 chunk 都触发全量渲染 → reorganizeContent → think-streaming
            # DOM 节点反复 destroy+recreate 导致"思考中"状态闪烁。
            last_block = self._content_data[-1] if self._content_data else None
            last_text = last_block.get("text", "") if isinstance(last_block, dict) else ""
            if _has_unclosed_think(last_text):
                if not self.viewer._think_text_streaming_started:
                    # 首 chunk：立即渲染一次显示"深度思考中..." spinner
                    self.viewer._think_text_streaming_started = True
                    self.viewer._thinking_finalized = False
                    self.viewer._schedule_render(immediate=True)
                # 后续 chunk：静默累积，不触发渲染/高度更新
                self._content_just_loaded = True
                return
            # <think> 已闭合或无 think 标签：恢复正常渲染
            self.viewer._think_text_streaming_started = False
            # 恢复 _thinking_finalized：避免 _render_markdown_to_html 误剥离
            # 末尾闭合的 </think>（仅 reasoning_content 路径需要此行为）
            self.viewer._thinking_finalized = True
            # ── 差量渲染（2026-07-22）──
            # 文字即时性已由 _append_text_incremental 保证。全量 HTML 渲染
            # 仅在自然边界触发（段落结束 / 块闭合），非边界时只启安全定时器。
            # last_text 已通过 append_text_block 包含新追加文本，判断可靠。
            if self._streaming and self.viewer._has_reached_clean_boundary(last_text):
                self.viewer._schedule_render(immediate=True)
            else:
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
        diff: str = None,
        echarts: str = None,
    ):
        self._content_data.append(
            make_tool_result_block(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                success=success,
                tool_call_id=tool_call_id,
                diff=diff,
                echarts=echarts,
            )
        )
        # 标记为已完成：后续 streaming 更新直接跳过，避免在完成态工具块上
        # 错误挂载 data-streaming 属性导致样式混乱
        if tool_call_id:
            self._finished_streaming_ids.add(tool_call_id)
        # 优化：懒渲染模式下直接跳过 markdown 渲染，避免不必要的计算
        if not self._lazy_rendered or not self.viewer:
            self._pending_content = self._content_data
            return
        # 🐛 就近恢复 viewer 流式模式：finish_streaming 后 viewer._streaming=False，
        # 但工具结果可能在新一轮流式开始后才到达。先恢复再更新 callback，
        # 与 start_streaming_anim 中的恢复形成双重保险。
        if self.viewer and not self.viewer._streaming:
            self.viewer._streaming = True
        # 同步已完成工具集合给 viewer，供 restore 逻辑判断运行框是否可复活
        if self.viewer:
            self.viewer._restore_finished_ids = self._finished_streaming_ids
        # [PERF] 預計算並緩存此工具塊的 markdown，後續增量渲染直接拼接
        # 避免 content_to_markdown 遍歷全部 _content_data 做 _sanitize_result + 排序
        if tool_call_id and self.viewer:
            block = self._content_data[-1]
            single_md = content_to_markdown([block])
            cache = getattr(self.viewer, "_tool_md_cache", None)
            if cache is not None:
                cache[tool_call_id] = single_md
        # 增量注入：直接通过 JS 追加工具块 HTML，跳过全量 markdown 重建
        # 避免 content_to_markdown() 遍历全部 content_data 持有 GIL 导致拖动卡顿
        try:
            # 编辑类工具注入到 content-placeholder，跳过回调与渲染避免闪烁。
            # DOM 已通过 JS 注入到位，markdown 缓存已就绪供后续全量渲染使用。
            _is_edit_tool = tool_name in _EDIT_TOOLS
            if not _is_edit_tool:
                self.viewer._lazy_markdown_cb = self._build_incremental_md
                self.viewer._schedule_render(immediate=True)

            # 简洁模式：工具块默认折叠；非简洁模式：默认展开便于查看结果
            _collapsed = self.viewer._tool_compact_mode if self.viewer else True
            block_html = render_tool_block(
                tool_name=tool_name,
                tool_args=arguments or {},
                result=str(result) if result is not None else None,
                success=success,
                collapsed=_collapsed,
                tool_call_id=tool_call_id,
                diff=diff,
                echarts=echarts,
            )
            safe_html = json.dumps(block_html).decode("utf-8")

            # 提取 inner HTML（去掉外层 <div> 包装），用于原地更新已有 DOM 节点
            # outerHTML 替换会销毁旧元素再创建新元素，在 WebEngine 渲染管线中
            # 可能形成"旧元素消失 → 新元素出现"的跨帧闪烁。
            # 原地更新保持同一 DOM 节点，消除闪烁。
            _inner_match = re.match(r"^<div[^>]*>(.*)</div>$", block_html, re.DOTALL)
            if _inner_match:
                inner_html = _inner_match.group(1).strip()
            else:
                inner_html = block_html  # 兜底：整个当作 inner HTML
            safe_inner = json.dumps(inner_html).decode("utf-8")

            # 提取外层 <div> 的 style 属性（如 display: flex; align-items: center;）
            # 用于 INLINE_TOOLS 原地转换时应用到现有元素，保持 flex 布局
            _outer_style_match = re.search(r'<div[^>]*\sstyle="([^"]*)"', block_html)
            outer_style = _outer_style_match.group(1) if _outer_style_match else ""
            safe_outer_style = json.dumps(outer_style).decode("utf-8")

            # 提取 block_key（用于设置 data-block-key 属性）
            _key_match = re.search(r'data-block-key="([^"]*)"', block_html)
            block_key = _key_match.group(1) if _key_match else ""

            # ── 增量更新解析：将 inner_html 拆分为 button 和 body 两部分 ──
            # 避免 existing.innerHTML = safe_inner 整体替换导致的子节点空窗期
            # （外层 div 子节点清空瞬间 margin 暴露为可见间距，详见 #间距修复）
            _btn_match = re.match(r"<button[^>]*>(.*?)</button>", inner_html, re.DOTALL)
            _body_match = re.search(r'<div[^>]*class="cm-collapsible__body"[^>]*>(.*)</div>$', inner_html, re.DOTALL)
            if _btn_match and _body_match:
                btn_inner = _btn_match.group(1)
                body_inner = _body_match.group(1)
                # 提取 body div 上可能携带的 style（如 expanded: height:auto）
                _body_style_match = re.search(r'<div[^>]*class="cm-collapsible__body"[^>]*style="([^"]*)"', inner_html)
                body_style = _body_style_match.group(1) if _body_style_match else ""
                safe_btn_inner = json.dumps(btn_inner).decode("utf-8")
                safe_body_inner = json.dumps(body_inner).decode("utf-8")
                safe_body_style = json.dumps(body_style).decode("utf-8")
                _use_incremental = "true"
            else:
                safe_btn_inner = safe_body_inner = safe_body_style = '""'
                _use_incremental = "false"

            # 编辑类工具始终注入到正文区域，不进入"工具与思考"
            tool_target = "content-placeholder" if tool_name in _EDIT_TOOLS else self.viewer._tool_target_id

            js_code = f"""
            (function() {{
                var tc = document.getElementById('{tool_target}');
                if (!tc) {{
                    tc = document.getElementById('content-placeholder');
                }}
                // 优先查找已有流式块（同一 tool_call_id），原地转换为完成态块
                var existing = document.querySelector('[data-tool-call-id="{tool_call_id}"]');
                if (existing) {{
                    // 🐛 修复：检测 existing 是否为流式态块（tool-streaming-block）。
                    // 流式态块内部是 spinner + preview text，没有 .cm-collapsible__summary
                    // 和 .cm-collapsible__body 子元素，增量更新查找返回 null，
                    // 仅改 className 不改内部结构，导致运行框卡在"运行中"。
                    // 对流式态块走 outerHTML 整体替换为完成态折叠框。
                    var _isStreamingBlock = existing.classList.contains('tool-streaming-block');
                    if (_isStreamingBlock) {{
                        var _wrap = document.createElement('div');
                        _wrap.innerHTML = {safe_html};
                        var _newBlock = _wrap.firstElementChild;
                        if (_newBlock && existing.parentNode) {{
                            existing.parentNode.replaceChild(_newBlock, existing);
                        }}
                    }} else {{
                        // 原地更新：保持同一 DOM 节点，只替换 className / 属性
                        // 避免 outerHTML 销毁+重建导致的"消失再出现"闪烁
                        existing.className = 'cm-collapsible tool-block';
                        existing.setAttribute('data-block-key', '{block_key}');
                        existing.setAttribute('data-expanded', 'false');
                        existing.removeAttribute('data-streaming');
                        existing.removeAttribute('data-tool-injected');
                        existing.setAttribute('style', {safe_outer_style});

                        if ({_use_incremental}) {{
                            var btn = existing.querySelector('.cm-collapsible__summary');
                            if (btn) btn.innerHTML = {safe_btn_inner};
                            var body = existing.querySelector('.cm-collapsible__body');
                            if (body) {{
                                body.innerHTML = {safe_body_inner};
                                if ({safe_body_style}) {{
                                    body.setAttribute('style', {safe_body_style});
                                }}
                            }}
                        }} else {{
                            existing.innerHTML = {safe_inner};
                        }}
                    }}
                    // 确保 tool-section 可见
                    if (window._toolCompactMode) {{
                        var ts = document.getElementById('tool-section');
                        if (ts) {{ ts.style.display = ''; _updateToolSectionHeader(); }}
                    }}
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        document.body.scrollTop = document.body.scrollHeight;
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            document.body.scrollTop = document.body.scrollHeight;
                            window._userScrolledWithin = false;
                        }}
                    }}
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                    reportHeight();
                    return;
                }}
                // 无已有流式块时，追加新块（兜底逻辑）
                // 🐛 修复：不使用包装器 div（createElement+innerHTML+appendChild），
                // 改为直接追加 .tool-block 元素到 #tool-content。
                // 原包装器 div 不是 .tool-block，无 data-tool-call-id，
                // reorganizeContent 排序时 getPos=1e9 → 永远沉底，也无法被清理。
                var _wrap = document.createElement('div');
                _wrap.innerHTML = {safe_html};
                var _newBlock = _wrap.firstElementChild;
                if (_newBlock) tc.appendChild(_newBlock);
                // 🐛 修复：追加新块后同步滚动 document.body，替换旧的 tc.scrollTop
                window._suppressScrollEvent = true;
                if (!window._userScrolledWithin) {{
                    document.body.scrollTop = document.body.scrollHeight;
                }} else {{
                    var _bd2 = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                    if (_bd2 < {AUTO_SCROLL_THRESHOLD}) {{
                        document.body.scrollTop = document.body.scrollHeight;
                        window._userScrolledWithin = false;
                    }}
                }}
                window._prevScrollTop = document.body.scrollTop;
                window._autoScrollTime = performance.now();
                window._suppressScrollEvent = false;
                // 🐛 修复：工具区内部自动滚底
                if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                // 确保 tool-section 可见
                if (window._toolCompactMode) {{
                    var ts2 = document.getElementById('tool-section');
                    if (ts2) ts2.style.display = '';
                }}
                reportHeight();
            }})();
            """
            self.viewer.page().runJavaScript(js_code)
        except Exception as e:
            logger.warning(f"增量工具块注入失败: {e}")

    def _copy_user_message(self):
        """用户卡片工具栏「复制」：直接复制全文，不走 actionRequested 信号链

        避免信号链引起的 _on_code_action（clipboard.setText + InfoBar 动画），
        消除主线程阻塞（大文本 clipboard 操作）和 InfoBar 滑入动画叠加造成的闪烁。
        """
        if hasattr(self.viewer, "_copy_to_clipboard"):
            self.viewer._copy_to_clipboard(copy_selection=False)

    def get_plain_text(self) -> str:
        if self.role == "assistant":
            return content_to_text(self._content_data, include_tool_results=True)
        if isinstance(self._content_data, list):
            return content_to_text(self._content_data)
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

        关键：立即在 DOM 端标记所有已有的流式思考块为完成态，
        使新块获得独立的 data-streaming 状态。
        """
        self._content_data.append({"type": "reasoning", "content": ""})
        # 新一轮思考开始，仅重置 streaming 标志。
        # _thinking_finalized 留在 True（上一轮已完成），直到新 reasoning chunk 到达
        # （append_reasoning 首 chunk）才置为 False，防止在两轮之间的窗口期，
        # 已完成 think-block 的 </think> 被 _render_markdown_to_html 错误剥离。
        if self.viewer:
            self.viewer._reasoning_streaming_started = False
            self.viewer._think_text_streaming_started = False
        # DOM 端：将所有 data-streaming="true" 的旧块标记为完成
        # 兼容两种渲染形式：think-block（折叠框完成态）和 think-streaming（流式纯文本）
        if self.viewer and getattr(self.viewer, "page", None):
            try:
                self.viewer.page().runJavaScript("""
                (function() {
                    var blocks = document.querySelectorAll(
                        '.think-block[data-streaming="true"], .think-streaming[data-streaming="true"]'
                    );
                    blocks.forEach(function(block) {
                        block.setAttribute('data-streaming', 'false');
                    });
                })();
                """)
            except RuntimeError:
                pass

    # ── 工具流式调用块 ──────────────────────────────────

    def _inject_tool_streaming_html(
        self,
        tool_call_id: str,
        tool_name: str,
        preview: str,
        char_count: int = 0,
        completed: bool = False,
    ):
        """通过 JS 注入/更新工具流式块

        已有同 ID 块时原地更新预览文本，不重建 DOM，保持折叠/展开状态不丢失。

        preview 为 None 时表示仅更新 data-streaming 状态，不修改任何文字内容。
        用于 placeholder 阶段的 finish_tool_streaming 调用（参数全是占位键时）。
        """
        if not hasattr(self, "viewer") or not self.viewer:
            return

        # 标记内容加载，确保后续卡片高度变化时 _on_message_card_height_changed
        # 触发消息列表滚底。工具流式块注入属于内容加载，应滚动。
        # ⚠️ 不在此处调用 _schedule_render：全量渲染会执行 updateContent()
        # 销毁所有 JS 注入的 [data-tool-injected] 元素，导致流式块闪灭→再现。
        # 流式文本已由 _append_text_incremental 增量推送，不需要全量渲染。
        # 🔧 不设置 _content_just_loaded：工具流式更新不应触发外部消息列表滚动，
        # 仅 tool-content 内部自动滚底（见 JS 注入代码）。

        # 构建预览文本（含 char_count），用于后续内容比较和 JS 注入
        _text_only = preview is None
        preview_content = escape(preview) if preview else "准备中..."
        if not completed and char_count > 0:
            preview_content += f'<span style="color: var(--text); font-size: {scale_font_size(10)}px; margin-left: 4px;">({char_count}字符)</span>'

        # ── 内容去重：相同预览内容跳过 JS 执行，减少流式高频更新压力 ──
        _cache_key = (tool_call_id, completed)
        _last = getattr(self, "_tool_streaming_preview_cache", None) or {}
        if _last.get(_cache_key) == preview_content:
            return
        if not hasattr(self, "_tool_streaming_preview_cache"):
            self._tool_streaming_preview_cache = {}
        self._tool_streaming_preview_cache[_cache_key] = preview_content

        # ── 停掉全量渲染定时器：流式更新期间不跑全量重渲染 ──
        # 同时重调度一个"静默后渲染"兜底，确保流式结束后最终状态同步
        if hasattr(self, "viewer") and self.viewer:
            if hasattr(self.viewer, "_render_timer") and self.viewer._render_timer.isActive():
                self.viewer._render_timer.stop()
            # 非完成态时重调度一次兜底渲染（500ms 后，流式更新会持续重置）
            if not completed:
                self.viewer._schedule_render(immediate=False)
            else:
                self.viewer._schedule_render(immediate=True)
        try:
            block_html = _render_tool_streaming_block(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                preview=preview if preview else "",
                char_count=char_count,
                completed=completed,
            )
            # 编辑类工具流式块始终注入到正文区域
            _stream_target = "content-placeholder" if tool_name in _EDIT_TOOLS else self.viewer._tool_target_id

            safe_html = json.dumps(block_html).decode("utf-8")
            safe_preview = json.dumps(preview_content).decode("utf-8")
            streaming_flag = "true" if not completed else "false"
            _text_only_js = "true" if _text_only else "false"
            js_code = f"""
            (function() {{
                var tc = document.getElementById('{_stream_target}');
                if (!tc) {{
                    tc = document.getElementById('content-placeholder');
                }}
                var el = document.querySelector('[data-tool-call-id="{tool_call_id}"]');
                var hr = (typeof reportHeightDebounced === 'function') ? reportHeightDebounced : reportHeight;
                if (el) {{
                    // 🐛 FIX: 清除旧 data-tool-injected，消除 save-remove-restore 闪烁循环
                    el.removeAttribute('data-tool-injected');
                    var curStreaming = el.getAttribute('data-streaming');
                    // text-only 模式：仅更新 data-streaming 状态，不碰文字
                    if ({_text_only_js}) {{
                        el.setAttribute('data-streaming', '{streaming_flag}');
                        // 🐛 修复：状态更新后 body 自动滚底
                        window._suppressScrollEvent = true;
                        if (!window._userScrolledWithin) {{
                            document.body.scrollTop = document.body.scrollHeight;
                        }}
                        // 🐛 修复：工具区内部自动滚底
                        if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                        window._prevScrollTop = document.body.scrollTop;
                        window._autoScrollTime = performance.now();
                        window._suppressScrollEvent = false;
                        hr();
                        return;
                    }}
                    // 防止状态回退：已完成的块（data-streaming="false"）不允许
                    // 再切回流式态（data-streaming="true"），避免 spinner 反复闪烁
                    if ('{streaming_flag}' === 'true' && curStreaming === 'false') {{
                        var previewEl2 = el.querySelector('.tool-streaming-preview');
                        if (previewEl2) {{
                            previewEl2.innerHTML = {safe_preview};
                        }}
                    }} else {{
                        el.setAttribute('data-streaming', '{streaming_flag}');
                        var previewEl = el.querySelector('.tool-streaming-preview');
                        if (previewEl) {{
                            previewEl.innerHTML = {safe_preview};
                        }}
                    }}
                    // 🐛 修复：预览内容更新后 body 自动滚底
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        document.body.scrollTop = document.body.scrollHeight;
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            document.body.scrollTop = document.body.scrollHeight;
                            window._userScrolledWithin = false;
                        }}
                    }}
                    // 🐛 修复：工具区（#tool-content）内部自动滚底
                    if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    hr();
                }} else {{
                    // text-only 模式下不存在块：不创建
                    if ({_text_only_js}) return;
                    // 新块：直接插入
                    var tmp = document.createElement('div');
                    tmp.innerHTML = {safe_html};
                    var block = tmp.firstElementChild;
                    if (block) tc.appendChild(block);
                    // 🐛 修复：追加新块后 body 自动滚底，替换旧的 tc.scrollTop
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        document.body.scrollTop = document.body.scrollHeight;
                    }} else {{
                        var _bd2 = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd2 < {AUTO_SCROLL_THRESHOLD}) {{
                            document.body.scrollTop = document.body.scrollHeight;
                            window._userScrolledWithin = false;
                        }}
                    }}
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    // 工具区内部自动滚底（新块追加后）
                    if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                    // 确保 tool-section 可见
                    if (window._toolCompactMode) {{
                        var ts = document.getElementById('tool-section');
                        if (ts) {{ ts.style.display = ''; _updateToolSectionHeader(); }}
                    }}
                    hr();
                }}
            }})();
            """
            self.viewer.page().runJavaScript(js_code)
        except RuntimeError:
            pass

    def _maybe_finish_thinking_for_tool(self, tool_call_id: str):
        """当工具参数第一次到达时，标记当前思考块为完成态（💡）。

        修复 bug：reasoning 流结束 → tool_call 开始时，思考块 DOM 上还显示"思考中"。

        触发条件：update_tool_streaming / _on_tool_call_started 第一次被某个 tool_call_id 调用。

        实现：
        - 对 .think-block（已有折叠框结构）→ JS 更新 summary 文字为完成态
        - 对 .think-streaming（流式纯文本）→ Python 生成完整折叠框 HTML 替换

        🐛 修复：原实现只检查 _content_data[-1]，若 reasoning 后跟了空 text block
        则末尾为 text 类型，转换被跳过。改为向前遍历查找最后一个非空 reasoning block，
        与 append_reasoning 的查找逻辑保持一致。
        """
        if tool_call_id in self._tool_args_first_seen_ids:
            return
        self._tool_args_first_seen_ids.add(tool_call_id)

        # 检查 _content_data 中最后一个 reasoning block（允许其后存在空 text block）
        if not self._content_data or not isinstance(self._content_data, list):
            return
        last_reasoning_idx = -1
        for i in reversed(range(len(self._content_data))):
            blk = self._content_data[i]
            if isinstance(blk, dict) and blk.get("type") == "reasoning":
                last_reasoning_idx = i
                break
            # 遇到非空非 reasoning block 停止向前查找（避免误绑定早期思考）
            if isinstance(blk, dict):
                bt = blk.get("type")
                if bt == "text" and (blk.get("text") or "").strip():
                    break
                if bt in ("tool_result", "tool_streaming"):
                    break
            if isinstance(blk, str) and blk.strip():
                break
        if last_reasoning_idx < 0:
            return
        last_block = self._content_data[last_reasoning_idx]
        if not isinstance(last_block, dict):
            return
        raw_content = last_block.get("content") or ""
        if not raw_content.strip():
            # 空 block（start_new_thinking_block 刚创建）跳过 — 等后续 reasoning chunks
            return
        # 保持原始 content 用于 block-key 计算，确保与 _inject_think_cards
        # （通过 _build_incremental_md）产生的 key 一致。
        # 否则每次 _perform_update → reorganizeContent 会因 key 不匹配
        # 删除已有的 think-block 再重新创建，触发 CSS 入场动画（消失→重现）。
        content = raw_content

        # 懒渲染未就绪 / viewer 未创建
        if not self._lazy_rendered or not self.viewer:
            return

        # 通知 viewer：思考已完成，后续全量渲染不要再剥离 </think>
        self.viewer._thinking_finalized = True

        # [PERF-opt] 取消待处理的全量渲染定时器，防止覆盖增量 JS 思考框更新
        if hasattr(self.viewer, "_render_timer") and self.viewer._render_timer.isActive():
            self.viewer._render_timer.stop()

        # Python 端预计算分类（与 _render_think_block 一致），保留图标 + 分类标签
        tag = _classify_think_tag(content)
        think_icon = _get_think_icon_html()
        if tag:
            status_html = f'<span class="think-bulb">{think_icon}</span> {escape(tag)}'
        else:
            status_html = f'<span class="think-bulb">{think_icon}</span>'
        safe_status = json.dumps(status_html).decode("utf-8")

        # 预生成完成态折叠框 HTML（用于替换 .think-streaming 纯文本 div）
        compact = self.viewer._tool_compact_mode if self.viewer else False
        completed_html = _render_think_block(content, completed=True, compact=compact)
        safe_completed_html = json.dumps(completed_html).decode("utf-8")

        # 直接 JS 处理 DOM 上残留的"思考中"状态
        # 注意：不能走全量渲染 — `_render_markdown_to_html` 流式模式会去掉末尾 </think>，
        # 导致 markdown 仍被解析为 completed=False（"思考中"）。
        try:
            js_code = f"""
            (function() {{
                // ── 处理 .think-streaming 纯文本块：替换为完成态折叠框 ──
                var streamingBlocks = document.querySelectorAll('.think-streaming[data-streaming="true"]');
                streamingBlocks.forEach(function(block) {{
                    block.setAttribute('data-streaming', 'false');
                    var tmp = document.createElement('div');
                    tmp.innerHTML = {safe_completed_html};
                    var newBlock = tmp.firstElementChild;
                    if (newBlock) {{
                        // 标记为恢复块，跳过 CSS 入场动画（_toolBlockEnter opacity:0→1），
                        // 避免工具调用切换时思考折叠框"消失→重现"的视觉闪烁
                        newBlock.setAttribute('data-restored', 'true');
                        block.parentNode.replaceChild(newBlock, block);
                    }}
                }});

                // ── 处理 .think-block 已有折叠框：只更新 summary 文字 ──
                var blocks = document.querySelectorAll('.think-block[data-streaming="true"]');
                blocks.forEach(function(block) {{
                    block.setAttribute('data-streaming', 'false');
                    var summary = block.querySelector('.think-block__summary');
                    if (summary) {{
                        var spans = summary.children;
                        var statusSpan = null;
                        for (var i = 0; i < spans.length; i++) {{
                            var s = spans[i];
                            var inline = s.getAttribute('style') || '';
                            if (inline.indexOf('white-space: nowrap') !== -1) {{
                                statusSpan = s;
                                break;
                            }}
                        }}
                        if (!statusSpan && spans.length >= 2) {{
                            statusSpan = spans[1];
                        }}
                        if (statusSpan) {{
                            statusSpan.innerHTML = {safe_status};
                        }}
                    }}
                }});
                if (typeof reportHeightDebounced === 'function') {{
                    reportHeightDebounced();
                }} else if (typeof reportHeight === 'function') {{
                    reportHeight();
                }}
            }})();
            """
            self.viewer.page().runJavaScript(js_code)
        except RuntimeError:
            pass

    def update_tool_streaming(
        self,
        tool_call_id: str,
        tool_name: str,
        partial_args: dict = None,
    ):
        """更新工具流式参数预览 — 更新已注入的工具块预览文本

        预览文本使用自然语言描述（如"搜索xxx中"），代替原始 JSON。
        流式期间附加"中"后缀表示进行中状态。

        Args:
            tool_call_id: 工具调用唯一 ID
            tool_name: 工具名
            partial_args: 部分参数
        """
        # 已完成参数接收或已追加工具结果的不再更新，防止完成态被退回 streaming 状态
        if tool_call_id in self._finished_streaming_ids:
            return
        # 🆕 第一次工具参数到达时，标记当前思考块为完成态（💡）
        # 修复 bug：reasoning 流结束 → tool_call 开始时，思考块 DOM 还显示"思考中"
        self._maybe_finish_thinking_for_tool(tool_call_id)
        preview = ""
        char_count = 0
        if partial_args:
            display = {k: v for k, v in partial_args.items() if not k.startswith("_")}
            if display:
                try:
                    natural = _format_natural_preview(tool_name, display)
                    if natural:
                        # 有实参时：显示自然语言描述 + "中"，不需要字符数进度
                        preview = natural + "中"
                        char_count = 0
                    else:
                        args_str = json.dumps(display).decode("utf-8")
                        if len(args_str) > 100:
                            preview = args_str[:100] + "..."
                        else:
                            preview = args_str
                        char_count = 0
                except Exception:
                    preview = "..."
            else:
                # 参数尚未到达或全是 _ 占位键
                # 用 _args_len 获取缓冲区实际接收长度作为字符数进度
                args_len = partial_args.get("_args_len", 0)
                # 缓冲区提前提取的 _path（chat_worker regex 提取），用于预览带真实文件名
                _path = partial_args.get("_path", "")
                preview_args = {"path": _path} if _path else {}
                natural = _format_natural_preview(tool_name, preview_args)
                if natural:
                    preview = natural + "中"
                else:
                    preview = "准备中..."
                char_count = args_len if args_len else len(preview)
        self._inject_tool_streaming_html(tool_call_id, tool_name, preview, char_count, completed=False)

    def finish_tool_streaming(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict = None,
    ):
        """工具参数接收完成 — 将流式块转为完成态，显示自然语言预览

        使用自然语言描述代替原始 JSON，完成态不加"中"后缀。

        Args:
            tool_call_id: 工具调用唯一 ID
            tool_name: 工具名
            arguments: 完整参数
        """
        preview = ""
        char_count = 0
        if arguments:
            display = {k: v for k, v in arguments.items() if not k.startswith("_")}
            if display:
                try:
                    natural = _format_natural_preview(tool_name, display)
                    if natural:
                        # 完成态：自然语言描述，不加"中"后缀
                        preview = natural
                        char_count = len(preview)
                    else:
                        args_str = json.dumps(display).decode("utf-8")
                        if len(args_str) > 100:
                            preview = args_str[:100] + "..."
                        else:
                            preview = args_str
                        char_count = len(args_str)
                except Exception:
                    preview = "..."
            else:
                # 参数全部是 _ 前缀占位键（preview 阶段），仅更新状态不覆盖文字
                self._inject_tool_streaming_html(tool_call_id, tool_name, preview=None, char_count=0, completed=True)
                return
        self._inject_tool_streaming_html(tool_call_id, tool_name, preview, char_count, completed=True)

    def remove_tool_streaming(self, tool_call_id: str):
        """移除工具流式块 — 工具执行完成后清理"""
        if not hasattr(self, "viewer") or not self.viewer:
            return
        try:
            js_code = f"""
            (function() {{
                var el = document.querySelector('[data-tool-call-id="{tool_call_id}"]');
                if (el) el.remove();
                reportHeight();
            }})();
            """
            self.viewer.page().runJavaScript(js_code)
        except RuntimeError:
            pass

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
            self._content_data[last_reasoning_idx]["content"] = (
                self._content_data[last_reasoning_idx].get("content", "") or ""
            ) + text
        else:
            # 未找到，新增 reasoning 块
            self._content_data.append({"type": "reasoning", "content": text})
        self._reasoning_total_len += len(text)

        if not self._lazy_rendered or not self.viewer:
            self._pending_content = self._content_data
            return

        # 标记内容已加载，高度变化时触发 _on_message_card_height_changed 滚底
        self._content_just_loaded = True

        # 🆕 方案B：首个 reasoning chunk 渲染"深度思考中..." spinner，后续静默累积
        # 不更新 DOM / 不触发渲染定时器 / 不更新高度，等 thinking 结束后的全量渲染
        # （由 append_text / finish_streaming / _maybe_finish_thinking_for_tool 触发）一并处理
        if not self.viewer._reasoning_streaming_started:
            self.viewer._reasoning_streaming_started = True
            # 🐛 修复：仅在新 reasoning 真正开始接收内容时才重置 _thinking_finalized。
            # start_new_thinking_block 不再重置此标志，防止两轮之间的空窗期
            # 已完成 think-block 的 </think> 被错误剥离为 think-streaming。
            self.viewer._thinking_finalized = False
            # 首 chunk：增量高度 + 立即全量渲染显示 spinner
            self._update_thinking_incremental(text)
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            self.viewer._schedule_render(immediate=True)
        else:
            # 后续 chunk：只累积到 _content_data，静默不触发任何 DOM 操作
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            # 不调用 _schedule_render / _update_thinking_incremental

    def _update_thinking_incremental(self, new_text: str):
        """流式思考增量更新（仅触发布局高度重算）

        思考中不再更新预览文字，仅显示转圈+思考中。
        结束时通过全量渲染更新预览文字到 summary 右侧。
        """
        if not hasattr(self.viewer, "page"):
            return

        # 标记内容加载，确保后续卡片高度变化时 _on_message_card_height_changed
        # 触发消息列表滚底。
        self._content_just_loaded = True

        try:
            # 仅触发布局高度重算，不再更新 .think-streaming-preview
            self.viewer.page().runJavaScript("""
            (function() {
                if (typeof reportHeightDebounced === 'function') {
                    reportHeightDebounced();
                } else {
                    reportHeight();
                }
            })();
            """)
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
        label.setStyleSheet(f"color: #4a9eff; {get_font_family_css()} {font_size_css(13)} cursor: pointer;")
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
        title_label.setStyleSheet(f"color: #888; {get_font_family_css()} {font_size_css(12)} margin-top: 8px;")
        self.options_layout.addWidget(title_label)

        for option in options:
            self.add_interactive_option(option)

    def _on_option_clicked(self, option: Dict[str, Any]):
        """选项被点击"""
        self.optionSelected.emit(option)

    def set_intervention_mode(self, enabled: bool):
        """设置人工干预模式"""
        if enabled:
            self.interventionRequested.emit({"card_id": id(self), "message": "请求人工干预"})

    def finish_streaming(self):
        try:
            if self.viewer is not None and hasattr(self.viewer, "finish_streaming"):
                self.viewer.finish_streaming()
                if hasattr(self.viewer, "_cleanup_render_cache"):
                    self.viewer._cleanup_render_cache()
        except RuntimeError:
            pass
        self.stop_streaming_anim()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 宽度同步由外层聊天窗口统一调度，避免卡片自身 resize 再次触发全量重算

    def _disconnect_all_signals(self):
        """断开 MessageCard 发射的所有信号，打破信号-槽引用环路"""
        signals = [
            self.heightChanged,
            self.deleteRequested,
            self.undoRequested,
            self.actionRequested,
            self.contextActionRequested,
            self.optionSelected,
            self.interventionRequested,
            self.toolDiffRequested,
            self.subAgentLogRequested,
            self.cardDiffRequested,
            self.reviewRequested,
            self.saveFileRequested,
            self.lazyRenderCompleted,
            self.modelLabelClicked,
        ]
        for sig in signals:
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass

    def cleanup(self):
        """
        清理 MessageCard 持有的资源，防止内存泄漏。
        应该在删除卡片前调用，或者在 closeEvent 中自动调用。
        """
        # 停止所有定时器
        timers_to_stop = [
            self._anim_timer,
            self._height_anim,
            self._elapsed_timer,
        ]
        for timer in timers_to_stop:
            try:
                if isinstance(timer, QTimer):
                    timer.stop()
                elif isinstance(timer, QVariantAnimation):
                    timer.stop()
            except RuntimeError:
                pass

        # 断开所有信号连接（打破引用环路）
        self._disconnect_all_signals()

        # 调用 viewer 的清理方法（先清理后释放引用）
        if hasattr(self.viewer, "cleanup"):
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
        self._pending_content = None  # 待渲染内容
        self._finished_streaming_ids.clear()  # 流式 ID 集合
        self._tool_args_first_seen_ids.clear()

        # 清理 markdown_cache 如果存在
        if hasattr(self, "_markdown_cache") and self._markdown_cache:
            self._markdown_cache.clear()
            self._markdown_cache = None

    def closeEvent(self, e):
        self.cleanup()
        super().closeEvent(e)


def create_welcome_card(
    parent=None,
    agent_name: str = "",
    agent_description: str = "",
    recent_sessions: list = None,
    top_by_count: list = None,
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

    # 随机选择欢迎语
    greeting = get_random_greeting()

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

            table_rows += f"<tr><td>{left_cell}</td><td>{right_cell}</td></tr>"

        history_section = f"""
<table class="session-table">
<tr><th>最近会话</th><th>最活跃会话</th></tr>
{table_rows}
</table>
"""

    welcome_md = f"""### 👋 {greeting}

{history_section}
"""

    card = MessageCard(role="welcome", timestamp="就绪", parent=parent)
    card.update_content(welcome_md)
    card.finish_streaming()
    return card
