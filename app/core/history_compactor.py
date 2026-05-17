# -*- coding: utf-8 -*-
"""
历史消息压缩器 - HistoryCompactor

独立工具类，负责对话历史的上下文压缩：
1. 尾保留策略 + 工具调用配对保护
2. LLM 摘要 + 启发式截断回退
3. 缓存复用机制
4. 可在任何时机调用（对话开始前、工具迭代中）

使用方式：
    compactor = HistoryCompactor(get_model_config, agent_manager)
    
    # 判断是否需要压缩
    if compactor.should_compact(messages, budget):
        compressed, state, cache = compactor.compact(messages, budget)
        
    # 获取当前使用情况
    usage = compactor.get_usage(messages, budget)
"""
import re
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable

import orjson as json
from loguru import logger
from openai import OpenAI

from app.core.message_content import (
    consolidate_messages,
    content_to_text,
)
from app.core.retry_helper import create_api_call_with_retry
from app.core.token_estimator import count_messages_tokens, estimate_tokens
from app.core.provider_profile import get_provider_profile

# ========== 常量 ==========
MAX_HISTORY_SNIPPET_CHARS = 1200
RECENT_HISTORY_MIN_MESSAGES = 6
SOFT_LIMIT_RATIO = 0.7  # 触发压缩检查
TARGET_LIMIT_RATIO = 0.5  # 压缩目标
MIN_RECENT_TOKEN_RATIO = 0.85  # 最小保留 token 比例
SUMMARY_OVERHEAD = 500  # 摘要基础开销

# ========== 工具保护配置 ==========
# 不应被压缩的工具列表（这些工具的内容需要完整保留）
PROTECTED_TOOLS = {"skill"}  # 可以添加更多工具


class HistoryCompactor:
    """
    历史消息压缩器
    
    职责：
    1. 判断是否需要压缩
    2. 执行压缩（尾保留 + 摘要）
    3. 提供使用情况统计
    
    特点：
    - 独立于 ChatEngine，可在任意时机调用
    - 统一处理普通消息和工具调用（不拆分 tool 配对）
    - 支持缓存复用，避免重复压缩
    """

    def __init__(
        self,
        get_model_config: Callable[[], Dict[str, Any]],
        agent_manager: Any = None,
    ):
        self._get_model_config = get_model_config
        self._agent_manager = agent_manager
        
        # HTTP 客户端缓存（性能优化）
        self._compaction_http_client: Optional[OpenAI] = None
        self._compaction_cache_config: Optional[str] = None

    # ========== 公共接口 ==========

    def should_compact(self, messages: List[Dict], budget: int) -> bool:
        """
        判断是否需要压缩
        
        Args:
            messages: 消息列表
            budget: 可用 token 预算
            
        Returns:
            bool: 是否需要压缩
        """
        if not messages or budget <= 0:
            return False
        
        soft_limit = self._get_soft_limit(budget)
        return count_messages_tokens(messages) > soft_limit

    def compact(
        self,
        messages: List[Dict[str, Any]],
        budget: int,
        existing_cache: Optional[Dict[str, Any]] = None,
        allow_llm_summary: bool = True,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
        """
        执行压缩
        
        Args:
            messages: 原始消息列表
            budget: 可用 token 预算
            existing_cache: 已有的压缩缓存（用于复用）
            allow_llm_summary: 是否允许 LLM 摘要（False 则只用启发式截断）
            
        Returns:
            tuple:
                - 压缩后的消息列表
                - 压缩状态 (compaction_state)
                - 压缩缓存 (compaction_cache)
        """
        if not messages or budget <= 0:
            return [], self._make_state(), self._make_cache()

        soft_limit = self._get_soft_limit(budget)
        
        # 消息规范化
        normalized = consolidate_messages(messages)
        if not normalized:
            return [], self._make_state(), self._make_cache()

        # 未超软限制，无需压缩
        if count_messages_tokens(normalized) <= soft_limit:
            return (
                normalized,
                self._make_state(original_count=len(normalized), kept_count=len(normalized)),
                self._make_cache(),
            )

        # 尝试复用缓存
        cached = existing_cache or {}
        if cached.get("active") and cached.get("summary_message"):
            result = self._try_use_cache(normalized, cached, soft_limit)
            if result:
                return result

        # 执行压缩：尾保留 + 摘要
        return self._do_compact(normalized, budget, allow_llm_summary)

    def get_usage(
        self,
        messages: List[Dict],
        budget: int,
    ) -> Dict[str, Any]:
        """
        获取当前使用情况
        
        Args:
            messages: 消息列表
            budget: 可用预算
            
        Returns:
            dict: { used_tokens, budget_tokens, percent, compaction_state }
        """
        used_tokens = count_messages_tokens(messages)
        budget_tokens = max(1, budget)
        percent = max(0, min(100, int((used_tokens / budget_tokens) * 100)))
        
        return {
            "used_tokens": used_tokens,
            "budget_tokens": budget_tokens,
            "percent": percent,
        }

    def get_budget(self, llm_config: Optional[Dict] = None) -> int:
        """
        计算可用历史预算
        
        Args:
            llm_config: 模型配置（不传则用 get_model_config）
            
        Returns:
            int: 可用于历史的 token 预算
        """
        if llm_config is None:
            llm_config = self._get_model_config() or {}
        
        profile = get_provider_profile(llm_config)
        context_limit = int(profile.get("context_limit", 128000))

        # 支持多种配置字段名
        for key in ("context_limit", "context_window", "max_context_tokens", "最大Token"):
            value = llm_config.get(key)
            if value not in (None, ""):
                try:
                    context_limit = int(value)
                    break
                except (ValueError, TypeError):
                    logger.debug(f"Failed to parse context_limit from: {value}")

        max_output_tokens = llm_config.get("最大新Token", 
            llm_config.get("max_tokens",
                llm_config.get("max_output_tokens", 
                    profile.get("max_output_tokens", 4096)
                )
            )
        )
        try:
            max_output_tokens = int(max_output_tokens)
        except (ValueError, TypeError):
            logger.debug(f"Failed to parse max_output_tokens: {max_output_tokens}, using default")
            max_output_tokens = int(profile.get("max_output_tokens", 4096))

        # O1 模型需要更大的输出预留
        model_name = str(llm_config.get("model", "")).lower()
        reserved = min(800, max_output_tokens)
        if "o1" in model_name or "o3" in model_name:
            reserved = min(max_output_tokens, 32000)

        return max(500, context_limit - reserved)

    # ========== 内部方法 ==========

    def _get_soft_limit(self, budget: int) -> int:
        """软限制 = 70% 预算"""
        return max(1, int(budget * SOFT_LIMIT_RATIO))

    def _get_target_limit(self, budget: int) -> int:
        """目标限制 = 50% 预算"""
        return max(1, int(budget * TARGET_LIMIT_RATIO))

    def _make_state(
        self,
        active: bool = False,
        source: str = "history",
        kind: str = "",
        original_count: int = 0,
        summarized_count: int = 0,
        kept_count: int = 0,
        summary_count: int = 0,
        note: str = "",
    ) -> Dict[str, Any]:
        """构建压缩状态"""
        return {
            "active": bool(active),
            "source": source,
            "kind": "structured",
            "original_count": int(original_count or 0),
            "summarized_count": int(summarized_count or 0),
            "kept_count": int(kept_count or 0),
            "summary_count": int(summary_count or 0),
            "note": note or "",
        }

    def _make_cache(
        self,
        active: bool = False,
        kind: str = "",
        cutoff_index: int = 0,
        source_message_count: int = 0,
        summarized_count: int = 0,
        tail_count: int = 0,
        budget_tokens: int = 0,
        summary_message: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构建压缩缓存"""
        return {
            "active": bool(active),
            "kind": kind or "",
            "cutoff_index": int(cutoff_index or 0),
            "source_message_count": int(source_message_count or 0),
            "summarized_count": int(summarized_count or 0),
            "tail_count": int(tail_count or 0),
            "budget_tokens": int(budget_tokens or 0),
            "summary_message": dict(summary_message) if isinstance(summary_message, dict) else None,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if active else "",
        }

    def _try_use_cache(
        self,
        normalized: List[Dict],
        cached: Dict,
        soft_limit: int,
    ) -> Optional[tuple]:
        """尝试复用缓存"""
        cutoff_index = int(cached.get("cutoff_index", 0) or 0)
        if 0 < cutoff_index <= len(normalized):
            cached_messages = [
                cached.get("summary_message"),
                *normalized[cutoff_index:],
            ]
            if count_messages_tokens(cached_messages) <= soft_limit:
                summarized_count = int(cached.get("summarized_count", cutoff_index) or cutoff_index)
                tail_count = len(normalized) - cutoff_index
                return (
                    cached_messages,
                    self._make_state(
                        active=True,
                        source="history",
                        kind=str(cached.get("kind", "plain") or "plain"),
                        original_count=len(normalized),
                        summarized_count=summarized_count,
                        kept_count=tail_count,
                        summary_count=1,
                        note=f"复用已压缩摘要，覆盖 {summarized_count} 条较早消息",
                    ),
                    self._make_cache(
                        active=True,
                        kind=str(cached.get("kind", "plain") or "plain"),
                        cutoff_index=cutoff_index,
                        source_message_count=len(normalized),
                        summarized_count=summarized_count,
                        tail_count=tail_count,
                        budget_tokens=cached.get("budget_tokens", 0),
                        summary_message=cached.get("summary_message"),
                    ),
                )
        return None

    def _do_compact(
        self,
        normalized: List[Dict],
        budget: int,
        allow_llm_summary: bool,
    ) -> tuple:
        """
        执行压缩的核心逻辑：
        1. 从后向前尾保留（不拆分 tool 配对）
        2. 对被截断的部分生成摘要
        """
        target_limit = self._get_target_limit(budget)
        min_recent_tokens = int(target_limit * MIN_RECENT_TOKEN_RATIO)

        # ========== 尾保留（从后向前） ==========
        recent_messages: List[Dict[str, Any]] = []
        recent_tokens = 0
        pending_tool_results: set = set()  # 等待找到对应 assistant 的 tool_call_id

        i = len(normalized) - 1
        while i >= 0:
            msg = normalized[i]
            msg_tokens = count_messages_tokens([msg])
            role = msg.get("role")
            
            # 工具调用配对保护
            if role == "assistant":
                tool_calls = msg.get("tool_calls", [])
                for tc in tool_calls:
                    tool_id = tc.get("id")
                    if tool_id in pending_tool_results:
                        pending_tool_results.discard(tool_id)
            elif role == "tool":
                pending_tool_results.add(msg.get("tool_call_id"))

            # 添加到 recent（从后向前插入）
            recent_messages.insert(0, msg)
            recent_tokens += msg_tokens

            # 停止条件
            if (
                recent_messages
                and recent_tokens > target_limit
                and not pending_tool_results
                and len(recent_messages) >= RECENT_HISTORY_MIN_MESSAGES
                and recent_tokens >= min_recent_tokens
            ):
                break

            i -= 1

        # 没有需要压缩的内容
        if len(recent_messages) == len(normalized):
            return (
                recent_messages,
                self._make_state(original_count=len(normalized), kept_count=len(recent_messages)),
                self._make_cache(),
            )

        # 被截断的部分
        compacted = normalized[: len(normalized) - len(recent_messages)]

        # ========== 生成摘要 ==========
        summary_budget = budget - recent_tokens - SUMMARY_OVERHEAD
        compact_summary = self._summarize(
            compacted, 
            allow_llm=allow_llm_summary,
            budget=summary_budget if summary_budget > 0 else None,
        )
        
        if not compact_summary:
            # 摘要失败，只保留 tail
            return (
                recent_messages,
                self._make_state(original_count=len(normalized), kept_count=len(recent_messages)),
                self._make_cache(),
            )

        summary_message = {"role": "user", "content": compact_summary}
        for i, msg in enumerate(recent_messages):
            if msg["role"] != "tool":
                break
        recent_messages = recent_messages[i:]
        result_messages = [summary_message] + recent_messages
        current_tokens = count_messages_tokens(result_messages)
        kept_count = len(recent_messages)
        return (
            result_messages,
            self._make_state(
                active=True,
                source="history",
                kind="structured",
                original_count=len(normalized),
                summarized_count=len(compacted),
                kept_count=kept_count,
                summary_count=current_tokens - count_messages_tokens(recent_messages),
                note=f"已压缩 {len(compacted)} 条较早消息",
            ),
            self._make_cache(
                active=True,
                kind="structured",
                cutoff_index=len(compacted),
                source_message_count=len(normalized),
                summarized_count=len(compacted),
                tail_count=kept_count,
                budget_tokens=budget,
                summary_message=summary_message,
            ),
        )

    def _summarize(
        self,
        messages: List[Dict],
        allow_llm: bool = True,
        budget: Optional[int] = None,
    ) -> str:
        """
        生成摘要：优先 LLM，回退启发式
        """
        if not messages:
            return ""
        
        # 优先 LLM 摘要
        if allow_llm:
            llm_summary = self._summarize_with_llm(messages)
            if llm_summary:
                return llm_summary
        
        # 启发式截断（遗忘曲线）
        return self._summarize_heuristic(messages, budget)

    def _summarize_with_llm(self, messages: List[Dict]) -> str:
        """使用 LLM 生成摘要"""
        llm_config = self._get_model_config() or {}
        api_key = str(llm_config.get("API_KEY", "")).strip()
        base_url = llm_config.get("API_URL") or None
        auth_type = llm_config.get("认证方式", "bearer")
        
        if not api_key and auth_type != "none":
            return ""

        # 获取 compaction agent 配置
        compaction_config = {}
        if self._agent_manager and self._agent_manager.get_agent("compaction"):
            compaction_config = self._agent_manager.get_agent_config("compaction")

        model = str(compaction_config.get("model") or llm_config.get("模型名称", "gpt-4o"))
        
        # 动态 max_tokens
        max_tokens = self._get_summary_max_tokens(model)
        
        client = self._get_http_client(api_key, base_url, auth_type)

        COMPACTION_TIMEOUT = 60  # LLM压缩调用超时秒数，超时后回退到启发式压缩

        req_kwargs = {
            "model": model,
            "messages": self._build_compaction_messages(messages),
            "stream": False,
            "max_tokens": max_tokens,
            "timeout": COMPACTION_TIMEOUT,
        }
        
        temperature = compaction_config.get("temperature")
        if temperature is not None:
            req_kwargs["temperature"] = temperature
        top_p = compaction_config.get("top_p")
        if top_p is not None:
            req_kwargs["top_p"] = top_p

        try:
            def create_task():
                return client.chat.completions.create(**req_kwargs)

            resp = create_api_call_with_retry(client, create_task)
            content = (resp.choices[0].message.content or "").strip()
            return content
        except Exception as exc:
            logger.warning(f"[Compactor] LLM summarization failed, fallback to heuristic: {exc}")
            return ""

    def _get_summary_max_tokens(self, model: str) -> int:
        """根据模型大小动态调整摘要长度"""
        model_lower = model.lower()
        if any(s in model_lower for s in ["mini", "small", "flash", "lite"]):
            return 600
        elif any(s in model_lower for s in ["32k", "128k"]):
            return 2000
        return 1200

    def _get_http_client(self, api_key: str, base_url: str, auth_type: str) -> OpenAI:
        """获取或创建 HTTP 客户端（复用）"""
        config_key = f"{auth_type}:{api_key[:8] if api_key else 'none'}:{base_url or 'default'}"
        
        if (self._compaction_http_client is not None and 
            self._compaction_cache_config == config_key):
            return self._compaction_http_client
        
        self._compaction_http_client = OpenAI(
            api_key=api_key if api_key and auth_type != "none" else "dummy",
            base_url=base_url,
            timeout=60.0,
        )
        self._compaction_cache_config = config_key
        return self._compaction_http_client

    def _build_compaction_messages(self, messages: List[Dict]) -> List[Dict]:
        """构建压缩用的 prompt"""
        transcript_lines = []
        for msg in messages or []:
            role = msg.get("role", "unknown")
            content = content_to_text(msg.get("content", "")).strip()
            if not content:
                continue
            single_line = " ".join(content.split())
            if role == "tool":
                tool_name = msg.get("name") or msg.get("tool_call_id") or "tool"
                transcript_lines.append(f"[tool:{tool_name}] {single_line[:1200]}")
            else:
                transcript_lines.append(f"[{role}] {single_line[:1200]}")

        prompt = (
            "请压缩下面的较早对话，使后续模型可以继续当前编码任务。\n\n"
            "输出要求：\n"
            "1. 使用 Markdown。\n"
            "2. 优先保留：任务目标、已完成工作、关键文件/模块、关键工具结果、当前剩余问题。\n"
            "3. 不要只重复用户原始提问。\n"
            "4. 删除寒暄、重复探索、低价值调试细节。\n"
            "5. 如果信息不足，不要编造。\n\n"
            f"【待压缩对话】\n" + "\n".join(transcript_lines)
        )

        system_prompt = ""
        if self._agent_manager and self._agent_manager.get_agent("compaction"):
            system_prompt = self._agent_manager.get_agent_system_prompt("compaction")
        if not system_prompt:
            system_prompt = "你是一个上下文压缩专家，负责提炼后续继续执行编码任务所需的摘要。"

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    def _summarize_heuristic(
        self,
        messages: List[Dict],
        budget: Optional[int] = None,
    ) -> str:
        """
        启发式摘要：遗忘曲线自适应截断
        
        越旧的消息，保留越少
        """
        if "content" in messages[0] and messages[0]["content"].startswith("## Earlier Conversation Summary"):
            summary_lines = [messages[0]["content"]]
        else:
            summary_lines = [
                "## Earlier Conversation Summary",
                "以下是为节省上下文窗口而压缩的较早对话，请把它当作已确认的历史上下文继续工作。",
            ]
        total_messages = len(messages)
        if total_messages == 0:
            return ""

        # 计算每条消息的内容长度比例
        contents = []
        for msg in messages:
            content = content_to_text(msg.get("content", "")).strip()
            single_line = " ".join(content.split())
            contents.append(single_line)

        total_content_length = sum(len(c) for c in contents)
        content_ratios = [
            len(c) / total_content_length if total_content_length > 0 else 1 / total_messages
            for c in contents
        ]

        # 目标总长度
        target_total_length: Optional[int] = None
        if budget is not None and budget > 0:
            target_total_length = int(budget * 0.6)

        for idx, msg in enumerate(messages):
            role = msg.get("role")
            content = contents[idx] if idx < len(contents) else ""
            # 如果工具执行失败，则直接跳过
            if role == "tool" and not msg.get("success"):
                continue
            # 对于受保护的工具（如 skill），保留完整内容不截断
            is_protected_tool = False
            if role == "tool":
                tool_name = msg.get("name", "")
                if tool_name in PROTECTED_TOOLS:
                    is_protected_tool = True

            # 自适应截断：越旧截断越多（受保护工具除外）
            if not is_protected_tool:
                content = self._adaptive_truncate(
                    content,
                    position=idx,
                    total=total_messages,
                    target_total=target_total_length,
                    ratios=content_ratios if total_content_length > 1000 else None,
                )

            if role == "user":
                summary_lines.append(f"# User\n{content}")
            elif role == "assistant":
                summary_lines.append(f"# Assistant\n{content}")
            elif role == "tool":
                tool_name = msg.get("name", "")
                arguments = msg.get("arguments", "")
                arguments = self._adaptive_truncate(
                    arguments,
                    position=idx,
                    total=total_messages,
                    target_total=target_total_length,
                    ratios=content_ratios if total_content_length > 1000 else None,
                )
                # 标记受保护的工具
                prefix = "[🔒] " if tool_name in PROTECTED_TOOLS else ""
                summary_lines.append(f"{prefix}# {tool_name}\nTool args: {arguments}\nTool Res: {content}")

        return "\n".join(summary_lines)

    def _adaptive_truncate(
        self,
        content: str,
        position: int,
        total: int,
        target_total: Optional[int] = None,
        ratios: Optional[List[float]] = None,
    ) -> str:
        """
        自适应截断：基于遗忘曲线
        
        公式：keep_ratio = 0.2 + 0.5 * (position / total) ** 0.5
        - 最早的消息：约 20%
        - 最新的消息：约 70%
        """
        content_len = len(content)
        
        # 基础保留比例（遗忘曲线）
        position_ratio = position / max(1, total - 1)
        min_keep = 0.2
        max_keep = 0.7
        keep_ratio = min_keep + (max_keep - min_keep) * (position_ratio ** 0.5)
        
        # 动态目标长度
        if target_total is not None and target_total > 0:
            msg_ratio = ratios[position] if ratios and position < len(ratios) else (1 / total)
            avg_ratio = 1.0 / total
            ratio_factor = 0.5 + 0.5 * (msg_ratio / max(avg_ratio, 0.001))
            target_quota = (target_total / max(1, total)) * ratio_factor
            keep_length = int(target_quota)
        else:
            keep_length = int(content_len * keep_ratio)
        
        # 限制
        keep_length = min(keep_length, MAX_HISTORY_SNIPPET_CHARS)
        keep_length = max(keep_length, 20)
        
        if keep_length >= content_len:
            return content
        
        # 首尾保留策略
        head_ratio = 0.4
        head_length = int(keep_length * head_ratio)
        tail_length = int(keep_length * head_ratio)
        
        head = content[:head_length]
        tail = content[-tail_length:] if tail_length > 0 else ""
        
        return f"{head}...{tail}"
