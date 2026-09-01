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
import concurrent.futures
import hashlib
import math
import os
import random
import time
import re
import sys
import threading
import time
import urllib.parse
import weakref
from collections import OrderedDict
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
    QObject,
    QPointF,
    QThread,
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
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextDocument,
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
    SegmentedWidget,
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
    BorderRadius,
    Colors,
    _get_global_font,
    current_theme,
    fade_in_widget,
    font_size_css,
    get_unified_scrollbar_style,
    scale_font_size,
    scale_icon_size,
)

# 正文 HTML 的圆角变量串：与 design_tokens.BorderRadius 同源，
# 保证 Web 侧（消息正文）与 Qt 侧（控件）使用同一套圆角节奏。
_BORDER_RADIUS_CSS_VARS = BorderRadius.CSS_VARS
from app.utils.utils import get_font_family_css, get_icon

# 纯 Qt 块级渲染器（灰度功能，默认关闭）——**延迟导入**。
# [PERF] markdown_block_viewer 顶层会导入 pygments/qrc 资源并定义 30+ 个渲染
# 控件类，累计导入耗时约 340ms（其中模块自身顶层代码约 100ms）。而灰度开关
# qt_message_renderer 默认关闭，启动时无条件导入纯属启动开销 —— 首屏时间
# 是最贵的时间。改为首次真正需要渲染时才导入；开启灰度的实例可在启动后
# 由 main.py 的 _deferred_startup 预热，避免首张卡片渲染时抖动。
_MarkdownBlockViewerCls = None


def _get_markdown_block_viewer_cls():
    """返回 MarkdownBlockViewer 类（首次调用时导入并缓存）。"""
    global _MarkdownBlockViewerCls
    if _MarkdownBlockViewerCls is None:
        from app.widgets.markdown_block_viewer import MarkdownBlockViewer

        _MarkdownBlockViewerCls = MarkdownBlockViewer
    return _MarkdownBlockViewerCls


def prewarm_markdown_block_viewer() -> None:
    """预热块级渲染器（供开启灰度的实例在启动后调用）。"""
    try:
        _get_markdown_block_viewer_cls()
    except Exception:
        pass


def _qt_renderer_enabled() -> bool:
    """灰度开关：assistant 卡片正文用纯 Qt 块级渲染器替代 QWebEngineView。

    配置项 Settings.qt_message_renderer（默认 False）+ 环境变量 DRIFOX_QT_RENDERER
    （"1" 强制开）双通道；welcome 卡不参与灰度（JS 交互复杂）。
    """
    import os

    if os.environ.get("DRIFOX_QT_RENDERER") == "1":
        return True
    try:
        from app.utils.config import Settings

        return bool(Settings.get_instance().qt_message_renderer.value)
    except Exception:
        return False


from app.widgets.render_helpers import (
    _format_natural_preview,
    _get_tool_cn_name,
    _get_tool_icon,
    _get_tool_icon_html,
    _get_tool_icon_name,
    _reg_metadata_flag,
    get_tool_qrc_prefix,
    render_tool_block,
)
from app.widgets.simple_hover_tooltip import install_hover_tooltip

# ======== Markdown 实例 ========
_md_instance = None
ACTION_COLOR_MAP = {
    "ask": "#FF6347",
}
DEFAULT_COLOR = "#888888"

# ======== B3: 渲染线程池（md.convert 等纯计算移出主线程） ========
# 线程池 worker 只做纯 CPU 渲染（sanitize→inject→md.convert→wrap→resolve），
# 不触碰任何 Qt 对象；结果通过 Future 回调 + QTimer.singleShot(0) 回主线程应用。
# 独立 2 worker：与 _SHARED_TOOL_POOL（工具执行）隔离，避免互相饿死。
_RENDER_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="md_render",
)
# 线程局部：每线程私有 Markdown 实例 + formatter。
# 不得用全局 _md_instance / _FORMATTER_CACHE / set_pygments_style 跨线程
# （Markdown.reset() 与 HtmlFormatter 均非线程安全）。
_render_tls = threading.local()

# ======== 预编译的正则表达式（提升到模块级别，避免重复编译）=======
_CODE_BLOCK_PATTERN = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_CODE_BLOCK_WITH_LANG_PATTERN = re.compile(r"<pre><code(?:\s+class=\"([^\"]*)\")?>(.*?)</code></pre>", re.DOTALL)
# [内容](ask) 旧格式已废弃，请改用 <ask>内容</ask>；
# 仅保留 jump/create/generate/view/session 的旧 markdown 链接兼容。
_CONTEXT_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((jump|create|generate|view|session)(?:\|([^)]*))?\)")
# 追问新格式：<ask>内容</ask>，直接生成胶囊（空内容丢弃整段标签，避免 [](ask) 残留）
_ASK_TAG_PATTERN = re.compile(r"<ask>(.*?)</ask>", re.DOTALL)
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
# 防御上限：语言种类有限（<64），超限整体清空防膨胀
_LEXER_CACHE_MAX = 64
_TEXT_LEXER = TextLexer()
# formatter 含动态字号，缓存当前字号对应的实例
_FORMATTER_CACHE: dict = {"font_size": None, "formatter": None}

# ======== 流式边框调色板（模块级共享，修 #1）========
# 原实现每张 MessageCard 构造时各 new 10+8 个 QColor（约 +18 个/卡），改为
# 模块级共享 tuple。QColor 不可变，多卡共享同一批实例无副作用。
_RAINBOW_NORMAL: tuple = tuple(
    QColor(c)
    for c in (
        "#60D4FF",
        "#40C8FF",
        "#4DA6FF",
        "#8B7BFF",
        "#C084FC",
        "#F472B6",
        "#FB7185",
        "#F59E0B",
        "#34D399",
        "#22D3EE",
    )
)
_RAINBOW_RETRY: tuple = tuple(
    QColor(c)
    for c in (
        "#ff2222",
        "#aa0000",
        "#ff3333",
        "#880000",
        "#ff1111",
        "#bb0000",
        "#ff4444",
        "#990000",
    )
)

# ===== 性能缓存：图标前缀和字号（避免每块代码都查主题和计算字号） =====
_ICON_PREFIX_CACHE: str = "qrc:/icons"
_CODE_FONT_SIZE: int = scale_font_size(13)


def _update_icon_prefix():
    """主题切换时更新图标前缀缓存（单一来源 get_tool_qrc_prefix）"""
    global _ICON_PREFIX_CACHE
    _ICON_PREFIX_CACHE = get_tool_qrc_prefix()


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
        if len(_LEXER_CACHE) >= _LEXER_CACHE_MAX:
            _LEXER_CACHE.clear()  # 防御膨胀：语言种类有限，整体清空代价可忽略
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
AUTO_SCROLL_THRESHOLD = 80  # "接近底部"判定阈值(px)：仅真接近底部才恢复自动跟随；过大会把用户阅读位置反复拉回底部
# 🐛 滚动自愈：内部被判定为"可滚"却始终滚不动时，连续多少次后强制转发外部。
# 兜底所有几何缓存滞后场景，消除"怎么滚都没反应"的粘性失效。
WHEEL_STUCK_LIMIT = 4
# 两次滚轮间隔小于该值时不计入 stuck（Chromium 滚动与 reportHeight 上报均有
# 帧级延迟，密集滚动期间 scrollTop 未刷新属正常，不得误判为卡死）。
WHEEL_STUCK_MIN_INTERVAL = 0.1


def wheel_delta_to_px(delta: int) -> int:
    """滚轮角位移 → 滚动条位移（px），三处 wheelEvent 共用。

    🐛 原实现 `-delta // 2` 有两个缺陷：
    1) 归零：delta = -1（触控板精细滚动）→ -(-1)//2 = 0 → 向下滚完全无反应；
    2) 不对称：Python 整除向下取整，±3 分别得到 -2 / +1，上下滚速度不一致。
    改为四舍五入并保底 ±1，保证任何幅度都至少有 1px 响应。
    """
    step = int(round(delta / 2.0))
    if step == 0 and delta != 0:
        step = 1 if delta > 0 else -1
    return step


# 编辑类工具/子智能体/提问类工具：无论简洁模式与否，这些工具的结果始终展示在正文中
# 子智能体和提问工具（subagent_para/subagent_dag/question）涉及 AI 与用户的直接交互，
# 留在正文中比收到工具区更符合直觉，体验更连贯。
# 集合由 registry 派生（注册时显式声明 keep_in_content=True，不硬编码工具名）：
#   write/edit/multi_edit + subagent_para/subagent_dag + question 等
def _edit_tools() -> frozenset:
    """编辑/子智能体/提问类工具集合（registry 派生，数据源统一）"""
    try:
        from app.tools.registry import ToolRegistry

        return ToolRegistry.get_instance().keep_in_content_tools()
    except Exception:
        return frozenset()


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
def _wrap_code_blocks_with_copy_button_web(
    html: str,
    icon_prefix: str = None,
    font_size: int = None,
    formatter: object = None,
) -> str:
    """包裹代码块为带复制按钮的容器。

    Args:
        html: markdown 渲染后的 HTML
        icon_prefix: 图标前缀（None=用全局 _ICON_PREFIX_CACHE，主线程默认）
        font_size: 代码字号（None=用全局 _CODE_FONT_SIZE，主线程默认）
        formatter: Pygments formatter（None=用全局缓存 formatter，主线程默认；
                   B3 线程池 worker 必须传入线程局部 formatter，避免跨线程共享）
    """
    _icon_prefix = icon_prefix if icon_prefix is not None else _ICON_PREFIX_CACHE
    _font_size = font_size if font_size is not None else _CODE_FONT_SIZE

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

        # ===== Mermaid 代码块：渲染为矢量图表 =====
        # 这里只产出占位容器，真正的 vendor（polyfill + mermaid 10.9.1）
        # 由 JS 侧首次遇到 .mermaid-block 时才动态加载，避免所有卡片都背上 3.3MB。
        # 背景：Chromium 83 缺 structuredClone，mermaid 10 在模块顶层就炸、
        #       window.mermaid 直接 undefined，必须先打 polyfill。见 docs/mermaid-chromium83.md。
        if lang == "mermaid":
            try:
                mmd_text = _unescape_html(code_content_raw)
                if mmd_text.strip():
                    b64_mmd = base64.b64encode(mmd_text.encode("utf-8")).decode("ascii")
                    mmd_id = "mmd-" + hashlib.sha1(mmd_text.encode("utf-8")).hexdigest()[:12]
                    return (
                        f'<div id="{mmd_id}" class="mermaid-block" '
                        f'data-mermaid-src="{b64_mmd}" style="margin: 12px 0;"></div>'
                    )
            except Exception:
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
            _fmt = formatter if formatter is not None else _get_formatter_cached()
            highlighted = highlight(copy_text, lexer, _fmt)
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
                padding: 6px 10px; height: 30px; background: var(--code-toolbar, rgba(255, 255, 255, 0.03));
                border-bottom: 1px solid var(--code-border, rgba(45, 45, 57, 0.5)); border-radius: 10px 10px 0 0;
            ">
                {f'<span style="color: var(--accent-warm, #FFA500); font-size: {_font_size}px; font-weight: bold;">{lang}</span>' if lang else '<span style="color: var(--text-muted, #888);">Plain Text</span>'}
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
    prefix = get_tool_qrc_prefix()
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


_THINK_SNAKE_SIZE: int = scale_icon_size(12)
# ★ 着色用 #RRGGBB + stroke-opacity：QtSvg（QSvgRenderer）不支持 CSS rgba() 函数——
# 解析失败后 stroke 无效，整个图标渲染成 0 像素全透明（原生 spinner 全空白）。
# WebEngine 两种写法等价；stroke-opacity 对浏览器与 QtSvg 双兼容。
_THINK_SNAKE_SVG = (
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{_THINK_SNAKE_SIZE}" height="{_THINK_SNAKE_SIZE}" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="8" fill="none" stroke="#ffc832" stroke-opacity="0.06" stroke-width="2.5" />'
    '<circle cx="12" cy="12" r="8" fill="none" stroke="#ffc832" stroke-opacity="0.2" stroke-width="2.5"'
    ' stroke-linecap="round" stroke-dasharray="20 30" class="think-snake-arc" />'
    '<circle cx="12" cy="12" r="8" fill="none" stroke="#ffc832" stroke-opacity="0.55" stroke-width="2.5"'
    ' stroke-linecap="round" stroke-dasharray="12 38" class="think-snake-arc think-snake-body" />'
    '<circle cx="12" cy="12" r="8" fill="none" stroke="#ffc832" stroke-opacity="1" stroke-width="2.5"'
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
    # 子智能体任务：与 render_tool_block 统一走 registry metadata 声明
    # （插件注册 metadata["subagent_task"]=True；工具已由 task 更名为 subagent_para/subagent_dag，
    #   历史消息中的旧名 task 经 ToolNameMapper.to_native 归一化后命中）
    try:
        from app.tools.registry import ToolRegistry
        from app.tools.tool_name_mapper import ToolNameMapper

        _native = ToolNameMapper.to_native(tool_name)
        _sub_reg = ToolRegistry.get_instance().get(_native)
        is_sub_agent_task = bool(_sub_reg and _sub_reg.metadata and _sub_reg.metadata.get("subagent_task"))
    except Exception:
        is_sub_agent_task = False
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

    icon_html = _get_tool_icon_html(icon_name, tool_name=tool_name if not is_mcp else None)
    cn_name = _get_tool_cn_name(tool_name) if not is_mcp else display_name

    # spinner
    spinner_html = f'<span class="tool-streaming-spinner">{_THINK_SNAKE_SVG}</span>'

    # 合并预览文本 + 字符数进度（放在同一个 span 里，JS 更新 innerHTML 时一起走）
    preview_display = escape(preview) if preview else "准备中..."
    if not completed and char_count > 0:
        preview_display += f'<span style="color: var(--text); font-size: {scale_font_size(10)}px; margin-left: 4px;">({char_count}字符)</span>'

    streaming_state = "false" if completed else "true"
    # 编辑/子智能体/提问类工具标记 data-keep-in-content：JS 正文分区据此保留在正文（registry 派生）
    _keep_attr = ' data-keep-in-content="true"' if tool_name in _edit_tools() else ""

    return f"""<div class="tool-block tool-streaming-block" data-tool-name="{escape(tool_name)}" data-tool-call-id="{tool_call_id}" data-streaming="{streaming_state}"{_keep_attr} style="margin: 4px 0; background: transparent; border: none; border-radius: 6px; box-shadow: none; display: flex; align-items: center; padding: 5px 10px; {get_font_family_css()}">
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
    # 字号与折叠框正文 _get_think_block_styles() 对齐（13px），避免 spinner 旁的提示文字
    # 在消息正文中显得过粗过大。
    font_style_inline = f"{get_font_family_css()} font-size: {scale_font_size(13)}px;"
    spinner_html = f'<span class="tool-streaming-spinner">{_THINK_SNAKE_SVG}</span>'
    return f"""<div class="think-streaming" data-streaming="true" style="margin: 4px 0; padding: 6px 10px; border: none; border-radius: 6px;">
    <span style="display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); {font_style_inline}">
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
    # 字号与折叠框正文 _get_think_block_styles() 对齐（13px），避免 spinner 旁的提示文字
    # 在消息正文中显得过粗过大。
    font_style_inline = f"{get_font_family_css()} font-size: {scale_font_size(13)}px;"
    spinner_html = f'<span class="tool-streaming-spinner">{_THINK_SNAKE_SVG}</span>'
    return f"""<div class="think-streaming" data-streaming="true" style="margin: 4px 0; padding: 6px 10px; border: none; border-radius: 6px;">
    <span style="display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); {font_style_inline}">
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


# ── 方案 D：data-order 统一排序 ──────────────────────────────────
# 根因（Bug B 复发的第三条路径）：JS 直接注入 #tool-content 的工具块
# （_inject_tool_streaming_html 流式块 / append_tool_result 完成块 /
# save-restore 恢复块）不在 #content-placeholder 中，reorganizeContent 的
# getPos 查不到 posMap → 返回 1e9 → 排序时恒沉底 → 折叠框内"所有思考在前、
# 所有工具在后"，与实际到达顺序不符。
# 修复：给 JS 注入的工具块设 data-order 属性。data-order = 工具调用锚点之前
# 的 think/tool 块计数 + 同锚点多工具启动序号细分 —— 与 reorganizeContent 的
# posMap（blocks 序号，不含 text 块）**同尺度**，保证 JS 注入块与 markdown
# 渲染块可以混合比较排序而不冲突。
_THINK_TOOL_TYPES = ("reasoning", "tool_result")


def _count_think_tool_prefix(content: Any, up_to: int) -> int:
    """统计 _content_data[0:up_to] 中 think/tool 块的数量（data-order 基准值）。

    与 reorganizeContent 的 posMap 语义一致：只计可迁移到"工具与思考"区的块
    （reasoning / tool_result），text 等正文块不计数。
    """
    if not isinstance(content, list):
        return 0
    count = 0
    for b in content[:up_to]:
        if isinstance(b, dict) and b.get("type") in _THINK_TOOL_TYPES:
            count += 1
    return count


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
            # 🐛 防御：无 `<think>` 开头的段（历史上被差量切碎的产物 `段2</think>`）
            # 清理孤立 </think>，避免思考内容以普通正文泄漏（<p>内容</think></p>）。
            parts.append(md_text[i:].replace("</think>", ""))
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


def _render_tag_block(tag: str, content: str, completed: bool, compact: bool = False) -> str:
    """单个已注册标签 → 插件渲染器 HTML。

    渲染器缺失或失败时返回空串：内联标签通常是人格内心独白类内容
    （如 <mood>），回退原文会把本应隐藏的内容泄漏到正文，丢弃比泄漏安全。
    """
    if not content.strip():
        return ""
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        info = UIPluginRegistry.get_instance().get_tag_renderer(tag)
    except Exception:
        info = None
    if info is None:
        return ""
    try:
        html = info.render_func(content, {"tag": tag, "completed": completed, "compact": compact})
        return f'<div class="plugin-tag-block" data-tag="{escape(tag)}">{html}</div>'
    except Exception as e:
        logger.warning(f"[message_card] 标签渲染器 <{tag}> 失败: {e}")
        return ""


def _inject_tag_cards(md_text: str, completed: bool = True, compact: bool = False) -> str:
    """注入插件注册的内联标签卡片（<tag>...</tag> → 插件 render_func 的 HTML）。

    标签集合来自 UIPluginRegistry 的 tag renderer 注册表（如 assistant_hub
    人格的 <mood> 内心独白）。无注册标签时零开销直通；切分策略与
    _inject_think_cards 一致：open 到「下一个 open 前的最后一个 close」，
    未闭合按流式态透传给渲染器自行降级。
    """
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        tag_names = UIPluginRegistry.get_instance().get_registered_tag_names()
    except Exception:
        return md_text
    if not tag_names:
        return md_text

    # 逐标签切分：已注册标签整体替换为渲染结果，剩余文本原样保留
    result = md_text
    for tag in tag_names:
        open_tag, close_tag = f"<{tag}>", f"</{tag}>"
        if open_tag not in result and close_tag not in result:
            continue
        parts: List[str] = []
        i = 0
        while i < len(result):
            start = result.find(open_tag, i)
            if start == -1:
                # 防御：无 open 的段清理孤立 close（流式半截产物）
                parts.append(result[i:].replace(close_tag, ""))
                break
            parts.append(result[i:start])
            t0 = start + len(open_tag)
            nxt = result.find(open_tag, t0)
            search_end = nxt if nxt != -1 else len(result)
            close = result.rfind(close_tag, t0, search_end)
            if close != -1:
                parts.append(_render_tag_block(tag, result[t0:close], True, compact))
                i = close + len(close_tag)
            else:
                parts.append(_render_tag_block(tag, result[t0:search_end], False, compact))
                i = search_end
        result = "".join(parts)
    return result


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

    # ========== 解析 name（行首匹配保持） ==========
    name_match = _TOOL_NAME_PATTERN.search(content)
    name_end = name_match.end() if name_match else 0
    if name_match:
        tool_name = name_match.group(1).strip()

    # ========== 字段位置索引（行锚定，取每个字段最后匹配） ==========
    # diff/success/tool_call_id/echarts 用 finditer 行锚定（^字段:\s*，MULTILINE）
    # 取最后一个匹配位置：流式接收中字段可能重复出现（如 result 内容内含字段字样），
    # 只有行首的字段声明才是真正字段；最后一个行首匹配是最终值。
    _field_positions = {}
    for _m in re.finditer(r"^(?:success|tool_call_id|diff|echarts):\s*", content, re.MULTILINE):
        _fname = _m.group(0).rstrip(": \t\n")
        _field_positions[_fname] = _m.start()

    # ========== 解析 args（定位从 name 之后开始） ===========
    args_start = content.find("args:", name_end)
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
        # 没有找到 args:，尝试直接解析整个 JSON 对象（从 name 之后开始）
        brace_start = content.find("{", name_end)
        if brace_start >= 0:
            tool_args_str = content[brace_start:]

    # ========== 解析 success（取最后一个行首匹配） ==========
    tool_success = True
    _success_match = None
    for _sm in _TOOL_SUCCESS_PATTERN.finditer(content):
        _success_match = _sm
    if _success_match:
        tool_success = _success_match.group(1).strip().lower() == "true"

    # ========== 解析 tool_call_id（取最后一个行首匹配） ==========
    tool_call_id = None
    _id_match = None
    for _im in _TOOL_ID_PATTERN.finditer(content):
        _id_match = _im
    if _id_match:
        tool_call_id = _id_match.group(1).strip()

    # ========== 解析 result（定位从 args JSON 闭合之后开始） ==========
    # result 终点 = 各字段最后匹配位置的最小值（须在 result 之后）；
    # 全 -1（无后续字段）时取到块尾（保留兜底）。
    _result_start = content.find("result:", result_search_start)
    _result_end = len(content)
    for _fpos in _field_positions.values():
        if _fpos > _result_start:
            _result_end = min(_result_end, _fpos)
    if _result_start >= 0:
        tool_result = content[_result_start + 7 : _result_end].strip()
    else:
        tool_result = ""

    # ========== 解析 diff（可选字段，仅 edit/write 工具有；行锚定取最后一个） ==========
    diff_content = ""
    _diff_pos = _field_positions.get("diff", -1)
    if _diff_pos != -1:
        diff_after = content[_diff_pos + 5 :]  # skip "diff:"
        # diff 内容持续到下一个字段（\nsuccess:）或末尾
        diff_next = _NEXT_FIELD_PATTERN.search(diff_after)
        if diff_next:
            diff_content = diff_after[: diff_next.start()].strip()
        else:
            diff_content = diff_after.strip()

    # ========== 解析 echarts（可选字段，仅 subagent_dag 有；行锚定取最后一个） ==========
    echarts_content = ""
    _echarts_pos = _field_positions.get("echarts", -1)
    if _echarts_pos != -1:
        echarts_after = content[_echarts_pos + 8 :]
        # echarts JSON 持续到末尾或下一个字段
        echarts_next = _NEXT_FIELD_PATTERN.search(echarts_after)
        if echarts_next:
            echarts_content = echarts_after[: echarts_next.start()].strip()
        else:
            echarts_content = echarts_after.strip()

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

    # 历史工具 diff 缺失时的 fallback（从参数重建）：仅 edit 工具的
    # operations/anchor/lines 参数结构支持重建，由注册声明 metadata["reconstruct_diff"] 驱动
    if not diff_content and _reg_metadata_flag(tool_name, "reconstruct_diff"):
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


@lru_cache(
    maxsize=16
)  # 256→64→16：>200KB 大文本已走 __wrapped__ 绕过缓存；实际唯一渲染内容通常 < 16 条，16 与 64 命中率差异 <5%，内存占用 -75%
def _render_markdown_to_html_cached_impl(raw_md: str, compact: bool = False) -> str:
    """
    Markdown 转 HTML 的核心渲染函数（带 LRU 缓存）。
    """
    safe_md = _sanitize_incomplete_markdown(raw_md)
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    processed_md = _inject_think_cards(safe_md, True, compact=compact)
    processed_md = _inject_tool_blocks(processed_md, True, compact=compact)
    processed_md = _inject_hook_blocks(processed_md, True)
    processed_md = _inject_tag_cards(processed_md, True, compact=compact)

    try:
        md = get_markdown_instance()
        md.reset()
        html_content = md.convert(processed_md)
        html_content = _wrap_code_blocks_with_copy_button_web(html_content)
        return html_content
    except Exception:
        return f"<pre>{escape(raw_md)}</pre>"


def _render_markdown_to_html_cached(raw_md: str, compact: bool = False) -> str:
    """
    带内存保护的 Markdown 渲染函数。
    - 对于超过阈值的文本，跳过缓存直接渲染
    - 保持 LRU 缓存以提高重复内容的性能

    注：reasoning 已作为 <think> 块按实际顺序嵌入 raw_md（由 _build_incremental_md /
    content_to_markdown 按 _content_data 顺序生成），此处不再前置拼接——旧的
    "思考恒顶部" 正是由前置拼接 + append 恒末尾共同造成（Bug B 修复）。
    """
    # 大文本跳过缓存，防止内存膨胀 — 用 __wrapped__ 绕过 LRU，不清空缓存
    text_size = len(raw_md.encode("utf-8"))
    if text_size > _LRU_CACHE_SIZE_THRESHOLD:
        return _render_markdown_to_html_cached_impl.__wrapped__(raw_md, compact=compact)

    return _render_markdown_to_html_cached_impl(raw_md, compact=compact)


# ============================================================
# B3：渲染移出主线程 — 线程池 worker
#
# 职责：把最昂贵的「sanitize→inject→md.convert→代码块高亮→resolve 图片」整条
# markdown→HTML 管线挪到后台线程池执行，主线程只做快照采集（md 引用、主题参数）
# 与最终 DOM 应用（runJavaScript），消除 20-80ms 主线程阻塞。
#
# 线程安全要点：
# - _md_instance / set_pygments_style / _FORMATTER_CACHE 均为全局可变状态，
#   严禁跨线程使用；worker 使用 _render_tls 线程局部 Markdown 实例 + formatter。
# - 快照 md 只读不复制（>200KB 引用传递），raw_md 不可变字符串并发读安全。
# - worker 不走 lru_cache（主线程快路径保留），避免跨线程缓存污染。
# ============================================================
def _render_markdown_to_html_worker(snapshot: dict) -> str:
    """线程池 worker：渲染 markdown → HTML（纯 CPU 计算，无 Qt 交互）

    Args:
        snapshot: 主线程采集的渲染快照，字段：
            md: str                 markdown 原文（引用传递）
            streaming: bool         流式标志
            thinking_finalized: bool 思考块完成标志（流式剥离 </think> 用）
            compact: bool           简洁模式
            pygments_style: str     "friendly"/"dracula"
            icon_prefix: str        代码块图标前缀
            code_font_size: int     代码字号

    Returns:
        HTML 字符串（流式模式含字符统计 <div>）
    """
    tls = _render_tls
    # 线程局部 Markdown 实例（非全局 _md_instance，避免 reset() 跨线程竞争）
    md = getattr(tls, "md", None)
    if md is None:
        md = Markdown(
            extensions=["fenced_code", "nl2br", "tables"],
            output_format="html5",
            safe=False,
        )
        tls.md = md

    # 线程局部 formatter（style/font_size 变化时重建，非全局 _FORMATTER_CACHE）
    style = snapshot["pygments_style"]
    font_size = snapshot["code_font_size"]
    fmt_key = (style, font_size)
    if getattr(tls, "formatter_key", None) != fmt_key:
        pre_color = "#1a1a1a" if style != "dracula" else "#D4D4D4"
        tls.formatter = HtmlFormatter(
            style=style,
            linenos=False,
            noclasses=True,
            cssclass="code-block",
            prestyles=(
                f"margin:0; padding:0; background:transparent; "
                f"font-family: Consolas, monospace; font-size:{font_size}px; color:{pre_color};"
            ),
        )
        tls.formatter_key = fmt_key
    formatter = tls.formatter

    raw_md = snapshot["md"]
    streaming = snapshot["streaming"]
    compact = snapshot["compact"]
    icon_prefix = snapshot["icon_prefix"]

    if not streaming:
        # 非流式分支（历史加载 / 流式结束调用方已切非流式）
        safe_md = _sanitize_incomplete_markdown(raw_md)
        safe_md = _unwrap_code_blocks_with_context_links(safe_md)
        safe_md = _inject_context_links(safe_md)
        processed_md = _inject_think_cards(safe_md, True, compact=compact)
        processed_md = _inject_tool_blocks(processed_md, True, compact=compact)
        processed_md = _inject_hook_blocks(processed_md, True)
        processed_md = _inject_tag_cards(processed_md, True, compact=compact)
        md.reset()
        html_content = md.convert(processed_md)
        html_content = _wrap_code_blocks_with_copy_button_web(
            html_content,
            icon_prefix=icon_prefix,
            font_size=font_size,
            formatter=formatter,
        )
        html_content = _resolve_image_src(html_content)
        return html_content

    # 流式分支（与 _render_markdown_to_html 流式逻辑一致）
    streaming_md = raw_md.rstrip()
    if streaming_md.endswith("</think>") and not snapshot["thinking_finalized"]:
        # 末尾正好是 reasoning 块的闭合标签，去掉它表示该块尚未完成
        streaming_md = streaming_md[: -len("</think>")].rstrip()

    safe_md = _sanitize_incomplete_markdown(streaming_md)
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    processed_md = _inject_think_cards(safe_md, False, compact=compact)
    processed_md = _inject_tool_blocks(processed_md, False, compact=compact)
    processed_md = _inject_hook_blocks(processed_md, False)
    processed_md = _inject_tag_cards(processed_md, False, compact=compact)

    md.reset()
    html_content = md.convert(processed_md)
    html_content = _wrap_code_blocks_with_copy_button_web(
        html_content,
        icon_prefix=icon_prefix,
        font_size=font_size,
        formatter=formatter,
    )
    html_content = _resolve_image_src(html_content)
    html_content = html_content + _CHAR_COUNT_HTML
    return html_content


def _dispatch_render_done(seq: int, fut, wself) -> None:
    """Future 完成回调（worker 线程执行）：取结果 → 通过 Qt 信号回主线程

    使用 weakref 而非强引用，避免线程池 Future 永久持有 viewer 导致泄漏。
    不能在此线程调用 QTimer.singleShot（worker 线程无事件循环，事件不会投递）；
    改用 CodeWebViewer.renderDone 信号跨线程 emit（自动 QueuedConnection）。
    """
    try:
        html = fut.result()
    except Exception as _e:
        html = None
    viewer = wself()
    if viewer is None:
        return  # viewer 已被回收，丢弃结果
    try:
        viewer.renderDone.emit(seq, html)
    except RuntimeError:
        pass  # viewer C++ 对象已销毁（sip deleted），丢弃


# ── Skeleton 全局缓存：_load_skeleton 返回的 HTML 字符串（~54KB）在
# 多张卡片间共享，避免每张卡片独立构造大段 CSS/JS 模板。
# 缓存键：(is_light, theme_fingerprint, font_family, ...)
# OrderedDict LRU：超限时淘汰最久未用条目（骨架 ~54KB/条，48 条 ≈ 2.6MB 上限）
_skeleton_cache: "OrderedDict[tuple, str]" = OrderedDict()
_SKELETON_CACHE_MAX = 48
# 🆕 方案 A（#33）：骨架缓存版本号——骨架 JS/DOM 结构变更时必须递增，
# 强制旧缓存失效。教训：#26 data-order 修复依赖骨架 JS 的 getPos 逻辑，
# 若进程内仍持有旧版骨架缓存与新代码混合（新代码注入 data-order + 旧骨架
# 无 data-order 分支 / 反之），JS 行为不一致可能导致消息卡片空白。
# 递增时机：任何改动 _load_skeleton 生成的 HTML/JS 结构时 +1。
# v3：reorganizeContent 的 getPos 新增 data-order 优先分支（方案 D），
# 旧骨架无此分支会导致新代码注入的 data-order 不参与排序。
# v4：reorganizeContent 新增"为 markdown 块补齐 data-order"分支（方案 D+），
# 旧骨架缺此分支会在流式完成时把思考块与工具块交错错位。
# v5：方案 E：save 阶段把流式块 data-order 暂存到 window.__pendingStreamFloors，
# reorganizeContent 的 _streamFloors 初始化时合并（修复 save 移除流式块后
# 补 data-order 缺"排前流式工具数"修正 → restore 沉底 → 坞态归位瞬间错乱）。
# v6：方案 F/G：块引用锚点 _tool_anchor_pos + save/restore 后强制 sort。
# v7（F1）：reorganizeContent 的 getPos 对运行中工具块（tool-streaming-block）
# 强制沉底（返回 1e9，不参与 data-order 比较）——旧骨架无此分支会把运行中块
# 按调用时刻快照 data-order 排到思考块上方。
# v8（2026-08-09）：updateContentAppend 第二参数升级为 tailHtml（行内渲染 HTML，
# 原 tailText 纯文本）；新增 updateTailHtml 尾部行内渲染；_append_text_incremental
# 新增 data-rendered 分支（渲染节点后新建纯文本节点）。旧骨架无 updateTailHtml /
# data-rendered 分支会导致新代码调用 ReferenceError → 尾部不渲染。
# v17（2026-08-29）：新增 Mermaid 渲染链路（_mmdEnsure / renderMermaidBlocks
# 与 .mermaid-block 样式）。旧骨架无 renderMermaidBlocks，调用点已用
# typeof 守卫，不会报错，但会静默不渲染——必须靠版本号让旧缓存失效。
# v18（2026-08-30）：修复流式正文"碎片化"——_append_text_incremental 对
# data-rendered 尾部节点改为**就地追加文本节点**（原为每 chunk 新建 <p>，
# 流式期间正文被切成一堆带段落间距的碎片行，随后又被 updateTailHtml 合并回
# 正文，观感是"文字先在最后几行冒出来再跳回正文"）；新增 data-pending-break
# 挂起分段标记（纯 \\n\\n chunk 不再堆空段落），旧骨架无 removeAttribute 清理
# 逻辑会导致标记残留 → 必须靠版本号让旧缓存失效。
_SKELETON_CACHE_VERSION = 18


# 流式模式追加的字符统计 HTML 标记，用于 finish_streaming 时移除
_CHAR_COUNT_HTML = '<div id="char-count" style="color: var(--text-muted); font-size: 11px; margin-top: 12px; text-align: right; opacity: 0.7;"></div>'


# ============================================================
# B1：差量渲染 — 闭合段提取
#
# 流式渲染的另一个 80ms 级开销是"每次自然边界全量 md→HTML"。差量策略：
# 只把「已经闭合的完整段落/代码块」增量渲染并追加到 DOM（updateContentAppend），
# 未闭合的尾部（think/tool 未闭合、fence 未闭合、行尾半段）保持增量纯文本状态，
# 等闭合瞬间的全量渲染（或流式结束）统一处理。
#
# 规则（收敛版）：
# - 空行（\n\n）分隔段落
# - ```fence 配对后才切割；fence 内不切（fence 状态跨段累计）
# - think/tool 未闭合不切（尾部留在稳定区之外）
# - 列表/表格/引用跨空行被拆段属可接受差异（diff 场景不参与差量）
#
# 返回 (stable_md_len, segments)：
# - stable_md_len：最后一个完整闭合段之后的偏移（下次从这继续扫描）
# - segments：闭合段列表（每段是一段完整 markdown 文本）
# ============================================================
def _has_unclosed_think_or_tool(md: str) -> bool:
    """md 中是否存在未闭合的 `<think>` / `<tool>` 块（开标签数 > 闭合标签数）。

    用途：全量渲染应用后决定是否推进差量基线 `_stable_md_len`。
    首次流式迭代的 `append_reasoning` 首 chunk 会触发全量渲染（显示
    "深度思考中" spinner），此时 md 是**部分**的思考内容（未闭合 think）。
    若基线照常推进到该位置（think 块内部），后续差量扫描的切片会以
    `内容</think>` 开头（无 `<think>` 配对）→ 配对守卫不触发 →
    残段被当普通正文渲染 → 思考内容泄漏到正文。
    含未闭合块时返回 True（基线保持旧值，等完整闭合后再推进）。
    """
    if not md:
        return False
    return md.count("<think>") > md.count("</think>") or md.count("<tool>") > md.count("</tool>")


# ===== 句号类标点（软边界触发器）=====
# 句号类标点仅用于 **触发即时渲染**（_has_reached_soft_boundary →
# _schedule_render(immediate=True)）：句号到达时立刻把未闭合尾部整体行内渲染
# （_render_tail_inline，单 convert 保持段落结构），缩短 markdown 语法
# 源码形态的滞留时间。
# ⚠️ 历史教训（2026-09-01 拆段 bug）：曾用句号作差量渲染的**切段边界**
# （_extract_closed_segments 软边界切闭合段），但句号不是 markdown 段落边界——
# 无空行连续正文的同一段被切成多个独立 <p>，闭合段封口 + tail 另起新段，
# 观感是"正在蹦字的片段先换行出现在最下面，全量渲染时又跳回正文合并"。
# 因此闭合段**只**按 \n\n 硬边界切，段落完整性优先于 stable 推进。
_SENTENCE_END_CHARS = frozenset("。！？；…!?;")


def _extract_closed_segments(md: str):
    """提取 markdown 中已闭合的完整段（供差量增量渲染）。

    Args:
        md: 待扫描的 markdown 文本（从上次 stable 偏移之后的部分）

    Returns:
        (stable_md_len, segments)
        - stable_md_len: 最后一个完整闭合段结束后的字符偏移
        - segments: 闭合段列表（完整段落/代码块 markdown 原文）
    """
    if not md:
        return 0, []

    # 🐛 起点防护：扫描起点可能位于未闭合 `<think>`/`<tool>` 块内部
    # （历史遗留：首次流式首 chunk 全量渲染把基线推进到 think 中间，
    # 或上一轮差量在未闭合块的半路上被打断）。此时切片第一个闭合标签
    # 出现在开标签之前——若直接产出会得到"无 `<think>` 开头的残段"，
    # _inject_think_cards 会把残段当普通正文渲染 → 思考内容泄漏到正文
    # （后续全量渲染时才折叠消失）。遇到这种情况整个切片不产出，
    # 交给全量渲染兜底（_has_reached_clean_boundary → _sequence_render）。
    _first_open_think = md.find("<think>")
    _first_close_think = md.find("</think>")
    if _first_close_think != -1 and (_first_open_think == -1 or _first_close_think < _first_open_think):
        return 0, []
    _first_open_tool = md.find("<tool>")
    _first_close_tool = md.find("</tool>")
    if _first_close_tool != -1 and (_first_open_tool == -1 or _first_close_tool < _first_open_tool):
        return 0, []

    segments = []
    stable_len = 0
    i = 0
    n = len(md)
    fence_open = False  # 是否在 ``` 代码块内（跨段累计）
    while i < n:
        # 硬边界：空行 \n\n（markdown 段落分隔）——闭合段**唯一**切段边界。
        # 句号类标点不是 markdown 段落边界，禁止在此切段（会拆裂同段文字，
        # 详见上方 _SENTENCE_END_CHARS 注释块的历史教训）。
        seg_end = md.find("\n\n", i)
        boundary_len = 2  # 空行占 2 字符，跳过
        if seg_end == -1:
            break  # 剩余文本无段落边界：整段未闭合（无稳定边界）→ 停止

        seg = md[i:seg_end]
        if not seg:
            # 空段（连续空行 / 段首恰为分隔符）：跳过，不产出
            i = seg_end + boundary_len
            continue
        fence_count = seg.count("```")

        if fence_open:
            # 在 fence 内：偶数个 ``` → 仍在 fence 内（不切）；奇数个 → fence 闭合
            if fence_count % 2 == 0:
                i = seg_end + boundary_len
                continue
            fence_open = False
        else:
            if fence_count % 2 == 1:
                # 段内 fence 打开（未闭合）→ 不产出，fence 状态延续到下一段
                fence_open = True
                i = seg_end + boundary_len
                continue

        # fence 已闭合（或与 fence 无关）：检查 think/tool 配对是否闭合
        # think 配对守卫必须用真实标签 `<think>` / `</think>`（与 _build_incremental_md
        # 生成的标签一致）。旧代码误用 ` think` / ` response`：`<think>` 不含子串
        # ` think`、`</think>` 不含 ` response`，count 恒 0 → 守卫恒不触发 → 多段思考
        # 内容（含 \n\n）被在中间切碎成 `<think>段1` + `段2</think>`，后者无 `<think>`
        # 开头 → _inject_think_cards 当普通正文渲染 → 思考内容泄漏到正文（高块闪现）。
        if seg.count("<think>") > seg.count("</think>"):
            break  # think 未闭合 → 停止（尾部留在稳定区之外）
        if seg.count("<tool>") > seg.count("</tool>"):
            break  # tool 未闭合 → 停止

        # 该段完整闭合：产出
        segments.append(seg)
        stable_len = seg_end + boundary_len
        i = seg_end + boundary_len

    return stable_len, segments


def _render_stable_segment(md_seg: str, compact: bool = False) -> str:
    """B1: 渲染单个闭合段为 HTML（差量增量渲染的段落级快速路径）。

    与 _render_markdown_to_html_worker 管线一致（sanitize→inject→md.convert→
    代码块高亮包装），差量段与全量渲染产物对齐（含 pygments 高亮 + copy 按钮
    + 语言标签），避免"流式期间代码块素色、结束后变高亮"的形态跳变。
    小段同步渲染耗时 <1ms，无需线程池。

    Args:
        md_seg: 单个完整闭合的 markdown 段落
        compact: 工具/思考区简洁模式开关（与全量渲染 _tool_compact_mode 对齐，
            避免差量/全量渲染形态分裂——差量段硬编码 compact=False 会把 think
            渲染成折叠框 think-block，而全量渲染简洁模式下渲染成 think-compact，
            导致差量段与后续全量段形态不一致）。

    Returns:
        该段的 HTML（不含外层容器包裹，供 updateContentAppend 追加）
    """
    safe_md = _sanitize_incomplete_markdown(md_seg)
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    processed_md = _inject_think_cards(safe_md, True, compact=compact)
    processed_md = _inject_tool_blocks(processed_md, True, compact=compact)
    processed_md = _inject_hook_blocks(processed_md, True)
    processed_md = _inject_tag_cards(processed_md, True, compact=compact)
    md = get_markdown_instance()
    md.reset()
    html = md.convert(processed_md)
    # 🐛 修复（结构错乱）：差量段缺 _wrap_code_blocks_with_copy_button_web，
    # 代码块在流式期间渲染为素色 <pre>（无 pygments 高亮、无 copy 按钮、
    # 无语言标签），流式结束全量渲染才补全 → 用户感知"代码块结束后才变样"。
    # 与全量渲染管线对齐补包装（主线程同步渲染，全局 formatter 缓存安全）。
    html = _wrap_code_blocks_with_copy_button_web(html)
    html = _resolve_image_src(html)
    return html


def _render_inline_tail(md_text: str, compact: bool = False) -> str:
    """渲染流式未闭合尾部为行内 HTML（差量渲染的即时格式化路径）。

    解决：无空行分隔的长段落（大模型常见输出，尤其中文）在流式期间
    `_extract_closed_segments` 找不到 `\\n\\n` 闭合边界，尾部只能以纯文本
    （textContent）显示 → `**加粗**`、`` `code` ``、`[链接](url)` 等 markdown
    源码字面呈现，直到流式结束全量渲染才格式化（用户感知"内容与最终不符"）。

    与 _render_stable_segment（逐段独立 convert）不同：尾部**整体**一次
    convert，未闭合的段落/列表/引用/代码块结构在单一 markdown 上下文中
    保持正确（不拆段）；未闭合的行内语法（如 `**加粗`）由 markdown 库
    原样输出（字面显示），闭合后由下一次渲染补全。

    含 think/tool 标签的尾部**整体跳过**（返回空串）：思考/工具内容应交由
    _inject_think_cards / _inject_tool_blocks 渲染为卡片/工具块，此处渲染
    会导致内容泄漏为正文。调用方 _render_tail_inline 已用
    _has_unclosed_think_or_tool 拦截未闭合场景。

    Returns:
        行内渲染的 HTML（不含外层 <p> 包裹的额外处理，供 JS innerHTML 注入；
        节点带 data-incremental 标记，后续差量/全量渲染会整体替换）
    """
    if not md_text or not md_text.strip():
        return ""
    # 🐛 防御：tail 含任何 think/tool 标签（已闭合或未闭合）都不在此渲染——
    # 只删标签会把思考内容泄漏到正文；它们应由差量段/全量渲染
    # （_inject_think_cards / _inject_tool_blocks）正确处理为思考卡片/工具块。
    # 调用方 _render_tail_inline 已用 _has_unclosed_think_or_tool 拦截未闭合
    # 场景，此处双保险（防御历史残段/异常路径）。
    if "<think>" in md_text or "</think>" in md_text or "<tool>" in md_text or "</tool>" in md_text:
        return ""
    safe_md = _sanitize_incomplete_markdown(md_text)
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    processed_md = _inject_think_cards(safe_md, True, compact=compact)
    processed_md = _inject_tool_blocks(processed_md, True, compact=compact)
    processed_md = _inject_hook_blocks(processed_md, True)
    processed_md = _inject_tag_cards(processed_md, True, compact=compact)
    md = get_markdown_instance()
    md.reset()
    html = md.convert(processed_md)
    html = _resolve_image_src(html)
    return html


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
                    /* 坞态正文限高：容器自身滚动，卡片总高稳定不随流式增长，
                       工具区+todo 保持可见；流式结束归位后恢复自然高度。
                       330→450 略微放宽，让流式长回复展示更多正文。 */
                    max-height: 450px;
                    overflow-y: auto;
                    /* 🐛 修复（禁横向滚动）：单轴 auto 时另一轴 visible 会被计算为
                       auto → 长行（URL/无空格长 token）超宽出现容器级横向滚动条。
                       对齐 body 的 overflow-x:hidden；代码块(.code-content)/表格
                       (table-scroll-wrapper) 自带嵌套横向滚动不受影响。
                       overflow-wrap:break-word 让超宽长词强制断行（仅无断行点时
                       生效，正常文本不受影响），避免 hidden 只裁切看不到尾巴。 */
                    overflow-x: hidden;
                    overflow-wrap: break-word;
                    overflow-anchor: none;
                }
                body.streaming-dock #tool-section {
                    order: 2;
                    margin: 8px 0 0 0;
                }
                /* 坞态限高：流式期间工具区保持内滚，但需能看到足够多的实时条目。
                   原值 110px 仅 ≈3-4 行，工具/思考稍多就只能看到一小截，
                   视觉上与"折叠"难区分 —— 用户反馈误以为工具区默认收起了。
                   放宽到 220px（≈8 行）后，常规工具序列可完整看到实时进度，
                   同时仍为 max-height（非无限增长），保持"卡片总高不随流式膨胀"
                   的坞态设计意图。 */
                body.streaming-dock #tool-content {
                    max-height: 220px;
                }
                /* 任务列表坞态：固定高度（非仅 max-height）——切断工具区流式抖动向 todo 传导，
                   项目增减时限高内高度也不变，流式期间观感稳定 */
                body.streaming-dock #todo-content {
                    height: 96px;
                    max-height: 96px;
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
                        // 归位后滚到底部展示最新条目——仅当用户未在上方阅读时；
                        // 用户上滚查看中则保持其位置（内容未更新，不打扰阅读）
                        var tc = document.getElementById('tool-content');
                        if (tc && !tc._userScrolledUp) { tc._progScroll = true; tc.scrollTop = tc.scrollHeight; }
                    } else if (on && !wasOn) {
                        // 顶部 → 坞态：正文上移，做对称补偿
                        if (!_atBottom && _dockH > 0) {
                            document.body.scrollTop = Math.max(0, document.body.scrollTop - _dockH);
                        }
                        // 🐛 修复：进入坞态时正文容器开始限高内滚，切换瞬间内容溢出
                        // 会触发一次程序性 scroll 事件；重置正文容器用户滚动标志并程序
                        // 置底跟随，避免遗留状态/切换抖动误判为正文上滚而卡在顶部。
                        var _cp = document.getElementById('content-placeholder');
                        if (_cp) {
                            _cp._userScrolledUp = false;
                            _cp._progScroll = true;
                            _cp.scrollTop = _cp.scrollHeight;
                        }
                    }
                    // 高度变化（110px ↔ 600px max-height）后报告文档高度。
                    // 切换会触发 #tool-content 的 max-height 200ms 过渡 →
                    // 用过渡感知报告（抑制中间态，终值单报），无内容时退回 debounced。
                    var _tsHasContent = ts && ts.style.display !== 'none' && ts.offsetHeight > 0;
                    if (_tsHasContent && typeof _beginToolSectionTransition === 'function') {
                        _beginToolSectionTransition();
                    } else if (typeof reportHeightDebounced === 'function') {
                        reportHeightDebounced();
                    }
                }
"""

# 正文容器（#content-placeholder）自动滚底 + 用户滚动跟踪。
# 🐛 修复（区域独立）：坞态下正文容器与工具区（#tool-content）是两个独立内滚动
# 容器。工具/思考区更新路径（流式块注入/完成块替换/_apply_viewer_height 高度
# 回调）同样会调用 _autoScrollStreamingBody()——原实现无条件
# _cp.scrollTop = _cp.scrollHeight 置底正文容器，而 _userScrolledWithin 只由
# body 的 scroll 事件置位（用户滚正文容器时 body 不滚，标志恒 false），
# 保护完全失效 → 工具区每来新内容就把正文拉底，打断阅读。
# 修复对齐工具区 _scrollToolContentToBottom 模式：用户主动上滚正文
# （_userScrolledUp）时不拉底，滚回底部附近自动恢复跟随；程序置底打
# _progScroll 标记防误判为用户滚动。
# 🐛 修复（区域独立 II）：_userScrolledUp 保护只覆盖"用户上滚过"的场景，
# 跟随态（标志 false）下工具/思考更新仍会把正文拉底——"工具与思考更新时
# 正文滚到固定位置"。语义修正：正文容器只在**正文自身更新**时置底；
# 工具/思考路径传 bodyOnly=true 仅滚 body（非坞态跟随），不碰正文容器。
# 🐛 修复（wheel 标记意图）：_userScrolledUp 原由 scroll 事件（异步派发）
# 的 atBottom 推断置位，两条失效链：
# 1. 竞争窗口——用户滚轮后 scroll 事件尚未派发（标志仍 false），流式渲染
#    JS（_autoScrollStreamingBody 无参调用）抢先执行 → 无条件拉底覆盖用户
#    位置；后续 updateContent 保存被污染的 _cpPrevTop → 每次恢复到同一错误
#    值 → 表现为"正文更新时滚轮跳到固定偏上位置"。
# 2. 钳制误标——innerHTML 重建/高度回调使内容变短 → scrollTop 被浏览器钳制
#    → 触发 scroll 事件 → atBottom 误判 → userUp 误置 true → 停止跟随漂移。
# 改用 wheel 事件（同步派发、仅用户滚轮/触控板触发，无程序来源）标记上滚
# 意图；scroll 事件只做"滚回底部恢复跟随"，不再置位。
_CONTENT_AUTOSCROLL_JS = """
                function _autoScrollStreamingBody(bodyOnly) {
                    // bodyOnly=true：调用方是工具/思考更新路径（流式块注入/
                    // 完成块替换/高度回调），正文内容未变 → 严禁触碰正文容器
                    // 滚动位置（否则跟随态下正文被拉到固定底部）。
                    if (bodyOnly) return;
                    // 坞态（流式中）：#content-placeholder 自身限高滚动 → 跟滚正文容器
                    // 保持最新输出可见；body 高度被钳不溢出，滚动赋值无害。
                    var _cp = document.getElementById('content-placeholder');
                    if (document.body.classList.contains('streaming-dock') && _cp) {
                        if (!_cp._userScrolledUp) {
                            _cp._progScroll = true;
                            _cp.scrollTop = _cp.scrollHeight;
                        }
                    }
                    // 🔧 核心修复：正文（document.body）只在「跟随底部」状态
                    // （window._userScrolledWithin === false，即用户接近底部）时才拉到底部；
                    // 用户已上滚离开阅读(_userScrolledWithin === true)时绝不触碰，
                    // 保留其阅读位置——这是「不强制控制滚轮、不跳到怪异位置」的关键。
                    if (!window._userScrolledWithin) {
                        document.body.scrollTop = document.body.scrollHeight;
                    }
                }
                // 正文容器滚动跟踪：用户主动上滚时停止自动置底跟随，
                // 滚回底部附近自动恢复；程序置底（_progScroll）不算用户行为。
                // wheel/键盘**同步**标记上滚意图（scroll 事件异步派发，与流式
                // 渲染 JS 存在竞争窗口，不得作为置位依据）；scroll 事件仅恢复跟随。
                document.getElementById('content-placeholder')?.addEventListener('wheel', function(e) {
                    // 上滚（deltaY<0）：同步置位，抢占任何在途渲染 JS 的拉底。
                    // 🐛 门控：仅当容器实际可滚（内容溢出）才记为"上滚正文"——
                    // 无溢出时 wheel 本应转发外层聊天列表（Qt wheelEvent 转发分支），
                    // 页面内收到的事件属冒泡残留，置位会让跟随被无关操作误锁死。
                    if (e.deltaY < 0 && this.scrollHeight > this.clientHeight) {
                        this._userScrolledUp = true;
                    }
                }, {passive: true});
                document.getElementById('content-placeholder')?.addEventListener('scroll', function() {
                    var cp = this;
                    // DOM 操作期间（updateContent 重写 innerHTML / reorganizeContent
                    // 搬移 think 块）触发的程序性 scroll 事件必须忽略——与 body 监听的
                    // _suppressScrollEvent 抑制对称。
                    if (window._suppressScrollEvent) return;
                    if (cp._progScroll) { cp._progScroll = false; return; }
                    // 位置判定（与 body / #tool-content 监听完全一致）：
                    // 接近底部 = 恢复跟随（_userScrolledUp=false），离开底部 =
                    // 用户主动阅读（_userScrolledUp=true），保留其阅读位置——
                    // 「不强制控制滚轮、不跳到怪异位置」的关键。
                    // 仅程序性滚动（_progScroll，由 _autoScrollStreamingBody /
                    // _cpPrevTop 还原显式打标）被排除，不再依赖"近期滚轮"启发式：
                    // 旧逻辑只在「上滚」时刷新 _lastUserWheelAt，下滚回底不刷新，
                    // 一旦间隔 >800ms 标志卡死为"已离开"→ 内容更新时跟随失效、
                    // 视口被 _cpPrevTop 还原拖回旧阅读位置（弹回中间的根因）。
                    var atBottom = Math.abs(cp.scrollHeight - cp.scrollTop - cp.clientHeight) < 30;
                    cp._userScrolledUp = !atBottom;
                });
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
    """将 <ask>文本</ask> 或 [文本](jump/create/generate/view/session) 转换为胶囊样式的追问标签

    注：[文本](ask) 旧格式已废弃，不再识别（残留会渲染成空 markdown 链接）。

    session 类型格式：[文本](session|session_id|last_time)
    last_time 如果为空则不显示
    """

    # 新格式 <ask>内容</ask> → 直接生成胶囊
    # strip 后空内容（如 <ask></ask>、<ask>   </ask>、<ask>\n</ask>）丢弃整段标签，
    # 避免旧逻辑归一化为 [](ask) 后残留为字面 markdown 链接导致渲染崩（<a href="ask"></a>）。
    def _ask_replacer(m: re.Match) -> str:
        content = m.group(1).strip()
        if not content:
            return ""
        attrs = f'data-type="ask" data-content="{escape(content)}" data-action="ask"'
        return f'<span class="context-tag" {attrs}>{content}</span>'

    md_text = _ASK_TAG_PATTERN.sub(_ask_replacer, md_text)

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


def _accent_rgba(accent: str, alpha: float) -> str:
    """主题 accent hex → 指定 alpha 的 rgba() 字符串。

    供消息卡 CSS 派生色（边框/微光）使用，随主题切换自动取色，
    替代历史上按 midnight 深色主题硬编码的 rgba(100,198,255,*)。
    """
    h = (accent or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) == 6:
        try:
            return f"rgba({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}, {alpha})"
        except ValueError:
            pass
    return f"rgba(100, 198, 255, {alpha})"  # 解析失败兜底：原 midnight 色


# ======== 本地 Vendor JS 脚本（离线优先，CDN 降级） ========
# 图表放大/导出通道 payload b64 上限（与 chart_viewer_card._MAX_PAYLOAD_B64 一致）
_MAX_CHART_PAYLOAD_B64 = 8 * 1024 * 1024
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


_mermaid_vendor_urls_cache: Optional[tuple] = None


def _get_mermaid_vendor_urls() -> tuple:
    """返回 (polyfill_url, mermaid_url)，本地优先、缺失时 mermaid 降级 CDN。

    **不并入** `_get_vendor_script_tags()`：那份结果被写进骨架 HTML 并被
    `_skeleton_cache` 缓存，会让**每条消息**都背上 3.3MB 的 mermaid。
    mermaid 由 JS 侧在真正遇到 ```mermaid 块时才动态加载，见 `renderMermaidBlocks`。

    polyfill 必须在 mermaid 之前加载：Qt 5.15.2 的 WebEngine 是 Chromium 83
    （实测 navigator.userAgent 确认），缺 `structuredClone` / `Object.hasOwn` /
    `String.replaceAll` / `Array.prototype.at`，而 mermaid 10 在**模块顶层**
    就会用到，缺一个即整体 `undefined`。详见 `docs/mermaid-chromium83.md`。

    polyfill 是项目自带文件，无 CDN 版本；本地缺失时返回空串，
    JS 侧跳过它（mermaid 大概率仍会失败，但不影响卡片其余部分渲染）。
    """
    global _mermaid_vendor_urls_cache
    if _mermaid_vendor_urls_cache is not None:
        return _mermaid_vendor_urls_cache

    base_dirs = [_PROJECT_ROOT]
    if hasattr(sys, "_MEIPASS"):
        base_dirs.append(sys._MEIPASS)

    polyfill_url = ""
    mermaid_url = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"

    for base in base_dirs:
        candidate = os.path.join(base, "app/resources/web/vendor/chromium83-polyfill.js")
        if os.path.isfile(candidate):
            polyfill_url = QUrl.fromLocalFile(candidate).toString()
            break

    for base in base_dirs:
        candidate = os.path.join(base, "app/resources/web/vendor/mermaid.min.js")
        if os.path.isfile(candidate):
            mermaid_url = QUrl.fromLocalFile(candidate).toString()
            break

    _mermaid_vendor_urls_cache = (polyfill_url, mermaid_url)
    return _mermaid_vendor_urls_cache


# ======== WebViewer ========
class ConsoleMonitorPage(QWebEnginePage):
    codeActionRequested = pyqtSignal(str, str)
    contextActionRequested = pyqtSignal(str, str)
    heightReported = pyqtSignal(int)
    # 🐛 滚动判据修复：wheelEvent 原先只能用 page().scrollPosition()（文档级），
    # 但真正的滚动容器是 body（CSS: body{overflow-y:scroll}），文档级 scrollTop
    # 恒为 0 → at_top 恒真 / at_bottom 恒假 → 向下滚动永远被判为"内部处理"，
    # 而内部其实滚不动，事件被吞 → 卡片内滚动完全失效。
    # 故在 reportHeight 回传时顺带携带 body 的真实滚动几何：
    # (scrollHeight, scrollTop, clientHeight)。
    bodyGeometryReported = pyqtSignal(int, int, int)
    contentReady = pyqtSignal()
    toolDiffRequested = pyqtSignal(str)  # tool_call_id
    subAgentLogRequested = pyqtSignal(str)  # task_ids (comma-separated)
    saveFileRequested = pyqtSignal(str, str)  # code, lang
    chartExpandRequested = pyqtSignal(str, str)  # (chart_type, payload_b64) — echarts/mermaid 放大查看
    saveChartPngRequested = pyqtSignal(str, str)  # (name_b64, png_b64) — 图表 PNG 导出回传

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

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        """拦截 file:// 链接点击：用系统默认程序打开（不导航）。

        用 PyQt 原生 navigation 钩子（不写 JS 拦截），符合"浏览器自带"语义。
        """
        from PyQt5.QtGui import QDesktopServices
        from PyQt5.QtWebEngineWidgets import QWebEnginePage

        if url.scheme() == "file" and nav_type == QWebEnginePage.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        msg = message.strip()
        # [PERF] pywebview_height 是最高频信号（流式时每周期多次触发），
        # 放在首位快速短路，避免对每条 height 消息都做 startswith("pywebview_ready") 等冗余判断
        if msg.startswith("pywebview_height:"):
            # 协议：'pywebview_height:<scrollHeight>[|<scrollTop>|<clientHeight>]'
            # 后两字段由 body 几何上报新增；旧格式（仅高度）仍兼容——骨架在 JS
            # 尚未注入、或第三方/降级路径下可能只发高度，此时不发射几何信号，
            # wheelEvent 回退到保守策略。
            try:
                payload = msg.split(":", 1)[1]
                if "|" in payload:
                    h_str, st_str, ch_str = payload.split("|", 2)
                    h = int(float(h_str))
                    self.heightReported.emit(h)
                    self.bodyGeometryReported.emit(h, int(float(st_str)), int(float(ch_str)))
                else:
                    self.heightReported.emit(int(float(payload)))
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
            elif msg.startswith("pywebview_action:chart_expand:"):
                # 图表放大查看请求：console.log('pywebview_action:chart_expand:<type>:<b64>')
                try:
                    rest = msg.split("pywebview_action:chart_expand:", 1)[1]
                    chart_type, payload = rest.split(":", 1)
                    if chart_type in ("echarts", "mermaid") and len(payload) <= _MAX_CHART_PAYLOAD_B64:
                        self.chartExpandRequested.emit(chart_type, payload)
                except Exception:
                    pass
            elif msg.startswith("pywebview_action:save_chart_png:"):
                # 图表 PNG 导出回传：console.log('pywebview_action:save_chart_png:<name_b64>:<png_b64>')
                try:
                    rest = msg.split("pywebview_action:save_chart_png:", 1)[1]
                    name_b64, png_b64 = rest.split(":", 1)
                    if len(png_b64) <= _MAX_CHART_PAYLOAD_B64:
                        self.saveChartPngRequested.emit(name_b64, png_b64)
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


class _DialogEventFilter(QObject):
    """全局对话框事件过滤器（模块级单例 + viewer 注册表）。

    原实现：每个 CodeWebViewer 都向 QApplication 安装一个全局事件过滤器，
    N 个 viewer = N 个过滤器，任意鼠标移动等事件都会触发 O(N) 次 eventFilter
    转发。本类合并为单实例：同一事件只经过一次 eventFilter，按 event.type()
    快速短路（仅关心 Show/FocusIn/Hide/Close/Destroy 5 类低频事件），再遍历
    注册表分发，高频事件路由降为 O(1)。
    """

    # 关注的事件类型（QEvent 枚举值）：
    # Show=17, FocusIn=8（弹窗出现）; Hide=18, Close=19, Destroy=52（弹窗关闭/销毁兜底恢复）
    _WATCHED_EVENT_TYPES = (17, 8, 18, 19, 52)
    # 弹窗类名关键词（与原每 viewer 独立过滤器的判定一致）
    _POPUP_KEYWORDS = (
        "Dialog",
        "Popup",
        "Flyout",
        "InfoBar",
        "Toast",
        "ComboBox",
        "Menu",
        "ToolTip",
    )

    def __init__(self):
        super().__init__()
        self._viewers = set()  # 已注册的 CodeWebViewer 集合（生命周期随 viewer 增删）
        self._attached = False  # 是否已安装到 QApplication（幂等 attach/detach 标志）

    def register(self, viewer):
        """注册 viewer：确保全局过滤器已安装（幂等）；销毁时自动注销防引用滞留"""
        # 每次注册都检查安装：QApplication 尚未创建（服务先行等时序）时
        # 本次警告跳过，后续 register 会再次尝试，时序问题可自愈
        self._attach_to_application()
        self._viewers.add(viewer)
        try:
            # 兜底：viewer 未走 cleanup（正常路径 deleteLater → cleanup）就销毁时，
            # 自动从注册表移除，避免单例过滤器滞留已销毁对象引用
            viewer.destroyed.connect(self._on_viewer_destroyed)
        except RuntimeError, TypeError:
            pass

    def unregister(self, viewer):
        """注销 viewer：从注册表移除并断开销毁监听（幂等，销毁后调用亦安全）。

        最后一个 viewer 注销后从 QApplication 卸载过滤器（对称清理）。
        """
        self._viewers.discard(viewer)
        try:
            viewer.destroyed.disconnect(self._on_viewer_destroyed)
        except RuntimeError, TypeError:
            pass
        if not self._viewers:
            self._detach_from_application()

    def _attach_to_application(self):
        """向 QApplication 安装本过滤器（幂等：已安装则直接返回）。

        QApplication.instance() 为 None（如服务先行创建）时输出警告，
        由后续 register 再次尝试补装，时序问题可自愈。
        """
        if self._attached:
            return
        try:
            from PyQt5.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None:
                logger.warning("[MessageCard] 全局事件过滤器未安装：QApplication 尚未创建")
                return
            app.installEventFilter(self)
            self._attached = True
        except Exception as e:
            logger.warning(f"[MessageCard] 全局事件过滤器安装异常: {e}")

    def _detach_from_application(self):
        """从 QApplication 卸载过滤器（最后一个 viewer 注销/销毁时对称清理）"""
        if not self._attached:
            return
        try:
            from PyQt5.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._attached = False
        except Exception as e:
            logger.warning(f"[MessageCard] 全局事件过滤器卸载异常: {e}")

    @staticmethod
    def _is_viewer_alive(viewer) -> bool:
        """判断 viewer 的 C++ 对象是否仍存活（sip 判活，销毁过程中调用安全）"""
        try:
            sip.unwrapinstance(viewer)
            return True
        except RuntimeError:
            return False

    def _on_viewer_destroyed(self, *_args):
        """任一 viewer 销毁：惰性清理注册表中 C++ 对象已删的条目。

        destroyed 信号在销毁过程中发射，其 QObject 参数可能被 PyQt 包装为
        新 wrapper（与原对象不等），故不依赖参数匹配，改用 sip 判活清理。
        """
        for v in tuple(self._viewers):
            if not self._is_viewer_alive(v):
                self._viewers.discard(v)
        if not self._viewers:
            self._detach_from_application()

    def eventFilter(self, obj, event):
        # 快速短路：非关注事件（鼠标移动/绘制/键盘等高频事件）立即返回，
        # 不再像旧实现那样对每个 viewer 转发一次
        event_type = event.type()
        if event_type not in self._WATCHED_EVENT_TYPES:
            return False
        # 关注事件为低频事件（弹窗显示/关闭），此时才遍历注册表分发
        for viewer in tuple(self._viewers):
            try:
                self._dispatch(viewer, obj, event_type)
            except RuntimeError as e:
                # 区分「viewer 已销毁」与「父链对象已删等瞬时异常」：
                # 前者惰性剔除防引用滞留；后者 viewer 仍存活，仅记录日志
                # 不剔除（误剔除会使 MaskDialog 防穿透静默失效且无法恢复）
                if self._is_viewer_alive(viewer):
                    logger.debug(f"[MessageCard] 对话框过滤分派异常（viewer 存活）: {e}")
                else:
                    self._viewers.discard(viewer)
        return False

    def _dispatch(self, viewer, obj, event_type):
        """对单个 viewer 执行过滤逻辑（与原每 viewer 独立过滤器的行为等价）"""
        if event_type in (17, 8):  # QEvent.Show, QEvent.FocusIn
            obj_class = obj.__class__.__name__
            if any(kw in obj_class for kw in self._POPUP_KEYWORDS):
                # 只对透明遮罩对话框（MaskDialogBase 等）隐藏 WebView 防穿透；
                # 普通对话框（QFileDialog 等）无需隐藏，仅降低层级即可
                if "Dialog" in obj_class and hasattr(obj, "winId") and viewer._is_mask_dialog(obj):
                    # 全屏 MaskDialog → 隐藏 WebView 防止原生 HWND 穿透遮罩
                    viewer._hide_for_dialog(obj)
                else:
                    # 小弹窗（Menu/ComboBox/ToolTip等）→ 降低 Qt 层级
                    viewer.lower()
                    parent = viewer.parent()
                    while parent:
                        parent.lower()
                        # 找到 MessageCard 或聊天容器为止
                        if hasattr(parent, "chat_layout") or parent.__class__.__name__ == "MessageCard":
                            break
                        parent = parent.parent()
                    if hasattr(obj, "raise_"):
                        obj.raise_()
        else:  # QEvent.Hide, QEvent.Close, QEvent.Destroy
            # 兜底恢复：对话框关闭/隐藏/销毁时，若它是导致 WebView 隐藏的对象则恢复
            hidden = getattr(viewer, "_hidden_dialogs", None)
            if hidden and obj in hidden:
                hidden.discard(obj)
                if not hidden:
                    viewer.show()


# 模块级单例：全局仅此一个 QApplication 级事件过滤器
_dialog_event_filter = _DialogEventFilter()


class CodeWebViewer(QWebEngineView):
    contentHeightChanged = pyqtSignal(int)
    codeActionRequested = pyqtSignal(str, str)
    contextActionRequested = pyqtSignal(str, str)
    toolDiffRequested = pyqtSignal(str)  # tool_call_id
    subAgentLogRequested = pyqtSignal(str)  # task_ids (comma-separated)
    saveFileRequested = pyqtSignal(str, str)  # code, lang
    chartExpandRequested = pyqtSignal(str, str)  # (chart_type, payload_b64) — 图表放大查看
    saveChartPngRequested = pyqtSignal(str, str)  # (name_b64, png_b64) — 图表 PNG 导出回传
    # WebEngine 上下文丢失信号
    contextLost = pyqtSignal()
    contextRestored = pyqtSignal()
    needRecreate = pyqtSignal()  # 需要完全重建控件（恢复失败时）

    # [B3] 线程池渲染完成信号（worker 线程 emit → 主线程槽执行）：
    # 不能从 worker 线程直接调用 QTimer.singleShot(0, ...)（worker 无事件循环，
    # 定时器事件不会投递到主线程）；Qt 信号跨线程 emit 是线程安全的，
    # 自动 QueuedConnection 到主线程执行 _apply_render_result。
    renderDone = pyqtSignal(int, object)  # (seq, html)

    # WebEngine 最大尺寸限制，防止 GPU 内存溢出
    # 降低 MAX_HEIGHT 可大幅减少每个 Chromium 实例的离屏渲染缓冲区
    # 4000→2000 将单视图 GPU 缓冲区从 ~28.8MB 降至 ~14.4MB
    #
    # 🐛 滚动体验重构（2026-08-30）：原 3000px 会让长回复（多个代码块 + 工具结果）
    # **在卡片内部**出现滚动条 —— 消息列表于是变成「外层 1 个 QScrollArea + 每张卡
    # 各自 1 个内滚区」，滚轮要先问「里面还能滚吗」再决定转发，是滚动发黏的根因。
    # 现在把上限抬到 10000px：真实内容几乎不可能触及，卡内滚动条不再出现，
    # 滚动统一由外层 chat_scroll_area 承载。
    # 保留上限的原因（**不能删**）：这是 Chromium 合成表面的硬约束兜底 ——
    # 卡片宽度 ~700px 时 10000px 高 ≈ 28MB 合成表面，再往上会明显放大 GPU 内存。
    # 极端长内容仍会回退到内滚（wheel 转发逻辑因此必须保留），但那是安全网而非常态。
    MAX_WIDTH = 1800
    MAX_HEIGHT = 10000

    def __init__(self, parent=None, light=False):
        super().__init__(parent)
        # [B4-强回收] renderer 进程 PID（强回收层 kill 离屏进程用；0 = 未就绪/已清理）
        self._renderer_pid: int = 0
        # [B3] 连接线程池渲染完成信号（worker 线程 emit → 本槽在主线程执行）
        self.renderDone.connect(self._on_render_done_signal)
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
        # [B2] 工具 DOM 脏标记：MessageCard 层 JS 增量注入工具块（_inject_tool_streaming_html /
        # append_tool_result）时置 True，_perform_update 则必须走 save/restore 保护（否则
        # updateContent 整块替换会被 JS 注入的运行框/完成框抹掉）；无工具 DOM 注入时走裸
        # updateContent（省整页 save/restore JS 包装，MB 级 IPC 瘦身）。更新成功后置 False。
        # 🐛 修复（编辑工具框运行中消失）：清除时机延后到 JS 渲染回调执行完成后（runJavaScript
        # 异步），并带双重守卫——_injected_pending_tools 非空（仍有 JS 注入未完成的工具块在
        # DOM）或代际变化（_tool_dom_dirty_gen 递增，期间有新注入）时**不清除**，避免下一次
        # 全量渲染误判"无工具 DOM 需保护"→ 裸 updateContent 抹掉运行框。
        self._tool_dom_dirty: bool = False
        # [B2] 工具 DOM 脏标记代际：每次置 True 时递增，JS 回调清除时与捕获值比较，
        # 防止"旧渲染回调误清新注入的 dirty"（新注入已递增代际 → 旧回调放弃清除）。
        self._tool_dom_dirty_gen: int = 0
        # [B2] JS 注入但尚未完成（结果未 append_tool_result）的工具 id 集合。
        # 这些工具的运行框/预览块只存在于 DOM、不在 markdown 中，全量渲染必须
        # save/restore 保护；集合非空时禁止清除 _tool_dom_dirty。
        self._injected_pending_tools: set = set()
        # [B3] 异步渲染序号与防抖状态（渲染移出主线程）：
        # - _render_seq：递增序号，回调时校验，过期结果（新渲染已提交）直接丢弃
        # - _render_inflight：是否有在途线程池渲染任务（防抖：在途时只记 pending）
        # - _render_pending：在途期间积压的最新 (seq, md, compact) 快照，完成后续派
        self._render_seq: int = 0
        self._render_inflight: bool = False
        self._render_pending: Optional[tuple] = None
        # [V1] 可见性门控：隐藏 tab 期间被门控跳过的渲染请求标记，
        # 恢复可见时（showEvent）据此补渲，保证流式/工具结果最终完整性。
        self._render_deferred: bool = False
        # [PERF] 主题刷新期间不可见 → 跳过 JS 注入，恢复可见时补注入标记
        self._theme_css_pending: bool = False
        # [B1] 差量渲染状态：
        # - _stable_html：已追加到 DOM 的稳定格式化 HTML 累积
        # - _stable_md_len：已差量消费的 markdown 偏移（后续 _extract_closed_segments 从这扫描）
        # - _needs_full_render：需要全量渲染（初值/主题/字体/状态切换/流式结束/缓存清理）
        self._stable_html: str = ""
        self._stable_md_len: int = 0
        # [B1] 尾部行内渲染哈希缓存：安全定时器重复触发时 tail 未变则跳过渲染
        self._tail_html_hash: int = 0
        self._needs_full_render: bool = True
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
        # 🐛 滚动判据修复：body 的真实滚动几何（由 reportHeight 顺带回传）。
        # _body_client_height: body 可视高度；_body_scroll_top: body 已滚动距离。
        # 真实可滚动量 = _document_height - _body_client_height，
        # 该值是"卡片内部能否滚动"的唯一正确判据。
        self._body_client_height = 0
        self._body_scroll_top = 0
        self._body_geom_valid = False
        # 自愈计数：连续判定为"内部处理"但 body.scrollTop 纹丝不动的滚轮次数。
        # 达到阈值说明内部其实滚不动（几何缓存滞后/内容未溢出），强制转发外部，
        # 彻底消除"怎么滚都没反应"的粘性失效。
        self._wheel_stuck_streak = 0
        self._wheel_last_scroll_top = -1
        self._wheel_last_ts = 0.0
        self._wheel_delegated_inner = False

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
        self._page.bodyGeometryReported.connect(self._on_body_geometry_reported)
        self._page.contentReady.connect(self._on_js_ready)
        self._page.toolDiffRequested.connect(self.toolDiffRequested.emit)
        self._page.subAgentLogRequested.connect(self.subAgentLogRequested.emit)
        self._page.saveFileRequested.connect(self.saveFileRequested.emit)
        self._page.chartExpandRequested.connect(self.chartExpandRequested.emit)
        self._page.saveChartPngRequested.connect(self.saveChartPngRequested.emit)

        self._load_skeleton()

        # ── 对话框层级管理 ──
        # _hidden_dialogs: set，记录当前导致 WebView 隐藏的对话框对象
        self._hidden_dialogs = set()

    # ──────────────────────────────────────────────
    # 对话框 HWND 穿透防护
    # ──────────────────────────────────────────────
    # QWebEngineView 在 Windows 上创建原生 HWND 子窗口，
    # 遇到 WA_TranslucentBackground 的 MaskDialog 分层窗口时，
    # Chromium GPU 合成表面可能穿透遮罩渲染在对话框之上。
    # 策略：检测到透明遮罩对话框显示时隐藏 WebView，
    #       对话框关闭（finished）或销毁（destroyed）后恢复；
    #       额外用 eventFilter 监听 Hide/Close/Destroy 事件兜底，
    #       避免原生对话框（无 Qt 信号）导致永久隐藏。

    def _hide_for_dialog(self, dialog):
        """对话框显示时隐藏 WebView，防止原生 HWND 穿透遮罩"""
        hidden = getattr(self, "_hidden_dialogs", None)
        if hidden is None:
            hidden = set()
            self._hidden_dialogs = hidden
        if dialog in hidden:
            return  # 同一对话框重复 Show/FocusIn 不叠加计数
        hidden.add(dialog)
        self.hide()
        # finished + destroyed 双信号：dismiss 即恢复，销毁兜底
        for sig_name in ("finished", "destroyed"):
            try:
                sig = getattr(dialog, sig_name, None)
                if sig is not None:
                    sig.connect(self._restore_from_dialog)
            except TypeError, RuntimeError, AttributeError:
                pass

    def _restore_from_dialog(self, _result=None):
        """对话框关闭后恢复 WebView 显示（_result 为 QDialog.finished 的 result code）"""
        hidden = getattr(self, "_hidden_dialogs", None)
        if not hidden:
            return
        sender = self.sender()
        if sender is not None:
            hidden.discard(sender)
        if not hidden:
            self.show()

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
        #
        # ⚠️ **这段转发不能删**：QWebEngineView 会把滚轮事件喂给内嵌 Chromium 并吞掉，
        # 不会自动冒泡到外层 QScrollArea。所以哪怕卡片内部不可滚，也必须显式转发，
        # 否则鼠标停在消息上滚轮毫无反应。
        # 现状（MAX_HEIGHT 抬到 10000 之后）：body 几乎恒不溢出 → max_scroll 恒为 0 →
        # 每次滚轮都走「转发外层」这条最短路，内层判定的分支基本不再命中。
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

            delta = event.angleDelta().y()
            if delta == 0:
                super().wheelEvent(event)
                return

            # ── 核心修复：判据必须是 body 的滚动几何，而非文档级指标 ──
            # CSS 为 body 设置了 overflow-y:scroll + max-height，body 才是唯一
            # 的滚动容器。而 page().scrollPosition() 是文档级指标，在 body-scroller
            # 架构下恒为 0 → at_top 恒真、at_bottom 恒假 → 所有向下滚动都被判为
            # "内部处理"，但内部其实滚不动，事件被吞 → 卡片内滚动完全失效。
            # contentsSize() 同样不含 body 的内部溢出，也不能用作判据。
            scroll_y, max_scroll = self._inner_scroll_range()

            if not self._body_geom_valid:
                # 几何尚未上报（首帧 / JS 未就绪 / 上报丢失）→ 必须转发外部。
                # ⚠️ 绝不能退化成"交给内部处理"：那正是本次修复的核心失效模式——
                # 绝大多卡片的 viewer 高度 == 内容高度（内部本就不可滚），
                # 一旦把事件交给内部就会被无声吞掉，表现为怎么滚都没反应。
                # 转发外部是这个不确定状态下的正确默认；真正需要内部滚动的
                # 只有内容超过 MAX_HEIGHT 的长卡片，而其内容渲染完必然已上报几何。
                outer_vbar.setValue(outer_vbar.value() - wheel_delta_to_px(delta))
                event.accept()
                return

            # ── 消除滞后假信号 ──
            # viewer 高度已等于文档高度 ⇒ 内容已完全展开，body 不可能还有可滚动量。
            # 此时残留的 max_scroll 只可能来自 resize/重排前的旧几何（内容变宽后
            # 高度已收拢，但 body 几何缓存尚未刷新），必须清零，否则会误判内部可滚。
            if max_scroll > SCROLL_BOUNDARY_TOLERANCE and abs(self.height() - self._document_height) <= 12:
                max_scroll = 0

            # ── 自愈：连续委托内部却毫无位移 → 强制转外部 ──
            # 覆盖所有残余的缓存不同步场景（如高度收敛窗口期内反复误判）。
            # 密集滚动期间（间隔 <100ms）不计入，避免帧级上报延迟造成误判。
            now = time.monotonic()
            if self._wheel_delegated_inner:
                if scroll_y == self._wheel_last_scroll_top and (now - self._wheel_last_ts) > WHEEL_STUCK_MIN_INTERVAL:
                    self._wheel_stuck_streak += 1
                else:
                    self._wheel_stuck_streak = 0
            self._wheel_delegated_inner = False

            at_top = scroll_y <= SCROLL_BOUNDARY_TOLERANCE
            at_bottom = scroll_y >= (max_scroll - SCROLL_BOUNDARY_TOLERANCE)
            inner_can_scroll = max_scroll > SCROLL_BOUNDARY_TOLERANCE and self._wheel_stuck_streak < WHEEL_STUCK_LIMIT

            if inner_can_scroll and not ((delta < 0 and at_bottom) or (delta > 0 and at_top)):
                # 内部还有可滚动空间 → 让内部处理
                self._wheel_delegated_inner = True
                self._wheel_last_scroll_top = scroll_y
                self._wheel_last_ts = now
                super().wheelEvent(event)
                return

            # 内部不可滚 / 已到边界 → 转发到外部
            self._wheel_stuck_streak = 0
            outer_vbar.setValue(outer_vbar.value() - wheel_delta_to_px(delta))
            event.accept()
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
        """注册到全局单例事件过滤器（注册表方式，不再每 viewer 安装一个过滤器）"""
        _dialog_event_filter.register(self)

    def _is_mask_dialog(self, obj) -> bool:
        """判断是否为透明遮罩对话框（WA_TranslucentBackground，需防穿透）"""
        try:
            return bool(obj.testAttribute(Qt.WA_TranslucentBackground))
        except Exception:
            return False

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

    def _on_body_geometry_reported(self, scroll_height: int, scroll_top: int, client_height: int):
        """缓存 body 的真实滚动几何（reportHeight 顺带回传）。

        body 是唯一的滚动容器（CSS: body{overflow-y:scroll; max-height}），
        因此"卡片内部能否滚动"只能由这三个值判定：
            可滚动量 = scrollHeight - clientHeight
        而 Qt 侧的 page().scrollPosition()/contentsSize() 是文档级指标，
        在 body-scroller 架构下恒为 0 / 不含 body 内部溢出，不能作为判据。
        """
        self._document_height = scroll_height
        self._body_scroll_top = scroll_top
        self._body_client_height = client_height
        self._body_geom_valid = client_height > 0

    def _inner_scroll_range(self) -> tuple:
        """返回 (scroll_top, max_scroll)：body 当前滚动位置与最大可滚动距离。

        无有效几何缓存时返回 (0, 0)，调用方据此走保守策略。
        """
        if not self._body_geom_valid:
            return 0, 0
        max_scroll = max(0, self._document_height - self._body_client_height)
        return self._body_scroll_top, max_scroll

    def update_height(self):
        """主动触发一次高度/几何上报（供外部宽度同步后驱动）。

        sync_width 原本只对 PlainTextViewer 生效（CodeWebViewer 无此方法），
        导致 resize 恢复后完全被动等待 JS 的 ResizeObserver + rAF×3 才上报，
        卡片高度收敛慢（表现为"窗口变大后内容迟迟不适配"）。补此方法后，
        宽度同步可主动驱动一次上报，无需等 ResizeObserver 的三帧延迟。
        """
        self._do_resize_check()

    def showEvent(self, event):
        """[V1] 可见性恢复：隐藏 tab 期间被门控的渲染请求在此补渲。

        tab 切回时 Qt 会向子 widget 传播 Show 事件（QStackedWidget 隐藏页
        isVisible()=False，切回后重新可见触发本事件）。若隐藏期间积压了
        渲染请求（_render_deferred），恢复可见后按 _schedule_render 现有
        调度机制补渲，保证流式输出/工具结果的最终完整性。
        """
        super().showEvent(event)
        # [PERF] 主题刷新期间不可见 → 跳过 JS 注入，恢复可见时补注入
        # CSS 变量（updateContent 不重载骨架，旧主题色会残留）
        if getattr(self, "_theme_css_pending", False):
            self._theme_css_pending = False
            try:
                from app.utils.theme_manager import theme_manager as _tm

                _is_light = _tm.is_light_theme()
                _theme = current_theme()
                from app.utils.theme_refresh import ThemeRefreshCoordinator

                _js = ThemeRefreshCoordinator.get_or_build_js(_theme, _is_light)
                if self.page():
                    self.page().runJavaScript(_js)
            except Exception:
                pass
        if getattr(self, "_render_deferred", False):
            if self._is_js_ready:
                self._render_deferred = False
                self._schedule_render(immediate=True)
            # 🛡️ F2：JS 未就绪时保留 deferred（不清标志）——先清标志再调
            # _schedule_render 会因 JS 未就绪直接 return，积压请求被清但永不
            # 补渲；保留后由 _on_js_ready 统一补渲（可以延迟，不能丢失）。

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
                    "if(_ts){if(typeof _beginToolSectionTransition==='function')_beginToolSectionTransition();"
                    "_ts.setAttribute('data-collapsed','true');}"
                    "if(_sep)_sep.setAttribute('aria-expanded','false');"
                )
            # 坞态同步：由 JS 读取 tool-section 的 data-collapsed 属性判断
            # collapsed="true"（历史会话/已完成）→ dock off
            # collapsed="false"（流式会话默认）→ dock on（受 _toolCompactMode 守卫）
            # 此 JS 在上方 collapse 之后执行，保证 data-collapsed 已更新到正确值
            # 🆕 F2（S2 兜底）：DOM 中存在运行中工具块（data-streaming="true"）
            # 时强制 dock on——覆盖"JS 就绪晚于工具流式注入"的竞态窗口（_on_js_ready
            # 执行时 _streaming 可能已 False，但运行中块仍在 DOM 等待结果）。
            # 🛡️ 欢迎卡片（light 骨架）跳过：坞态会限死正文高度，欢迎页长内容被截断。
            if not self._light_skeleton:
                self.page().runJavaScript(
                    "var _ts2=document.getElementById('tool-section');"
                    "var _co=_ts2&&_ts2.getAttribute('data-collapsed')==='true';"
                    "var _act=document.querySelector('#tool-content [data-tool-call-id][data-streaming=\"true\"]');"
                    "if(typeof _setStreamingDock==='function')_setStreamingDock(!!_act||!_co);"
                )
        except RuntimeError:
            pass
        # 🐛 修复：流式内容可能在 JS 就绪前通过 _lazy_markdown_cb 缓存，
        # 仅检查 _markdown_text 会遗漏这些内容，导致卡片永久空白。
        # 当 _lazy_markdown_cb 存在时也触发渲染，_perform_update 会消费它。
        # 🛡️ F2：_render_deferred 积压（JS 未就绪期间 / 隐藏期间门控的渲染请求）
        # 在 JS 就绪时统一补渲——保证 viewer 创建后未显示 + JS 未加载期间的
        # 积压渲染在恢复后必然补上（可以延迟，不能丢失）。
        if self._render_deferred or self._markdown_text or self._lazy_markdown_cb:
            self._render_deferred = False
            self._schedule_render(immediate=True)
        # [B4-强回收] 记录 renderer 进程 PID（强回收层 kill 离屏进程用）
        try:
            self._renderer_pid = self.page().renderProcessPid()
        except Exception:
            self._renderer_pid = 0
        # 任务列表补推：骨架重载（主题/字体变化 setHtml）会清空 JS 注入的
        # todo DOM；JS 就绪后按 _pending_todos 快照重推，保证卡片底部
        # 任务列表在骨架重建后不丢失。
        if getattr(self, "_pending_todos", None) is not None:
            try:
                payload = json.dumps(self._pending_todos).decode("utf-8")
                self.page().runJavaScript(f"window._updateTodoList && window._updateTodoList({payload});")
            except RuntimeError:
                pass

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
        # mermaid vendor URL 进 key：热替换 vendor 文件后能让旧骨架缓存失效
        _mmd_polyfill_url, _mmd_lib_url = _get_mermaid_vendor_urls()
        # mermaid 主题联动：取 Colors 而非硬编码，避免浅色主题下白叠白
        # （MEMORY.md 记录的反复出现的缺陷模式）。Colors 大写属性由主题 YAML 自动填充。
        mmd_text_color = Colors.TEXT_PRIMARY
        mmd_line_color = Colors.TEXT_SECONDARY
        mmd_node_bg = Colors.CONTENT_BG
        mmd_border = Colors.BORDER
        cache_key = (
            # 🆕 方案 A（#33）：骨架缓存版本号——骨架 JS/DOM 结构变更时递增，
            # 防止旧版骨架缓存与新代码混合导致 JS 行为不一致（卡片空白根因之一）。
            _SKELETON_CACHE_VERSION,
            self._light_skeleton,
            theme_fp,
            font_family,
            font_family_global,
            body_font_size,
            code_font_size,
            tag_font_size,
            small_font_size,
            tiny_font_size,
            _mmd_polyfill_url,
            _mmd_lib_url,
            mmd_text_color,
            mmd_line_color,
            mmd_node_bg,
            mmd_border,
        )
        cached = _skeleton_cache.get(cache_key)
        if cached is not None:
            _skeleton_cache.move_to_end(cache_key)  # LRU：命中提升为最新
            self.setHtml(cached, QUrl.fromLocalFile(_PROJECT_ROOT + "/"))
            return

        tag_css = []
        for act, col in ACTION_COLOR_MAP.items():
            tag_css.append(
                f'.context-tag[data-type="{act}"] {{ background: {col}15; border-color: {col}60; color: {col}; }}'
            )
            tag_css.append(f'.context-tag[data-type="{act}"]:hover {{ background: {col}30; border-color: {col}; }}')

        # 离线优先：本地 vendor JS（app/resources/web/vendor/），缺失时降级 CDN。
        # light 骨架（欢迎卡片）也加载 echarts：欢迎 tab 插件（如 context-stats）
        # 通过 ```echarts 代码块渲染图表，依赖 window.echarts 存在。
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
                    /* 语义派生层：欢迎卡/表格等组件用，浅/深主题通吃（P0 去硬编码） */
                    --accent-text: {theme["accent"]};
                    --accent-soft: {theme["hover_bg"]};
                    --accent-soft-strong: {theme["selected_bg"]};
                    --accent-border-weak: {_accent_rgba(theme["accent"], 0.22)};
                    --accent-glow: {_accent_rgba(theme["accent"], 0.10)};
                    --row-alt: {"rgba(15, 23, 42, 0.03)" if _is_light else "rgba(255, 255, 255, 0.02)"};
                    --row-hover: {"rgba(15, 23, 42, 0.05)" if _is_light else "rgba(255, 255, 255, 0.05)"};
                    /* 表头底色：比 --row-hover 更实一层，浅色主题下用深色叠加而非白叠加，
                       否则白底上叠白 = 表头与表体完全无分界（此前的硬编码缺陷）。 */
                    --row-header: {"rgba(15, 23, 42, 0.06)" if _is_light else "rgba(255, 255, 255, 0.04)"};
                    /* 圆角节奏（与 design_tokens.BorderRadius 同源，4/6/10/14/18/全圆） */
                    {_BORDER_RADIUS_CSS_VARS}
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
                    /* 右侧 8px = 14px 视觉边距 - 6px 常驻滚动轨道：轨道占位使
                       内容右视觉边距比左侧多 6px，扣减后左右对称 */
                    padding: 6px 8px 0 14px;
                    /* ⚠️ 安全网，非常态：MAX_HEIGHT 已抬到 10000px（见类常量注释），
                       真实内容几乎不会触及 → body 不会溢出 → 不出现内滚条 →
                       滚动统一由外层 chat_scroll_area 承载。
                       触及上限时（极端长内容）才回退为卡内滚动，此时 wheelEvent 的
                       内外转发逻辑仍然生效，是最后一道兜底。 */
                    max-height: {self.MAX_HEIGHT}px;
                    /* 🛡️ 稳定性修复：滚动条轨道常驻，内容可用宽度恒定。
                       overflow-y:auto 时滚动条出现/消失会使内容宽度 ±6px 波动 →
                       长行换行变化 → scrollHeight 波动 → 高度报告 → setFixedHeight
                       → 滚动条再切换 → 反馈振荡（流式"一抖一抖"的主因之一）。
                       scroll 常驻轨道后宽度恒定，斩断反馈环。track 透明无视觉噪点。 */
                    overflow-y: scroll;
                    overflow-x: hidden;
                    overflow-anchor: auto;
                }}
                /* body 滚动轨道常驻但视觉隐形（覆盖全局 6px 滚动条样式的 track 底色） */
                body::-webkit-scrollbar-track {{
                    background: transparent;
                }}
                /* 内层滚动容器（工具区/任务列表/思考体/工具结果）轨道同样隐形：
                   常驻轨道(scroll) + 右 padding 扣减 6px，消除滚动条带来的右侧加宽 */
                #tool-content::-webkit-scrollbar-track,
                #todo-content::-webkit-scrollbar-track,
                .think-content::-webkit-scrollbar-track,
                .result-content::-webkit-scrollbar-track {{
                    background: transparent;
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
                    border-radius: var(--r-md);
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
                /* 排版优化：标题改用负字距（-0.01em）——字号越大越需收紧字距，
                   这是通行排版实践；正字距会让大标题显得松散。
                   同时统一标题行高 1.3，避免大字号下默认行高过松。 */
                h1, h2, h3, h4, h5, h6 {{
                    color: var(--text) !important;
                    font-weight: 700;
                    letter-spacing: -0.01em;
                    line-height: 1.3;
                }}
                /* 上边距 > 下边距：标题在视觉上归属于其后的内容（格式塔接近原则） */
                h1 {{ font-size: 1.45em; margin: 18px 0 8px; }}
                h2 {{ font-size: 1.25em; margin: 16px 0 6px; }}
                h3 {{ font-size: 1.1em; margin: 14px 0 4px; }}
                /* 正文行高 1.65：中英混排下兼顾可读性与密度（浏览器默认 ~1.2 过挤） */
                p {{ margin: 8px 0; color: var(--text-secondary); line-height: 1.65; }}
                a {{ color: var(--accent) !important; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
                ul, ol {{ margin: 8px 0; padding-left: 24px; line-height: 1.65; }}
                li {{ margin: 4px 0; color: var(--text-secondary); }}
                strong {{ color: var(--text) !important; font-weight: 600; }}
                em {{ color: var(--text-secondary) !important; font-style: italic; }}
                code:not(.code-content *):not(pre code) {{ 
                    background: var(--accent-glow) !important; 
                    color: var(--accent-text) !important;
                    padding: 2px 6px; 
                    border-radius: var(--r-sm); 
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
                    border-radius: var(--r-md);
                    overflow: hidden;
                    font-family: '{font_family}', sans-serif;
                    font-size: {body_font_size}px;
                }}
                table:not(.code-table) th {{
                    /* 原为硬编码 rgba(255,255,255,0.04)：浅色主题下白叠白，表头
                       与表体完全无分界。改用主题感知的 --row-header。 */
                    background: var(--row-header);
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
                /* 原为硬编码白色叠加，浅色主题不可见 → 改用已定义的语义变量 */
                table:not(.code-table) tr:nth-child(even) {{ background: var(--row-alt); }}
                table:not(.code-table) tr:hover {{ background: var(--row-hover); }}
                /* 表体行 hover 时文字提亮，增强可扫描性 */
                table:not(.code-table) tr:hover td {{ color: var(--text) !important; }}

                /* ── 表格滚动容器（JS 在 updateContent 中自动包裹每个 <table>） ── */
                .table-scroll-wrapper {{
                    overflow-x: auto;
                    overflow-y: hidden;
                    margin: 10px 0;
                    border: 1px solid var(--border);
                    border-radius: var(--r-md);
                }}
                .table-scroll-wrapper::-webkit-scrollbar {{
                    height: 8px;
                }}
                .table-scroll-wrapper::-webkit-scrollbar-thumb {{
                    background: var(--border);
                    border-radius: var(--r-xs);
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
                    /* 与上方 table th 同步：改用主题感知变量，修复浅色主题白叠白 */
                    background: var(--row-header);
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
                .table-scroll-wrapper > table tr:nth-child(even) {{ background: var(--row-alt); }}
                .table-scroll-wrapper > table tr:hover {{ background: var(--row-hover); }}

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

                /* session 历史会话标签样式（胶囊按内容宽度自然展开） */
                .session-tag {{
                    background: var(--accent-soft);
                    border-color: var(--accent-border-weak);
                    color: var(--accent-text);
                    margin: 4px 6px 4px 0;
                    max-width: 100%;
                }}
                .session-tag:hover {{
                    background: var(--accent-soft-strong);
                    border-color: var(--accent);
                }}
                /* session 时间显示在标题下方 */
                .session-tag .session-time {{
                    display: block;
                    font-size: {tiny_font_size}px;
                    font-weight: normal;
                    opacity: 0.6;
                    margin-top: 4px;
                    color: var(--accent-text);
                }}

                /* 欢迎卡片历史会话：分区标题 + 卡片行列表 */
                .session-section {{
                    margin: 4px 0 14px;
                }}
                .session-header {{
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    margin-bottom: 8px;
                }}
                .session-header-icon {{
                    font-size: {tag_font_size}px;
                    line-height: 1;
                }}
                .session-header-title {{
                    font-size: {tag_font_size}px;
                    font-weight: 600;
                    color: var(--text);
                    letter-spacing: 0.02em;
                }}
                .session-header-count {{
                    font-size: {tiny_font_size}px;
                    color: var(--accent-text);
                    background: var(--accent-soft);
                    border: 1px solid var(--accent-border-weak);
                    padding: 0 7px;
                    border-radius: 999px;
                    line-height: 1.7;
                }}
                .session-list {{
                    display: grid;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                    gap: 6px 8px;
                }}
                /* 会话卡片行：复用 .context-tag 点击事件链，覆盖胶囊默认外观 */
                .session-item.context-tag {{
                    display: flex;
                    align-items: center;
                    gap: 10px;
                    width: 100%;
                    box-sizing: border-box;
                    padding: 8px 12px;
                    margin: 0;
                    border-radius: 10px;
                    border: 1px solid var(--border);
                    background: var(--accent-soft);
                    font-weight: 500;
                    color: var(--text);
                    cursor: pointer;
                    transition: background 0.18s ease, border-color 0.18s ease,
                                transform 0.18s ease, box-shadow 0.18s ease;
                    max-width: 100%;
                }}
                .session-item.context-tag:hover {{
                    background: var(--accent-soft-strong);
                    border-color: var(--accent);
                    transform: translateX(2px);
                    box-shadow: 0 2px 10px var(--accent-glow);
                }}
                .session-item-badge {{
                    flex: 0 0 auto;
                    width: 30px;
                    height: 30px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    border-radius: 9px;
                    background: var(--accent-soft-strong);
                    font-size: 14px;
                    line-height: 1;
                }}
                .session-item-body {{
                    flex: 1;
                    min-width: 0;
                    display: flex;
                    flex-direction: column;
                    gap: 2px;
                }}
                .session-item-title {{
                    font-size: {tag_font_size}px;
                    font-weight: 600;
                    color: var(--text);
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }}
                .session-item-meta {{
                    font-size: {tiny_font_size}px;
                    color: var(--text-muted);
                    opacity: 0.85;
                }}
                .session-item-arrow {{
                    flex: 0 0 auto;
                    font-size: 15px;
                    color: var(--text-muted);
                    opacity: 0;
                    transform: translateX(-4px);
                    transition: opacity 0.18s ease, transform 0.18s ease;
                    line-height: 1;
                }}
                .session-item.context-tag:hover .session-item-arrow {{
                    opacity: 1;
                    transform: translateX(0);
                }}
                /* 卡片进入动画：逐行 stagger fade-in（backwards 保证延迟期隐藏，
                   播完恢复自然样式，不锁死 transform，hover 位移不受影响） */
                @keyframes session-item-in {{
                    from {{ opacity: 0; transform: translateY(6px); }}
                    to {{ opacity: 1; transform: translateY(0); }}
                }}
                .session-item.context-tag {{
                    animation: session-item-in 0.32s ease backwards;
                }}
                @media (prefers-reduced-motion: reduce) {{
                    .session-item.context-tag {{ animation: none; }}
                }}
                .welcome-empty {{
                    opacity: 0.55;
                    font-size: {tag_font_size}px;
                    padding: 8px 0;
                }}

                /* changelog 模式：左列版本列表 + 右列描述 */
                .changelog-shell {{
                    display: flex;
                    gap: 12px;
                    margin-top: 6px;
                    min-height: 200px;
                }}
                .changelog-versions {{
                    list-style: none;
                    padding: 0;
                    margin: 0;
                    min-width: 130px;
                    max-width: 160px;
                    border-right: 1px solid var(--accent-border-weak);
                    overflow-y: auto;
                    max-height: 360px;
                }}
                .changelog-version {{
                    padding: 6px 10px;
                    cursor: pointer;
                    border-radius: 6px;
                    margin-bottom: 2px;
                    transition: 0.15s ease;
                }}
                .changelog-version:hover {{
                    background: var(--accent-soft);
                }}
                .changelog-version.active {{
                    background: var(--accent-soft-strong);
                }}
                .changelog-version .ver-tag {{
                    font-weight: 600;
                    color: var(--accent-text);
                    font-size: {tag_font_size}px;
                }}
                .changelog-version .ver-date {{
                    font-size: {tiny_font_size}px;
                    opacity: 0.6;
                    margin-top: 2px;
                }}
                .changelog-detail {{
                    flex: 1;
                    min-width: 0;
                    overflow-y: auto;
                    max-height: 360px;
                    padding-right: 4px;
                }}
                .changelog-body h1, .changelog-body h2, .changelog-body h3 {{
                    color: var(--accent-text);
                    margin-top: 0;
                }}
                .changelog-body img {{ max-width: 100%; }}

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
                    color: var(--text-muted);
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

                /* 代码工具按钮 hover：原为硬编码白色叠加，浅色主题下几乎不可见。
                   改为按主题取反色叠加，保证两种主题下都有明确反馈。 */
                .code-btn:hover {{
                    background: {"rgba(15, 23, 42, 0.08)" if _is_light_diff else "rgba(255,255,255,0.08)"} !important;
                }}

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
                    /* 右 4px = 10px - 6px 滚动轨道：轨道常驻后内容宽度稳定，右侧视觉边距与左对称 */
                    padding: 8px 4px 8px 10px;
                    border-top: 1px solid var(--border);
                    background: transparent;
                    color: var(--text-secondary) !important;
                    font-style: italic;
                    font-size: {code_font_size + 2}px;
                    font-family: '{font_family}', sans-serif;
                    line-height: 1.6;
                    max-height: 500px;
                    overflow-y: scroll;
                    /* 🐛 修复（偶发横向滚动条）：CSS 规范规定一轴非 visible 时另一轴 visible
                       会被自动计算为 auto，未显式声明会导致内部 .code-container / .table-scroll-wrapper
                       等 overflow-x:auto 的子容器在内容超宽时撑出整个折叠框的横向滚动条 */
                    overflow-x: hidden;
                    transition: opacity 200ms ease;
                }}
                /* 思考内容加载骨架屏动画
                   原为硬编码白色渐变：浅色主题下白叠白，骨架屏完全看不见
                   （用户会误以为内容没加载）。改为按主题选择反色叠加。 */
                .think-content.loading {{
                    background-image: linear-gradient(
                        90deg,
                        {"rgba(15, 23, 42, 0.03)" if _is_light_diff else "rgba(255, 255, 255, 0.02)"} 25%,
                        {"rgba(15, 23, 42, 0.07)" if _is_light_diff else "rgba(255, 255, 255, 0.05)"} 50%,
                        {"rgba(15, 23, 42, 0.03)" if _is_light_diff else "rgba(255, 255, 255, 0.02)"} 75%
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
                /* 流式思考中的标题底色：原为硬编码白色叠加，浅色主题下不可见
                   （用户感知不到"思考中"的状态反馈）。改为主题感知反色叠加。 */
                .think-block[data-streaming="true"] .think-block__summary {{
                    background: {"rgba(15, 23, 42, 0.05)" if _is_light_diff else "rgba(255, 255, 255, 0.04)"};
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
                    /* 右 6px = 12px - 6px 滚动轨道：同 .think-content，右侧视觉边距与左对称 */
                    padding: 6px 6px 10px 12px;
                    color: var(--text);
                    font-size: {tag_font_size}px;
                    line-height: 1.5;
                    word-break: break-word;
                    font-family: {mono_font};
                    max-height: 400px;
                    overflow-y: scroll;
                    /* 🐛 修复（偶发横向滚动条）：显式 hidden 避免一轴非 visible
                       导致另一轴 visible 被自动计算为 auto 而撑出横向滚动条 */
                    overflow-x: hidden;
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

                /* ===== ECharts 图表容器 ===== */
                .echarts-container {{
                    position: relative;
                    width: 100%;
                    min-height: 300px;
                    height: auto;
                    margin: 12px 0;
                    border-radius: 10px;
                    background: {"rgba(255, 255, 255, 0.75)" if _is_light_diff else "rgba(22, 27, 34, 0.6)"};
                    border: 1px solid var(--code-border, rgba(58, 63, 71, 0.6));
                }}
                /* ===== 图表 hover 浮动工具栏（放大 / 导出）；按钮底色与 icon 目录由 _CHART_IS_DARK 同源驱动 ===== */
                .chart-toolbar {{
                    position: absolute;
                    top: 8px;
                    right: 24px;
                    display: flex;
                    gap: 6px;
                    opacity: 0;
                    transition: opacity 150ms ease;
                    z-index: 10;
                }}
                .echarts-container:hover .chart-toolbar,
                .mermaid-block:hover .chart-toolbar {{
                    opacity: 1;
                }}
                .chart-toolbar button {{
                    position: relative;
                    width: 28px;
                    height: 28px;
                    border-radius: 6px;
                    border: 1px solid var(--code-border, rgba(58, 63, 71, 0.6));
                    background: {"rgba(255, 255, 255, 0.92)" if _is_light_diff else "rgba(22, 27, 34, 0.85)"};
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 0;
                }}
                .chart-toolbar button:hover {{
                    background: {"rgba(228, 233, 240, 1)" if _is_light_diff else "rgba(40, 46, 56, 0.95)"};
                }}
                /* 自绘 tooltip（代替 HTML title，避免 Chromium 原生 tooltip 黑块）；向下弹避免被容器 overflow:hidden 裁剪 */
                .chart-toolbar button::after {{
                    content: attr(data-tooltip);
                    position: absolute;
                    top: calc(100% + 6px);
                    right: 0;
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
                .chart-toolbar button:hover::after {{
                    opacity: 1;
                    z-index: 100;
                }}
                .chart-toolbar button img {{
                    width: 16px;
                    height: 16px;
                    pointer-events: none;
                }}

                /* ===== Mermaid 图表容器 ===== */
                .mermaid-block {{
                    position: relative;
                    width: 100%;
                    margin: 12px 0;
                    padding: 12px 10px;
                    border-radius: 10px;
                    background: transparent;
                    border: 1px solid var(--code-border, rgba(58, 63, 71, 0.6));
                    overflow-x: auto;
                    text-align: center;
                }}
                /* mermaid 输出的 svg 自带固定 width，需放开以便窄卡片内自适应 */
                .mermaid-block svg {{
                    max-width: 100%;
                    height: auto;
                }}
                .mermaid-block.mermaid-pending {{
                    min-height: 42px;
                    color: var(--text-muted, #8b949e);
                    font-size: 12px;
                }}
                /* 渲染失败：退回源码，保证内容不丢 */
                .mermaid-error {{
                    text-align: left;
                    margin: 0;
                    padding: 10px 12px;
                    white-space: pre-wrap;
                    word-break: break-word;
                    font-size: 12px;
                    color: var(--text-secondary, #c9d1d9);
                }}
                '''

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
                    font-size: 13px;
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
                    /* 轨道常驻：出现/消失切换不再使内容宽度 ±6px 波动；
                       右 padding 扣减 6px，右侧视觉边距与左基本对称 */
                    overflow-y: scroll;
                    /* 🐛 修复（偶发横向滚动条）：CSS 规范规定一轴非 visible 时另一轴 visible
                       会被自动计算为 auto，未显式声明会让内部 .tool-diff-inline__body /
                       .code-container / .table-scroll-wrapper 等 overflow-x:auto 的子容器
                       在内容超宽时撑出整个"工具与思考"区的横向滚动条 */
                    overflow-x: hidden;
                    overflow-anchor: none;  /* 禁用 scroll anchoring，防止浏览器在 reorganizeContent 后调整 scrollTop 覆盖 JS 设置的滚底位置 */
                    background: transparent;
                    border: none;
                    border-radius: 6px;
                    padding: 2px 0 2px 4px;
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

                /* ── 任务列表（工具区最底部）── */
                #todo-panel {{
                    margin: 2px 2px 0 2px;
                }}
                /* 工具区折叠时 todo 面板一起收起 */
                #tool-section[data-collapsed="true"] #todo-panel {{
                    display: none;
                }}
                /* 任务列表分隔线（与 #tool-separator 同源样式）：标题+完成统计嵌在分隔线中间，任务项在其下 */
                #todo-separator {{
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    font-size: 13px;
                    color: var(--text-muted);
                    user-select: none;
                    padding: 2px 2px 6px 2px;
                }}
                #todo-separator::before,
                #todo-separator::after {{
                    content: '';
                    flex: 1;
                    height: 1px;
                    background: var(--border);
                    opacity: 0.6;
                }}
                #todo-separator #todo-progress {{
                    font-size: 12px;
                    color: var(--text-muted);
                    white-space: nowrap;
                }}
                .todo-panel-header {{
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    font-size: 12px;
                    font-weight: 600;
                    color: var(--text-muted);
                    user-select: none;
                    padding: 2px 4px 3px 4px;
                }}
                /* 列表限高与 #tool-content 同尺度（600px），超出滚动 */
                #todo-content {{
                    position: relative;  /* 子项 offsetTop 相对本容器计算（in_progress 定位滚动依赖） */
                    max-height: 600px;
                    overflow-y: scroll;  /* 轨道常驻 + 右 padding 扣减：同 #tool-content */
                    /* 🐛 修复（偶发横向滚动条）：同上 #tool-content，显式 hidden 阻止
                       overflow-x 自动计算为 auto，避免长 todo 文本撑出横向滚动条 */
                    overflow-x: hidden;
                    overflow-anchor: none;
                    background: transparent;
                    border-radius: 6px;
                    padding: 2px 0 2px 4px;
                }}
                .todo-item {{
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    padding: 4px 6px;
                    font-size: {scale_font_size(13)}px;
                    line-height: 1.5;
                    color: var(--text);
                }}
                .todo-item + .todo-item {{
                    margin-top: 1px;
                }}
                /* 进行中：左侧蛇形转圈（.think-snake 由 _animateThinkSnake 统一驱动） */
                .todo-item .todo-spin {{
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    flex: 0 0 auto;
                }}
                .todo-item .todo-spin svg {{
                    display: block;
                }}
                .todo-item[data-status="in_progress"] .todo-text {{
                    color: var(--accent-warm);
                    font-weight: 600;
                }}
                /* 完成：✓ + 划掉 */
                .todo-item .todo-done-icon {{
                    flex: 0 0 auto;
                    color: rgba(63, 185, 80, 0.95);
                    font-weight: 700;
                }}
                .todo-item[data-status="completed"] .todo-text {{
                    color: var(--text-muted);
                    text-decoration: line-through;
                }}
                /* 待办：○ */
                .todo-item .todo-pending-icon {{
                    flex: 0 0 auto;
                    color: var(--text-muted);
                }}
                /* 优先级染色：仅影响待办 ○ 圆点（in_progress 是 SVG 动画、completed 是 ✓ 已带语义色） */
                .todo-item[data-priority="high"] .todo-pending-icon {{
                    color: #ef4444;
                }}
                .todo-item[data-priority="medium"] .todo-pending-icon {{
                    color: #f59e0b;
                }}
                .todo-item[data-priority="low"] .todo-pending-icon {{
                    color: #3b82f6;
                }}
                .todo-item .todo-text {{
                    flex: 1 1 auto;
                    min-width: 0;
                    overflow-wrap: break-word;
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
              <div id="todo-panel" style="display: none;">
                <div id="todo-separator"><span>📋 任务列表</span><span id="todo-progress"></span></div>
                <div id="todo-content"></div>
              </div>
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

                // ===== tool-section 折叠/展开过渡的高度报告抑制 =====
                // #tool-content 有 max-height 200ms CSS 过渡，过渡期间 ResizeObserver
                // 会上报中间态高度 → viewer setFixedHeight 连跳多次（流式抖动主因之二）。
                // 统一模式：切换属性前先抑制报告 → transitionend（260ms 兜底）终值单报。
                // _finishToolSectionTransition 带 guard：先到者生效，后到者 no-op
                // （同时修复旧 _toggleToolSection transitionend+setTimeout 双报告）。
                var _tsTransitionDone = false;
                var _tsTransitionToken = 0;
                function _finishToolSectionTransition() {{
                    if (_tsTransitionDone) return;
                    _tsTransitionDone = true;
                    reportHeight();          // 终值直报（不经 debounced，且此时抑制已可释放）
                    _collapsibleHeightReporting = false;
                }}
                function _beginToolSectionTransition() {{
                    _collapsibleHeightReporting = true;   // 抑制 ResizeObserver + reportHeightDebounced
                    _tsTransitionDone = false;
                    var token = ++_tsTransitionToken;     // 轮次令牌：连续切换时旧 timer 失效
                    var _tcEl = document.getElementById('tool-content');
                    var _onTsEnd = function(ev) {{
                        // 只认 max-height 过渡结束（同元素 opacity 过渡会额外触发一次）
                        if (ev && ev.propertyName && ev.propertyName !== 'max-height') return;
                        if (token !== _tsTransitionToken) return;
                        if (_tcEl) _tcEl.removeEventListener('transitionend', _onTsEnd);
                        _finishToolSectionTransition();
                    }};
                    if (_tcEl) _tcEl.addEventListener('transitionend', _onTsEnd);
                    // 兜底：display:none 等场景 transitionend 不触发
                    setTimeout(function() {{
                        if (token !== _tsTransitionToken) return;
                        if (_tcEl) _tcEl.removeEventListener('transitionend', _onTsEnd);
                        _finishToolSectionTransition();
                    }}, 260);
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

                // ===== Mermaid 渲染（Chromium 83 兼容：polyfill + mermaid 10.9.1 懒加载） =====
                // Qt 5.15.2 的 WebEngine 是 Chromium 83，缺 structuredClone / Object.hasOwn /
                // replaceAll / Array.prototype.at；mermaid 10 在模块顶层就会用到，缺一个即
                // 整体 undefined。故必须先加载 polyfill，再加载 mermaid。见 docs/mermaid-chromium83.md。
                var _MMD_POLYFILL = '{_mmd_polyfill_url}';
                var _MMD_LIB = '{_mmd_lib_url}';

                function _mmdLoadScript(src, onOk) {{
                    if (!src) {{ onOk(); return; }}
                    var s = document.createElement('script');
                    s.src = src;
                    s.onload = onOk;
                    s.onerror = function () {{ console.error('[mermaid] load failed: ' + src); }};
                    document.head.appendChild(s);
                }}

                function _mmdEnsure(cb) {{
                    if (window.mermaid && window.mermaid.render) {{ cb(); return; }}
                    if (!window._mmdQueue) window._mmdQueue = [];
                    window._mmdQueue.push(cb);
                    if (window._mmdLoading) return;          // 已在加载中，排队即可
                    window._mmdLoading = true;
                    _mmdLoadScript(_MMD_POLYFILL, function () {{
                        _mmdLoadScript(_MMD_LIB, function () {{
                            try {{
                                window.mermaid.initialize({{
                                    startOnLoad: false,
                                    // 内容来自 LLM，不可信：strict 会 sanitize 标签、禁用交互
                                    securityLevel: 'strict',
                                    theme: 'base',
                                    themeVariables: {{
                                        primaryTextColor: '{mmd_text_color}',
                                        lineColor: '{mmd_line_color}',
                                        mainBkg: '{mmd_node_bg}',
                                        nodeBorder: '{mmd_border}',
                                        background: 'transparent',
                                        fontSize: '{body_font_size}px'
                                    }}
                                }});
                            }} catch (e) {{
                                console.error('[mermaid] initialize failed:', e);
                            }}
                            window._mmdLoading = false;
                            var q = window._mmdQueue || [];
                            window._mmdQueue = [];
                            for (var qi = 0; qi < q.length; qi++) {{ q[qi](); }}
                        }});
                    }});
                }}

                function renderMermaidBlocks() {{
                    var blocks = document.querySelectorAll('.mermaid-block[data-mermaid-src]');
                    if (!blocks.length) return;
                    _mmdEnsure(function () {{
                        if (!window.mermaid || !window.mermaid.render) return;
                        for (var i = 0; i < blocks.length; i++) {{
                            (function (el) {{
                                if (el._mmdDone) return;
                                el._mmdDone = true;             // 流式追加不重复渲染
                                var decoded;
                                try {{
                                    // atob 按 ISO-8859-1 解码，直接用于 UTF-8 中文会 mojibake
                                    var bytes = Uint8Array.from(atob(el.getAttribute('data-mermaid-src')),
                                        function (c) {{ return c.charCodeAt(0); }});
                                    decoded = new TextDecoder('utf-8').decode(bytes);
                                }} catch (e) {{
                                    decoded = '';
                                }}
                                if (!decoded) return;
                                var rid = (el.id || 'mmd') + '-svg';
                                window.mermaid.render(rid, decoded).then(function (r) {{
                                    var svg = r && r.svg ? r.svg : String(r);
                                    if (svg.indexOf('<svg') < 0) throw new Error('no svg in result');
                                    el.classList.remove('mermaid-pending');
                                    el.innerHTML = svg;
                                    el.setAttribute('data-mermaid-src', '');   // 渲染完释放 b64
                                    if (window._attachChartToolbar) window._attachChartToolbar(el, 'mermaid');
                                    if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                                }})['catch'](function (e) {{
                                    // 失败不吞内容：退回原始源码，用户仍可复制
                                    el.classList.remove('mermaid-pending');
                                    el.innerHTML = '<pre class="mermaid-error"></pre>';
                                    el.firstChild.textContent = decoded;
                                    if (typeof reportHeightDebounced === 'function') reportHeightDebounced();
                                }});
                            }})(blocks[i]);
                        }}
                    }});
                }}

                function updateContent(newHtml) {{
                    const container = document.getElementById('content-placeholder');
                    if (container.innerHTML !== newHtml) {{
                        // 记录当前展开状态的思考块
                        // [PERF] 简洁模式：completed 思考块是 think-compact（无折叠），跳过 save
                        //       节省 querySelectorAll + Map 构造；非简洁模式行为不变
                        var expandedStates = null;
                        if (!window._toolCompactMode) {{
                            expandedStates = new Map();
                            container.querySelectorAll('.think-block').forEach(function(block) {{
                                expandedStates.set(block.dataset.blockKey, block.dataset.expanded === 'true');
                            }});
                        }}

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
                        // 🐛 修复（区域独立 II）：同步保存**正文容器**的 scrollTop——
                        // innerHTML 全量重写会把它重置为 0，而下方只恢复了 body 的。
                        // 思考/工具更新同样触发全量渲染，若不恢复，正文阅读位置
                        // 被抹成 0（上滚态卡顶）/被末尾置底拉到固定底部。
                        var _cpEl = document.getElementById('content-placeholder');
                        var _cpPrevTop = _cpEl ? _cpEl.scrollTop : 0;
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
                            // 🆕 方案 C（#33）：innerHTML 替换异常时**不再 throw**——
                            // 原实现 re-throw 会导致调用方 JS 中断（后续渲染逻辑不执行），
                            // 消息卡片呈现空白（P0 回归根因候选之一）。
                            // 回退为 textContent 纯文本兜底：保证正文永远显示（即使 JS
                            // 异常也不空白），同时恢复透明度避免半透明残影。
                            console.error('updateContent innerHTML failed, fallback to textContent:', e);
                            container.style.opacity = '1';
                            container.style.transition = '';
                            try {{
                                container.textContent = newHtml;
                            }} catch(e2) {{
                                console.error('updateContent textContent fallback also failed:', e2);
                            }}
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
                        // [PERF] 简洁模式：expandedStates 为 null，跳过整段（think-compact 无折叠）
                        if (expandedStates) {{
                            container.querySelectorAll('.think-block').forEach(function(block) {{
                                var savedState = expandedStates.get(block.dataset.blockKey);
                                if (savedState !== undefined) {{
                                    block.dataset.expanded = savedState ? 'true' : 'false';
                                    var body = block.querySelector('.cm-collapsible__body');
                                    if (body) {{
                                        body.style.height = savedState ? 'auto' : '0px';
                                        body.style.opacity = savedState ? '1' : '0';
                                    }}
                                }}
                            }});
                        }}

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
                                    var chart = echarts.init(el, _CHART_IS_DARK ? 'dark' : undefined);
                                    chart.setOption(option);
                                    el._echartInited = true;
                                    el._chartInstance = chart;
                                    if (window._attachChartToolbar) window._attachChartToolbar(el, 'echarts');
                                    // 卡片 resize 时自适应
                                    var _ro = new ResizeObserver(function() {{ chart.resize(); }});
                                    _ro.observe(el);
                                }} catch(e) {{
                                    console.error('ECharts init error:', e);
                                }}
                            }});
                        }}

                        // 渲染 Mermaid 图表（内部按需懒加载 polyfill + mermaid）
                        if (typeof renderMermaidBlocks === 'function') renderMermaidBlocks();

                        // 将工具/思考块分流到独立滚动容器（仅简洁模式）
                        // 必须在 _suppressScrollEvent=false 之前执行，
                        // 否则移动 DOM 触发的 scroll 事件会错误标记 _userScrolledWithin=true
                        if (window._toolCompactMode) reorganizeContent();

                        // 🐛 修复（区域独立 II）：所有影响正文高度的 DOM 操作（reorganize
                        // 搬移/折叠恢复/ECharts）完成后，恢复重建前的阅读位置。
                        // 钐到新 max（内容变短时不越界）；值实际变化才打 _progScroll
                        // （防 scroll 事件在 _suppressScrollEvent=false 后异步到达被
                        // 误判为用户滚动）；值未变不打标记（避免残留吞掉下次真实滚动）。
                        // 跟随态（_userScrolledUp=false）时下方 _autoScrollStreamingBody
                        // 置底会覆盖此值（正文有新内容需跟随）；上滚态则保持原位。
                        var _cpEl2 = document.getElementById('content-placeholder');
                        if (_cpEl2 && _cpPrevTop > 0) {{
                            var _cpMax = Math.max(0, _cpEl2.scrollHeight - _cpEl2.clientHeight);
                            var _cpTarget = Math.min(_cpPrevTop, _cpMax);
                            if (_cpEl2.scrollTop !== _cpTarget) {{
                                _cpEl2._progScroll = true;
                                _cpEl2.scrollTop = _cpTarget;
                            }}
                        }}

                        // 🐛 修复：auto-scroll 延后到所有 DOM 操作（table 包裹、折叠框状态恢复、
                        // think-block 展开、ECharts 初始化、reorganizeContent）之后执行，
                        // 确保 scrollHeight 值反映最终渲染结果，避免因 collapsible 展开 /
                        // tool-block restore 等操作在 auto-scroll 后增加高度而导致的
                        // "滚不到底部"问题。
                        // 附加修复：打 auto-scroll 时间戳，让 scroll 事件回调识别
                        // 程序触发的滚动事件（解决 suppress=false 之后异步派发 scroll 的 race）。
                        // 此时 _suppressScrollEvent 仍为 true，所有 scroll 事件仍被抑制。
                        if (!_wasUserScrolled) {{
                            _autoScrollStreamingBody();
                            window._userScrolledWithin = false;
                        }} else {{
                            var _wasAtBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < _scrollThreshold;
                            if (_wasAtBottom) {{
                                _autoScrollStreamingBody();
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
                // ===== B1 差量渲染：追加闭合段到 DOM（不整块替换） =====
                // 移除所有 data-incremental="true" 的增量纯文本节点，
                // 再把新闭合的格式化 HTML 追加到 #content-placeholder 末尾。
                // 🐛 修复（正文尾部丢失）：tailHtml 参数——移除增量节点时
                // 会连带删除**未闭合的尾部文本**（尚未到 \\n\\n 的段落后半段），
                // 且新闭合段 HTML 不含它 → 尾部永久消失（用户可见"正文显示不全"）。
                // 因此 Python 端把未闭合尾部的**行内渲染 HTML**传进来，
                // 移除后重建增量节点保尾（innerHTML 注入，流式期间 markdown
                // 语法即时格式化，不再字面显示源码）。
                function updateContentAppend(newHtml, tailHtml) {{
                    const container = document.getElementById('content-placeholder');
                    if (!container) return;
                    // 移除增量纯文本节点（差量渲染会以格式化 HTML 替代它们）
                    container.querySelectorAll('[data-incremental="true"]').forEach(function(el) {{
                        el.remove();
                    }});
                    // 段落分隔已由本次渲染的 HTML 表达，清掉挂起分段标记
                    container.removeAttribute('data-pending-break');
                    // 追加格式化 HTML（含 table 包裹等后续处理）
                    container.insertAdjacentHTML('beforeend', newHtml);
                    // 🐛 修复（正文尾部丢失）：未闭合尾部重建为增量渲染节点。
                    // ⚠️ 用 <div> 而非 <p> 包裹：tailHtml 是 md.convert 产物
                    // （含 <p>/<h1>/<ul>/<pre> 等块级元素），<p> 内嵌块级会触发
                    // HTML 解析器自动闭合/提升，导致 DOM 结构错乱。
                    if (tailHtml) {{
                        var tailDiv = document.createElement('div');
                        tailDiv.setAttribute('data-incremental', 'true');
                        // [B1] data-rendered 标记：_append_text_incremental 检测到
                        // 该标记时不再 textContent 原地追加（会抹掉已渲染的 HTML），
                        // 改为新建纯文本增量节点。
                        tailDiv.setAttribute('data-rendered', 'true');
                        tailDiv.innerHTML = tailHtml;
                        container.appendChild(tailDiv);
                    }}
                    // 🐛 修复（思考块滞留正文）：与全量 updateContent 对齐——简洁模式下
                    // 差量追加的思考/工具块立即搬移到"工具与思考"区，否则滞留
                    // #content-placeholder，视觉上"思考内容在正文闪现，随后消失回折叠区"。
                    if (window._toolCompactMode) reorganizeContent();
                    // 包裹所有 <table>（不含 .code-table）到可横向滚动的容器中
                    container.querySelectorAll('table:not(.code-table)').forEach(function(table) {{
                        if (table.parentNode && table.parentNode.classList.contains('table-scroll-wrapper')) return;
                        var wrapper = document.createElement('div');
                        wrapper.className = 'table-scroll-wrapper';
                        table.parentNode.insertBefore(wrapper, table);
                        wrapper.appendChild(table);
                    }});
                    // 恢复展开状态
                    restoreCollapsibleStates(container);
                    // 同步滚动到底（流式期间通常期望跟到底部）
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody();
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody();
                            window._userScrolledWithin = false;
                        }}
                    }}
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    // 初始化 ECharts 图表（追加的闭合段可能含 echarts 代码块）
                    if (window.echarts) {{
                        container.querySelectorAll('.echarts-container').forEach(function(el) {{
                            try {{
                                var jsonB64 = el.getAttribute('data-echarts-json');
                                if (!jsonB64 || el._echartInited) return;
                                var _bytes = Uint8Array.from(atob(jsonB64), function(c) {{ return c.charCodeAt(0); }});
                                var option = JSON.parse(new TextDecoder('utf-8').decode(_bytes));
                                var chart = echarts.init(el, 'dark');
                                chart.setOption(option);
                                el._echartInited = true;
                                el._chartInstance = chart;
                                if (window._attachChartToolbar) window._attachChartToolbar(el, 'echarts');
                                var _ro = new ResizeObserver(function() {{ chart.resize(); }});
                                _ro.observe(el);
                            }} catch(e) {{
                                console.error('ECharts init error:', e);
                            }}
                        }});
                    }}
                    // 渲染 Mermaid 图表（追加的闭合段可能含 ```mermaid 代码块）
                    if (typeof renderMermaidBlocks === 'function') renderMermaidBlocks();
                    // 使用延迟报告，确保浏览器布局完成
                    setTimeout(() => reportHeight(), 30);
                }}
                // ===== B1 差量渲染：未闭合尾部行内渲染（整体替换增量节点） =====
                // 无空行分隔的长段落（`\\n\\n` 缺失）没有闭合段可差量渲染，
                // 尾部长时间以纯文本显示 markdown 源码（**加粗**、`code`、[链接]）。
                // Python 端把尾部整体 convert 成行内 HTML 传入，替换所有增量节点
                // （纯文本 + 已渲染），DOM 尾部始终是**一个** data-rendered 渲染节点，
                // 后续 _append_text_incremental 在其后追加纯文本，差量/全量渲染再整体替换。
                function updateTailHtml(html) {{
                    const container = document.getElementById('content-placeholder');
                    if (!container || !html) return;
                    container.querySelectorAll('[data-incremental="true"]').forEach(function(el) {{
                        el.remove();
                    }});
                    // 段落分隔已由本次尾部 HTML 表达，清掉挂起分段标记
                    container.removeAttribute('data-pending-break');
                    // ⚠️ 用 <div> 而非 <p> 包裹：html 是 md.convert 产物（块级元素），
                    // <p> 内嵌块级会触发解析器自动闭合，结构错乱。
                    var tailDiv = document.createElement('div');
                    tailDiv.setAttribute('data-incremental', 'true');
                    tailDiv.setAttribute('data-rendered', 'true');
                    tailDiv.innerHTML = html;
                    container.appendChild(tailDiv);
                    // 与 updateContentAppend 对齐：表格包裹 + 折叠状态恢复 + 滚动
                    container.querySelectorAll('table:not(.code-table)').forEach(function(table) {{
                        if (table.parentNode && table.parentNode.classList.contains('table-scroll-wrapper')) return;
                        var wrapper = document.createElement('div');
                        wrapper.className = 'table-scroll-wrapper';
                        table.parentNode.insertBefore(wrapper, table);
                        wrapper.appendChild(table);
                    }});
                    restoreCollapsibleStates(container);
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody();
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody();
                            window._userScrolledWithin = false;
                        }}
                    }}
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    setTimeout(() => reportHeight(), 30);
                }}
                {_CONTENT_AUTOSCROLL_JS}
                function reportHeight() {{
                    // 用 body.scrollHeight 获取完整内容高度。
                    // getBoundingClientRect 在 html{{overflow:hidden}} 下
                    // 返回视口高度而非内容高度，导致卡片无法完全展开。
                    const _b = document.body;
                    if (!_b) return;
                    const h = _b.scrollHeight;
                    // 🐛 滚动判据修复：body 才是真正的滚动容器
                    // （CSS body{{overflow-y:scroll; max-height}}），而 Qt 侧的
                    // page().scrollPosition() 是文档级、恒为 0，无法用于边界判定。
                    // 故在高频回传中顺带携带 body 的 scrollTop / clientHeight，
                    // Python 侧据此算出真实可滚动量 = scrollHeight - clientHeight。
                    // 注意保持'|'分隔协议，旧解析器（仅高度）仍可工作。
                    console.log('pywebview_height:' + h + '|' + (_b.scrollTop|0) + '|' + (_b.clientHeight|0));
                }}
                // 批量报告高度：流式每 chunk 一次 IPC 开销高，改为 3 帧合并
                // （rAF ×3 后 reportHeight 一次），动画期间仍暂停报告
                let _heightReportPending = false;
                let _heightReportFrames = 0;
                function reportHeightDebounced() {{
                    if (_collapsibleHeightReporting) return;  // 动画期间暂停
                    if (_heightReportPending) return;
                    _heightReportPending = true;
                    _heightReportFrames = 0;
                    requestAnimationFrame(function _batchTick() {{
                        _heightReportFrames++;
                        if (_heightReportFrames < 3) {{
                            requestAnimationFrame(_batchTick);
                            return;
                        }}
                        reportHeight();
                        _heightReportPending = false;
                    }});
                }}

                // ===== 正文/非正文分区：将工具块/思考块从内容区移到独立可滚动容器 =====
                // 编辑类工具（write/edit/multi_edit）保留在正文中，不迁移到"工具与思考"区域
                // 子智能体/提问类工具（subagent_para/subagent_dag/question）与编辑工具类似，
                // 属于 AI 与用户之间的直接交互结果，保留在正文中体验更连贯。
                // 工具名集合由 Python 渲染端派生（registry 声明），经 data-keep-in-content 属性传入，
                // JS 不再硬编码工具名。
                var _EDIT_TOOLS_SELECTOR = ':not([data-keep-in-content="true"])';

                // 更新"工具与思考"标题（总项数）
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
                        // 过渡期间抑制中间态高度报告，结束后终值单报
                        _beginToolSectionTransition();
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
                        // 容器没有需要迁移的块 —— 若 tool-content 空且无 todo 就隐藏整个区
                        if (toolContent.children.length === 0 && !window._todoCount) {{
                            toolSection.style.display = 'none';
                            return;
                        }}
                        // tool-content 仍有 data-tool-injected 流式块 / 旧搬移块
                        // （markdown 被缩短、块被删除），仍需刷新 header
                        toolSection.style.display = '';
                        _updateToolSectionHeader();
                        // 坞态（流式中）：自动滚底显示最新活动（尊重用户上滚）
                        if (window._streamingActive && window._toolCompactMode) _scrollToolContentToBottom();
                        return;
                    }}
                    // ── [PERF v2] 单次扫描 blocks：posMap + thinkKeys + toolIds + thinkStreaming ──
                    // 原实现有 4 个独立 forEach 重复遍历，v2 合并为单次，O(n²) → O(n)
                    var posMap = Object.create(null);
                    var _currentThinkKeys = new Set();
                    var _currentToolIds = new Set();
                    var _hasNewThinkStreaming = false;
                    var _thinkStreamingEl = null;
                    for (var _bi = 0; _bi < blocks.length; _bi++) {{
                        var _el = blocks[_bi];
                        var _bk = _el.getAttribute('data-block-key');
                        var _tid = _el.getAttribute('data-tool-call-id');
                        if (_bk) posMap['bk:' + _bk] = _bi;
                        if (_tid) {{
                            posMap['tcid:' + _tid] = _bi;
                            _currentToolIds.add(_tid);
                        }}
                        if (_bk && (
                            _el.classList.contains('think-block')
                            || _el.classList.contains('think-streaming')
                            || _el.classList.contains('think-compact')
                        )) {{
                            _currentThinkKeys.add(_bk);
                        }}
                        if (_el.classList.contains('think-streaming')) {{
                            _hasNewThinkStreaming = true;
                            _thinkStreamingEl = _el;
                        }} else if (!_bk && !_tid) {{
                            // 无稳定标识的块（备用扩展）—— 用 blocks 中的序号
                            _el._posIdx = _bi;
                        }}
                    }}
                    // ── [PERF v2] 单次遍历 toolContent 子节点：think-streaming + 过期清理 ──
                    var _oldThinkStreaming = null;
                    var _toolKids = toolContent.children;
                    for (var _ti = 0; _ti < _toolKids.length; _ti++) {{
                        var _tk = _toolKids[_ti];
                        if (_tk.classList.contains('think-streaming') && !_oldThinkStreaming) {{
                            _oldThinkStreaming = _tk;
                        }}
                    }}
                    if (!_hasNewThinkStreaming && _oldThinkStreaming) {{
                        _oldThinkStreaming.remove();
                        _oldThinkStreaming = null;
                    }}
                    // 清理过期 think-block / think-compact + 过期 tool-block
                    var _existingKids = Array.prototype.slice.call(toolContent.children);
                    for (var _ei = 0; _ei < _existingKids.length; _ei++) {{
                        var _eel = _existingKids[_ei];
                        if (!_eel || !_eel.parentNode) continue;
                        var _ebk = _eel.getAttribute('data-block-key');
                        var _etid = _eel.getAttribute('data-tool-call-id');
                        // 过期 think 块
                        if (
                            _ebk
                            && !_currentThinkKeys.has(_ebk)
                            && !_etid
                            && (
                                _eel.classList.contains('think-block')
                                || _eel.classList.contains('think-compact')
                            )
                        ) {{
                            _eel.remove();
                            continue;
                        }}
                        // 过期 tool 块（保留流式进行中的块）
                        if (
                            _etid
                            && !_currentToolIds.has(_etid)
                            && _eel.getAttribute('data-streaming') !== 'true'
                            && _eel.classList.contains('tool-block')
                        ) {{
                            _eel.remove();
                            continue;
                        }}
                    }}
                    // 从正文移除已存在稳定标识的重叠块，其余搬移到工具区
                    // 🐛 修复吞内容 + 闪烁：think-streaming 用 replaceChild 原地替换
                    // （保 DOM 位置，更新内容）
                    var moved = false;
                    for (var _mi = 0; _mi < blocks.length; _mi++) {{
                        var _mel = blocks[_mi];
                        var _mbk = _mel.getAttribute('data-block-key');
                        var _mtid = _mel.getAttribute('data-tool-call-id');
                        var _dup = (_mbk && toolContent.querySelector('[data-block-key="' + _mbk + '"]'))
                                || (_mtid && toolContent.querySelector('[data-tool-call-id="' + _mtid + '"]'));
                        if (_dup) {{
                            if (_mel.parentNode === container) _mel.remove();
                        }} else if (_mel.classList.contains('think-streaming') && _oldThinkStreaming) {{
                            if (_mel.parentNode === container && _oldThinkStreaming.parentNode) {{
                                _oldThinkStreaming.parentNode.replaceChild(_mel, _oldThinkStreaming);
                            }}
                        }} else if (_mel.parentNode === container) {{
                            toolContent.appendChild(_mel);
                            moved = true;
                        }}
                    }}
                    // ▓▓ Bug B 方案 D+：把 markdown 渲染块的 data-order 补齐，统一排序/插入的尺度 ▓▓
                    // 根因：flow 结束时 save/restore 会按 data-order 把"仍在流式"的工具块插回，
                    // 但其插入循环只比较带 data-order 的子节点；而 think 块/完成工具块由 markdown
                    // 渲染，只有 posMap（btn-key/tool-call-id）没有 data-order → 插入循环找不到
                    // 目标 → appendChild 沉底 → 折叠框内"所有思考在前、所有工具在后"（容器 posMap
                    // 未含仍在流式、尚未进入 _content_data 的工具块，需用其 floor(data-order) 修正）。
                    // 此处为缺 data-order 的块补上 = posMap 位置 + 排在其前的流式工具数，使 sort
                    // 与 save/restore 插入共用同一把尺子（与 _count_think_tool_prefix 同尺度）。
                    // 🆕 方案 E：_streamFloors 初始化为 save 阶段暂存的流式块 data-order
                    // （window.__pendingStreamFloors）——save 会把所有 data-tool-call-id 块
                    // （含仍在流式的工具块）从 DOM 移除，导致下方从 toolContent.children 收集
                    // 时恒为空、修正失效；暂存数组补回这条信息，使"排在其前的流式工具数"
                    // 在坞态归位瞬间也能正确计入（否则思考块补齐的 data-order 偏小 → restore
                    // 插回的工具块找不到比它大的节点 → 全部 appendChild 沉底）。
                    var _streamFloors = (window.__pendingStreamFloors || []).slice();
                    var _allKids = Array.prototype.slice.call(toolContent.children);
                    for (var _sf = 0; _sf < _allKids.length; _sf++) {{
                        if (_allKids[_sf].getAttribute('data-streaming') === 'true') {{
                            var _sfOd = parseFloat(_allKids[_sf].getAttribute('data-order'));
                            if (!isNaN(_sfOd)) _streamFloors.push(Math.floor(_sfOd));
                        }}
                    }}
                    // 🆕 Bug B 方案 G：标记"是否补齐过 data-order"。save/restore 后
                    // tool-content 的键序列可能与上次相同（__lastOrder diff 误判"顺序未变"
                    // → 跳过 sort），但块物理顺序已被 restore/迁移打乱（data-order 与
                    // 物理顺序不一致）→ 折叠框内"思考在前、工具在后"。凡补齐过
                    // data-order（说明经历 markdown 重渲染 + save/restore），必须强制 sort。
                    var _assignedDataOrder = false;
                    for (var _oa = 0; _oa < _allKids.length; _oa++) {{
                        var _oaKid = _allKids[_oa];
                        if (_oaKid.getAttribute('data-order') !== null) continue;
                        var _oaBk = _oaKid.getAttribute('data-block-key');
                        var _oaTid = _oaKid.getAttribute('data-tool-call-id');
                        var _oaPos = (_oaBk && posMap['bk:' + _oaBk] !== undefined)
                            ? posMap['bk:' + _oaBk]
                            : (_oaTid && posMap['tcid:' + _oaTid] !== undefined)
                                ? posMap['tcid:' + _oaTid]
                                : null;
                        if (_oaPos === null) continue;
                        var _oaBefore = 0;
                        for (var _sf2 = 0; _sf2 < _streamFloors.length; _sf2++) {{
                            if (_streamFloors[_sf2] <= _oaPos) _oaBefore++;
                        }}
                        _oaKid.setAttribute('data-order', String(_oaPos + _oaBefore));
                        _assignedDataOrder = true;
                    }}
                    // ── [PERF v2] 顺序哈希 diff：键序列未变时跳过 sort + appendChild ──
                    // 流式期间大部分 updateContent 走"键序列未变"快路径，避免 sort 抖动
                    var _curKeys = [];
                    var _curKids = toolContent.children;
                    for (var _ci = 0; _ci < _curKids.length; _ci++) {{
                        var _ck = _curKids[_ci];
                        var _ckbk = _ck.getAttribute('data-block-key');
                        var _cktid = _ck.getAttribute('data-tool-call-id');
                        if (_ckbk) {{
                            _curKeys.push('bk:' + _ckbk);
                        }} else if (_cktid) {{
                            _curKeys.push('tcid:' + _cktid);
                        }} else {{
                            _curKeys.push('idx:' + _ci);
                        }}
                    }}
                    var _lastOrder = toolContent.__lastOrder;
                    // 🆕 Bug B 方案 G：补齐过 data-order（markdown 重渲染 + save/restore 路径）
                    // → 键序列 diff 不可靠（键相同但物理顺序已被 restore 打乱），强制 sort。
                    var _orderChanged = _assignedDataOrder || !_lastOrder || _lastOrder.length !== _curKeys.length;
                    if (!_orderChanged) {{
                        for (var _di = 0; _di < _curKeys.length; _di++) {{
                            if (_curKeys[_di] !== _lastOrder[_di]) {{
                                _orderChanged = true;
                                break;
                            }}
                        }}
                    }}
                    if (_orderChanged) {{
                        // 顺序变了：用 sort 一次性重排（避免多次 appendChild 抖动）
                        var _sortedChildren = Array.prototype.slice.call(toolContent.children).sort(function(a, b) {{
                            function getPos(el) {{
                                // 🆕 F1：运行中工具块（tool-streaming-block）强制沉底——
                                // 不被调用时刻快照 data-order 排到思考块上方（dock 语义：
                                // 最新活动最下）。be57674d 方案 D 引入 data-order 排序后，
                                // 运行中块 data-order 是工具调用时刻锚点前 think/tool 计数
                                // （固定快照），后续思考块补出更大 data-order → sort 把运行中
                                // 块排到思考块上方。此处对运行中工具块直接返回 1e9（沉底），
                                // 与 be57674d~1 回归前行为一致（无 data-order → 1e9 恒沉底）。
                                // ⚠️ 必须用 class 判定而非 data-streaming 属性——think-streaming
                                // （思考流式块）同样带 data-streaming="true"，但应保持在上方。
                                if (el.classList && el.classList.contains('tool-streaming-block')) {{
                                    return 1e9;
                                }}
                                // 🆕 方案 D：data-order 优先——JS 注入的工具块
                                // （save-restore 恢复块 / append_tool_result
                                // 完成块）不在 #content-placeholder 中，posMap 查不到，
                                // 无 data-order 会返回 1e9 恒沉底 → 折叠框内
                                // "所有思考在前、所有工具在后"（Bug B 第三条路径）。
                                // data-order 与 posMap 同尺度（锚点前 think/tool 块
                                // 计数 + 同锚点序号细分），可直接混合比较排序。
                                var od = el.getAttribute('data-order');
                                if (od !== null) {{
                                    return parseFloat(od);
                                }}
                                var bk = el.getAttribute('data-block-key');
                                var tid = el.getAttribute('data-tool-call-id');
                                if (bk && posMap['bk:' + bk] !== undefined) return posMap['bk:' + bk];
                                if (tid && posMap['tcid:' + tid] !== undefined) return posMap['tcid:' + tid];
                                if (el._posIdx !== undefined) return el._posIdx;
                                return 1e9;
                            }}
                            return getPos(a) - getPos(b);
                        }});
                        for (var _ri = 0; _ri < _sortedChildren.length; _ri++) {{
                            toolContent.appendChild(_sortedChildren[_ri]);
                        }}
                        toolContent.__lastOrder = _curKeys;
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
                    // 过渡期间抑制中间态高度报告，结束后终值单报（替代旧 transitionend+setTimeout 双报告）
                    _beginToolSectionTransition();
                    toolSection.setAttribute('data-collapsed', collapsed ? 'false' : 'true');
                    sep.setAttribute('aria-expanded', collapsed ? 'true' : 'false');
                    try {{ sessionStorage.setItem('_toolSectionCollapsed', collapsed ? '0' : '1'); }} catch(_err) {{}}
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
                    const verItem = e.target.closest('.changelog-version');
                    if (verItem) {{
                        e.stopPropagation();
                        e.preventDefault();
                        var vIdx = verItem.getAttribute('data-idx');
                        var vShell = verItem.closest('.changelog-shell');
                        if (vShell) {{
                            vShell.querySelectorAll('.changelog-version').forEach(function(el){{ el.classList.remove('active'); }});
                            vShell.querySelectorAll('.changelog-body').forEach(function(el){{ el.style.display = 'none'; }});
                            verItem.classList.add('active');
                            var vBody = vShell.querySelector('.changelog-body[data-idx="' + vIdx + '"]');
                            if (vBody) vBody.style.display = 'block';
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
                        // file:// 链接：交给 QWebEnginePage.acceptNavigationRequest 处理（系统打开）
                        if (link.href && link.href.startsWith('file://')) {{
                            return;  // 不拦截，触发默认 navigation
                        }}
                        console.log('pywebview_action:link_found:' + link.href);
                    }}
                    if (link && link.href && !link.href.startsWith('file://')) {{
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

                // ===== 图表工具栏：echarts / mermaid 放大查看 + 3x PNG 导出 =====
                var _CHART_IS_DARK = {str(not _is_light).lower()};
                var _CHART_BG = _CHART_IS_DARK ? '#1B1E24' : '#FFFFFF';
                // icon 目录与按钮底色同源（_CHART_IS_DARK），避免主题切换时 prefix 缓存滞后致白底白 icon
                var _ICON_BASE = _CHART_IS_DARK ? 'qrc:/icons' : 'qrc:/icons_light';
                function _b64EncodeUtf8(str) {{
                    return btoa(unescape(encodeURIComponent(str)));
                }}
                function _emitChartPng(dataUrl) {{
                    var b64 = (dataUrl || '').split(',', 2)[1] || '';
                    if (!b64 || b64.length > 8 * 1024 * 1024) {{ console.error('[chart] png too large or empty'); return; }}
                    console.log('pywebview_action:save_chart_png:' + _b64EncodeUtf8('chart') + ':' + b64);
                }}
                function _svgIntrinsicSize(svg) {{
                    // mermaid 输出 svg 带 width="100%"：parseFloat 会得 100 导致导出窄条，必须排除百分比、viewBox 优先
                    var vb = svg.viewBox && svg.viewBox.baseVal;
                    var num = function (v) {{ var n = parseFloat(v); return (n && String(v).indexOf('%') === -1) ? n : 0; }};
                    var w = (vb && vb.width) || num(svg.getAttribute('width')) || 800;
                    var h = (vb && vb.height) || num(svg.getAttribute('height')) || 600;
                    return [w, h];
                }}
                function _exportMermaidSvgPng(svg, scale) {{
                    if (!svg) return;
                    var serialized = new XMLSerializer().serializeToString(svg);
                    var wh = _svgIntrinsicSize(svg);
                    var w = wh[0], h = wh[1];
                    var img = new Image();
                    img.onload = function () {{
                        var canvas = document.createElement('canvas');
                        canvas.width = Math.round(w * scale);
                        canvas.height = Math.round(h * scale);
                        var ctx = canvas.getContext('2d');
                        ctx.fillStyle = _CHART_BG;
                        ctx.fillRect(0, 0, canvas.width, canvas.height);
                        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                        _emitChartPng(canvas.toDataURL('image/png'));
                    }};
                    img.src = 'data:image/svg+xml;base64,' + _b64EncodeUtf8(serialized);
                }}
                window._attachChartToolbar = function (el, type) {{
                    if (!el || el._toolbarAttached) return;
                    el._toolbarAttached = true;
                    // 防御兜底：absolute 定位基准必须是容器自身（历史 CSS 曾被字面 ''' 破坏）
                    if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
                    var bar = document.createElement('div');
                    bar.className = 'chart-toolbar';
                    var btnExpand = document.createElement('button');
                    btnExpand.setAttribute('data-tooltip', '放大查看');
                    btnExpand.innerHTML = '<img src="' + _ICON_BASE + '/最大化.svg" />';
                    var btnExport = document.createElement('button');
                    btnExport.setAttribute('data-tooltip', '导出 PNG（3x）');
                    btnExport.innerHTML = '<img src="' + _ICON_BASE + '/导入.svg" />';
                    bar.appendChild(btnExpand);
                    bar.appendChild(btnExport);
                    el.appendChild(bar);
                    btnExpand.addEventListener('click', function (ev) {{
                        ev.stopPropagation();
                        try {{
                            if (type === 'echarts' && el._chartInstance) {{
                                var opt = JSON.stringify(el._chartInstance.getOption());
                                console.log('pywebview_action:chart_expand:echarts:' + _b64EncodeUtf8(opt));
                            }} else if (type === 'mermaid') {{
                                var svg = el.querySelector('svg');
                                if (!svg) return;
                                console.log('pywebview_action:chart_expand:mermaid:' + _b64EncodeUtf8(svg.outerHTML));
                            }}
                        }} catch (e) {{ console.error('[chart] expand failed:', e); }}
                    }});
                    btnExport.addEventListener('click', function (ev) {{
                        ev.stopPropagation();
                        try {{
                            if (type === 'echarts' && el._chartInstance) {{
                                el._chartInstance.resize();  // 防实例内部宽度过期导致导出畸形
                                _emitChartPng(el._chartInstance.getDataURL({{ type: 'png', pixelRatio: 3, backgroundColor: _CHART_BG }}));
                            }} else if (type === 'mermaid') {{
                                _exportMermaidSvgPng(el.querySelector('svg'), 3);
                            }}
                        }} catch (e) {{ console.error('[chart] export failed:', e); }}
                    }});
                }};

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
                // 初始即「跟随底部」：未滚动时自动滚底；一旦用户上滚离开，
                // 由下方 scroll 监听按位置判定改为停止跟随，滚回底部附近自动恢复。
                window._userScrolledWithin = false;
                window._suppressScrollEvent = false;
                window._prevScrollTop = 0;  // 历史基线，已不再用于判定
                document.body.addEventListener('scroll', function() {{
                    var _st = document.body.scrollTop;
                    // 即使被抑制也保持 _prevScrollTop 同步，避免后续用户滚动时
                    // _prevScrollTop 陈旧（该字段仅作历史基线，不再用于任何判定）。
                    if (window._suppressScrollEvent) {{
                        window._prevScrollTop = _st;
                        return;
                    }}
                    window._prevScrollTop = _st;
                    // 🔧 核心修复：用「位置判定」取代脆弱的 delta 阈值。
                    // 靠近底部(_scrollThreshold 内) = 跟随态(_userScrolledWithin=false)，
                    // 离开底部 = 用户主动上滚(_userScrolledWithin=true)。
                    // 程序性滚底同样落在底部 → 自动恢复跟随；用户滚轮上滚 → 立即停止
                    // 跟随；滚回底部附近 → 恢复跟随。彻底消除 delta 竞态导致的
                    // “输出跳到莫名其妙位置 / 滚轮被永久锁死”问题。
                    var _nearBottom = Math.abs(document.body.scrollHeight - _st - document.body.clientHeight) < {
            AUTO_SCROLL_THRESHOLD
        };
                    window._userScrolledWithin = !_nearBottom;
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

                // ===== 任务列表（嵌入工具区，随工具区折叠/归位/沉底）=====
                var _TODO_SNAKE_SVG = '{_THINK_SNAKE_SVG}';
                window._todoCount = 0;
                window._todoProgressText = '';
                window._updateTodoList = function(todos) {{
                    var panel = document.getElementById('todo-panel');
                    if (!panel) return;
                    var content = document.getElementById('todo-content');
                    var prog = document.getElementById('todo-progress');
                    var ts = document.getElementById('tool-section');
                    var hr = (typeof reportHeightDebounced === 'function') ? reportHeightDebounced : null;
                    if (!todos || !todos.length) {{
                        window._todoCount = 0;
                        window._todoProgressText = '';
                        if (prog) prog.textContent = '';
                        if (panel.style.display !== 'none') {{
                            panel.style.display = 'none';
                            // 无工具块时连工具区一起隐藏
                            var _tc0 = document.getElementById('tool-content');
                            if (ts && _tc0 && _tc0.children.length === 0) ts.style.display = 'none';
                            if (hr) hr();
                        }}
                        if (ts && typeof _updateToolSectionHeader === 'function') _updateToolSectionHeader();
                        return;
                    }}
                    var html = '';
                    var done = 0;
                    for (var i = 0; i < todos.length; i++) {{
                        var t = todos[i] || {{}};
                        var status = t.status || 'pending';
                        if (status === 'completed') done++;
                        var icon;
                        if (status === 'in_progress') {{
                            icon = '<span class="todo-spin">' + _TODO_SNAKE_SVG + '</span>';
                        }} else if (status === 'completed') {{
                            icon = '<span class="todo-done-icon">✓</span>';
                        }} else {{
                            icon = '<span class="todo-pending-icon">○</span>';
                        }}
                        html += '<div class="todo-item" data-status="' + status + '" data-priority="' + (t.priority || 'medium') + '">' + icon +
                                '<span class="todo-text">' + (t.content || '') + '</span></div>';
                    }}
                    window._todoCount = todos.length;
                    // 重建前保存用户滚动状态：innerHTML 重建会把 scrollTop 归零，
                    // 且归零触发的 scroll 事件会误置 _userScrolledUp（用 _progScroll 吞掉）
                    var _wasUp = !!content._userScrolledUp;
                    var _prevTop = content.scrollTop;
                    content._progScroll = true;
                    content.innerHTML = html;
                    var progText = ' ' + done + '/' + todos.length + ' 完成';
                    window._todoProgressText = progText;
                    if (prog) prog.textContent = progText;
                    panel.style.display = '';
                    // 有 todo 时工具区必须可见（即使暂无工具/思考块）
                    if (ts) ts.style.display = '';
                    if (ts && typeof _updateToolSectionHeader === 'function') _updateToolSectionHeader();
                    // 始终保持第一个进行中任务可见（列表超出限高时滚动到可视区）
                    // 双 rAF：面板可能刚 display:''，等布局完成后再读 offsetTop/clientHeight。
                    // 手动设 scrollTop 只动本容器，不扰动祖先链（scrollIntoView 会连带滚 body/工具区）。
                    // 用户上滚查看中 → 恢复原位置；未滚动 → 定位到进行中项
                    window._todoScrollToken = (window._todoScrollToken || 0) + 1;
                    var _tk = window._todoScrollToken;
                    requestAnimationFrame(function() {{
                        requestAnimationFrame(function() {{
                            if (_tk !== window._todoScrollToken) return;  // 已有更新，放弃旧滚动
                            content._progScroll = true;
                            if (_wasUp) {{
                                var _maxT = Math.max(0, content.scrollHeight - content.clientHeight);
                                content.scrollTop = Math.min(_prevTop, _maxT);
                                return;
                            }}
                            var act = content.querySelector('.todo-item[data-status="in_progress"]');
                            if (!act) return;
                            var target = act.offsetTop - (content.clientHeight - act.offsetHeight) / 2;
                            var maxScroll = content.scrollHeight - content.clientHeight;
                            content.scrollTop = Math.max(0, Math.min(target, Math.max(0, maxScroll)));
                        }});
                    }});
                    if (hr) hr();
                }};

                // ===== 工具区（#tool-content）自动滚底 =====
                // 当工具/思考区有新内容时，自动滚动到底部，让用户始终看到最新状态。
                // 用户主动上滚后不再打扰（_userScrolledUp），滚回底部附近自动恢复跟随。
                function _scrollToolContentToBottom() {{
                    var tc = document.getElementById('tool-content');
                    if (!tc) return;
                    // 用户主动向上滚动了工具区则不自动滚底
                    if (tc._userScrolledUp) return;
                    // 抑制本次程序滚底触发的 scroll 事件：异步 scroll 到达时
                    // scrollHeight 可能已增长（流式新块加入），atBottom 误判 false
                    // 会错误置位 _userScrolledUp 导致跟随中断。
                    tc._progScroll = true;
                    tc.scrollTop = tc.scrollHeight;
                }}
                // 工具区滚动跟踪：用户主动向上滚动时标记，滚到底部时取消标记
                document.getElementById('tool-content')?.addEventListener('scroll', function() {{
                    var tc = this;
                    // 程序性滚底（_scrollToolContentToBottom / innerHTML 重建）不视为用户行为
                    if (tc._progScroll) {{ tc._progScroll = false; return; }}
                    var atBottom = Math.abs(tc.scrollHeight - tc.scrollTop - tc.clientHeight) < 30;
                    tc._userScrolledUp = !atBottom;
                    if (atBottom) tc._userScrolledUp = false;
                }});
                // 任务列表滚动跟踪：与工具区同款（程序滚动/重建不算用户行为）
                document.getElementById('todo-content')?.addEventListener('scroll', function() {{
                    var td = this;
                    if (td._progScroll) {{ td._progScroll = false; return; }}
                    var atBottom = Math.abs(td.scrollHeight - td.scrollTop - td.clientHeight) < 30;
                    td._userScrolledUp = !atBottom;
                    if (atBottom) td._userScrolledUp = false;
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
        _skeleton_cache.move_to_end(cache_key)
        if len(_skeleton_cache) > _SKELETON_CACHE_MAX:
            _skeleton_cache.popitem(last=False)  # LRU：淘汰最久未用
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

    @staticmethod
    def _has_reached_soft_boundary(md_text: str) -> bool:
        """检测 markdown 是否以句号类标点结尾（软边界，适合差量渲染）。

        大段中文正文常无 \n\n 空行，_has_reached_clean_boundary（硬边界）无法
        在流式期间及时触发渲染。句号结尾即视为「可增量闭合」的软边界，
        触发差量渲染（_extract_closed_segments 会按句号软边界切段），
        而不必等安全定时器兜底（300ms）——显著缩短纯文本停留时间。

        Returns:
            True: 文本以句号类标点结尾，适合触发差量渲染
        """
        if not md_text:
            return False
        stripped = md_text.rstrip()
        return bool(stripped) and stripped[-1] in _SENTENCE_END_CHARS

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
            # 差量渲染：仅在自然边界（硬/软）触发，否则靠增量文本 + 安全兜底
            if self._has_reached_clean_boundary(self._markdown_text) or self._has_reached_soft_boundary(
                self._markdown_text
            ):
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
        # [PERF] 不可见期间跳过 DOM 注入。resize preview / 对话框穿透防护 / 切 tab
        # 隐藏期间，viewer 已被 MessageCard.hide() 或 WA_TranslucentBackground
        # 守卫关掉，但 runJavaScript 仍会执行 → Chromium 持续累积 DOM 节点 →
        # preview 退出或恢复可见时首帧 paint 阻塞整页重排，是流式 + resize 卡顿的
        # 根因之一。文本已在 _markdown_text 累积，恢复可见时由 _perform_update
        # 一次性渲染（_render_deferred 标记 + showEvent 补渲已覆盖此路径）。
        if not self.isVisible():
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
                var text = {json.dumps(text_clean).decode("utf-8")};
                var c = document.getElementById('content-placeholder');
                if (!c || !text) return;
                // ── 尾部文本宿主定位 ──
                // rendered 尾部节点的 innerHTML 是 md.convert 产物（<p>/<ul>/<li>/<pre>…），
                // 直接把文本追加到节点本身会落在最后一个块级元素**之后**（另起一段）。
                // 下潜到最后一块级元素内，让文字接在已有文字后面**连续增长**。
                var _BLOCK_TAGS = {{P:1, UL:1, OL:1, LI:1, BLOCKQUOTE:1, PRE:1, H1:1, H2:1, H3:1, H4:1, H5:1, H6:1}};
                function _tailTextHost(node) {{
                    var host = node;
                    for (var _g = 0; _g < 6; _g++) {{
                        var lc = host.lastElementChild;
                        if (!lc) break;
                        if (lc.tagName === 'PRE') {{
                            // 代码块：文本承载在 <code> 内（行内 <code> 不下潜，
                            // 避免后续正文钻进行内代码）
                            host = (lc.lastElementChild && lc.lastElementChild.tagName === 'CODE')
                                ? lc.lastElementChild : lc;
                            break;
                        }}
                        if (!_BLOCK_TAGS[lc.tagName]) break;
                        host = lc;
                    }}
                    return host;
                }}
                function _newIncrementalP(txt) {{
                    var _p = document.createElement('p');
                    // [B1] 标记为增量纯文本节点：差量渲染追加格式化 HTML 时会先移除
                    _p.setAttribute('data-incremental', 'true');
                    _p.textContent = txt;
                    c.appendChild(_p);
                    return _p;
                }}
                // ── 智能段落处理 ──
                // 只有**段落分隔**（>=2 个换行）才新建 <p>；单个换行是 Markdown 软换行
                // （最终渲染为空格），必须接在当前段内 —— 否则每个 chunk 独占一行，
                // 流式期间整段正文被切成一堆碎片行。
                var lead = text.match(/^[\\r\\n]+/);
                var newlines = lead ? lead[0].replace(/\\r\\n/g, '\\n').length : 0;
                var last = c.lastElementChild;
                if (newlines >= 2) {{
                    // 段落分隔：去掉前导换行，创建独立 <p>
                    var clean = text.replace(/^[\\n\\r]+/, '');
                    if (clean) {{
                        _newIncrementalP(clean);
                    }} else {{
                        // 纯分隔换行（chunk 里只有 \\n\\n）：**不建空节点** ——
                        // 空 <p> 的上下 margin 会凭空撑高一行，150~500ms 后又被
                        // tail 渲染移除，表现为"正文下方闪一段空白"。改为打挂起标记，
                        // 由下一个文字 chunk 建新段落（无空行抖动 + 段落立即正确）。
                        c.setAttribute('data-pending-break', '1');
                    }}
                }} else if (c.getAttribute('data-pending-break') === '1') {{
                    // 上一段以纯分隔换行收尾：新文字必须另起一段。
                    // 否则会短暂粘在上一段末尾，等下次 tail 渲染才分开 → 又是一次跳位。
                    c.removeAttribute('data-pending-break');
                    _newIncrementalP(text);
                }} else if (last && last.getAttribute('data-incremental') === 'true') {{
                    // 🐛 修复（流式文字碎片化）：增量节点（含 data-rendered 的尾部渲染节点）
                    // **就地追加文本节点**承接新文字，保持与已有内容连续。
                    // 旧逻辑对 data-rendered 节点新建独立 <p> —— 流式期间每个 chunk 都堆出
                    // 一个带段落间距的新行，观感就是"文字先在最后几行以片段形式冒出来，
                    // 随后 updateTailHtml 又把碎片合并回正文"（文字不断跳位重排）。
                    // appendChild(textNode) 既不覆盖已渲染的行内 HTML（textContent += 会把
                    // <strong>/<code> 抹回 markdown 源码形态），又让文字连续增长。
                    _tailTextHost(last).appendChild(document.createTextNode(text));
                }} else if (last && last.tagName === 'P') {{
                    // 🐛 修复（正文段落丢失）：最后是已格式化渲染的稳定段落（非增量节点）。
                    // 不能打 data-incremental 标记/原地追加——否则下次差量渲染
                    // updateContentAppend 移除全部 data-incremental 节点时会连带
                    // 删除该稳定段落（已渲染正文永久丢失，"内容显示不全"）。
                    // 新建增量节点承载：格式化段落必为已闭合段（\\n\\n 结尾），
                    // 后续文本属新段落，独立 <p> 结构正确。
                    _newIncrementalP(text);
                }} else {{
                    // 思考块 / 工具块 / 空容器等：新段落承载
                    _newIncrementalP(text);
                }}
                // 🐛 修复：同步 auto-scroll（无 setTimeout 渲染间隙），
                // 避免浏览器在异步间隙中 paint 出滚动位置不一致的画面。
                // 附加修复：auto-scroll 成功后复位 _userScrolledWithin，
                // 防止用户一次滚轮操作后永久丧失粘性滚底能力。
                // 用 scrollTop 差值识别用户滚动（替代原 200ms 时间窗，避免
                // 快速流式时时间窗永不过期导致用户滚轮被永久忽略）。
                window._suppressScrollEvent = true;
                if (!window._userScrolledWithin) {{
                    _autoScrollStreamingBody();
                }} else {{
                    var wasAtBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < {AUTO_SCROLL_THRESHOLD};
                    if (wasAtBottom) {{
                        _autoScrollStreamingBody();
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
        processed_md = _inject_tag_cards(processed_md, self._streaming is False, compact=self._tool_compact_mode)

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
            # 🛡️ F2：JS 未就绪时的渲染请求标记 deferred（不直接丢弃），
            # _on_js_ready 时统一补渲。否则 viewer 创建后未显示 + JS 未加载
            # 完成 + 期间渲染请求（隐藏 tab 积压）→ 请求被清但永不补渲，
            # 工具区/消息区永久空白且无自愈路径。
            self._render_deferred = True
            return
        # [V1] 可见性门控：隐藏 tab 不启动渲染定时器、不立即渲染，
        # 仅标记 deferred，恢复可见时（showEvent）按需补渲。
        # 流式数据由 worker 驱动写入 _markdown_text，门控只跳过 UI 渲染帧，不丢数据。
        if not self.isVisible():
            self._render_deferred = True
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
            # 流式模式下检查自然边界（硬边界空行 / 软边界句号结尾）
            if self._has_reached_clean_boundary(self._markdown_text) or self._has_reached_soft_boundary(
                self._markdown_text
            ):
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
        # [B1] 字体变化：差量 HTML 缓存失效，强制全量重渲染
        self._needs_full_render = True
        self._stable_html = ""
        self._stable_md_len = 0
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

        # 🐛 主题确实变化：失效实例级 markdown HTML 缓存。
        # 否则 _perform_update 非流式分支的 _cached_streaming_html 复用逻辑
        # 会返回旧主题渲染的 HTML（旧 pygments 代码高亮 + 旧思考图标路径），
        # 导致主题切换后代码块颜色/思考图标不更新。
        self._cached_streaming_html = None
        self._processed_md_hash = 0
        self._cached_raw_md_hash = 0
        # [B3] 主题变化：递增渲染序号使在途线程池任务过期（旧主题 HTML 丢弃），
        # 强制后续 _schedule_render 以新主题重新提交渲染。
        self._render_seq += 1
        # [B1] 主题变化：差量 HTML 缓存失效（旧主题高亮/图标颜色），强制全量重渲染
        self._needs_full_render = True
        self._stable_html = ""
        self._stable_md_len = 0

        theme = current_theme()
        js_code = ThemeRefreshCoordinator.get_or_build_js(theme, _is_light)

        # [PERF] 仅对可见 viewer 注入 CSS 变量：隐藏卡（不可见 tab / 未渲染）
        # 跳过 runJavaScript（WebEngine IPC 开销大，200 卡 ≈ 100ms）。
        # 跳过时置 _theme_css_pending 标记，恢复可见（showEvent）补注入，
        # 避免 updateContent 复用旧骨架 CSS 变量导致主题色残留。
        try:
            if self.page():
                if self.isVisible():
                    self.page().runJavaScript(js_code)
                    self._theme_css_pending = False
                else:
                    self._theme_css_pending = True
        except RuntimeError:
            pass

    def _perform_update(self):
        try:
            if not self.page():
                return

            # [V1] 可见性门控（双保险）：直接调用路径（如工具结果到达时
            # MessageCard 直接调 viewer._perform_update）绕过 _schedule_render，
            # 隐藏 tab 时不执行 setHtml/runJavaScript，标记 deferred 待恢复补渲。
            if not self.isVisible():
                self._render_deferred = True
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
                # [B2] IPC 瘦身：仅当工具 DOM 被 JS 增量注入（_tool_dom_dirty）或存在
                # 已完成工具块待 restore（_restore_finished_ids）时才需要 save/restore 保护；
                # 否则裸 updateContent（省整页 JS 包装，MB 级 IPC 载荷下降）。
                _needs_save_restore = self._tool_dom_dirty or bool(getattr(self, "_restore_finished_ids", set()))
                if _needs_save_restore:
                    _gen = self._tool_dom_dirty_gen
                    self.page().runJavaScript(
                        self._build_save_and_restore_js(html_content, getattr(self, "_restore_finished_ids", set())),
                        lambda _r, _g=_gen: self._clear_tool_dom_dirty_guarded(_g),
                    )
                else:
                    self.page().runJavaScript(
                        f"updateContent({json.dumps(html_content).decode('utf-8')});",
                        lambda _result: None,
                    )
                # 🐛 修复（编辑工具框运行中消失）：不再同步清除 _tool_dom_dirty——
                # runJavaScript 异步，JS 未执行完时 DOM 中运行框仍在；若立即清 dirty，
                # 紧随其后的渲染（正文流式/兜底/finish_streaming）判定 _needs_save_restore=False
                # → 裸 updateContent 重建 content-placeholder → 抹掉 JS 注入的运行框。
                # 清除交由 JS 回调 _clear_tool_dom_dirty_guarded（pending + 代际守卫）。
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

            # [B1] 差量渲染快路径：流式且非全量模式时，仅增量渲染已闭合的完整段。
            # 条件：流式 + 未强制全量 + 无活跃工具 DOM（工具块走 save/restore 全量保护）
            if self._streaming and not self._needs_full_render and not self._has_active_tool_dom():
                stable_len, segs = _extract_closed_segments(self._markdown_text[self._stable_md_len :])
                if segs:
                    # 增量渲染闭合段：sanitize→inject→md.convert（主线程小段快速路径）
                    # 差量段很小（单个段落/代码块），同步渲染耗时 <1ms，无需线程池
                    # 🐛 修复：传 _tool_compact_mode，与全量渲染 _render_markdown_to_html
                    # 的 compact 对齐——否则差量段硬编码 compact=False 会把思考块渲染成
                    # think-block 折叠框（简洁模式下应为 think-compact），形态分裂
                    # （9c76d04f 只给 _render_stable_segment 加了参数，调用点漏改）。
                    new_html = "".join(_render_stable_segment(seg, compact=self._tool_compact_mode) for seg in segs)
                    self._stable_md_len += stable_len
                    self._stable_html += new_html
                    # ⚠️ updateContentAppend 是"追加"语义：只推送本次新增段，
                    # 不能推送累积值（否则旧段重复渲染）。
                    # 🐛 修复（正文尾部丢失）：_extract_closed_segments 只产出
                    # 已闭合段；未闭合尾部（stable 之后的剩余 md）若不移交给 JS，
                    # updateContentAppend 移除 data-incremental 节点时会连带删除
                    # 该尾部 → 正文尾部永久丢失（用户可见"显示不全"）。
                    # 将未闭合尾部**行内渲染后的 HTML**作为第二参数传入，JS 端重建
                    # 增量节点保尾（innerHTML 注入：已闭合的行内语法即时格式化，
                    # 不再字面显示 markdown 源码）。
                    # ⚠️ 未闭合 think/tool：tail 含未闭合块时不渲染（静默累积，
                    # 等闭合后由差量段/全量渲染处理，避免思考内容泄漏到正文）。
                    _tail = self._markdown_text[self._stable_md_len :]
                    _tail_html = ""
                    if _tail and not _has_unclosed_think_or_tool(_tail):
                        _tail_html = _render_inline_tail(_tail, compact=self._tool_compact_mode)
                    js = (
                        "updateContentAppend("
                        f"{json.dumps(new_html).decode('utf-8')},"
                        f"{json.dumps(_tail_html).decode('utf-8')});"
                    )
                    self.page().runJavaScript(js)
                    # 已差量消费的 markdown 视为"已渲染"（避免重复全量）
                    self._last_rendered_markdown = self._markdown_text[: self._stable_md_len]
                    return
                # 无新闭合段：增量纯文本已在 DOM（_append_text_incremental）。
                # 🐛 修复（思考块滞留/泄漏）：think 配对守卫 break（未闭合 think 段）
                # 使差量一个段都产不出时，若 md 已达自然边界（think 闭合 `</think>` /
                # 段落 `\n\n` 结尾），必须走全量渲染消费——否则安全定时器触发的
                # _perform_update 也会被此分支拦截，思考块/闭合段永远滞留
                # （或仅靠流式结束才一次性显示）。
                if self._has_reached_clean_boundary(self._markdown_text):
                    self._refresh_viewer_font_css()
                    self._sequence_render(self._markdown_text, self._tool_compact_mode)
                else:
                    # 🐛 修复（流式显示与最终不符）：无空行分隔的长段落没有闭合段
                    # 可差量渲染，尾部在流式期间以纯文本显示 markdown 源码
                    # （**加粗**、`code`、[链接](url)），直到流式结束全量渲染才
                    # 格式化。将尾部整体行内渲染（单个 convert 保持段落/列表/代码块
                    # 结构正确），替换 DOM 增量节点；未闭合 think/tool 跳过防泄漏。
                    self._render_tail_inline()
                return

            # 🐛 修复（大段正文流式期间纯文本滞留）：差量快路径被 _needs_full_render
            # （初始 True，首次全量渲染应用成功才置 False）或 _has_active_tool_dom()
            # 让位时，流式正文只能依赖全量线程池渲染。大段正文渲染耗时长，期间新
            # chunk 持续提交新 seq → 在途结果被 _apply_render_result 的 seq 校验
            # 丢弃 → _needs_full_render 保持 True → 差量路径永远进不去 → 纯文本
            # 滞留到流式结束才一次性刷新成 HTML。
            # 尾部行内渲染不依赖全量渲染（自带哈希缓存、只操作 data-incremental
            # 节点，对工具 DOM 安全），在差量不可走的流式路径也先执行，保证流式
            # 期间 markdown 语法（**加粗**、`code`、[链接]）即时格式化。
            if self._streaming:
                self._render_tail_inline()

            # 刷新字体 CSS var
            self._refresh_viewer_font_css()

            # [B3] 渲染移出主线程：提交线程池渲染，完成回调在主线程应用 DOM。
            # 主线程不再同步执行 md.convert（20-80ms 阻塞消除），
            # 渲染参数以快照形式传引用（md 不复制）。
            self._last_rendered_markdown = self._markdown_text
            self._sequence_render(self._markdown_text, self._tool_compact_mode)

        except RuntimeError:
            pass

    def _render_tail_inline(self):
        """把流式未闭合尾部整体行内渲染为 HTML，替换 DOM 增量纯文本节点。

        解决：无空行分隔的长段落（大模型常见输出，尤其中文）在流式期间没有
        `\\n\\n` 闭合段可差量渲染，尾部长时间以纯文本显示 markdown 源码
        （**加粗**、`code`、[链接](url)），只有流式结束全量渲染才格式化——
        用户感知"流式显示内容与最终不符"。

        尾部整体一次 convert（_render_inline_tail）：段落/列表/引用/代码块
        结构在单一 markdown 上下文中保持正确；未闭合行内语法由 markdown 库
        字面保留、闭合后由下一次渲染补全。产物为带 data-incremental +
        data-rendered 标记的节点，后续差量段（updateContentAppend）与全量
        （updateContent）会整体移除替换，无重复。

        带哈希缓存：尾部文本未变化（安全定时器重复触发）时跳过重复渲染。
        """
        _tail = self._markdown_text[self._stable_md_len :]
        if not _tail or not _tail.strip():
            return
        # 🐛 未闭合 think/tool：静默累积，不在此渲染（过滤标签会把思考内容
        # 当正文泄漏显示），等闭合后由差量段/全量渲染处理。
        if _has_unclosed_think_or_tool(_tail):
            return
        _h = hash(_tail)
        if _h == self._tail_html_hash:
            return
        html = _render_inline_tail(_tail, compact=self._tool_compact_mode)
        # 无论结果是否为空都记录哈希（think/tool 尾部返回空串时避免重复计算）
        self._tail_html_hash = _h
        if not html:
            return
        try:
            js = f"updateTailHtml({json.dumps(html).decode('utf-8')});"
            self.page().runJavaScript(js)
        except RuntimeError:
            pass

    def _clear_tool_dom_dirty_guarded(self, gen: int):
        """JS 渲染回调：带守卫地清除 _tool_dom_dirty。

        🐛 修复（编辑工具框运行中消失）的双重守卫：
        - pending 守卫：_injected_pending_tools 非空（仍有 JS 注入未完成的工具块在
          DOM，如运行框/完成态预览框）→ 不清除。这些块不在 markdown 中，若清 dirty，
          下一次全量渲染会裸 updateContent 抹掉它们（直到 append_tool_result 才重现）。
        - 代际守卫：_tool_dom_dirty_gen 与捕获值一致才清除。若期间有新注入
          （append_tool_result / update_tool_streaming 递增了代际），本回调放弃清除，
          避免"旧渲染回调误清新 dirty"导致运行框失去保护。

        仅在"渲染 JS 真正执行完成"后由 runJavaScript 回调调用（同步清除的旧逻辑
        在 JS 异步未执行时就把 dirty 清掉，是"运行中→完成中间消失"的根因）。
        """
        try:
            if getattr(self, "_injected_pending_tools", None):
                return
            if getattr(self, "_tool_dom_dirty_gen", 0) == gen:
                self._tool_dom_dirty = False
        except Exception:
            pass

    def _has_active_tool_dom(self) -> bool:
        """B1: 是否有活跃工具 DOM（JS 注入的工具块 / 待 restore 的完成块）。
        返回 True 时差量渲染必须让位全量渲染（工具块涉及 save/restore 保护，
        且 _tool_md_cache 影响 _inject_tool_blocks 输出——差量段渲染不带该缓存，
        会导致工具块 HTML 与全量不一致）。
        """
        if self._tool_dom_dirty:
            return True
        try:
            if getattr(self, "_restore_finished_ids", None):
                return True
            # pending 集合非空 = 仍有 JS 注入未完成的工具块在 DOM（运行框/预览框），
            # 差量渲染同样必须让位全量渲染（save/restore 保护）。防御：dirty 清除
            # 回调理论上已受 pending 守卫，此处再兜底一次防其他路径直接改 dirty。
            if getattr(self, "_injected_pending_tools", None):
                return True
        except Exception:
            pass
        return False

    # ========== B3: 异步渲染（线程池 + 序号校验 + 防抖） ==========

    def _collect_render_snapshot(self, md: str, compact: bool) -> dict:
        """主线程：采集渲染快照（只读全局参数，md 引用传递不复制）"""
        try:
            from app.utils.theme_manager import theme_manager

            _style = "friendly" if theme_manager.is_light_theme() else "dracula"
        except Exception:
            _style = "dracula"
        return {
            "md": md,
            "streaming": self._streaming,
            "thinking_finalized": getattr(self, "_thinking_finalized", False),
            "compact": compact,
            "pygments_style": _style,
            "icon_prefix": _ICON_PREFIX_CACHE,
            "code_font_size": _CODE_FONT_SIZE,
        }

    def _sequence_render(self, md: str, compact: bool):
        """B3: 序列化异步渲染——提交线程池，在途时只记 pending（防抖积压最新快照）

        - 序号校验：每次提交 seq+=1，回调时 seq != self._render_seq 视为过期丢弃
        - 防抖：在途任务未完成时，新请求只覆盖 _render_pending；完成后续派最新
        """
        self._render_seq += 1
        seq = self._render_seq
        if self._render_inflight:
            # 在途：只记录最新 pending，完成回调后统一续派
            self._render_pending = (seq, md, compact)
            return
        self._render_inflight = True
        snapshot = self._collect_render_snapshot(md, compact)
        try:
            fut = _RENDER_POOL.submit(_render_markdown_to_html_worker, snapshot)
        except RuntimeError:
            # 线程池已关闭（进程退出）：降级为同步渲染
            self._render_inflight = False
            self._apply_render_result(seq, self._render_markdown_to_html(md))
            return
        wself = weakref.ref(self)
        fut.add_done_callback(lambda f, s=seq, w=wself: _dispatch_render_done(s, f, w))

    def _on_render_done_signal(self, seq: int, html):
        """主线程槽：接收 worker 线程池渲染完成信号（renderDone.emit 跨线程投递）"""
        try:
            self._apply_render_result(seq, html)
        except RuntimeError:
            pass

    def _apply_render_result(self, seq: int, html):
        """B3: 主线程应用渲染结果（线程池回调经 QTimer.singleShot 转发至此）

        - seq 守卫：过期结果（新渲染已提交）直接丢弃
        - 成功后检查 pending 续派（在途期间积压的最新快照）
        """
        try:
            if seq != self._render_seq:
                # 过期结果：丢弃（新渲染已提交或已失效）
                return
            if html is None:
                return
            if sip.isdeleted(self) or not self.page():
                return
            self._last_rendered_html = html
            self._height_report_pending = True
            # [B1] 全量渲染成功应用后：重置差量基线——差量稳定区与全量内容对齐，
            # 后续流式新段从当前 markdown 末尾继续差量追加（不再重复渲染已全量覆盖的内容）。
            # ⚠️ 必须用 _last_rendered_markdown（线程池提交时的渲染对象），而非
            # _markdown_text（回调到达时可能已被新 chunk 追加，造成差量跳过未渲染内容）。
            self._stable_html = html
            # 🐛 修复（思考泄漏）：md 含未闭合  thinking/<tool> 块时**不**推进差量基线。
            # 首次流式迭代的 append_reasoning 首 chunk 会触发全量渲染（显示
            # "深度思考中" spinner），此时 md 是部分的思考内容（未闭合 think）。
            # 若照常推进基线，后续差量扫描起点会落在 think 块内部，切片以
            # `内容 response` 开头（无 ` thinking` 配对）→ 配对守卫不触发 →
            # 残段被当普通正文渲染 → 思考内容泄漏到正文（后续全量渲染才消失）。
            # 保持旧基线 → 下一次差量从 think 开头扫描，配对守卫正确 break，
            # 等思考完整闭合后整体差量/全量渲染（无重复：基线未推进期间不产出段）。
            if self._streaming and not _has_unclosed_think_or_tool(self._last_rendered_markdown):
                self._stable_md_len = len(self._last_rendered_markdown)
            self._needs_full_render = False
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
            # [B2] IPC 瘦身：仅当工具 DOM 被 JS 增量注入（_tool_dom_dirty）或存在
            # 已完成工具块待 restore（_restore_finished_ids）时才走 save/restore 包装
            _needs_save_restore = self._tool_dom_dirty or bool(getattr(self, "_restore_finished_ids", set()))
            if _needs_save_restore:
                js_code = self._build_save_and_restore_js(html, getattr(self, "_restore_finished_ids", set())).replace(
                    "})();", auto_scroll_js + "})();"
                )
            else:
                js_code = f"updateContent({json.dumps(html).decode('utf-8')});" + auto_scroll_js
            # 🐛 修复（编辑工具框运行中消失）：dirty 清除延后到 JS 回调（pending + 代际守卫），
            # 原理同 _perform_update 非流式分支——避免异步 JS 未执行期间被下一次渲染
            # 误判"无工具 DOM"而裸 updateContent 抹掉 JS 注入的运行框。
            _gen = self._tool_dom_dirty_gen
            self.page().runJavaScript(js_code, lambda _r, _g=_gen: self._clear_tool_dom_dirty_guarded(_g))
            # 释放缓存：HTML 已推送到 WebEngine，Python 端不再保留减少内存占用
            self._last_rendered_html = None
        except RuntimeError:
            pass
        finally:
            # 无论成功/过期，都要释放 in-flight 并续派 pending（若有）
            self._render_inflight = False
            if self._render_pending:
                pseq, pmd, pcompact = self._render_pending
                self._render_pending = None
                self._sequence_render(pmd, pcompact)

    def _build_save_and_restore_js(self, html_content: str, finished_ids: set = None) -> str:
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

        Args:
            html_content: 全量渲染的 HTML
            finished_ids: 已完成（结果已 append_tool_result）的工具 id 集合。
                restore 时这些 id 的块**不恢复**（markdown 已含其结果，updateContent
                会重新生成）；未完成的块（运行中 / finish_tool_streaming 完成态预览）
                必须恢复——它们不在 markdown 中，save 后若不恢复会被抹掉。

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
        _finished_js = json.dumps(list(finished_ids or set())).decode("utf-8")
        return (
            "(function(){"
            # 🆕 Bug B 方案 E：save 阶段把流式工具块的 data-order 暂存到 window，
            # 供 reorganizeContent 补 data-order 时合并（_streamFloors）。根因：save 会
            # 把所有 data-tool-call-id 块（含仍在流式、尚未进入 _content_data 的工具块）
            # 从 #tool-content 移除，导致 reorganizeContent 执行时 toolContent.children
            # 里已无流式块 → _streamFloors 恒为空 → 思考/完成工具块补的 data-order 缺少
            # "排在其前的流式工具数"修正 → restore 按保存的 data-order 插回时与思考块
            # 尺度不一致 → 找不到比它大的节点 → appendChild 沉底 → 折叠框内
            # "所有思考在前、所有工具在后"（坞态归位瞬间错乱）。
            "window.__pendingStreamFloors=[];"
            f"var _tc=document.getElementById('{_target_id}');"
            # 🆕 修复（简洁模式编辑工具框消失）：save 阶段必须同时覆盖正文容器
            # #content-placeholder——编辑类工具（write/edit/multi_edit 等 _edit_tools() 派生）
            # 的流式/完成块由 JS 注入到正文（L9328 _stream_target），简洁模式下
            # _tool_target_id="tool-content"，旧 save 只遍历 _tc → 编辑工具运行框
            # 不在保存范围 → 全量渲染 updateContent 重建正文时被抹除，直到
            # append_tool_result 才重新出现（"运行中→完成"中间消失一阵子）。
            "var _tcBody=document.getElementById('content-placeholder');"
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
            "var _saveRoots=[_tc];"
            "if(_tcBody&&_tcBody!==_tc)_saveRoots.push(_tcBody);"
            "for(var _sr=0;_sr<_saveRoots.length;_sr++){var _root=_saveRoots[_sr];"
            "if(_root&&_root.children.length){"
            "Array.prototype.forEach.call(Array.prototype.slice.call(_root.children),function(el,i){"
            "if(el.hasAttribute&&el.hasAttribute('data-tool-call-id')){"
            # 🆕 方案 E：暂存流式块（data-streaming="true"）的 data-order，供
            # reorganizeContent 补 data-order 时修正"排在其前的流式工具数"。
            # 这些块即将被 remove()，不在 markdown 中、不会被重新渲染，
            # 只有 save/restore 保留；若不暂存其 data-order，reorganizeContent
            # 的 _streamFloors 收集不到它们 → 思考块补的 data-order 缺修正 → restore
            # 插入循环（只比较带 data-order 的节点）找不到目标 → appendChild 沉底。
            "if(el.getAttribute('data-streaming')==='true'){"
            "var _pfo=parseFloat(el.getAttribute('data-order'));"
            "if(!isNaN(_pfo)){window.__pendingStreamFloors.push(Math.floor(_pfo));}"
            "}"
            "_saved.push({id:el.getAttribute('data-tool-call-id'),"
            "html:el.outerHTML,kind:'tool',"
            "streaming:el.getAttribute('data-streaming')||'',"
            "src:_root.id||''});"
            "el.remove();}"
            "});}"
            "}"
            "document.querySelectorAll('[data-tool-injected]').forEach(function(el){el.remove()});"
            f"updateContent({json.dumps(html_content).decode('utf-8')});"
            # 🐛 修复：只恢复流式进行中的块（data-streaming="true"）。
            # 已完成块已由 markdown 重新生成 + reorganizeContent 迁移到 #tool-content。
            # 恢复流式块时检查同 ID 是否已存在（避免与 reorganizeContent 迁移的块重复）。
            # [PERF] _saved 为空时跳过 restore，这是最常见场景（无活跃工具块）
            f"var _finishedSet={_finished_js};"
            f"if(_saved.length){{_tc=document.getElementById('{_target_id}');if(_tc){{"
            "_saved.forEach(function(b){"
            # 🐛 修复（编辑工具框"运行中→完成"中间消失）：restore 条件从
            # `b.streaming==='true'` 放宽为 `streaming 或未完成`——finish_tool_streaming
            # 注入的完成态预览块（data-streaming="false"）在 append_tool_result 之前
            # **不在 markdown 中**（_content_data 尚无结果块），若只恢复 streaming=true，
            # 该预览块 save 后不 restore → 全量渲染后被抹掉，直到 append_tool_result
            # 才重现。已完成（结果已 append_tool_result）的块才由 markdown 重新生成，
            # 无需 restore（且恢复会与 markdown 生成的块重复）。
            "var _isFinished=(_finishedSet.indexOf(b.id)!==-1);"
            "if(!_isFinished&&!document.querySelector('[data-tool-call-id=\"'+b.id+'\"]')){"
            "var _t=document.createElement('div');_t.innerHTML=b.html;"
            "var _bk=_t.firstElementChild;if(_bk){"
            "_bk.removeAttribute('data-tool-injected');"
            "_bk.setAttribute('data-restored','true');"
            # 🆕 F1：restore 恢复的运行中块（data-streaming="true"）直接 appendChild 沉底——
            # 不再按 data-order 插位。be57674d 方案 D 的按 data-order 插位逻辑本意是
            # 让"流式块恢复后保持交错顺序"，但运行中块 data-order 是调用时刻快照，
            # 与后续思考块补出的 data-order 尺度不一致 → 恢复插回时被排到思考块上方。
            # 运行中块语义为"当前最新活动"，dock 语义下应恒在最下面；data-order 属性
            # 仍保留（供 append_tool_result 完成态继承归位，不破坏"完成块归位"语义）。
            'var _odMatch=b.html.match(/data-order="([^"]*)"/);'
            "var _odVal=_odMatch?_odMatch[1]:null;"
            "if(_odVal){"
            "_bk.setAttribute('data-order',_odVal);"
            "}"
            "var _home=(b.src&&document.getElementById(b.src))||_tc;"
            "_home.appendChild(_bk);"
            "}}})"
            "}}"
            # 🐛 修复：save-restore 恢复块后工具区自动滚底
            "if(typeof _scrollToolContentToBottom==='function')_scrollToolContentToBottom();"
            "if(window._toolCompactMode){"
            "var _ts2=document.getElementById('tool-section');"
            "if(_ts2){_ts2.style.display=(_tc&&_tc.children.length>0)||window._todoCount?'':'none';_updateToolSectionHeader();}"
            "}"
            "})();"
        )

    def finish_streaming(self, keep_dock: bool = False):
        """流式结束收尾。

        Args:
            keep_dock: True 时保留坞态（简洁模式下工具区仍沉底）——流式文本可能
                先于工具结果结束（S1：dock 归位早于工具完成），此时不应立即归位，
                等最后一个工具完成时再由 append_tool_result 兜底归位。
        """
        self._streaming = False
        # [B1] 流式结束：差量缓存失效（尾部未闭合内容需全量渲染收尾），
        # 清空稳定区避免差量/全量混合导致重复段落。
        self._needs_full_render = True
        self._stable_html = ""
        self._stable_md_len = 0
        # [B3] 流式结束：递增渲染序号使在途线程池任务过期（避免旧流式 HTML
        # 晚到覆盖最终非流式渲染结果）；pending 积压清空。
        self._render_seq += 1
        self._render_pending = None
        # [B2] 流式结束：重置工具 DOM 脏标记。随后 _schedule_render 走非流式分支，
        # 该分支依据 _tool_dom_dirty/_restore_finished_ids 决定 save/restore 或裸更新；
        # 显式清零保证完成渲染后不再残留"脏"状态（防误走整页 save/restore 包装）。
        # 🐛 修复（编辑工具框消失）：keep_dock=True 时仍有活跃工具（S1：文本先于
        # 工具结果流式结束），此时**不能**清理脏标记——否则 _schedule_render 走
        # 非流式裸更新重建 #content-placeholder，把 JS 注入的编辑工具运行框抹掉，
        # 直到 append_tool_result 才重现（"运行中→完成"中间消失一阵子）。保留
        # dirty 使最终渲染走 save/restore 保护（_saved 为空时零开销）。
        if not keep_dock and not getattr(self, "_injected_pending_tools", None):
            self._tool_dom_dirty = False
        # 流式结束：坞态归位（简洁模式下工具区从底部回到顶部）
        # 🆕 F2（S1）：keep_dock=True 时保留坞态——流式文本先于工具结果结束是
        # 常见时序（工具执行耗时 > 文本流式），此时立即归位会让用户看到
        # "工具还在运行但工具区已回顶部"的跳动。归位推迟到最后一个工具完成时。
        if not keep_dock:
            self._sync_streaming_dock(False)
        # 🐛 FIX: 流式结束时清除 tool_md_cache，防止缓存过期导致
        # 后续非流式渲染拿到缺内容的旧 <tool> markdown，造成 tool-block
        # 在 reorganizeContent 中因不匹配而被清除或生成重复。
        if hasattr(self, "_tool_md_cache"):
            self._tool_md_cache.clear()
        # 🆕 Bug B 方案 D+：流式结束必须清除"流式语义缓存"的 HTML。
        # _cached_streaming_html 是流式渲染产物：thinking 被渲染成 .think-streaming
        # （无 data-block-key，reorganizeContent 查不到 posMap → getPos=1e9 沉底）。
        # 若 finish 的非流式分支直接复用它，就会在"坞态归位/折叠框从底部移到上部"的
        # 最终渲染中，把思考块与 save/restore 插入的工具块错位（"所有思考在前、
        # 所有工具在后"）。清除后强制以完成态重新渲染（think-compact/think-block
        # 带稳定 data-block-key），与加载历史会话的排序尺度一致。
        self._cached_streaming_html = None
        self._processed_md_hash = 0
        self._cached_raw_md_hash = 0
        # 重置思考文本流式标志，防止下一轮对话误判
        self._think_text_streaming_started = False
        self._reasoning_streaming_started = False
        # 流式结束：触发一次最终全量渲染，完成所有未完成的内容
        # 注意：不强制清除 _last_rendered_markdown —— 流式对话期间
        # think-streaming（展开）应保持，只有历史会话加载走非流式分支
        # 才会渲染为 think-block（折叠）。强制重渲染会把流式期间的
        # 展开态误转为折叠态，违背"流式展开 / 历史折叠"的产品预期。
        self._schedule_render(immediate=True)
        # 简洁模式：流式结束后自动折叠工具与思考区（收起为"工具与思考 · N 项"
        # 标题栏）。坞态归位 + 折叠由 MessageCard.finish_streaming 统一触发
        # （需 Python 端 _streaming/_has_active_tools 判据，viewer 侧无此状态，
        # 故不在此处调用）；非简洁模式保持流式结束后的展开态不变。

    def _auto_collapse_tool_section(self):
        """流式结束时自动折叠工具与思考区（仅简洁模式）

        在 dock 归位 + stop_streaming_anim 标完流式块后调用，收起为标题栏。
        调用方（MessageCard.finish_streaming / append_tool_result 兜底归位）
        已保证无活跃工具、非流式，故不做 DOM 流式块查询守卫——0ms 时序下
        最终渲染尚未落 DOM，陈旧的 data-streaming="true" 会误致跳过。
        非简洁模式保持展开态（与旧产品决策一致），直接 no-op。
        getattr 默认 True：stub viewer（测试桩）无该 property，视为简洁模式。
        """
        if not getattr(self, "_tool_compact_mode", True):
            return
        try:
            if self._is_js_ready and self.page():
                self.page().runJavaScript(
                    "(function(){"
                    "var _ts=document.getElementById('tool-section');"
                    "var _sep=document.getElementById('tool-separator');"
                    "if(_ts){"
                    "  if(typeof _beginToolSectionTransition==='function')_beginToolSectionTransition();"
                    "  _ts.setAttribute('data-collapsed','true');"
                    "  if(_sep)_sep.setAttribute('aria-expanded','false');"
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
        # 欢迎卡片（light 骨架）不进入坞态：坞态 CSS 会限死正文高度，
        # 欢迎卡片的长内容（会话列表/项目列表）会被截断在 330px。
        if self._light_skeleton:
            return
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
        # [B3] 清空渲染缓存：递增序号使在途线程池任务过期（避免旧内容被应用）
        self._render_seq += 1
        self._render_pending = None
        # [B1] 清空差量缓存：强制下次全量渲染（流式结束后的最终态）
        self._needs_full_render = True
        self._stable_html = ""
        self._stable_md_len = 0
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

        # Phase D：插件右键菜单项（target="message_card"）
        context = {
            "round_index": getattr(self, "_round_index", None),
            "message_index": getattr(self, "_message_index", None),
            "window_id": self._resolve_window_id(),
        }
        self._current_context_menu = menu  # 供 action_func 返回 False 时关闭菜单
        self._inject_plugin_context_actions(menu, context)

        try:
            menu.exec_(self.mapToGlobal(pos))
        finally:
            self._current_context_menu = None

    def _resolve_window_id(self):
        """沿父链查找窗口 window_id（注入插件菜单 context 用）"""
        parent = self.parent()
        while parent is not None:
            wid = getattr(parent, "_window_id", None)
            if wid:
                return wid
            parent = parent.parent()
        return None

    def _inject_plugin_context_actions(self, menu: QMenu, context: dict):
        """注入消息卡片右键菜单插件项（Phase D，target="message_card"）

        action_func 返回 False 表示"处理完成关菜单"——与现有菜单项行为对齐：
        action_func 由插件实现，返回 False 时此处自动关闭菜单（menu.close()）。
        """
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            actions = UIPluginRegistry.get_instance().get_context_actions("message_card")
        except Exception:
            return
        for info in actions:
            try:
                if info.separator_before:
                    menu.addSeparator()
                action = menu.addAction(info.label)
                enabled = True
                if info.enabled_func is not None:
                    try:
                        enabled = bool(info.enabled_func(context))
                    except Exception:
                        enabled = True
                action.setEnabled(enabled)
                action.triggered.connect(lambda checked=False, i=info: self._run_plugin_context_action(i, context))
            except Exception as e:
                logger.warning(f"[MessageCard] 插件菜单项 {info.action_id} 注入失败：{e}")

    def _run_plugin_context_action(self, info, context: dict):
        """执行插件菜单项：action_func(context)；返回 False → 关闭菜单（保持现有语义）"""
        try:
            close_menu = info.action_func(context) is False
        except Exception as e:
            logger.error(f"[MessageCard] 插件菜单项 {info.action_id} 执行失败：{e}")
            close_menu = True
        if close_menu:
            try:
                menu = self._current_context_menu
                if menu is not None:
                    menu.close()
            except Exception:
                pass

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

        优先复制页面选中文本（右键菜单标准行为），无选中时降级复制全文。

        🐛 修复：使用 get_plain_text() 替代直接读 _markdown_text，
        因为 _cleanup_render_cache 会将 _markdown_text 清空。
        get_plain_text() 会通过 _lazy_markdown_cb 或父 MessageCard 自动兜底。
        """
        # 优先复制选中文本：QWebEnginePage.selectedText() 返回 DOM 选区，
        # 无选中时返回空字符串；\u2029 为 WebEngine 块级换行分隔符，规范化为 \n。
        try:
            selected = self.page().selectedText()
            if selected:
                text = selected.replace("\u2029", "\n")
            else:
                text = self.get_plain_text()
        except Exception:
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

    def _compose_with_solid_bg(self, source: "QPixmap", width: int, height: int, dpr: float = 1.0) -> "QPixmap":
        """在 QPixmap 上填充实心卡片背景，再合成 source（dpr>1 时输出高清物理像素）

        Args:
            source:  从 widget.grab() 拿到的 pixmap（可能含透明区）
            width:   目标逻辑宽度
            height:  目标逻辑高度
            dpr:     输出 devicePixelRatio（物理像素 = 逻辑 × dpr；<1 钳制为 1）

        Returns:
            填充实心卡片背景 + 绘制 source 的合成 pixmap
        """
        from PyQt5.QtGui import QPainter, QPixmap

        if width <= 0 or height <= 0:
            return source
        dpr = max(1.0, float(dpr))
        result = QPixmap(round(width * dpr), round(height * dpr))
        result.setDevicePixelRatio(dpr)
        result.fill(self._get_card_bg_color())
        if not source.isNull():
            painter = QPainter(result)
            painter.drawPixmap(0, 0, source)  # source 与 result 同 DPR 时按物理像素 1:1 绘制
            painter.end()
        return result

    def _grab_render_widget(self) -> "QPixmap":
        """抓取 WebEngine 渲染内容（GPU 合成环境下 QWebEngineView.grab() 拿不到内容）

        QWebEngineView 的内容由 Chromium 进程远程合成，QWidget::grab 抓自身
        往往得到空白/纯背景（Qt 已知限制）；实际渲染发生在内部 RenderWidget
        （focusProxy）上，必须对它 grab。拿不到 focusProxy 或结果为空时回退
        QWidget 原生 grab（软件渲染环境该路径可用）。
        """
        target = self.focusProxy()
        if target is not None:
            try:
                pix = target.grab()
                if pix is not None and not pix.isNull() and pix.width() > 0 and pix.height() > 0:
                    return pix
            except Exception:
                logger.warning("[export] RenderWidget grab 为空，回退 QWidget grab")
        return super().grab()

    def _capture_looks_healthy(self, pix: "QPixmap") -> bool:
        """粗采样检查抓取结果：整体内容占比过低或出现大面积连续空白块视为合成未完成

        zoom 3x 撑大控件后 WebEngine 走异步合成，弱 GPU/大纹理时定时等待可能
        不够——合成器未画完的 tile 抓出来是纯背景色。粗采样 ≤64×64 点毫秒级。

        两级判据（部分渲染的"窄条内容"能骗过整体占比，拦不住连续空块）：
        1. 整体非背景采样点占比 ≥ 1%
        2. 8×8 分块中"整块皆背景"的空块占比 < 50%（正常内容散布多数块，
           部分渲染会出现大段连续空白）
        """
        from PyQt5.QtGui import QImage

        img = pix.toImage()
        if img.isNull():
            return False
        bg = self._get_card_bg_color()
        w, h = img.width(), img.height()
        step = max(4, min(w, h) // 64)
        grid_n = 64  # 采样网格 64×64 点
        cols = max(1, (w + step - 1) // step)
        if cols > grid_n:
            cols = grid_n
        rows = max(1, (h + step - 1) // step)
        if rows > grid_n:
            rows = grid_n
        total = 0
        diff = 0
        block_size = 8  # 每 8×8 采样点为一块
        block_empty = [0] * ((grid_n // block_size + 1) * (grid_n // block_size + 1))
        yi = 0
        y = 0
        while y < h:
            x = 0
            xi = 0
            while x < w:
                c = img.pixelColor(x, y)
                total += 1
                is_bg = abs(c.red() - bg.red()) + abs(c.green() - bg.green()) + abs(c.blue() - bg.blue()) <= 24
                if not is_bg:
                    diff += 1
                else:
                    bi = (yi // block_size) * (grid_n // block_size + 1) + (xi // block_size)
                    block_empty[bi] += 1
                x += step
                xi += 1
            y += step
            yi += 1
        if total == 0:
            return False
        if (diff / total) < 0.01:
            return False
        # 分块：块内采样点全部为背景 → 空块
        bw = grid_n // block_size + 1
        empty_blocks = 0
        total_blocks = 0
        for r in range(min(rows, grid_n) // block_size + (1 if min(rows, grid_n) % block_size else 0)):
            for c in range(min(cols, grid_n) // block_size + (1 if min(cols, grid_n) % block_size else 0)):
                total_blocks += 1
                if block_empty[r * bw + c] == block_size * block_size:
                    empty_blocks += 1
        if total_blocks == 0:
            return False
        return (empty_blocks / total_blocks) < 0.5

    def _wait_render_stable(self, deadline_ms: int = 1200) -> None:
        """轮询等待 WebEngine 布局/合成稳定：body.scrollHeight 连续两次读数一致即放行

        替代固定 250ms 定时——大尺寸重排（zoom 3x 撑到 4K+）时固定等待可能不足，
        或小页面白白等满。最长 deadline_ms 兜底，卡死不可能（_run_js_sync 有超时）。
        """
        import json as json_mod

        from PyQt5.QtCore import QElapsedTimer, QEventLoop, QTimer

        loop = QEventLoop()
        elapsed = QElapsedTimer()
        elapsed.start()
        state = {"last": None, "stable": 0}

        def _tick():
            try:
                raw = self._run_js_sync("JSON.stringify({sh: document.body ? document.body.scrollHeight : 0})")
                cur = json_mod.loads(raw).get("sh", 0) if raw else 0
            except Exception:
                cur = 0
            if cur == state["last"]:
                state["stable"] += 1
            else:
                state["stable"] = 0
            state["last"] = cur
            # 至少 240ms（2 tick）且读数连续两次一致 → 布局稳定
            if state["stable"] >= 2 and elapsed.elapsed() >= 240:
                loop.quit()
                return
            if elapsed.elapsed() >= deadline_ms:
                loop.quit()
                return

        timer = QTimer()
        timer.setInterval(120)
        timer.timeout.connect(_tick)
        timer.start()
        try:
            loop.exec_()
        finally:
            timer.stop()

    def _capture_full_content_1x(self) -> "QPixmap":
        """1x 兜底抓取（旧逻辑完整保留）：解除 max-height 撑高后单次 grab + 实心合成"""
        import json as json_mod

        from PyQt5.QtCore import QEventLoop, QPoint, QRect, QTimer
        from PyQt5.QtWidgets import QApplication

        view_w = self.width()
        cur_h = self.height()

        dims_raw = self._run_js_sync("JSON.stringify({sh: document.body.scrollHeight})")
        if not dims_raw:
            return self._compose_with_solid_bg(self._grab_render_widget(), view_w, cur_h)

        try:
            scroll_h = json_mod.loads(dims_raw).get("sh", 0)
        except Exception:
            scroll_h = 0

        if scroll_h <= cur_h or scroll_h <= 0:
            grabbed = self._grab_render_widget()
            return self._compose_with_solid_bg(
                grabbed,
                view_w,
                max(cur_h, grabbed.height() if not grabbed.isNull() else cur_h),
            )

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
        QApplication.processEvents()

        self._run_js_sync("window.scrollTo(0, 0);")

        stable_loop = QEventLoop()
        QTimer.singleShot(200, stable_loop.quit)
        stable_loop.exec_()

        full_pix = self._grab_render_widget()

        final_w = full_pix.width() if not full_pix.isNull() else view_w
        final_h = max(target_h, full_pix.height() if not full_pix.isNull() else 0)
        result = self._compose_with_solid_bg(full_pix, final_w, final_h)

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
            return self._grab_render_widget()
        return result

    def _capture_full_content(self) -> "QPixmap":
        """截取消息的完整内容为一张高清大图（3x 物理像素 + 实心背景合成）

        策略：临时 setZoomFactor(3) 并把控件尺寸×3（布局视口 CSS 宽度 = w*3/3 = w
        不变，排版不重排，内容以 3x 物理像素渲染），grab 后按实际物理/逻辑比设置
        devicePixelRatio 还原逻辑尺寸。导出 PNG 保存物理像素，高分屏/放大查看均清晰。

        长消息：临时解除 body max-height 并撑高到完整内容高度后单次 grab。
        稳健性：渲染稳定用 scrollHeight 轮询（非固定定时）；grab 后做像素健康
        检查——3x 结果大面积空白/黑块（合成未完成或视口重排异常）时自动回退
        1x 完整路径，保证导出功能永不失败。
        """
        import json as json_mod

        from PyQt5.QtCore import QPoint, QRect
        from PyQt5.QtWidgets import QApplication

        _SCALE = 3.0

        view_w = self.width()
        cur_h = self.height()
        if view_w <= 0:
            return self._compose_with_solid_bg(self._grab_render_widget(), max(1, view_w), cur_h)

        # 1. 获取完整内容高度（CSS 逻辑像素，与 zoom 无关）
        dims_raw = self._run_js_sync("JSON.stringify({sh: document.body.scrollHeight})")
        scroll_h = 0
        if dims_raw:
            try:
                scroll_h = json_mod.loads(dims_raw).get("sh", 0)
            except Exception:
                scroll_h = 0
        if scroll_h <= 0:
            # 拿不到高度 → 按当前视口高度走 zoom 高清路径
            scroll_h = cur_h

        # 2. 目标逻辑高度：内容超出视图时展开全部
        is_long = scroll_h > cur_h
        target_logical_h = (scroll_h + 20) if is_long else cur_h

        orig_zoom = self.zoomFactor()
        orig_size = self.size()
        old_styles = None
        try:
            # 3. 长消息：临时解除 body max-height
            if is_long:
                old_styles = self._run_js_sync("""
                    var s = document.body.style;
                    JSON.stringify({maxHeight: s.maxHeight, overflowY: s.overflowY})
                """)
                self._run_js_sync("""
                    document.body.style.maxHeight = 'none';
                    document.body.style.overflowY = 'hidden';
                """)

            # 4. zoom 3x + 控件尺寸×3：内容物理渲染 3x，布局视口 CSS 宽度不变
            self.setZoomFactor(_SCALE)
            self.setFixedSize(round(view_w * _SCALE), round(target_logical_h * _SCALE))
            self.update()
            # ★ 强制布局：让 setFixedSize 真的撑大 widget
            QApplication.processEvents()
            self._run_js_sync("window.scrollTo(0, 0);")

            # ★ 轮询等待 zoom 重排 + 合成稳定（大纹理时固定 250ms 不够）
            self._wait_render_stable(deadline_ms=1200)

            # 5. 显式 grab 整个目标区域，按实际物理/逻辑比还原逻辑尺寸
            full_pix = self._grab_render_widget()
            if full_pix.isNull() or full_pix.width() <= 0:
                logger.warning("[export] zoom 3x 抓到空图，回退 1x")
            elif not self._capture_looks_healthy(full_pix):
                logger.warning("[export] zoom 3x 抓取疑似未完成合成（大面积空白），回退 1x")
            else:
                dpr = full_pix.width() / view_w  # 物理/逻辑（= 窗口 DPR × zoom）
                final_h = max(target_logical_h, round(full_pix.height() / dpr))
                return self._compose_with_solid_bg(full_pix, view_w, final_h, dpr=dpr)
        except Exception:
            logger.exception("[export] zoom 3x 抓取失败，回退 1x")
        finally:
            # 6. 恢复 zoom / 尺寸 / 样式
            try:
                self.setZoomFactor(orig_zoom)
                self.setFixedSize(orig_size)
            except Exception:
                pass
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

        # 7. 回退：1x 完整路径（健康检查通过才返回，异常仍有裸 grab 兜底）
        return self._capture_full_content_1x()

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
                    vbar.setValue(vbar.value() - wheel_delta_to_px(delta))
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
        # 🔧 内存修复：从全局单例过滤器注销，防止注册表持有对已销毁
        # CodeWebViewer 实例的引用，导致 GC 无法回收且事件循环误调用已释放对象
        _dialog_event_filter.unregister(self)

        # [B3] 视口销毁：递增渲染序号使在途线程池任务过期（weakref 判活兜底下，
        # 序号守卫提供第二道防线，防止旧任务结果应用到已释放的 DOM）。
        self._render_seq += 1
        self._render_pending = None
        self._render_inflight = False

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

        # 清理页面：先停加载并卸载到空白页（比 setHtml("") 更轻，避免 WebEngine 异步导航竞态）
        try:
            self.stop()  # 停止页面加载
            from PyQt5.QtCore import QUrl

            self.setUrl(QUrl("about:blank"))  # 卸载，比 setHtml("") 更轻
        except RuntimeError:
            pass
        # 幂等守卫：二次 cleanup 不重复 deleteLater
        if getattr(self, "_page", None) is not None:
            self._page.deleteLater()
            self._page = None
        self.setPage(None)  # 断开 view→page，避免 view 析构再引用已删 page

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

        # [B4-强回收] 防悬挂：清理时清零 renderer PID（进程可能已随页面销毁退出）
        self._renderer_pid = 0

    def deleteLater(self):
        self.cleanup()
        super().deleteLater()


class PlainTextViewer(QWidget):
    contentHeightChanged = pyqtSignal(int)

    # 用户消息卡片最大高度（px）：超过此高度启用 QTextEdit 内部滚动条
    # 约可容纳 13 行 14px 文本，平衡阅读完整性与卡片视觉占位
    #
    # ⚠️ 用户明确要求保持 300（2026-08-30）：用户气泡不应因正文变长而撑开整屏，
    # 长内容在气泡内部滚动是**预期行为**，不是缺陷。不要为了「减少滚动区域」
    # 擅自抬高它 —— 本轮滚动体验改动只针对 assistant 卡片与自动滚底守卫。
    MAX_HEIGHT = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        # 气泡宽度自适应：未换行内容理想宽度（ChatGPT 式紧凑气泡），
        # 由 MessageCard.sync_width 按容器宽度注入上限
        # PyQt5 未导出 QWIDGETSIZE_MAX，16777215 即其值（未 sync 前的不限制初始态）
        self._width_cap = 16777215
        # [PERF] “超高”单调缓存：全文档实测高度撞上 MAX_HEIGHT 上限时的最大确认宽度（0=未确认）。
        # 文档高度在某宽度撞上限后，宽度变窄只会行数更多、高度更高，故后续宽度 ≤ 该值时
        # 可直接 O(1) 判定 (cap, MAX_HEIGHT)，跳过全文档重排——消除超大用户消息的 resize 卡顿。
        # 文本替换（set_text）使缓存失效；append_chunk 只增不减，无需失效。
        self._tall_cap = 0
        self._init_ui()
        # 性能优化：添加 resize 防抖定时器
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(50)  # 50ms 防抖
        self._resize_debounce_timer.timeout.connect(self._do_resize_update)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        # 底部 2：正文与时间行间距紧凑（时间行在卡片 footer，不在 viewer 内）
        layout.setContentsMargins(8, 6, 8, 2)
        layout.setSpacing(0)

        self.text_edit = QTextEdit(self)
        self.text_edit.setReadOnly(True)
        self.text_edit.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.text_edit.setFrameShape(QTextEdit.NoFrame)
        self.text_edit.setContextMenuPolicy(Qt.CustomContextMenu)
        self.text_edit.customContextMenuRequested.connect(self._show_context_menu)
        # 显式声明：超出可视区域时自动显示垂直滚动条
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 气泡内禁横向滚动：超宽行强制软换行（达上限自动折行，不出横向滚动条）
        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_edit.setLineWrapMode(QTextEdit.WidgetWidth)
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
        # [PERF] 字体/字号可能随主题设置变化 → 行数结论失效，清除“超高”缓存待重测
        self._tall_cap = 0

    def append_chunk(self, text: str):
        self._text += text
        self.text_edit.setPlainText(self._text)
        # 设置文档宽度以确保正确计算换行
        vp_width = self.text_edit.viewport().width()
        if vp_width > 0:
            self.text_edit.document().setTextWidth(vp_width)
        self._schedule_update_height()

    def finish_streaming(self, keep_dock: bool = False):
        """流式结束收尾。

        🆕 F4：与 CodeWebViewer.finish_streaming 保持相同签名——MessageCard.
        finish_streaming 统一以 keep_dock=self._has_active_tools() 调用两个 Viewer。
        PlainTextViewer 无 dock 概念（用户卡片无工具与思考折叠框），忽略该参数。
        """
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
        # [PERF] 文本被整体替换（可能变短）→ “超高”结论不再必然成立，失效单调缓存
        self._tall_cap = 0
        self.text_edit.setPlainText(text)
        # 设置文档宽度以确保正确计算换行
        vp_width = self.text_edit.viewport().width()
        if vp_width > 0:
            self.text_edit.document().setTextWidth(vp_width)
        self._schedule_update_height()

    def set_width_cap(self, cap: int):
        """设置气泡最大宽度（由 MessageCard.sync_width 按容器宽度注入）"""
        cap = max(60, int(cap))
        if cap != self._width_cap:
            self._width_cap = cap
            self._schedule_update_height()

    def _definitely_tall_chars(self) -> int:
        """[PERF] “必然超高”字符阈值（O(1) 计算）。

        内容达到该字符数时，即使按最乐观排版估算——窗口宽至 4000px、字符窄至
        0.35×字号——渲染高度也必然撞上 MAX_HEIGHT 上限。超过阈值即可跳过全文档
        布局直接判定 (cap, MAX_HEIGHT)。误判方向只会让气泡偏高留白（内部滚动条仍可见
        全文），不会裁剪文字。
        """
        fs = self.text_edit.font().pixelSize()
        if fs <= 0:
            fs = 14
        # 填满 MAX_HEIGHT 所需行数（行高 1.5×字号）× 4000px 宽下每行最多容纳字符数
        return int((self.MAX_HEIGHT / (1.5 * fs)) * (4000.0 / (0.35 * fs)))

    def _measure_document(self) -> QTextDocument:
        """新建独立测量文档（同步布局，字体/边距与 text_edit 对齐）。

        text_edit 的共享文档会被 QTextEdit 钉在 viewport 宽，且 QTextDocument
        布局是 layoutTimer 异步的——setTextWidth(w) 后立即读 size() 拿到旧宽
        排版缓存，短消息被误判"多行"钉死 MAX_HEIGHT（气泡下方大片空白根因）。
        独立新文档无历史布局状态，size() 同步正确。调用方用完交由 GC 释放。
        """
        src = self.text_edit.document()
        doc = QTextDocument()
        doc.setDefaultFont(src.defaultFont())
        doc.setDocumentMargin(src.documentMargin())
        return doc

    def _update_height(self):
        """宽度自适应 + 高度重算：气泡按未换行理想宽度收缩，不占满整行"""
        # [PERF] 超大文本快速路径：跳过全文档布局，O(1) 判定 (cap, MAX_HEIGHT)。
        # 依据一（单调缓存）：曾实测高度撞上限的宽度 _tall_cap，更窄只会更高；
        # 依据二（字符阈值）：字符数达“必然超高”阈值，最乐观排版也撞上限。
        # 12 万字符实测：每步 2 次全文档布局 ~1.4s → O(1)，消除 resize 卡死。
        cap = self._width_cap
        if cap < 100000:  # 排除 16777215 初始未限宽态（几何不代表真实窗口）
            tall_cached = bool(self._tall_cap) and cap <= self._tall_cap
            if tall_cached or len(self._text) >= self._definitely_tall_chars():
                if cap > self._tall_cap:
                    self._tall_cap = cap
                if self.maximumWidth() != cap:
                    self.setMaximumWidth(cap)
                if self.width() != cap or self.height() != self.MAX_HEIGHT:
                    self.setFixedSize(cap, self.MAX_HEIGHT)
                    self.contentHeightChanged.emit(self.MAX_HEIGHT)
                return

        # 测量用独立同步文档：共享文档被 QTextEdit 钉在 viewport 宽且布局异步，
        # setTextWidth(w) 后立即读 size() 拿到旧宽缓存 → 短消息误判"多行"
        # 走 WIDE 分支钉死 MAX_HEIGHT（气泡下方大片空白的根因）
        doc = self._measure_document()
        doc.setPlainText(self._text)
        fm = QFontMetrics(self.text_edit.font())

        # ── 宽度自适应（ChatGPT 式）──
        # 先测内容在 cap 宽下的总高度：仍超过约 3 行 → 内容多，用满上限拉宽；
        # 短消息（≤ 2-3 行）才按最长单行收缩，避免窄气泡被迫多行换行
        doc.setTextWidth(self._width_cap)
        if doc.size().height() > 3.0 * fm.lineSpacing():
            bubble_w = self._width_cap
        else:
            # 短消息：按最长单行收缩。
            # 用 QTextLayout 实测行渲染宽度（含 fallback 字体/字距），而非 QFontMetrics：
            # 特殊字符（emoji/全角标点等）fallback 渲染实际宽度常大于 QFontMetrics
            # 测量值，旧实现 +32px 余量被 viewer 布局边距(16) + documentMargin(8)
            # 抵消后仅剩 8px，测量一旦偏小即出现文字溢出气泡右缘。
            longest = 0.0
            block = doc.begin()
            while block.isValid():
                layout = block.layout()
                if layout is not None:
                    for i in range(layout.lineCount()):
                        longest = max(longest, layout.lineAt(i).naturalTextWidth())
                block = block.next()
            # 可用文字宽 = 气泡宽 - 布局边距(8*2) - documentMargin(4*2)，
            # 故最长行 + 40（16 边距 + 8 docMargin + 16 视觉余量）
            bubble_w = max(80, min(int(math.ceil(longest)) + 40, self._width_cap))
        if self.maximumWidth() != bubble_w:
            self.setMaximumWidth(bubble_w)

        # 高度按气泡实际宽计算（viewport 在气泡收紧瞬间可能仍是旧值，不可信）。
        # 测量宽必须对齐真实渲染视口：text_edit 宽 = bubble_w - 16(布局边距 8×2)，
        # QTextDocument 渲染内容宽再减 docMargin 4×2。旧实现 setTextWidth(bubble_w)
        # 比渲染宽大 16px → "测量不溢出、渲染溢出"错位 → 滚动条出现 → 视口再窄
        # 6px → 内容重折行 → 高度变化 → 滚动条消失 → 宽度反馈环（气泡滚动条
        # 反复出现/消失抖动）。对齐无滚动条渲染宽后两态各自稳定：无溢出时测量=
        # 渲染；溢出时滚动条只会让渲染更窄更高，方向单调不回摆。
        doc.setTextWidth(bubble_w - 16)
        h = int(math.ceil(doc.size().height())) + 12  # 上下边距

        # 🛡️ 短消息收缩分支测出超高 = longest 测量伪信号（如字体 fallback 未就绪时
        # naturalTextWidth 异常偏小 → bubble_w 收到 ~80 → tiny 宽度下短文本折出
        # 十几行 → h 必然撞 MAX_HEIGHT）。回退全宽重测一次自愈，避免：
        # 1) 气泡真的收缩成 80px 孤条；2) 下方 _tall_cap 把该 cap 记为"确认超高"，
        # 之后所有 ≤cap 的宽度永久走 O(1) 快速路径 → 2 行短消息被锁死
        # (cap, MAX_HEIGHT)，气泡全宽 300 高全是空白（实测截图症状）。
        if h > self.MAX_HEIGHT and bubble_w < self._width_cap:
            bubble_w = self._width_cap
            if self.maximumWidth() != bubble_w:
                self.setMaximumWidth(bubble_w)
            doc.setTextWidth(bubble_w - 16)
            h = int(math.ceil(doc.size().height())) + 12

        # 限制最大高度：内容超出 MAX_HEIGHT 后由 QTextEdit 内部滚动条处理滚动
        h = max(40, min(h, self.MAX_HEIGHT))

        # [PERF] 更新“超高”单调缓存：撞上限 → 记录确认宽度（取 max 保留最宽确认点），
        # 后续更窄宽度走 O(1) 快速路径；未撞上限不更新（更宽时结论仍可能对更窄宽度有效）。
        # 🛡️ 仅在 bubble_w 用满上限（bubble_w >= cap，真·内容超高）时记录：
        # 短消息收缩分支的撞限是 tiny 宽度测量伪信号，一旦记录，后续宽度
        # 全部被 O(1) 快速路径锁死 (cap, MAX_HEIGHT)（见上方回退重测注释）。
        if h >= self.MAX_HEIGHT and self._width_cap < 100000 and bubble_w >= self._width_cap:
            self._tall_cap = max(self._tall_cap, self._width_cap)

        # ⚠️ 必须 setFixedSize：仅设 maximumWidth 时布局仍按 QTextEdit 的
        # 默认 sizeHint(272px) 分配宽度，气泡实际展不开（AlignRight 下尤甚）
        if self.width() != bubble_w or self.height() != h:
            self.setFixedSize(bubble_w, h)
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
        self._tall_cap = 0

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
    welcomeModeChanged = pyqtSignal(str)  # 欢迎卡片模式切换（sessions / projects / changelog）
    saveChartPngRequested = pyqtSignal(str, str)  # (name_b64, png_b64) — 图表 PNG 导出（内部处理保存，不透传）

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
        # 本轮对话是否已完成流式输出。用于区分"本轮已结束的消息"与
        # "从磁盘加载的历史会话"——两者 _streaming 都是 False，但产品诉求
        # 是前者工具区保持展开，后者折叠（避免长会话加载时信息过载）。
        # 缺失此标志时，本轮消息在虚拟滚动回收重建后会被误判为历史而突然折叠。
        self._streaming_finished = False
        self._retrying = False  # 重试模式标志
        # 任务列表快照（卡片底部内嵌 todo 区数据）：viewer 未创建/JS 未就绪时
        # 暂存，viewer 就绪后补推；骨架重载后据此恢复。
        self._todos_snapshot: Optional[list] = None
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
        # M1 性能缓存：流式脉动渐变/调色板/裁剪路径模板（相位无关对象，
        # paintEvent 内仅改色/坐标，避免每帧 new 数十个临时对象）。
        self._rainbow_normal = _RAINBOW_NORMAL  # 模块级共享，0 个新 QColor
        self._rainbow_retry = _RAINBOW_RETRY
        self._grad_main, self._grad_inner, self._grad_glow = (QLinearGradient(0, 0, 1, 1) for _ in range(3))
        self._clip_inner = self._clip_outer = self._clip_inner_edge = self._clip_border = QPainterPath()
        self._clip_inner_border = self._clip_shimmer = self._clip_top = self._clip_glow_region = (
            self._clip_border_region
        ) = QPainterPath()
        self._clip_w = self._clip_h = -1
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
        # [PERF] preview 期间 viewer height 目标值累积。_apply_viewer_height 在
        # preview 模式只写此字段，不真正 setFixedHeight（避免 Chromium 级联
        # relayout）。set_resize_preview_mode(False) 退出时一次性应用。
        self._pending_viewer_height: Optional[int] = None
        # WebEngine 上下文恢复标志
        self._webengine_needs_restore = False
        # 懒渲染标志：未进入可视区域前不创建QWebEngine
        self._lazy_rendered = False
        # 标记：内容刚加载到viewer，首次heightChanged后滚动并清除
        self._content_just_loaded = False
        self._finished_streaming_ids: set = set()  # 防止 streaming 状态回退
        # 欢迎卡片模式数据（set_welcome_content 时填充；切换 mode 不重建 QWebEngineView）
        self._welcome_mode: str = ""
        # 首次渲染时固定的问候语，软刷新（其他标签页会话变更广播）复用，
        # 避免欢迎卡片内容无谓跳变（仅会话列表应静默更新）。
        self._welcome_greeting: str = ""
        self._welcome_recent: list = []
        self._welcome_top: list = []
        self._welcome_mode_tabs: Optional["SegmentedWidget"] = None
        self._pending_welcome_md: Optional[str] = None  # viewer 懒渲染前的等待内容
        # 窗口上下文提供者（多窗口隔离）：欢迎卡片渲染插件 tab 时调用，注入
        # 当前窗口的 project_root / project_name / window_id，避免插件回读全局
        # 状态导致多标签页内容串项目（create_welcome_card 传入 window._build_ui_context）
        self._welcome_ctx_provider: Optional[Callable[[], Dict[str, Any]]] = None
        # changelog 异步加载：单实例持有 fetcher + 已加载 releases
        self._changelog_fetcher: Optional["_ChangelogFetcher"] = None
        self._changelog_releases: list = []
        # 工具参数首次到达跟踪：每个 tool_call_id 第一次 update_tool_streaming 时
        # 触发"标记当前思考块为完成"，避免 reasoning→tool_call 切换时思考块残留"思考中"
        self._tool_args_first_seen_ids: set = set()
        # 🆕 Bug B（顺序错乱修复）：工具结果插入锚点 + 启动序号。
        # 同一卡片内"思考/工具/正文"必须按实际流式到达顺序交错，
        # 而不是思考恒顶部、工具恒底部。锚点 = 工具调用发生时 _content_data 的长度，
        # append_tool_result 时 insert(锚点) 而非 append，工具结果插回调用发生的位置。
        self._tool_insert_anchors: Dict[str, int] = {}  # tool_call_id → 工具调用时流末尾位置
        # 🆕 Bug B 方案 F（数据层稳定锚点）：_tool_insert_anchors 的 int 索引在
        # 其他工具结果插入 / 思考块追加后**失效**（列表偏移），导致 finish 完整重渲染
        # 时 _content_data 顺序错乱（"思考在前、工具在后"）。改为记录**块引用**：
        # 引用在列表增删中保持稳定，index(ref)+1 恒等于"工具调用时刻的逻辑末尾"。
        self._tool_anchor_refs: Dict[str, Any] = {}  # tool_call_id → 调用时 _content_data[-1] 块引用
        self._tool_call_order: Dict[str, int] = {}  # tool_call_id → 递增启动序号（同锚点工具按调用序）
        # 🆕 Bug B：当前活动思考块。append_reasoning 只追加到它（避免合并进已完成的
        # 旧思考块导致多轮思考堆积）；工具调用/新块开始时置 None / 覆盖。
        self._active_thinking_block: Optional[dict] = None
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
            if self.role == "user":
                # 简洁气泡：无胶囊背景的弱化小字
                self._ts_label.setStyleSheet(
                    f"{get_font_family_css()} font-size: {scale_font_size(11)}px; color: {self._theme['muted']};"
                )
            else:
                self._ts_label.setStyleSheet(
                    f"""
                    QLabel {{
                        {get_font_family_css()} font-size: {scale_font_size(11)}px;
                        color: {self._theme["muted"]};
                        background: {Colors.CONTENT_BG};
                        border: 1px solid {Colors.BORDER};
                        border-radius: 9px;
                        padding: 2px 8px;
                    }}
                    """
                )
        # 刷新 viewer 主题（注入 CSS 变量 + 失效实例渲染缓存）
        # ⚠️ 顺序必须在 _refresh_viewer_font() 之前：主题变化时先让
        # refresh_theme 清掉 _cached_streaming_html 等实例缓存并注入新 CSS
        # 变量，随后 _refresh_viewer_font 触发的重渲染才会使用新主题 HTML。
        if hasattr(self, "viewer") and self.viewer and hasattr(self.viewer, "refresh_theme"):
            self.viewer.refresh_theme()
        # 刷新富文本视图字体并触发重渲染（缓存已在 refresh_theme 中失效）
        if hasattr(self, "viewer") and self.viewer and hasattr(self.viewer, "_refresh_viewer_font"):
            self.viewer._refresh_viewer_font()

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
                    background: {Colors.CONTENT_BG};
                    border: 1px solid {Colors.BORDER};
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

        # 全减模式：hover 操作组（复制/差异对比），卡片 hover 时浮现（与 user 气泡一致）。
        # 固定高度占位：按钮显隐切换时 footer 高度不变，卡片不跳动。
        hover_btns = QWidget(self)
        self._assistant_action_btns = hover_btns
        hb = QHBoxLayout(hover_btns)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(2)
        for ic, tp, cb in [
            (get_icon("复制"), "复制", lambda: self.actionRequested.emit(self.get_plain_text(), "copy")),
        ]:
            b = TransparentToolButton(ic, self)
            b.setToolTip(tp)
            b.clicked.connect(cb)
            b.setFixedSize(20, 20)  # 弱化处理：比原顶部按钮 32px 更小
            install_hover_tooltip(b, delay_ms=200)
            hb.addWidget(b)
        hover_btns.setFixedHeight(20)
        hover_btns.setVisible(False)  # hover 浮现，保持卡片简洁
        layout.addWidget(hover_btns)

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
            try:
                self._footer_elapsed_label.setText(f"⏱ {elapsed:.0f}s")
                self._footer_elapsed_label.setVisible(True)
            except RuntimeError:
                # 🛡️ 防御：footer label 可能已被 C++ 侧销毁（deleteLater 排队中），
                # 访问已删除 QLabel 会抛 wrapped C/C++ object ... has been deleted。
                # 静默忽略（项目既有风格参考 _safe_report_height / L2465 先例）。
                pass
        # Token
        if token_usage is not None and self._footer_tokens_label:
            total = token_usage.get("total", 0)
            if total >= 1000:
                text = f"{total / 1000:.1f}K tokens"
            else:
                text = f"{total} tokens"
            try:
                self._footer_tokens_label.setText(text)
                self._footer_tokens_label.setVisible(True)
            except RuntimeError:
                # 🛡️ 同上：token label 可能已被 C++ 侧销毁，静默忽略。
                pass
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
        try:
            has_tokens = bool(self._footer_tokens_label and self._footer_tokens_label.text())
            has_elapsed = bool(self._footer_elapsed_label and self._footer_elapsed_label.text())
            has_model = bool(self._footer_model_label and self._footer_model_label.text())
            if self._footer_sep1:
                self._footer_sep1.setVisible(has_tokens and has_elapsed)
            if self._footer_sep2:
                self._footer_sep2.setVisible(has_elapsed and has_model)
        except RuntimeError:
            # 🛡️ 防御：footer label / separator 可能已被 C++ 侧销毁（deleteLater 排队中），
            # 访问已删除 QLabel 会抛 wrapped C/C++ object ... has been deleted。静默忽略。
            pass

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
        # [V1] 可见性门控：隐藏 tab 跳过 label setText（空转），
        # elapsed 基于 _elapsed_start_time 绝对时间戳计算，恢复后数值依然准确。
        if not self.isVisible():
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

    # ========== 欢迎卡片 mode 切换（PyQt segmented tabs）==========
    _WELCOME_MODE_ITEMS = [
        ("sessions", "💬 会话"),
        ("changelog", "📜 更新"),
    ]

    def _build_welcome_mode_tabs(self, top_layout):
        """在卡片标题栏右上角构建 segmented tabs（welcome 角色专属）"""
        seg = SegmentedWidget(self)
        for i, (key, label) in enumerate(self._WELCOME_MODE_ITEMS):
            seg.insertItem(i, key, label, onClick=lambda checked=False, k=key: self._on_welcome_mode_tab_clicked(k))
        # 插件注册的欢迎 tab 动态追加（系统项之后）
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            for key, info in UIPluginRegistry.get_instance().get_welcome_tabs().items():
                seg.addItem(
                    key,
                    info.label,
                    onClick=lambda checked=False, k=key: self._on_welcome_mode_tab_clicked(k),
                )
        except Exception:
            pass
        self._welcome_mode_tabs = seg
        top_layout.addWidget(seg)
        top_layout.addStretch()
        # 字号适配：SegmentedItem 内部写死 setFont(self, 14)，不读当前 delta，
        # 必须在此按当前字号缩放一次，否则新建/重建欢迎卡片时 tab 字体恒为 14px。
        self._apply_welcome_tabs_font()

    def _apply_welcome_tabs_font(self):
        """欢迎卡片 segmented tabs 适配系统字号

        SegmentedItem._postInit() 硬 setFont(self, 14)，qfluentwidgets 原组件不感知
        DriFox 的 font_size delta；此处按当前 delta 缩放覆盖，保证 tab 字体随
        系统字号变化（首次构建 + _apply_runtime_ui_settings 字体块都会调用）。
        """
        if self._welcome_mode_tabs is None:
            return
        fs = scale_font_size(14)
        ff = _get_global_font()
        for item in self._welcome_mode_tabs.items.values():
            font = item.font()
            font.setFamily(ff)
            font.setPixelSize(fs)
            item.setFont(font)

    def _on_welcome_mode_tab_clicked(self, mode: str):
        """PyQt tabs 点击：切换 mode + 重新渲染 body（不重建 QWebEngineView）"""
        if mode == self._welcome_mode:
            return
        self.set_welcome_mode(mode)
        self.welcomeModeChanged.emit(mode)

    def _get_welcome_window_context(self) -> dict:
        """获取当前窗口的 UI 上下文（注入插件 render_func 用）

        多窗口隔离：每张欢迎卡片持有自己窗口的 context provider（创建时由
        create_welcome_card 传入 window._build_ui_context），渲染插件 tab 时
        读到的 project_root / project_name 属于**本窗口**，不会因标签页切换
        或全局配置变更而串成其他窗口的项目。
        """
        if self._welcome_ctx_provider is not None:
            try:
                ctx = self._welcome_ctx_provider()
                if isinstance(ctx, dict):
                    return ctx
            except Exception:
                pass
        return {}

    def set_welcome_mode(self, mode: str):
        """切换欢迎卡片模式（同步 active tab + 重渲染 body）"""
        self._welcome_mode = mode
        if self._welcome_mode_tabs is not None:
            try:
                self._welcome_mode_tabs.setCurrentItem(mode)
            except Exception:
                pass
        if mode == "changelog":
            self._render_welcome_with_body(_render_changelog_body(loading=True))
            self._start_changelog_fetcher()
            return
        # sessions 走 markdown 渲染
        body_html = _render_welcome_body(
            mode,
            self._welcome_recent,
            self._welcome_top,
            self._get_welcome_window_context(),
        )
        self._render_welcome_with_body(body_html)

    def _render_welcome_with_body(self, body_html: str):
        """统一的 body 渲染入口：拼接 greeting + 写入 viewer（markdown 路径）

        软刷新（refresh_welcome_data）也经此入口，复用首次渲染的固定问候语，
        避免其他标签页会话变更广播到本窗口时欢迎卡片问候语无谓跳变。
        """
        greeting = self._welcome_greeting or get_random_greeting()
        welcome_md = f"### 👋 {greeting}\n\n{body_html}\n"
        if self.viewer is not None and self._lazy_rendered:
            self.set_content(welcome_md)
        else:
            self._pending_welcome_md = welcome_md

    def _start_changelog_fetcher(self):
        """启动 changelog 后台拉取（幂等：缓存有效直接渲染；fetcher 在跑则跳过）"""
        cache = _changelog_cache
        if cache and (time.time() - cache.get("fetched_at", 0)) < _CHANGELOG_CACHE_TTL:
            self._apply_changelog_releases(cache["releases"])
            return
        if self._changelog_fetcher is not None and self._changelog_fetcher.isRunning():
            return
        self._changelog_fetcher = _ChangelogFetcher(etag=cache.get("etag", ""))
        self._changelog_fetcher.finished.connect(self._on_changelog_finished)
        self._changelog_fetcher.error.connect(self._on_changelog_error)
        self._changelog_fetcher.start()

    def _apply_changelog_releases(self, releases: list):
        """用 release 数据渲染 changelog body"""
        self._changelog_releases = list(releases or [])
        body_html = _render_changelog_body(releases=self._changelog_releases)
        self._render_welcome_with_body(body_html)

    def _on_changelog_finished(self, payload):
        """_ChangelogFetcher.finished 回调：payload 是 list（304 → []，否则 [{releases, etag}]）"""
        try:
            if isinstance(payload, list) and payload and isinstance(payload[0], dict) and "releases" in payload[0]:
                data = payload[0]
                new_releases = data["releases"]
                # 增量判断：拉回的 tag 列表与缓存前 N 个完全一致 → 无更新，跳过渲染
                old_tags = [r.get("tag_name", "") for r in _changelog_cache.get("releases", [])]
                new_tags = [r.get("tag_name", "") for r in new_releases]
                if old_tags and old_tags == new_tags:
                    # tag 列表未变（即使 etag 不同也可能是 GitHub 临时重生成）→ 不重渲染
                    _changelog_cache["etag"] = data.get("etag", _changelog_cache.get("etag", ""))
                    _changelog_cache["fetched_at"] = time.time()
                    return
                _changelog_cache["releases"] = new_releases
                _changelog_cache["etag"] = data.get("etag", "")
                _changelog_cache["fetched_at"] = time.time()
                self._apply_changelog_releases(new_releases)
            # 304：缓存仍新鲜（fetched_at 已存在），无需重渲染
        except Exception as e:
            logger.warning(f"[WelcomeChangelog] 处理 fetcher 结果失败：{e}")
        finally:
            self._changelog_fetcher = None

    def _on_changelog_error(self, msg: str):
        """_ChangelogFetcher.error 回调：渲染错误占位"""
        self._render_welcome_with_body(_render_changelog_body(error_msg=msg))
        self._changelog_fetcher = None

    def set_welcome_content(
        self,
        recent_sessions: list,
        top_by_count: list,
        mode: str = "sessions",
        context_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        """一次性设置欢迎卡片数据 + 初始 mode（被 create_welcome_card 调用）

        Args:
            recent_sessions: 最近会话列表
            top_by_count: 最活跃会话列表
            mode: 初始欢迎模式（sessions / changelog / 插件注册 tab）
            context_provider: 窗口上下文提供者（无参回调 → dict）。多窗口隔离：
                渲染插件 tab 时注入当前窗口的 project_root / project_name /
                window_id，避免插件回读全局状态导致跨标签页内容串项目。
        """
        self._welcome_ctx_provider = context_provider
        self._welcome_recent = list(recent_sessions or [])
        self._welcome_top = list(top_by_count or [])
        self._welcome_mode = mode
        if self._welcome_mode_tabs is not None:
            try:
                self._welcome_mode_tabs.setCurrentItem(mode)
            except Exception:
                pass
        if mode == "changelog":
            # changelog 走异步：先存 loading 占位；viewer 就绪后会调 fetcher
            self._pending_welcome_md = f"### 👋 {get_random_greeting()}\n\n{_render_changelog_body(loading=True)}\n"
            return
        body_html = _render_welcome_body(
            mode,
            self._welcome_recent,
            self._welcome_top,
            self._get_welcome_window_context(),
        )
        greeting = get_random_greeting()
        self._welcome_greeting = greeting
        self._pending_welcome_md = f"### 👋 {greeting}\n\n{body_html}\n"

    def refresh_welcome_data(self, recent_sessions: list, top_by_count: list) -> None:
        """会话数据变更后的轻量刷新：更新列表数据并重渲染 body（保留卡片实例）。

        与 set_welcome_content 的区别：set_welcome_content 只写
        _pending_welcome_md（懒渲染消费），已渲染的卡片调用后 UI 不更新；
        本方法在卡片已渲染时直接重渲染 DOM，避免调用方走「销毁缓存卡片 +
        重建 QWebEngineView」路径（100-500ms 主线程占用 + 视觉闪烁）。

        仅 sessions 类 body 展示会话列表，changelog / 插件 tab 不依赖该数据，
        跳过重渲染（插件 tab 的 render_func 也不应因会话变更被反复调用）。
        """
        old_recent, old_top = self._welcome_recent, self._welcome_top
        new_recent = list(recent_sessions or [])
        new_top = list(top_by_count or [])
        # 数据无变化时跳过重渲染：其他标签页对话完成广播到本窗口时，
        # 若新会话不在本窗口当前项目下（按项目过滤），recent/top 完全不变，
        # 重渲染会白播一遍 stagger fade-in 动画。
        if new_recent == old_recent and new_top == old_top:
            return
        self._welcome_recent = new_recent
        self._welcome_top = new_top
        if self._welcome_mode != "sessions":
            return
        # 软刷新：数据更新导致的重渲染，抑制 session-item 进入动画（仅首次
        # 进入播放），避免其他标签页对话完成广播到本窗口时所有列表项重播
        # stagger fade-in（见 _render_sessions_body / _render_item 的 suppress_anim）。
        body_html = _render_welcome_body(
            self._welcome_mode,
            self._welcome_recent,
            self._welcome_top,
            self._get_welcome_window_context(),
            suppress_anim=True,
        )
        self._render_welcome_with_body(body_html)

    def _build_card_header(self, main: QVBoxLayout):
        """头部：头像 + 名称/副标题 + 时间戳/模型名 + 顶部操作按钮 + 分隔线

        仅 welcome 卡片使用；assistant 全减模式无头部（见 _setup_ui）；
        user 卡片为简洁气泡（见 _setup_user_bubble）。
        """
        if self.role == "assistant":
            # 全减模式：assistant 无头像/标题/顶部按钮/分隔线，直接进入正文
            return
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
            av_icon = get_icon("用户")
            pixmap = av_icon.pixmap(28, 28)
            av.setPixmap(pixmap)
            av.setFixedSize(30, 30)
            av.setAlignment(Qt.AlignCenter)

        font_css = get_font_family_css()
        top.addWidget(av)
        # 欢迎卡片：极简头部，只剩头像 + 右侧 mode 切换 tabs（无标题/副标题文字）
        # 其他角色（assistant/user）：保留原 title_wrap + 模型名/时间戳 + 顶部操作按钮
        if self.role == "welcome":
            # 仍创建 label 引用占位以兼容 hasattr 守卫（refresh_theme 等），但不显示
            nm_l = QLabel(self._theme["title"], self)
            self._name_label = nm_l
            nm_l.setVisible(False)
            sub_l = QLabel(self._theme["subtitle"], self)
            self._subtitle_label = sub_l
            sub_l.setVisible(False)
            self._build_welcome_mode_tabs(top)
        else:
            title_wrap = QWidget(self)
            title_layout = QVBoxLayout(title_wrap)
            title_layout.setContentsMargins(0, 0, 0, 0)
            title_layout.setSpacing(1)

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
            top.addWidget(title_wrap)
            # 助手卡片显示模型名称
            label_text = self.model_name if (self.role == "assistant" and self.model_name) else self.timestamp
            ts = QLabel(label_text, self)
            self._ts_label = ts
            ts.setVisible(bool(label_text))
            ts.setStyleSheet(
                f"""
                QLabel {{
                    {get_font_family_css()} font-size: {scale_font_size(11)}px;
                    color: {self._theme["muted"]};
                    background: {Colors.CONTENT_BG};
                    border: 1px solid {Colors.BORDER};
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
                    get_icon("复制"),
                    "复制",
                    lambda: self.actionRequested.emit(self.get_plain_text(), "copy"),
                ),
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

    def _setup_user_bubble(self, main: QVBoxLayout):
        """用户消息简洁气泡：纯文本 + 底部 hover 操作行（主流大模型式）

        - 无头像 / "User·Prompt" 标题 / 分隔线
        - 复制/撤销/删除按钮 hover 浮现（见 enterEvent/leaveEvent），时间戳常显弱化
        - 宽度自适应见 PlainTextViewer._update_height（idealWidth 收缩，不占满整行）
        """
        # 修 #2：用户气泡 PlainTextViewer 改为懒加载——首次 set_content / showEvent 时才
        # 创建，避免每张卡片 __init__ 即构造 QTextEdit+QTextDocument+QTimer+QWidget 子树
        # （参考 assistant/welcome 卡片的懒渲染模式，复用 _lazy_rendered 守卫）。
        self.viewer = None
        self._viewer_pending_text = None
        main.addWidget(self._viewer_container)
        self._lazy_rendered = True

        # 底部操作行：stretch | 时间戳 | 复制/撤销/删除（hover 浮现）。
        # 外层 wrap 固定高度：按钮显隐切换时 footer 占位不变，卡片不跳动
        footer_wrap = QWidget(self)
        footer_wrap.setStyleSheet("background: transparent;")
        footer_wrap.setFixedHeight(28)  # 26px 按钮 + 垂直余量，紧凑
        footer = QHBoxLayout(footer_wrap)
        footer.setContentsMargins(6, 0, 6, 0)
        footer.setSpacing(6)
        footer.addStretch()

        ts = QLabel(self.timestamp, self)
        self._ts_label = ts
        ts.setVisible(bool(self.timestamp))
        ts.setStyleSheet(f"{get_font_family_css()} font-size: {scale_font_size(11)}px; color: {self._theme['muted']};")
        footer.addWidget(ts)

        btns = QWidget(self)
        self._user_action_btns = btns
        bl = QHBoxLayout(btns)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(2)
        for ic, tp, cb in [
            (get_icon("复制"), "复制", lambda: self._copy_user_message()),
            (get_icon("撤销"), "撤销到这里", self.undoRequested.emit),
            (get_icon("删除"), "删除", self.deleteRequested.emit),
        ]:
            b = TransparentToolButton(ic, self)
            b.setToolTip(tp)
            b.clicked.connect(cb)
            b.setFixedSize(26, 26)  # 弱化处理：比助手卡 32px 更小
            install_hover_tooltip(b, delay_ms=200)
            bl.addWidget(b)
        btns.setVisible(False)  # hover 浮现，保持气泡简洁（高度占位由 wrap 固定）
        footer.addWidget(btns)
        main.addWidget(footer_wrap)

    def _ensure_user_viewer(self) -> None:
        """懒创建用户气泡 PlainTextViewer（修 #2）。

        首次 set_content / showEvent / append_text 时调用；创建后连接 contentHeightChanged
        并应用暂存的待显示文本。幂等。避免 __init__ 即建 QTextEdit 子树造成的内存占用。
        """
        if self.viewer is not None:
            return
        self.viewer = PlainTextViewer(self)
        self.viewer.contentHeightChanged.connect(self._update_height)
        self._viewer_layout.addWidget(self.viewer)
        if getattr(self, "_viewer_pending_text", None):
            self.viewer.set_text(self._viewer_pending_text)
            self._viewer_pending_text = None

    def _setup_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(4, 4, 4, 4)
        main.setSpacing(4 if self.role != "user" else 0)  # user：正文与时间行零间隙

        if self.role == "user":
            # 用户消息：ChatGPT 式简洁气泡（无头像/标题/分隔线），
            # 右对齐由 chat_layout 的 AlignRight 控制，宽度自适应见 PlainTextViewer
            self._setup_user_bubble(main)
        else:
            self._build_card_header(main)

        # ── 内容区（welcome/assistant 走懒渲染，user 已在气泡方法内创建）──
        if self.role == "welcome":
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
        elif self.role != "user":  # user 已在 _setup_user_bubble 创建，不再进入懒渲染
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

        if self.role == "welcome":  # 全减：assistant 无底部装饰线；user 简洁气泡本就不带
            main.addWidget(CardSeparator(self))

        # ===== 助手卡片底部元信息栏（分割线下方） =====
        if self.role == "assistant":
            self._build_footer_bar(main)
        # 卡片背景/圆角：user 简洁气泡 12px 圆角无边框，其余 10px
        self._apply_card_style()

        # 淡入动画：新消息微妙出现（200ms，仅透明度）
        fade_in_widget(self, 200)

    def start_streaming_anim(self):
        if self._streaming:
            return
        self._streaming = True
        # 新一轮开始：清除上一轮的"已完成流式"标记，使本轮重新按流式语义
        # 走展开路径（同 _is_history 的修正，见下方）。
        self._streaming_finished = False
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
        # [V1] 可见性门控：隐藏 tab 不执行动画帧（避免隐藏页每 50ms 空转 update()）。
        # 相位 _pulse_phase 是模 2π 的循环累积，暂停后从原相位继续，无视觉跳变；
        # 恢复可见后下一拍定时器自动续跑，无需显式重启。
        if not self.isVisible():
            return
        # 拖拽期间暂停重绘：原生拖拽时主线程在 DefWindowProc 模态循环里，
        # 每 50ms 触发一次 update() 会强制 DWM 对整窗重新合成 → 拖拽卡顿。
        # 直接跳过 update() 让窗口保持静止，DWM 仅平移已有纹理，拖拽顺滑；
        # 松手后 _any_window_dragging 复位，下一拍定时器自然恢复动画。
        from app.utils.window_drag_state import any_window_dragging

        if any_window_dragging:
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
        # user 简洁气泡：12px 圆角 + 无边框（仅轻量背景色）；错误态仍显示红色边框
        if self.role == "user" and not self.error:
            self.setStyleSheet(
                f"""
                CardWidget {{
                    background-color: {bg or self._base_bg};
                    border: none;
                    border-radius: 12px;
                }}
                """
            )
            return
        if self.role == "assistant" and not self.error:
            # 全减模式：assistant 纯文字流（无边框无背景）；
            # 错误/重试/上下文丢失态仍走下方原逻辑（红框提示）
            self.setStyleSheet(
                """
                CardWidget {
                    background-color: transparent;
                    border: none;
                }
                """
            )
            return
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
        # 标记本轮已走过流式（含用户中断/出错中断）：viewer 创建或虚拟滚动
        # 回收重建时据此保持工具区展开，不再被判为"历史"而折叠。
        self._streaming_finished = True
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

    def _on_chart_expand(self, chart_type: str, payload_b64: str):
        """图表放大查看 → 打开覆盖右侧对话区域的 chart_viewer 全局卡

        内部直接处理（不走 main_widget 回调），assistant/welcome/历史卡统一生效；
        ui_helpers 顶部反向 import MessageCard，必须延迟导入避免循环依赖。
        """
        try:
            from app.widgets.ui_helpers import show_chart_viewer

            show_chart_viewer(self, chart_type, payload_b64)
        except Exception as e:
            logger.error(f"[MessageCard] 图表放大失败: {e}")

    def _on_save_chart_png(self, name_b64: str, png_b64: str):
        """图表 PNG 导出保存（消息卡小图导出与放大视图共用通道）"""
        try:
            name = base64.b64decode(name_b64).decode("utf-8") if name_b64 else "图表"
        except Exception:
            name = "图表"
        from app.widgets.ui_helpers import save_png_from_b64

        path = save_png_from_b64(self, png_b64, name or "图表")
        if path:
            logger.info(f"[MessageCard] 图表 PNG 已导出: {path}")

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

        # 灰度：Qt 渲染器无 context lost，不应进入此方法；防御性回退到 WebEngine
        self.viewer = CodeWebViewer(self)
        self.viewer._lazy_markdown_cb = self._build_incremental_md
        self.viewer.codeActionRequested.connect(self.actionRequested.emit)
        self.viewer.contextActionRequested.connect(self.contextActionRequested.emit)
        self.viewer.contentHeightChanged.connect(self._update_height)
        self.viewer.toolDiffRequested.connect(self.toolDiffRequested.emit)
        self.viewer.subAgentLogRequested.connect(self.subAgentLogRequested.emit)
        self.viewer.saveFileRequested.connect(self.saveFileRequested.emit)
        self.viewer.chartExpandRequested.connect(self._on_chart_expand)
        self.viewer.saveChartPngRequested.connect(self._on_save_chart_png)
        self.viewer.contextLost.connect(self._on_webengine_context_lost)
        self.viewer.contextRestored.connect(self._on_webengine_context_restored)
        self.viewer.needRecreate.connect(self._on_webengine_need_recreate)
        self.viewer._install_dialog_filter()

        self._viewer_layout.addWidget(self.viewer)

        # 恢复内容
        if markdown_text:
            self.viewer._markdown_text = markdown_text
            self.viewer._schedule_render(immediate=True)

        # 任务列表随 viewer 重建补推（_pending_todos 由 viewer._on_js_ready 消费）
        if self._todos_snapshot is not None:
            self._push_todo_list()

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
        if self.role == "welcome":
            # 静态 accent 侧边竖条（user 简洁气泡 / assistant 全减模式不画，保持纯净）
            accent.setAlpha(75)
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

            rainbow = self._rainbow_retry if self._retrying else self._rainbow_normal
            N = len(rainbow)
            # 主边框连续相位
            shift_main = (self._pulse_phase / (math.pi * 2)) * N
            # 发光层更慢
            shift_glow = shift_main * 0.5
            # 流光带相位
            shift_shimmer = shift_main * 1.15

            def build_gradient(grad: QLinearGradient, shift: float, stops: list, alpha_base: float) -> QLinearGradient:
                """相位无关模板 grad 复用：仅改坐标与 stop 颜色，不每帧 new"""
                grad.setStart(0, 0)
                grad.setFinalStop(w, h)
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
        # M1：裁剪路径按几何缓存，仅尺寸变化时重建，不再每帧 new QPainterPath
        if self._clip_w != w or self._clip_h != h:
            self._clip_w, self._clip_h = w, h
            self._clip_inner = QPainterPath()
            self._clip_inner.addRoundedRect(3, 3, w - 6, h - 6, radius - 2, radius - 2)
            self._clip_outer = QPainterPath()
            self._clip_outer.addRoundedRect(-2, -2, w + 4, h + 4, radius + 3, radius + 3)
            self._clip_inner_edge = QPainterPath()
            self._clip_inner_edge.addRoundedRect(0, 0, w, h, radius + 1, radius + 1)
            self._clip_border = QPainterPath()
            self._clip_border.addRoundedRect(0, 0, w, h, radius + 1, radius + 1)
            self._clip_inner_border = QPainterPath()
            self._clip_inner_border.addRoundedRect(2, 2, w - 4, h - 4, radius - 1, radius - 1)
            self._clip_shimmer = QPainterPath()
            self._clip_shimmer.addRoundedRect(1, 1, w - 2, h - 2, radius, radius)
            self._clip_top = QPainterPath()
            self._clip_top.addRoundedRect(0, 0, w, h, radius, radius)
            self._clip_glow_region = self._clip_outer - self._clip_inner_edge
            self._clip_border_region = self._clip_border - self._clip_inner_border
        inner_clip = self._clip_inner
        painter.setClipPath(inner_clip)
        if self.role == "assistant":
            inner_gradient = build_gradient(self._grad_inner, shift_glow, inner_stops, 12)
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
        outer_clip = self._clip_outer
        inner_edge_clip = self._clip_inner_edge
        glow_region = self._clip_glow_region
        painter.setClipPath(glow_region)
        if self.role == "assistant":
            glow_gradient = build_gradient(self._grad_glow, shift_glow, glow_stops, 48)
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
        border_clip = self._clip_border
        inner_border_clip = self._clip_inner_border
        border_region = self._clip_border_region
        painter.setClipPath(border_region)
        if self.role == "assistant":
            main_gradient = build_gradient(self._grad_main, shift_main, main_stops, 215)
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
            shimmer_clip = self._clip_shimmer
            painter.setClipPath(shimmer_clip)
            # 流光位置：连续小数，避免跳变
            shimmer_pos = (shift_shimmer % N) / N
            # 注意：stop 位置随相位连续变化，不能复用模板渐变（setColorAt 会不断追加 stop 导致残留脏色），必须每帧新建
            shimmer_band_gradient = QLinearGradient(0, 0, w, h)
            shimmer_band_gradient.setStart(0, 0)
            shimmer_band_gradient.setFinalStop(w, h)
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
        top_clip = self._clip_top
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
        if h >= current_height:
            # 增长方向：小阈值立即应用，保证流式输出滚底跟随
            if h - current_height > 2:
                self._apply_viewer_height(h)
        else:
            # 收拢方向：小步回弹（<40px）流式期间不应用。
            # 来源：流式块→完成块的 DOM 替换、滚动条出现/消失的重排噪声。
            # 延迟到 finish_streaming 后的全量渲染统一收敛，消除"长一下又缩回去"的抖动。
            # 大幅收拢（≥40px，折叠/展开/dock 切换）仍正常应用。
            if current_height - h >= 40:
                self._apply_viewer_height(h)

    def _on_qt_viewer_height(self, h: int) -> None:
        """灰度：纯 Qt viewer 高度自治（layout 自适应，不 setFixedHeight），
        仅转发高度变化给父容器（滚底跟随依赖 heightChanged 链路）。"""
        self.heightChanged.emit(max(40, int(h)))

    def _apply_viewer_height(self, value):
        height = max(40, int(value))
        if height == self._last_applied_viewer_height:
            return
        self._last_applied_viewer_height = height
        # [PERF] resize preview 期间 viewer 已 hide + setUpdatesEnabled(False)，
        # 此时 setFixedHeight 仍会触发 Qt 布局链 → QWebEngineView Chromium
        # 视口大小变化 → 整页 relayout → ResizeObserver → reportHeight →
        # _stream_height_timer 80ms 防抖 → 又一轮 setFixedHeight 的循环。
        # 是流式 + resize 卡顿的根因之一。仅记录目标高度，preview 退出时
        # set_resize_preview_mode(False) 一次性应用 + 上报，避免级联重排。
        if self._resize_preview_mode:
            self._pending_viewer_height = height
            return
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
                    # 区域独立 II：高度回调由任何内容变化（含工具/思考区高度）触发，
                    # 不代表正文更新 → bodyOnly 不碰正文容器滚动位置
                    "  if (!window._userScrolledWithin) {"
                    "    _autoScrollStreamingBody(true);"
                    "  } else {"
                    "    var wasAtBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < "
                    + str(AUTO_SCROLL_THRESHOLD)
                    + ";"
                    "    if (wasAtBottom) {"
                    "      _autoScrollStreamingBody(true);"
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
            if self.role == "user":
                # 与 main_widget 的比例 margin 保持一致（约容器 94%，留少量对齐余量）
                horizontal_margin = max(150, int(parent_width * 0.15))
            else:
                horizontal_margin = 20
            target_width = max(320, parent_width - horizontal_margin)

        # 性能优化：只有宽度真正变化时才更新（user/非 user 统一守卫）
        if not force and target_width == self._last_synced_width:
            return
        self._last_synced_width = target_width

        if self.role == "user":
            # 简洁气泡：释放最小宽，只设上限，
            # 实际宽度由 PlainTextViewer 按内容最长行自适应收缩
            self.setMinimumWidth(60)
            self.setMaximumWidth(target_width)
            # 上限同步给 viewer（卡内边距 4*2 + viewer 布局边距 8*2），
            # cap 未变化时 set_width_cap 内部为 no-op。
            # 🐛 不受 _resize_preview_mode 拦截：preview 守卫是为 CodeWebViewer
            # （WebEngine 重排昂贵）设计的，PlainTextViewer 轻量无需保护；
            # 若在 resize 期间拦截，而退出 preview 时 user 卡片直接 return
            # 不补同步，气泡宽度/高度将永远停留在 resize 前的旧值，
            # 窗口缩小后固定尺寸的气泡超出可视区（文字跑到显示范围之外）。
            if self.viewer is not None:
                self.viewer.set_width_cap(target_width - 24)
            return

        # 非 user（assistant/welcome）：固定宽度（min=max）
        if self.minimumWidth() != target_width or self.maximumWidth() != target_width:
            self.blockSignals(True)
            self.setMinimumWidth(target_width)
            self.setMaximumWidth(target_width)
            self.blockSignals(False)

        # 宽度同步后触发 viewer 高度重算（CodeWebViewer 内容重排）
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

        # 懒渲染还没创建viewer，跳过（welcome 卡已创建 viewer 时同样走占位逻辑）
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

        # [PERF] 流式卡片：preview 期间累积的高度变化一次性应用。
        # _apply_viewer_height 在 preview 模式下只记录 _pending_viewer_height，
        # 此处 setFixedHeight 把 viewer 设到正确高度，触发一次 ResizeObserver →
        # reportHeight → Python 拿到真实高度，结束 preview 期间累积的高度死循环。
        # 只对 CodeWebViewer（非 PlainTextViewer）有效：PlainTextViewer 的
        # update_height() 已在上方调用且自身无 _pending_viewer_height 字段。
        pending_h = getattr(self, "_pending_viewer_height", None)
        if pending_h is not None and self.role != "user" and self.viewer is not None:
            try:
                self.viewer.setFixedHeight(pending_h)
                self.heightChanged.emit(pending_h)
            except RuntimeError:
                pass
            self._pending_viewer_height = None
            # 强制一次高度上报：让 JS 侧 ResizeObserver 也跟上 viewer 新高度，
            # 防止 Chromium 内部仍按旧高度布局（_apply_viewer_height 没真正
            # 改高度时 Chromium 视口尺寸未变）→ 首帧 paint 仍按旧宽排版。
            if hasattr(self.viewer, "page") and self.viewer.page():
                try:
                    self.viewer.page().runJavaScript("reportHeight();")
                except RuntimeError:
                    pass

    def enterEvent(self, event):
        # 用户气泡 / assistant 全减：hover 浮现操作按钮，保持静态简洁
        if self.role == "user" and getattr(self, "_user_action_btns", None) is not None:
            self._user_action_btns.setVisible(True)
        elif self.role == "assistant" and getattr(self, "_assistant_action_btns", None) is not None:
            self._assistant_action_btns.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.role == "user" and getattr(self, "_user_action_btns", None) is not None:
            self._user_action_btns.setVisible(False)
        elif self.role == "assistant" and getattr(self, "_assistant_action_btns", None) is not None:
            self._assistant_action_btns.setVisible(False)
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        # MessageCard 的 wheelEvent 仅在子 widget（viewer）未消费事件时被调用。
        # 此时说明内部没有可滚动内容，或内部已达边界 → 直接转发到外部。
        try:
            scroll_area = self._parent.chat_scroll_area
            if scroll_area:
                vbar = scroll_area.verticalScrollBar()
                if vbar and vbar.minimum() != vbar.maximum() and event.angleDelta().y() != 0:
                    vbar.setValue(vbar.value() - wheel_delta_to_px(event.angleDelta().y()))
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

    def showEvent(self, event):
        """可见性恢复：窗口切回可见时补渲此前因不可见而推迟的欢迎卡片 QWebEngineView。

        批量建标签页时，非 current 标签页的欢迎卡片在 200ms 懒渲染队列触发
        ensure_rendered 时窗口不可见，被 _do_ensure_rendered 的可见性守卫推迟
        （设 _render_deferred=True 并 return，避免弹出幽灵窗口）。切回该标签页
        时本事件触发，此时父 HWND 已就绪，可安全创建 QWebEngineView。
        """
        super().showEvent(event)
        # 修 #2：用户气泡 viewer 懒加载——若已暂存待显示文本且 viewer 尚未创建，补建
        if self.role == "user" and self.viewer is None and getattr(self, "_viewer_pending_text", None):
            self._ensure_user_viewer()
        if getattr(self, "_render_deferred", False) and not getattr(self, "_lazy_rendered", False):
            self.ensure_rendered()

    def _is_effectively_visible(self) -> bool:
        """判断本卡片是否真正显示在屏幕上（有有效父 native window 供 QWebEngineView 附着）。

        仅用 isVisible() 不可靠：批量建标签页时，被 QStackedWidget 挤出 current 的
        隐藏页在某些时序下 isVisible() 仍返回 True，导致 QWebEngineView（Windows 上
        创建原生 HWND 子窗口）在缺少有效父句柄时弹出独立原生窗口（幽灵窗口/白窗一闪而过）。
        故直接检查本卡片是否在其所在 QStackedWidget 的当前页子树中。

        注意：必须遍历**所有**父链上的 QStackedWidget，而不是只检查第一个。
        Tab 管理器有嵌套两层 QStackedWidget——窗口级 _content_area 与外层覆盖级
        _content_stack（index 0 对话区 / index 1 系统卡片覆盖层）。若只检查第一层，
        覆盖层打开时（如项目选择卡片）对话区实际隐藏，但窗口级 currentWidget 仍是
        当前窗口 → 误判可见 → 创建 QWebEngineView 弹出幽灵窗口。
        """
        top = self.window()
        if top is None or not top.isVisible():
            return False
        # 沿父链查找 QStackedWidget（duck-typing，避免强依赖导入），逐层检查全部层级
        p = self.parentWidget()
        while p is not None:
            cur = getattr(p, "currentWidget", None)
            if cur is not None and callable(cur):
                current = cur()
                if current is None or not current.isAncestorOf(self):
                    return False
                # 本层通过，继续向上检查外层 QStackedWidget（覆盖层级）
            p = p.parentWidget()
        return True

    def ensure_rendered(self, delay_ms: int = 0):
        """如果还没渲染，懒加载创建QWebViewer并渲染内容

        Args:
            delay_ms: 延迟加载毫秒数。默认0立即加载，>0则延迟加载并发送信号
        """
        if self._lazy_rendered or self.role == "user":
            return

        def _do_ensure_rendered():
            # 🛡️ 防幽灵窗口：与 _schedule_render 对称，不可见时不创建 QWebEngineView。
            # QWebEngineView 在 Windows 上创建原生 HWND 子窗口（见 _hide_for_dialog 注释）。
            # 当 widget 所在窗口不可见（Tab 管理器中非 current 标签页）时，父链无有效
            # native window 句柄，Chromium 会弹出独立原生窗口（幽灵窗口）。
            # 快速批量建标签页时，只有最后一个标签页可见，前 N-1 个在 200ms 懒渲染队列
            # 触发 ensure_rendered 时已不可见 → 弹出幽灵窗口。
            # 修复：非当前可见标签页时标记 _render_deferred 并 return，等 showEvent（窗口切回可见）补渲。
            if not self._is_effectively_visible():
                self._render_deferred = True
                return
            # 移除占位符，创建真正的viewer
            for i in reversed(range(self._viewer_layout.count())):
                item = self._viewer_layout.itemAt(i)
                if item and item.widget():
                    item.widget().deleteLater()

            # welcome 卡片使用轻量骨架（无 echarts CDN）
            is_welcome = self.role == "welcome"
            if not is_welcome and _qt_renderer_enabled():
                # 灰度：纯 Qt 块级渲染器（无 Chromium/JS 层）
                self.viewer = _get_markdown_block_viewer_cls()(self)
                self.viewer.contentHeightChanged.connect(self._on_qt_viewer_height)
                self.viewer.saveFileRequested.connect(self.saveFileRequested.emit)
                # 仅"从磁盘加载的历史会话"折叠；本轮对话（流式进行中或已完成）
                # 保持展开 —— 后者若按 _streaming=False 判为历史，会在虚拟滚动
                # 回收重建后突然折叠，与首次渲染的展开态不一致。
                self.viewer._is_history = not (self._streaming or self._streaming_finished)
                # 历史会话：同步非流式态——viewer 初始 _streaming=True 是为流式
                # 增量注入设计；历史渲染须走非流式分支（完成态渲染、不追加流式
                # 字数统计、不残留流式坞态）。
                if self.viewer._is_history:
                    self.viewer._streaming = False
                self._viewer_layout.addWidget(self.viewer)
                self._lazy_rendered = True
                self._render_deferred = False
                if self._todos_snapshot is not None:
                    self._push_todo_list()
                if self._pending_content is not None:
                    self.set_content(self._pending_content)
                    self._pending_content = None
                elif self._pending_welcome_md is not None:
                    self.set_content(self._pending_welcome_md)
                    self._pending_welcome_md = None
                    if self._welcome_mode == "changelog":
                        self._start_changelog_fetcher()
                self.lazyRenderCompleted.emit()
                return
            self.viewer = CodeWebViewer(self, light=is_welcome)
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            if not is_welcome:
                # 标记是否为历史会话：非流式加载的历史消息自动折叠工具区
                # 仅"从磁盘加载的历史会话"折叠；本轮对话（流式进行中或已完成）
                # 保持展开 —— 后者若按 _streaming=False 判为历史，会在虚拟滚动
                # 回收重建后突然折叠，与首次渲染的展开态不一致。
                self.viewer._is_history = not (self._streaming or self._streaming_finished)
                # 历史会话：同步非流式态（viewer 初始 _streaming=True 为流式设计，
                # 历史渲染须走非流式分支：完成态渲染、无流式字数统计/坞态）。
                if self.viewer._is_history:
                    self.viewer._streaming = False
                # 让 viewer 的 restore 逻辑知道哪些工具结果已到达，
                # 避免全量重渲染时把已完成的运行框以“运行中”状态复活。
                self.viewer._restore_finished_ids = self._finished_streaming_ids
            self.viewer.codeActionRequested.connect(self.actionRequested.emit)
            self.viewer.contextActionRequested.connect(self.contextActionRequested.emit)
            self.viewer.contentHeightChanged.connect(self._update_height)
            self.viewer.toolDiffRequested.connect(self.toolDiffRequested.emit)
            self.viewer.subAgentLogRequested.connect(self.subAgentLogRequested.emit)
            self.viewer.saveFileRequested.connect(self.saveFileRequested.emit)
            self.viewer.chartExpandRequested.connect(self._on_chart_expand)
            self.viewer.saveChartPngRequested.connect(self._on_save_chart_png)
            # WebEngine 上下文丢失处理
            self.viewer.contextLost.connect(self._on_webengine_context_lost)
            self.viewer.contextRestored.connect(self._on_webengine_context_restored)
            self.viewer.needRecreate.connect(self._on_webengine_need_recreate)
            # 安装对话框过滤
            self.viewer._install_dialog_filter()

            self._viewer_layout.addWidget(self.viewer)
            self._lazy_rendered = True
            # 创建 viewer 完成（不可见门控已放行），清除"推迟渲染"标记；
            # 若下方 set_content 因 JS 未就绪再次 deferred，由 _on_js_ready 兜底补渲。
            self._render_deferred = False

            # 任务列表随 viewer 创建补推（JS 未就绪时由 _on_js_ready 兜底）
            if self._todos_snapshot is not None:
                self._push_todo_list()

            # 如果有等待渲染的内容，现在渲染
            if self._pending_content is not None:
                self.set_content(self._pending_content)
                self._pending_content = None
            elif self._pending_welcome_md is not None:
                # 欢迎卡片懒渲染：set_welcome_content 在 viewer 创建前存的内容
                self.set_content(self._pending_welcome_md)
                self._pending_welcome_md = None
                # 欢迎卡片 viewer 就绪后，changelog 模式启动后台拉取
                if self._welcome_mode == "changelog":
                    self._start_changelog_fetcher()

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

        # 修 #2：用户气泡 viewer 懒加载，首次 set_content 时创建（set_text 前确保存在）
        if self.role == "user" and self.viewer is None:
            self._ensure_user_viewer()
        if hasattr(self.viewer, "_markdown_text"):
            self.viewer._markdown_text = rendered
            # [B1] 内容整体替换：使差量渲染缓存失效（_stable_md_len 指向旧内容偏移，
            # 继续差量会重复渲染旧段），强制下次全量渲染建立新基线。
            self.viewer._needs_full_render = True
            self.viewer._stable_html = ""
            self.viewer._stable_md_len = 0
            self.viewer._schedule_render(immediate=True)
        elif hasattr(self.viewer, "set_text"):
            self.viewer.set_text(rendered)
        self._content_just_loaded = True

    def rerender_custom_blocks(self, plugin_name: str = "") -> bool:
        """插件热重载后重绘该插件渲染的自定义内容块（custom block）

        已渲染消息的 HTML 是加载时刻的快照：content renderer 的 render_func
        在 content_to_markdown 时执行一次，热重载不会自动重绘。本方法
        检测本卡片是否包含属于该插件的 custom 块（plugin_name 为空 = 全部），
        命中则用最新 render_func 重新生成 markdown 并刷新视图；未命中零开销。

        Returns:
            True 表示已重绘；False 表示本卡片无该插件的 custom 块（无需处理）。
        """
        blocks = getattr(self, "_content_data", None)
        if self.role != "assistant" or not blocks:
            return False
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        registry = UIPluginRegistry.get_instance()
        hit = False
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "custom":
                continue
            custom_type = block.get("custom_type", "")
            if not custom_type:
                continue
            info = registry.get_content_renderer(custom_type)
            if info is not None and (not plugin_name or info.plugin_name == plugin_name):
                hit = True
                break
        if not hit:
            return False
        rendered = content_to_markdown(blocks)
        if not self._lazy_rendered:
            # 懒渲染尚未执行：无需主动重绘，下次 ensure_rendered 自然用新 render_func
            return True
        if hasattr(self.viewer, "_markdown_text"):
            self.viewer._markdown_text = rendered
            # 内容整体替换：失效差量渲染缓存，强制全量渲染建立新基线（同 set_content）
            self.viewer._needs_full_render = True
            self.viewer._stable_html = ""
            self.viewer._stable_md_len = 0
            if hasattr(self.viewer, "_tool_md_cache"):
                self.viewer._tool_md_cache.clear()
            self.viewer._schedule_render(immediate=True)
        elif hasattr(self.viewer, "set_text"):
            self.viewer.set_text(rendered)
        return True

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
        if self.role == "user" and self.viewer is None:
            self._ensure_user_viewer()
        if self.role == "assistant":
            # 🆕 Bug B 方案 F：优先**原地**追加文本块（不重建列表）。
            # append_text_block 在末尾非 text 块时走 ensure_content_blocks 重建整个
            # 列表 → 所有块 dict 对象被替换 → _tool_anchor_refs 对象引用失效 →
            # append_tool_result 稳定锚点退化 → finish 完整重渲染时思考/工具顺序错乱。
            # 流式中 _content_data 已是标准块列表，原地追加/合并保住引用稳定。
            if (
                isinstance(self._content_data, list)
                and self._content_data
                and isinstance(self._content_data[-1], dict)
                and self._content_data[-1].get("type") == "text"
            ):
                self._content_data[-1]["text"] = str(self._content_data[-1].get("text", "") or "") + str(text or "")
            elif isinstance(self._content_data, list) and all(isinstance(b, dict) for b in self._content_data):
                # 直接构造 text 块（避免 make_text_block 未导入），保持原地 append 不重建
                self._content_data.append({"type": "text", "text": str(text or "")})
            else:
                self._content_data = append_text_block(self._content_data, text)
            # 优化：懒渲染模式下直接跳过 markdown 渲染，避免不必要的计算
            if not self._lazy_rendered or not self.viewer:
                self._pending_content = self._content_data
                return
            # [PERF] 增量 markdown 構建：已完成的 tool_result 塊走緩存，只有文本塊即時轉換
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            # 🆕 检测未闭合 <think> 标签：静默累积不触发渲染，与 append_reasoning 策略一致
            # 避免每个思考文本 chunk 都触发全量渲染 → reorganizeContent → think-streaming
            # DOM 节点反复 destroy+recreate 导致"思考中"状态闪烁。
            last_block = self._content_data[-1] if self._content_data else None
            last_text = last_block.get("text", "") if isinstance(last_block, dict) else ""
            _think_unclosed = _has_unclosed_think(last_text)
            # 流式模式下增量追加纯文本到 DOM，让用户立即看到文字。
            # 🐛 修复（高块闪现）：think 未闭合期间**不**调用 _append_text_incremental ——
            # 否则思考内容会以普通正文逐行注入 #content-placeholder 堆叠成高块，
            # 待 </think> 闭合后才由 _inject_think_cards 折叠成 think-compact，高块
            # 闪现后消失。与 append_reasoning 一致：未闭合期间静默累积、仅靠全量
            # 渲染落地；think 已闭合 / 无 think 标签时保持原有增量注入行为不变。
            if self._streaming and not _think_unclosed:
                self.viewer._append_text_incremental(text)
            if _think_unclosed:
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
            # 仅在自然边界触发（段落结束 / 块闭合 / 句号软边界），非边界时只启安全定时器。
            # last_text 已通过 append_text_block 包含新追加文本，判断可靠。
            if self._streaming and (
                self.viewer._has_reached_clean_boundary(last_text) or self.viewer._has_reached_soft_boundary(last_text)
            ):
                self.viewer._schedule_render(immediate=True)
            else:
                self.viewer._schedule_render(immediate=False)
            self._content_just_loaded = True
            return

        self._content_data = str(self._content_data or "") + str(text or "")
        if self.viewer:
            self.viewer.append_chunk(str(text or ""))
            self._content_just_loaded = True

    def _tool_anchor_pos(self, tool_call_id: str) -> Optional[int]:
        """返回工具调用时刻的稳定逻辑位置（append_tool_result 插入位 / data-order 基准）。

        🆕 Bug B 方案 F：优先用块引用锚点——index(ref) + 1 在列表因其他工具结果
        插入、思考/正文追加而偏移后仍精确指向"工具调用时刻的逻辑末尾"。int 索引锚点
        （_tool_insert_anchors）在偏移后失效，是 finish 完整重渲染时"思考在前、
        工具在后"数据层错乱的根因。

        Returns:
            稳定位置（0..len）；无任何锚点（历史会话等非流式路径）→ None（调用方兜底 append）。
        """
        ref = self._tool_anchor_refs.get(tool_call_id)
        if ref is not None and isinstance(self._content_data, list):
            # 用对象身份（is）定位，避免内容相同的不同块误匹配
            for _i, _b in enumerate(self._content_data):
                if _b is ref:
                    return _i + 1
        return self._tool_insert_anchors.get(tool_call_id)

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
        block = make_tool_result_block(
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            success=success,
            tool_call_id=tool_call_id,
            diff=diff,
            echarts=echarts,
        )
        # 🆕 Bug B（顺序错乱修复）：按锚点插入（工具调用发生的位置），而非恒 append 末尾。
        # 锚点 = 工具调用时 _content_data 的长度（update_tool_streaming 记录），
        # 使"思考→工具→正文→工具→正文"按实际到达顺序交错，而不是思考恒顶部、
        # 工具恒底部。同锚点多工具（一轮并行调用）：跳过所有"启动序号更早"的
        # 已插入工具块插到其后，保证按调用顺序排列（结果晚到也不乱序）。
        # 乱序兜底：无锚点（历史会话渲染等非流式路径）/ content 非 list / 锚点越界
        # → append 末尾，与修复前行为一致。
        # 🆕 Bug B 方案 F：用**块引用**锚点定位（_tool_anchor_pos），替代 int 索引。
        # int 索引在"其他工具结果插入 + 思考/正文追加"后偏移，导致工具结果插到
        # 错误位置 → _content_data 顺序错 → finish 完整重渲染时思考/工具错乱。
        anchor = self._tool_anchor_pos(tool_call_id) if isinstance(self._content_data, list) else None
        if anchor is not None and isinstance(self._content_data, list) and 0 <= anchor <= len(self._content_data):
            my_order = self._tool_call_order.get(tool_call_id, 0)
            insert_at = anchor
            _n = len(self._content_data)
            while insert_at < _n:
                _blk = self._content_data[insert_at]
                if isinstance(_blk, dict) and _blk.get("type") == "tool_result":
                    _tid = _blk.get("tool_call_id", "")
                    if self._tool_call_order.get(_tid, 0) < my_order:
                        insert_at += 1
                        continue
                break
            self._content_data.insert(insert_at, block)
        else:
            self._content_data.append(block)
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
            single_md = content_to_markdown([block])
            cache = getattr(self.viewer, "_tool_md_cache", None)
            if cache is not None:
                cache[tool_call_id] = single_md
        # 增量注入：直接通过 JS 追加工具块 HTML，跳过全量 markdown 重建
        # 避免 content_to_markdown() 遍历全部 content_data 持有 GIL 导致拖动卡顿
        try:
            # 编辑类工具注入到 content-placeholder，跳过回调与渲染避免闪烁。
            # DOM 已通过 JS 注入到位，markdown 缓存已就绪供后续全量渲染使用。
            _is_edit_tool = tool_name in _edit_tools()
            # 🐛 修复（停止吞框）：编辑工具结果也必须重设懒回调。finish_streaming
            # （停止/流式结束）的非流式渲染消费 _lazy_markdown_cb 刷新 _markdown_text；
            # 旧逻辑编辑分支跳过渲染时连 cb 一起跳过 → cb 停留 None（上一次流式渲染
            # 已消费）→ 停止渲染用旧 md（不含工具块）→ save 移除 DOM 完成框后，
            # restore 因 tid 已入 _finished_streaming_ids 不恢复 → 完成框被吞（永久消失）。
            # 此处只设 cb 不触发渲染，保持"编辑工具跳过即时渲染防闪烁"设计不变。
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            if not _is_edit_tool:
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
            # 🆕 方案 D：计算完成态工具块的 data-order（与 _inject_tool_streaming_html
            # 同口径）。无锚点（历史会话等非流式路径）→ 基准取当前末尾位置，与
            # _content_data.append 兜底行为一致（沉底不早于任何已有块）。
            # 🆕 Bug B 方案 F：用块引用锚点定位稳定位置（int 索引在列表偏移后失效）。
            _anchor = self._tool_anchor_pos(tool_call_id)
            _order = self._tool_call_order.get(tool_call_id) or 0
            _base = float(_count_think_tool_prefix(self._content_data, _anchor))
            _order_value_js = f"{_base + _order * 0.001:.3f}"
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
            tool_target = "content-placeholder" if tool_name in _edit_tools() else self.viewer._tool_target_id

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
                        // 🆕 方案 D：替换流式块时继承原 data-order（JS 注入块的排序位置），
                        // 避免完成态块丢失 data-order 后在 reorganizeContent 中 getPos=1e9
                        // 沉底，导致"思考在前、工具在后"的顺序错乱。
                        var _odOld = existing.getAttribute('data-order');
                        var _wrap = document.createElement('div');
                        _wrap.innerHTML = {safe_html};
                        var _newBlock = _wrap.firstElementChild;
                        if (_newBlock && existing.parentNode) {{
                            if (_odOld) {{
                                _newBlock.setAttribute('data-order', _odOld);
                            }} else {{
                                _newBlock.setAttribute('data-order', {_order_value_js});
                            }}
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
                        // 🆕 方案 D：原地更新保留原 data-order；缺失时注入（兜底）
                        if (!existing.getAttribute('data-order')) {{
                            existing.setAttribute('data-order', {_order_value_js});
                        }}

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
                    // 区域独立 II：完成块替换是纯工具区更新 → bodyOnly 不碰正文容器
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody(true);
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody(true);
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
                if (_newBlock) {{
                    // 🆕 方案 D：追加完成块时注入 data-order（与流式注入同口径），
                    // 保证下次 reorganizeContent 排序能回到正确位置而非恒沉底。
                    _newBlock.setAttribute('data-order', {_order_value_js});
                    tc.appendChild(_newBlock);
                }}
                // 🐛 修复：追加新块后同步滚动 document.body，替换旧的 tc.scrollTop
                // 区域独立 II：追加完成块是纯工具区更新 → bodyOnly 不碰正文容器
                window._suppressScrollEvent = true;
                if (!window._userScrolledWithin) {{
                    _autoScrollStreamingBody(true);
                }} else {{
                    var _bd2 = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                    if (_bd2 < {AUTO_SCROLL_THRESHOLD}) {{
                        _autoScrollStreamingBody(true);
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
            # [B2] 工具 DOM 已被 JS 增量注入 → 标记脏，下一次 _perform_update 必须走
            # save/restore 保护（否则 updateContent 整块替换会抹掉 JS 注入的工具块）。
            # 代际递增：使在途渲染回调放弃清除（防止旧回调误清新 dirty）。
            # 结果块已注入 → 该工具从 pending 移除：markdown 已含结果块，后续全量
            # 渲染可由 markdown 重新生成，不再依赖 save/restore 保护。
            try:
                self.viewer._tool_dom_dirty = True
                self.viewer._tool_dom_dirty_gen = getattr(self.viewer, "_tool_dom_dirty_gen", 0) + 1
                pending = getattr(self.viewer, "_injected_pending_tools", None)
                if pending is not None:
                    pending.discard(tool_call_id)
            except Exception:
                pass
            self.viewer.page().runJavaScript(js_code)
        except Exception as e:
            logger.warning(f"增量工具块注入失败: {e}")
        # 🆕 F2（S1 归位兜底）：最后一个工具完成时关闭坞态。
        # 流式文本可能先于工具结果结束（finish_streaming(keep_dock=True) 保留了坞态），
        # 此处是归位时机：所有已登记工具都完成 → 工具区从坞态沉底回到顶部。
        # ⚠️ 必须 hasattr 守卫：stub viewer（测试桩）无 _sync_streaming_dock 方法。
        # 🐛 F3（#R1 P1）：归位判据必须用 MessageCard 层 self._streaming——
        # 本函数中段「就近恢复 viewer 流式模式」（L8737）在 viewer._streaming=False 时
        # 无条件置 True，viewer 层状态已被污染，恒 True → 归位兜底永不触发
        # （会话末轮 dock 永久沉底）。self._streaming 由 start/stop_streaming_anim
        # 管理（本轮流式结束后 stop_streaming_anim 已置 False；新一轮开始置 True 时
        # 正确跳过归位），不受 8738 行恢复逻辑影响。
        # 🐛 F3（次要提示 1）：lambda 捕获动态属性判空——0ms 内 viewer 被 cleanup
        # 置 None 时避免 AttributeError traceback。
        try:
            if (
                self.viewer is not None
                and hasattr(self.viewer, "_sync_streaming_dock")
                and not self._streaming
                and not self._has_active_tools()
            ):
                # F2（S1 兜底归位）+ 简洁模式折叠：最后一个工具完成时归位，
                # 并与 finish_streaming 路径一致地收起工具与思考区。
                # getattr 兜底：Qt 渲染器（markdown_block_viewer）无
                # _auto_collapse_tool_section，折叠由其 exit_dock 完成。
                def _dock_off_and_collapse() -> None:
                    if self.viewer is None:
                        return
                    self.viewer._sync_streaming_dock(False)
                    getattr(self.viewer, "_auto_collapse_tool_section", lambda: None)()

                QTimer.singleShot(0, _dock_off_and_collapse)
        except Exception:
            pass

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
        # 🆕 Bug B：新块成为当前活动思考块。append_reasoning 只追加到它，
        # 避免后续 reasoning 内容合并进已完成的旧思考块导致多轮思考堆积顶部。
        self._active_thinking_block = self._content_data[-1]
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
            # 🐛 修复（编辑工具框运行中消失）：preview 相同不重新注入，但 DOM 中
            # 运行框仍在 → 仍需 dirty 保护标记。否则 dirty 被某次渲染回调清除后，
            # 该工具框永远失去 save/restore 保护，下一次全量渲染裸 updateContent
            # 抹掉它，直到 append_tool_result 才重现（"运行中→完成"中间消失）。
            # 代际递增防止"在途渲染回调误清本标记"。
            try:
                self.viewer._tool_dom_dirty = True
                self.viewer._tool_dom_dirty_gen = getattr(self.viewer, "_tool_dom_dirty_gen", 0) + 1
            except Exception:
                pass
            return
        if not hasattr(self, "_tool_streaming_preview_cache"):
            self._tool_streaming_preview_cache = {}
        self._tool_streaming_preview_cache[_cache_key] = preview_content

        # 🐛 修复（编辑工具框运行中消失）：dirty 标记必须**先于** _schedule_render
        # 设置。completed=True 时 _schedule_render(immediate=True) 会立即执行
        # _perform_update，若此时 dirty 还是旧值（False），该渲染判定
        # _needs_save_restore=False → 裸 updateContent 抹掉旧运行框（新完成态块
        # 尚未注入），产生"运行框闪灭"。
        # pending 集合：该工具结果未 append_tool_result → 运行框/预览框只在 DOM，
        # 不在 markdown → 全量渲染必须 save/restore 保护（_clear_tool_dom_dirty_guarded
        # 据此阻止 dirty 清除）。
        try:
            self.viewer._tool_dom_dirty = True
            self.viewer._tool_dom_dirty_gen = getattr(self.viewer, "_tool_dom_dirty_gen", 0) + 1
            self.viewer._injected_pending_tools = getattr(self.viewer, "_injected_pending_tools", set())
            if not completed or tool_call_id not in getattr(self, "_finished_streaming_ids", set()):
                self.viewer._injected_pending_tools.add(tool_call_id)
        except Exception:
            pass

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
            _stream_target = "content-placeholder" if tool_name in _edit_tools() else self.viewer._tool_target_id

            # 🆕 方案 D：计算工具块的 data-order（与 reorganizeContent 的 posMap 同尺度），
            # 使 JS 注入的流式块在下次全量渲染排序时能回到正确位置，而非恒沉底
            # （"所有思考在前、所有工具在后"的根因）。锚点 = 工具调用时 _content_data
            # 长度；基准 = 锚点前 think/tool 块计数；同锚点多工具按启动序号细分。
            # 🆕 Bug B 方案 F：用块引用锚点定位稳定位置（int 索引在列表偏移后失效）。
            _anchor = self._tool_anchor_pos(tool_call_id)
            _order = self._tool_call_order.get(tool_call_id) or 0
            _base = float(_count_think_tool_prefix(self._content_data, _anchor))
            _order_value_js = f"{_base + _order * 0.001:.3f}"

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
                        // 区域独立 II：状态更新是纯工具区更新 → bodyOnly
                        window._suppressScrollEvent = true;
                        if (!window._userScrolledWithin) {{
                            _autoScrollStreamingBody(true);
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
                    // 区域独立 II：预览内容更新是纯工具区更新 → bodyOnly
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody(true);
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody(true);
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
                    if (block) {{
                        // 🆕 方案 D：注入 data-order，供 reorganizeContent 排序定位
                        // （JS 注入块不在 #content-placeholder 中，无 posMap 记录，
                        // 无 data-order 会 getPos=1e9 恒沉底 → 思考/工具不交错）。
                        block.setAttribute('data-order', {_order_value_js});
                        tc.appendChild(block);
                    }}
                    // 🐛 修复：追加新块后 body 自动滚底，替换旧的 tc.scrollTop
                    // 区域独立 II：新流式块追加是纯工具区更新 → bodyOnly
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody(true);
                    }} else {{
                        var _bd2 = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd2 < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody(true);
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
            # [B2] 工具 DOM 已被 JS 增量注入 → 标记脏（已在函数开头 _schedule_render
            # 之前统一设置并维护 pending，此处不再重复设置——避免与开头逻辑分叉）。
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
        # 🆕 Bug B：优先使用当前活动思考块（start_new_thinking_block 创建的新块），
        # 避免工具调用时误绑定到更早的已完成思考块（多轮思考堆积的根因之一）。
        last_block = self._active_thinking_block if isinstance(self._active_thinking_block, dict) else None
        if last_block is None:
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
        # 🆕 Bug B：思考完成 → 活动块置空，后续 reasoning 不再追加到此块
        # （下一轮思考由 start_new_thinking_block 创建新块并重新登记）。
        self._active_thinking_block = None
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
                        // 🆕 方案 C：若替换后的 think-block 仍在 #content-placeholder
                        // （未被 reorganizeContent 迁移，如渲染节流/坞态切换），
                        // 立即迁移到 #tool-content，根治"思考框跑出折叠框"
                        // （正文区出现孤立思考框、折叠框内思考缺失）。
                        if (newBlock.parentNode === document.getElementById('content-placeholder')) {{
                            var _tc = document.getElementById('tool-content');
                            if (_tc) {{
                                _tc.appendChild(newBlock);
                            }}
                        }}
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

    # ── 任务列表（卡片底部内嵌 todo 区，替代原悬浮卡片）──

    def update_todo_list(self, todos):
        """更新卡片底部任务列表

        Args:
            todos: [{status: pending|in_progress|completed, content: str, priority: ...}, ...]
                   空列表 → 隐藏任务区。
        """
        self._todos_snapshot = list(todos or [])
        self._push_todo_list()

    def _push_todo_list(self):
        """把 _todos_snapshot 推送到 viewer 内的 #todo-section

        viewer 未创建（懒加载）/ JS 未就绪时仅写 viewer._pending_todos，
        由 viewer 创建点或 _on_js_ready 兜底补推。
        """
        v = self.viewer
        if v is None:
            return
        # 灰度：纯 Qt viewer 走原生任务列表面板
        # 延迟导入：未开启灰度时 _MarkdownBlockViewerCls 为 None，
        # 此时 viewer 必然是 CodeWebViewer，跳过判断即可。
        _qt_cls = _MarkdownBlockViewerCls
        if _qt_cls is not None and isinstance(v, _qt_cls):
            v.update_todo_list(self._todos_snapshot or [])
            return
        if not isinstance(v, CodeWebViewer):
            return
        v._pending_todos = self._todos_snapshot
        if not getattr(v, "_is_js_ready", False):
            return
        try:
            payload = [
                {
                    "status": item.get("status", "pending") if isinstance(item, dict) else "pending",
                    "content": escape(item.get("content", "") if isinstance(item, dict) else str(item)),
                    # 优先级：high/medium/low（来自 todowrite 工具 _normalize_todos 默认 medium）
                    "priority": (item.get("priority", "medium") if isinstance(item, dict) else "medium") or "medium",
                }
                for item in (self._todos_snapshot or [])
            ]
            data = json.dumps(payload).decode("utf-8")
            v.page().runJavaScript(f"window._updateTodoList && window._updateTodoList({data});")
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
        # 🆕 Bug B：首次见 tool_call_id 记录插入锚点 = 工具调用发生时 _content_data 的长度。
        # append_tool_result 用 insert(锚点) 把结果插回工具调用发生的位置（而非恒末尾），
        # 保持"思考→工具→正文→工具→正文"交错顺序。同锚点多工具按启动序号保序。
        # 🆕 Bug B 方案 F：同时记录**块引用**锚点。int 索引（len）在后续其他工具结果
        # 插入 / 思考块追加后失效（列表偏移），引用在列表增删中保持稳定，
        # index(ref)+1 恒等于"工具调用时刻的逻辑末尾"——这是数据层正确顺序的关键。
        if tool_call_id not in self._tool_insert_anchors:
            self._tool_insert_anchors[tool_call_id] = (
                len(self._content_data) if isinstance(self._content_data, list) else 0
            )
            if isinstance(self._content_data, list) and self._content_data:
                self._tool_anchor_refs[tool_call_id] = self._content_data[-1]
            self._tool_call_order[tool_call_id] = len(self._tool_call_order)
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
        # 块从 DOM 移除 → 该工具不再需要 save/restore 保护（pending 移除）
        try:
            pending = getattr(self.viewer, "_injected_pending_tools", None)
            if pending is not None:
                pending.discard(tool_call_id)
        except Exception:
            pass
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
        # 🆕 Bug B：只追加到当前活动思考块（start_new_thinking_block 创建的新块）。
        # 兜底：无活动块时查找最后一个 reasoning block（兼容未走 start_new_thinking_block
        # 的流路径），仍未找到才新建块。活动块是 dict 对象引用，即使中间被工具结果
        # 锚点 insert 挤动列表位置，引用仍有效。
        _target_block = self._active_thinking_block if isinstance(self._active_thinking_block, dict) else None
        if _target_block is None:
            for i in reversed(range(len(self._content_data))):
                if self._content_data[i].get("type") == "reasoning":
                    _target_block = self._content_data[i]
                    break

        if _target_block is not None:
            # 找到已有的（活动）思考块，追加内容
            _target_block["content"] = (_target_block.get("content", "") or "") + text
        else:
            # 未找到，新增 reasoning 块
            self._content_data.append({"type": "reasoning", "content": text})
        self._reasoning_total_len += len(text)

        if not self._lazy_rendered or not self.viewer:
            self._pending_content = self._content_data
            return

        # 🔧 不设置 _content_just_loaded：思考流式更新不应触发外部消息列表滚动，
        # 仅 #tool-content 内部自动滚底（见 JS 注入代码）。与 _inject_tool_streaming_html
        # 行为一致——工具与思考区是卡片内部独立滚动容器，正文区未更新时外部滚动条
        # 不应被强制拉底（用户在阅读正文时会被打断）。
        #
        # 🆕 方案B：首个 reasoning chunk 渲染"深度思考中..." spinner，后续静默累积
        # 不更新 DOM / 不触发渲染定时器 / 不更新高度，等 thinking 结束后的全量渲染
        # （由 append_text / finish_streaming / _maybe_finish_thinking_for_tool 触发）一并处理
        if not self.viewer._reasoning_streaming_started:
            self.viewer._reasoning_streaming_started = True
            # 🐛 修复：仅在新 reasoning 真正开始接收内容时才重置 _thinking_finalized。
            # start_new_thinking_block 不再重置此标志，防止两轮之间的空窗期
            # 已完成 think-block 的 </think> 被错误剥离为 think-streaming。
            self.viewer._thinking_finalized = False
            # 首 chunk：立即全量渲染显示 spinner。_schedule_render 会触发高度报告，
            # 由 _on_message_card_height_changed 走"流式首屏"语义统一滚底——这与正文
            # 首次到达场景一致（用户期待滚底跟随）。
            # 不调用 _update_thinking_incremental：原方法会主动 reportHeightDebounced
            # 并设置 _content_just_loaded，导致外部 chat_scroll_area 在正文未更新时被强制
            # 滚底，破坏阅读。首 chunk 的全量渲染已自然带高度报告，无需额外触发。
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

        注意：本方法已被 append_reasoning 首 chunk 路径不再调用（保留为内部辅助函数，
        供未来增量思考场景使用）。不在此设置 _content_just_loaded，也不主动报告
        高度——避免外部 chat_scroll_area 因思考区内部高度变化被强制滚底，破坏正文阅读。
        """
        if not hasattr(self.viewer, "page"):
            return

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

    def finish_streaming(self, history: bool = False):
        """流式结束收尾。

        Args:
            history: True 表示历史会话加载收尾（非流式渲染路径）。
                此时跳过 stop_streaming_anim()——它会置 _streaming_finished=True，
                使 ensure_rendered 把卡片误判为"本轮已结束的流式消息"
                （_is_history=False → 工具与思考不折叠、正文按流式坞态限高），
                与"历史会话默认折叠"的产品预期冲突。历史卡片从未启动过
                流式动画，跳过 stop_streaming_anim 无副作用。
        """
        try:
            if self.viewer is not None and hasattr(self.viewer, "finish_streaming"):
                self.viewer.finish_streaming(keep_dock=False if history else self._has_active_tools())
                if hasattr(self.viewer, "_cleanup_render_cache"):
                    self.viewer._cleanup_render_cache()
                # 简洁模式：坞态归位后自动折叠工具与思考区。keep_dock=True
                # （文本先于工具结束，S1）时保留坞态不折叠，等最后一个工具
                # 完成时由 append_tool_result 兜底归位处折叠。singleShot(0)
                # 等本函数尾部的 stop_streaming_anim 先把流式块标完成。
                # hasattr 守卫：stub viewer（测试桩）无该方法。
                if not history and not self._has_active_tools() and hasattr(self.viewer, "_auto_collapse_tool_section"):
                    QTimer.singleShot(
                        0,
                        lambda: self.viewer._auto_collapse_tool_section() if self.viewer is not None else None,
                    )
        except RuntimeError:
            pass
        if history:
            self._streaming = False
        else:
            self.stop_streaming_anim()

    def _has_active_tools(self) -> bool:
        """是否有仍在执行中的工具（已登记但未完成）。

        dock 状态机的判据：只要还有工具在运行（流式文本已结束但工具结果未全部
        到达，S1 场景），工具区应保持坞态沉底；全部完成后才归位。
        """
        return any(tid not in self._finished_streaming_ids for tid in self._tool_call_order)

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
            except TypeError, RuntimeError:
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
        # [B4-强回收] 防悬挂：MessageCard 清理时同步清零 renderer PID
        self._renderer_pid = 0
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
        # 🐛 F3（次要提示 2）：工具登记集合随卡片销毁清空——否则边缘场景
        # （cleanup 后 _has_active_tools() 被误调）会因残留登记误判"仍有活跃工具"。
        self._tool_call_order.clear()

        # [PERF] preview 期间累积的 viewer 高度目标清理
        self._pending_viewer_height = None
        self._resize_preview_mode = False
        self._resize_preview_height = 0

        # 清理 markdown_cache 如果存在
        if hasattr(self, "_markdown_cache") and self._markdown_cache:
            self._markdown_cache.clear()
            self._markdown_cache = None

    def closeEvent(self, e):
        self.cleanup()
        super().closeEvent(e)


def resolve_initial_welcome_mode(saved_mode: str, saved_plugin_tab: str, registered_tabs: dict) -> str:
    """解析欢迎卡片初始 mode：上次选中的插件 tab 仍注册时优先，否则回退内置 mode

    - saved_plugin_tab: 配置里记忆的插件 mode_key（插件可能被卸载/停用）
    - registered_tabs: 当前 UIPluginRegistry 已注册的插件 tabs（dict，key 为 mode_key）
    """
    if saved_plugin_tab and saved_plugin_tab in registered_tabs:
        return saved_plugin_tab
    return saved_mode


def create_welcome_card(
    parent=None,
    agent_name: str = "",
    agent_description: str = "",
    recent_sessions: list = None,
    top_by_count: list = None,
    mode: str = "sessions",
    context_provider: Optional[Callable[[], Dict[str, Any]]] = None,
) -> MessageCard:
    """创建欢迎卡片

    Args:
        parent: 父控件
        agent_name: 当前智能体名称
        agent_description: 智能体描述
        recent_sessions: 最近的历史会话列表，每项包含 title, last_time, session_id, message_count
        top_by_count: 消息最多的会话列表，每项包含 title, last_time, session_id, message_count
        mode: 欢迎卡片模式（sessions / changelog / 插件注册 tab）
        context_provider: 窗口上下文提供者（无参回调 → dict）。多窗口隔离：
            渲染插件 tab 时注入当前窗口的 project_root / project_name /
            window_id，避免插件回读全局状态导致多标签页内容串项目。
    """
    card = MessageCard(role="welcome", timestamp="就绪", parent=parent)
    # 一次性把数据 + 模式交给卡片：tabs 在 PyQt 层；body 由卡片内部渲染
    card.set_welcome_content(
        recent_sessions=recent_sessions,
        top_by_count=top_by_count,
        mode=mode,
        context_provider=context_provider,
    )
    return card


def _render_welcome_body(
    mode: str,
    recent_sessions: list,
    top_by_count: list,
    window_context: Optional[dict] = None,
    suppress_anim: bool = False,
) -> str:
    """渲染欢迎卡片 body（不含标题和 tabs）；按 mode 分发

    Args:
        mode: 欢迎卡片模式（sessions / changelog / 插件注册 tab）
        recent_sessions: 最近会话列表
        top_by_count: 最活跃会话列表
        window_context: 当前窗口的 UI 上下文（project_root / project_name /
            window_id / session_id 等）。多窗口隔离的关键：注入插件 render_func，
            保证每个窗口渲染自己项目的内容，避免插件回读全局状态串项目。

    注意：**不缓存 render_func 结果**。部分插件 tab 是异步采集模式——首次渲染
    返回「加载中」占位，后台采集完成后再次调用 render_func 返回真实图表；
    缓存占位内容会导致数据永远不显示（project-dashboard 踩坑）。
    插件如需缓存应在自己内部做（如 collector 数据缓存），主程序不越俎代庖。
    """
    if mode == "changelog":
        return _render_changelog_body()
    # 插件注册的欢迎 tab：render_func 返回 HTML 片段，走现有 markdown 管线
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        tab = UIPluginRegistry.get_instance().get_welcome_tabs().get(mode)
        if tab is not None:
            # 注入主题上下文：插件 HTML 拿不到 Qt 主题，必须由主程序传入。
            # prefers-color-scheme 跟随 OS 而非 Qt 主题（theme_manager），
            # 单独依赖它会导致 Qt 暗色 + OS 亮色时不生效。
            try:
                from app.utils.theme_manager import theme_manager

                is_dark = not theme_manager.is_light_theme()
            except Exception:
                is_dark = False
            # 窗口上下文合并进插件 ctx（window_context 自带 is_dark，覆盖兜底值）
            ctx = {"is_dark": is_dark}
            if window_context:
                ctx.update(window_context)
            return tab.render_func(ctx) or ""
    except Exception:
        pass
    return _render_sessions_body(recent_sessions, top_by_count, suppress_anim=suppress_anim)


_SESSION_ROWS = 3  # 双列网格行数（每分类显示 3×2 = 6 张）
_SESSION_COLS = 2


def _render_sessions_body(recent_sessions: list, top_by_count: list, suppress_anim: bool = False) -> str:
    """渲染会话导览 body：最近 / 最活跃两个卡片双列网格（每分类 3 行）

    每张卡片：左侧图标徽章 + 标题/副标题 + hover 滑入箭头。
    复用 .context-tag 点击事件链（data-type="session" + data-session-id），
    仅替换视觉外观，JS 拦截逻辑不变。
    """

    def _render_item(s: dict, count_mode: bool, idx: int, suppress_anim: bool = False) -> str:
        """渲染单个会话卡片；idx 用于 stagger 动画延迟

        suppress_anim=True（软刷新：其他标签页会话变更广播到本窗口）时用内联
        `animation:none` 覆盖 CSS 进入动画，避免列表项重播 stagger fade-in。
        """
        t = escape(s.get("title", "未命名会话"))
        sid = escape(s.get("session_id", ""))
        if count_mode:
            mc = s.get("message_count", 0)
            meta = f"{mc} 条消息"
            icon = "⚡"
        else:
            meta = escape(s.get("last_time") or "")
            icon = "💬"
        anim_style = "animation: none;" if suppress_anim else f"animation-delay:{idx * 55}ms"
        return (
            f'<div class="context-tag session-item" data-type="session" '
            f'data-session-id="{sid}" data-action="session" '
            f'style="{anim_style}">'
            f'<span class="session-item-badge">{icon}</span>'
            f'<span class="session-item-body">'
            f'<span class="session-item-title">{t}</span>'
            f'<span class="session-item-meta">{meta}</span>'
            f"</span>"
            f'<span class="session-item-arrow">›</span>'
            f"</div>"
        )

    def _render_section(
        title: str, icon: str, items: list, count_mode: bool = False, start_idx: int = 0, suppress_anim: bool = False
    ) -> str:
        """渲染单个分类 section；items 为空则返回空串

        start_idx: 全局连续卡片序号起点，保证跨分区的 stagger 动画连贯
        （否则两个分区各自从 0 开始，动画同时播放显得凌乱）。
        """
        if not items:
            return ""
        shown = items[: _SESSION_ROWS * _SESSION_COLS]
        rows = "".join(_render_item(s, count_mode, start_idx + i, suppress_anim) for i, s in enumerate(shown))
        return (
            f'<div class="session-section">'
            f'<div class="session-header">'
            f'<span class="session-header-icon">{icon}</span>'
            f'<span class="session-header-title">{title}</span>'
            f'<span class="session-header-count">{len(shown)}</span>'
            f"</div>"
            f'<div class="session-list">{rows}</div>'
            f"</div>"
        )

    recent_block = _render_section(
        "最近会话", "📅", recent_sessions, count_mode=False, start_idx=0, suppress_anim=suppress_anim
    )
    top_start = len(recent_sessions[: _SESSION_ROWS * _SESSION_COLS])
    top_block = _render_section(
        "最活跃会话", "🔥", top_by_count, count_mode=True, start_idx=top_start, suppress_anim=suppress_anim
    )
    if not (recent_block or top_block):
        return '<div class="welcome-empty">还没有历史会话，开始第一次对话吧 ✨</div>'
    return recent_block + top_block


def _render_changelog_body(releases: list = None, loading: bool = False, error_msg: str = "") -> str:
    """渲染 changelog body：左列版本列表 + 右列描述（SPA，JS 切换不调 Python）

    Args:
        releases: 已加载的 release 列表（每项 {tag_name, name, body_html, published_at, html_url}）
        loading: True 时显示 loading 占位
        error_msg: 错误信息（网络失败等）
    """
    if error_msg:
        return f'<div class="welcome-empty">⚠️ 加载更新日志失败：{escape(error_msg)}<br><span style="opacity:0.7">检查网络后切换 mode 重试</span></div>'

    if loading or not releases:
        return '<div class="welcome-empty">📜 正在从 GitHub Releases 拉取更新日志...</div>'

    # 左列版本列表 + 右列描述（首条默认显示）
    items = []
    bodies = []
    for i, r in enumerate(releases[:20]):
        tag = escape(r.get("tag_name") or r.get("name") or f"v{i + 1}")
        date = escape((r.get("published_at") or "")[:10])
        body_html = r.get("body_html") or "<em>无更新说明</em>"
        active = "active" if i == 0 else ""
        # body 用 data-attr 存（HTML 字符串），切换时直接读 attr 替换右列
        items.append(
            f'<li class="changelog-version {active}" data-idx="{i}">'
            f'<div class="ver-tag">{tag}</div>'
            f'<div class="ver-date">{date}</div></li>'
        )
        bodies.append(
            f'<div class="changelog-body" data-idx="{i}" style="{"display:block" if i == 0 else "display:none"}">{body_html}</div>'
        )

    return (
        '<div class="changelog-shell">'
        f'<ul class="changelog-versions">{"".join(items)}</ul>'
        f'<div class="changelog-detail">{"".join(bodies)}</div>'
        "</div>"
    )


# ───── 欢迎卡片 changelog 异步加载 ─────
_CHANGELOG_REPO = "martin98-afk/DriFox"
_CHANGELOG_CACHE_TTL = 3600  # 1h
_changelog_cache: dict = {}  # in-memory: {releases: [...], fetched_at: float, etag: str}


class _ChangelogFetcher(QThread):
    """后台拉 GitHub Releases；走完 emit finished(list) 或 error(str)"""

    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, etag: str = "", parent=None):
        super().__init__(parent)
        self._etag = etag

    def run(self):
        try:
            import httpx
        except ImportError:
            self.error.emit("缺少 httpx 依赖")
            return
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._etag:
            headers["If-None-Match"] = self._etag
        url = f"https://api.github.com/repos/{_CHANGELOG_REPO}/releases?per_page=5"
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0)) as client:
                resp = client.get(url, headers=headers)
        except Exception as e:
            self.error.emit(f"网络错误：{e}")
            return
        if resp.status_code == 304:
            # 缓存仍新鲜（带 etag 才可能命中；用 fetched_at 判断也兜底）
            self.finished.emit([])
            return
        if resp.status_code != 200:
            self.error.emit(f"GitHub API {resp.status_code}：{resp.text[:120]}")
            return
        try:
            data = resp.json()
        except Exception as e:
            self.error.emit(f"解析失败：{e}")
            return
        new_etag = resp.headers.get("ETag", "")
        releases = []
        # 线程私有实例：全局 _md_instance 禁止跨线程使用（Markdown.reset()/convert()
        # 非线程安全）。本方法跑在 QThread 后台线程，若与主线程消息渲染并发共用全局
        # 实例，会互相打乱解析状态——曾致消息卡片表格偶发渲染失败/内容串扰（约0.4%）。
        md = Markdown(
            extensions=["fenced_code", "nl2br", "tables"],
            output_format="html5",
            safe=False,
        )
        for item in data:
            body_md = item.get("body") or ""
            try:
                body_html = md.convert(body_md)
            except Exception:
                body_html = escape(body_md).replace("\n", "<br>")
            md.reset()
            releases.append(
                {
                    "tag_name": item.get("tag_name", ""),
                    "name": item.get("name", ""),
                    "body_html": body_html,
                    "published_at": item.get("published_at", ""),
                    "html_url": item.get("html_url", ""),
                }
            )
        self.finished.emit([{"releases": releases, "etag": new_etag}])
