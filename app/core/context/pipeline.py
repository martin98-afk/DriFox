# -*- coding: utf-8 -*-
"""上下文 cascade 编排器 — 三个 stage 共用同一条降级链。

编排规则（spec §4.6）：
  1. 按 order 升序跑，trigger 不过则跳过
  2. 每跑一层重新度量，used <= target_tokens 即 break（达标即停）
  3. 连续 TIER_FAILURE_LIMIT 次无收益 → 熔断该层（本轮后续跳过）
  4. tier 抛错 → 记录 TierStat(skipped=True) 并继续下一层，不阻断发送
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.core.context.view import ContextView, TierStat
from app.core.model_capabilities import resolve_context_limit
from app.plugins.contracts.context_policy import (
    STAGE_INGEST,
    STAGE_SEND,
    STAGE_UI,
    TIER_FAILURE_LIMIT,
    ContextTier,
)


def _resolve_registry():
    from app.plugins.registries.context_policy_registry import ContextPolicyRegistry

    return ContextPolicyRegistry.get_instance()


class ContextPipeline:
    """上下文 cascade 编排器（无状态，可随时实例化）"""

    def __init__(self, registry=None) -> None:
        # registry 可注入（测试 / 隔离场景）；不传则用全局单例
        self._registry = registry

    @property
    def _reg(self):
        return self._registry if self._registry is not None else _resolve_registry()

    # ---------- 三个 stage 入口 ----------

    def ingest_tool_results(
        self,
        tool_results: List[Dict[str, Any]],
        llm_config: Dict[str, Any],
        session_id: str = "",
    ) -> List[Dict[str, Any]]:
        """ingest stage：工具结果入库前（允许落盘副作用）。"""
        # 会话归属下传：落盘类 tier（如 tool_offload）按 session 隔离
        # 落盘目录与冻结表。duck-typing 传参，不约束无会话语义的 tier。
        for tier in self._chain(STAGE_INGEST):
            set_sid = getattr(tier, "set_session_id", None)
            if callable(set_sid):
                try:
                    set_sid(session_id)
                except Exception as e:
                    logger.warning(f"[Context] tier '{tier.id}' set_session_id 失败: {e}")
        view = self._build_view(tool_results, llm_config, STAGE_INGEST)
        view = self.run(view)
        return view.messages

    def project_for_send(
        self,
        messages: List[Dict[str, Any]],
        llm_config: Dict[str, Any],
        system_content: str = "",
        state: Optional[Dict[str, Any]] = None,
        cache: Optional[Dict[str, Any]] = None,
    ) -> ContextView:
        """send stage：组装发给 LLM 的上下文（禁止副作用）。"""
        view = self._build_view(messages, llm_config, STAGE_SEND, system_content=system_content)
        if state:
            view.compaction_state = state
        if cache:
            view.compaction_cache = cache
        return self.run(view)

    def project_for_ui(
        self,
        messages: List[Dict[str, Any]],
        llm_config: Dict[str, Any],
        system_content: str = "",
    ) -> ContextView:
        """ui stage：UI 用量估算（禁止副作用，只读）。"""
        view = self._build_view(messages, llm_config, STAGE_UI, system_content=system_content)
        return self.run(view)

    # ---------- 核心 cascade ----------

    def run(self, view: ContextView) -> ContextView:
        for tier in self._chain(view.stage):
            try:
                if not tier.should_apply(view):
                    continue
            except Exception as e:
                logger.warning(f"[Context] tier '{tier.id}' trigger 抛错，跳过: {e}")
                continue

            before = view.used_tokens
            try:
                outcome = tier.apply(view)
            except Exception as e:
                logger.exception(f"[Context] tier '{tier.id}' 执行抛错，跳过: {e}")
                view.record(
                    TierStat(
                        tier_id=tier.id,
                        label=getattr(tier, "label", tier.id),
                        before_tokens=before,
                        after_tokens=before,
                        note=f"抛错跳过: {e}",
                        cache_impact=getattr(tier, "cache_impact", ""),
                        skipped=True,
                    )
                )
                continue

            if not outcome.applied or outcome.messages is None:
                continue

            view.replace_messages(outcome.messages)
            after = view.used_tokens
            view.record(
                TierStat(
                    tier_id=tier.id,
                    label=getattr(tier, "label", tier.id),
                    before_tokens=before,
                    after_tokens=after,
                    saved_tokens=outcome.saved_tokens,
                    cache_impact=getattr(tier, "cache_impact", ""),
                    note=outcome.note,
                )
            )

            if view.used_tokens <= view.target_tokens:
                break  # 达标即停

            if outcome.saved_tokens <= 0:
                n = view.note_failure(tier.id)
                if n >= TIER_FAILURE_LIMIT:
                    logger.warning(f"[Context] tier '{tier.id}' 连续 {n} 次无收益，本轮后续跳过（熔断）")
                    break
            else:
                view.clear_failure(tier.id)
        return view

    # ---------- 内部 ----------

    def _fallback_budget(self, llm_config: Dict[str, Any]) -> Tuple[int, int]:
        """常数级兜底预算（registry 无 resolver / 解析抛错时）。

        只保留「上下文 − 输出预留」与固定达标比，不复刻插件内的
        系统提示挤压等策略 —— 插件在时策略归插件，插件不在时够用即可。
        """
        try:
            ctx = resolve_context_limit(llm_config or {})
        except Exception:
            ctx = 128000
        budget = max(500, ctx - 4096)
        return budget, max(1, int(budget * 0.6))

    def _chain(self, stage: str) -> List[ContextTier]:
        try:
            chain = self._reg.resolve_chain(stage)
        except Exception as e:
            logger.warning(f"[Context] 注册解析失败，按空链处理: {e}")
            return []
        if not chain and self._registry is None:
            logger.warning("[Context] 未加载 system-context 插件，上下文降级链为空（全量发送）")
        return chain

    def _build_view(
        self,
        messages: List[Dict[str, Any]],
        llm_config: Dict[str, Any],
        stage: str,
        system_content: str = "",
    ) -> ContextView:
        resolver = None
        try:
            resolver = self._reg.get_budget_resolver()
        except Exception:
            resolver = None
        if resolver is not None:
            try:
                result = resolver.resolve(llm_config or {}, system_content)
                budget, target = result.budget, result.target_tokens
            except Exception as e:
                logger.warning(f"[Context] 预算解析失败，回退常数兜底: {e}")
                budget, target = self._fallback_budget(llm_config)
        else:
            budget, target = self._fallback_budget(llm_config)
        return ContextView(
            messages=list(messages),
            budget=budget,
            target_tokens=target,
            llm_config=llm_config or {},
            stage=stage,
        )
