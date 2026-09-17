# -*- coding: utf-8 -*-
"""order 40 — 长工具结果落盘：单结果 > 50K 或消息级 > 200K → 写盘 + 替换 content。

stage 仅 ingest：落盘按决策 D2 继续改存储，属入库副作用，不参与 send/ui 投影。
这同时解释了原 engine.py 为何能只调 prune 不调 persister —— 改后为声明式约束。

实现体：app.core.tool_result_persister.ToolResultPersister（行为零变化）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.context.config import cfg_int
from app.plugins.contracts.context_policy import CACHE_NONE, STAGE_INGEST, TierOutcome

# 落盘判定用的字符/token 换算（与 persister 内部口径一致，用于估算节省量）
_CHARS_PER_TOKEN = 4


class ToolOffloadTier:
    id = "tool_offload"
    label = "长工具结果落盘"
    order = 40
    stages = frozenset({STAGE_INGEST})
    cache_impact = CACHE_NONE

    def __init__(self, session_id: str = "") -> None:
        self._session_id = session_id
        self._persister = None

    def _get_persister(self):
        if self._persister is None:
            from app.core.tool_result_persister import ToolResultPersister

            self._persister = ToolResultPersister(session_id=str(self._session_id))
        return self._persister

    def should_apply(self, view) -> bool:
        single = cfg_int("single_result_threshold", 50_000)
        total = cfg_int("message_total_threshold", 200_000)
        acc = 0
        for r in view.messages:
            if self._skip(r.get("name", "")):
                continue
            c = r.get("content", "")
            if not isinstance(c, str):
                c = str(c) if c is not None else ""
            acc += len(c)
            if len(c) > single:
                return True
        return acc > total

    def apply(self, view) -> TierOutcome:
        results, stats = self._get_persister().process(view.messages)
        persisted = getattr(stats, "persisted_count", 0) or 0
        if not persisted:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无超大结果")
        saved_chars = getattr(stats, "saved_chars", 0) or 0
        return TierOutcome(
            messages=results,
            saved_tokens=max(0, saved_chars // _CHARS_PER_TOKEN),
            note=f"落盘 {persisted} 个工具结果",
        )

    @staticmethod
    def _skip(name: str) -> bool:
        """免落盘判定：先查工具注册声明，再回退模块兜底名单。"""
        if not name:
            return False
        from app.core.tool_result_persister import SKIP_TOOLS

        if name in SKIP_TOOLS:
            return True
        try:
            from app.tools.registry import ToolRegistry

            return ToolRegistry.get_instance().is_no_offload(name)
        except Exception:
            return False
