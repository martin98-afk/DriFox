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

from PyQt5.QtCore import QFile, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap, QTextOption
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtWidgets import QSizePolicy

try:  # QSvgRenderer：思考流式 spinner / qrc 图标渲染依赖
    from PyQt5.QtSvg import QSvgRenderer

    _HAS_QT_SVG = True
except Exception:  # pragma: no cover
    _HAS_QT_SVG = False

import html as _html_mod

from app.utils.design_tokens import Animations, Colors, get_unified_scrollbar_style, scale_font_size
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

# ── 流式活动坞（Streaming Dock）常量（对齐 WebEngine 版 _STREAMING_DOCK_CSS）──
# 与 WebEngine 版 body.streaming-dock #tool-content 的 max-height 保持一致。
# 原值 110（≈3-4 行）视觉上与"折叠"难区分，用户反馈误以为工具区默认收起。
_DOCK_LOG_MAX = 220  # 工具卡区内滚限高 ≈8 行（web #tool-content max-height）
_DOCK_TODO_H = 96  # 任务列表坞态固定高（web #todo-content height:96px）

_QWIDGETSIZE_MAX = 16777215

try:  # qrc 编译资源注册（幂等）：main.py 仅注册深色 icons_rc，
    # 浅色主题 icons_light_rc 主程序链路从未 import → 必须在此补齐
    from app.utils import icons_light_rc as _icons_light_rc  # noqa: F401
    from app.utils import icons_rc as _icons_rc  # noqa: F401

    _HAS_QRC_ICONS = True
except Exception:
    _HAS_QRC_ICONS = False


# qrc svg → QPixmap 缓存：key=(prefix/icon_name, size)，含主题前缀故主题切换自动换图。
_ICON_PIXMAP_CACHE: Dict[Tuple[str, int], QPixmap] = {}
_ICON_PIXMAP_CACHE_MAX = 256


def _qrc_icon_pixmap(icon_name: str, size: int = 16) -> Optional[QPixmap]:
    """qrc svg 图标 → QPixmap（主题感知 prefix，QSvgRenderer 直渲）。

    不能用 QLabel 富文本 <img src="qrc:...">：该路径经 QImageReader/svg
    imageformat 插件 + style 缩放，含椭圆弧（A/a）的 SVG 小尺寸下渲染为空白
    （实测 思考过程/工具 全空白，无弧的 todo 正常）。QSvgRenderer 为 QtSvg 模块
    API，直渲矢量 path 不经插件，弧完整；渲染结果 setPixmap 到 QLabel。
    失败返回 None，调用方回退 emoji。
    """
    if not _HAS_QRC_ICONS or not _HAS_QT_SVG:
        return None
    try:
        from app.widgets.render_helpers import get_tool_qrc_prefix, _qrc_icon_exists

        prefix = get_tool_qrc_prefix()
        if not _qrc_icon_exists(prefix, icon_name):
            return None
        key = (f"{prefix}/{icon_name}", size)
        cached = _ICON_PIXMAP_CACHE.get(key)
        if cached is not None:
            return cached
        qrc_path = ":" + prefix[len("qrc:") :] if prefix.startswith("qrc:") else prefix
        f = QFile(f"{qrc_path}/{icon_name}.svg")
        if not f.open(QFile.ReadOnly):
            return None
        data = bytes(f.readAll())
        f.close()
        renderer = QSvgRenderer(data)
        if not renderer.isValid():
            return None
        dpr = 2.0
        try:
            _screen = QApplication.primaryScreen()
            if _screen is not None:
                dpr = _screen.devicePixelRatio() or 2.0
        except Exception:
            dpr = 2.0
        pm = QPixmap(int(size * dpr), int(size * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        if len(_ICON_PIXMAP_CACHE) >= _ICON_PIXMAP_CACHE_MAX:
            _ICON_PIXMAP_CACHE.clear()
        _ICON_PIXMAP_CACHE[key] = pm
        return pm
    except Exception:
        return None


def _measure_expanded_height(wrap: QWidget) -> int:
    """以当前实际宽度测量折叠容器展开后的真实高度。

    QLabel(wordWrap) 的 sizeHint 按"理想宽度"算行数，在窄布局里低估高度——
    直接拿 sizeHint 当动画终点会导致到位后放开限高的瞬间二次跳变。
    此函数对 hasHeightForWidth 的子项用 heightForWidth(可用宽) 精确测量。
    """
    lay = wrap.layout()
    if lay is None:
        return wrap.sizeHint().height()
    m = lay.contentsMargins()
    inner_w = wrap.width() - m.left() - m.right()
    if inner_w <= 40:  # 未布局/宽度未知 → 退回 sizeHint
        return max(wrap.sizeHint().height(), 24)
    total = m.top() + m.bottom()
    n = 0
    for i in range(lay.count()):
        it = lay.itemAt(i)
        wdg = it.widget() if it is not None else None
        if wdg is None or wdg.isHidden():
            continue
        if n:
            total += lay.spacing()
        h = wdg.heightForWidth(inner_w) if wdg.hasHeightForWidth() else 0
        if h <= 0:
            h = wdg.sizeHint().height()
        total += max(h, wdg.minimumSizeHint().height())
        n += 1
    return int(total)


# ── 思考 spinner：SVG 预渲染缓存 ──────────────────────────────────
# [PERF] 原实现在 paintEvent 里 `QSvgRenderer(svg.encode())` —— 每帧都重新构造
# 渲染器并重新解析一遍 SVG XML（含 4 个 circle + dasharray），40ms 定时器即 25fps，
# 每个可见思考块每秒 25 次解析，多思考块线性放大。
# 改为：模块级单例渲染器 + 按 devicePixelRatio 缓存位图，每帧只剩 drawPixmap。
# 旋转仍交给 painter 变换（省内存，无需缓存 30 个角度）。
_SPINNER_RENDERER: Any = None  # None=未初始化 / False=不可用 / QSvgRenderer=就绪
_SPINNER_PIXMAP_CACHE: Dict[float, QPixmap] = {}
_SPINNER_PIXMAP_CACHE_MAX = 8  # DPR 取值极少（1.0/1.25/1.5/2.0…），封顶防异常环境膨胀


def _get_spinner_renderer() -> Any:
    """惰性构造全局唯一的 QSvgRenderer（解析一次，终身复用）。"""
    global _SPINNER_RENDERER
    if _SPINNER_RENDERER is None:
        if not _HAS_QT_SVG:
            _SPINNER_RENDERER = False
        else:
            try:
                from app.widgets.message_card import _THINK_SNAKE_SVG

                r = QSvgRenderer(_THINK_SNAKE_SVG.encode("utf-8"))
                _SPINNER_RENDERER = r if r.isValid() else False
            except Exception:
                _SPINNER_RENDERER = False
    return _SPINNER_RENDERER or None


def _get_spinner_pixmap(dpr: float) -> Optional[QPixmap]:
    """按 devicePixelRatio 返回预渲染好的 spinner 位图，失败返回 None。"""
    renderer = _get_spinner_renderer()
    if renderer is None:
        return None
    cached = _SPINNER_PIXMAP_CACHE.get(dpr)
    if cached is not None:
        return cached
    try:
        sz = renderer.defaultSize()
        w = int(sz.width()) if sz.width() > 0 else 18
        h = int(sz.height()) if sz.height() > 0 else 18
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()
    except Exception:
        return None
    if len(_SPINNER_PIXMAP_CACHE) >= _SPINNER_PIXMAP_CACHE_MAX:
        _SPINNER_PIXMAP_CACHE.clear()
    _SPINNER_PIXMAP_CACHE[dpr] = pm
    return pm


class _ThinkingSpinner(QWidget):
    """思考流式 spinner：金色 snake 圆环旋转（复刻 WebEngine 版 _THINK_SNAKE_SVG 观感）。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._angle = 0
        self.setFixedSize(18, 18)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        drawn = False
        if _HAS_QT_SVG:
            try:
                pm = _get_spinner_pixmap(self.devicePixelRatioF())
                if pm is not None:
                    p.translate(self.width() / 2, self.height() / 2)
                    p.rotate(self._angle)
                    p.translate(-self.width() / 2, -self.height() / 2)
                    p.drawPixmap(0, 0, pm)
                    drawn = True
            except Exception:
                drawn = False
        if not drawn:
            # 兜底：无 SVG / 渲染失败时画简单圆弧
            rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
            p.setPen(QPen(QColor(255, 200, 50), 2.5, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, self._angle * 16, 100 * 16)
        p.end()


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
    """markdown 全文 → 块列表。type ∈ think/tool/code/html/tag。"""
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
                    blocks.extend(_split_plugin_tags(cbody))
    return blocks


def _split_plugin_tags(text: str) -> List[Dict[str, Any]]:
    """文本段按插件注册的内联标签切分（如 assistant_hub 的 <mood>）。

    无注册标签时零开销直通（单个 html 块）；命中时已注册标签段转
    {type:"tag", tag, content, completed, html} 块，剩余文本保持
    html 块不变。渲染失败/空内容的 tag 块丢弃（不原文泄漏）。
    """
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        tag_names = UIPluginRegistry.get_instance().get_registered_tag_names()
    except Exception:
        tag_names = []
    if not tag_names:
        return [{"type": "html", "html": _md_to_html(text)}]

    # 逐标签切分：tag 段标记为 f"tag:{name}"，普通段继续下一层
    queue: List[Tuple[str, str, bool]] = [("plain", text, True)]
    for tag in tag_names:
        open_tag, close_tag = f"<{tag}>", f"</{tag}>"
        if not any(kind == "plain" and open_tag in body for kind, body, _c in queue):
            continue
        nxt: List[Tuple[str, str, bool]] = []
        for kind, body, closed in queue:
            if kind != "plain":
                nxt.append((kind, body, closed))
                continue
            for skind, sbody, sclosed in _split_tag_segments(body, open_tag, close_tag):
                if skind == "tag":
                    nxt.append((f"tag:{tag}", sbody, sclosed))
                else:
                    nxt.append(("plain", sbody, True))
        queue = nxt

    out: List[Dict[str, Any]] = []
    for kind, body, closed in queue:
        if kind.startswith("tag:"):
            tag = kind[4:]
            html = _render_plugin_tag_html(tag, body, closed)
            if html:
                out.append({"type": "tag", "tag": tag, "content": body, "completed": closed, "html": html})
        elif body.strip():
            out.append({"type": "html", "html": _md_to_html(body)})
    return out


def _render_plugin_tag_html(tag: str, content: str, completed: bool) -> str:
    """已注册内联标签 → 插件渲染器 HTML（Qt 路径）。失败/空内容返回空串（丢弃，不泄漏原文）。"""
    if not content.strip():
        return ""
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        info = UIPluginRegistry.get_instance().get_tag_renderer(tag)
        if info is None:
            return ""
        return info.render_func(content, {"tag": tag, "completed": completed, "compact": False})
    except Exception:
        return ""


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

    def update_content(self, b: Dict[str, Any]) -> None:
        """流式原地更新（reconcile slot 复用约定）。"""
        self.set_html(b.get("html") or "")


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

    def update_content(self, b: Dict[str, Any]) -> None:
        """流式原地更新（不重建控件）。签名统一接收 block dict。"""
        code = b.get("code") or ""
        self._raw_code = code
        self._lang = b.get("lang") or ""
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
        self._content = content or ""
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
        hl.setSpacing(6)
        # 思考过程 qrc svg 图标（QSvgRenderer 直渲，富文本 img 对含弧 SVG 空白），流式态切为 spinner
        self._icon_label = QLabel(header)
        icon_pm = _qrc_icon_pixmap("思考过程", 16)
        self._icon_is_img = icon_pm is not None
        if icon_pm is not None:
            self._icon_label.setPixmap(icon_pm)
        else:
            self._icon_label.setText("💡")
        self._icon_label.setFixedSize(18, 18)
        self._icon_label.setAlignment(Qt.AlignCenter)
        self._spinner = _ThinkingSpinner(header)
        self._spinner.hide()
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
        for w in (self._icon_label, self._spinner, self._title_label, self._preview_label):
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
        if completed:
            self._refresh_title()

    def set_streaming(self) -> None:
        """流式态：单行提示，无折叠。"""
        self.apply_streaming(True)

    def apply_streaming(self, active: bool) -> None:
        """校正流式态标记（reconcile 复用控件时内容指纹不变但状态可能翻转）。"""
        if active:
            self._title_label.setText("深度思考中...")
            self._preview_label.setText("")
            self._chevron.hide()
            self._icon_label.hide()
            self._spinner.start()
            self._spinner.show()
        else:
            self._spinner.stop()
            self._spinner.hide()
            self._icon_label.show()
            self._chevron.show()
            self._refresh_title()

    def _refresh_title(self) -> None:
        """完成态标题/预览：复刻 WebEngine 版分类标签(_classify_think_tag)+结论优先预览。"""
        try:
            from app.widgets.message_card import _classify_think_tag, _get_think_preview

            tag = _classify_think_tag(self._content) if self._content else ""
            self._title_label.setText(tag or "深度思考")
            self._preview_label.setText(_get_think_preview(self._content) if self._content else "")
        except Exception:
            self._title_label.setText("深度思考")

    def update_content(self, b: Dict[str, Any]) -> None:
        """流式原位更新（不重建控件）：正文文本 + 完成态标题/预览。"""
        self._content = b.get("content") or ""
        plain = self._content.replace("```", "").strip()
        self._body_label.setText(plain)
        if b.get("completed", True):
            self.apply_streaming(False)

    def toggle(self) -> None:
        # 收起从"当前实际渲染高度"起步：展开完成时限高已放开(16777215)，
        # 直接拿 maximumHeight 当起点会导致大半动画时长无视觉变化、末尾骤缩。
        if not self._expanded:
            target = max(_measure_expanded_height(self._body_wrap), 1)
            start = 0
            self._expanded = True
        else:
            start = max(self._body_wrap.height(), 1)
            self._body_wrap.setMaximumHeight(start)  # 先钳到实际高度再动画
            target = 0
            self._expanded = False
        self._chevron.setText(self.CHEVRON_DOWN if self._expanded else self.CHEVRON_RIGHT)
        if not Animations.motion_enabled():
            # reduced-motion：跳过补间，直接落终值（收尾回调幂等）
            self._body_wrap.setMaximumHeight(_QWIDGETSIZE_MAX if self._expanded else 0)
            self._on_anim_done()
            return
        if self._anim is not None:
            self._anim.stop()
        self._anim = QPropertyAnimation(self._body_wrap, b"maximumHeight", self)
        self._anim.setDuration(Animations.EXPAND_MS)
        self._anim.setEasingCurve(QEasingCurve(Animations.EASE_OUT))
        self._anim.setStartValue(start)
        self._anim.setEndValue(target)
        self._anim.finished.connect(self._on_anim_done)
        self._anim.start()

    def _on_anim_done(self) -> None:
        if self._expanded:
            # 展开终点经 heightForWidth 精确测量，放开限高不会二次跳变
            self._body_wrap.setMaximumHeight(_QWIDGETSIZE_MAX)


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
        self.update_diff(diff_text)

    def update_diff(self, diff_text: str) -> None:
        """原位刷新 diff 内容（流式增量注入复用，不重建控件）。"""
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


def _tool_icon_pixmap(tool_name: str, args_raw: str, size: int = 16) -> Optional[QPixmap]:
    """工具自定义图标 pixmap（对齐 WebEngine 版 _get_tool_icon_name + qrc 机制）。

    返回 QPixmap；查不到时返回 None（调用方回退 ⚙ emoji）。
    """
    if not _HAS_QRC_ICONS or not _HAS_QT_SVG:
        return None
    try:
        from app.tools.registry import DEFAULT_FALLBACK_ICON
        from app.widgets.render_helpers import _get_tool_icon_name, _qrc_icon_exists, get_tool_qrc_prefix

        args: Dict[str, Any] = {}
        try:
            parsed = json.loads(args_raw or "{}")
            if isinstance(parsed, dict):
                args = parsed
        except Exception:
            pass
        icon_name = _get_tool_icon_name(tool_name, args)
        prefix = get_tool_qrc_prefix()
        if not _qrc_icon_exists(prefix, icon_name):
            icon_name = DEFAULT_FALLBACK_ICON
        return _qrc_icon_pixmap(icon_name, size)
    except Exception:
        return None


def _format_args_preview_safe(args_raw: str, max_len: int = 80) -> str:
    """参数单行预览 `k=v; k2=v2`（复用 render_helpers._format_args_preview）。"""
    try:
        from app.widgets.render_helpers import _format_args_preview

        args = json.loads(args_raw or "{}")
        if not isinstance(args, dict):
            return ""
        return _format_args_preview(args, max_len)
    except Exception:
        return ""


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
        # 工具自定义图标（registry/qrc 驱动，QSvgRenderer 直渲，对齐 WebEngine 版），失败回退 ⚙
        gear = QLabel(header)
        self._icon_pm = _tool_icon_pixmap(block.get("name") or "", block.get("args_raw") or "")
        if self._icon_pm is not None:
            gear.setPixmap(self._icon_pm)
        else:
            gear.setText("⚙")
        self._icon_is_pixmap = self._icon_pm is not None  # 兼容 update_content 判断（语义：img 可用）
        gear.setFixedSize(18, 18)
        gear.setAlignment(Qt.AlignCenter)
        self._gear_label = gear
        self._name_label = QLabel(block.get("cn_name") or block.get("name") or "工具调用", header)
        # 标题色对齐 WebEngine 版 title_color=#FFA500
        self._name_label.setStyleSheet(
            f"{get_font_family_css()} color:{_LANG_COLOR};"
            f" font-size:{_CODE_FONT_SIZE}px; font-weight:600; background:transparent;"
        )
        # 参数单行预览（对齐 WebEngine 版 _format_args_preview：k=v; k2=v2 截断）
        self._preview_label = QLabel(_format_args_preview_safe(block.get("args_raw") or ""), header)
        self._preview_label.setStyleSheet(
            f"{get_font_family_css()} color:{Colors.ASSISTANT_CARD_MUTED};"
            f" font-size:{scale_font_size(11)}px; background:transparent;"
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
        for w in (gear, self._name_label, self._preview_label, self._status_label):
            hl.addWidget(w)
        self._update_diff_stats()
        hl.addStretch()
        # diff 统计靠右（stretch 之后），避免与长工具名挤压重叠
        hl.addWidget(self._diff_stats_label)
        hl.addWidget(self._chevron)
        header.clicked.connect(self.toggle)
        root.addWidget(header)

        # 展开区：diff 优先 → 参数+结果表格 → args JSON → 纯 result 文本（对齐 WebEngine 版分支）
        self._body_wrap = QFrame(self)
        self._body_wrap.setMaximumHeight(0)
        self._body_lay = QVBoxLayout(self._body_wrap)
        self._body_lay.setContentsMargins(20, 4, 8, 6)
        self._body_lay.setSpacing(6)
        self._build_body(block)
        root.addWidget(self._body_wrap)

        self._apply_status()

    def _build_body(self, block: Dict[str, Any]) -> None:
        """构建展开区内容（__init__ 与 update_content 共用）。"""
        bw = self._body_lay
        while bw.count():
            item = bw.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        self._body_kind = ""
        self._body_label_ref: Optional[QLabel] = None
        muted = Colors.ASSISTANT_CARD_MUTED
        diff_text = block.get("diff") or ""
        result_text = (block.get("result") or "").strip()
        table_html = "" if diff_text else _unified_table_html(block)
        if diff_text:
            diff_view = DiffViewWidget(diff_text, self._body_wrap)
            bw.addWidget(diff_view)
            self._body_kind, self._body_label_ref = "diff", diff_view
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
            self._body_kind, self._body_label_ref = "table", table_label
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
                self._body_kind, self._body_label_ref = "args", args_label
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
            if self._body_kind == "":
                self._body_kind, self._body_label_ref = "result", result_label

    def update_content(self, b: Dict[str, Any]) -> None:
        """流式原位更新：头部预览/徽章/diff 统计 + body 分支原位或重建（不重建卡片，
        保留用户展开态与动画）。"""
        old = self._block
        self._block = b
        self._preview_label.setText(_format_args_preview_safe(b.get("args_raw") or ""))
        new_icon_pm = _tool_icon_pixmap(b.get("name") or "", b.get("args_raw") or "")
        if self._icon_is_pixmap and new_icon_pm is not None:
            self._gear_label.setPixmap(new_icon_pm)
        self._update_diff_stats()
        new_kind = "diff" if (b.get("diff") or "") else ""
        if not new_kind:
            has_result = bool((b.get("result") or "").strip())
            if _unified_table_html(b):
                new_kind = "table"
            elif (b.get("args_raw") or "") and not has_result:
                new_kind = "args"
            elif has_result:
                new_kind = "result"
            else:
                new_kind = "args"
        if old is b or new_kind == getattr(self, "_body_kind", ""):
            # 同分支：原位刷新文本
            ref = self._body_label_ref
            if ref is None:
                pass
            elif new_kind == "diff":
                ref.update_diff(b.get("diff") or "")
            elif new_kind == "table":
                ref.setText(_unified_table_html(b))
            elif new_kind == "result":
                ref.setText(_html_mod.escape((b.get("result") or "").strip()))
            else:
                ref.setText(self._pretty_args(b.get("args_raw") or ""))
        else:
            # 分支迁移（如 args → table/result）：重建 body 子树
            self._build_body(b)
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
        # 收起从"当前实际渲染高度"起步（ThinkCard 同策略），避免从 16777215 起步
        if not self._expanded:
            target = max(_measure_expanded_height(self._body_wrap), 1)
            start = 0
            self._expanded = True
        else:
            start = max(self._body_wrap.height(), 1)
            self._body_wrap.setMaximumHeight(start)
            target = 0
            self._expanded = False
        self._chevron.setText(self.CHEVRON_DOWN if self._expanded else self.CHEVRON_RIGHT)
        if not Animations.motion_enabled():
            # reduced-motion：跳过补间，直接落终值（收尾回调幂等）
            self._body_wrap.setMaximumHeight(_QWIDGETSIZE_MAX if self._expanded else 0)
            self._on_anim_done()
            return
        if self._anim is not None:
            self._anim.stop()
        self._anim = QPropertyAnimation(self._body_wrap, b"maximumHeight", self)
        self._anim.setDuration(Animations.EXPAND_MS)
        self._anim.setEasingCurve(QEasingCurve(Animations.EASE_OUT))
        self._anim.setStartValue(start)
        self._anim.setEndValue(target)
        self._anim.finished.connect(self._on_anim_done)
        self._anim.start()

    def _on_anim_done(self) -> None:
        if self._expanded:
            self._body_wrap.setMaximumHeight(_QWIDGETSIZE_MAX)


# ── 「工具与思考」分区 + 任务列表面板 ─────────────────────────────
class _SeparatorRow(QFrame):
    """可点击折叠的分隔条（对照 WebEngine 版 tool-separator/todo-separator：
    两侧细线 + 图标 + 标题 + 附加信息 + chevron）。"""

    clicked = pyqtSignal()

    def __init__(self, text: str, parent: Optional[QWidget] = None, icon_name: Optional[str] = None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(6)

        def _line() -> QFrame:
            ln = QFrame(self)
            ln.setFrameShape(QFrame.HLine)
            ln.setStyleSheet(f"background:{Colors.ASSISTANT_CARD_MUTED}; border:none; max-height:1px;")
            ln.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            ln.setFixedHeight(1)
            ln.setMaximumHeight(1)
            return ln

        self._chevron = QLabel("▾", self)
        label = QLabel(text, self)
        label.setStyleSheet(
            f"{get_font_family_css()} color:{Colors.ASSISTANT_CARD_MUTED};"
            f" font-size:{scale_font_size(12)}px; font-weight:600; background:transparent;"
        )
        self._icon_label = QLabel(self)
        icon_pm = _qrc_icon_pixmap(icon_name, 14) if icon_name else None
        if icon_pm is not None:
            self._icon_label.setPixmap(icon_pm)
        else:
            self._icon_label.setText("•" if icon_name else "")
        self._icon_label.setFixedSize(16, 16)
        self._icon_label.setAlignment(Qt.AlignCenter)
        self._icon_label.setStyleSheet("background:transparent;")
        self._extra = QLabel("", self)
        self._extra.setStyleSheet(
            f"{get_font_family_css()} color:{Colors.ASSISTANT_CARD_MUTED};"
            f" font-size:{scale_font_size(11)}px; background:transparent;"
        )
        lay.addWidget(_line(), 1)  # 左侧细线（web ::before flex:1）
        for w in (self._chevron, self._icon_label, label, self._extra):
            lay.addWidget(w)
        lay.addWidget(_line(), 1)  # 右侧细线（web ::after flex:1）
        self.setStyleSheet(
            "_SeparatorRow { background:rgba(127,127,127,0.04); border-radius:6px; padding:1px 4px; }"
            "_SeparatorRow:hover { background:rgba(127,127,127,0.10); }"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)

    def set_collapsed(self, collapsed: bool) -> None:
        self._chevron.setText("▸" if collapsed else "▾")

    def set_extra(self, text: str) -> None:
        self._extra.setText(text)


def _slot_items(blocks: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    """块列表 → [(slot, block)]。slot = 类型#同类序号：跨 reconcile 稳定的位置标识。"""
    counters: Dict[str, int] = {}
    out: List[Tuple[str, Dict[str, Any]]] = []
    for b in blocks:
        t = b["type"]
        seq = counters.get(t, 0)
        counters[t] = seq + 1
        out.append((f"{t}#{seq}", b))
    return out


def reconcile_widgets(
    layout: QVBoxLayout,
    widgets: List[QWidget],
    keys: List[Tuple[str, str]],
    items: List[Tuple[str, Dict[str, Any]]],
    builder,
    streaming: bool,
) -> Tuple[List[QWidget], List[Tuple[str, str]]]:
    """差量对齐（slot 双轨版）。返回新的 (widgets, keys)。

    keys 元素 = (slot, 内容指纹)：
    - slot（类型+序号）相同 → 复用控件；指纹变了走 widget.update_content 原位更新，
      不重建——流式期间 think/tool 每 chunk 变内容，旧版按纯指纹比较会整卡重建，
      导致展开态复位、折叠动画中断、视觉闪烁。
    - slot 序列分歧 → 只重建分歧点之后的控件。
    """
    new_pairs = [(slot, _block_key(b), b) for slot, b in items]
    common = 0
    while common < len(new_pairs) and common < len(keys):
        old_slot, old_fp = keys[common]
        new_slot, new_fp, b_new = new_pairs[common]
        if old_slot != new_slot:
            break  # 结构分歧：从此处开始重建
        if old_fp != new_fp:
            w = widgets[common] if common < len(widgets) else None
            upd = getattr(w, "update_content", None)
            if upd is None:
                break  # 无原位更新能力：退回重建路径
            try:
                upd(b_new)
            except Exception:
                break
        common += 1
    while len(widgets) > common:
        w = widgets.pop()
        layout.removeWidget(w)
        w.deleteLater()
    del keys[common:]
    for slot, fp, b in new_pairs[common:]:
        w = builder(b)
        widgets.append(w)
        keys.append((slot, fp))
        layout.addWidget(w)
    # 统一校正流式态标记：复用控件的内容指纹不变但闭合状态可能已翻转
    # （think/tool 在流式期间闭合且内容不再变化时，需原地恢复正常态）
    if streaming:
        for i, (_, _, b) in enumerate(new_pairs[: len(widgets)]):
            w = widgets[i]
            if isinstance(w, ThinkCard):
                w.apply_streaming(not b.get("completed", True))
            elif isinstance(w, ToolCardWidget):
                w.apply_streaming(not b.get("closed", True))
    return widgets, keys


class ToolSectionWidget(QWidget):
    """「工具与思考」折叠分区：think/tool 卡统一收纳，分隔条可整体折叠。

    卡片区常驻 QScrollArea：非坞态无限高（不出滚动条），坞态限高 _DOCK_LOG_MAX
    内滚并自动跟随最新条目——复刻 WebEngine 版 #tool-content max-height:110px。
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        # 默认展开（与 WebEngine 版对齐：流式结束后工具区保持展开）
        # 旧值为 True —— 加载即收起，用户每次都要手动点开才能看到工具/思考内容，
        # 与研究 AI 执行过程的场景相悖。改为展开，需要收起时点分隔条即可。
        self._collapsed = False
        self._anim: Optional[QPropertyAnimation] = None
        self._keys: List[Tuple[str, str]] = []
        self._widgets: List[QWidget] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(2)
        self._separator = _SeparatorRow("工具与思考", self, icon_name="工具")
        self._separator.clicked.connect(self.toggle)
        self._separator.set_collapsed(False)  # 默认展开：箭头 ▾，与 _collapsed=False 一致
        root.addWidget(self._separator)

        self._content_wrap = QFrame(self)
        # 默认展开：不限高（0 会让内容区完全不可见）
        self._content_wrap.setMaximumHeight(_QWIDGETSIZE_MAX)
        cv = QVBoxLayout(self._content_wrap)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(2)
        # ── 卡片内滚容器：坞态限高的载体 ──
        self._cards_scroll = QScrollArea(self)
        self._cards_scroll.setWidgetResizable(True)
        self._cards_scroll.setFocusPolicy(Qt.NoFocus)
        self._cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._cards_scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }\n" + get_unified_scrollbar_style(6)
        )
        self._cards_host = QWidget()
        self._cards_host.setStyleSheet("background:transparent;")
        self._cards_scroll.setWidget(self._cards_host)
        cv.addWidget(self._cards_scroll)
        # cards_layout 挂到 scroll 内部 host 上
        ch = QVBoxLayout(self._cards_host)
        ch.setContentsMargins(0, 0, 4, 0)
        ch.setSpacing(2)
        self._cards_layout = QVBoxLayout()
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(2)
        ch.addLayout(self._cards_layout)
        ch.addStretch(1)
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
        total = len(items)
        self._separator.set_extra(f"· {total} 项" if total else "")
        if not self._collapsed and not self._content_wrap.isVisible():
            self._apply_expand_state()
        self.setVisible(bool(items))
        # 坞态实时日志：新条目到达自动滚到底（web 版 _scrollToolContentToBottom 语义）
        if streaming:
            QTimer.singleShot(0, self._scroll_cards_to_bottom)

    def _scroll_cards_to_bottom(self) -> None:
        sb = self._cards_scroll.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    @staticmethod
    def _build(b: Dict[str, Any]) -> QWidget:
        if b["type"] == "think":
            return ThinkCard(b["content"], b["completed"])
        return ToolCardWidget(b)

    def attach_todo_panel(self, panel: QWidget) -> None:
        self._panel_holder.addWidget(panel)

    # ── 坞态接口（对齐 WebEngine 版 body.streaming-dock） ──
    def enter_dock(self) -> None:
        """进入坞态：直接展开（无动画）+ 卡片区限高内滚。"""
        self._cards_scroll.setMaximumHeight(_DOCK_LOG_MAX)
        if self._collapsed:
            self._set_collapsed_instant(False)

    def exit_dock(self) -> None:
        """退出坞态：解除限高并折叠（对齐 WebEngine 版简洁模式行为）。

        简洁模式语义：流式结束后工具与思考区收起为「工具与思考 · N 项」
        标题栏（WebEngine 版 MessageCard._auto_collapse_tool_section 同行为）。
        本渲染器 _tool_compact_mode 恒为 True（坞态仅简洁模式启用），
        归位即折叠；流式期间由 enter_dock 保持展开可见。
        """
        self._cards_scroll.setMaximumHeight(_QWIDGETSIZE_MAX)
        if not self._collapsed:
            self._set_collapsed_instant(True)

    def _set_collapsed_instant(self, collapsed: bool) -> None:
        """无动画切换折叠态（坞态进出用，避免与 dock 布局搬移叠加抖动）。"""
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        self._collapsed = collapsed
        self._separator.set_collapsed(collapsed)
        self._content_wrap.setMaximumHeight(0 if collapsed else _QWIDGETSIZE_MAX)
        self._apply_expand_state()

    def _apply_expand_state(self) -> None:
        """按折叠状态显隐内容区（收起时隐藏，展开时显示）。"""
        self._content_wrap.setVisible(not self._collapsed)

    def toggle(self) -> None:
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        if not self._collapsed:
            start = max(self._content_wrap.height(), 1)
            self._content_wrap.setMaximumHeight(start)
            target = 0
            self._collapsed = True
        else:
            self._content_wrap.setVisible(True)
            target = max(_measure_expanded_height(self._content_wrap), 1)
            start = 0
            self._collapsed = False
        self._separator.set_collapsed(self._collapsed)
        if not Animations.motion_enabled():
            # reduced-motion：跳过补间，直接落终值（收尾回调幂等）
            self._content_wrap.setMaximumHeight(0 if self._collapsed else _QWIDGETSIZE_MAX)
            self._on_done()
            return
        self._anim = QPropertyAnimation(self._content_wrap, b"maximumHeight", self)
        self._anim.setDuration(Animations.EXPAND_MS)
        self._anim.setEasingCurve(QEasingCurve(Animations.EASE_OUT))
        self._anim.setStartValue(start)
        self._anim.setEndValue(target)
        self._anim.finished.connect(self._on_done)
        self._anim.start()

    def _on_done(self) -> None:
        if not self._collapsed:
            self._content_wrap.setMaximumHeight(_QWIDGETSIZE_MAX)
        else:
            self._content_wrap.setVisible(False)


class TodoPanel(QWidget):
    """任务列表面板：分隔条(todo.svg 图标) + 进度 + 状态条目（对齐 WebEngine 版 .todo-item 视觉）。

    列表区常驻 QScrollArea：坞态固定高 _DOCK_TODO_H 内滚（web #todo-content
    height:96px 语义，切断工具区流式抖动向 todo 传导），非坞态无限高。
    """

    _PRI_COLORS = {"high": "#ef4444", "medium": "#f59e0b", "low": "#3b82f6"}

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(2)
        self._separator = _SeparatorRow("任务列表", self, icon_name="todo")
        self._separator.clicked.connect(self._toggle)
        root.addWidget(self._separator)
        self._list_scroll = QScrollArea(self)
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setFocusPolicy(Qt.NoFocus)
        self._list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._list_scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }\n" + get_unified_scrollbar_style(6)
        )
        self._list_wrap = QWidget()
        self._list_wrap.setStyleSheet("background:transparent;")
        self._list_scroll.setWidget(self._list_wrap)
        lw = QVBoxLayout(self._list_wrap)
        lw.setContentsMargins(6, 2, 6, 2)
        lw.setSpacing(1)
        self._list_layout = QVBoxLayout()
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(1)
        lw.addLayout(self._list_layout)
        lw.addStretch(1)
        root.addWidget(self._list_scroll)
        self._collapsed = False
        self.hide()

    def set_dock(self, active: bool) -> None:
        """坞态任务列表固定高（对齐 web #todo-content height:96px）。"""
        if active:
            self._list_scroll.setFixedHeight(_DOCK_TODO_H)
            self._list_scroll.setMaximumHeight(_DOCK_TODO_H)
        else:
            self._list_scroll.setMinimumHeight(0)
            self._list_scroll.setMaximumHeight(_QWIDGETSIZE_MAX)

    def update_todos(self, todos: List[Dict[str, Any]]) -> None:
        todos = list(todos or [])
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        done = 0
        for t in todos:
            status = (t.get("status") or "pending") if isinstance(t, dict) else "pending"
            raw = (t.get("content") if isinstance(t, dict) else t) or ""
            # 兜存量脏数据：content 非 str 时 dict/list 转 JSON 文本（html.escape 只收 str）
            content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
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
        self._list_scroll.setVisible(not self._collapsed)


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
        self._keys: List[Tuple[str, str]] = []
        self._widgets: List[QWidget] = []
        # ── 坞态（Streaming Dock）状态 ──
        self._dock_active = False
        # ── CodeWebViewer 兼容属性（MessageCard 动态读写） ──
        self._is_js_ready = True  # _push_todo_list 判断：Qt 渲染器始终"就绪"
        self._tool_compact_mode = True  # 简洁模式：工具卡默认折叠（坞态仅此模式启用）
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
        # 正文块统一挂独立容器：坞态搬移只动 tool_section，body 追加顺序不受影响
        self._body_box = QWidget()
        self._body_box.setStyleSheet("background:transparent;")
        self._body_lay = QVBoxLayout(self._body_box)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(2)
        self._vbox.addWidget(self._tool_section)
        self._vbox.addWidget(self._body_box)

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
        """流式结束。keep_dock=True 时保留坞态（S1：文本先于工具结果结束），
        等最后工具完成由 MessageCard 回调 _sync_streaming_dock(False) 归位。"""
        self._streaming = False
        if not keep_dock and self._dock_active:
            self._sync_streaming_dock(False)
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
        """流式活动坞（对齐 WebEngine 版 _setStreamingDock）：

        简洁模式 + 流式期间：工具/思考区从顶部沉底、卡片区限高内滚、任务列表
        固定高 → 卡片底部一块固定高度的实时日志；流式结束归位顶部并折叠。
        纯 layout 搬移（同 widget remove+add），无控件重建，无闪烁。
        正文高度交给外层聊天滚动链路（流式自动滚底跟随，视口底部即日志区），
        不做 viewer 级限高——QScrollArea 尺寸协商会撑爆卡片高度（实测教训）。
        """
        # 仅简洁模式启用坞态；欢迎卡片（light 骨架语义）不进坞态
        on = bool(active) and bool(self._tool_compact_mode) and not self._light
        if on == self._dock_active:
            return
        self._dock_active = on
        self._vbox.removeWidget(self._tool_section)
        if on:
            self._vbox.addWidget(self._tool_section)  # 沉底
            self._tool_section.enter_dock()
            self._todo_panel.set_dock(True)
        else:
            self._vbox.insertWidget(0, self._tool_section)  # 归位顶部
            self._tool_section.exit_dock()
            self._todo_panel.set_dock(False)
        QTimer.singleShot(0, lambda: self.contentHeightChanged.emit(self.height()))

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
        side_items = _slot_items(
            [b for b in blocks if b["type"] == "think" or (b["type"] == "tool" and not b.get("is_edit"))]
        )
        body_items = _slot_items(
            [b for b in blocks if b["type"] in ("html", "code", "tag") or (b["type"] == "tool" and b.get("is_edit"))]
        )
        self._widgets, self._keys = reconcile_widgets(
            self._body_lay, self._widgets, self._keys, body_items, self._build_block_widget, self._streaming
        )
        self._tool_section.reconcile(side_items, self._streaming)

    @staticmethod
    def _build_block_widget(b: Dict[str, Any]) -> QWidget:
        if b["type"] == "code":
            return CodeBlockWidget(b["code"], b["lang"])
        if b["type"] == "tool":
            return ToolCardWidget(b)  # 编辑类工具：结果展示在正文之中
        if b["type"] == "tag":
            return RichTextLabel(b["html"])  # 插件内联标签卡（已由渲染器生成 HTML）
        return RichTextLabel(b["html"])
