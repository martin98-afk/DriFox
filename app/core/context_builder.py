# -*- coding: utf-8 -*-
"""
消息上下文构建器 - ContextBudgetAllocator

负责：
1. 组装 LLM 消息上下文（系统提示、技能、自定义提示、记忆等）
2. 预算分配决策（决定压缩策略、保留比例）
3. 委托 HistoryCompactor 执行实际压缩

设计原则：
- 预算决策集中在此处，调整 token 分配只需改这里
- 实际压缩逻辑委托给 HistoryCompactor，避免重复
"""

import anyio
from datetime import datetime
from typing import Dict, List, Optional, Any

from loguru import logger

from app.core.message_content import consolidate_messages
from app.core.token_estimator import count_messages_tokens
from app.utils.config import Settings


# 预算分配常量
SYSTEM_PROMPT_RESERVE_RATIO = 0.15  # 预留 15% 给系统提示（含技能、记忆等）
MIN_HISTORY_BUDGET_RATIO = 0.60  # 历史消息分配占比 60%（配合 SOFT_LIMIT=84% → 触发于总体 ~50%）


class ContextBudgetAllocator:
    """
    上下文预算分配器 - 负责任务：
    1. 计算可用预算并分配给各部分
    2. 组装完整消息上下文
    3. 委托压缩器执行实际压缩

    性能优化：
    - 系统提示 token 缓存：避免重复计算相同内容
    """

    def __init__(
        self,
        agent_manager,
        compactor=None,
        backend=None
    ):
        """
        Args:
            agent_manager: AgentManager 实例
            compactor: HistoryCompactor 实例（用于实际压缩）
            backend: 后端实例（用于获取记忆上下文）
        """
        self._agent_manager = agent_manager
        self.backend = backend
        self._compactor = compactor

        # ========== 性能优化：系统提示 token 缓存 ==========
        # 避免重复计算相同系统内容的 token 数
        self._system_tokens_cache: Dict[str, int] = {}

    def _get_cached_system_tokens(self, system_content: str) -> int:
        """
        获取系统提示的 token 数（带缓存）。

        Args:
            system_content: 系统提示内容

        Returns:
            token 数
        """
        # 使用内容 hash 作为缓存键
        cache_key = hash(system_content)
        if cache_key not in self._system_tokens_cache:
            self._system_tokens_cache[cache_key] = count_messages_tokens(
                [{"role": "system", "content": system_content}]
            )
            # 限制缓存大小
            if len(self._system_tokens_cache) > 64:
                # 清除最旧的条目
                self._system_tokens_cache.pop(next(iter(self._system_tokens_cache)))
        return self._system_tokens_cache[cache_key]

    def build_messages(
        self,
        session,
        llm_config: Dict,
        allow_llm_summary: bool = False,
        current_agent: Optional[str] = None,
    ) -> List[Dict]:
        """
        构建发送给 LLM 的消息列表。

        Args:
            session: ChatSession 实例
            llm_config: LLM 配置字典
            allow_llm_summary: 是否允许 LLM 摘要
            current_agent: 当前智能体名称

        Returns:
            消息列表
        """
        messages: List[Dict[str, Any]] = []

        # 获取系统提示
        full_system_prompt = self._agent_manager.get_agent_system_prompt(
            current_agent, is_subagent_call=False
        )
        prompt_parts = [full_system_prompt]

        # 添加启用的技能内容（放在 system prompt 最后，可能变化但相对稳定）
        enabled_skills = Settings.get_instance().llm_enabled_skills.value
        if enabled_skills and self._agent_manager:
            skills_content = self._agent_manager.get_enabled_skills_content(enabled_skills)
            if skills_content:
                prompt_parts.append(skills_content)

        # 【不】将记忆上下文放入 system prompt，避免动态内容破坏缓存
        # 记忆上下文将在 user message 中注入（见下方）
        # 使用单一 join 操作
        full_system_content = "\n\n".join(prompt_parts)

        messages.append({
            "role": "system",
            "content": full_system_content,
        })

        session.system_prompt = full_system_content

        # 处理历史消息
        normalized_session_messages = consolidate_messages(session.get_context_messages())
        latest_user_message = ""
        latest_user_timestamp = ""
        params = {}
        history_messages = normalized_session_messages

        if history_messages and history_messages[-1].get("role") == "user":
            latest_user_message = history_messages[-1].get("content", "")
            latest_user_timestamp = history_messages[-1].get("timestamp", "")
            params = history_messages[-1].get("params", {})
            history_messages = history_messages[:-1]

        # 上下文压缩 - 使用分配器计算的预算
        budget = self._allocate_history_budget(full_system_content, llm_config)
        history_for_api, compaction_state, compaction_cache = anyio.run(
            anyio.to_thread.run_sync,
            lambda: self._compactor.compact(
                history_messages,
                budget,
                existing_cache=getattr(session, "compaction_cache", None),
                allow_llm_summary=allow_llm_summary,
            )
        )
        session.set_compaction_state(compaction_state)
        session.set_compaction_cache(compaction_cache)

        # 过滤 system 消息并添加到结果
        filtered_history = [m for m in history_for_api if m.get("role") != "system"]
        messages.extend(filtered_history)

        # 添加用户消息
        user_msg = {"role": "user", "content": latest_user_message, "params": params}
        if latest_user_timestamp:
            user_msg["timestamp"] = latest_user_timestamp
        messages.append(user_msg)

        # 在最后一个用户消息前插入动态上下文（时间 + 记忆）
        # 不在 system prompt 中，避免动态内容破坏 API 缓存命中率
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        memory_context = self.backend.get_memory_context_string()

        dynamic_parts = [f"## 当前系统时间\n{current_time}"]
        if memory_context:
            dynamic_parts.append(memory_context)

        dynamic_context = "\n\n".join(dynamic_parts)
        messages[-1]["content"] = f"{dynamic_context}\n\n用户提问：{messages[-1]['content']}"

        return messages

    def _allocate_history_budget(self, system_content: str, llm_config: Dict) -> int:
        """
        分配历史消息预算。

        策略：
        1. 获取模型上下文上限
        2. 估算系统提示 token
        3. 剩余空间分配给历史消息

        Args:
            system_content: 组装后的系统提示内容
            llm_config: LLM 配置

        Returns:
            历史消息可用 token 数
        """
        # 委托给 compactor 计算总预算
        total_budget = self._compactor.get_budget(llm_config)

        # 估算系统提示 token（使用缓存）
        system_tokens = self._get_cached_system_tokens(system_content)

        # 动态分配：系统提示越大，历史预算越少
        system_ratio = system_tokens / max(total_budget, 1)
        if system_ratio > SYSTEM_PROMPT_RESERVE_RATIO:
            # 系统提示超出预期，压缩历史预算
            history_budget = int(total_budget * (1 - system_ratio))
        else:
            # 正常情况：至少保留 MIN_HISTORY_BUDGET_RATIO
            history_budget = int(total_budget * MIN_HISTORY_BUDGET_RATIO)

        # 预留动态上下文（build_messages 末尾注入的系统时间 + 记忆上下文）
        # 这部分 token 不在系统提示中，需从历史预算中扣除
        DYNAMIC_CONTEXT_RESERVE = 300  # 保守估算：时间戳(~15) + 8条记忆(~250)
        history_budget = max(500, history_budget - DYNAMIC_CONTEXT_RESERVE)

        return history_budget

    def get_context_budget(self, llm_config: Dict) -> int:
        """
        计算上下文预算（委托给 HistoryCompactor）。

        Args:
            llm_config: LLM 配置字典

        Returns:
            可用的上下文 token 数
        """
        return self._compactor.get_budget(llm_config)

    def get_budget_breakdown(self, llm_config: Dict, system_content: str = None) -> Dict[str, int]:
        """
        获取预算分解（用于 UI 显示）。

        Args:
            llm_config: LLM 配置
            system_content: 系统提示内容（可选）

        Returns:
            预算分解字典，包含 total、system、history 等
        """
        total = self._compactor.get_budget(llm_config)
        result = {
            "total": total,
            "system": 0,
            "history": total,
        }

        if system_content:
            system_tokens = self._get_cached_system_tokens(system_content)
            result["system"] = system_tokens
            result["history"] = max(500, total - system_tokens)

        return result

    def count_tokens(self, messages: List[Dict]) -> int:
        """计算消息列表的 token 数"""
        return count_messages_tokens(messages)