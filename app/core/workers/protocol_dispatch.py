# -*- coding: utf-8 -*-
"""协议分派：llm_config + adapter flags → transport 目标（worker 与 subagent 共用）。

Phase 3（2026-11-07）把 Responses API 从 worker 原生通道迁到 transport 插件后，
「用哪个协议」收敛成一件事：解析 transport 目标。ChatWorker 与 SubAgentExecutor
各自维护 adapter 缓存，但映射规则必须一致，否则主链路与子智能体走不同协议。

映射优先级（高 → 低）：
1. 旧键 ``使用ResponsesAPI`` 显式设置（兼容纪律：不可被新键翻案）
2. 协议入口 ``responses`` / ``transport:<id>``（能力弹窗手选）
3. adapter flags：``protocol == "responses"`` 或 ``use_responses_api``
4. adapter flags 的 ``extra["transport"]``（插件自声明，可为 id 或实现对象）
5. 认证方式 ``anthropic``（x-api-key 族）→ ``anthropic_messages``（原生 messages 协议）
6. None（TransportRegistry 默认 openai_chat）

返回值：transport id（str）或 adapter 直接给的实现对象；None = 注册表默认。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# responses 协议的 transport 插件 id（system-transports/transports/openai_responses.py）
RESPONSES_TRANSPORT_ID = "openai_responses"
# Anthropic 原生 messages 协议（system-transports/transports/anthropic_messages.py）
ANTHROPIC_TRANSPORT_ID = "anthropic_messages"


def resolve_transport_target(llm_config: Dict[str, Any], flags: Any = None) -> Optional[Any]:
    """llm_config + ProtocolFlags → transport id / 实现对象 / None（默认）。"""
    cfg = llm_config or {}
    legacy = cfg.get("使用ResponsesAPI")
    if legacy is not None:
        return RESPONSES_TRANSPORT_ID if legacy else None
    entry = str(cfg.get("协议入口", "") or "").strip()
    if entry == "responses":
        return RESPONSES_TRANSPORT_ID
    if entry.startswith("transport:"):
        return entry.split(":", 1)[1]
    if flags is not None:
        if getattr(flags, "protocol", "openai") == "responses" or getattr(flags, "use_responses_api", False):
            return RESPONSES_TRANSPORT_ID
        extra = getattr(flags, "extra", None) or {}
        transport = extra.get("transport") if isinstance(extra, dict) else None
        if transport is not None:
            return transport
    # 认证方式 anthropic（x-api-key 族）→ 走 Anthropic 原生 messages 协议。
    # OpenAI 兼容层无法表达 prompt caching 断点（前缀缓存 opt-in，不发
    # cache_control 即命中恒 0），只有原生通道才发得出去。优先级低于「协议入口」
    # 手选：只提供 chat/completions 的中转可用手选覆盖本条。
    if str(cfg.get("认证方式", "") or "").strip().lower() == "anthropic":
        return ANTHROPIC_TRANSPORT_ID
    return None
