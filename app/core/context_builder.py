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

import re
from typing import Any, Dict, List, Optional

import anyio
from loguru import logger

from app.core.message_content import consolidate_messages
from app.core.model_capabilities import resolve_context_limit
from app.core.token_estimator import count_messages_tokens

# 预算分配常量
SYSTEM_PROMPT_RESERVE_RATIO = 0.15  # 预留 15% 给系统提示（含技能、记忆等）
MIN_HISTORY_BUDGET_RATIO = 0.60  # 历史消息分配占比 60%（配合 SOFT_LIMIT=84% → 触发于总体 ~50%）

# ============================================================
# 工具结果截断（S1，对齐 DSH tool-result-pruner 参数）
# 超过阈值的工具输出，保留头 + 尾，中间用省略标记替换 —— 避免超大
# 工具结果全量占用上下文 token。阈值随模型上下文容量动态放大。
# 仅在「组装发送给 LLM 的上下文」时截断，session 原始存储不受影响。
#
# 分层截断（避免"截断后 LLM 因信息缺失反复调用工具补查"）：
#   A. read 类文件内容：解析 #File: (Lines a-b of N) 元信息，省略标记
#      给出被截断区间的文件行号 + read 取回指引（一次精准补查）
#   B. 行式条目结果（grep/list/glob）：按行保留头尾条目（保索引），
#      省略标记给出条目区间 + 缩小范围重查指引
#   C. 通用自由文本：字符 head+tail + 缩小输出范围指引
# ============================================================
TOOL_RESULT_MAX_LEN = 8192  # 基础阈值（短上下文模型默认）
TOOL_RESULT_HEAD_KEEP = 4096  # 保留头部字符数
TOOL_RESULT_TAIL_KEEP = 1024  # 保留尾部字符数
TOOL_RESULT_ELLIPSIS = "\n...[工具结果过长，已截断中间 {dropped} 字符，头 {head} + 尾 {tail}]...\n"

# 不应截断的工具（结果小且语义完整，截断会破坏关键信息）
PRUNE_SKIP_TOOLS = frozenset({"question", "skill", "todoread"})

# read 工具输出头: #File: {path} (Lines {a}-{b} of {N})
_FILE_HEADER_RE = re.compile(r"^#File: (.+?) \(Lines (\d+)-(\d+) of (\d+)\)\s*$", re.MULTILINE)

# 行式条目结果标记（grep/list/glob 输出首行）
_LINE_INDEXED_MARKERS = ("匹配文件: ", "目录: ")


def resolve_tool_result_max_len(llm_config: Optional[Dict] = None) -> int:
    """动态工具结果截断阈值：按模型上下文容量缩放。

    - 上下文 <= 32K   -> 8192（基础）
    - 上下文 32K~64K  -> 12288
    - 上下文 64K~128K -> 16384
    - 上下文 >= 128K  -> 24576
    """
    base = TOOL_RESULT_MAX_LEN
    if not llm_config:
        return base
    try:
        context_limit = resolve_context_limit(llm_config)
    except Exception:
        return base
    if context_limit >= 128000:
        return base * 3
    if context_limit >= 64000:
        return base * 2
    if context_limit >= 32000:
        return int(base * 1.5)
    return base


def _is_line_indexed_result(content: str) -> bool:
    """识别行式条目结果（grep/list/glob）：首行带标记，后续按行输出条目。"""
    for line in content.split("\n")[:4]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_LINE_INDEXED_MARKERS):
            return True
        if "个文件:" in stripped:
            return True
    return False


def _prune_line_indexed(content: str, head_keep: int, tail_keep: int) -> Optional[str]:
    """行式条目结果按行截断：保留头尾条目，中间按行省略（保索引）。

    返回截断结果；行数不足时返回 None 交给通用截断。
    """
    lines = content.split("\n")
    total = len(lines)
    if total <= 6:
        return None
    avg_line_len = len(content) / max(1, total)
    head_lines = max(3, int(head_keep / max(1.0, avg_line_len)))
    tail_lines = max(2, int(tail_keep / max(1.0, avg_line_len)))
    if head_lines + tail_lines >= total:
        return None
    head = lines[:head_lines]
    tail = lines[-tail_lines:]
    dropped = total - head_lines - tail_lines
    marker = (
        f"\n...[结果共 {total} 行，已省略中间第 {head_lines + 1}-{total - tail_lines} 行"
        f"（{dropped} 个条目）。如需查看被省略内容，请重新调用工具并缩小范围"
        f"（更精确的 path/pattern/include）]...\n"
    )
    return "\n".join(head) + marker + "\n".join(tail)


def _prune_read_result(content: str, head_keep: int, tail_keep: int) -> Optional[str]:
    """read 类文件内容截断：省略标记给出被截断区间的文件行号 + 取回指引。"""
    m = _FILE_HEADER_RE.match(content)
    if not m:
        return None
    display = m.group(1)
    start_line, end_line, total_lines = int(m.group(2)), int(m.group(3)), int(m.group(4))
    dropped = len(content) - head_keep - tail_keep
    if dropped <= 0:
        return content[:head_keep]
    head = content[:head_keep]
    tail = content[-tail_keep:]
    # head 内完整内容行数（去掉 header 行）
    head_content_lines = max(0, head.count("\n") - 1)
    # tail 内行数（从尾部起）
    tail_content_lines = max(0, tail.count("\n"))
    trunc_start = start_line + head_content_lines
    trunc_end = end_line - tail_content_lines
    if trunc_start > trunc_end:
        trunc_start, trunc_end = trunc_end, trunc_start
    marker = (
        f"\n...[已截断中间约 {dropped} 字符（文件第 {trunc_start}-{trunc_end} 行，共 {total_lines} 行）。"
        f"如需查看该区间，可用 read 工具：read {display} startline={trunc_start} endline={trunc_end}]...\n"
    )
    return head + marker + tail


def _prune_generic(content: str, head_keep: int, tail_keep: int) -> str:
    """通用自由文本：字符 head+tail + 缩小输出范围指引。"""
    dropped = len(content) - head_keep - tail_keep
    if dropped <= 0:
        # 阈值配置异常（head+tail >= len），保底只留 head
        return content[:head_keep]
    head = content[:head_keep]
    tail = content[-tail_keep:]
    marker = TOOL_RESULT_ELLIPSIS.format(dropped=dropped, head=head_keep, tail=tail_keep)
    # 追加可操作指引：让 LLM 知道如何取回完整内容，避免盲目重查
    marker += "如需完整内容，可重新调用该工具并缩小输出范围（指定 path/pattern/行号）。"
    return head + marker + tail


def prune_tool_result(
    content: str,
    max_len: Optional[int] = None,
    head_keep: Optional[int] = None,
    tail_keep: Optional[int] = None,
    tool_name: str = "",
    llm_config: Optional[Dict] = None,
) -> str:
    """截断超长工具结果，按结果类型分层保留可操作信息。

    返回截断后的字符串；未超阈值、非字符串或受保护工具原样返回。
    省略标记内含被裁信息，供 token 计量（S2）量化截断收益。
    """
    if not isinstance(content, str) or not content:
        return content
    if tool_name in PRUNE_SKIP_TOOLS:
        return content
    limit = max_len if max_len is not None else resolve_tool_result_max_len(llm_config)
    if len(content) <= limit:
        return content
    head = head_keep if head_keep is not None else TOOL_RESULT_HEAD_KEEP
    tail = tail_keep if tail_keep is not None else TOOL_RESULT_TAIL_KEEP

    # B: 行式条目结果 → 按行保索引（条目区间 + 重查指引）
    if _is_line_indexed_result(content):
        pruned = _prune_line_indexed(content, head, tail)
        if pruned is not None:
            return pruned
    # A: read 文件结果 → 文件行号区间 + read 取回指引
    pruned = _prune_read_result(content, head, tail)
    if pruned is not None:
        return pruned
    # C: 通用自由文本
    return _prune_generic(content, head, tail)


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
                from loguru import logger

                logger.warning(f"[ContextBuilder] Token 缓存超限 (大小={len(self._system_tokens_cache)})")
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

        # 上下文压缩 - 使用分配器计算的预算
        budget = self._allocate_history_budget(full_system_content, llm_config)
        history_for_api, compaction_state, compaction_cache = anyio.run(
            anyio.to_thread.run_sync,
            lambda: self._compactor.compact(
                history_messages,
                budget,
                existing_cache=getattr(session, "compaction_cache", None),
                allow_llm_summary=allow_llm_summary,
            ),
        )
        session.set_compaction_state(compaction_state)
        session.set_compaction_cache(compaction_cache)

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
