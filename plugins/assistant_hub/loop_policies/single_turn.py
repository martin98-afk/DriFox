# -*- coding: utf-8 -*-
"""单回合循环策略 — 助手记忆/经验整理专用（id="assistant_hub_single_turn"）。

背景
====
assistant_hub 的记忆传送带（compile）、Dream、经验反思都是"丢一段 prompt
进去、拿回一段纯文本"的工具型调用，本质是 **一次 API 调用**，不需要
工具迭代、不需要 Stop hook 续命、不需要重复循环检测。

原先走 OpenAI 兼容直连（core/llm_client.py 的 urllib 裸请求），与主对话
引擎完全脱钩：模型参数（思考/reasoning/温度/额外字段）、provider 适配、
重试/流式容错、序列化格式全都得自己维护一份 → 实际不可用。

现在改走主对话引擎（services["create_engine_session"]），本策略负责把引擎
"钳制"成单回合：max_rounds=1 + should_continue 恒 STOP，即使上游传了工具
schema 也只跑一轮就收。

激活方式
========
**不调用 set_active** —— LoopPolicyRegistry 的激活槽按 scope 全局共享，
set_active 会把主对话的策略一起改掉。正确用法是由引擎按 id 直接取对象：

    session = services["create_engine_session"](
        "assistant-hub",
        loop_policy_id=LOOP_POLICY_ID,   # 引擎级声明，零污染全局槽
    )

对应主程序改动：ConversationConfig.loop_policy_id → ConversationExecutor
→ OpenAIChatWorker._loop_policy()（未设置时回落 get_active()）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.plugins.contracts.loop_policy import LoopDecision, LoopPolicy, LoopState

LOOP_POLICY_ID = "assistant_hub_single_turn"


class SingleTurnLoopPolicy:
    """单回合策略：无论工具调用/Stop hook 注入/重复循环，一律一回合即停"""

    id = LOOP_POLICY_ID
    scope = "main"

    def should_continue(self, state: LoopState) -> LoopDecision:
        return LoopDecision.STOP

    def max_rounds(self, llm_config: Dict[str, Any]) -> Optional[int]:
        # 1 = 只允许一次 API 调用（worker 的 while 迭代计数，含流式 pending）
        return 1


def register(registry):
    """注册入口 — 被 runtime_component_loader.scan_roots 调用。

    只注册不激活：策略对象进 registry.policies() 表，由引擎按 id 取用。
    """
    registry.register(SingleTurnLoopPolicy())
