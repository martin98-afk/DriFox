# -*- coding: utf-8 -*-
"""工具结果截断（S1）— 从 app/core/context_builder.py 迁出，行为零变化。

迁移动机：context_builder 需 import pipeline，而 pipeline 的 tier 需 import
本模块的 prune_tool_result —— 留在本模块会形成环。迁出后依赖方向为
单向：context_builder → pipeline → tiers → tool_prune。

context_builder 与 message_content 均保留 re-export，既有导入不断。

超过阈值的工具输出，保留头 + 尾，中间用省略标记替换 —— 避免超大
工具结果全量占用上下文 token。阈值随模型上下文容量动态放大。
仅在「组装发送给 LLM 的上下文」时截断，session 原始存储不受影响。

分层截断（避免"截断后 LLM 因信息缺失反复调用工具补查"）：
  A. read 类文件内容：解析 #File: (Lines a-b of N) 元信息，省略标记
     给出被截断区间的文件行号 + read 取回指引（一次精准补查）
  B. 行式条目结果（grep/list/glob）：按行保留头尾条目（保索引），
     省略标记给出条目区间 + 缩小范围重查指引
  C. 通用自由文本：字符 head+tail + 缩小输出范围指引
"""

from __future__ import annotations

import re
from typing import Dict, Optional

from app.core.model_capabilities import resolve_context_limit

TOOL_RESULT_MAX_LEN = 8192  # 基础阈值（短上下文模型默认）
TOOL_RESULT_HEAD_KEEP = 4096  # 保留头部字符数
TOOL_RESULT_TAIL_KEEP = 1024  # 保留尾部字符数
TOOL_RESULT_ELLIPSIS = "\n...[工具结果过长，已截断中间 {dropped} 字符，头 {head} + 尾 {tail}]...\n"

# 不应截断的工具（结果小且语义完整，截断会破坏关键信息）
# 兜底名单：正式来源为工具注册时的 metadata["no_prune"]（见 ToolRegistry.is_no_prune），
# 本集合保留给未经 registry 注册即直接调用本函数的场景（如序列化器插件）。
PRUNE_SKIP_TOOLS = frozenset({"question", "skill", "todoread"})

# read 工具输出头: #File: {path} (Lines {a}-{b} of {N})
_FILE_HEADER_RE = re.compile(r"^#File: (.+?) \(Lines (\d+)-(\d+) of (\d+)\)\s*$", re.MULTILINE)

# 行式条目结果标记（grep/list/glob 输出首行）
_LINE_INDEXED_MARKERS = ("匹配文件: ", "目录: ")


def resolve_tool_result_max_len(llm_config: Optional[Dict] = None) -> int:
    """动态工具结果截断阈值：按模型上下文容量缩放。

    - 上下文 <= 32K   -> 8192（基础）
    - 上下文 32K~64K  -> 12288
    - 上下文 64K~128K -> 16384
    - 上下文 >= 128K  -> 24576
    """
    base = TOOL_RESULT_MAX_LEN
    if not llm_config:
        return base
    try:
        context_limit = resolve_context_limit(llm_config)
    except Exception:
        return base
    if context_limit >= 128000:
        return base * 3
    if context_limit >= 64000:
        return base * 2
    if context_limit >= 32000:
        return int(base * 1.5)
    return base


def _is_line_indexed_result(content: str) -> bool:
    """识别行式条目结果（grep/list/glob）：首行带标记，后续按行输出条目。"""
    for line in content.split("\n")[:4]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_LINE_INDEXED_MARKERS):
            return True
        if "个文件:" in stripped:
            return True
    return False


def _prune_line_indexed(content: str, head_keep: int, tail_keep: int) -> Optional[str]:
    """行式条目结果按行截断：保留头尾条目，中间按行省略（保索引）。

    返回截断结果；行数不足时返回 None 交给通用截断。
    """
    lines = content.split("\n")
    total = len(lines)
    if total <= 6:
        return None
    avg_line_len = len(content) / max(1, total)
    head_lines = max(3, int(head_keep / max(1.0, avg_line_len)))
    tail_lines = max(2, int(tail_keep / max(1.0, avg_line_len)))
    if head_lines + tail_lines >= total:
        return None
    head = lines[:head_lines]
    tail = lines[-tail_lines:]
    dropped = total - head_lines - tail_lines
    marker = (
        f"\n...[结果共 {total} 行，已省略中间第 {head_lines + 1}-{total - tail_lines} 行"
        f"（{dropped} 个条目）。如需查看被省略内容，请重新调用工具并缩小范围"
        f"（更精确的 path/pattern/include）]...\n"
    )
    return "\n".join(head) + marker + "\n".join(tail)


def _prune_read_result(content: str, head_keep: int, tail_keep: int) -> Optional[str]:
    """read 类文件内容截断：省略标记给出被截断区间的文件行号 + 取回指引。"""
    m = _FILE_HEADER_RE.match(content)
    if not m:
        return None
    display = m.group(1)
    start_line, end_line, total_lines = int(m.group(2)), int(m.group(3)), int(m.group(4))
    dropped = len(content) - head_keep - tail_keep
    if dropped <= 0:
        return content[:head_keep]
    head = content[:head_keep]
    tail = content[-tail_keep:]
    # head 内完整内容行数（去掉 header 行）
    head_content_lines = max(0, head.count("\n") - 1)
    # tail 内行数（从尾部起）
    tail_content_lines = max(0, tail.count("\n"))
    trunc_start = start_line + head_content_lines
    trunc_end = end_line - tail_content_lines
    if trunc_start > trunc_end:
        trunc_start, trunc_end = trunc_end, trunc_start
    marker = (
        f"\n...[已截断中间约 {dropped} 字符（文件第 {trunc_start}-{trunc_end} 行，共 {total_lines} 行）。"
        f"如需查看该区间，可用 read 工具：read {display} startline={trunc_start} endline={trunc_end}]...\n"
    )
    return head + marker + tail


def _prune_generic(content: str, head_keep: int, tail_keep: int) -> str:
    """通用自由文本：字符 head+tail + 缩小输出范围指引。"""
    dropped = len(content) - head_keep - tail_keep
    if dropped <= 0:
        # 阈值配置异常（head+tail >= len），保底只留 head
        return content[:head_keep]
    head = content[:head_keep]
    tail = content[-tail_keep:]
    marker = TOOL_RESULT_ELLIPSIS.format(dropped=dropped, head=head_keep, tail=tail_keep)
    # 追加可操作指引：让 LLM 知道如何取回完整内容，避免盲目重查
    marker += "如需完整内容，可重新调用该工具并缩小输出范围（指定 path/pattern/行号）。"
    return head + marker + tail


def prune_tool_result(
    content: str,
    max_len: Optional[int] = None,
    head_keep: Optional[int] = None,
    tail_keep: Optional[int] = None,
    tool_name: str = "",
    llm_config: Optional[Dict] = None,
) -> str:
    """截断超长工具结果，按结果类型分层保留可操作信息。

    返回截断后的字符串；未超阈值、非字符串或受保护工具原样返回。
    省略标记内含被裁信息，供 token 计量（S2）量化截断收益。
    """
    if not isinstance(content, str) or not content:
        return content
    if tool_name in PRUNE_SKIP_TOOLS:
        return content
    limit = max_len if max_len is not None else resolve_tool_result_max_len(llm_config)
    if len(content) <= limit:
        return content
    head = head_keep if head_keep is not None else TOOL_RESULT_HEAD_KEEP
    tail = tail_keep if tail_keep is not None else TOOL_RESULT_TAIL_KEEP

    # B: 行式条目结果 → 按行保索引（条目区间 + 重查指引）
    if _is_line_indexed_result(content):
        pruned = _prune_line_indexed(content, head, tail)
        if pruned is not None:
            return pruned
    # A: read 文件结果 → 文件行号区间 + read 取回指引
    pruned = _prune_read_result(content, head, tail)
    if pruned is not None:
        return pruned
    # C: 通用自由文本
    return _prune_generic(content, head, tail)
