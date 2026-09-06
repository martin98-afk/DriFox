# -*- coding: utf-8 -*-
"""内置兜底实现 — 当 system 插件整体被禁/加载失败时，registry 兜底回退的对象。

设计原则：
- 极简自洽：仅满足 Protocol 形状，不依赖任何 app.core.* 模块（避免循环引用）
- 行为退化：主链路不抛错为最高优先级，特性丢失可接受
- logger.warning 已在调用方（registry.get_active / resolve）触发；本模块只管实现
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.plugins.contracts.loop_policy import LoopDecision, LoopState
from app.plugins.contracts.message_serializer import (
    MessageSerializer,
    SerializeContext,
    SerializeResult,
)


class BuiltInDefaultLoopPolicy:
    """内置兜底循环策略 — 行为与 plugins/system/loop_policies/default.py 子集等价。

    差异：不实现 final_summary_prompt（子智能体用，主链路不调）。
    子智能体域缺插件时仍由本兜底顶替，子智能体的 max_rounds 上限由调用方配置补。
    """

    id = "__builtin__"
    scope = "main"  # 仅主智能体域兜底；子智能体域仍由 system 插件提供

    def should_continue(self, state: LoopState) -> LoopDecision:
        # 与 DefaultLoopPolicy 一致：tool_calls_found / stop_hook_injected 续循环；其余停
        if getattr(state, "tool_calls_found", False):
            return LoopDecision.CONTINUE
        if getattr(state, "stop_hook_injected", False):
            return LoopDecision.CONTINUE
        if getattr(state, "repetitive_loop_detected", False):
            return LoopDecision.CONTINUE
        return LoopDecision.STOP

    def max_rounds(self, llm_config: Dict[str, Any]) -> Optional[int]:
        # 现状 while 无上限——返回 None（不限）
        try:
            v = (llm_config or {}).get("最大循环轮数")
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    def final_summary_prompt(self) -> str:
        return ""


class BuiltInNoopStorageEngine:
    """内置兜底存储引擎 — 不持久化；返回安全空值，主链路不抛错。

    ⚠ 会话写入/读取都将丢失，仅在 system 插件整体被禁/加载失败的极端情况下生效。
    sqlite 系统插件正常加载时本类永远不会被构造（registry.get_active 先命中 sqlite）。
    """

    id = "__builtin_noop__"

    def save(self, session: Dict) -> bool:
        return False  # 写入失败（不抛错）

    def get(self, session_id: str) -> Optional[Dict]:
        return None

    def get_all(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        return []

    def get_by_project(self, project: str, limit: int = 100) -> List[Dict]:
        return []

    def get_projects(self) -> List[str]:
        return []

    def delete(self, session_id: str) -> bool:
        return False


class BuiltInPassthroughSerializer:
    """内置兜底序列化器 — 不做协议特判；serialize 透传 messages，responses 形态返回空。

    行为差异（与 openai 默认实现）：
    - 不区分 vision/tools/responses API 特判
    - 多模态图像/工具调用块直接当 dict 透传
    - responses 形态返回 (input_items=[], instructions="")
    仅在 system 插件整体被禁/加载失败时生效，主链路不至于抛错。
    """

    id = "__builtin_passthrough__"

    def serialize(self, messages: List[Dict[str, Any]], ctx: SerializeContext) -> SerializeResult:
        return SerializeResult(messages=list(messages), input_items=[], instructions="")

    def serialize_messages(self, messages: List[Dict[str, Any]], ctx: SerializeContext) -> List[Dict[str, Any]]:
        # 透传：只保留有 role 字段的项；其它异常形态让 LLM 端处理
        out: List[Dict[str, Any]] = []
        for m in messages or []:
            if isinstance(m, dict) and m.get("role"):
                out.append(m)
        return out

    def serialize_responses(self, messages: List[Dict[str, Any]], ctx: SerializeContext) -> tuple:
        # 兜底态不实现 responses 形态（openai 系统插件正常加载时本类不构造）
        return ([], "")
