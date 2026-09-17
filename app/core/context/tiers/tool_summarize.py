# -*- coding: utf-8 -*-
"""order 60 — 旧大工具输出 1-line 摘要化。

来源：_prune_old_tool_results Pass 2（_summarize_tool_result）。
四个跳过条件（现状行为，缺一不可）：
  1. _is_protected_tool(tool_name) 为真 → 保护工具完整保留
  2. 内容 < 1000 字符 → 不值得摘要
  3. content 以 '<persisted-output>' 开头 → 保文件路径；摘要掉会导致
     LLM 无法按路径回读 → 超大结果 → 再落盘 → 死循环
  4. 未超达标线（used <= target）→ 交给 cascade 判定，本层不动
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.context.tiers import count_tokens
from app.plugins.contracts.context_policy import CACHE_INVALIDATE, STAGE_SEND, STAGE_UI, TierOutcome

SUMMARIZE_MIN_CHARS = 1000


class ToolSummarizeTier:
    id = "tool_summarize"
    label = "旧工具输出摘要化"
    order = 60
    stages = frozenset({STAGE_SEND, STAGE_UI})
    cache_impact = CACHE_INVALIDATE

    def should_apply(self, view) -> bool:
        # 仅当未达标才介入：本层破坏前缀，不该在已达标时白跑
        if view.used_tokens <= view.target_tokens:
            return False
        return any(
            m.get("role") == "tool"
            and isinstance(m.get("content"), str)
            and len(m["content"]) >= SUMMARIZE_MIN_CHARS
            and not m["content"].lstrip().startswith("<persisted-output>")
            for m in view.messages
        )

    def apply(self, view) -> TierOutcome:
        from app.core.history_compactor import _is_protected_tool, _summarize_tool_result

        # tool_call_id → (tool_name, arguments_json)
        call_id_to_tool: Dict[str, tuple] = {}
        for m in view.messages:
            if m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    call_id_to_tool[tc.get("id", "")] = (fn.get("name", "unknown"), fn.get("arguments", ""))

        result: List[Dict[str, Any]] = list(view.messages)
        pruned = 0
        for i, m in enumerate(result):
            if m.get("role") != "tool":
                continue
            cid = m.get("tool_call_id")
            if not cid or cid not in call_id_to_tool:
                continue
            tool_name, tool_args = call_id_to_tool[cid]
            if _is_protected_tool(tool_name):
                continue
            content = m.get("content") or ""
            if not isinstance(content, str) or len(content) < SUMMARIZE_MIN_CHARS:
                continue
            if content.lstrip().startswith("<persisted-output>"):
                continue
            result[i] = {**m, "content": _summarize_tool_result(tool_name, tool_args, content)}
            pruned += 1

        if pruned == 0:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无旧大工具输出")
        saved = view.used_tokens - count_tokens(result)
        return TierOutcome(
            messages=result, saved_tokens=max(0, saved), note=f"摘要化 {pruned} 条旧工具输出"
        )
