# -*- coding: utf-8 -*-
"""内置 tier（order 15）：长工具结果落盘。

单条结果超阈值、或单条消息内结果合计超阈值时，把内容写入磁盘，
上下文内替换为 <persisted-output> 块（含文件路径 + 预览），
模型可按需用 read 回读完整内容。

════ 顺序：为何在 tool_prune(20) 之前 ════
落盘可回读全文（信息保留），截断中间段永久丢失（有损）。单条 >50K
的结果无论先后都必然被处理，先落盘只多一次写盘；若排在截断之后，
内容已被截到 ≤24K，single 阈值(50K)永不触发，本层退化成死层。
先落盘后截断：>50K 落盘、24K~50K 截断，两层语义正交。

════ 缓存保护 ════
这是全 cascade 中对 prompt cache 最友好的一层：persister 内部的 _frozen
冻结表保证「同一 tool_call_id 每轮都用同一份预览字符串」，字节恒定，
因此本层实际 cache_impact=none —— 落盘只发生一次，后续每轮投影完全相同。

stage 仅 ingest：落盘按既有决策继续改存储（省内存与磁盘带宽），
属入库副作用，不参与 send/ui 投影。声明式约束替代了原先
「ui 估算处没写那行 persister 调用」的隐式保证。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.context.config import cfg_int, offload_skip_tools
from app.plugins.contracts.context_policy import CACHE_NONE, STAGE_INGEST, TierOutcome

_CHARS_PER_TOKEN = 4  # 节省字符 → token 的粗算口径（仅用于 stats 统计）


class ToolOffloadTier:
    id = "tool_offload"
    label = "长工具结果落盘"
    order = 15
    stages = frozenset({STAGE_INGEST})
    cache_impact = CACHE_NONE

    def __init__(self, session_id: str = "") -> None:
        self._session_id = session_id
        self._persister = None

    def set_session_id(self, session_id: str) -> None:
        """会话切换时更新落盘归属（pipeline 在 ingest 前调用）。

        persister 内含按会话隔离的落盘目录与冻结表，session 变化必须
        重建实例：跨会话复用会让冻结表与落盘目录语义失效。
        """
        if session_id == self._session_id:
            return
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
        skip = offload_skip_tools()
        acc = 0
        for r in view.messages:
            if self._skip(r.get("name", ""), skip):
                continue
            c = r.get("content", "")
            if not isinstance(c, str):
                c = str(c) if c is not None else ""
            acc += len(c)
            if len(c) > single:
                return True
        return acc > total

    def apply(self, view) -> TierOutcome:
        single = cfg_int("single_result_threshold", 50_000)
        total = cfg_int("message_total_threshold", 200_000)
        # 阈值打通：插件配置直达 persister 判定（process 不再用模块常量兜底）
        results, stats = self._get_persister().process(view.messages, single_threshold=single, total_threshold=total)
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
    def _skip(name: str, skip: frozenset = frozenset()) -> bool:
        """免落盘判定：工具自声明（metadata["no_offload"]）∪ 插件配置名单。"""
        if not name:
            return False
        try:
            from app.tools.registry import ToolRegistry

            if ToolRegistry.get_instance().is_no_offload(name):
                return True
        except Exception:
            pass
        return name in skip


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(ToolOffloadTier())
