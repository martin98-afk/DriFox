# -*- coding: utf-8 -*-
"""消息卡片渲染管线（自 message_card.py 拆出，纯搬移）。

markdown → HTML、think/tool 块注入、代码高亮、sanitize、vendor 脚本管理
等模块级渲染基础设施；MessageCard 与 card_viewers 共用。
"""

import base64
import concurrent.futures
import hashlib
import os
import random
import re
import sys
import threading
from collections import OrderedDict
from functools import lru_cache
from html import escape, unescape
from typing import Any, Dict, List, Optional
import orjson as json
from loguru import logger
from markdown import Markdown
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from PyQt5.QtCore import (
    QByteArray,
    QEasingCurve,
    QElapsedTimer,
    QObject,
    QRect,
    QSize,
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
from app.utils.design_tokens import (
    Animations,
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
from app.widgets.custom_title_bar import CustomTabButton, TabHoverSyncHost, TabIndicatorController
from app.widgets.flow_layout import FlowLayout
from app.widgets.modules.message_bubble import MessageBubble, ensure_bubble_contrast

# 懒渲染占位 QSS（welcome / assistant 两条路径共用，T35 去重）：
# 依赖 scale_font_size / get_font_family_css，故在 import 之后求值。
_PLACEHOLDER_QSS = f"color: #888888; font-size: {scale_font_size(14)}px; padding: 8px; {get_font_family_css()}"
# resize 占位幽灵框 QSS（两处共用）
_RESIZE_GHOST_QSS = """
                QFrame {
                    background: rgba(255,255,255,0.035);
                    border: 1px dashed rgba(255,255,255,0.08);
                    border-radius: 12px;
                }
                """

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


# Qt 的「无限制」尺寸常量（QWIDGETSIZE_MAX），message_card 内多处高度钳制共用
_QWIDGETSIZE_MAX = 16777215


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
    _reg_metadata_flag,
    get_tool_qrc_prefix,
    render_tool_block,
)
from app.widgets.render_crash_queue import RenderCrashQueue
from app.utils.session_preview import format_relative_time
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
# 内容段两条硬约束（防误匹配吞正文，详见 _ask_content_ok）：
#   1. 禁止再出现 <ask>（防嵌套标签把两段之间的正文一起吃掉）
#   2. 长度封顶 _ASK_MAX_CONTENT_LEN（追问是一句话，跨段落的超长"内容"必是误匹配）
_ASK_MAX_CONTENT_LEN = 120
_ASK_CONTENT_BODY = rf"(?:(?!<ask>).){{0,{_ASK_MAX_CONTENT_LEN}}}?"
_ASK_TAG_PATTERN = re.compile(rf"<ask>({_ASK_CONTENT_BODY})</ask>", re.DOTALL)
# 追问收拢：摘除正文里的 ask 标签（连带行内多余空白），改由末尾区块统一渲染
_ASK_STRIP_PATTERN = re.compile(rf"[ \t]*<ask>({_ASK_CONTENT_BODY})</ask>[ \t]*", re.DOTALL)
# 协议块（思考 / 工具）：摘除发生在 think/tool 注入之前，块内文本不当正文处理，
# 其中的 <ask> 字面量不参与收拢（否则会一路吃到文末真追问，把块尾与正文一起摘走）
_ASK_PROTO_BLOCK_PATTERN = re.compile(r"<(think|tool)\b[^>]*>.*?(?:</\1>|\Z)", re.DOTALL | re.IGNORECASE)
# 补充保护区间：~~~ 围栏与行内代码（``` 围栏由 _CODE_BLOCK_PATTERN 覆盖）
_ASK_TILDE_FENCE_PATTERN = re.compile(r"~~~[^\n]*\n.*?~~~", re.DOTALL)
_ASK_INLINE_CODE_PATTERN = re.compile(r"`[^`\n]{1,80}`")
# 摘除后可能残留的空列表项（"-" 后无内容）
_ASK_EMPTY_BULLET_PATTERN = re.compile(r"^[ \t]*[-*+][ \t]*$", re.MULTILINE)
# 追问区块最多展示条数（模型通常给 1~3 条，超量截断避免卡片尾部过长）
_ASK_MAX_ITEMS = 4
# 追问模板占位词：stop 追问预测 hook 的提示词里出现过「<ask>原话</ask>」这类示例，
# 模型偶尔照抄字面输出成一个假追问项。渲染阶段兜底丢弃，历史消息同样受益。
_ASK_PLACEHOLDER_TEXTS = frozenset({"原话", "追问", "问题", "xxx", "某问题"})
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

# ======== 流式态单色 tint（替代原彩虹循环色板）========
# 旧 _RAINBOW_NORMAL 为 10 色高饱和绕卡循环，色相跳变（青→紫→粉→橙→绿）
# 过于抢眼；现流式指示统一走单色（accent / 警示红），运动只保留底部光块。
_STREAM_TINT_RETRY = "#ff2222"

# ======== 流式底部光块运动参数 ========
# 单程时长(ms)：往返一趟 = 2 × _STREAM_BAND_SWEEP_MS
_STREAM_BAND_SWEEP_MS = 1600.0
# 单拍最大推进(ms)：主线程被内容渲染占满时 Qt 定时器会延迟触发，若按真实 dt
# 全额推进，恢复后光块会一次瞬移到新位置（观感「跳变」）。钳到一帧多的量，
# 掉帧只表现为动画变慢，不瞬移、也不在恢复时集中补帧（观感「卡住后猛冲」）。
_STREAM_BAND_MAX_DT_MS = 100.0
_STREAM_BAND_H = 3  # 光块高度(px)
_STREAM_BAND_BOTTOM = 3  # 光块距卡片底边(px)
# 动画帧脏区在光块上下的纵向余量(px)。必须让脏区保持在内壁描边**之上**：
# 脏区一旦覆盖底部描边，重绘会先擦掉该带内的描边、而窄带重绘只画光块，
# 描边就在「整卡重绘画出」与「动画帧擦掉」之间反复 —— 表现为下边框闪烁。
_STREAM_BAND_REPAINT_PAD = 1

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

# ======== 结束态卡片高度过渡（默认开，出问题可一键关）========
# 背景：流式结束时卡片高度会从"坞态限高"收敛到自然高度（常是数百 px 的突变），
# 原来 _update_height 对大 delta 直接 snap（noContainerAnimation），外层滚动区
# 跟着瞬移 —— 页面内的位移已被 FLIP 补间，唯独 Qt 侧这一跳没动画。
# 这里在"结束态窗口"内改用既有的 _height_anim 做缓动（时长走全局 token，
# 与 JS 侧 FLIP 的 220ms 完全一致，Qt 侧与页内补间同拍收尾）。
# 关闭方式：环境变量 DRIFOX_FINISH_HEIGHT_ANIM=0，或运行时
# set_finish_height_anim_enabled(False)。
FINISH_HEIGHT_ANIM_ENABLED = os.environ.get("DRIFOX_FINISH_HEIGHT_ANIM", "1") != "0"
FINISH_HEIGHT_ANIM_MS = Animations.ENTER_MS  # 与 JS 侧 FLIP 时长 220ms 对齐
FINISH_HEIGHT_ANIM_MIN_DELTA = 16  # 小于该变化不值得动画（避免噪声抖动；16≈一行正文，60px 会放过结束态的行级收敛）
FINISH_HEIGHT_ANIM_WINDOW_S = 2.0  # 结束态窗口：只覆盖结束后的高度收敛
FINISH_HEIGHT_ANIM_MAX_USES = 2  # 窗口内最多缓动几次（归位+重排、随后折叠）

# ======== 流式期「坞态预算」高度上限（起步跳变兜底）========
# 简洁模式坞态下 CSS 把正文限高 600px、工具区限高 220px，卡片总高因此天然封顶
# （真机日志实测封顶值 863px）。但坞态是 runJavaScript 异步生效的：若某条渲染
# 路径抢在它落地之前完成，正文会先按**自然高度**排版（长回复可达数千 px），
# 坞态到达后再被压回 → 起步瞬间「卡片先胀成很大再缩回」。
# 这里给流式期的上报高度加一道软上限：只在坞态未生效的竞态窗口内起作用
# （CSS 一旦生效，实测高度恒 < 本值，不会误伤），把那一次跳变压成无感。
STREAM_DOCK_BUDGET_PX = 900

# ======== 流式期高度追踪（默认开，出问题可一键关）========
# 背景：流式期间卡片高度是「台阶式落地」——上报延迟（打字机节流 + Python 侧
# 防抖）之后一次性 setFixedHeight 到目标值。文字是连续出来的，卡片高度却每
# ~160ms 蹦一格：观感是"文字先顶到卡片边缘、憋一下、再整块蹦高"。
# 实现：流式激活时开一个固定节拍的追踪 tick（STREAM_HEIGHT_TICK_MS），每拍朝
# 最新目标值按比例逼近（STREAM_HEIGHT_TRACK_FACTOR）。相比一次性 QVariantAnimation
# 补间：目标值可随时更新（天然 retarget）、无 stop/start 抖动、追踪永不打断。
# 关闭方式：环境变量 DRIFOX_STREAM_HEIGHT_ANIM=0，或运行时
# set_stream_height_anim_enabled(False)。
STREAM_HEIGHT_ANIM_ENABLED = os.environ.get("DRIFOX_STREAM_HEIGHT_ANIM", "1") != "0"
# 追踪节拍（ms）。[T28/P0-2] 曾统一为 40ms（与 JS 上报、message_card 防抖
# 同拍，消除互质漂移）。[T34] 40 → 16（≈单帧）：流式正文由打字机按 rAF
# 每 ~17ms 揭示一次，容器高度却按 40ms 一格逼近 —— 文字连续长、容器跳格，
# 底部一行反复「顶到边缘 → 憋住 → 蹦一下」。16ms 让容器与 rAF 同频，二者
# 视觉上同步生长。成本实测：setFixedHeight p50≈1ms，62 次/秒 ≈ 6% 主线程
# 占用，且回环已被 _stream_height_anim_active 上报隔离封死。
STREAM_HEIGHT_TICK_MS = 16
# 每拍逼近比例：剩余差值的 45%。0.45 → 约 5 拍（150ms）收敛 94%，慢流式下
# 观感为"卡片跟着文字匀速生长"；过小（<0.3）会明显滞后，过大（>0.7）趋近 snap。
STREAM_HEIGHT_TRACK_FACTOR = 0.45
# 自适应提速：|diff| 超过 FAR_PX 的拍次 factor 加 BOOST（上限 0.7），大台阶
# （代码块/表格整块落地）更快跟上，消除"文字顶边憋一拍再蹦高"；小台阶维持
# 基线（流式 0.45 / 结束态 0.28）防噪声抖动。
STREAM_HEIGHT_TRACK_FAR_PX = 80
STREAM_HEIGHT_TRACK_BOOST = 0.15
# 落定阈值（px）：剩余差小于它直接对齐并停 tick，避免无限趋近。
STREAM_HEIGHT_TRACK_EPSILON = 2
# 小于该变化量直接 snap：流式尾巴上的小噪声不值得起追踪（避免常开 tick 空转）
# [T34] 8 → 2：流式期实测高度变化幅度 p50=4 / max=14 —— 也就是说**大部分**
# 变化都落在 8px 阈值之下被直接 snap 成硬跳，追踪只在偶发大台阶才生效，
# 「容器台阶式蹦高」的体感正来源于此。降到 EPSILON(=2) 后凡超过落定阈值
# 的变化一律走追踪；tick 本身在收敛后即停（EPSILON 内对齐并停拍），
# 不存在「常开空转」问题（那是 MIN_DELTA 存在的历史原因，已被 epsilon
# 停拍机制覆盖）。
STREAM_HEIGHT_ANIM_MIN_DELTA = 2
# 结束态（FINISH 窗口）追踪的逼近比例：比流式更缓——结束态的高度变化
# 与页面内 CSS 过渡（200ms）/ FLIP（220ms）同量级，追踪太快会"Qt 侧先到、
# 页面还在动"的两段感，即丝绸尾音。[T34] 0.28 → 0.12：按 τ 不变重算
# （原 0.28@40ms → τ≈121ms；16ms 拍 f'=1-0.72^(16/40)≈0.12）。
FINISH_HEIGHT_TRACK_FACTOR = 0.12


def set_stream_height_anim_enabled(enabled: bool) -> None:
    """运行时开关流式期高度追踪（灰度/回滚用）。"""
    global STREAM_HEIGHT_ANIM_ENABLED
    STREAM_HEIGHT_ANIM_ENABLED = bool(enabled)


# ─── 差量收尾（流式结束不再整页重渲染）────────────────────────────────
# 现状：finish_streaming 强制全量 → `container.innerHTML = newHtml` 整页替换，
# 稳定区（流式期间已差量渲染好的段落）被一起销毁重建 → 结束瞬间整体重排闪一下，
# 工具区坞态归位也跟着跳。差量收尾改为：稳定区 DOM 不动，只把剩余段差量上去 +
# 流式思考块就地定稿（按 data-flip-key 位置键配对替换）。
# [T35] 默认**打开**（验证见 tests/debug/finalize_probe.py）：
#   ① 稳定区 DOM 保留率 100%（纯段落 / 未闭合代码块 / think 块 / 混合）；
#   ② 五个场景的最终 DOM 文本与全量终渲染**字节级一致** —— 此前不一致的
#      根因是 _render_inline_tail 缺代码块包装，已修（见 _TAIL_CODE_WRAP_MAX_CHARS）。
# 收益：结束这一拍不再整页 innerHTML 替换，稳定段落不被销毁重建 →
# 消除每轮回答结束必然发生一次的整页重排闪烁。
# 回滚：环境变量 DRIFOX_INCREMENTAL_FINALIZE=0，无需改代码。
INCREMENTAL_FINALIZE_ENABLED = os.getenv("DRIFOX_INCREMENTAL_FINALIZE", "1") != "0"


# 结束态耗时打点（默认关）：环境变量 DRIFOX_FINISH_TIMING=1 打开，
# 用于在真机定位"结束这一拍"到底卡在 render / json.dumps / 哪一段。
FINISH_TIMING_ENABLED = os.environ.get("DRIFOX_FINISH_TIMING", "0") == "1"


def set_finish_height_anim_enabled(enabled: bool) -> None:
    """运行时开关结束态高度动画（灰度/回滚用）。"""
    global FINISH_HEIGHT_ANIM_ENABLED
    FINISH_HEIGHT_ANIM_ENABLED = bool(enabled)


# 编辑类工具/子智能体/提问类工具：无论简洁模式与否，这些工具的结果始终展示在正文中
# 子智能体和提问工具（subagent_para/question）涉及 AI 与用户的直接交互，
# 留在正文中比收到工具区更符合直觉，体验更连贯。
# 集合由 registry 派生（注册时显式声明 keep_in_content=True，不硬编码工具名）：
#   write/edit/multi_edit + subagent_para + question 等
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


# ======== 流式图表骨架 ========
# 半截 ```echarts / ```mermaid fence 期间的占位。原先残缺内容会被包成真正的
# .echarts-container，JS 侧 JSON.parse / mermaid.parse 必然失败，只剩一个 400px
# 空洞（用户视角"图表区空一块"，且上一版视觉残留会闪）。骨架与真图等高
# （CSS min-height 300px），fence 闭合切真图时零布局跳动。
# class `chart-streaming` 供 JS `_chartReady` 识别：永不入 vault、永不 init。
_CHART_SKELETON_HTML = (
    '<div class="chart-skeleton chart-streaming">'
    '<div class="chart-skeleton__bars"><i></i><i></i><i></i><i></i><i></i></div>'
    '<div class="chart-skeleton__label">图表生成中…</div>'
    "</div>"
)

# ===== ```html fence 净化 =====
# 可视化协议（plugins/system-skills/skills/visualization）会产出 ```html 围栏的
# UI 效果稿 / 指标卡。此前分发器没有 html 分支 → 落到兜底被当源码高亮，
# 用户看到的是一堆 HTML 文本而不是渲染结果。
#
# 但直接内联 HTML = 任意 HTML 注入：卡片页面是 file:// 源且开了本地文件
# 访问，未净化的 <script> 能读本地磁盘。净化策略：
#   保留：普通标签、class/id/data-*、内联 style、<style> 样式表
#   移除：script / iframe / object / embed / applet / form / base / link / meta、
#         所有 on* 事件属性、javascript: 等危险协议、<style> 里的 @import
# 需要交互能力的场景走插件 fence 渲染器 + __drifoxBridge（受权限声明约束），
# 不走裸 html fence。
_HTML_STRIP_PAIR_PATTERN = re.compile(
    r"<\s*(script|iframe|object|embed|applet|form)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_STRIP_VOID_PATTERN = re.compile(
    r"<\s*(script|iframe|object|embed|applet|form|base|link|meta|portal)\b[^>]*>",
    re.IGNORECASE,
)
_HTML_EVENT_ATTR_PATTERN = re.compile(
    r"\son[a-z][a-z0-9_-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)
_HTML_JS_PROTO_PATTERN = re.compile(r"(javascript|vbscript|livescript|mocha)\s*:", re.IGNORECASE)
_HTML_DATA_HTML_PATTERN = re.compile(r"data\s*:\s*text/html", re.IGNORECASE)
_HTML_CSS_IMPORT_PATTERN = re.compile(r"@import\b[^;]*;?", re.IGNORECASE)
# 只有"真正的 HTML"才渲染：以标准 HTML 元素 / DOCTYPE / 注释开头。
# 反例：模型在 ```html 围栏里写 DriFox 协议片段（<tool>...</tool>、<think>...</think>），
# 这是协议文本而不是 UI 稿 —— 若当真实 DOM 内联，标签会被浏览器当未知元素吞掉，
# 内部字段（工具参数 JSON 等）裸露成结构化正文。回归用例见
# tests/widgets/test_message_card_diff_tail_loss.py。此类内容降级普通代码块。
_HTML_WIDGET_HEAD_PATTERN = re.compile(
    r"^\s*(?:<!doctype\b|<!--"
    r"|<(?:html|head|body|div|span|section|article|aside|header|footer|nav|main"
    r"|p|h[1-6]|ul|ol|li|table|thead|tbody|tr|td|th|figure|figcaption|dl|dt|dd"
    r"|svg|style|pre|blockquote|strong|em|a|img|button|label|details|summary|canvas)\b)",
    re.IGNORECASE,
)


def _sanitize_widget_html(raw_html: str, max_len: int = 200_000) -> str:
    """净化 ```html fence 内容，产出可直接内联的静态 HTML 片段。

    Args:
        raw_html: fence 原始内容（已解 HTML 实体）
        max_len: 长度上限，超限直接判空（调用方降级为普通代码块）

    Returns:
        净化后的 HTML 片段；空串表示超限或净化后无实质内容。
    """
    if not raw_html or len(raw_html) > max_len:
        return ""
    html = raw_html
    html = _HTML_STRIP_PAIR_PATTERN.sub("", html)
    html = _HTML_STRIP_VOID_PATTERN.sub("", html)
    html = _HTML_EVENT_ATTR_PATTERN.sub("", html)
    html = _HTML_JS_PROTO_PATTERN.sub("#", html)
    html = _HTML_DATA_HTML_PATTERN.sub("#", html)
    html = _HTML_CSS_IMPORT_PATTERN.sub("", html)
    html = html.strip()
    # 净化后只剩空标签骨架（内容全是可执行物）→ 判空，由调用方降级
    text_only = re.sub(r"<[^>]+>", "", html).strip()
    if not text_only and not re.search(r"<(img|svg|canvas|hr|br)\b", html, re.IGNORECASE):
        return ""
    return html


def _get_plugin_fence_renderer(lang: str):
    """查插件 fence 渲染器注册表；未注册 / 插件系统未就绪返回 None。

    在 B3 渲染线程池里调用：`UIPluginRegistry` 是进程级单例，只读的 dict.get
    在 GIL 下是原子操作，无需额外加锁。任何异常都吞掉并返回 None —— 消息渲染
    永远不能被插件拖垮（插件出错降级为普通代码块）。
    """
    if not lang:
        return None
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        return UIPluginRegistry.get_instance().get_fence_renderer(lang)
    except Exception:
        return None


def _render_plugin_fence(info, code_content_raw: str) -> str:
    """调用插件 render_func 渲染 fence。

    Returns:
        渲染后的 HTML 片段（带 data-fence-renderer / data-plugin-name 标记）；
        插件抛错或返回空时返回空串 → 调用方继续走内置链/降级代码块。
    """
    try:
        code = _unescape_html(code_content_raw)
    except Exception:
        code = code_content_raw
    # P2-1：fence render 入口同口径 watchdog（超时 degrade → 连续熔断停用）
    try:
        from app.core.infra.ui_callback_watchdog import timed_ui_callback

        html = timed_ui_callback(
            info.plugin_name,
            f"fence:{info.lang}",
            info.render_func,
            code,
            {"lang": info.lang, "plugin_name": info.plugin_name},
        )
    except Exception:
        return ""
    if not isinstance(html, str) or not html.strip():
        return ""
    return (
        f'<div class="plugin-fence" data-fence-renderer="{info.lang}" '
        f'data-plugin-name="{info.plugin_name}">{html}</div>'
    )


def _plugin_fence_placeholder(info, code_content_raw: str) -> str:
    """流式半截 fence 的占位：插件自定义优先，否则用宿主通用图表骨架。

    插件的 streaming_placeholder 可以是 str，也可以是 callable(半截源码) -> str。
    """
    ph = getattr(info, "streaming_placeholder", None)
    try:
        if callable(ph):
            out = ph(code_content_raw)
            return out if isinstance(out, str) and out.strip() else _CHART_SKELETON_HTML
        if isinstance(ph, str) and ph.strip():
            return ph
    except Exception:
        pass
    return _CHART_SKELETON_HTML


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

        # ===== 流式中的半截图表：骨架占位 =====
        # _sanitize_incomplete_markdown 把末尾未闭合的图表 fence 改标为
        # <lang>-streaming。这里必须在此之前拦截：残缺 JSON 若照常包成
        # .echarts-container，其 b64 每轮随内容增长而变化 → vault key 每轮都不同，
        # 节点被反复塞进 vault 迅速膨胀（多图白屏的次因）。骨架不入 vault、不 init。
        if lang in ("echarts-streaming", "mermaid-streaming", "html-streaming", "widget-streaming"):
            return _CHART_SKELETON_HTML
        # 插件注册的 fence 在流式期同样降级为占位：半截源码交给 render_func
        # 必然产出残缺节点，且内容每轮变化会让产物反复变更、高度抖动。
        if lang.endswith("-streaming"):
            _streaming_info = _get_plugin_fence_renderer(lang[: -len("-streaming")])
            if _streaming_info is not None:
                return _plugin_fence_placeholder(_streaming_info, code_content_raw)

        # ===== 插件 fence 渲染器（架构档）：先查注册表 =====
        # 命中 → 插件路径（产物带 data-fence-renderer 标记，供后续按需注入
        #        插件 assets）；未命中 → 走下方内置硬编码链。
        # 注册表禁止注册与内置同名的 lang，故不会影响 echarts/mermaid 行为。
        _fence_info = _get_plugin_fence_renderer(lang)
        if _fence_info is not None:
            _plugin_html = _render_plugin_fence(_fence_info, code_content_raw)
            if _plugin_html:
                return _plugin_html

        # ===== ECharts 代码块：渲染为交互式图表 =====
        if lang == "echarts":
            try:
                # 解码 HTML 实体（&quot; → " 等），确保 JSON 可解析
                json_text = _unescape_html(code_content_raw)
                json.loads(json_text)  # 校验：非法 JSON 降级为普通代码块，不留空洞容器
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

        # ===== SVG 代码块：透传 raw HTML，浏览器直渲 =====
        # 协议上 SVG 应内联正文（visualization skill），但模型习惯性输出 ```svg 围栏，
        # 这里兜底：语言为 svg 且内容以 <svg 开头时按 raw HTML 透传，
        # 与正文内联 <svg>（markdown safe=False）走同一条渲染路径。
        # 内容不以 <svg 开头（教学代码等）保持普通代码块渲染，避免误吞。
        if lang == "svg":
            svg_html = _unescape_html(code_content_raw)
            if svg_html.lstrip().lower().startswith("<svg"):
                return svg_html

        # ===== HTML 代码块：净化后内联（UI 效果稿 / 指标卡）=====
        # 与 svg 同层：内容不以 "<" 开头（教学代码、配置片段等）保持普通代码块，
        # 避免把讲 HTML 语法的示例代码误吞成真实 DOM。
        if lang == "html":
            try:
                raw_widget = _unescape_html(code_content_raw)
            except Exception:
                raw_widget = code_content_raw
            if _HTML_WIDGET_HEAD_PATTERN.match(raw_widget):
                widget_html = _sanitize_widget_html(raw_widget)
                # 净化后为空（内容全是脚本 / 超长 / 无实质内容）→ 降级普通代码块，
                # 不留空洞容器
                if widget_html:
                    return (
                        f'<div class="html-widget" data-fence-renderer="html" '
                        f'style="margin: 12px 0;">{widget_html}</div>'
                    )

        # ===== 可交互 widget 围栏：沙箱 iframe + 白名单桥 =====
        # 与 ```html 的分工：html 走 _sanitize_widget_html（脚本被剥离、纯静态），
        # widget 保留脚本并放进沙箱 iframe。内容**不**内联进主文档，只以 base64
        # 存在 data 属性里，由 JS 侧装配 —— 主文档开了 file:// 互访，脚本内联
        # 等于任意本地文件读取 + 外传，必须隔离。桥权限与插件 fence 同款。
        if lang == "widget":
            try:
                raw_widget = _unescape_html(code_content_raw)
            except Exception:
                raw_widget = code_content_raw
            if _HTML_WIDGET_HEAD_PATTERN.match(raw_widget) and len(raw_widget) <= 200_000:
                b64_widget = base64.b64encode(raw_widget.encode("utf-8")).decode("ascii")
                widget_id = "wgt-" + hashlib.sha1(raw_widget.encode("utf-8")).hexdigest()[:12]
                return (
                    f'<div id="{widget_id}" class="drifox-widget" data-fence-renderer="widget" '
                    f'data-widget-src="{b64_widget}" style="margin: 12px 0;"></div>'
                )

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
        # 流式中间态：末尾未闭合 fence 若是图表语言，改语言标记降级为骨架占位。
        #
        # mermaid：半截源码必然 parse 失败，mermaid 10 失败时会向 body 追加 error
        #   bomb SVG（innerHTML 全量重建清不掉，且占位 id 随内容 hash 变化无法去重，
        #   逐轮累积成消息结尾一串「Syntax error in text」）。
        # echarts：原先只处理 mermaid，半截 JSON 会被照常包成 .echarts-container。
        #   ① JSON.parse 必然失败 → 400px 空洞；② b64 随内容逐字增长 → vault key
        #   每轮都变，未渲染节点被反复塞进 vault 快速膨胀（多图白屏次因）。
        # 两者统一改标为 <lang>-streaming，由 _wrap_code_blocks_with_copy_button_web
        # 产出等高骨架；fence 闭合后语言标记复原，自动切换为真实图表。
        lines = md_text.split("\n")
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped.startswith("```"):
                lang_token = stripped[3:].strip()
                # html 同样需要骨架：半截 HTML 透传会因标签未闭合破坏页面结构。
                # 插件注册的 fence 也要改标（否则半截源码会被当成品渲染）。
                if (
                    lang_token.lower() in ("mermaid", "echarts", "html", "widget")
                    or _get_plugin_fence_renderer(lang_token.lower()) is not None
                ):
                    lines[i] = lines[i].replace(lang_token, f"{lang_token}-streaming", 1)
                    md_text = "\n".join(lines)
                break
        md_text += "\n```"
    return md_text


def _protect_inline_svg_blocks(md_text: str) -> str:
    """把独立成段的多行内联 <svg> 包进块级 <div>，防止 markdown 撕碎 SVG 结构。

    背景：模型按 visualization 协议内联输出多行 SVG 时，Python-Markdown 会把
    <style>/<defs> 等块级子元素当段落分隔符，把 SVG 腰斩成多个 <p> 碎片，
    nl2br 还会在 SVG 行间插 <br>，浏览器无法渲染（表现为"SVG 画不出来"）。
    包一层 <div> 后 Python-Markdown 对块级容器内部原样保留，结构完整透传。

    规则：
    - 跳过 ``` 围栏内的行（围栏 SVG 由 _wrap_code_blocks_with_copy_button_web 透传）
    - 行首 <svg 开、</svg> 行闭；单行自闭合的 SVG 不动（无撕碎风险）
    - 未闭合的 SVG（流式中间态）保持原样，闭合后自然走包裹路径
    """
    if "<svg" not in md_text.lower():
        return md_text

    lines = md_text.split("\n")
    out_lines = []
    buf: list = []
    in_svg = False
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if not in_svg:
            if stripped.startswith("```"):
                in_fence = not in_fence
            if not in_fence and stripped[:4].lower() == "<svg" and "</svg>" not in stripped.lower():
                in_svg = True
                buf = [line]
                continue
            out_lines.append(line)
        else:
            buf.append(line)
            if "</svg>" in stripped.lower():
                in_svg = False
                out_lines.append("")
                out_lines.append("<div>")
                out_lines.extend(buf)
                out_lines.append("</div>")
                out_lines.append("")
                buf = []
                continue
    if in_svg and buf:
        # 未闭合：原样还回，交给下一次渲染（流式下一轮或全量重渲）
        out_lines.extend(buf)
    return "\n".join(out_lines)


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


# 编辑类工具在 progress 阶段（path 尚未到达）的兜底文案：避免运行框空窗成「准备中...」
_FILE_EDIT_TOOLS_FALLBACK_TEXT = {
    "write": "写入文件",
    "edit": "编辑文件",
    "multi_edit": "批量编辑文件",
}


def _format_tool_progress_badge(char_count: int, add_lines: int = 0, del_lines: int = 0) -> str:
    """运行框进度徽标：编辑类工具显示 `+N/-M` 胶囊，其它工具显示 `(N字符)`。

    行数优先——编辑类工具的字符数对用户没有信息量（设计：
    docs/superpowers/specs/2026-09-13-tool-streaming-line-stats-design.md）。
    胶囊结构与完成框的 diff 统计完全一致（复用 `.tool-diff-stats` 系列 class）。
    返回值是**独立元素**（class 含 `tool-streaming-badge`），需与预览 span 平级放在
    块末尾：预览 span 是 `overflow:hidden + ellipsis`，徽标嵌在里面会被长文本裁掉。
    """
    if add_lines or del_lines:
        # 颜色与完成框的 diff 统计完全一致（render_helpers 里同款内联色，
        # 不依赖 .tool-diff-stats__add/__del 的 CSS —— 流式块下 CSS 优先级不可靠）
        return (
            f'<span class="tool-diff-stats tool-streaming-badge" '
            f'style="font-size: {scale_font_size(11)}px; flex: 0 0 auto; margin-left: 6px;">'
            f'<span class="tool-diff-stats__add" style="color: #39d353; font-weight: 600;">+{add_lines}</span>'
            f'<span class="tool-diff-stats__sep">/</span>'
            f'<span class="tool-diff-stats__del" style="color: #f85149; font-weight: 600;">-{del_lines}</span>'
            f"</span>"
        )
    if char_count > 0:
        return (
            f'<span class="tool-streaming-badge" style="color: var(--text); '
            f'font-size: {scale_font_size(10)}px; flex: 0 0 auto; margin-left: 6px;">'
            f"({char_count}字符)</span>"
        )
    return ""


def _render_tool_streaming_block(
    tool_call_id: str,
    tool_name: str,
    preview: str,
    char_count: int = 0,
    completed: bool = False,
    add_lines: int = 0,
    del_lines: int = 0,
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
    # （插件注册 metadata["subagent_task"]=True；工具已由 task 更名为 subagent_para，
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

    # 预览文本（右侧徽标是独立兄弟节点，避免长文本 ellipsis 把徽标裁掉）
    preview_display = escape(preview) if preview else "准备中..."
    badge_html = "" if completed else _format_tool_progress_badge(char_count, add_lines, del_lines)

    streaming_state = "false" if completed else "true"
    # 编辑/子智能体/提问类工具标记 data-keep-in-content：JS 正文分区据此保留在正文（registry 派生）
    _keep_attr = ' data-keep-in-content="true"' if tool_name in _edit_tools() else ""
    # 🐛 修复（工具完成框残留/两份/沉底）：无 tool_call_id 时合成 data-block-key。
    # 【根因】`<tool>` 协议块解析不到 tool_call_id 时（模型正文复述协议格式、
    # 输出被截断/停止、旧历史数据缺该字段），产物是 data-tool-call-id="" —— 而
    # reorganizeContent 全线用 `if (_tid)` 判定，**空串为假**：
    #   · 不登记 tool id / posMap → 过期清理分支短路 → 块永不清理
    #   · 排序 getPos 无 data-order / 无 posMap 记录 → 返回 1e9 → 恒沉底
    # 内容一变 block_key 就变（tool_name+args+result 的 sha1）→ 新块迁入工具区、
    # 旧块留在原地，渲染几次就几份，全部 data-order 相同且不再重排（真实 DOM
    # 复现见 tests/debug/unclosed_tool_completed_linger.py：结果逐版变化时
    # 工具区块数 1→2→3→4，永不回落）。
    # 【修复】按「工具名 + 预览文本 + 完成态」合成稳定身份：同内容重复渲染 key
    # 不变（可查重、可清理），内容变化 key 变化（不与新块撞身份被误删）。
    _syn_key_attr = ""
    if not tool_call_id:
        _seed = f"{tool_name}|{preview}|{streaming_state}"
        _syn_key_attr = ' data-block-key="syn-' + hashlib.sha1(_seed.encode("utf-8")).hexdigest()[:12] + '"'

    return f"""<div class="tool-block tool-streaming-block" data-tool-name="{escape(tool_name)}" data-tool-call-id="{tool_call_id}" data-streaming="{streaming_state}"{_syn_key_attr}{_keep_attr} style="margin: 4px 0; background: transparent; border: none; border-radius: 6px; box-shadow: none; display: flex; align-items: center; padding: 4px 10px; {get_font_family_css()}">
        <span style="display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto;">
            <span style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:0 0 auto;">
                {icon_html}
            </span>
            <span style="white-space: nowrap; flex: 0 0 auto; color: {title_color}; font-size: {scale_font_size(13)}px; font-weight: 500; line-height: 22px;">{escape(cn_name)}</span>
            {spinner_html}
        </span>
        <span class="tool-streaming-preview" data-dfx-preview data-dfx-key="tool-{escape(tool_call_id)}" data-dfx-text="{escape(preview) if preview else "准备中..."}" style="flex: 0 1 auto; min-width: 0; text-align: left; color: var(--text-secondary); font-size: {scale_font_size(11)}px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-left: 12px;">
            {preview_display}
        </span>{badge_html}
    </div>"""


def _render_think_block(content: str, completed: bool = True, compact: bool = False, flip_idx: int = -1) -> str:
    # 🆕 FLIP 稳定键：流式态（.think-streaming）与完成态（.think-compact/.think-block）
    # 用的是不同 DOM 结构与不同 block-key（后者按内容哈希），导致结束态重排时
    # FLIP 无法配对 → 思考块"瞬移到顶部"没有动画。
    # 这里额外打一个**按消息内出现顺序**的位置键 data-flip-key，两种形态共用，
    # FLIP 即可把"第 N 个思考块"的位移补间成平滑动画。
    _flip_attr = f' data-flip-key="think-{flip_idx}"' if flip_idx >= 0 else ""
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
            preview_right = (
                f'<span data-dfx-preview data-dfx-key="{block_key}" data-dfx-text="{escape(preview)}" '
                f'style="color: var(--text-secondary); font-weight: normal; margin-left: 8px; font-size: {scale_font_size(11)}px;">'
                f"{escape(preview)}</span>"
            )
            return f"""<div class="think-compact" data-block-key="{block_key}"{_flip_attr} style="margin: 2px 0; padding: 4px 8px; {font_style} display: flex; align-items: baseline; gap: 6px; border-radius: 4px;">
    <span style="white-space: nowrap; flex-shrink: 0;">{status_text}</span>
    {preview_right}
</div>"""

        # ── 非简洁模式：完整折叠框UI（图标标签 + 预览 + 可展开全文）──
        content_escaped = escape(_strip_code_blocks(content))
        block_seed = f"{content}|1"
        block_key = "think-" + hashlib.sha1(block_seed.encode("utf-8")).hexdigest()[:12]
        summary_right = (
            f'<span data-dfx-preview data-dfx-key="{block_key}" data-dfx-text="{escape(preview)}" '
            f'style="color: var(--text-secondary); font-weight: normal; margin-left: 12px; font-size: {scale_font_size(11)}px;">'
            f"{escape(preview)}</span>"
        )
        body_html = f'<div class="think-content loading" style="white-space: normal; word-break: break-word; line-height: 1.6; {font_style}">{content_escaped}</div>'
        return f"""<div class="cm-collapsible think-block" data-block-key="{block_key}"{_flip_attr} data-expanded="false" style="margin: 4px 0;">
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
    return f"""<div class="think-streaming"{_flip_attr} data-streaming="true" style="margin: 4px 0; padding: 6px 10px; border: none; border-radius: 6px;">
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
        summary_right = (
            f'<span data-dfx-preview data-dfx-key="think-light" data-dfx-text="{escape(preview)}" '
            f'style="color: var(--text-secondary); font-weight: normal; margin-left: 12px; font-size: {scale_font_size(11)}px;">'
            f"{escape(preview)}</span>"
        )
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


def _registered_tag_names_safe() -> List[str]:
    """安全获取插件注册的块标签名列表（注册表不可用时返回空列表）。

    mood/plan 等人格块标签与 <think>/<tool> 同属流式协议标签：未闭合期间
    内容不得以正文/纯文本形态进 DOM。
    """
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        return list(UIPluginRegistry.get_instance().get_registered_tag_names())
    except Exception:
        return []


def _has_unclosed_registered_tag(text: str) -> bool:
    """检测文本中是否存在未闭合的插件注册标签（如 <mood>，判据同 _has_unclosed_think）。

    未闭合注册 tag 的内容若以正文/纯文本形态进 DOM，全量渲染落地时会被
    _inject_tag_cards 替换为插件卡片 → 视觉上"文字先流式出现又消失"。
    """
    if not text:
        return False
    # chunk 边界可能把 <mood> 切成半截（尾部 "<mo"）：rfind 找不到 open 会误判
    # 已闭合 → 放行生肉。宽松拦（误拦代价只是该 chunk 延迟一次渲染）。
    if _PARTIAL_TAG_TAIL_RE.search(text):
        return True
    for tag in _registered_tag_names_safe():
        last_open = text.rfind(f"<{tag}>")
        if last_open == -1:
            continue
        if text.rfind(f"</{tag}>", last_open) == -1:
            return True
    return False


# ===== 渲染型 fence 流式静默 =====
# ```echarts / ```mermaid / ```html / ```widget（及插件注册 fence lang）闭合后
# 由全量渲染分发为图表/卡片。半截 fence 的代码若以生肉形式增量注入 DOM，
# 观感是"代码流式打出来、fence 闭合后被替换消失"（与 think/mood 泄漏同族）。
# 未闭合期间静默累积，闭合后由全量渲染落地（chart-streaming 骨架/真图）。
_CHART_FENCE_OPEN_RE = re.compile(r"^\s*(```|~~~)\s*(\w+)")
_CHART_FENCE_LANGS = frozenset({"echarts", "mermaid", "html", "widget", "svg"})
# chunk 边界切开标记的半截尾巴：行尾 1-3 个反引号/波浪（可能正在写 fence 开标记）
_PARTIAL_FENCE_TAIL_RE = re.compile(r"(?:^|\n)[ \t]{0,3}[`~]{1,3}[a-zA-Z0-9]{0,15}$")
# chunk 边界切开标签的半截尾巴：行尾 <xx（可能正在写 <mood> 等协议标签）
_PARTIAL_TAG_TAIL_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]{0,11}$")


def _is_render_fence_lang(lang: str) -> bool:
    """lang 是否会渲染成卡片/图表（内置集合 + 插件 fence 渲染器注册表）。"""
    if not lang:
        return False
    lang = lang.strip().lower()
    if lang in _CHART_FENCE_LANGS:
        return True
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        return lang in UIPluginRegistry.get_instance().get_all_fence_renderers()
    except Exception:
        return False


def _has_unclosed_chart_fence(text: str) -> bool:
    """检测文本是否停在未闭合的渲染型 fence（图表/卡片类）内部。

    fence 开闭按行首 ``` / ~~~ 判定（与 _extract_fenced_code 同语义）。
    普通代码块（python/js 等）流式生肉显示是预期行为，不在本检测范围。
    """
    # chunk 边界可能把 fence 开标记切成半截（尾部 "```e" 或 "``"）：状态机
    # 匹配不到完整 lang 会误判闭合 → 放行生肉。宽松拦，代价同上。
    if _PARTIAL_FENCE_TAIL_RE.search(text):
        return True
    if "```" not in text and "~~~" not in text:
        return False
    inside = False
    chart = False
    marker = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if not inside:
            m = _CHART_FENCE_OPEN_RE.match(stripped)
            if m:
                inside = True
                marker = m.group(1)
                chart = _is_render_fence_lang(m.group(2))
            elif stripped.startswith("```") or stripped.startswith("~~~"):
                inside = True
                marker = stripped[:3]
                chart = False
        elif marker and stripped.startswith(marker):
            inside = False
            chart = False
    return inside and chart


def _first_unclosed_chart_fence_pos(md: str) -> int:
    """第一个未闭合渲染型 fence 的开标记起始偏移（无 → -1）。

    差量 tail 行内渲染的截断基准：未闭合 fence 起点之前的正文照常行内
    渲染，fence 起点之后静默（与 _tail_before_unclosed_block 的 tag/think
    截断同语义，防 updateContentAppend 删增量节点时连带丢失正文）。
    """
    if "```" not in md and "~~~" not in md:
        return -1
    inside = False
    chart = False
    marker = ""
    open_pos = -1
    offset = 0
    for line in md.split("\n"):
        stripped = line.strip()
        if not inside:
            m = _CHART_FENCE_OPEN_RE.match(stripped)
            if m:
                inside = True
                marker = m.group(1)
                chart = _is_render_fence_lang(m.group(2))
                if chart:
                    open_pos = offset + line.find(marker)
            elif stripped.startswith("```") or stripped.startswith("~~~"):
                inside = True
                marker = stripped[:3]
        elif marker and stripped.startswith(marker):
            inside = False
            chart = False
            open_pos = -1
        offset += len(line) + 1
    return open_pos if (inside and chart) else -1


def _last_para_break_outside_fence(md: str) -> int:
    """最后一个不在 fence 内部的 ``\n\n`` 偏移（无 → -1）。

    差量基线推进点若落进未闭合 fence 内部，后续差量切片起点就在 fence 内，
    切片内的 fence 状态机（从外判起）会把内部代码行当普通段落产出 → 图表
    源码以生肉段流入正文。stable 永不越过未闭合 fence 起点。
    """
    if "```" not in md and "~~~" not in md:
        return md.rfind("\n\n")
    inside = False
    marker = ""
    last = -1
    i = 0
    n = len(md)
    while i < n:
        if i == 0 or md[i - 1] == "\n":
            # 行首：判定 fence 开闭
            j = i
            while j < n and md[j] in " \t":
                j += 1
            tok = md[j : j + 3]
            if tok in ("```", "~~~"):
                if not inside:
                    inside = True
                    marker = tok
                elif tok == marker:
                    inside = False
                i = j + 3
                continue
        if not inside and md.startswith("\n\n", i):
            last = i
            i += 2
            continue
        i += 1
    return last


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


def _iter_think_segments(md_text: str) -> list[tuple[int, str, bool]]:
    """按 `_inject_think_cards` 同样的切分规则，列出消息内的思考块。

    Returns:
        ``[(ordinal, content, closed)]`` —— ordinal 与 DOM 上的
        ``data-flip-key="think-{n}"`` 一致（流式态与完成态共用同一序号，见
        `_render_think_block`），closed 表示 ``</think>`` 已闭合。
    """
    out: list[tuple[int, str, bool]] = []
    i = 0
    ordinal = 0
    while i < len(md_text):
        start_idx = md_text.find("<think>", i)
        if start_idx == -1:
            break
        think_start = start_idx + len("<think>")
        next_think = md_text.find("<think>", think_start)
        search_end = next_think if next_think != -1 else len(md_text)
        end_idx = md_text.rfind("</think>", think_start, search_end)
        if end_idx != -1:
            content = md_text[think_start:end_idx]
            if content.strip():
                out.append((ordinal, content, True))
                ordinal += 1
            i = end_idx + len("</think>")
        else:
            content = md_text[think_start:search_end]
            if content.strip():
                out.append((ordinal, content, False))
                ordinal += 1
            i = search_end
    return out


def _inject_think_cards(md_text: str, completed: bool = True, compact: bool = False) -> str:
    """注入思考框HTML。

    关键逻辑：<think> 匹配到下一个 <think> 之前的最后一个 </think>，
    避免流式输出时多个 </think> 导致内容泄露。
    """
    parts = []
    i = 0
    # 消息内思考块序号：作为 FLIP 位置键（data-flip-key），流式态与完成态共用，
    # 结束态重排时 FLIP 才能把"第 N 个思考块"的位移补间成动画。
    think_ordinal = 0
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
                parts.append(_render_think_block(content, completed=True, compact=compact, flip_idx=think_ordinal))
                think_ordinal += 1
            # 空思考块跳过渲染，避免页面末尾遗留空折叠框
            i = end_idx + len("</think>")
        else:
            # 未闭合：内容截取到边界处，避免吞掉后续 <think>
            content = md_text[think_start:search_end]
            if content.strip():
                parts.append(_render_think_block(content, completed=False, flip_idx=think_ordinal))
                think_ordinal += 1
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


# 历史渲染"重负载字段"每块上限 (result, diff, echarts)。
# 2026-09-09 内存治理：大会话卡 HTML 达 120~473KB，大头是工具块 diff/echarts。
_HISTORY_TOOL_CAPS = (800, 3000, 8000)


@lru_cache(maxsize=128)
def _render_tool_block_content(content: str, compact: bool = False, heavy_caps=None) -> str:
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

    # ========== 解析 echarts（可选字段；行锚定取最后一个） ==========
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
    # ── 历史渲染重负载治理（heavy_caps 仅历史卡传入）──
    if heavy_caps:
        _r_cap, _d_cap, _e_cap = heavy_caps
        if _r_cap and len(tool_result) > _r_cap:
            tool_result = tool_result[:_r_cap] + " …[结果过长，已省略 " + str(len(tool_result) - _r_cap) + " 字符]"
        if _d_cap and len(diff_content) > _d_cap:
            diff_content = diff_content[:_d_cap] + " …[diff 过长，已省略 " + str(len(diff_content) - _d_cap) + " 字符]"
        if _e_cap and len(echarts_content) > _e_cap:
            echarts_content = ""
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


def _inject_tool_blocks(md_text: str, completed: bool = True, compact: bool = False, heavy_caps=None) -> str:
    """注入工具块HTML，类似think块"""
    if not md_text:
        return md_text

    parts = []
    i = 0
    while i < len(md_text):
        start_idx = md_text.find("<tool>", i)
        if start_idx == -1:
            # 🐛 防御（工具源码泄漏）：无 <tool> 开头的剩余段清理孤立 </tool>，
            # 对齐 _inject_think_cards 的孤立闭合标签清理——HTML 解析器虽会
            # 忽略孤立闭合标签，但 <p>正文</tool></p> 在嵌套解析下产生怪异结构。
            parts.append(md_text[i:].replace("</tool>", ""))
            break
        parts.append(md_text[i:start_idx])
        end_idx = md_text.find("</tool>", start_idx + len("<tool>"))
        if end_idx != -1:
            content = md_text[start_idx + len("<tool>") : end_idx]
            parts.append(_render_tool_block_content(content, compact=compact, heavy_caps=heavy_caps))
            i = end_idx + len("</tool>")
        else:
            # 🐛 修复（工具源码泄漏）：未闭合块此前原样保留 → md.convert 把
            # <tool> 当未知 HTML 元素（标签本身不可见），内部 name:/args:/result:
            # 字段文本裸露为正文（截图级症状：name: bash args: {...} result: ...）。
            # 未闭合来源：模型在正文中输出协议格式文本（讨论工具调用机制时
            # 模仿上下文格式，流式中 </tool> 未到达）、max_tokens 截断、停止生成。
            # 按到达程度渲染：流式中间态 → 运行中占位框（与 JS 注入运行框视觉
            # 一致，闭合后自然过渡为工具卡）；非流式终态（截断/停止）→ 容错
            # 解析已有字段渲染完成态卡。fence 内示例由 _extract_fenced_code
            # 保护，不进入本分支。
            # 🐛 防御（审查 I-1，吞正文）：容错解析范围截断到下一个 <tool>
            # 开标签（嵌套/后续块的字段不得混入本块）；剩余段递归走同一
            # 渲染逻辑（每轮至少消费一个开标签，无死循环风险）。
            content = md_text[start_idx + len("<tool>") :]
            _next_open = content.find("<tool>")
            _rest = ""
            if _next_open != -1:
                _rest = content[_next_open:]
                content = content[:_next_open]
            if completed:
                if content.strip():
                    parts.append(_render_tool_block_content(content, compact=compact, heavy_caps=heavy_caps))
            elif content.strip():
                _name_m = _TOOL_NAME_PATTERN.search(content)
                parts.append(
                    _render_tool_streaming_block(
                        tool_call_id="",
                        tool_name=_name_m.group(1).strip() if _name_m else "",
                        preview="接收中...",
                        completed=False,
                    )
                )
            if _rest:
                parts.append(_inject_tool_blocks(_rest, completed, compact, heavy_caps))
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


# ======== KaTeX 公式提取（GitHub 规则 + CJK 收紧）========
# 设计文档：docs/superpowers/specs/2026-09-04-katex-formula-rendering-design.md
# 在 markdown 渲染前把公式源码提取为 b64 占位标签：
# - 公式不进 markdown 管线，天然免疫 _ ^ \ 的转义
# - fence / inline code 内永不提取
# - 未闭合定界符（流式半截）不命中，保持原文
_RE_KATEX_FENCE_OPEN = re.compile(r"(```|~~~)")
_RE_KATEX_INLINE_CODE = re.compile(r"`[^`\n]+`")
_RE_KATEX_SENTINEL = re.compile("\x00(\\d+)\x00")
_RE_KATEX_DISPLAY_DOLLAR = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_RE_KATEX_DISPLAY_BRACKET = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_RE_KATEX_INLINE_PAREN = re.compile(r"\\\((.+?)\\\)")
# GitHub 规则：开 $ 右侧非空白/数字/$；闭 $ 左侧非空白、右侧非数字/字母
# （闭侧允许数字结尾：$E=mc^2$ 是合法公式；开侧禁数字已挡住 $100 类价格）
_RE_KATEX_INLINE_DOLLAR = re.compile(
    r"(?<![\\$])\$(?=[^\s\d$])"
    r"([^$\n]+?)"
    r"(?<=[^\s])\$(?![\d\w])"
)
_RE_KATEX_CJK = re.compile(r"[\u4e00-\u9fff]")


def _katex_placeholder(src: str, display: bool) -> str:
    """公式源码 b64 编码为占位标签；JS 侧 renderKatexBlocks 消费。"""
    b64 = base64.b64encode(src.encode("utf-8")).decode("ascii")
    if display:
        return f'<div class="katex-block katex-pending" data-katex-src="{b64}" style="margin:12px 0;"></div>'
    return f'<span class="katex-inline katex-pending" data-katex-src="{b64}"></span>'


def _katex_sub(pattern: re.Pattern, text: str, display: bool) -> str:
    """按一条定界符规则替换；内容含 CJK 时放弃该对保持原文。"""

    def _repl(m: re.Match) -> str:
        src = m.group(1)
        if not src.strip() or _RE_KATEX_CJK.search(src):
            return m.group(0)
        return _katex_placeholder(src, display)

    return pattern.sub(_repl, text)


def _extract_formulas_in_plain(seg: str) -> str:
    """非 fence 段：inline code 哨兵保护 → 四类定界符按优先级提取 → 还原。"""
    stash: list[str] = []

    def _save(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    seg = _RE_KATEX_INLINE_CODE.sub(_save, seg)
    # 长定界优先，防止 $$ 被两个 $ 拆食
    seg = _katex_sub(_RE_KATEX_DISPLAY_DOLLAR, seg, display=True)
    seg = _katex_sub(_RE_KATEX_DISPLAY_BRACKET, seg, display=True)
    seg = _katex_sub(_RE_KATEX_INLINE_PAREN, seg, display=False)
    seg = _katex_sub(_RE_KATEX_INLINE_DOLLAR, seg, display=False)
    return _RE_KATEX_SENTINEL.sub(lambda m: stash[int(m.group(1))], seg)


def _extract_formulas(md_text: str) -> str:
    """markdown 渲染前提取 LaTeX 公式为占位标签（GitHub 规则 + CJK 收紧）。

    - fence 状态机切段，fence 内（含流式未闭合 fence）原样保留
    - 四类定界符优先级：$$ → \\[ → \\( → $
    - 未闭合定界符不命中，原文保留（流式安全）
    """
    if "$" not in md_text and "\\(" not in md_text and "\\[" not in md_text:
        return md_text

    segments: list[tuple[bool, str]] = []  # (is_fence, text)
    buf: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in md_text.split("\n"):
        stripped = line.lstrip()
        if in_fence:
            buf.append(line)
            if stripped.startswith(fence_marker):
                segments.append((True, "\n".join(buf)))
                buf, in_fence, fence_marker = [], False, ""
        else:
            m = _RE_KATEX_FENCE_OPEN.match(stripped)
            if m:
                if buf:
                    segments.append((False, "\n".join(buf)))
                buf, in_fence, fence_marker = [line], True, m.group(1)
            else:
                buf.append(line)
    # 流式半截 fence 到结尾：整段视为 fence，不提取
    segments.append((in_fence, "\n".join(buf)))

    return "\n".join(seg if is_fence else _extract_formulas_in_plain(seg) for is_fence, seg in segments)


def _extract_fenced_code(md_text: str) -> tuple[list[str], str]:
    """提取完整 fenced code 块为 \x00F<i>\x00 哨兵（inject 免疫保护）。

    背景：_inject_think_cards/_inject_tool_blocks/_inject_tag_cards 全文扫描
    <think>/<tool>/<tag> 协议标签，不感知 fence——代码示例内容里含协议标签
    （讨论插件协议、展示调用格式时极常见）会被抽出渲染成假思考卡/工具框，
    经差量 append 滞留正文底部。fence 跨空行整块产出修复后暴露（旧版尾段
    产出丢内容反而掩盖了它）；全量渲染管线同样受影响。

    提取后 inject 只作用于 fence 外文本；_restore_fenced_code 在 md.convert
    前放回原文，fenced_code 正常渲染代码内容（协议标签转义为字面文本）。
    fence 状态机与 _extract_formulas 同款语义；未闭合 fence（流式中间态，
    _sanitize_incomplete_markdown 已补闭合，此处兜底）原样放回不提取。
    """
    if "```" not in md_text and "~~~" not in md_text:
        return [], md_text

    fences: list[str] = []
    out_lines: list[str] = []
    buf: list[str] | None = None
    fence_marker = ""
    for line in md_text.split("\n"):
        stripped = line.lstrip()
        if buf is None:
            m = _RE_KATEX_FENCE_OPEN.match(stripped)
            if m:
                fence_marker = m.group(1)
                buf = [line]
            else:
                out_lines.append(line)
        else:
            buf.append(line)
            if stripped.startswith(fence_marker):
                fences.append("\n".join(buf))
                out_lines.append(f"\x00F{len(fences) - 1}\x00")
                buf = None
    if buf is not None:
        # 未闭合 fence：原样放回（流式中间态兜底，语义与 _extract_formulas 一致）
        out_lines.extend(buf)
    return fences, "\n".join(out_lines)


def _restore_fenced_code(md_text: str, fences: list[str]) -> str:
    """_extract_fenced_code 提取的 fence 原文放回（md.convert 前）。"""
    for i, src in enumerate(fences):
        md_text = md_text.replace(f"\x00F{i}\x00", src)
    return md_text


# 缓存大小阈值（KB）：超过此大小的文本不缓存，防止内存膨胀
_LRU_CACHE_SIZE_THRESHOLD = 200 * 1024  # 200KB


@lru_cache(
    maxsize=32
)  # 16→32（T16）：key 加 theme_ver 尾参后深浅两版各占一份；>200KB 大文本已走 __wrapped__ 绕过缓存
def _render_markdown_to_html_cached_impl(
    raw_md: str, compact: bool = False, heavy_caps=None, theme_ver: int = 0
) -> str:
    """
    Markdown 转 HTML 的核心渲染函数（带 LRU 缓存）。

    theme_ver：主题版本号（ThemeRefreshCoordinator.get_version()），随 key 分桶——
    同一 markdown 在深/浅主题下各存一份渲染结果，来回切换直接命中。
    默认值 0 供直接调用（历史测试/无主题上下文场景）与 __wrapped__ 绕过分支
    保持旧签名兼容；生产路径由 _render_markdown_to_html_cached 显式传入。
    """
    safe_md = _sanitize_incomplete_markdown(raw_md)
    safe_md = _protect_inline_svg_blocks(safe_md)
    safe_md = _extract_formulas(safe_md)  # KaTeX 公式提取（设计文档 2026-09-04）
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    # fence 内容保护：代码块内协议标签不被 inject 抽出渲染成假卡片
    _fences, safe_md = _extract_fenced_code(safe_md)
    processed_md = _inject_think_cards(safe_md, True, compact=compact)
    processed_md = _inject_tool_blocks(processed_md, True, compact=compact, heavy_caps=heavy_caps)
    processed_md = _inject_hook_blocks(processed_md, True)
    processed_md = _inject_tag_cards(processed_md, True, compact=compact)
    processed_md = _restore_fenced_code(processed_md, _fences)

    try:
        md = get_markdown_instance()
        md.reset()
        html_content = md.convert(processed_md)
        html_content = _wrap_code_blocks_with_copy_button_web(html_content)
        return html_content
    except Exception:
        return f"<pre>{escape(raw_md)}</pre>"


def _render_markdown_to_html_cached(raw_md: str, compact: bool = False, heavy_caps=None) -> str:
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
        return _render_markdown_to_html_cached_impl.__wrapped__(raw_md, compact=compact, heavy_caps=heavy_caps)

    # [T16] 主题版本入 key：按 (theme_ver, raw_md, compact, heavy_caps) 分桶，
    # 深浅主题各自缓存各自的渲染结果（get_version 内部有锁，线程安全）
    from app.utils.theme_refresh import ThemeRefreshCoordinator

    theme_ver = ThemeRefreshCoordinator.get_version()
    return _render_markdown_to_html_cached_impl(raw_md, compact=compact, heavy_caps=heavy_caps, theme_ver=theme_ver)


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
    heavy_caps = snapshot.get("heavy_caps")  # 历史卡重负载上限（None=不限）

    if not streaming:
        # 非流式分支（历史加载 / 流式结束调用方已切非流式）
        safe_md = _sanitize_incomplete_markdown(raw_md)
        safe_md = _extract_formulas(safe_md)  # KaTeX 公式提取
        safe_md = _unwrap_code_blocks_with_context_links(safe_md)
        safe_md = _inject_context_links(safe_md)
        # fence 内容保护：代码块内协议标签不被 inject 抽出渲染成假卡片
        _fences, safe_md = _extract_fenced_code(safe_md)
        processed_md = _inject_think_cards(safe_md, True, compact=compact)
        processed_md = _inject_tool_blocks(processed_md, True, compact=compact, heavy_caps=heavy_caps)
        processed_md = _inject_hook_blocks(processed_md, True)
        processed_md = _inject_tag_cards(processed_md, True, compact=compact)
        processed_md = _restore_fenced_code(processed_md, _fences)
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
    safe_md = _extract_formulas(safe_md)  # KaTeX 公式提取
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    # fence 内容保护：代码块内协议标签不被 inject 抽出渲染成假卡片
    _fences, safe_md = _extract_fenced_code(safe_md)
    processed_md = _inject_think_cards(safe_md, False, compact=compact)
    processed_md = _inject_tool_blocks(processed_md, False, compact=compact)
    processed_md = _inject_hook_blocks(processed_md, False)
    processed_md = _inject_tag_cards(processed_md, False, compact=compact)
    processed_md = _restore_fenced_code(processed_md, _fences)

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
# v19（2026-09-05）：① echarts 单图渲染完成 + _pumpEcharts 队列排空时补
# reportHeightDebounced（原只在 updateContent 末尾 30~50ms 定时上报，rAF 分帧
# 下多图卡片常被提前触发 → 高度偏小、底部图表被裁）；② updateTailHtml 补齐
# 图表四连；③ 新增 ```html fence（.html-widget + 工具栏 html 分支）；
# ④ 正文 <img> 补 loading=lazy / decoding=async。
# 旧骨架无上述分支 → 必须靠版本号让旧缓存失效。
# v20（2026-09-05）：① 图表主题三件套（_CHART_IS_DARK/_CHART_BG/_ICON_BASE）
# 与 mermaid themeVariables 由骨架构建期常量改为运行时可更新（window._applyChartTheme
# / window._mmdApplyTheme），refresh_theme 同步刷新；② echarts 改懒加载
# （_echartsEnsure），骨架不再常驻 1MB vendor。
# 旧骨架无上述函数 → 必须靠版本号让旧缓存失效。
# v21（2026-09-05）：正文 <img> 点击的 action 从 open_url 改为 preview_image
# （内置预览弹窗 + 滚轮缩放）。旧骨架仍发 open_url → 不会崩但走不到预览，
# 必须靠版本号让旧缓存失效。
# v22（2026-09-05）：插件 fence assets 按需注入（window.__fenceAssets /
# _ensureFenceAssets / _runFenceAssets）+ __drifoxBridge 权限桥
# （_syncFenceBridge：theme / sendPrompt / storage）。旧骨架无这些函数与映射表
# → 插件 fence 只剩静态 HTML，必须靠版本号让旧缓存失效。
# v24（2026-09-05）：桥装配时序修复 —— _syncFenceBridge 改为在发起 assets 加载
# **之前**先执行一次（原先只在 onload 回调之后）。插件脚本末尾的首帧兜底初始化
# 若先跑，会看到空桥并把未授权状态锁死在节点上（幂等标记已打，后续救不回来）。
# 旧骨架仍是旧时序 → 必须靠版本号让旧缓存失效。
# v26（2026-09-06）：① 新增内置 ```widget 围栏（沙箱 iframe + 白名单桥：
# _initWidgets / _WIDGET_PRELUDE_JS / window.__WIDGET_GRANTS / window 级 message
# 监听）；② SVG 图形节点可挂 .context-tag 走同一条点击链（_closestTag）。
# 旧骨架两样都没有 —— widget 围栏退化成普通代码块、图节点点不动 ——
# 必须靠版本号让旧缓存失效。
# v27（2026-09-08）：① 打字机揭示队列（_TYPEWRITER_JS：window._twPush/_twReset/
# _twFlush + rAF 帧级揭示）；② FLIP 位移动画与动画串行队列（_FLIP_JS：
# _flipCapture/_flipPlay/_animEnqueue）。旧骨架两者都没有 —— 流式仍是整块蹦字、
# 结束态三动画叠加跳变 —— 必须靠版本号让旧缓存失效。
# v28~v31（2026-09-14）：滚动锚点恢复（_anchorable/_anchorables/_progScroll +
# _beginDomUpdate/_endDomUpdate 事务深度）。旧骨架仍用绝对 scrollTop + 钳制恢复
# → 阅读位置漂移 —— 必须靠版本号让旧缓存失效。
# v32（2026-09-14）：reorganizeContent 过期清理支持无 tool_call_id 的工具块
# （_currentToolBlockKeys 按 data-block-key 判定）。旧骨架只认 tool-call-id，
# 空串 id 的块（未闭合 <tool> 协议文本渲染产物）永不清理 → 每轮渲染追加一份、
# 全部同 data-order 且不再重排（工具完成框残留/两份/沉底）。
# v33（2026-09-15）：新增 finalizeStreamingBlocks（差量收尾：流式思考块就地
# 定稿）。旧骨架不含该函数 → 收尾时 runJavaScript 调用未定义函数，静默失败。
# v34（2026-09-15）：reorganizeContent 快路径新增 data-order 物理单调性检查。
# restore 恒 appendChild 沉底（F1）+ append_tool_result 原地 replaceChild 转换后，
# 完成块 data-order 正确但物理滞留底部；S1（正文先于工具结束）后键集合恒同 →
# 键序列 diff 全绿且无块缺 data-order → 跳过 sort → 末轮工具完成框沉底固化。
# 物理顺序 data-order 倒序 → 强制 sort。旧骨架无此检查，必须靠版本号失效。
# v35（2026-09-29）：打字机高度上报节流 80 → 40ms（_twStep._skipReport）。
# Python 侧追踪 tick（30ms 节拍）已把高度应用连续化并封死 ResizeObserver 回环，
# 上报端收紧只减"文字已出、目标未到"的滞后。旧骨架节流常量编译在缓存 HTML 里
# → 必须靠版本号让旧缓存失效。
# v36（2026-09-29）：_setStreamingDock 增加 collapseAfter 参数——坞态归位与工具区
# 折叠同帧合并（两个 max-height 变化合成一条 220px→0 过渡曲线），消除结束态
# 「归位展开到自然高度峰值→再折叠」的往返峰（剧烈抖动主因）。旧骨架无此参数，
# 归位仍两段式 → 必须靠版本号让旧缓存失效。
# v37（2026-10-08）：任务看板迁出 WebEngine——#todo-panel/#todo-content DOM、
# .todo-item 全套 CSS、window._updateTodoList / _todoCount / _todoProgressText
# 全部删除；任务区改由卡片内原生 Qt 面板承担（app/widgets/inline_todo_panel.py）。
# 旧骨架仍带 todo DOM 与 JS（虽无数据源、恒隐藏），必须靠版本号让旧缓存失效。
# _SKELETON_CACHE_VERSION +1（v39）：[#12] R2 恢复工具区滚动保护链（_scrollToolContentToBottom
# + tc scroll 监听 + 意图绑定）；R1 display 空窗 scrollTop 快照保护；R3 flush 纯揭示化。
# _SKELETON_CACHE_VERSION +1（v40）：打字机 DOM 替换闸门（window._twGate/_twDrainGate）：
# updateTailHtml / updateContentAppend 挂起到缓冲降到水位再执行，消除"揭示途中被替换
# 打断 → 文字整块跳变"（实测每 ~200ms 一次、单次 4~9 字符）。旧骨架无 _twGate →
# 调用被判 undefined 走原路径（无字幕），必须靠版本号让旧缓存失效。
# _SKELETON_CACHE_VERSION +1（v41）：[T28/P0-1] 差量渲染接入 roots 作用域查询——
# updateContentAppend/updateTailHtml 八个后处理收窄到新增区间；_initEchartsIn/
# renderWidgetToolbars/_runFenceAssets/_initWidgets/_scanFenceLangs 加 roots 形参
# （null/undefined 退化全文档，全量路径兼容）。骨架 JS 结构变更必须 bump。
# _SKELETON_CACHE_VERSION +1（v42）：[T31] 打字机闸门单槽改 FIFO 队列——
# _gateFn（新覆盖旧）→ _gateQueue（按序执行全部）。单槽对 updateTailHtml 成立
# （每轮传入当前全文尾部，后者含前者），对追加语义的 updateContentAppend 不成立
# （newHtml 只含本轮新闭合段，Python 已推进 _stable_md_len）→ 被覆盖那轮段格式化
# HTML 永不落地，紧随替换又移除 [data-incremental] 节点 → 该段从屏上消失。
# 实测 40 段 × 30ms：覆盖 3 次、流式态缺 7 段 token；禁用闸门 0 覆盖 0 缺失。
# 旧骨架仍为单槽 → 必须靠版本号让旧缓存失效。
_SKELETON_CACHE_VERSION = 42


def _js_literal(value) -> str:
    """把 Python 对象序列化成可直接内联进骨架 <script> 的 JS 字面量。

    本模块顶部是 `import orjson as json`：dumps 返回 **bytes** 且不接受
    ensure_ascii 关键字（与 _fence_assets_for_skeleton 的 _dump_js 同因），
    这里统一归一化成 str。
    """
    raw = json.dumps(value)
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


# ===== 内置可交互 widget 围栏（```widget）=====
# 与 ```html 的分工：html = 静态（脚本被 _sanitize_widget_html 剥离），
# widget = 可执行脚本（沙箱 iframe + 白名单桥）。
#
# 为什么必须隔离（安全红线）：宿主 WebEngine 开了 LocalContentCanAccessFileUrls
# + LocalContentCanAccessRemoteUrls，模型脚本若直接内联进主文档，等价于给出
# 「任意本地文件读取 + 外传」能力。故 widget 内容只以 base64 存在 data 属性里，
# 由 JS 侧装进 sandbox="allow-scripts" 的 iframe —— **不**授予
# allow-same-origin → 内部源为 opaque，拿不到宿主 DOM / sessionStorage /
# file:// 读取能力；iframe 内再叠一层 CSP 禁 connect-src / form-action /
# base-uri。宿主能力经 postMessage 白名单暴露，与插件 fence 的 __drifoxBridge
# 权限模型同款（theme / sendPrompt / storage）。
_BUILTIN_FENCE_PERMS: dict = {"widget": ["theme", "sendPrompt", "storage"]}

_WIDGET_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'unsafe-inline'; img-src data: blob: https: http:; "
    "media-src data: blob:; font-src data: https:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none'"
)
_WIDGET_CSP_JS = '"' + _WIDGET_CSP + '"'
_WIDGET_BASE_CSS = (
    "html,body{margin:0;padding:0;background:transparent;color:var(--text);font-size:13px;}*{box-sizing:border-box;}"
)
_WIDGET_BASE_CSS_JS = '"' + _WIDGET_BASE_CSS + '"'

# iframe 内部前置脚本：必须先于模型内容执行（模型脚本首帧就要用桥）。
# 只做三件事：① 接收宿主下发的主题变量并写进 :root，让 var(--panel) 等宿主
# 令牌在沙箱里继续可用；② 暴露 __drifoxBridge（postMessage 代理）；③ 上报高度。
_WIDGET_PRELUDE = """(function () {
  function post(m) { try { parent.postMessage({ __drifoxWidget: 1, p: m }, '*'); } catch (e) {} }
  var _pending = {}, _seq = 0;
  window.addEventListener('message', function (e) {
    var d = e && e.data;
    if (!d || !d.__drifoxWidgetHost) return;
    if (d.t === 'vars') {
      var s = document.getElementById('__drifoxVars');
      if (!s) { s = document.createElement('style'); s.id = '__drifoxVars'; document.head.appendChild(s); }
      var css = ':root{', k;
      for (k in d.v) { if (Object.prototype.hasOwnProperty.call(d.v, k)) css += k + ':' + d.v[k] + ';'; }
      css += '}';
      if (d.v && d.v['--font-family']) css += 'body{font-family:' + d.v['--font-family'] + ';}';
      s.textContent = css;
      return;
    }
    if (d.t === 'reply') {
      var r = _pending[d.id];
      if (r) { delete _pending[d.id]; r(d.v); }
    }
  });
  function _call(method, arg) {
    return new Promise(function (res) {
      var id = ++_seq;
      _pending[id] = res;
      post({ t: 'call', id: id, m: method, a: arg });
      setTimeout(function () { if (_pending[id]) { delete _pending[id]; res(null); } }, 4000);
    });
  }
  window.__drifoxBridge = {
    getTheme: function () { return _call('getTheme'); },
    sendPrompt: function (t) { post({ t: 'call', m: 'sendPrompt', a: String(t) }); },
    storage: {
      get: function (k) { return _call('storage.get', k); },
      set: function (k, v) { post({ t: 'call', m: 'storage.set', a: [k, v] }); }
    }
  };
  var _lastH = 0;
  function _report() {
    var h = 0;
    try { h = Math.ceil(document.documentElement.getBoundingClientRect().height); } catch (e) { h = 0; }
    if (h > 0 && Math.abs(h - _lastH) > 1) { _lastH = h; post({ t: 'h', v: h }); }
  }
  window.addEventListener('load', _report);
  if (window.ResizeObserver) { try { new ResizeObserver(_report).observe(document.documentElement); } catch (e) {} }
  setInterval(_report, 400);
  _report();
})();"""
_WIDGET_PRELUDE_JS = _js_literal(_WIDGET_PRELUDE)

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
    """md 中是否存在未闭合的 ``<think>`` / ``<tool>`` / 插件注册 tag 块（开标签数 > 闭合标签数）。

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
    if md.count("<think>") > md.count("</think>") or md.count("<tool>") > md.count("</tool>"):
        return True
    # 插件注册 tag（如 <mood>）未闭合同样视为未闭合协议块：tail 行内渲染、
    # 差量基线推进等守卫点共用本函数，tag 泄漏与 think 泄漏同症（闪现后消失）
    return _has_unclosed_registered_tag(md)


def _last_unpaired_open_pos(md: str, open_tag: str, close_tag: str) -> int:
    """返回最后一个**未闭合**开标签的起始偏移（无未闭合块 → -1）。"""
    depth = 0
    first_open = -1
    i = 0
    n = len(md)
    o_len, c_len = len(open_tag), len(close_tag)
    while i < n:
        if md.startswith(open_tag, i):
            if depth == 0:
                first_open = i
            depth += 1
            i += o_len
            continue
        if md.startswith(close_tag, i):
            if depth > 0:
                depth -= 1
            i += c_len
            continue
        i += 1
    return first_open if depth > 0 else -1


def _tail_before_unclosed_block(md: str) -> str:
    """截取 md 中第一个**未闭合** `<think>` / `<tool>` / 注册 tag / 渲染型 fence 之前的部分。

    差量渲染的 tail（未闭合尾部）含未闭合协议块时会被静默丢弃（防思考内容泄漏
    到正文、防半截 `<tool>` 被渲染成假卡片、防半截图表源码行内渲染成代码块）。
    但 JS `updateContentAppend` 会无条件 remove 全部 `[data-incremental]` 节点
    ——若 tail 整段不重建，未闭合块**之前**已经显示出来的正文会跟着一起消失，
    且此后无人补回（用户可见“流式输出吞内容”）。

    因此丢弃只应发生在未闭合块起点**之后**：之前的正文照常行内渲染。
    """
    if not md:
        return md
    _fence_pos = _first_unclosed_chart_fence_pos(md)
    if not _has_unclosed_think_or_tool(md) and _fence_pos == -1:
        return md
    cut = len(md)
    pairs = [("<think>", "</think>"), ("<tool>", "</tool>")]
    pairs += [(f"<{t}>", f"</{t}>") for t in _registered_tag_names_safe()]
    for open_tag, close_tag in pairs:
        pos = _last_unpaired_open_pos(md, open_tag, close_tag)
        if pos != -1:
            cut = min(cut, pos)
    if _fence_pos != -1:
        cut = min(cut, _fence_pos)
    return md[:cut] if cut != len(md) else md


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
    # 插件注册 tag 同防护：切片起点落在未闭合 tag 内部（历史遗留基线）时，
    # 第一个 close 在 open 之前 → 整个切片不产出，交给全量渲染兜底
    for _tag in _registered_tag_names_safe():
        _fo = md.find(f"<{_tag}>")
        _fc = md.find(f"</{_tag}>")
        if _fc != -1 and (_fo == -1 or _fc < _fo):
            return 0, []

    segments = []
    stable_len = 0
    i = 0
    n = len(md)
    fence_open = False  # 是否在 ``` 代码块内（跨段累计）
    fence_start = 0  # fence 开启段起点偏移（fence 跨 \n\n 时闭合段需回溯到此）
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
            # 🐛 修复（流式闪现孤立空代码块）：fence 跨 \n\n 时闭合段只是代码块
            # 尾部，若单独产出：①开启段/中间段落在 stable 内却从未追加
            # （updateContentAppend 删增量节点时连带删掉 tail 行内渲染的完整
            # 代码块）；②尾段经 _sanitize_incomplete_markdown 补闭合渲染成
            # 「半截正文 + 空 Plain Text 代码块」，全量渲染才恢复。
            # 回溯到 fence 开启段起点，把整个 fence 区间作为完整闭合段产出。
            seg = md[fence_start:seg_end]
        else:
            if fence_count % 2 == 1:
                # 段内 fence 打开（未闭合）→ 不产出，fence 状态延续到下一段
                fence_open = True
                fence_start = i
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
        # 插件注册 tag（如 <mood>，人格块常含 \n\n 多段落）未闭合 → 停止：
        # 半截 tag 段若照常产出，基线推进到 tag 内部，闭合后的 tail 无 open 有
        # close → 孤立 close 被清理、内容当正文渲染（泄漏后随全量渲染消失）
        if _has_unclosed_registered_tag(seg):
            break

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
    safe_md = _protect_inline_svg_blocks(safe_md)
    safe_md = _extract_formulas(safe_md)  # KaTeX 公式提取
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    # fence 内容保护：代码块内协议标签不被 inject 抽出渲染成假卡片
    _fences, safe_md = _extract_fenced_code(safe_md)
    processed_md = _inject_think_cards(safe_md, True, compact=compact)
    processed_md = _inject_tool_blocks(processed_md, True, compact=compact)
    processed_md = _inject_hook_blocks(processed_md, True)
    processed_md = _inject_tag_cards(processed_md, True, compact=compact)
    processed_md = _restore_fenced_code(processed_md, _fences)
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


# ── [PERF] 尾部行内渲染的「纯文本快路径」─────────────────────────────────
# 流式期间 _render_tail_inline 每次都要把整个 tail 走一遍完整管线（sanitize →
# 公式提取 → code 解包 → 上下文链接 → fence 抽取 → think/tool/hook/tag 四次
# inject → md.convert → 图片解析），是十余次 O(tail) 扫描 + 一次完整 markdown
# 转换。中文正文绝大多数时候 tail 是**纯文本**（没有 `` ` `` `*` `[` 等任何
# markdown 语法），此时这些扫描全部是无效功。
#
# 命中快路径时直接 escape 输出（与 nl2br 扩展对齐：空行分段、段内换行转
# <br>），把 O(tail) × 10+ 降为 O(tail) × 1。判据保守：只要出现任一语法字符
# 就退回完整管线，**宁可少快一次，不可错渲染一次**。
# 尾部代码块包装（pygments 高亮 + copy 按钮 + 语言标签）的规模上限（字符）。
# [T35] _render_inline_tail 原本**不包装**代码块 —— 与全量渲染
# （_render_markdown_to_html_cached_impl）和差量段（_render_stable_segment）
# 都不一致：流式期间代码块是素色 <pre>（实测同一段 md：inline_tail 87 字节
# vs 全量 2663 字节，差 30 倍），流式结束全量渲染时才变成带高亮/行号/语言
# 标签的卡片 → 每轮回答结束都「代码块整体变样」一次，也是差量收尾产物与
# 全量不一致的根因。补上包装后二者字节级一致。
# 代价：pygments 是 tail 渲染里最贵的一步（200 行 ≈ 5.7ms，而整段 markdown
# convert 只要 0.2ms）。流式期间代码块单调增长、每轮 tail 都重高亮整块 →
# O(n²)。超过本阈值退回素色，结束后由全量渲染补齐。
_TAIL_CODE_WRAP_MAX_CHARS = 4000

_TAIL_MD_SYNTAX_RE = re.compile(
    r"[`*_~\[\]#<>|\\$]"  # 行内/块级 markdown 标记（$ 为公式定界符，一并保守排除）
    r"|!\["  # 图片
    r"|https?://"  # 自动链接
    r"|^\s{0,3}(?:[-+*]|\d+[.)])\s"  # 列表项
    r"|^\s{0,3}>"  # 引用
    r"|^\s{0,3}#{1,6}\s"  # 标题
    r"|^\s{0,3}```"  # 代码围栏
    r"|^\s{0,3}\|.*\|"  # 表格行
    r"|^\s{0,3}(?:---+|\*\*\*+)$",  # 分隔线
    re.MULTILINE,
)


def _render_plain_tail(text: str) -> str:
    """纯文本尾部的等价 HTML（仅供 _render_inline_tail 快路径使用）。

    与 markdown + nl2br 的输出对齐：空行分段为 <p>，段内单换行转 <br>。
    """
    parts = []
    for para in text.split("\n\n"):
        para = para.strip("\n")
        if not para.strip():
            continue
        parts.append("<p>" + escape(para).replace("\n", "<br>") + "</p>")
    return "".join(parts)


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
    # [PERF] 纯文本快路径：无任何 markdown / 公式 / 扩展语法时直接 escape 输出，
    # 跳过下方十余道 O(tail) 扫描与一次完整 markdown 转换。中文正文命中率极高
    # （实测长段落流式下这是主线程最大的单项开销）。
    if not _TAIL_MD_SYNTAX_RE.search(md_text):
        return _render_plain_tail(md_text)
    safe_md = _sanitize_incomplete_markdown(md_text)
    safe_md = _extract_formulas(safe_md)  # KaTeX 公式提取
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    # fence 内容保护：代码块内协议标签不被 inject 抽出渲染成假卡片
    _fences, safe_md = _extract_fenced_code(safe_md)
    processed_md = _inject_think_cards(safe_md, True, compact=compact)
    processed_md = _inject_tool_blocks(processed_md, True, compact=compact)
    processed_md = _inject_hook_blocks(processed_md, True)
    processed_md = _inject_tag_cards(processed_md, True, compact=compact)
    processed_md = _restore_fenced_code(processed_md, _fences)
    md = get_markdown_instance()
    md.reset()
    html = md.convert(processed_md)
    # [T35] 与全量/差量段渲染对齐：补代码块包装（pygments 高亮 + copy 按钮 +
    # 语言标签）。缺它时流式期间代码块是素色 <pre>，结束全量渲染才变样 ——
    # 每轮回答结束一次的可见「代码块变脸」，且让差量收尾产物与全量不一致。
    # 规模保护：超大 tail 退回素色（成本说明见 _TAIL_CODE_WRAP_MAX_CHARS）。
    if len(md_text) <= _TAIL_CODE_WRAP_MAX_CHARS:
        try:
            html = _wrap_code_blocks_with_copy_button_web(html)
        except Exception:  # 包装失败绝不能让正文消失：保留未包装的 HTML
            pass
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
                       330→450→600：流式长回复展示更多正文。 */
                    max-height: 600px;
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
"""

_STREAMING_DOCK_JS = """
                // ===== 流式活动坞（Streaming Dock）=====
                window._streamingActive = false;
                function _setStreamingDock(active, collapseAfter) {
                    // 仅简洁模式启用坞态
                    var on = !!active && !!window._toolCompactMode;
                    var wasOn = document.body.classList.contains('streaming-dock');
                    window._streamingActive = !!active;
                    if (on === wasOn) return;
                    var ts = document.getElementById('tool-section');
                    // 切换前记录工具区高度与用户是否在底部，用于阅读位置补偿
                    var _dockH = ts ? ts.offsetHeight : 0;
                    var _atBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < 40;
                    // FLIP：工具区在「沉底 ↔ 顶部」之间换位（CSS order 调换）是瞬移，
                    // 记录切换前位置，切换后补间成平滑位移。
                    if (typeof window._flipArm === 'function') window._flipArm(1200);
                    var _flipPrev = (typeof window._flipCapture === 'function') ? window._flipCapture() : null;
                    document.body.classList.toggle('streaming-dock', on);
                    // [T30] 归位同帧折叠：坞态归位本会让 #tool-content 从 220px 限高
                    // 展开到自然高度（工具多时暴涨数百 px），随后 finish 流程又立刻
                    // 把它折叠回 0 —— 「先胀后缩」的往返峰是结束态剧烈抖动的主因，
                    // 而终态本来就是折叠，中间那次全展开纯属浪费。collapseAfter=true
                    // 时在同一同步块内连置折叠属性：两个 max-height 变化合并为一帧，
                    // 浏览器只算一条 220px→0 的过渡曲线，自然高度峰值从不进布局。
                    // 折叠的 transitionend 终值单报由函数尾部统一的
                    // _beginToolSectionTransition 承接（本帧内开启抑制，无漏报窗口）。
                    if (on === false && wasOn && collapseAfter && (window._toolCompactMode !== false) && ts) {
                        ts.setAttribute('data-collapsed', 'true');
                        var _sepC = document.getElementById('tool-separator');
                        if (_sepC) _sepC.setAttribute('aria-expanded', 'false');
                    }
                    if (!on && wasOn) {
                        // 🐛 修复（坞态归位正文置顶）：坞态下正文容器限高内滚，用户
                        // 阅读位置在 #content-placeholder.scrollTop。归位移除
                        // max-height 后 clientHeight 骤增至全高，浏览器把该值钳到 0
                        // → 正文跳顶、阅读位置丢失（坞态内展开工具完成框阅读时必现）。
                        // 切 class 前先读出位置，切换后迁移到新滚动容器 document.body；
                        // 归位后正文起点在工具区下方，迁移值需加工具区实际高度。
                        var _cpDock = document.getElementById('content-placeholder');
                        var _cpReading = _cpDock ? _cpDock.scrollTop : 0;
                        if (_cpReading > 0 && _cpDock) {
                            _cpDock.scrollTop = 0;
                            var _tsDocked = ts ? ts.offsetHeight : 0;
                            var _cpMigrated = _cpReading + (_tsDocked > 0 ? _tsDocked : 0);
                            var _bodyMax = Math.max(0, document.body.scrollHeight - document.body.clientHeight);
                            document.body.scrollTop = Math.min(_cpMigrated, _bodyMax);
                        }
                        // 坞态 → 归位顶部：正文整体下移 ≈ 工具区高度，
                        // 用户上滚阅读时补偿 scrollTop，避免阅读位置跳动
                        if (!_atBottom && _dockH > 0) {
                            document.body.scrollTop = document.body.scrollTop + _dockH;
                        }
                        // 归位后滚到底部展示最新条目——仅当用户未在上方阅读时；
                        // 用户上滚查看中则保持其位置（内容未更新，不打扰阅读）
                        var tc = document.getElementById('tool-content');
                        if (tc && !tc._userScrolledUp) { _progScroll(tc, tc.scrollHeight); }
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
                            _progScroll(_cp, _cp.scrollHeight);
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
                    // FLIP Play（入队串行：不会与后续重排/折叠动画同时开跑）
                    if (_flipPrev && typeof window._flipPlay === 'function') window._flipPlay(_flipPrev, 220);
                }
"""

# ── 复用前的内容清理（保留骨架）──
# 归还 WebViewPool 时执行：只清空内容容器与易失状态，**不重建文档**。
# 与 setHtml("") 相比省掉一次文档重建 + 骨架 JS 重新执行（setInterval /
# ResizeObserver / 事件监听全套重注册），加载大会话时批次反复卸载重建的
# 内存与 CPU 成本显著下降。
_RESET_CONTENT_FOR_REUSE_JS = """
                (function () {
                    var c = document.getElementById('content-placeholder');
                    if (c) { c.innerHTML = ''; c.removeAttribute('data-pending-break'); c.scrollTop = 0; }
                    var t = document.getElementById('tool-content');
                    if (t) { t.innerHTML = ''; t.scrollTop = 0; }
                    var ts = document.getElementById('tool-section');
                    if (ts) { ts.removeAttribute('data-collapsed'); ts.style.display = ''; }
                    // 坞态/滚动跟随等易失标志复位（避免沿用上一张卡片的阅读状态）
                    document.body.classList.remove('streaming-dock');
                    document.body.scrollTop = 0;
                    window._userScrolledWithin = false;
                    window._userScrolledUp = false;
                    window._suppressScrollEvent = false;
                    window._streamingActive = false;
                    // 图表：vault 里的节点已随 innerHTML 清空，逐个 dispose 防孤儿实例
                    if (window.__chartVault && window.__chartVault.size) {
                        window.__chartVault.forEach(function (el) {
                            if (typeof window._disposeChartNode === 'function') window._disposeChartNode(el);
                        });
                        window.__chartVault.clear();
                    }
                    // 打字机缓冲（页面仍存活，若上一张卡有未揭示文本必须丢弃）
                    if (typeof window._twReset === 'function') window._twReset();
                })();
"""

# ── 打字机揭示队列（Typewriter Reveal Queue）骨架资产 ──
# 为什么需要它：
# 流式正文的到达节奏由**网络 chunk**决定（上游 80ms 批处理 + 令牌生成抖动，
# 中文长段落常无 \n\n 闭合段 → 安全定时器兜底 150~500ms 才渲染一次），
# 每次到达就把整块文本一次性塞进 DOM → 观感是"一块一块蹦出来"，没有打字机感。
# 队列把"到达节奏"与"显示节奏"解耦：Python 只管把文本 push 进来，
# JS 用 requestAnimationFrame 按帧揭示（~60fps），积压时用指数追赶收敛
# （CATCHUP_MS 内排空），既不拖慢最终显示，也不因突发 chunk 越拉越长。
#
# 与既有渲染路径的关系：
# - 揭示内容同样走 window._dfxAppendStreamText（与旧逻辑同一套段落/host 判定）；
# - updateContent / updateTailHtml / updateContentAppend 会**整体替换**增量节点，
#   且 Python 侧 markdown 已包含全部文本（含未揭示部分），故入口调用 _twReset()
#   丢弃缓冲即可，不会丢字。
_TYPEWRITER_JS = """
                // ===== 打字机揭示队列 =====
                window._tw = {
                    buf: "",          // 待揭示文本
                    raf: 0,           // rAF 句柄（0 = 未运行）
                    last: 0,          // 上一帧时间戳
                    enabled: true,    // 总开关（灰度/降级用）
                    CATCHUP_MS: 110,  // 目标追赶窗口：积压在此时间内排空
                    BURST_LEN: 400,   // 超过该积压视为突发，加速揭示
                    GATE_MIN_BUF: 3,  // DOM 替换闸门：缓冲不高于此值即执行（跳变量上限）
                    GATE_STILL_MS: 60,      // 静默窗：距最近一次 push 超此毫秒即认为"间隙"，可执行替换
                    GATE_POLL_MS: 30,       // 静默窗轮询间隔（buf 空时 rAF 停摆，需独立轮询）
                    GATE_MAX_WAIT_MS: 400,  // 硬上限：连续高速流式下防格式化无限延后
                    BOOST_FACTOR: 0.3,      // 挂起期间揭示加速（τ × 此系数）
                    pushed: 0,        // 累计 push 字符数
                    _boost: false,    // 闸门挂起中：揭示加速标记
                    _force: false,    // 强制执行一次标记（超时兜底路径放行，防自我挂起）
                    _lastPushAt: 0,   // 最近一次 push 时刻（静默窗判定）
                    _gateDeadline: 0, // 挂起硬上限时刻
                    // [T31] 挂起的 DOM 替换改为 FIFO 队列（原单槽覆盖）：
                    // updateContentAppend 的 newHtml **只含本轮新闭合段**（Python 侧
                    // 每轮推进 _stable_md_len，后续轮次不再包含早前段），因此
                    // 「新任务含更多文本、旧任务语义被包含」的前提对它不成立 ——
                    // 单槽覆盖 = 被覆盖那轮的段格式化 HTML 永不落地，而紧随的替换
                    // 又无条件移除全部 [data-incremental] 节点（含承载该段纯文本的
                    // 节点）→ 该段从屏上消失且无人补回（用户可见“流式正文偶发丢段”）。
                    // 实测：40 段 × 30ms 喂入，闸门覆盖 3 次 → 流式态缺 7 个段落 token；
                    // 禁用闸门后覆盖 0 次、缺失 0。
                    _gateQueue: [],   // 挂起的 DOM 替换队列（按序执行，不丢任务）
                    _gateTimer: 0,    // 静默窗轮询句柄
                    _gateExtra: ''    // 挂起期间新 push 的文本（不在替换快照里，替换后补回）
                };
                window._twPush = function (text) {
                    var st = window._tw;
                    if (!st || !st.enabled || !text) return;
                    // 骨架尚未注册追加函数（理论上不会发生）：退化为直接调用
                    if (typeof window._dfxAppendStreamText !== 'function') return;
                    st.buf += text;
                    st.pushed += text.length;
                    st._lastPushAt = performance.now();
                    // 闸门挂起期间的新文本不在替换快照里：记账并在替换后补回
                    // （它已随揭示上屏，替换会整段移除，不补回就真丢了）
                    if (st._gateQueue.length) st._gateExtra += text;
                    if (!st.raf) {
                        st.last = performance.now();
                        st.raf = requestAnimationFrame(window._twStep);
                    }
                };
                window._twStep = function (ts) {
                    var st = window._tw;
                    if (!st) return;
                    st.raf = 0;
                    if (!st.buf) return;
                    var now = (typeof ts === 'number' && ts > 0) ? ts : performance.now();
                    var dt = Math.max(1, Math.min(200, now - st.last));
                    st.last = now;
                    // 揭示时间常数：闸门挂起中（等替换）时缩短，尽快把积压放完
                    // ——跳变量 = 执行替换时的剩余缓冲，加速直接压低它
                    var _tau = st.CATCHUP_MS;
                    if (st._boost) _tau = Math.max(16, st.CATCHUP_MS * (st.BOOST_FACTOR || 0.3));
                    // 每帧揭示量：按"剩余缓冲在 CATCHUP_MS 内排空"做指数追赶（最少 1 字）
                    var n = Math.max(1, Math.ceil(st.buf.length * (dt / _tau)));
                    // 突发积压（网络一次送来一大段）：提高下限，避免越拖越长
                    if (st.buf.length > st.BURST_LEN) {
                        n = Math.max(n, Math.ceil(st.buf.length / 8));
                    }
                    var slice = st.buf.slice(0, n);
                    st.buf = st.buf.slice(n);
                    // [PERF] 高度上报节流：揭示是每帧进行，但高度上报会触发
                    // reportHeight → Python setFixedHeight → Chromium 视口变化 →
                    // 重排的回环。按帧上报会让回环频率翻数倍（流式卡顿来源），
                    // 这里限制上报频率——与旧"按 chunk 上报"的节奏一致。
                    // [T29] 80 → 40ms：目标值进入 Python 追踪的频率。应用侧
                    // （追踪 tick 30ms 节拍）已连续化且回环被封死，上报端唯一
                    // 代价是 console.log IPC（微秒级），收紧只减滞后不增成本。
                    var _skipReport = (now - (st.reportedAt || 0)) < 40;
                    if (!_skipReport) st.reportedAt = now;
                    try {
                        window._dfxAppendStreamText(slice, _skipReport);
                    } catch (e) {}
                    // 缓冲降到水位（或已空）：执行挂起的 DOM 替换
                    if (st._gateQueue.length && st.buf.length <= (st.GATE_MIN_BUF || 3)) {
                        window._twDrainGate();
                    }
                    // ⚠️ 不得在此 return：drainGate 后 buf 可能仍非空（非水位路径），
                    // 提前返回会断掉 rAF 链，残留文本不再揭示
                    if (st.buf) {
                        st.raf = requestAnimationFrame(window._twStep);
                    }
                };
                // ── DOM 替换闸门（消除"成块蹦字"）──
                // 背景：updateTailHtml / updateContentAppend 会用「含全部文本」的
                // 格式化 HTML 整体替换增量节点，并在入口 _twReset() 丢弃未揭示缓冲
                // —— 揭示途中被打断时，文字从"已揭示量"瞬跳到"全量"（实测每
                // ~200ms 一次、单次 4~9 字符），观感就是"一顿一顿成块蹦出"。
                //
                // 执行时机（三者取先）：
                //   ① 缓冲降到水位（buf <= GATE_MIN_BUF）：跳变量压到水位以内；
                //   ② 静默窗（距最近 push >= GATE_STILL_MS）：chunk 间隙到了——
                //      持续流式下 buf 稳态高于水位，仅靠①永不触发（只能等超时 →
                //      大跳变），间隙是天然的"文字已停在屏上"时刻；
                //   ③ 硬上限（挂起累计 >= GATE_MAX_WAIT_MS）：防格式化无限延后。
                // 挂起期间揭示加速（τ×BOOST_FACTOR），尽快把积压放完压低①的等待。
                // 返回 true = 已挂起；返回 false = 立即执行（缓冲已在水位内）。
                window._twGate = function (fn) {
                    var st = window._tw;
                    if (!st || !st.enabled || typeof fn !== 'function') return false;
                    // 强制执行标记（超时兜底 / 排空放行）：本次放行，防自我挂起
                    if (st._force) { st._force = false; return false; }
                    if (!st.buf || st.buf.length <= (st.GATE_MIN_BUF || 3)) return false;
                    // [T31] 入队而非覆盖：追加语义的 updateContentAppend 之间互相
                    // 不包含（newHtml 只含本轮新闭合段），覆盖即永久丢失该段。
                    st._gateQueue.push(fn);
                    // 清空 extra：新快照含旧 extra 的全部文本（Python 侧 markdown
                    // 是单调增长的），旧账不清会重复补回
                    st._gateExtra = '';
                    st._boost = true;
                    if (!st._gateTimer) {
                        // 静默窗轮询：buf 揭示完时 rAF 链停摆，静默判定必须独立于
                        // _twStep（否则高速流式挂起后无人检查窗口）
                        st._gateDeadline = performance.now() + (st.GATE_MAX_WAIT_MS || 400);
                        st._gateTimer = setInterval(function () {
                            var now = performance.now();
                            var quiet = (now - (st._lastPushAt || 0)) >= (st.GATE_STILL_MS || 60);
                            if (quiet || now >= st._gateDeadline) {
                                window._twDrainGate();  // 内部 clearInterval
                            }
                        }, st.GATE_POLL_MS || 30);
                    }
                    return true;
                };
                window._twDrainGate = function () {
                    // 执行挂起的 DOM 替换（水位 / 静默窗 / 硬上限）
                    var st = window._tw;
                    if (!st || !st._gateQueue.length) return;
                    if (st._gateTimer) { clearInterval(st._gateTimer); st._gateTimer = 0; }
                    st._boost = false;
                    var queue = st._gateQueue;
                    var extra = st._gateExtra;
                    st._gateQueue = [];
                    st._gateExtra = '';
                    // 强制放行：f 内部重入渲染入口 → _twGate 时必须直接执行
                    // （硬上限路径下缓冲可能仍高于水位，无此标记会再次挂起自己 →
                    //  替换永不执行、格式化永久停留纯文本）
                    // [T31] 按序执行全部挂起任务（原实现为单槽，后者覆盖丢弃前者）：
                    // updateContentAppend 各轮的 newHtml 互不包含（只含本轮新闭合段），
                    // 覆盖即该段格式化 HTML 永不落地 → 随后替换又移除 [data-incremental]
                    // 节点 → 该段从屏上消失。逐任务独立 try：单个失败不阻断其余。
                    for (var _qi = 0; _qi < queue.length; _qi++) {
                        st._force = true;
                        try {
                            queue[_qi]();
                        } catch (e) {
                        } finally {
                            // f 未重入（异常/其他路径）时清理，防标记外泄误放行下一次
                            st._force = false;
                        }
                    }
                    // 替换快照不含挂起期间新 push 的文本：替换后补回。走 _twPush
                    // 而非直接上屏 —— 保留打字机节奏，避免补回本身变成一次跳变
                    // （挂起期间该文本已在屏上，同帧重排无闪烁）。
                    if (extra) {
                        try { window._twPush(extra); } catch (e) {}
                    }
                };
                window._twFlush = function () {
                    // 立即揭示全部（需要"当前文本必须已在 DOM"的场景）
                    var st = window._tw;
                    if (!st) return;
                    if (st.raf) { cancelAnimationFrame(st.raf); st.raf = 0; }
                    // 挂起的 DOM 替换作废：flush 后的调用方（updateContent）
                    // 会用含全部文本的新 HTML 整页替换，旧替换已无意义
                    if (st._gateTimer) { clearInterval(st._gateTimer); st._gateTimer = 0; }
                    st._gateQueue = [];
                    st._gateExtra = '';
                    st._boost = false;
                    if (st.buf) {
                        var all = st.buf;
                        st.buf = "";
                        // [#12 R3] updateContent 场景 flush 后立即整页替换，
                        // _dfxAppendStreamText 尾部的 auto-scroll 与
                        // _userScrolledWithin/_prevScrollTop 基线复位是有害副作用
                        // （滚到底随即被替换丢弃，body 滚动与跟随态却被污染）。
                        // 快照受影响状态、揭示后还原 → 纯揭示语义，不动 _dfx 本体；
                        // cp 的滚动位置由 _endDomUpdate 锚点机制接管。
                        var _svWithin = window._userScrolledWithin;
                        var _svPrev = window._prevScrollTop;
                        var _svBody = document.body ? document.body.scrollTop : 0;
                        try { window._dfxAppendStreamText(all); } catch (e) {}
                        if (document.body) document.body.scrollTop = _svBody;
                        window._userScrolledWithin = _svWithin;
                        window._prevScrollTop = _svPrev;
                    }
                };
                window._twReset = function () {
                    // DOM 已被整体替换：丢弃缓冲（Python 侧 markdown 已含全部文本，
                    // 新渲染结果自带这些文字，保留反而会重复）
                    var st = window._tw;
                    if (!st) return;
                    if (st.raf) { cancelAnimationFrame(st.raf); st.raf = 0; }
                    st.buf = "";
                    // 挂起的替换同样作废：它即将执行的替换对象（旧 DOM）已被本次
                    // 替换覆盖，残留执行会造成旧 HTML 回灌
                    if (st._gateTimer) { clearInterval(st._gateTimer); st._gateTimer = 0; }
                    st._gateQueue = [];
                    st._gateExtra = '';
                    st._boost = false;
                };
"""

# ── 预览文字打字机（Preview Typewriter）骨架资产 ──
# 背景：思考/工具预览文字是「静默累积 → 一次性全量渲染」落地的（append_reasoning 的
# 既定设计，避免每 chunk 全量重排导致 think-streaming DOM 反复销毁重建），因此它
# 出现在 DOM 的那一刻是整段瞬间出现，观感生硬。这里让**预览行**逐字显现：
#   - 首次落地：从空打到完整预览文本
#   - 后续更新：从"已显示文本"续打到新文本，只补增量，不重放已显示部分
# 只作用于带 [data-dfx-preview] 的短文本预览行（单行、nowrap、高度恒定），
# 不动正文/思考正文——长文本逐字成本高且与 _tw 揭示队列职责重叠。
# 全文存 data-dfx-text 属性（HTML 已转义），JS 只写首个文本节点，
# 子元素（如工具的"(N字符)"计数 span）不受影响。
_PREVIEW_TYPEWRITER_JS = """
                // ===== 预览文字打字机 =====
                window._pt = {
                    shown: Object.create(null),  // key -> 已显现文本
                    node: Object.create(null),   // key -> 当前正在写的文本节点
                    raf: Object.create(null),    // key -> rAF 句柄（0 = 空闲）
                    enabled: true,
                    TOTAL_MS: 240,   // 单次要补的字符数在此时间内补齐
                    MAX_CHARS: 120   // 超过该长度直接全显（长预览不逐字）
                };
                window._ptType = function (el, key, full) {
                    var st = window._pt;
                    if (!st || !st.enabled || !el) return;
                    if (st.shown[key] === full) return;
                    var node = el.firstChild;
                    if (!node || node.nodeType !== 3) {
                        // 没有可直接写的文本节点（理论上预览行必有）：
                        // 退化直接显示，不影响可见性
                        st.shown[key] = full;
                        return;
                    }
                    // 打字中被全量重渲染打断（流式每 150~500ms 就会 innerHTML 重建一次，
                    // 且 updateContent 内部 reorganizeContent 之后还会再播一次）：
                    // 新节点携带完整文本，若从头重打会"全显→清空→再补"闪一下。
                    // 这里只把写指针切到新节点的首个文本节点，动画按原进度续跑。
                    if (st.raf[key]) {
                        if (st.node[key] !== node) st.node[key] = node;
                        return;
                    }
                    var shown = st.shown[key] || "";
                    // 非前缀延续（预览被整体替换/回退）→ 从头重打
                    if (full.indexOf(shown) !== 0) shown = "";
                    var delta = full.length - shown.length;
                    if (delta > st.MAX_CHARS) {
                        node.nodeValue = full;
                        st.shown[key] = full;
                        return;
                    }
                    node.nodeValue = shown;
                    var step = Math.max(1, Math.ceil(delta / (st.TOTAL_MS / 16)));
                    var pos = shown.length;
                    st.node[key] = node;
                    var tick = function () {
                        pos = Math.min(full.length, pos + step);
                        var n = st.node[key];
                        if (n && n.nodeType === 3) n.nodeValue = full.slice(0, pos);
                        if (pos < full.length) {
                            st.raf[key] = requestAnimationFrame(tick);
                        } else {
                            st.raf[key] = 0;
                            st.node[key] = null;
                            st.shown[key] = full;
                        }
                    };
                    st.raf[key] = requestAnimationFrame(tick);
                };
                window._ptPlay = function (root) {
                    var st = window._pt;
                    if (!st || !st.enabled) return;
                    var els = (root || document).querySelectorAll('[data-dfx-preview]');
                    for (var i = 0; i < els.length; i++) {
                        var el = els[i];
                        var full = el.getAttribute('data-dfx-text');
                        if (full === null) continue;
                        window._ptType(el, el.getAttribute('data-dfx-key') || ('pt' + i), full);
                    }
                };
"""

# ── FLIP 位移动画 + 动画串行队列骨架资产 ──
# 解决"流式结束时工具与思考弹到最顶上"：
# 简洁模式下流式期间工具区沉底（body.streaming-dock 纯 CSS order 调换），
# 结束时归位顶部 + 最终全量重排（innerHTML 整体替换）+ 自动折叠三个动作叠加，
# 每个都带 200ms 过渡 → 视觉上是一次生硬跳变 + 三动画互相抢帧的卡顿。
#
# 对策：
# 1) FLIP（First-Last-Invert-Play）：动作前记录关键容器/块的视口位置，
#    动作后把它们 transform 反向补偿回旧位置，再动画归零 → 位置变化被
#    "补间"成平滑位移，而不是瞬移。
# 2) 动画串行队列 _animEnqueue：归位 / 重排 / 折叠不再同时开跑，
#    按入队顺序一条一条放，避免三段 200ms 过渡叠加造成的掉帧与位置抖动。
_FLIP_JS = """
                // ===== 动画串行队列（结束态三段动画不叠加）=====
                window._animQ = { items: [], running: false };
                window._animEnqueue = function (fn, dur) {
                    var q = window._animQ;
                    if (!q) return;
                    q.items.push({ fn: fn, dur: dur || 0 });
                    if (!q.running) window._animNext();
                };
                window._animNext = function () {
                    var q = window._animQ;
                    if (!q) return;
                    var it = q.items.shift();
                    if (!it) { q.running = false; return; }
                    q.running = true;
                    try { it.fn(); } catch (e) {}
                    if (it.dur > 0) {
                        setTimeout(window._animNext, it.dur);
                    } else {
                        window._animNext();
                    }
                };

                // ===== FLIP：位置突变 → 平滑位移 =====
                window._flipFind = function (key) {
                    if (!key) return null;
                    if (key.charAt(0) === '#') return document.getElementById(key.slice(1));
                    var kind = key.slice(0, 3);
                    var val = key.slice(3);
                    if (kind === 'tc:') return document.querySelector('[data-tool-call-id="' + val + '"]');
                    if (kind === 'bk:') return document.querySelector('[data-block-key="' + val + '"]');
                    if (kind === 'fk:') return document.querySelector('[data-flip-key="' + val + '"]');
                    return null;
                };
                // ⚡ 性能：capture 会强制同步布局（getBoundingClientRect），
                // 流式期间每次全量渲染都做一遍代价过高。只有在"即将发生位置突变"
                // 时（坞态归位 / 流式结束的最终重排）由调用方 arm 一个短窗口，
                // 窗口内才真正采集；其余渲染 capture 直接返回 null（零开销）。
                window._flipArm = function (ms) {
                    window._flipArmedUntil = performance.now() + (ms || 1200);
                };
                window._flipCapture = function () {
                    if (!(window._flipArmedUntil > performance.now())) return null;
                    var map = new Map();
                    ['tool-section', 'content-placeholder'].forEach(function (id) {
                        var el = document.getElementById(id);
                        if (el) map.set('#' + id, el.getBoundingClientRect());
                    });
                    document.querySelectorAll('[data-tool-call-id]').forEach(function (el) {
                        var id = el.getAttribute('data-tool-call-id');
                        if (id) map.set('tc:' + id, el.getBoundingClientRect());
                    });
                    document.querySelectorAll('[data-block-key]').forEach(function (el) {
                        var k = el.getAttribute('data-block-key');
                        if (k) map.set('bk:' + k, el.getBoundingClientRect());
                    });
                    // 思考块：流式态（.think-streaming）与完成态（.think-compact/
                    // .think-block）DOM 结构不同、block-key 也不同（内容哈希），
                    // 只有位置键 data-flip-key 能跨形态配对。
                    document.querySelectorAll('[data-flip-key]').forEach(function (el) {
                        var k = el.getAttribute('data-flip-key');
                        if (k) map.set('fk:' + k, el.getBoundingClientRect());
                    });
                    return map;
                };
                window._flipPlay = function (prev, dur) {
                    if (!prev || !prev.size || typeof window._animEnqueue !== 'function') return;
                    dur = dur || 200;
                    var moves = [];
                    prev.forEach(function (rect, key) {
                        var el = window._flipFind(key);
                        if (!el || !el.isConnected) return;
                        var r2 = el.getBoundingClientRect();
                        var dx = rect.left - r2.left;
                        var dy = rect.top - r2.top;
                        // 位移过小（亚像素/重排噪声）不做动画，避免无谓的 transform 抖动
                        if (Math.abs(dx) < 2 && Math.abs(dy) < 2) return;
                        moves.push({ el: el, dx: dx, dy: dy });
                    });
                    if (!moves.length) return;
                    window._animEnqueue(function () {
                        // Invert：先无过渡地搬回旧位置
                        moves.forEach(function (m) {
                            m.el.style.transition = 'none';
                            m.el.style.transform = 'translate(' + m.dx + 'px,' + m.dy + 'px)';
                        });
                        void document.body.offsetHeight;  // 强制同步样式，让跳变立即生效
                        // Play：过渡到 0（回到真实新位置）
                        moves.forEach(function (m) {
                            m.el.style.transition = 'transform ' + dur + 'ms cubic-bezier(0.22, 0.61, 0.36, 1)';
                            m.el.style.transform = '';
                        });
                        setTimeout(function () {
                            moves.forEach(function (m) {
                                m.el.style.transition = '';
                                m.el.style.transform = '';
                            });
                        }, dur + 40);
                    }, dur + 30);
                };
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
                // ── 滚动锚点：DOM 重建期间保持阅读位置 ──
                // 旧实现用「绝对 scrollTop + 新 max 钳制」恢复，而 save 与 restore 之间
                // 内容结构会变（reorganizeContent 搬走工具/思考块、save/restore 移除工具
                // 块）→ scrollHeight 收缩 → 恢复值被钳到「新内容底部」，与用户原阅读位置
                // 不是同一语义点（表现为「跳到别处、也不是滚底」）。
                // 改为记录「视口顶部第一个可见块 + 相对偏移」，重建后把同一块拉回同一偏移：
                // 等价于浏览器 scroll anchoring（本页多处显式禁用原生锚定），同时覆盖
                // 「上方内容被搬走」与「下方内容增长」两种情形。
                // 🐛 二次修复（置顶回归）：锚块只认「重建后仍存在且顺序稳定」的 markdown
                // 块。data-incremental（tail 增量节点，每轮整体替换）与 think/tool 块
                // （会被 reorganizeContent 搬走）一当作锚，重建后 key/idx 全对不上，
                // 恢复值系统性偏小，反复更新把阅读位置一步步顶到 0。
                function _anchorable(k) {
                    if (!k || k.nodeType !== 1) return false;
                    if (k.hasAttribute('data-incremental') || k.hasAttribute('data-tool-call-id')) return false;
                    var cl = k.classList;
                    return !(cl.contains('think-block') || cl.contains('think-streaming') ||
                             cl.contains('think-compact') || cl.contains('tool-block') ||
                             cl.contains('tool-streaming-block'));
                }
                function _anchorables(el) {
                    var out = [];
                    var kids = el.children;
                    for (var i = 0; i < kids.length; i++) {
                        if (_anchorable(kids[i])) out.push(kids[i]);
                    }
                    return out;
                }
                function _captureAnchor(el) {
                    if (!el) return null;
                    var st = el.scrollTop;
                    var list = _anchorables(el);
                    for (var i = 0; i < list.length; i++) {
                        var k = list[i];
                        if (k.offsetTop + k.offsetHeight > st) {
                            var key = (k.getAttribute && (k.getAttribute('data-order') || k.getAttribute('data-block-key'))) || '';
                            // 文本前缀：markdown 块重建后内容不变，用它校验 idx 对位是否可靠
                            var text = (k.textContent || '').replace(/\\s+/g, ' ').slice(0, 64);
                            return { idx: i, delta: k.offsetTop - st, key: key, text: text };
                        }
                    }
                    return null;
                }
                function _applyAnchor(el, a) {
                    if (!el || !a) return false;
                    var target = null;
                    if (a.key) {
                        var byKey = el.querySelector('[data-order="' + a.key + '"],[data-block-key="' + a.key + '"]');
                        if (byKey && byKey.parentNode === el) target = byKey;
                    }
                    if (!target) {
                        var list = _anchorables(el);
                        if (!list.length) return false;
                        var c = list[Math.min(a.idx, list.length - 1)];
                        // idx 对位校验：文本对不上说明块序列漂移，线性找同文本块；
                        // 再找不到就放弃锚点（回退绝对值兜底），宁准勿跳。
                        if (a.text && (c.textContent || '').replace(/\\s+/g, ' ').slice(0, 64) !== a.text) {
                            var found = null;
                            for (var i = 0; i < list.length; i++) {
                                if ((list[i].textContent || '').replace(/\\s+/g, ' ').slice(0, 64) === a.text) { found = list[i]; break; }
                            }
                            if (!found) return false;
                            c = found;
                        }
                        target = c;
                    }
                    if (!target) return false;
                    var max = Math.max(0, el.scrollHeight - el.clientHeight);
                    var want = Math.max(0, Math.min(target.offsetTop - a.delta, max));
                    if (Math.abs(el.scrollTop - want) < 1) return false;
                    _progScroll(el, want);
                    return true;
                }
                // 程序滚动令牌：计数而非布尔。布尔在「赋值前后值未变 → 不触发 scroll 事件」
                // 时会残留，吞掉下一次真实用户滚动（跟随标志错乱的根因）；rAF 兜底清零。
                function _progBegin(el) {
                    el._progDepth = (el._progDepth || 0) + 1;
                    requestAnimationFrame(function () { if (el._progDepth > 0) el._progDepth--; });
                }
                function _progScroll(el, value) {
                    _progBegin(el);
                    el.scrollTop = value;
                }
                // 用户滚动意图：只由**真实输入**驱动（wheel / 触摸 / 键盘）。scroll 事件
                // 不再承担置位职责 —— 程序性滚动与浏览器钳制同样派发 scroll，用它推断
                // 意图必然误判（P033）。置位同步完成，用于抢占「滚轮 → 在途渲染 JS 拉底」
                // 的竞争窗口（scroll 事件异步派发，抢不过渲染 JS）。
                function _bindUserScrollIntent(el) {
                    if (!el || el._intentBound) return;
                    el._intentBound = true;
                    var mark = function () {
                        // 🐛 门控：仅当容器实际可滚（内容溢出）才记为上滚 —— 无溢出时 wheel
                        // 本应转发外层聊天列表，页面内收到的事件属冒泡残留，置位会让
                        // 跟随被无关操作误锁死。
                        if (el.scrollHeight > el.clientHeight) {
                            el._userScrolledUp = true;
                            // 即时上报阅读状态：不等下一次渲染的 reportHeight，抢在
                            // 「滚轮 → 下一帧 chunk 渲染 → 高度变化外层拉底」之前。
                            if (typeof reportHeight === 'function') reportHeight();
                        }
                    };
                    el.addEventListener('wheel', function (e) { if (e.deltaY < 0) mark(); }, {passive: true});
                    el.addEventListener('touchstart', mark, {passive: true});
                    el.addEventListener('keydown', function (e) {
                        if (e.key === 'ArrowUp' || e.key === 'PageUp' || e.key === 'Home') mark();
                    });
                }
                // DOM 重建事务：最外层（save/restore 包装 / updateContent）开始前捕获锚点 +
                // 用户上滚意图，结束后按锚点复位。嵌套只由最外层生效（depth 守卫）。
                window._domUpdateDepth = 0;
                window._domUpdateAnchors = null;
                function _beginDomUpdate() {
                    if (window._domUpdateDepth++ > 0) return;
                    var cp = document.getElementById('content-placeholder');
                    var tc = document.getElementById('tool-content');
                    window._domUpdateAnchors = {
                        cp: cp ? { a: _captureAnchor(cp), top: cp.scrollTop, up: !!cp._userScrolledUp } : null,
                        tc: tc ? { a: _captureAnchor(tc), top: tc.scrollTop, up: !!tc._userScrolledUp } : null
                    };
                }
                function _endDomUpdate() {
                    window._domUpdateDepth = Math.max(0, window._domUpdateDepth - 1);
                    if (window._domUpdateDepth > 0) return;
                    var s = window._domUpdateAnchors;
                    window._domUpdateAnchors = null;
                    if (!s) return;
                    var cp = document.getElementById('content-placeholder');
                    var tc = document.getElementById('tool-content');
                    if (cp && s.cp) {
                        // 锚点失效（块整体消失）→ 兜底用绝对值钳到新 max
                        if (!_applyAnchor(cp, s.cp.a)) {
                            var cMax = Math.max(0, cp.scrollHeight - cp.clientHeight);
                            var cWant = Math.min(s.cp.top, cMax);
                            if (Math.abs(cp.scrollTop - cWant) >= 1) _progScroll(cp, cWant);
                        }
                        // 恢复用户上滚意图：重建期的钳制不得反过来改写跟随态
                        cp._userScrolledUp = s.cp.up;
                    }
                    if (tc && s.tc) {
                        // [#12 R1] display:none 期间节点在渲染树外，clientHeight/
                        // scrollHeight 均为 0 → tMax=0，钳制恢复会把 scrollTop 写 0
                        // （顶掉 display 切换点的快照）。隐藏时跳过钳制，仅恢复跟随
                        // 标志；滚动位置由 display='' 处的快照写回接管。
                        var _tsEl = document.getElementById('tool-section');
                        var _tsGone = _tsEl && _tsEl.style.display === 'none';
                        if (!_tsGone) {
                            if (!_applyAnchor(tc, s.tc.a)) {
                                var tMax = Math.max(0, tc.scrollHeight - tc.clientHeight);
                                var tWant = Math.min(s.tc.top, tMax);
                                if (Math.abs(tc.scrollTop - tWant) >= 1) _progScroll(tc, tWant);
                            }
                        }
                        tc._userScrolledUp = s.tc.up;
                    }
                }
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
                            _progScroll(_cp, _cp.scrollHeight);
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
                // 用户滚动意图绑定（wheel / 触摸 / 键盘），语义见 _bindUserScrollIntent
                _bindUserScrollIntent(document.getElementById('content-placeholder'));
                document.getElementById('content-placeholder')?.addEventListener('scroll', function() {
                    var cp = this;
                    // DOM 操作期间（updateContent 重写 innerHTML / reorganizeContent
                    // 搬移 think 块）触发的程序性 scroll 事件必须忽略——与 body 监听的
                    // _suppressScrollEvent 抑制对称。
                    if (window._suppressScrollEvent) return;
                    if (cp._progDepth > 0) { cp._progDepth--; return; }
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
                // 异步渲染（mermaid/katex/echarts/widget iframe）完成后补滚底。
                // 这些渲染在 updateContent 置底**之后**才完成并增高内容 →
                // _cp（坞态正文容器）停在旧位置，下一个 chunk 的 updateContent
                // 又拉回底部 → 视口在「固定位置 ↔ 底部」往返抖动（图表卡片
                // 滚轮来回跳的根因）。增高完成后立即补滚。
                // 守卫：仅坞态流式生效；用户上滚阅读（_userScrolledUp）不拉底，
                // 由 _autoScrollStreamingBody 内部判定；非流式（历史卡片懒渲染
                // 图表）绝不滚动。
                function _autoScrollAfterAsyncRender() {
                    if (window._streamingActive && typeof _autoScrollStreamingBody === 'function') {
                        _autoScrollStreamingBody();
                    }
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


def _is_ask_content_valid(content: str) -> bool:
    """判断一段 <ask> 内容是否像真追问（一句话）。

    模型偶尔在正文里字面写出 `<ask>` 却把 `</ask>` 留到文末（或写错闭合标签），
    非贪婪匹配会把两标签之间的整段正文当成"追问内容"摘走并塞进胶囊 —— 表现为
    「正文已经渲染出来，最后却整段消失」。这里做结构熔断：超长、跨空行、夹带协议
    标签的一律判为误匹配，保留原文不摘除。
    空内容（<ask></ask>、<ask>   </ask>）仍算合法标签：要摘除，但不产出条目。
    """
    c = content.strip()
    if not c:
        return True
    if len(c) > _ASK_MAX_CONTENT_LEN:
        return False
    if "\n\n" in c:
        return False
    low = c.lower()
    return not any(t in low for t in ("<ask>", "<think>", "</think>", "<tool>", "</tool>"))


def _collect_and_strip_asks(md_text: str) -> tuple[str, list[str]]:
    """摘出正文里所有 <ask> 内容并移除标签，返回（去 ask 后的文本, 去重后的追问列表）。

    追问由模型分散输出（多数在末尾，也可能夹在段落中）。渲染时全部摘除、按内容
    去重（保序、忽略空白差异），交给 _build_ask_suggest_block 在文末集中渲染。
    strip 后空内容（如 <ask></ask>、<ask>   </ask>）直接丢弃。

    ⚠ 只摘"像追问"的标签：判据见 _is_ask_content_valid，宁可残留字面量也不能吞正文。
    """
    # 受保护区间：这些位置里的 <ask> 是示例 / 非正文文本，一律不摘（摘了等于删内容）
    #   - ``` 围栏（_CODE_BLOCK_PATTERN）与 ~~~ 围栏：代码块内容原样展示
    #   - 行内代码：同理
    #   - think / tool 协议块：本函数跑在 think/tool 注入之前，块内此时还是裸文本
    protected = [
        (m.start(), m.end())
        for p in (
            _CODE_BLOCK_PATTERN,
            _ASK_TILDE_FENCE_PATTERN,
            _ASK_INLINE_CODE_PATTERN,
            _ASK_PROTO_BLOCK_PATTERN,
        )
        for m in p.finditer(md_text)
    ]

    def _in_protected(pos: int) -> bool:
        return any(s <= pos < e for s, e in protected)

    items: list[str] = []
    seen: set[str] = set()
    found = False
    for m in _ASK_TAG_PATTERN.finditer(md_text):
        if _in_protected(m.start()):
            continue
        found = True
        content = m.group(1).strip()
        if not content:
            continue
        # 形态不像追问（超长 / 跨段 / 夹协议标签）→ 疑似误匹配，不收拢也不摘除
        if not _is_ask_content_valid(content):
            continue
        key = _MULTIPLE_SPACES_PATTERN.sub(" ", content)
        # 占位词（模型照抄提示词模板）不是真追问
        if key.lower() in _ASK_PLACEHOLDER_TEXTS or key.strip("？?。. ") in _ASK_PLACEHOLDER_TEXTS:
            continue
        if key in seen:
            continue
        seen.add(key)
        items.append(content)
    # 一个 ask 标签都没有 → 原文返回；有标签但内容全空 → 仍要摘除，避免字面残留
    if not found:
        return md_text, []

    source = md_text

    def _strip(m: re.Match) -> str:
        # 受保护区间 / 不像追问的匹配：原样保留（宁可残留字面量，也不能吞掉正文）
        if _in_protected(m.start()) or not _is_ask_content_valid(m.group(1)):
            return m.group(0)
        before = source[m.start() - 1] if m.start() > 0 else ""
        after = source[m.end()] if m.end() < len(source) else ""
        # 标签夹在文字中间时留一个空格，避免两个词被直接粘连
        return " " if before.strip() and after.strip() else ""

    md_text = _ASK_STRIP_PATTERN.sub(_strip, md_text)
    md_text = _ASK_EMPTY_BULLET_PATTERN.sub("", md_text)
    md_text = re.sub(r"\n{3,}", "\n\n", md_text)
    return md_text, items[:_ASK_MAX_ITEMS]


def _build_ask_suggest_block(items: list[str]) -> str:
    """把去重后的追问渲染成文末「你可以继续问」区块（点击仍走 context-tag 链路）。"""
    chips = "".join(
        f'<span class="context-tag" data-type="ask" data-content="{escape(it)}" data-action="ask">{escape(it)}</span>'
        for it in items
    )
    return (
        '<div class="ask-suggest">'
        '<div class="ask-suggest-title"><span class="ask-suggest-dot"></span>你可以继续问</div>'
        f'<div class="ask-suggest-list">{chips}</div>'
        "</div>"
    )


def _inject_context_links(md_text: str) -> str:
    """将 <ask>文本</ask> 或 [文本](jump/create/generate/view/session) 转换为胶囊样式的追问标签

    注：[文本](ask) 旧格式已废弃，不再识别（残留会渲染成空 markdown 链接）。

    <ask> 不就地渲染：先全文摘除去重，再统一拼到文末的追问区块（只影响卡片渲染）。

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

    md_text = _CONTEXT_LINK_PATTERN.sub(replacer, md_text)

    # 追问统一收拢到文末：正文里的 <ask> 全部摘除（去重）后集中渲染成一个区块。
    md_text, ask_items = _collect_and_strip_asks(md_text)
    if ask_items:
        md_text = md_text.rstrip() + "\n\n" + _build_ask_suggest_block(ask_items)
    return md_text


# ===== _resolve_image_src 模块级常量（避免每次渲染重编译正则+重算路径） =====
_IMG_SRC_PATTERN = re.compile(r'(<img\s[^>]*?src\s*=\s*["\'])([^"\']+)(["\'][^>]*?>)', re.IGNORECASE)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 正文图片懒加载属性：http(s) 远程图在长会话里会随滚动/重渲染反复请求，
# 一次性打开几十张卡会瞬间打出 N 个并发连接。loading=lazy 让浏览器只在图
# 片接近视口时才发起请求（已在视口内的立即加载，不影响首屏观感）；
# decoding=async 把解码挪出主线程，滚动不卡。
_IMG_LAZY_ATTRS = ' loading="lazy" decoding="async"'


def _append_img_load_attrs(suffix: str, src: str) -> str:
    """给 <img> 尾部属性串补懒加载属性（幂等）。

    Args:
        suffix: 正则 group(3)，形如 引号 + 其余属性 + '>'
        src: 图片地址

    Returns:
        补好属性的 suffix；跳过场景原样返回。

    跳过 qrc:/（UI 图标内联资源，lazy 无意义且可能闪一帧）与 data:/blob:
    （已在内存里，无需网络加载）。
    """
    if src.startswith(("qrc:/", "data:", "blob:")):
        return suffix
    if "loading=" in suffix or "decoding=" in suffix:
        return suffix
    if suffix.endswith(">"):
        return suffix[:-1] + _IMG_LAZY_ATTRS + ">"
    return suffix + _IMG_LAZY_ATTRS


def _resolve_image_src(html_content: str) -> str:
    """
    将 HTML 中的图片 src 相对路径转为绝对 file:/// 路径，并补懒加载属性。

    检测 <img src="相对路径"> 中的 src，如果路径是相对路径且本地文件存在，
    则转换为 file:/// 绝对路径，确保 QWebEngineView 能正常加载。
    已存在的绝对路径（http/https/file/data/qrc）跳过路径解析，但仍会补
    loading/decoding 属性。
    """

    def _replacer(match):
        prefix = match.group(1)
        src = match.group(2)
        suffix = match.group(3)
        new_suffix = _append_img_load_attrs(suffix, src)

        # 跳过已经是绝对 URL 或 data URI 的 src（不改写路径，只补属性）
        if src.startswith(("http://", "https://", "file://", "data:", "qrc:/", "#", "blob:")):
            return f"{prefix}{src}{new_suffix}"

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
            return f"{prefix}{file_url}{new_suffix}"

        return f"{prefix}{src}{new_suffix}"

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


_katex_vendor_urls_cache: tuple[str, str] | None = None


def _get_katex_urls() -> tuple:
    """返回 (css_url, js_url)。本地优先成对返回；任一缺失时整体降级 CDN。

    css 与 js 必须同源：katex.min.css 用相对路径 url(fonts/...) 引字体，
    混用本地 css + CDN js（或反之）会导致字体路径错位。
    不并入 `_get_vendor_script_tags()`（骨架缓存会让每条消息都背上 vendor），
    由 JS 侧 `_katexEnsure` 首次遇到公式时动态加载，与 mermaid 同策略。
    """
    global _katex_vendor_urls_cache
    if _katex_vendor_urls_cache is not None:
        return _katex_vendor_urls_cache

    base_dirs = [_PROJECT_ROOT]
    if hasattr(sys, "_MEIPASS"):
        base_dirs.append(sys._MEIPASS)

    css_url = js_url = ""
    for base in base_dirs:
        css_c = os.path.join(base, "app/resources/web/vendor/katex/katex.min.css")
        js_c = os.path.join(base, "app/resources/web/vendor/katex/katex.min.js")
        if os.path.isfile(css_c) and os.path.isfile(js_c):
            css_url = QUrl.fromLocalFile(css_c).toString()
            js_url = QUrl.fromLocalFile(js_c).toString()
            break
    if not css_url:
        css_url = "https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.css"
        js_url = "https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.js"

    _katex_vendor_urls_cache = (css_url, js_url)
    return _katex_vendor_urls_cache


_echarts_vendor_urls_cache: tuple[str, str] | None = None


def _get_echarts_vendor_urls() -> tuple:
    """返回 (echarts_url, wordcloud_url)，本地优先、缺失时降级 CDN。

    与 mermaid / KaTeX 同策略：**不并入骨架**。原实现由 `_get_vendor_script_tags()`
    把 echarts.min.js(1MB) + wordcloud 写进骨架 HTML，而骨架被 `_skeleton_cache`
    缓存并在卡片间共享 → 每条消息都背上 1MB 的解析与内存开销，哪怕整篇没有一个
    图表。改为 JS 侧首次遇到 `.echarts-container` 时动态加载，见 `_echartsEnsure`。

    wordcloud 是 echarts 插件，必须在 echarts 本体之后加载（顺序强依赖）。
    """
    global _echarts_vendor_urls_cache
    if _echarts_vendor_urls_cache is not None:
        return _echarts_vendor_urls_cache

    base_dirs = [_PROJECT_ROOT]
    if hasattr(sys, "_MEIPASS"):
        base_dirs.append(sys._MEIPASS)

    echarts_url = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"
    wordcloud_url = "https://cdn.jsdelivr.net/npm/echarts-wordcloud@2/dist/echarts-wordcloud.min.js"

    for base in base_dirs:
        candidate = os.path.join(base, "app/resources/web/vendor/echarts.min.js")
        if os.path.isfile(candidate):
            echarts_url = QUrl.fromLocalFile(candidate).toString()
            break

    for base in base_dirs:
        candidate = os.path.join(base, "app/resources/web/vendor/echarts-wordcloud.min.js")
        if os.path.isfile(candidate):
            wordcloud_url = QUrl.fromLocalFile(candidate).toString()
            break

    _echarts_vendor_urls_cache = (echarts_url, wordcloud_url)
    return _echarts_vendor_urls_cache


def _mmd_theme_vars_js(body_font_size: int) -> str:
    """mermaid themeVariables 的 JS 对象字面量（按当前主题取色）。

    骨架构建与 `refresh_theme` 共用：mermaid 的 `initialize()` 原先只在首次懒加载
    时跑一次，主题切换后新渲染的图沿用建卡时的旧配色（浅色主题下白叠白）。
    现在主题变量挂 `window._MMD_THEME_VARS`，切主题时重新注入并调
    `window._mmdApplyTheme()` 重设。
    """
    from app.utils.design_tokens import Colors

    return (
        "{"
        f"primaryTextColor: '{Colors.TEXT_PRIMARY}', "
        f"lineColor: '{Colors.TEXT_SECONDARY}', "
        f"mainBkg: '{Colors.CONTENT_BG}', "
        f"nodeBorder: '{Colors.BORDER}', "
        "background: 'transparent', "
        f"fontSize: '{body_font_size}px'"
        "}"
    )


def _format_elapsed(elapsed: float) -> str:
    """自适应单位格式化耗时：<60s=秒；<1h=分秒；<1d=时分；>=1d=天时。

    阈值固定（60/3600/86400），与设置/语言无关。返回值不含前缀，前缀由调用方拼接。
    """
    total = int(elapsed)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m}m {s}s"
    if total < 86400:
        h, rem = divmod(total, 3600)
        m = rem // 60
        return f"{h}h {m}m"
    d, rem = divmod(total, 86400)
    h = rem // 3600
    return f"{d}d {h}h"


# ======== WebViewer ========
def _defer_emit(sender, fn):
    """延迟到事件循环下一拍执行 ``fn``，并绑定到 ``sender`` 的生命周期。

    用于 Chromium 回调栈（javaScriptConsoleMessage）内的动作类 emit：既要把
    emit 挪出回调栈（族⑤），又要保证延迟期间 sender 不会被「销毁后照旧回调」
    （族⑤-2）。

    ⚠️ 不能退化为 ``QTimer.singleShot(0, fn)``：延迟期间若 sender 被销毁
    （卡片卸载 / viewer 池回收 / 会话切换），回调照样执行 → 对已释放的 sender
    调 emit → ACCESS_VIOLATION（实机 2026-09-16 22:46:30 例，崩在
    ``Qt5Core!QObject::signalsBlocked`` +0x4 —— emit 第一步读
    ``sender->d_ptr->blockSig``，此时对象内存已被释放清零）。

    以 sender 为 parent 的一次性 QTimer 随 sender 析构而销毁，pending 的
    timeout 不再派发，从根上掐断「延迟窗口内 sender 死亡」这条路径。
    """
    from PyQt5.QtCore import QTimer

    timer = QTimer(sender)
    timer.setSingleShot(True)

    def _run():
        try:
            fn()
        finally:
            timer.deleteLater()

    timer.timeout.connect(_run)
    timer.start(0)
