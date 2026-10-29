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
from typing import Any, Dict, List

from app.plugins.contracts.context_policy import ALL_STAGES, CACHE_NONE, TierOutcome
from app.plugins.sdk import count_messages_tokens

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


def normalize_text(text: str) -> str:
    """无损规范化（纯函数、幂等）：ANSI / 控制字符 / 行尾空白 / 连续空行。"""
    out = _ANSI_RE.sub("", text)
    out = _CTRL_RE.sub("", out)
    out = _TRAILING_WS_RE.sub("", out)
    out = _BLANK_RUN_RE.sub("\n\n", out)
    return out


class ToolNormalizeTier:
    id = "tool_normalize"
    label = "工具结果无损规范化"
    order = 12
    # 设置页细项行副标题（component_items 走 AST 静态读取，仅支持字面量）
    description = "剥离 ANSI 转义与控制字符、压缩连续空行、去行尾空白；不删任何有效信息，每轮投影一致，不破坏 prompt cache"
    stages = frozenset(ALL_STAGES)
    cache_impact = CACHE_NONE

    def should_apply(self, view) -> bool:
        for m in view.messages:
            if m.get("role") != "tool":
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
            if m.get("role") == "tool" and isinstance(c, str) and len(c) >= _MIN_LEN:
                new_content = normalize_text(c)
                if new_content != c:
                    m = {**m, "content": new_content}
                    changed += 1
            result.append(m)
        if changed == 0:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无需规范化")
        try:
            saved = max(0, view.used_tokens - count_messages_tokens(result))
        except Exception:
            saved = 0
        return TierOutcome(messages=result, saved_tokens=saved, note=f"规范化 {changed} 条工具结果")


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(ToolNormalizeTier())
