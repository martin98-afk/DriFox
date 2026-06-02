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
import hashlib
import re
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any

import orjson as json
from loguru import logger
from openai import OpenAI

from app.core.message_content import (
    consolidate_messages,
    content_to_text,
)
from app.core.model_capabilities import resolve_context_limit, resolve_max_output_tokens
from app.core.token_estimator import count_messages_tokens
from app.core.workers.error_handler import create_api_call_with_retry

# ========== 常量 ==========
MAX_HISTORY_SNIPPET_CHARS = 1200
RECENT_HISTORY_MIN_MESSAGES = 6
SOFT_LIMIT_RATIO = 0.84  # 触发压缩检查（历史预算的 84% = 总体的 ~42% 时触发）
TARGET_LIMIT_RATIO = 0.6  # 压缩目标（tail保留60% budget，留更多实际消息）
MIN_RECENT_TOKEN_RATIO = 0.5  # 最小保留比例（tail最少30% budget）
SUMMARY_OVERHEAD = 500  # 摘要基础开销

# ========== 安全保护常量 ==========
# 单条消息最大比例：超过此比例的消息内容会被截断
MAX_SINGLE_MESSAGE_RATIO = 0.15
# 紧急压缩目标：压缩结果必须控制在 budget * EMERGENCY_TARGET_RATIO 以内
EMERGENCY_TARGET_RATIO = 0.7
# 启发式摘要字符硬上限（无论budget是否有效，都应用此上限）
# 当压缩消息数较多时动态扩大
# 每条被压缩消息分配更多字符，保证摘要可读性
MAX_HEURISTIC_SUMMARY_CHARS = 55000
MAX_HEURISTIC_SUMMARY_CHARS_PER_MSG = 40  # 每条压缩消息额外允许的摘要字符
MAX_HEURISTIC_SUMMARY_CHARS_ABS = 200000  # 摘要绝对硬上限（基于预算动态计算，此值为安全阀）
# 工具结果内容最大保留字符数
MAX_TOOL_CONTENT_CHARS = 3000
# 工具配对保护导致tail跑飞时的硬限制倍数
MAX_TAIL_OVERFLOW_MULTIPLIER = 2.5

# ========== 工具保护配置 ==========
# 不应被压缩的工具列表（这些工具的内容需要完整保留）
PROTECTED_TOOLS = {"skill", "todowrite"}  # 可以添加更多工具

# ========== Hermes Agent 预剪枝常量 ==========
# 单图 token 估算（匹配 Claude Code 常量）
IMAGE_TOKEN_ESTIMATE = 1600
CHARS_PER_TOKEN = 4
IMAGE_CHAR_EQUIVALENT = IMAGE_TOKEN_ESTIMATE * CHARS_PER_TOKEN

# 图片类型集合
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})


# ----------------------------------------------------------------------------
# 以下辅助函数完整复刻自 Hermes Agent (NousResearch/hermes-agent)
# 完整移植了工具预剪枝、去重、图片处理逻辑
# ----------------------------------------------------------------------------

def _content_length_for_budget(raw_content: Any) -> int:
    """
    Return the effective char-length of a message's content for token budgeting.
    完整复刻自 Hermes Agent
    
    Plain strings: ``len(content)``. Multimodal lists: sum of text-part ``len(text)`` 
    plus a flat ``_IMAGE_CHAR_EQUIVALENT`` per image part.
    """
    if isinstance(raw_content, str):
        return len(raw_content)
    if not isinstance(raw_content, list):
        return len(str(raw_content or ""))
    total = 0
    for p in raw_content:
        if isinstance(p, str):
            total += len(p)
            continue
        if not isinstance(p, dict):
            total += len(str(p))
            continue
        ptype = p.get("type")
        if ptype in _IMAGE_PART_TYPES:
            total += IMAGE_CHAR_EQUIVALENT
        else:
            # text / input_text / tool_result-with-text / anything else with
            # a text field. Ignore the raw base64 payload inside image_url
            # dicts — dimensions don't matter, only whether it's an image.
            total += len(p.get("text", "") or "")
    return total


def _is_image_part(part: Any) -> bool:
    """True if ``part`` is a multimodal image content block."""
    if not isinstance(part, dict):
        return False
    return part.get("type") in _IMAGE_PART_TYPES


def _content_has_images(content: Any) -> bool:
    """True if a message's ``content`` is a multimodal list with image parts."""
    if not isinstance(content, list):
        return False
    return any(_is_image_part(p) for p in content)


def _strip_images_from_content(content: Any) -> Any:
    """
    Return a copy of ``content`` with every image part replaced by a short text placeholder.
    完整复刻自 Hermes Agent
    """
    if not isinstance(content, list):
        return content
    if not any(_is_image_part(p) for p in content):
        return content
    new_parts: List[Any] = []
    for p in content:
        if _is_image_part(p):
            new_parts.append({
                "type": "text",
                "text": "[Attached image — stripped after compression]",
            })
        else:
            new_parts.append(p)
    return new_parts


def _strip_historical_media(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Replace image parts in older messages with placeholder text.
    完整复刻自 Hermes Agent
    
    The anchor is the *last* user message that has any image content.
    Every message before that anchor gets its image parts replaced with a short placeholder
    so the outgoing request stops re-shipping the same multi-MB base-64 image blobs on every turn.
    """
    if not messages:
        return messages
    # Find the newest user message that carries at least one image part.
    anchor = -1
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        if _content_has_images(msg.get("content")):
            anchor = i
            break
    if anchor <= 0:
        # No image-bearing user message, or it's the very first message —
        # nothing before it to strip.
        return messages
    changed = False
    result: List[Dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if i >= anchor or not isinstance(msg, dict):
            result.append(msg)
            continue
        content = msg.get("content")
        if not _content_has_images(content):
            result.append(msg)
            continue
        new_msg = msg.copy()
        new_msg["content"] = _strip_images_from_content(content)
        result.append(new_msg)
        changed = True
    return result if changed else messages


def _truncate_tool_call_args_json(args: str, head_chars: int = 200) -> str:
    """
    Shrink long string values inside a tool-call arguments JSON blob 
    while preserving JSON validity.
    完整复刻自 Hermes Agent
    
    This helper parses the arguments, shrinks long string leaves inside 
    the parsed structure, and re-serialises. If the arguments are not valid 
    JSON to begin with, the original string is returned unchanged.
    """
    try:
        parsed = json.loads(args)
    except (json.JSONDecodeError, TypeError):
        return args

    def _shrink(obj: Any) -> Any:
        if isinstance(obj, str):
            if len(obj) > head_chars:
                return obj[:head_chars] + "...[truncated]"
            return obj
        if isinstance(obj, dict):
            return {k: _shrink(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_shrink(v) for v in obj]
        return obj

    shrunken = _shrink(parsed)
    # ensure_ascii=False preserves CJK/emoji instead of bloating with \uXXXX
    return json.dumps(shrunken).decode('utf-8')


def _summarize_tool_result(tool_name: str, tool_args: str, tool_content: str) -> str:
    """
    Create an informative 1-line summary of a tool call + result.
    完整复刻自 Hermes Agent
    
    Used during the pre-compression pruning pass to replace large tool outputs 
    with a short but useful description of what the tool did, rather than 
    a generic placeholder that carries zero information.
    """
    try:
        args = json.loads(tool_args) if tool_args else {}
    except (json.JSONDecodeError, TypeError):
        args = {}

    content = tool_content or ""
    content_len = len(content)
    line_count = content.count("\n") + 1 if content.strip() else 0

    if tool_name == "terminal" or tool_name == "bash" or tool_name == "shell":
        cmd = args.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        # 尝试从输出中提取 exit code
        exit_match = re.search(r'"exit_code"\s*:\s*(-?\d+)', content)
        exit_code = exit_match.group(1) if exit_match else "?"
        return f"[{tool_name}] ran `{cmd}` -> exit {exit_code}, {line_count} lines output"

    if tool_name in ("read", "read_file", "Read"):
        path = args.get("path", "?")
        offset = args.get("offset", 1)
        return f"[{tool_name}] read {path} from line {offset} ({content_len:,} chars)"

    if tool_name in ("write", "write_file", "Write", "edit"):
        path = args.get("path", "?")
        written_lines = args.get("content", "").count("\n") + 1 if args.get("content") else "?"
        return f"[{tool_name}] wrote to {path} ({written_lines} lines)"

    if tool_name in ("grep", "search_files", "grep_content", "search"):
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        target = args.get("target", "content")
        match_count = re.search(r'"total_count"\s*:\s*(\d+)', content)
        count = match_count.group(1) if match_count else "?"
        return f"[{tool_name}] {target} search for '{pattern}' in {path} -> {count} matches"

    if tool_name in ("browser_navigate", "browser_click", "browser_snapshot",
                     "browser_type", "browser_scroll", "browser_vision", "web"):
        url = args.get("url", "")
        ref = args.get("ref", "")
        detail = f" {url}" if url else (f" ref={ref}" if ref else "")
        return f"[{tool_name}]{detail} ({content_len:,} chars result)"

    if tool_name in ("web_search", "search"):
        query = args.get("query", "?")
        return f"[web_search] query='{query}' ({content_len:,} chars result)"

    if tool_name in ("web_fetch", "web_extract", "fetch"):
        urls = args.get("urls", [])
        url_desc = urls[0] if isinstance(urls, list) and urls else "?"
        if isinstance(urls, list) and len(urls) > 1:
            url_desc += f" (+{len(urls) - 1} more)"
        return f"[web_fetch] {url_desc} ({content_len:,} chars)"

    if tool_name in ("delegate_task", "subagent", "agent"):
        goal = args.get("goal", "")
        if len(goal) > 60:
            goal = goal[:57] + "..."
        return f"[{tool_name}] '{goal}' ({content_len:,} chars result)"

    if tool_name in ("execute_code", "run_code", "python"):
        code_preview = (args.get("code") or "")[:60].replace("\n", " ")
        if len(args.get("code", "")) > 60:
            code_preview += "..."
        return f"[execute_code] `{code_preview}` ({line_count} lines output)"

    if tool_name in ("skill_view", "skills_list", "skill_manage", "skill"):
        name = args.get("name", "?")
        return f"[{tool_name}] name={name} ({content_len:,} chars)"

    if tool_name in ("vision", "vision_analyze", "analyze_image", "image_understanding"):
        question = args.get("question", "")[:50]
        return f"[vision] '{question}' ({content_len:,} chars)"

    if tool_name in ("memory", "Todo"):
        action = args.get("action", "?")
        target = args.get("target", "?")
        return f"[{tool_name}] {action} on {target}"

    if tool_name == "Todo" or tool_name == "todowrite":
        return "[todo] updated task list"

    if tool_name in ("clarify", "ask", "question"):
        return "[clarify] asked user a question"

    # 通用回退
    first_arg = ""
    for k, v in list(args.items())[:2]:
        sv = str(v)[:40]
        first_arg += f" {k}={sv}"
    return f"[{tool_name}]{first_arg} ({content_len:,} chars result)"


def _safe_token_count(
    messages: List[Dict],
    model: str = "gpt-4",
) -> int:
    """
    安全的 token 计数（返回非负值）
    
    Hermes Agent 风格：
    - 返回 0 而不是负数
    - 对异常进行防御性处理
    """
    try:
        return max(0, count_messages_tokens(messages, model))
    except Exception:
        # 降级：按消息数量估算
        return len(messages) * 4


def _safe_subtract(a: int, b: int, fallback: int = 0) -> int:
    """
    安全的减法（返回非负值）
    
    Args:
        a: 被减数
        b: 减数
        fallback: 结果为负时的默认值
    """
    result = a - b
    return max(0, result, fallback)


def _prune_old_tool_results(
    self,
    messages: List[Dict[str, Any]],
    protect_tail_count: int,
    protect_tail_tokens: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], int]:
    """
    Replace old tool result contents with informative 1-line summaries.
    完整复刻自 Hermes Agent
    
    Instead of a generic placeholder, generates a summary like::
    [terminal] ran `npm test` -> exit 0, 47 lines output
    [read_file] read config.py from line 1 (3,400 chars)
    
    Also deduplicates identical tool results (e.g. reading the same file 5x 
    keeps only the newest full copy) and truncates large tool_call arguments 
    in assistant messages outside the protected tail.
    
    Walks backward from the end, protecting the most recent messages that fall 
    within ``protect_tail_tokens`` (when provided) OR the last 
    ``protect_tail_count`` messages (backward-compatible default).
    
    Returns (pruned_messages, pruned_count).
    """
    if not messages:
        return messages, 0

    result = [m.copy() for m in messages]
    pruned = 0

    # Build index: tool_call_id -> (tool_name, arguments_json)
    call_id_to_tool: Dict[str, tuple] = {}
    for msg in result:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    cid = tc.get("id", "")
                    fn = tc.get("function", {})
                    call_id_to_tool[cid] = (fn.get("name", "unknown"), fn.get("arguments", ""))
                else:
                    cid = getattr(tc, "id", "") or ""
                    fn = getattr(tc, "function", None)
                    name = getattr(fn, "name", "unknown") if fn else "unknown"
                    args_str = getattr(fn, "arguments", "") if fn else ""
                    call_id_to_tool[cid] = (name, args_str)

    # Determine the prune boundary
    if protect_tail_tokens is not None and protect_tail_tokens > 0:
        # Token-budget approach: walk backward accumulating tokens
        accumulated = 0
        boundary = len(result)
        min_protect = min(protect_tail_count, len(result))
        for i in range(len(result) - 1, -1, -1):
            msg = result[i]
            raw_content = msg.get("content") or ""
            content_len = _content_length_for_budget(raw_content)
            msg_tokens = content_len // CHARS_PER_TOKEN + 10
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    args = tc.get("function", {}).get("arguments", "")
                    msg_tokens += len(args) // CHARS_PER_TOKEN
            if accumulated + msg_tokens > protect_tail_tokens and (len(result) - i) >= min_protect:
                boundary = i
                break
            accumulated += msg_tokens
            boundary = i

        # Translate the budget walk into a "protected count"
        budget_protect_count = len(result) - boundary
        protected_count = max(budget_protect_count, min_protect)
        prune_boundary = len(result) - protected_count
    else:
        prune_boundary = len(result) - protect_tail_count

    # Pass 1: Deduplicate identical tool results.
    # When the same file is read multiple times, keep only the most recent
    # full copy and replace older duplicates with a back-reference.
    content_hashes: dict = {}  # hash -> (index, tool_call_id)
    for i in range(len(result) - 1, -1, -1):
        msg = result[i]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content") or ""
        # Multimodal content — dedupe by the text summary if available.
        if isinstance(content, list):
            continue
        if not isinstance(content, str):
            continue
        if len(content) < 200:
            continue
        h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
        if h in content_hashes:
            # This is an older duplicate — replace with back-reference
            result[i] = {**msg, "content": "[Duplicate tool output — same content as a more recent call]"}
            pruned += 1
        else:
            content_hashes[h] = (i, msg.get("tool_call_id", "?"))

    # Pass 2: Replace old tool results with informative summaries
    for i in range(prune_boundary):
        msg = result[i]
        if msg.get("role") != "tool":
            continue

        tool_call_id = msg.get("tool_call_id")
        if not tool_call_id or tool_call_id not in call_id_to_tool:
            continue

        tool_name, tool_args = call_id_to_tool[tool_call_id]
        if tool_name in PROTECTED_TOOLS:
            continue  # 保护特定工具不被剪枝

        content = msg.get("content", "")
        if not content:
            continue

        # 如果内容很短，不需要摘要
        if isinstance(content, str) and len(content) < 1000:
            continue

        # 生成信息丰富的 1-line 摘要
        summary = _summarize_tool_result(tool_name, tool_args, content)
        result[i] = {**msg, "content": summary}
        pruned += 1

    # Pass 3: Truncate large tool_call arguments in assistant messages outside tail
    for i in range(prune_boundary):
        msg = result[i]
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not tool_calls or not isinstance(tool_calls, list):
            continue

        msg_copy = msg.copy()
        new_tool_calls = []
        truncated = False
        for tc in tool_calls:
            if not isinstance(tc, dict):
                new_tool_calls.append(tc)
                continue
            fn = tc.get("function")
            if not fn or not isinstance(fn, dict):
                new_tool_calls.append(tc)
                continue
            args = fn.get("arguments")
            if not args or not isinstance(args, str):
                new_tool_calls.append(tc)
                continue
            if len(args) <= 400:
                new_tool_calls.append(tc)
                continue
            # Truncate while preserving JSON validity
            new_args = _truncate_tool_call_args_json(args, 200)
            if new_args != args:
                truncated = True
                new_fn = {**fn, "arguments": new_args}
                new_tc = {**tc, "function": new_fn}
                new_tool_calls.append(new_tc)
            else:
                new_tool_calls.append(tc)
        if truncated:
            msg_copy["tool_calls"] = new_tool_calls
            result[i] = msg_copy
            pruned += 1

    return result, pruned


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
        
        # ========== 迭代摘要支持 (参考 Hermes Agent) ==========
        self._previous_summary: Optional[str] = None  # 上一次压缩的摘要
        self._last_summary_error: Optional[str] = None  # 上次摘要失败的错误
        self._compression_count: int = 0  # 压缩次数统计
        self._last_compression_savings_pct: float = 100.0  # 上次压缩节省比例
        self._ineffective_compression_count: int = 0  # 无效压缩连续计数（反抖动）
        self._summary_failure_cooldown_until: float = 0.0  # 摘要失败冷却时间

    def on_session_reset(self) -> None:
        """重置会话状态（/new 或 /reset 时调用）"""
        self._previous_summary = None
        self._last_summary_error = None
        self._compression_count = 0
        self._last_compression_savings_pct = 100.0
        self._ineffective_compression_count = 0
        self._summary_failure_cooldown_until = 0.0

    def _prune_large_tool_outputs(
        self,
        messages: List[Dict],
        protect_tail_count: int,
        protect_tail_tokens: Optional[int] = None,
    ) -> tuple[List[Dict], int]:
        """
        预压缩剪枝：替换大型工具输出为摘要（无需 LLM）
        
        完整复刻 Hermes Agent 算法：
        1. 为每个 tool_call 建立索引
        2. 去重：相同内容的工具输出只保留最新一份完整拷贝
        3. 修剪：旧的大工具输出替换为信息丰富的 1-line 摘要
        4. 截断：过大的 tool_call 参数，保持 JSON 有效性
        5. 图片处理：移除旧的图片（只保留最新用户消息中的图片）
        
        Args:
            messages: 原始消息列表
            protect_tail_count: 最少保护尾部消息数
            protect_tail_tokens: 保护尾部的 token 预算（优先于此）
        
        Returns:
            (pruned_messages, pruned_count)
        """
        # 先移除旧图片
        pruned = _strip_historical_media(messages)
        # 然后进行工具预剪枝
        result, pruned_count = _prune_old_tool_results(
            self,
            pruned,
            protect_tail_count,
            protect_tail_tokens,
        )
        return result, pruned_count

    # ========== 公共接口 ==========

    def should_compact(self, messages: List[Dict], budget: int) -> bool:
        """
        判断是否需要压缩
        
        包含 Hermes Agent 的反抖动保护：
        如果最后两次压缩每次节省都小于 10%，跳过压缩避免无限循环。
        
        Args:
            messages: 消息列表
            budget: 可用 token 预算
            
        Returns:
            bool: 是否需要压缩
        """
        if not messages or budget <= 0:
            return False
        
        soft_limit = self._get_soft_limit(budget)
        current_tokens = count_messages_tokens(messages)
        
        if current_tokens <= soft_limit:
            return False
        
        # ========== 反抖动保护（Hermes Agent） ==========
        # 如果最近两次压缩效果都不好（每次节省 < 10%），跳过压缩
        if hasattr(self, '_ineffective_compression_count') and self._ineffective_compression_count >= 2:
            logger.warning(
                f"[Compactor] 压缩跳过 — 最近 {self._ineffective_compression_count} 次压缩每次节省 < 10%。"
                f"建议 /new 开始新会话，或手动 /compress。"
            )
            return False
        
        return current_tokens > soft_limit

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
        current_tokens = _safe_token_count(normalized)
        if current_tokens <= soft_limit:
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

        context_limit = resolve_context_limit(llm_config)
        max_output_tokens = resolve_max_output_tokens(llm_config)

        # O1/O3 模型需要更大的输出预留
        model_name = str(llm_config.get("model", "")).lower()
        reserved = min(800, max_output_tokens)
        if "o1" in model_name or "o3" in model_name:
            reserved = min(max_output_tokens, 32000)

        return max(500, context_limit - reserved)

    # ========== 内部方法 ==========

    def _get_soft_limit(self, budget: int) -> int:
        """软限制 = 84% 历史预算（对应总体 ~42% 触发压缩）"""
        return max(1, int(budget * SOFT_LIMIT_RATIO))

    def _get_target_limit(self, budget: int) -> int:
        """目标限制 = 60% 历史预算（对应总体 ~30%，压缩后有 ~12% 空间才再次触发）"""
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
        1. 预剪枝（Hermes Agent）：替换大型工具输出为摘要 + 去重 + 移除旧图片
        2. 从后向前尾保留（不拆分 tool 配对）
        3. 对被截断的部分生成摘要

        性能优化：
        - 预先计算所有消息的 token 数，避免循环内重复调用 count_messages_tokens
        - 使用 append + reverse 替代 list.insert(0)，将 O(n) 改为 O(1)
        - 预剪枝不需要 LLM，节省大量 token 预算
        """
        target_limit = self._get_target_limit(budget)
        min_recent_tokens = int(target_limit * MIN_RECENT_TOKEN_RATIO)

        # ========== 预压缩剪枝 (完整复刻 Hermes Agent) ==========
        # 在 LLM 摘要之前先做廉价预剪枝：
        # 1. 移除旧图片（只保留最新用户消息中的图片）
        # 2. 去重：相同工具输出只保留最新一份完整拷贝
        # 3. 用信息丰富的 1-line 摘要替换旧的大工具输出
        # 4. 截断过大的 tool_call 参数，保持 JSON 有效性
        pruned_messages, pruned_count = self._prune_large_tool_outputs(
            normalized,
            protect_tail_count=RECENT_HISTORY_MIN_MESSAGES,
            protect_tail_tokens=target_limit,
        )
        if pruned_count > 0:
            logger.info(f"[Compactor] Hermes 预剪枝完成: 修剪了 {pruned_count} 项")
        normalized = pruned_messages

        # ========== 性能优化：预先计算 token 数（安全检查）==========
        # 避免循环内重复调用 count_messages_tokens
        msg_tokens_list = []
        for msg in normalized:
            tokens = count_messages_tokens([msg])
            # 防御：每条消息至少有基本开销
            msg_tokens_list.append(max(4, tokens))

        # ========== 尾保留（从后向前） ==========
        # 性能优化：使用 append 后 reverse，替代 list.insert(0)
        recent_messages: List[Dict[str, Any]] = []
        recent_tokens = 0
        pending_tool_results: set = set()  # 等待找到对应 assistant 的 tool_call_id

        i = len(normalized) - 1
        # 单条消息 token 硬上限：超过此值的内容会被截断
        single_msg_max = max(500, int(budget * MAX_SINGLE_MESSAGE_RATIO))
        while i >= 0:
            msg = normalized[i]
            msg_tokens = msg_tokens_list[i]
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

            # ========== 单条消息截断保护 ==========
            # 避免一条超大 tool 结果撑爆整个 budget
            if msg_tokens > single_msg_max and role != "system":
                # 创建副本，不修改原始消息
                msg = dict(msg)
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > MAX_TOOL_CONTENT_CHARS:
                    # 工具结果：保留首尾
                    head_len = MAX_TOOL_CONTENT_CHARS // 2
                    tail_len = MAX_TOOL_CONTENT_CHARS // 2
                    # 如果是 tool 角色，限制更严格
                    max_chars = MAX_TOOL_CONTENT_CHARS if role == "tool" else MAX_HISTORY_SNIPPET_CHARS * 3
                    if len(content) > max_chars:
                        msg["content"] = content[:head_len] + "\n\n... [内容已截断，省略 " + str(len(content) - head_len - tail_len) + " 字符] ...\n\n" + content[-tail_len:]
                        msg["_truncated"] = True
                elif isinstance(content, list):
                    # 多段 content（如文本+图片url），截断超大文本段
                    truncated_blocks = []
                    for block in content if isinstance(content, list) else [content]:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if len(text) > MAX_TOOL_CONTENT_CHARS:
                                block = dict(block)
                                block["text"] = text[:MAX_TOOL_CONTENT_CHARS // 2] + "\n\n... [内容截断] ...\n\n" + text[-MAX_TOOL_CONTENT_CHARS // 2:]
                                msg["_truncated"] = True
                        truncated_blocks.append(block)
                    msg["content"] = truncated_blocks
                # 重新计算 token
                msg_tokens = count_messages_tokens([msg])

            # 添加到 recent（性能优化：append 后 reverse，替代 list.insert(0)）
            recent_messages.append(msg)
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

            # ========== tail 跑飞保护 ==========
            # 工具配对保护可能导致 tail 远超 target_limit。
            # 设置硬上限，超过时即使有 pending 配对也强制截断。
            hard_tail_cap = int(target_limit * MAX_TAIL_OVERFLOW_MULTIPLIER)
            if recent_tokens > hard_tail_cap and len(recent_messages) >= RECENT_HISTORY_MIN_MESSAGES * 2:
                logger.warning(
                    f"[Compactor] tail 达到硬上限: {recent_tokens} > {hard_tail_cap}，"
                    f"强制截断 (pending_tool_results={len(pending_tool_results)})"
                )
                break

            i -= 1

        # 性能优化：反转列表（因为是从后向前 append 的）
        recent_messages.reverse()

        # 没有需要压缩的内容
        if len(recent_messages) == len(normalized):
            return (
                recent_messages,
                self._make_state(original_count=len(normalized), kept_count=len(recent_messages)),
                self._make_cache(),
            )

        # 被截断的部分
        compacted = normalized[: len(normalized) - len(recent_messages)]

        # ========== 预压缩剪枝（参考 Hermes Agent）==========
        # 在 LLM 摘要前，先用廉价的方式替换大型工具输出为简短摘要
        # 这里全部都需要剪枝（因为已经被截断了），所以保护 0 条
        compacted, extra_pruned = self._prune_large_tool_outputs(
            compacted,
            protect_tail_count=0,
            protect_tail_tokens=None,
        )

        # ========== 生成摘要 ==========
        # 安全的预算计算（参考 Hermes Agent）
        # 确保不会因为计算错误导致负数
        # Hermes Agent 使用: summary_budget = target_tokens - recent_tokens - overhead
        remaining_for_summary = budget - recent_tokens - SUMMARY_OVERHEAD
        # 如果剩余空间不足，压缩可能效果不好，但仍尝试
        if remaining_for_summary <= 0:
            logger.warning(
                f"[Compactor] 剩余空间不足: budget={budget}, recent_tokens={recent_tokens}, "
                f"remaining={remaining_for_summary}，仍尝试压缩"
            )
            remaining_for_summary = 500  # 最小预算
        summary_budget_safe = remaining_for_summary
        compact_summary = self._summarize(
            compacted, 
            allow_llm=allow_llm_summary,
            budget=summary_budget_safe,
            compacted_count=len(compacted),
        )
        
        if not compact_summary:
            # 摘要失败，只保留 tail
            return (
                recent_messages,
                self._make_state(original_count=len(normalized), kept_count=len(recent_messages)),
                self._make_cache(),
            )

        summary_message = {"role": "user", "content": compact_summary}
        # 跳过开头的 tool 角色（配对保护遗留的孤立 tool 结果）
        first_non_tool = 0
        for idx, msg in enumerate(recent_messages):
            if msg["role"] != "tool":
                first_non_tool = idx
                break
        if first_non_tool > 0:
            # 需要重新计算 recent_tokens（因为 recent_messages 被截断了）
            recent_messages = recent_messages[first_non_tool:]
            recent_tokens = sum(count_messages_tokens([msg]) for msg in recent_messages)
        result_messages = [summary_message] + recent_messages
        current_tokens = count_messages_tokens(result_messages)
        kept_count = len(recent_messages)

        # ========== 压缩后预算校验 ==========
        # 如果压缩结果仍然超过 budget，执行紧急缩减
        if current_tokens > budget:
            logger.warning(
                f"[Compactor] 压缩结果仍超 budget: {current_tokens} > {budget}，"
                f"执行紧急缩减 (tail={kept_count}, summary={len(compacted)})"
            )
            result_messages, kept_count, summary_note = self._ensure_budget(
                result_messages, recent_messages, summary_message, budget,
                normalized_len=len(normalized), compacted_len=len(compacted)
            )
            # 重新计数
            current_tokens = count_messages_tokens(result_messages)
        else:
            summary_note = f"已压缩 {len(compacted)} 条较早消息"

        # 计算摘要实际占用的 token 数（确保非负）
        # 使用安全的 token 计数函数
        summary_tokens = _safe_token_count([summary_message])
        recent_tokens_safe = _safe_token_count(recent_messages)
        summary_count = _safe_subtract(current_tokens, recent_tokens_safe)

        # ========== 反抖动统计更新（Hermes Agent） ==========
        # 计算压缩节省比例，更新无效压缩计数
        original_tokens = _safe_token_count(normalized)
        if original_tokens > 0 and current_tokens > 0:
            savings_pct = ((original_tokens - current_tokens) / original_tokens) * 100
            self._last_compression_savings_pct = savings_pct
            # 如果这次压缩节省不到 10%，增加无效计数
            if savings_pct < 10:
                self._ineffective_compression_count += 1
                logger.warning(
                    f"[Compactor] 压缩效果不佳: 仅节省 {savings_pct:.1f}%，"
                    f"累计 {self._ineffective_compression_count} 次"
                )
            else:
                # 压缩有效，重置计数
                self._ineffective_compression_count = 0

        # 增加压缩次数统计
        self._compression_count += 1

        return (
            result_messages,
            self._make_state(
                active=True,
                source="history",
                kind="structured",
                original_count=len(normalized),
                summarized_count=len(compacted),
                kept_count=kept_count,
                summary_count=summary_count,
                note=summary_note,
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

    def _ensure_budget(
        self,
        result_messages: List[Dict],
        recent_messages: List[Dict],
        summary_message: Dict,
        budget: int,
        normalized_len: int,
        compacted_len: int,
    ) -> tuple:
        """
        紧急预算保障：当压缩结果仍超过 budget 时，
        迭代减少直到 fit。

        性能优化：
        - 使用字典缓存每条消息的 token 数，避免重复计算

        Returns:
            (result_messages, kept_count, note)
        """
        emergency_target = int(budget * EMERGENCY_TARGET_RATIO)
        kept_count = len(recent_messages)
        note = f"紧急缩减：原始 {normalized_len} 条"

        # 摘要保留底线：至少 2000 字符（约 500 tokens），
        # 保证压缩结果对早期对话仍有可用信息
        MIN_SUMMARY_CHARS = 2000

        result_tokens = count_messages_tokens(result_messages)

        # 策略1：截断超大摘要内容（保留底线）
        if result_tokens > emergency_target:
            summary_content = summary_message.get("content", "")
            if isinstance(summary_content, str) and len(summary_content) > MIN_SUMMARY_CHARS:
                # 仅当摘要远超底线时才截断
                target_chars = max(MIN_SUMMARY_CHARS, min(len(summary_content), MAX_HEURISTIC_SUMMARY_CHARS))
                if len(summary_content) > target_chars:
                    summary_message = dict(summary_message)
                    summary_message["content"] = (
                        summary_content[:target_chars // 2]
                        + "\n\n[摘要因预算限制截断]\n\n"
                        + summary_content[-target_chars // 2:]
                    )
                    result_messages = [summary_message] + recent_messages
                    result_tokens = count_messages_tokens(result_messages)
                    note = "紧急缩减：截断摘要"

        # 性能优化：预计算 tail 消息的 token 数
        tail_token_cache = {id(msg): count_messages_tokens([msg]) for msg in recent_messages}

        # 策略2：从 tail 中移除最旧的消息
        while result_tokens > emergency_target and len(result_messages) > 2:
            removed = result_messages.pop(1)  # index 0 是 summary
            result_tokens -= tail_token_cache.get(id(removed), 0)
            if removed in recent_messages:
                recent_messages.remove(removed)
                kept_count -= 1

        # 策略3：截断剩余 tail 中的工具消息内容
        if result_tokens > emergency_target:
            for idx, msg in enumerate(result_messages):
                if idx == 0:
                    continue
                if result_tokens <= emergency_target:
                    break
                if msg.get("role") == "tool":
                    content = msg.get("content", "")
                    tool_name = msg.get("name", "")
                    if isinstance(content, str) and len(content) > MAX_TOOL_CONTENT_CHARS:
                        new_msg = dict(msg)
                        new_msg["content"] = content[:MAX_TOOL_CONTENT_CHARS // 2] + "\n\n...[工具结果截断]...\n\n" + content[-MAX_TOOL_CONTENT_CHARS // 2:]
                        result_messages[idx] = new_msg
                        # 只更新当前消息的 token
                        tail_token_cache[id(new_msg)] = count_messages_tokens([new_msg])
                        result_tokens = sum(tail_token_cache.values()) + count_messages_tokens([summary_message])
                        note = f"紧急缩减：截断工具 {tool_name} 的内容"

        kept_count = len([m for m in recent_messages if m in result_messages])
        return result_messages, kept_count, note + f"，保留 {kept_count}/{compacted_len} 条"

    def _calculate_dynamic_summary_chars(self, compacted_count: int) -> int:
        """根据压缩消息数动态计算摘要字符上限"""
        return min(
            MAX_HEURISTIC_SUMMARY_CHARS_ABS,
            MAX_HEURISTIC_SUMMARY_CHARS + compacted_count * MAX_HEURISTIC_SUMMARY_CHARS_PER_MSG
        )

    def _summarize(
        self,
        messages: List[Dict],
        allow_llm: bool = True,
        budget: Optional[int] = None,
        compacted_count: int = 0,
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
        heuristic = self._summarize_heuristic(messages, budget)
        
        # ========== 动态字符上限（预算驱动）==========
        # 目标：让压缩后的总上下文（系统提示 + 摘要 + tail + 新消息）保持在 50%-60%
        # 通过可用预算（剩余可分配 token）反向计算摘要的合理字符上限
        if budget and budget > 0:
            # budget = 摘要可用的 token 预算
            # 字符上限 = 预算 × 4 字符/token × 90%（留余量）
            budget_based_max = int(budget * CHARS_PER_TOKEN * 0.9)
            max_chars = min(budget_based_max, MAX_HEURISTIC_SUMMARY_CHARS_ABS)
        else:
            # 无预算时回退固定上限
            max_chars = MAX_HEURISTIC_SUMMARY_CHARS
            if compacted_count > 0:
                max_chars = min(
                    MAX_HEURISTIC_SUMMARY_CHARS_ABS,
                    MAX_HEURISTIC_SUMMARY_CHARS + compacted_count * MAX_HEURISTIC_SUMMARY_CHARS_PER_MSG
                )
        if len(heuristic) > max_chars:
            logger.warning(
                f"[Compactor] 启发式摘要超长: {len(heuristic)} > {max_chars} "
                f"(compacted={compacted_count})，强制截断"
            )
            head = heuristic[:max_chars // 2]
            tail = heuristic[-max_chars // 2:]
            heuristic = head + "\n\n[摘要因长度限制截断]\n\n" + tail
        
        return heuristic

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

        req_kwargs = {
            "model": model,
            "messages": self._build_compaction_messages(messages),
            "stream": False,
            "max_tokens": max_tokens,
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
            if not resp.choices:
                logger.warning("[Compaction] API 返回空 choices，跳过摘要")
                return ""
            content = (resp.choices[0].message.content or "").strip()
            
            # 更新迭代摘要缓存
            self._previous_summary = content
            self._last_summary_error = None
            self._compression_count += 1
            
            return content
        except Exception as exc:
            logger.warning(f"[Compactor] LLM summarization failed, fallback to heuristic: {exc}")
            self._last_summary_error = str(exc)
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

        # ========== 迭代摘要支持 (参考 Hermes Agent) ==========
        # 如果有之前的摘要，将其包含在 prompt 中以便迭代更新
        prompt_parts = []
        if self._previous_summary:
            prompt_parts.append(
                "【上一次压缩的摘要（请在此基础上迭代更新）】\n"
                f"{self._previous_summary}\n\n"
                "请在此基础上更新摘要，添加新的内容，修正之前的错误。\n\n"
            )

        prompt_parts.append(
            "请压缩下面的较早对话，使后续模型可以继续当前编码任务。\n\n"
            "输出要求：\n"
            "1. 使用 Markdown。\n"
            "2. 优先保留：任务目标、已完成工作、关键文件/模块、关键工具结果、当前剩余问题。\n"
            "3. 不要只重复用户原始提问。\n"
            "4. 删除寒暄、重复探索、低价值调试细节。\n"
            "5. 如果信息不足，不要编造。\n\n"
            f"【待压缩对话】\n" + "\n".join(transcript_lines)
        )

        prompt = "\n".join(prompt_parts)

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

        # ========== 预处理: 内容清理（在所有消息上统一执行）==========
        cleaned_contents = []
        for idx, msg in enumerate(messages):
            raw_content = contents[idx] if idx < len(contents) else ""
            # 移除 <hook> 标签
            raw_content = re.sub(r'<hook[^>]*>.*?</hook>', '', raw_content, flags=re.DOTALL)
            # 移除 <think> 标签
            raw_content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL)
            # 移除 Tool args 内容
            raw_content = re.sub(r'Tool args:\s*\{[^}]*\}', '', raw_content)
            cleaned_contents.append(raw_content)

        for idx, msg in enumerate(messages):
            role = msg.get("role")
            content = cleaned_contents[idx] if idx < len(cleaned_contents) else ""
            
            # ========== 内容过滤 ==========
            # 1. 跳过失败的工具执行
            if role == "tool" and not msg.get("success"):
                continue
            # 2. 跳过纯工具调用的 assistant 消息（无有用文本，只有 tool_calls）
            if role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls and (not content or len(content) < 20):
                    continue
            # 3. 跳过 tool 消息中的无价值结果
            if role == "tool":
                empty_results = [
                    "(command completed with no output)",
                    "(completed with no output)",
                    "No results found",
                ]
                stripped = content.strip()
                if any(er in stripped for er in empty_results) and len(stripped) < 100:
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
                    target_total=int(budget *  content_ratios[idx]),
                    ratios=content_ratios if total_content_length > 1000 else None,
                )
                # 标记受保护的工具
                prefix = "[🔒] " if tool_name in PROTECTED_TOOLS else ""
                summary_lines.append(f"{prefix}# {tool_name}\nTool Args: {arguments}\nTool Res: {content}")

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
