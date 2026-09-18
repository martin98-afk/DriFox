# -*- coding: utf-8 -*-
"""内置 tier（order 20）：工具结果截断（S1）。

超阈值 tool 输出按类型分层截断，保留头 + 尾 + 可操作省略标记：
  - read 类：给出被截断区间的文件行号 + read 取回指引
  - 行式条目（grep/list/glob）：按行保索引 + 缩小范围重查指引
  - 通用文本：字符头尾 + 缩小输出范围指引

stage=all：ingest 也要截断，否则落盘判定会基于未截断内容（阈值失真）。

cache_impact=none：截断发生在入口，同一条 tool 结果每次投影的字节完全一致，
前缀稳定 —— 这是本层与其他层的关键区别。

order 20 排在 tool_dedupe(30) 之前：截断保留头尾 + 取回指引，信息损失可控；
去重整条替换为占位符，损失不可逆。若先去重，去重后常已达标而 break，
超长单条结果会全文进上下文（实测 30K 未被截断）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.context.config import cfg_int, prune_skip_tools
from app.core.context.tool_prune import prune_tool_result, resolve_tool_result_max_len
from app.core.infra.token_estimator import count_messages_tokens
from app.plugins.contracts.context_policy import ALL_STAGES, CACHE_NONE, TierOutcome


def _count_tokens(messages: List[Dict[str, Any]]) -> int:
    try:
        return count_messages_tokens(messages)
    except Exception:
        return 0


class ToolPruneTier:
    id = "tool_prune"
    label = "工具结果截断"
    order = 20
    # 设置页细项行副标题（component_items 走 AST 静态读取，仅支持字面量）
    description = "超阈值工具结果保留头尾、省略中段并附重查指引；阈值为 0 时按模型上下文容量自动定"
    stages = frozenset(ALL_STAGES)
    cache_impact = CACHE_NONE

    def should_apply(self, view) -> bool:
        limit = self._limit(view)
        skip = prune_skip_tools()
        for m in view.messages:
            if m.get("role") != "tool" or self._skip(m.get("name", ""), skip):
                continue
            c = m.get("content")
            if isinstance(c, str) and len(c) > limit:
                return True
        return False

    def apply(self, view) -> TierOutcome:
        limit = self._limit(view)
        skip = prune_skip_tools()
        result: List[Dict[str, Any]] = []
        changed = 0
        for m in view.messages:
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                name = m.get("name", "")
                if self._skip(name, skip):
                    result.append(m)
                    continue
                new_content = prune_tool_result(m["content"], max_len=limit, tool_name=name)
                if new_content != m["content"]:
                    m = {**m, "content": new_content}
                    changed += 1
            result.append(m)
        if changed == 0:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无超阈值结果")
        saved = view.used_tokens - _count_tokens(result)
        return TierOutcome(messages=result, saved_tokens=max(0, saved), note=f"截断 {changed} 条工具结果")

    def _limit(self, view) -> int:
        """截断阈值：插件配置优先（0/未配则按模型上下文容量动态放大）。"""
        override = cfg_int("tool_result_max_len", 0)
        return override if override > 0 else resolve_tool_result_max_len(view.llm_config)

    @staticmethod
    def _skip(name: str, skip: frozenset = frozenset()) -> bool:
        """免裁剪判定：工具自声明（metadata["no_prune"]）∪ 插件配置名单。"""
        if not name:
            return False
        try:
            from app.tools.registry import ToolRegistry

            if ToolRegistry.get_instance().is_no_prune(name):
                return True
        except Exception:
            pass
        return name in skip


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(ToolPruneTier())
