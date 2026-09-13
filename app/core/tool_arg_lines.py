# -*- coding: utf-8 -*-
"""编辑类工具流式参数的进度工具集（零 Qt 依赖，worker 与单测共用）。

三件事：
1. **行数估算**——write/edit/multi_edit 在参数流式接收期间的表现从「字符数」升级为
   增删行数（+N/-M），从**半截 JSON 缓冲**估算，不依赖完整解析；
2. **路径提前提取**——容忍未闭合字符串，让运行框尽早显示真实文件名（原正则要求引号闭合，
   写文件时 path 常长时间不可见）；
3. **进度推送节流**——原 200/500 字符门槛让运行框长时间不动，改为字符增长 + 时间间隔
   双门槛（首帧必发、超保底间隔无条件发）。

口径：行数 = 换行转义数 + 1（片段非空），与 diff 统计的「行数」语义一致。
设计：docs/superpowers/specs/2026-09-13-tool-streaming-line-stats-design.md
"""

import re
from typing import Dict, Iterator, Tuple

# 工具 → 参与统计的字段（field, kind）；kind: add=新增行 del=删除行
LINE_COUNT_FIELDS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "write": (("content", "add"),),
    "edit": (("oldString", "del"), ("newString", "add")),
    "multi_edit": (("oldString", "del"), ("newString", "add")),
}

_FIELD_PATTERN_CACHE: Dict[str, "re.Pattern[str]"] = {}

# 路径字段提前提取：**不要求引号闭合**（写文件时 path 常落在未闭合的 JSON 片段里）
_PATH_KEY_PATTERN = re.compile(r'"(?:path|file_path|file|target)"\s*:\s*"([^"]*)')


def _field_pattern(field: str) -> "re.Pattern[str]":
    """`"field"\\s*:\\s*"` 的匹配模式（缓存，流式高频调用不重复编译）"""
    pat = _FIELD_PATTERN_CACHE.get(field)
    if pat is None:
        pat = re.compile(r'"' + re.escape(field) + r'"\s*:\s*"')
        _FIELD_PATTERN_CACHE[field] = pat
    return pat


def _iter_string_fragments(buffer_text: str, field: str) -> Iterator[str]:
    """产出 `"field": "` 之后的原始片段（截到未转义引号或缓冲末尾）。

    - 未闭合的字符串片段截到缓冲末尾（流式中间态，最常见）；
    - 已闭合的截到结束引号；
    - 转义字符用「反斜杠跳过下一字符」处理，避免把 `\\"` 误当结束。
    """
    if not buffer_text:
        return
    for match in _field_pattern(field).finditer(buffer_text):
        start = match.end()
        i = start
        n = len(buffer_text)
        while i < n:
            ch = buffer_text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                break
            i += 1
        yield buffer_text[start : min(i, n)]


def count_escaped_newlines(fragment: str) -> int:
    """统计片段里的换行转义数：连续反斜杠个数为**奇数**时才是 `\\n` 转义"""
    count = 0
    i = 0
    n = len(fragment)
    while i < n:
        if fragment[i] != "\\":
            i += 1
            continue
        j = i
        while j < n and fragment[j] == "\\":
            j += 1
        run = j - i
        if j < n and fragment[j] == "n" and run % 2 == 1:
            count += 1
            i = j + 1
            continue
        i = j
    return count


def estimate_streaming_lines(tool_name: str, buffer_text: str) -> Tuple[int, int]:
    """估算 (新增行, 删除行)；非编辑类工具或空缓冲返回 (0, 0)

    行数 = 换行转义数 + 1（片段非空），与 diff 统计的「行数」语义一致；
    片段闭合前后内容不变，故不会出现数字跳变。
    """
    fields = LINE_COUNT_FIELDS.get(tool_name or "")
    if not fields or not buffer_text:
        return 0, 0
    add = 0
    dele = 0
    for field, kind in fields:
        for fragment in _iter_string_fragments(buffer_text, field):
            lines = count_escaped_newlines(fragment)
            if fragment:
                lines += 1  # 片段非空 = 至少一行内容
            if kind == "add":
                add += lines
            else:
                dele += lines
    return add, dele


def extract_partial_path(buffer_text: str) -> str:
    """从半截 JSON 缓冲里提取文件路径（**容忍未闭合字符串**，返回已接收部分）。

    原正则要求引号闭合（`"([^"]+)"`），而写文件时 JSON 长时间不闭合
    → 运行框长时间只能显示「写入文件中」而没有文件名。改成吃到已接收字符为止，
    路径可随流式逐渐变长。
    """
    if not buffer_text:
        return ""
    m = _PATH_KEY_PATTERN.search(buffer_text)
    return m.group(1) if m else ""


# 进度推送节流：字符增长门槛与时间间隔（原 200/500 字符门槛让运行框长时间不动）
PROGRESS_MIN_CHARS = 40
PROGRESS_MIN_INTERVAL_MS = 150
PROGRESS_MAX_INTERVAL_MS = 400


def should_emit_progress(
    prev_len: int,
    args_len: int,
    last_ts_ms: float,
    now_ms: float,
    min_chars: int = PROGRESS_MIN_CHARS,
    min_interval_ms: int = PROGRESS_MIN_INTERVAL_MS,
    max_interval_ms: int = PROGRESS_MAX_INTERVAL_MS,
) -> bool:
    """是否推送一次工具参数进度（兼顾界面动效与 IPC 频率）。

    - 首帧（prev_len=0）恒发；
    - 字符增长达门槛**且**距上次 ≥ min_interval_ms：发（防高频 IPC）；
    - 距上次 ≥ max_interval_ms：保底发（即使字符没怎么涨，也让界面持续有反馈）。
    """
    if not prev_len:
        return True
    elapsed = now_ms - last_ts_ms
    if elapsed >= max_interval_ms:
        return True
    return (args_len - prev_len) >= min_chars and elapsed >= min_interval_ms


def build_progress_payload(
    tool_name: str,
    buffer_text: str,
    args_len: int,
    path: str = "",
    prev_lines: Tuple[int, int] = (0, 0),
) -> Tuple[Dict[str, object], Tuple[int, int]]:
    """构造 progress 事件参数，并返回本次行数估计（供调用方保存以实现「只增不减」）。

    Returns:
        (payload, (add, dele)) —— payload 至少含 `_status` / `_args_len`；
        编辑类工具行数非零时追加 `_add_lines` / `_del_lines`；`path` 非空时追加 `_path`。
    """
    payload: Dict[str, object] = {"_status": "loading", "_args_len": args_len}
    if path:
        payload["_path"] = path
    add, dele = estimate_streaming_lines(tool_name, buffer_text)
    # 只增不减：流式期间参数只增，已计出的值不回退（防正则失配导致数字抖动）
    add = max(add, int(prev_lines[0]))
    dele = max(dele, int(prev_lines[1]))
    if add or dele:
        payload["_add_lines"] = add
        payload["_del_lines"] = dele
    return payload, (add, dele)
