# -*- coding: utf-8 -*-
"""agent_trace 数据模型与主题色板。

定义轨迹查看器用到的核心数据结构：
- ``EntryKind``：条目类型枚举（SYSTEM/USER/CONTEXT/ASSISTANT/TOOL）
- ``Lane``：时间线泳道（Input / Model / Tools，对齐 DeepSeek Harness）
- ``TraceRecord``：单条轨迹记录
- ``ThemePalette``：**统一主题色板**（见下方 ⚠️ P022）

⚠️ 坑点 P022：``QColor("rgba(r,g,b,a)")`` **解析失败并静默返回黑色**。
而 DriFox 的主题 YAML 里 ``text_secondary`` / ``card_bg`` / ``hover_bg`` 等
**大量使用 rgba() 字符串**：

    text_secondary: rgba(226, 235, 249, 0.72)
    card_bg:        rgba(22, 30, 45, 230)

凡是把主题色直接喂给 QPainter / QColor 的自绘代码都会画出**纯黑**（历史 bug：
深色主题下时间线泳道标签 "Input / Model / Tools" 与刻度文字是黑字）。
统一入口 :func:`to_qcolor` + :class:`ThemePalette` 是本插件唯一的取色方式，
禁止再写 ``QColor(colors.get("text_secondary"))``。

⚠️ 坑点 P024：alpha 参数语义。主题里 ``card_bg: rgba(22,30,45,230)`` 的
0-255 与 ``hover_bg: rgba(255,255,255,0.08)`` 的 0.0-1.0 **混用**；
:func:`to_qcolor` 按字段是否含小数点自动判定。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from PyQt5.QtGui import QColor

_RGBA_RE = re.compile(r"rgba?\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*(?:,\s*([0-9.]+)\s*)?\)", re.I)

# ── 「占用区间」参数（让时间线连贯，而不是一堆 0ms 碎片）──
# 消息写入是瞬时事件（end=0），若直接用 end 画条带，整条时间线会碎成一排
# 最小宽度的点 —— 视觉上就是「全部 0ms、不连贯」。
# 改为把每条的占用延伸到「下一条的起点」，即 DeepSeek Harness / DevTools
# Waterfall 的语义：这一项占用这段时间。
# ⚠️ 同秒注入的多条消息间隔为 0（消息时间戳只有秒级精度）→ 间隔 0 就是瞬时，
# **不要**保底成某个最小宽度，否则时长列会退化成「全是最小值 / 全是封顶值」。
MIN_SPAN_S = 0.0  # 瞬时项的最小占用（秒）；仅作绘制保底，不参与数值
GAP_CAP_S = 3.0  # 空闲间隔上限：用户思考 1 小时不该把条带拉成 1 小时


class EntryKind(str, Enum):
    """轨迹条目类型 — 对应 DeepSeek Harness 的彩色 type 标签。

    CONTEXT 的徽章显示文本为 "HOOK"（DriFox 语义：hook 注入消息）。
    """

    SYSTEM = "SYSTEM"
    USER = "USER"
    CONTEXT = "HOOK"  # hook 注入（user 角色 + _hook_event），徽章显示 HOOK
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"

    @property
    def label(self) -> str:
        return self.value

    @property
    def lane(self) -> "Lane":
        """该类型归属的时间线泳道。"""
        if self == EntryKind.TOOL:
            return Lane.TOOLS
        if self == EntryKind.ASSISTANT:
            return Lane.MODEL
        return Lane.INPUT


class Lane(Enum):
    """时间线泳道 — 对齐 DeepSeek Harness 的 Input / Model / Tools。"""

    INPUT = "Input"
    MODEL = "Model"
    TOOLS = "Tools"


LANE_ORDER: tuple = (Lane.INPUT, Lane.MODEL, Lane.TOOLS)

# ── 类型主色（hex，深浅主题通吃；对齐 DeepSeek Harness 的彩色标签）──
# ⚠️ 键必须是 **EntryKind 成员**（定义顺序：本表须在 EntryKind 之后）：
# CONTEXT 的 value 是 "HOOK"（徽章文案），若按 value 查表会 miss →
# 全部 HOOK 条目退化成兜底灰 #888888。
ENTRY_KIND_COLORS: Dict[EntryKind, str] = {
    EntryKind.SYSTEM: "#7AA2F7",  # 蓝
    EntryKind.USER: "#E0AF68",  # 金橙
    EntryKind.CONTEXT: "#9ECE6A",  # 绿（徽章显示 HOOK）
    EntryKind.ASSISTANT: "#BB9AF7",  # 紫
    EntryKind.TOOL: "#7DCFFF",  # 青
}

# 按「枚举名 / 枚举值」反查成员 —— 兼容历史调用方传字符串的情况
_KIND_BY_NAME: Dict[str, EntryKind] = {k.name: k for k in EntryKind}
_KIND_BY_VALUE: Dict[str, EntryKind] = {k.value: k for k in EntryKind}


def kind_color(kind: Any) -> QColor:
    """类型主色（不透明 QColor）。

    ⚠️ 必须按**枚举成员**取色，不能用 ``kind.value``：CONTEXT 的 value 是
    "HOOK"（徽章显示文案，见 :class:`EntryKind`），按 value 查表必然 miss，
    结果是所有 HOOK 条目的徽章/时间线条带/详情标题全退化成兜底灰 #888888。
    """
    k = kind
    if not isinstance(k, EntryKind):
        key = str(getattr(kind, "name", "") or kind)
        k = _KIND_BY_NAME.get(key) or _KIND_BY_VALUE.get(str(kind)) or kind
    return QColor(ENTRY_KIND_COLORS.get(k, "#888888"))


def with_alpha(color: QColor, alpha: int) -> QColor:
    """从 QColor 派生带透明度的颜色（禁止用 "rgba(...)" 字符串构造 QColor）。"""
    c = QColor(color)
    c.setAlpha(max(0, min(255, int(alpha))))
    return c


def to_qcolor(value: Any, fallback: Any = "#888888") -> QColor:
    """把任意主题色值安全转成 QColor（**修 P022 黑字 bug 的唯一入口**）。

    支持：``QColor`` / ``"#RRGGBB"`` / ``"#RGB"`` / ``"rgb(r,g,b)"`` /
    ``"rgba(r,g,b,a)"``（a 既接受 0-255 也接受 0.0-1.0）。
    解析失败返回 fallback 的 QColor。
    """
    if isinstance(value, QColor):
        return QColor(value)
    if not isinstance(value, str):
        return QColor(fallback)  # type: ignore[arg-type]
    text = value.strip()
    if not text:
        return QColor(fallback)  # type: ignore[arg-type]
    m = _RGBA_RE.match(text)
    if m:
        r, g, b = (int(float(m.group(i))) for i in (1, 2, 3))
        a_raw = m.group(4)
        if a_raw is None:
            a = 255
        else:
            # 主题里两种 alpha 语义混用：含小数点按 0.0-1.0，否则按 0-255
            a = int(round(float(a_raw) * 255)) if ("." in a_raw and float(a_raw) <= 1.0) else int(float(a_raw))
        c = QColor(r, g, b, max(0, min(255, a)))
        if c.isValid():
            return c
        return QColor(fallback)  # type: ignore[arg-type]
    c = QColor(text)
    return c if c.isValid() else QColor(fallback)  # type: ignore[arg-type]


def css(color: QColor, alpha: Optional[int] = None) -> str:
    """QColor → CSS ``rgba(...)`` 字符串（QSS 用；QSS 能正确解析 rgba）。"""
    c = QColor(color)
    if alpha is not None:
        c.setAlpha(max(0, min(255, int(alpha))))
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha() / 255:.3g})"


@dataclass
class ThemePalette:
    """统一主题色板 — 三个面板共用，保证「同一语义色处处一致」。

    所有字段都是 **QColor**（已解析 rgba），可直接喂 QPainter；
    需要写 QSS 时用 :meth:`q` 取 rgba 字符串。
    """

    is_dark: bool = True
    text: QColor = field(default_factory=lambda: QColor("#E6E9F0"))
    text_secondary: QColor = field(default_factory=lambda: QColor(226, 235, 249, 184))
    text_muted: QColor = field(default_factory=lambda: QColor("#8B98AD"))
    border: QColor = field(default_factory=lambda: QColor("#3D4A60"))
    accent: QColor = field(default_factory=lambda: QColor("#66C6FF"))
    card_bg: QColor = field(default_factory=lambda: QColor(22, 30, 45, 230))
    hover_bg: QColor = field(default_factory=lambda: QColor(255, 255, 255, 20))
    selected_bg: QColor = field(default_factory=lambda: QColor(102, 198, 255, 82))
    line: QColor = field(default_factory=lambda: QColor("#FFFFFF"))
    danger: QColor = field(default_factory=lambda: QColor("#FF6B7A"))
    warning: QColor = field(default_factory=lambda: QColor("#E0AF68"))
    success: QColor = field(default_factory=lambda: QColor("#9ECE6A"))
    track: QColor = field(default_factory=lambda: QColor(255, 255, 255, 18))
    # ⚠️ ui 字体族必须是**系统字体**（ctx.font_family），否则整个面板会掉回
    # Qt 默认字体，跟主程序其它界面不是一套（用户反馈「全部字体都没应用系统字体」）。
    font_family: str = "Segoe UI"
    # 仅代码 / JSON / 数字列使用；逗号分隔 → Qt 按序回退
    mono_family: str = "Cascadia Mono, Consolas, Courier New, monospace"
    font_px: int = 13

    # ── 构造 ──

    @classmethod
    def from_theme(cls, colors: Optional[Dict[str, Any]], is_dark: bool = True, **extra: Any) -> "ThemePalette":
        """从 ``ctx["colors"]`` 构造；缺键逐字段回退，永不产生黑色。"""
        c = dict(colors or {})
        line = QColor("#FFFFFF") if is_dark else QColor("#000000")
        track = to_qcolor(c.get("card_bg_dim"), QColor(255, 255, 255, 18) if is_dark else QColor(0, 0, 0, 20))
        if not track.isValid() or track.alpha() == 0:
            track = QColor(255, 255, 255, 18) if is_dark else QColor(0, 0, 0, 20)
        pal = cls(
            is_dark=bool(is_dark),
            text=to_qcolor(c.get("text_primary"), "#E6E9F0" if is_dark else "#1A1F2B"),
            text_secondary=to_qcolor(c.get("text_secondary"), "#B9C3D4" if is_dark else "rgba(60,70,90,0.75)"),
            text_muted=to_qcolor(c.get("text_muted"), "#8B98AD" if is_dark else "#6B7688"),
            border=to_qcolor(c.get("border"), "#3D4A60" if is_dark else "#D6DCE6"),
            accent=to_qcolor(c.get("accent"), "#66C6FF"),
            card_bg=to_qcolor(c.get("card_bg"), QColor(22, 30, 45, 230) if is_dark else QColor(255, 255, 255, 245)),
            hover_bg=to_qcolor(c.get("hover_bg"), QColor(255, 255, 255, 20) if is_dark else QColor(0, 0, 0, 18)),
            selected_bg=to_qcolor(
                c.get("selected_bg"), QColor(102, 198, 255, 82) if is_dark else QColor(0, 110, 200, 48)
            ),
            line=line,
            danger=to_qcolor(c.get("status_error") or c.get("syntax_error"), "#FF6B7A"),
            warning=to_qcolor(c.get("accent_warm"), "#E0AF68"),
            success=to_qcolor(c.get("syntax_success"), "#9ECE6A"),
            track=track,
        )
        for k, v in extra.items():
            if hasattr(pal, k):
                setattr(pal, k, v)
        return pal

    # ── 工具 ──

    def q(self, name: str, alpha: Optional[int] = None) -> str:
        """取 CSS rgba 字符串（写 QSS 用）。"""
        return css(getattr(self, name, self.text), alpha)

    def line_at(self, alpha: int) -> QColor:
        """分隔线/底色用的半透明线色（深色叠白、浅色叠黑）。"""
        return with_alpha(self.line, alpha)


@dataclass
class TraceRecord:
    """单条轨迹记录。

    Attributes:
        kind: 类型标签（SYSTEM/USER/CONTEXT/ASSISTANT/TOOL）。
        label: 显示在 type 标签右侧的标题（hook 事件名 / tool 名 / role）。
        preview: 内容首行预览。
        raw: 完整原始内容（字符串或 JSON 化后的字符串）。
        source: 来源描述（"hook · PostToolUse" / "session.messages[5]" 等）。
        start_ts: 起始时间戳（time.time() 浮点秒）。0.0 表示未知。
        end_ts: 终止时间戳。0 / <=start 表示瞬时（消息写入类事件）。
        is_pending: 是否仍处于 in-flight（流式生成、tool 调用未返回）。
        is_error: 是否失败（仅 TOOL 适用）。
        turn_no: 所属对话轮次（从 1 起；collector 按真实 USER 消息计数填充）。
        meta: 额外结构化元数据（tool_call_id / arguments / result 等）。
    """

    kind: EntryKind
    label: str
    preview: str
    raw: str
    source: str = ""
    start_ts: float = 0.0
    end_ts: float = 0.0
    is_pending: bool = False
    is_error: bool = False
    turn_no: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def lane(self) -> Lane:
        return self.kind.lane

    @property
    def duration_ms(self) -> int:
        """持续毫秒数。

        - 未启动（start_ts<=0）→ -1
        - in-flight → now - start（由 UI 定时器驱动重绘）
        - 已结束且 end<=start（瞬时消息写入）→ 0
        - 其余 → end - start（**固定值**，不随时间增长）
        """
        if self.start_ts <= 0:
            return -1
        if self.is_pending:
            return max(0, int((time.time() - self.start_ts) * 1000))
        if self.end_ts <= self.start_ts:
            return 0
        return int((self.end_ts - self.start_ts) * 1000)

    @property
    def span_end_ts(self) -> float:
        """占用区间终点（绘制用）。

        有真实耗时（TOOL / ASSISTANT 由实时信号回填）→ 用 end_ts；
        瞬时消息 → 延伸到「下一条的起点」（由 collector 算好放 meta["span_end"]），
        上限 ``GAP_CAP_S``，让时间线是一条连续的带子而不是一排碎点。
        """
        if self.is_pending:
            return time.time()
        if self.end_ts > self.start_ts:
            return self.end_ts
        e = self.meta.get("span_end")
        if e:
            try:
                return max(self.start_ts, float(e))
            except Exception:
                pass
        # 兜底：collector 没预填（例如手工构造的记录）→ 视为瞬时，不伪造时长
        return self.start_ts

    @property
    def span_ms(self) -> int:
        """占用时长（毫秒）—— 与条带宽度一致。

        ⚠️ 0 = 瞬时事件（同秒注入的消息写入，**没有**伪造一个保底值）。
        早期版本给 0 间隔保底 80ms，导致时长列全是 80ms / 3s 两个怪值。
        """
        if self.start_ts <= 0:
            return 0
        return max(0, int((self.span_end_ts - self.start_ts) * 1000))

    @property
    def span_label(self) -> str:
        """占用时长文案：``—`` / ``1.20 s`` / ``≥3.00 s``（被封顶时加 ≥）。"""
        ms = self.span_ms
        if ms <= 0:
            return "—"
        text = format_duration(ms)
        return f"≥{text}" if self.meta.get("span_capped") else text

    @property
    def absolute_time(self) -> str:
        """绝对时间戳（HH:MM:SS），未知返回 '-'。"""
        if self.start_ts <= 0:
            return "-"
        return _format_hms(self.start_ts)

    @property
    def size(self) -> int:
        """内容体量（字节数，UTF-8）。"""
        try:
            return len((self.raw or "").encode("utf-8", errors="ignore"))
        except Exception:
            return 0

    @property
    def tokens(self) -> int:
        """内容占用的 token 数（懒计算 + 写回 meta 缓存）。

        collector 会用主程序的 ``token_estimator``（tiktoken）预填
        ``meta["tokens"]``；没有预填时按字符比例兜底估算，并缓存回 meta，
        避免自绘时每行重算。
        """
        t = self.meta.get("tokens")
        if t is None:
            t = estimate_tokens_text(self.raw)
            self.meta["tokens"] = t
        try:
            return int(t)
        except Exception:
            return 0

    @property
    def status(self) -> str:
        """状态文案（失败 / 进行中 / 完成 / 已记录）。"""
        if self.is_error:
            return "失败"
        if self.is_pending:
            return "进行中"
        return "完成"


def _format_hms(epoch: float) -> str:
    """把浮点时间戳格式化为 HH:MM:SS。"""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def format_duration(ms: int) -> str:
    """人类可读时长：<1000ms → ``830 ms``；<60s → ``2.45 s``；其余 → ``1m 23s``。"""
    if ms is None or ms < 0:
        return "-"
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    minutes, rem = divmod(ms, 60_000)
    seconds = rem // 1000
    return f"{minutes}m {seconds}s"


def format_duration_compact(ms: int) -> str:
    """压缩版时长（时间线条内文字用）。"""
    if ms is None or ms < 0:
        return "-"
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, rem = divmod(ms, 60_000)
    seconds = rem // 1000
    return f"{minutes}m{seconds}s"


def format_size(nbytes: int) -> str:
    """Network 面板风格体量：<1024 → ``812 B``；<1MB → ``12.4 kB``；其余 MB。"""
    if not nbytes or nbytes < 0:
        return "—"
    if nbytes < 1024:
        return f"{nbytes} B"
    if nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f} kB"
    return f"{nbytes / 1024 / 1024:.2f} MB"


def format_tokens(n: int) -> str:
    """紧凑 token 显示（列宽有限）：``812`` / ``1.2k`` / ``24k``。"""
    if not n or n <= 0:
        return "—"
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    return f"{round(n / 1000)}k"


_TOKEN_ESTIMATOR: Any = None


def estimate_tokens_text(text: str, model: str = "gpt-4") -> int:
    """文本 token 估算：优先主程序 ``app.core.token_estimator``（tiktoken），
    失败/导入不到时按「中文 0.7 / 西文 1/3.5」比例兜底。

    ⚠️ 结果是**逐条缓存**的（写回 ``TraceRecord.meta``），不要在绘制热路径上
    对长文本反复调用。
    """
    if not text:
        return 0
    global _TOKEN_ESTIMATOR
    if _TOKEN_ESTIMATOR is None:
        try:
            from app.core.token_estimator import estimate_tokens as _est

            _TOKEN_ESTIMATOR = _est
        except Exception:
            _TOKEN_ESTIMATOR = _fallback_tokens
    try:
        return int(_TOKEN_ESTIMATOR(text, model))
    except Exception:
        return _fallback_tokens(text)


def _fallback_tokens(text: str) -> int:
    """无 tiktoken 时的兜底估算（中文按字、西文按 3.5 字符/token）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return max(1, int(cjk * 0.7 + (len(text) - cjk) / 3.5))


def truncate(text: str, max_chars: int = 120) -> str:
    """首行截断。空内容返回 '（空）'。"""
    if not text:
        return "（空）"
    head = text.strip().split("\n", 1)[0].strip()
    if len(head) <= max_chars:
        return head
    return head[: max_chars - 1] + "…"


def content_to_text(content: Any) -> str:
    """把 message.content（str | list[dict] | 其它）统一转成可读文本。

    多模态 list 优先提取 ``text`` 段；tool_use / tool_result 段 JSON 化，
    避免 ``str(list)`` 那种 python-repr 噪音。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for seg in content:
            if isinstance(seg, dict):
                if isinstance(seg.get("text"), str):
                    parts.append(seg["text"])
                else:
                    try:
                        import json as _json

                        parts.append(_json.dumps(seg, ensure_ascii=False, indent=2))
                    except Exception:
                        parts.append(str(seg))
            else:
                parts.append(str(seg))
        return "\n".join(p for p in parts if p)
    try:
        import json as _json

        return _json.dumps(content, ensure_ascii=False, indent=2)
    except Exception:
        return str(content)


def infer_message_kind(message: Dict[str, Any]) -> EntryKind:
    """把 ``session.messages`` 中的 dict 映射到 ``EntryKind``。"""
    role = message.get("role", "")
    hook_event = message.get("_hook_event")
    if role == "system":
        return EntryKind.SYSTEM
    if hook_event:
        # UserPromptSubmit 是真实用户输入的增强，归 USER；其余 hook 注入归 CONTEXT
        if hook_event == "UserPromptSubmit":
            return EntryKind.USER
        return EntryKind.CONTEXT
    if role == "user":
        return EntryKind.USER
    if role == "tool":
        return EntryKind.TOOL
    if role == "assistant":
        return EntryKind.ASSISTANT
    return EntryKind.CONTEXT


def is_real_user_message(message: Dict[str, Any]) -> bool:
    """是否真实用户输入（非 hook 注入）— 用于 turn 计数。"""
    return message.get("role") == "user" and not message.get("_hook_event")


def message_label(message: Dict[str, Any]) -> str:
    """条目展示标题（type 标签右侧那行字）。"""
    hook_event = message.get("_hook_event")
    if hook_event and hook_event != "UserPromptSubmit":
        return hook_event
    role = message.get("role", "")
    if role == "tool":
        return message.get("name") or message.get("tool_name") or "tool"
    if role == "system":
        return "System Prompt"
    if role == "user":
        return "User"
    if role == "assistant":
        return "Assistant"
    return role or "Entry"


def message_source(idx: int, extra: str = "") -> str:
    """把内部来源信息格式化为可读字符串。"""
    base = f"messages[{idx}]"
    return f"{base}{(' · ' + extra) if extra else ''}"


def pretty_json(text: str, indent: int = 2) -> str:
    """能解析成 JSON 就 pretty print，否则原样返回（详情面板通用）。"""
    if not text:
        return ""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        import json as _json

        return _json.dumps(_json.loads(stripped), ensure_ascii=False, indent=indent)
    except Exception:
        return text


def time_bounds(records: list) -> Tuple[float, float]:
    """一组记录的时间边界 (t0, t1)；无有效时间返回 (0.0, 1.0) 防除零。

    右端用 :attr:`TraceRecord.span_end_ts`（占用终点）而不是 ``end_ts`` ——
    否则全是瞬时消息时 t1 == t0，时间线退化成一个点。
    """
    starts = [r.start_ts for r in records if getattr(r, "start_ts", 0) > 0]
    if not starts:
        return 0.0, 1.0
    t0 = min(starts)
    ends = [max(r.span_end_ts, r.start_ts) for r in records if getattr(r, "start_ts", 0) > 0]
    t1 = max(ends) if ends else t0
    if t1 <= t0:
        t1 = t0 + 1.0
    return t0, t1


def merge_overlapping(records: list) -> Tuple[int, int]:
    """重叠时间区间的并集统计 → ``(总毫秒, 连通段数)``。

    为什么不用「Σ duration_ms」求和（2026-09-03）：

    宿主 chat_worker 落盘时把一次 API 响应里的**每个并行 tool_call 拆成
    一条独立 assistant 消息**（``_build_response_message_sequence`` 对
    ``tool_call_marker`` 逐个建条），这些消息共享**同一次 API 调用的
    elapsed_ms**、同一时刻写入 → 投影出的 N 条 ASSISTANT 区间完全重叠。
    求和会把同一次调用计时 N 遍（实测 LLM 总时长放大 2-4 倍）；并行执行
    的工具同理。并集（墙钟口径）才是真实耗时。

    段数 = 合并后的独立区间块数，可当「LLM 调用次数」用：同批拆条的
    N 条 assistant 完全重叠 → 1 段 = 1 次真实调用。

    口径细节：
    - ``start_ts <= 0``（无时间）→ 跳过；瞬时条目（end <= start 且非
      pending，如无 elapsed_ms 的历史消息）不贡献时长，与旧求和口径一致；
    - in-flight（``is_pending``）按 ``now - start`` 计（与
      :attr:`TraceRecord.duration_ms` 一致）；
    - 相接区间（下一段 start == 当前段 end）视为连续 —— 毫秒打点上相接
      基本只出现在同一次调用链内。
    """
    spans: list = []
    now = time.time()
    for r in records:
        s = getattr(r, "start_ts", 0)
        if s <= 0:
            continue
        e = getattr(r, "end_ts", 0)
        if e <= s:
            if getattr(r, "is_pending", False):
                spans.append((s, now))
            continue
        spans.append((s, e))
    if not spans:
        return 0, 0
    spans.sort()
    total = 0.0
    segments = 0
    cs, ce = spans[0]
    for s, e in spans[1:]:
        if s > ce:
            total += ce - cs
            segments += 1
            cs, ce = s, e
        elif e > ce:
            ce = e
    total += ce - cs
    segments += 1
    return int(total * 1000), segments
