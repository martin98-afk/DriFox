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

from collections import OrderedDict
from typing import Dict, Optional, Tuple

from loguru import logger

# [B3] hooks/UI 双投影统一快照 store：两侧（本模块 hook 签名缓存 + engine 的
# _ctx_usage_cache）键统一后共享同一份投影产物，消除「消息版本 bump 后两侧
# 各 miss、各跑一次全量 project_for_ui（8 层 tier + per_message_tokens 全量
# + tools tokens）」的结构性重复。键 = 两侧失效来源的语义并集（见
# _unified_snapshot_key）。线程面：hooks 侧 worker 线程、UI 侧后台线程+节流，
# OrderedDict 原子操作与现状同级（不加锁，项目惯例）。LRU 上限 64，
# 命中刷新位，未命中不刷位（与旧 _snap_cache 同法）。
_unified_snap_store: "OrderedDict" = OrderedDict()
_UNIFIED_STORE_MAX = 64


def _unified_snapshot_key(
    session,
    llm_config: Optional[Dict],
    backend=None,
    api_prompt_tokens: int = 0,
    from_api=None,
    budget_tokens: Optional[int] = None,
    ratio: Optional[float] = None,
) -> tuple:
    """统一快照键 = hooks/UI 两侧失效来源的语义并集。
    - usage.py 旧签名：(len, id(last), last_api_prompt_tokens)
    - engine.py 旧键：(id(session), _messages_version, model, api_prompt_tokens,
      from_api, len, hash(system_prompt), budget, ratio)
    budget/ratio 由 engine 侧写入时传入（已就地算好）；hooks 侧传 None 时
    经 backend.chat_engine._conversation_core.context_builder（公开 dataclass
    字段，与 engine 同源）就地求值，取不到时置 0/1.0——此时键与 engine 侧
    不同构，该轮退化为双 miss（与改造前行为等价，无错乱）。
    """
    msgs = getattr(session, "messages", None) or []
    model = str((llm_config or {}).get("模型名称", "gpt-4o") or "gpt-4o")
    if ratio is None:
        try:
            from app.core.modelmeta.provider_profile import resolve_token_ratio

            ratio = resolve_token_ratio(llm_config or {}, model)
        except Exception:
            ratio = 1.0
    if budget_tokens is None:
        budget_tokens = 0
        try:
            cb = getattr(
                getattr(getattr(backend, "chat_engine", None), "_conversation_core", None),
                "context_builder",
                None,
            )
            if cb is not None:
                budget_tokens = int(cb.get_context_budget(llm_config))
        except Exception:
            budget_tokens = 0
    return (
        str(getattr(session, "session_id", "") or ""),
        id(session),
        int(getattr(session, "_messages_version", 0) or 0),
        len(msgs),
        id(msgs[-1]) if msgs else 0,
        model,
        ratio,
        budget_tokens,
        int(api_prompt_tokens or 0),
        from_api,
        # 校准因子（多槽 LRU 必需）：calibration 学习写回 session 但不 bump
        # 消息版本，不进键会让旧条目在 LRU 中复活、返回校准前的旧估算
        # （test_calibration_learned_and_applied 抓到）。
        float(getattr(session, "token_calibration", 0) or 0),
        str(getattr(session, "token_calibration_model", "") or ""),
        hash(str(getattr(session, "system_prompt", "") or "")),
    )


def _store_snapshot(key: tuple, snapshot: Dict) -> None:
    """统一 store 写入 + LRU 淘汰（唯一写入口）。"""
    _unified_snap_store[key] = snapshot
    _unified_snap_store.move_to_end(key)
    if len(_unified_snap_store) > _UNIFIED_STORE_MAX:
        _unified_snap_store.popitem(last=False)


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

        # [B3] 统一键（含旧签名全部失效来源）：同会话同状态下重复调用（每
        # tool call / 每 assistant 消息各一次）直接复用 UI 侧已产出的快照，
        # 免 O(N) 全量重算；任一变化源变动即新键（自然失效）。
        api_tokens = int(getattr(session, "last_api_prompt_tokens", 0) or 0)
        from_api = getattr(session, "last_api_prompt_from_usage", None)
        key = _unified_snapshot_key(
            session, llm_config, backend=backend, api_prompt_tokens=api_tokens, from_api=from_api
        )
        cached = _unified_snap_store.get(key)
        if cached is not None:
            # 命中刷新 LRU 位；未命中不刷位（保持旧条目优先淘汰）
            _unified_snap_store.move_to_end(key)
            used = int(cached.get("used_tokens", 0) or 0)
            budget = int(cached.get("budget_tokens", 0) or 0)
            if used > 0 and budget > 0:
                return (used, budget)

        snap = {}
        if hasattr(backend, "get_context_usage_snapshot"):
            snap = (
                backend.get_context_usage_snapshot(
                    session,
                    llm_config,
                    api_prompt_tokens=api_tokens,
                    api_message_count=int(getattr(session, "last_api_message_count", 0) or 0),
                    # 三态透传：None = 未知（老记录无该标记）→ 引擎按"可用"处理
                    from_api=from_api,
                )
                or {}
            )
        used = int(snap.get("used_tokens", 0) or 0)
        budget = int(snap.get("budget_tokens", 0) or 0)
        if used > 0 and budget > 0:
            result = (used, budget)
            _store_snapshot(key, snap)
            return result
    except Exception as e:
        logger.warning(f"[ContextUsage] hook 快照口径失败，回退本地估算: {e}")

    # 回退：旧本地全量估算（原始消息不截断 + 完整窗口 limit）
    return _legacy_estimate(session, llm_config)


def _legacy_estimate(session, llm_config: Optional[Dict] = None) -> Tuple[int, int]:
    """旧口径兜底：count_messages_tokens(全量) + resolve_context_limit"""
    try:
        from app.core.infra.token_estimator import count_messages_tokens
        from app.core.modelmeta.provider_profile import resolve_token_ratio

        model = str((llm_config or {}).get("模型名称", "gpt-4") or "gpt-4")
        token_count = count_messages_tokens(
            session.messages,
            model=model,
            ratio=resolve_token_ratio(llm_config or {}, model),
        )
    except Exception:
        return 0, 0

    token_limit = 0
    try:
        from app.core.modelmeta.model_capabilities import resolve_context_limit

        token_limit = resolve_context_limit(llm_config or {})
    except Exception:
        pass
    return token_count, token_limit
