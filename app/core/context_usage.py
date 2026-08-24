# -*- coding: utf-8 -*-
"""hook 注入专用的上下文用量计算 —— 与圆环（engine 快照）同源。

背景 bug（2026-08-24）：auto-compact hook 的 token_count 走
count_messages_tokens(session.messages)（原始未截断全量、不含 tools/system），
而圆环走 engine.get_context_usage_snapshot（工具结果截断投影 + API 精确值
优先 + tools schema）。重度工具会话下 hook 估算可达圆环实际值的 2~3 倍，
导致圆环显示 50% 时 hook 已判 80%+，自动压缩提前触发。

统一口径：hook 注入一律走 backend.get_context_usage_snapshot（与圆环同函数
同参数），快照不可用时回退旧本地估算。
"""

from typing import Dict, Optional, Tuple

from loguru import logger


def snapshot_usage_for_hooks(
    backend,
    session=None,
    llm_config: Optional[Dict] = None,
) -> Tuple[int, int]:
    """返回与上下文圆环同源的 (token_count, token_limit)。

    优先走 backend.get_context_usage_snapshot（内部委托 engine 快速路径：
    API 精确 prompt_tokens 优先 + 工具结果截断投影 + tools schema + 预算分母），
    保证 hook 触发占比与圆环显示一致；快照为空/异常时回退旧的本地全量估算。

    Args:
        backend: ChatBackend 实例（需提供 get_current_session /
            get_context_usage_snapshot / _get_model_config）
        session: 当前会话（不传则由 backend 取）
        llm_config: LLM 配置（不传则由 backend 取；与圆环刷新路径一致）

    Returns:
        (token_count, token_limit)，任一为 0 表示无法获取。
    """
    try:
        if session is None and hasattr(backend, "get_current_session"):
            session = backend.get_current_session()
        if session is None or not getattr(session, "messages", None):
            return 0, 0

        if llm_config is None:
            getter = getattr(backend, "_get_model_config", None)
            if callable(getter):
                llm_config = getter() or {}  # type: ignore[assignment]
        llm_config = llm_config or {}

        snap = {}
        if hasattr(backend, "get_context_usage_snapshot"):
            snap = (
                backend.get_context_usage_snapshot(
                    session,
                    llm_config,
                    api_prompt_tokens=int(getattr(session, "last_api_prompt_tokens", 0) or 0),
                    api_message_count=int(getattr(session, "last_api_message_count", 0) or 0),
                    from_api=bool(getattr(session, "last_api_prompt_from_usage", False)),
                )
                or {}
            )
        used = int(snap.get("used_tokens", 0) or 0)
        budget = int(snap.get("budget_tokens", 0) or 0)
        if used > 0 and budget > 0:
            return used, budget
    except Exception as e:
        logger.warning(f"[ContextUsage] hook 快照口径失败，回退本地估算: {e}")

    # 回退：旧本地全量估算（原始消息不截断 + 完整窗口 limit）
    return _legacy_estimate(session, llm_config)


def _legacy_estimate(session, llm_config: Optional[Dict] = None) -> Tuple[int, int]:
    """旧口径兜底：count_messages_tokens(全量) + resolve_context_limit"""
    try:
        from app.core.token_estimator import count_messages_tokens

        token_count = count_messages_tokens(session.messages)
    except Exception:
        return 0, 0

    token_limit = 0
    try:
        from app.core.model_capabilities import resolve_context_limit

        token_limit = resolve_context_limit(llm_config or {})
    except Exception:
        pass
    return token_count, token_limit
