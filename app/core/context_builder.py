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

from collections import OrderedDict
from hashlib import md5
from typing import Any, Dict, List, Optional

from app.core.context.tool_prune import (  # noqa: F401  向后兼容 re-export（实现已迁至 app/core/context/tool_prune.py）
    PRUNE_SKIP_TOOLS,
    TOOL_RESULT_ELLIPSIS,
    TOOL_RESULT_HEAD_KEEP,
    TOOL_RESULT_MAX_LEN,
    TOOL_RESULT_TAIL_KEEP,
    prune_tool_result,
    resolve_tool_result_max_len,
)
from app.core.message_content import consolidate_messages
from app.core.token_estimator import count_messages_tokens

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

    def __init__(self, agent_manager, compactor=None, backend=None):
        """
        Args:
            agent_manager: AgentManager 实例
            compactor: HistoryCompactor 实例（用于实际压缩）
            backend: 后端实例（用于获取记忆上下文）
        """
        self._agent_manager = agent_manager
        self.backend = backend
        self._compactor = compactor

        # ========== 性能优化：系统提示 token 缓存（LRU，md5 键跨进程稳定） ==========
        # 避免重复计算相同系统内容的 token 数；容量 64，最久未用者先逐
        self._system_tokens_cache: "OrderedDict[str, int]" = OrderedDict()

    def _get_cached_system_tokens(self, system_content: str) -> int:
        """
        获取系统提示的 token 数（带缓存）。

        Args:
            system_content: 系统提示内容

        Returns:
            token 数
        """
        # 使用内容 md5 摘要作为缓存键（跨进程稳定，避免 hash() 随机化与碰撞歧义）
        cache_key = md5(system_content.encode("utf-8")).hexdigest()
        cached = self._system_tokens_cache.get(cache_key)
        if cached is not None:
            self._system_tokens_cache.move_to_end(cache_key)
            return cached
        self._system_tokens_cache[cache_key] = count_messages_tokens(
            [{"role": "system", "content": system_content}]
        )
        # LRU 淘汰：容量超限逐最久未用条目
        if len(self._system_tokens_cache) > 64:
            self._system_tokens_cache.popitem(last=False)
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

        # 复用缓存的 system prompt：避免每次 tool iteration 都重新触发 BuildSystemPrompt hooks
        # 仅在 agent 切换或首次调用时重建
        # 构建 extra_context 传递给 BuildSystemPrompt hooks（项目信息等）
        extra_context: Dict[str, Any] = {}
        if self.backend:
            try:
                project_root = self.backend.tool_executor.get_workdir() if self.backend.tool_executor else ""
                project_name = self.backend.current_project or ""
                extra_context["project_root"] = project_root
                extra_context["project_name"] = project_name
                # 项目笔记由 read_project_notes hook 从本地 AGENTS.md 直接读取，不再预取
            except Exception:
                pass
        # 会话标识：assistant_hub 等插件按 session_id 做会话级助手覆盖
        sid = getattr(session, "session_id", "")
        if sid:
            extra_context["session_id"] = sid

        # 复用缓存的 system prompt：避免每次 tool iteration 都重新触发 BuildSystemPrompt hooks
        # 仅在 agent 切换或首次调用时重建
        cached_prompt = getattr(session, "system_prompt", None)
        if cached_prompt:
            # 直接复用缓存的 system prompt
            full_system_content = cached_prompt
        else:
            # 首次调用或 agent 切换，重新构建 system prompt
            full_system_prompt = self._agent_manager.get_agent_system_prompt(
                current_agent, is_subagent_call=False, extra_context=extra_context
            )
            full_system_content = full_system_prompt
            # 缓存起来，后续 tool iteration 直接复用
            session.system_prompt = full_system_content
            session._system_prompt_agent = current_agent

        messages.append(
            {
                "role": "system",
                "content": full_system_content,
            }
        )

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

        # 上下文压缩 —— 使用分配器计算的预算
        budget = self._allocate_history_budget(full_system_content, llm_config)
        # [PERF T33] 发送前处理缓存：consolidate + compact（含 token 估算）在
        # 同一消息版本 + 同一预算/模型/截断参数下结果完全确定，而一次发送会经
        # 多次调用（主发送 + 工具迭代回环）。长会话下这两步是 100-300ms 级开销。
        # 命中条件为 8 元组全等；任一变化即 miss 走原路径并回写。
        # S1 工具截断与 filtered_history 过滤留在缓存外（对 history_for_api 原地
        # 改 content，缓存持有的是 compact 输出，不能被下游改动污染）。
        tool_result_max_len = resolve_tool_result_max_len(llm_config)
        prep_key = (
            getattr(session, "_messages_version", -1),
            len(history_messages),
            history_messages[0].get("timestamp", "") if history_messages else "",
            history_messages[-1].get("timestamp", "") if history_messages else "",
            budget,
            llm_config.get("model", "") if isinstance(llm_config, dict) else "",
            tool_result_max_len,
            bool(allow_llm_summary),
        )
        cached_prep = getattr(session, "_send_prep_cache", None)
        if cached_prep is not None and cached_prep.get("key") == prep_key:
            history_for_api = cached_prep["history"]
            session.set_compaction_state(cached_prep["state"])
            session.set_compaction_cache(cached_prep["cache"])
        else:
            # compact 为纯同步函数，调用方（PreSendWorker / chat_worker /
            # gateway 主线程入口）已各自决定执行线程；此前的 anyio.run(to_thread) 包装
            # 只增加 event loop 创建与线程切换开销，不改阻塞语义（同步等待）。
            history_for_api, compaction_state, compaction_cache = self._compactor.compact(
                history_messages,
                budget,
                existing_cache=getattr(session, "compaction_cache", None),
                allow_llm_summary=allow_llm_summary,
                prenormalized=history_messages,
            )
            session.set_compaction_state(compaction_state)
            session.set_compaction_cache(compaction_cache)
            try:
                session._send_prep_cache = {
                    "key": prep_key,
                    "history": history_for_api,
                    "state": compaction_state,
                    "cache": compaction_cache,
                }
            except Exception:
                pass  # 缓存写回失败不影响发送（session 可能为只读桩）

        # 过滤旧 system 消息（保留 _hook_event 标记的系统消息，它们是由 hook 注入的动态上下文；
        # 同时保留 _compaction_summary 标记的压缩摘要，它是合法上下文，不应被当作"旧 system 消息"丢弃，
        # 否则压缩后的历史摘要会整体丢失，见 history_compactor.py issue #225 修复）
        filtered_history = [
            m
            for m in history_for_api
            if m.get("role") != "system" or m.get("_hook_event") or m.get("_compaction_summary")
        ]
        # S1: 工具结果截断 — 超阈值 tool 输出分层截断（阈值随模型上下文容量动态放大）。
        # 只在「发给 LLM 的上下文」层裁剪，session 原始存储不受影响（可追溯/可重放）。
        tool_result_max_len = resolve_tool_result_max_len(llm_config)
        for m in filtered_history:
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                m["content"] = prune_tool_result(
                    m["content"],
                    max_len=tool_result_max_len,
                    tool_name=m.get("name", ""),
                )
        messages.extend(filtered_history)

        # 添加用户消息
        user_msg = {"role": "user", "content": latest_user_message, "params": params}
        if latest_user_timestamp:
            user_msg["timestamp"] = latest_user_timestamp
        messages.append(user_msg)

        # 注意：所有动态上下文已迁移至 hook 体系：
        #   - BuildSystemPrompt: 行为约束、项目笔记、技能内容、子智能体列表
        #   - PreUserMessage: 长期记忆、系统时间、命令/技能提示词
        #   - SessionStart: 仅会话生命周期事件（其他插件可能监听）
        # system prompt 中不再有任何硬编码的上下文注入。
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

        # 预留动态上下文（已全部迁移至 hook 体系，无需再预留）
        # - 系统时间 → PostUserMessage hook
        # - 条目记忆 + 关键文档 → PreUserMessage hook（去重，只保留最新）
        # - 项目笔记 + 路径建议 + Worktree → SessionStart hook
        DYNAMIC_CONTEXT_RESERVE = 0
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


