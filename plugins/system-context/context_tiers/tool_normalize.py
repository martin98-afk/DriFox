# -*- coding: utf-8 -*-
"""内置 tier（order 12）：工具结果无损规范化。

只做语义无关的字节清洗，模型读到的信息量不减：
  - 剥离 ANSI 转义序列（彩色日志、进度条控制码）
  - 压缩 3 行以上连续空行
  - 去行尾空白、去 NUL 等不可见控制字符

为什么放在 tool_offload(15) 与 tool_prune(20) 之前：
落盘与截断都会按字符数判定阈值，先规范化能让阈值口径更准，
也避免把 ANSI 控制码当成有效内容计入预算。

cache_impact=none：纯函数、幂等、同内容每轮投影完全一致，前缀稳定。
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any, Dict, List

from app.plugins.contracts.context_policy import ALL_STAGES, CACHE_NONE, TierOutcome, needs_tool_projection

# 只处理长内容：短结果清洗收益低，复制开销反而显眼
_MIN_LEN = 1024

# CSI / OSC 转义序列（颜色、光标移动、标题设置）
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# 除 \n \t 外的 C0 控制字符
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# 行尾空白
_TRAILING_WS_RE = re.compile(r"[ \t]+(?=\n)")
# 3 行以上连续空行 → 2 行
_BLANK_RUN_RE = re.compile(r"\n{3,}")


# 快速门控：四个正则各扫全文一次 ≈ 1ms/20KB，且绝大多数工具结果本就是干净文本
# （源码/JSON/日志无 ANSI、无行尾空白）。先用 C 层子串检测（合计 ≈0.03ms）判断
# 有没有可处理目标，全无则直接返回原文，不产生任何拷贝。
# 覆盖关系：`[ \t]+(?=\n)` 命中必然以空格或制表符收尾 → 含 " \n" 或 "\t\n"。
_CLEAN_HINTS = ("\x1b", " \n", "\t\n", "\n\n\n")

# [E2-K] 规范化产物 memo：(id(content), len(content)) → (原文引用, 产物)。
# 规范化幂等（产物再跑输出不变），条目持原文引用保证 id 存活期内不复用；
# 命中即跳过四条正则全文扫描。与 tool_shape 各自实现，无共享状态依赖。
_NORM_MEMO_MAX = 512
_norm_memo: OrderedDict[tuple[int, int], tuple[str, str]] = OrderedDict()

def _norm_memo_write(memo_key: tuple[int, int], text: str, out: str) -> None:
    """写入 memo 并在超限时按 LRU 逐出最旧条目。"""
    _norm_memo[memo_key] = (text, out)
    if len(_norm_memo) > _NORM_MEMO_MAX:
        _norm_memo.popitem(last=False)

def normalize_text(text: str) -> str:
    """无损规范化（纯函数、幂等）：ANSI / 控制字符 / 行尾空白 / 连续空行。"""
    memo_key = (id(text), len(text))
    hit = _norm_memo.get(memo_key)
    if hit is not None:
        orig, out = hit
        if orig is text:
            _norm_memo.move_to_end(memo_key)
            return out
        _norm_memo.pop(memo_key, None)  # id 已被复用：过期条目，重算
    if not any(h in text for h in _CLEAN_HINTS) and _CTRL_RE.search(text) is None:
        _norm_memo_write(memo_key, text, text)
        return text
    out = _ANSI_RE.sub("", text)
    out = _CTRL_RE.sub("", out)
    out = _TRAILING_WS_RE.sub("", out)
    out = _BLANK_RUN_RE.sub("\n\n", out)
    _norm_memo_write(memo_key, text, out)
    return out


class ToolNormalizeTier:
    id = "tool_normalize"
    label = "工具结果无损规范化"
    order = 12
    # 设置页细项行副标题（component_items 走 AST 静态读取，仅支持字面量）
    description = "剥离 ANSI 转义与控制字符、压缩连续空行、去行尾空白；不删任何有效信息，每轮投影一致，不破坏 prompt cache"
    stages = frozenset(ALL_STAGES)
    cache_impact = CACHE_NONE
    per_message = True

    def should_apply(self, view) -> bool:
        for m in view.messages:
            if not needs_tool_projection(m):
                continue
            c = m.get("content")
            if isinstance(c, str) and len(c) >= _MIN_LEN:
                return True
        return False

    def apply(self, view) -> TierOutcome:
        result: List[Dict[str, Any]] = []
        changed = 0
        for m in view.messages:
            c = m.get("content")
            if needs_tool_projection(m) and isinstance(c, str) and len(c) >= _MIN_LEN:
                new_content = normalize_text(c)
                if new_content != c:
                    m = {**m, "content": new_content}
                    changed += 1
            result.append(m)
        if changed == 0:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无需规范化")
        # [#R6] saved 由 pipeline 统一增量计（同 tool_prune），不再全量 count。
        return TierOutcome(messages=result, saved_tokens=0, note=f"规范化 {changed} 条工具结果")


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(ToolNormalizeTier())
