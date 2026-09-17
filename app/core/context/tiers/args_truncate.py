# -*- coding: utf-8 -*-
"""order 50 — tool_call 参数截断：保持 JSON 有效的前提下收缩长字符串叶子。

来源：_prune_old_tool_results Pass 3 + _truncate_tool_call_args_json。
cache_impact=invalidate：改写 assistant 消息的 tool_calls.arguments。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.context.config import cfg_int
from app.core.context.tiers import count_tokens
from app.plugins.contracts.context_policy import CACHE_INVALIDATE, STAGE_SEND, STAGE_UI, TierOutcome

ARGS_TRIGGER_CHARS = 400  # 参数 JSON 超过此长度才处理（与现状一致）
ARGS_HEAD_CHARS = 200  # 长字符串叶子保留长度


def _truncate_args_json(args: str, head_chars: int) -> str:
    from app.core.history_compactor import _truncate_tool_call_args_json

    return _truncate_tool_call_args_json(args, head_chars)


class ArgsTruncateTier:
    id = "args_truncate"
    label = "工具调用参数截断"
    order = 50
    stages = frozenset({STAGE_SEND, STAGE_UI})
    cache_impact = CACHE_INVALIDATE

    def should_apply(self, view) -> bool:
        return any(
            m.get("role") == "assistant"
            and isinstance(m.get("tool_calls"), list)
            and any(
                isinstance(tc, dict)
                and isinstance((tc.get("function") or {}).get("arguments"), str)
                and len(tc["function"]["arguments"]) > ARGS_TRIGGER_CHARS
                for tc in m["tool_calls"]
            )
            for m in view.messages
        )

    def apply(self, view) -> TierOutcome:
        head = cfg_int("args_head_chars", ARGS_HEAD_CHARS)
        result: List[Dict[str, Any]] = []
        changed = 0
        for m in view.messages:
            if m.get("role") != "assistant" or not isinstance(m.get("tool_calls"), list):
                result.append(m)
                continue
            new_tcs = []
            hit = False
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    new_tcs.append(tc)
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    new_tcs.append(tc)
                    continue
                args = fn.get("arguments")
                if not isinstance(args, str) or len(args) <= ARGS_TRIGGER_CHARS:
                    new_tcs.append(tc)
                    continue
                new_args = _truncate_args_json(args, head)
                if new_args != args:
                    hit = True
                    new_tcs.append({**tc, "function": {**fn, "arguments": new_args}})
                else:
                    new_tcs.append(tc)
            if hit:
                result.append({**m, "tool_calls": new_tcs})
                changed += 1
            else:
                result.append(m)
        if changed == 0:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无超长参数")
        saved = view.used_tokens - count_tokens(result)
        return TierOutcome(
            messages=result, saved_tokens=max(0, saved), note=f"截断 {changed} 条工具调用参数"
        )
