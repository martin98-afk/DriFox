# -*- coding: utf-8 -*-
"""
Token 估算模块 - 提供精确的 token 计数功能

支持多种模型编码:
- GPT-4, GPT-3.5 (cl100k_base)
- GPT-3 (r50k_base)
- Claude (cl100k_base)

自动降级到快速估算算法如果 tiktoken 不可用。
"""

import re
import threading
from functools import lru_cache
from typing import Dict, List, Optional

# tiktoken 优先，否则降级
try:
    import tiktoken
    _TIKTOKEN_AVAILABLE = True
except ImportError:
    _TIKTOKEN_AVAILABLE = False
    tiktoken = None


# 预编译正则表达式
_CHINESE_PATTERN = re.compile(r'[\u4e00-\u9fff]')

# 编码映射
ENCODING_MAPPING = {
    # OpenAI 模型
    "gpt-4": "cl100k_base",
    "gpt-3.5-turbo": "cl100k_base",
    "gpt-3.5": "cl100k_base",
    "gpt-35-turbo": "cl100k_base",
    # Claude 模型 (使用相同编码)
    "claude": "cl100k_base",
    "claude-2": "cl100k_base",
    "claude-3": "cl100k_base",
    "claude-3-5": "cl100k_base",
    # GPT-3 及更早
    "gpt-3": "r50k_base",
    "davinci": "r50k_base",
    # 默认
    "default": "cl100k_base",
}

# 模型 token 校正系数 — cl100k_base 编码器与实际模型 tokenizer 之间的补偿
# 系数 = 实际 API token / cl100k_base 估算值
# DeepSeek/Claude/Qwen/GLM 的中文分词效率与 OpenAI 不同，需要乘以校正系数
_MODEL_TOKEN_RATIOS: Dict[str, float] = {
    "deepseek": 1.05,   # DeepSeek tokenizer 对中文/代码比 cl100k_base 略多 5%
    "claude": 1.08,     # Anthropic tokenizer 差异更大
    "minimax": 1.04,    # MiniMax 近似 OpenAI
    "qwen": 1.07,       # 通义千问对中文更费 token
    "glm": 1.06,        # 智谱 tokenizer
    "kimi": 1.04,       # Moonshot 近似
    "gemini": 1.03,     # Google tokenizer 较高效
    "gpt-4": 1.00,      # OpenAI 原生，无需校正
    "gpt-3.5": 1.00,
    "gpt-3": 1.00,
    "default": 1.00,    # 未知模型不校正
}


def _get_model_token_ratio(model: str) -> float:
    """获取模型 token 校正系数"""
    model_lower = model.lower()
    for key, ratio in _MODEL_TOKEN_RATIOS.items():
        if key in model_lower:
            return ratio
    return _MODEL_TOKEN_RATIOS["default"]


def _get_encoding_name(model: str = "gpt-4") -> str:
    """根据模型名称获取编码名称"""
    model = model.lower()
    for key, encoding in ENCODING_MAPPING.items():
        if key in model:
            return encoding
    return ENCODING_MAPPING["default"]


@lru_cache(maxsize=8)
def _get_encoder(encoding_name: str):
    """获取编码器实例 (带缓存)"""
    if not _TIKTOKEN_AVAILABLE:
        return None

    try:
        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return None


def _fast_estimate_tokens(text: str) -> int:
    """
    快速估算算法 - 当 tiktoken 不可用时的降级方案

    基于 cl100k_base 类分词器的经验近似（覆盖 GPT / DeepSeek / Qwen / Claude 等的近似行为）:
    - 中文（CJK）约 1.2 token/字（实测区间 1.0~1.6，取略偏高的中值以避免低估触发上下文溢出）
    - 英文/代码/符号约 1 token / 4 字符

    ⚠️ 旧实现按「中文 2 字符 = 1 token」估算，对中文严重低估约 3 倍，
    导致本地上下文占用与 API 返回的 prompt_tokens 相差数万 token。现已修正。

    适用于 tiktoken 不可用时的降级。
    """
    if not text:
        return 0

    text_len = len(text)

    # 统计中文字符
    chinese_chars = len(_CHINESE_PATTERN.findall(text))
    non_chinese = text_len - chinese_chars

    # 中文 ~1.2 token/字；英文/其他 ~1 token/4 字符
    estimated = int(chinese_chars * 1.2 + non_chinese / 4.0)

    # 极短文本至少 1 token，避免 0 值
    return max(1, estimated)


def _encode_with_tiktoken(text: str, model: str = "gpt-4") -> List[int]:
    """使用 tiktoken 编码文本为 token IDs"""
    encoder = _get_encoder(_get_encoding_name(model))
    if encoder:
        return encoder.encode(text, disallowed_special=())
    return None


@lru_cache(maxsize=1024)
def estimate_tokens(text: str, model: str = "gpt-4") -> int:
    """
    估算文本的 token 数量
    
    Args:
        text: 要估算的文本
        model: 模型名称 (用于选择编码)
    
    Returns:
        token 数量
    
    Note:
        使用 lru_cache 缓存结果，相同文本只需计算一次。
        对于长文本或重复调用的场景效果显著。
    """
    if not text:
        return 0

    # 尝试使用 tiktoken
    encoder = _get_encoder(_get_encoding_name(model))
    if encoder:
        try:
            tokens = encoder.encode(text, disallowed_special=())
            return len(tokens)
        except Exception:
            pass

    # 降级到快速估算
    return _fast_estimate_tokens(text)


# ========== count_messages_tokens 脏标记缓存 ==========
# 基于 id+len+model 的缓存，避免同列表反复遍历
_token_count_cache_local = threading.local()


def _get_token_cache() -> dict:
    cache = getattr(_token_count_cache_local, "cache", None)
    if cache is None:
        cache = {"key": None, "result": 0}
        _token_count_cache_local.cache = cache
    return cache


def _set_token_cache(list_id: int, list_len: int, model: str, result: int):
    cache = _get_token_cache()
    cache["key"] = (list_id, list_len, model)
    cache["result"] = result
# ==========


def count_messages_tokens(
    messages: List[Dict],
    model: str = "gpt-4",
    tools: Optional[List[Dict]] = None
) -> int:
    """
    计算消息列表的总 token 数

    OpenAI 消息格式费用计算:
    - 每条消息: 4 tokens (overhead)
    - role 字段: + tokens
    - content: + tokens
    - tool_calls: + tokens
    - tool_call_id: + tokens

    Args:
        messages: 消息列表
        model: 模型名称
        tools: 工具定义列表

    Returns:
        总 token 数 (最小为 0)
    """
    if not messages:
        return 0

    # 脏标记缓存：仅对完整列表且 model 相同时命中（tools 几乎总为 None）
    cache_key = (id(messages), len(messages), model)
    _cache = _get_token_cache()
    if _cache["key"] == cache_key:
        return _cache["result"]

    total = 0

    # 消息 overhead
    total += len(messages) * 4

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")
        if role:
            total += estimate_tokens(str(role), model)

        # content 处理
        content = msg.get("content")
        if content is None:
            pass  # 无 content，跳过
        elif isinstance(content, str):
            if content:  # 确保非空字符串
                total += estimate_tokens(content, model)
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        total += estimate_tokens(text, model)
                elif item.get("type") == "image_url":
                    # 图片 token 估算 (简化版)
                    total += 85  # 图片基准开销

        # reasoning_content 处理 (DeepSeek V4 / GLM-5 thinking mode)
        reasoning = msg.get("reasoning_content")
        if reasoning and isinstance(reasoning, str):
            total += estimate_tokens(reasoning, model)

        # tool_calls 处理
        tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                total += 3  # tool_call overhead
                function = tool_call.get("function") or {}
                name = function.get("name") if isinstance(function, dict) else ""
                args = function.get("arguments") if isinstance(function, dict) else ""
                if name:
                    total += estimate_tokens(str(name), model)
                if args:
                    total += estimate_tokens(str(args), model)

        # tool_call_id 处理
        tool_call_id = msg.get("tool_call_id")
        if tool_call_id:
            total += estimate_tokens(str(tool_call_id), model)

    # 工具定义 tokens
    if tools:
        total += count_tools_tokens(tools, model)

    # 模型 tokenizer 校正系数（cl100k_base → 实际模型编码补偿）
    total = int(total * _get_model_token_ratio(model))

    # 确保返回值非负（防御性编程）
    result = max(0, total)
    _set_token_cache(id(messages), len(messages), model, result)
    return result


def count_tools_tokens(tools: List[Dict], model: str = "gpt-4") -> int:
    """
    计算工具定义的总 token 数
    
    工具格式 (参考 OpenAI 文档):
    - type: 8 tokens
    - function: 14 tokens
    - name: + tokens
    - description: + tokens
    - parameters: + tokens
    """
    if not tools:
        return 0

    total = 0

    for tool in tools:
        if not isinstance(tool, dict):
            continue

        tool_type = tool.get("type", "function")
        total += 8 if tool_type else 0

        function = tool.get("function", {})
        if function:
            total += 14

            name = function.get("name", "")
            if name:
                total += estimate_tokens(name, model)

            desc = function.get("description", "")
            if desc:
                total += estimate_tokens(desc, model)

            # parameters JSON string
            params = function.get("parameters")
            if params:
                params_str = str(params)
                total += estimate_tokens(params_str, model)

    return total


def count_response_tokens(
    prompt_tokens: int,
    model: str = "gpt-4",
    max_tokens: Optional[int] = None
) -> int:
    """
    计算响应可能的 token 数
    
    用于计算总费用/限制
    
    Args:
        prompt_tokens: 提示的 token 数
        model: 模型名称
        max_tokens: 最大生成 token 数
    
    Returns:
        估算的总 token 数
    """
    # 响应 overhead
    overhead = 3  # completion message overhead

    if max_tokens is not None:
        return prompt_tokens + overhead + max_tokens

    # 根据模型估算最大值
    limits = {
        "gpt-4": 8192,
        "gpt-4o": 16384,
        "gpt-3.5-turbo": 4096,
        "claude-3": 4096,
    }

    default_limit = limits.get(model.lower(), 4096)
    return prompt_tokens + overhead + default_limit


def truncate_text_to_token_limit(
    text: str,
    max_tokens: int,
    model: str = "gpt-4",
    suffix: str = "..."
) -> str:
    """
    将文本截断到指定的 token 限制
    
    Args:
        text: 原始文本
        max_tokens: 最大 token 数
        model: 模型名称
        suffix: 截断后缀
    
    Returns:
        截断后的文本
    """
    if not text:
        return text

    current_tokens = estimate_tokens(text, model)
    if current_tokens <= max_tokens:
        return text

    # 二分查找截断点
    left, right = 0, len(text)

    while left < right:
        mid = (left + right) // 2
        truncated = text[:mid]
        tokens = estimate_tokens(truncated, model)

        if tokens > max_tokens:
            right = mid - 1
        else:
            left = mid + 1

    result = text[:left]
    if suffix and left < len(text):
        # 保留 suffix 的空间
        suffix_tokens = estimate_tokens(suffix, model)
        available = max_tokens - suffix_tokens
        if available > 0:
            while estimate_tokens(result, model) > available:
                result = result[:-10]
            result += suffix
        else:
            result = suffix

    return result


class TokenCounter:
    """Token 计数器类 - 带状态和缓存"""

    def __init__(self, model: str = "gpt-4"):
        self.model = model
        self._cache: Dict[str, int] = {}
        self._cache_enabled = True
        self._miss_count = 0
        self._hit_count = 0

    def count(self, text: str, use_cache: bool = True) -> int:
        """计数 (带可选缓存)"""
        if not text:
            return 0

        if use_cache and self._cache_enabled:
            cache_key = hash(text)
            if cache_key in self._cache:
                self._hit_count += 1
                return self._cache[cache_key]
            self._miss_count += 1

        tokens = estimate_tokens(text, self.model)

        if use_cache and self._cache_enabled and self._miss_count < 100:
            self._cache[hash(text)] = tokens

        return tokens

    def count_messages(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]] = None
    ) -> int:
        """计数消息列表"""
        return count_messages_tokens(messages, self.model, tools)

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        self._miss_count = 0
        self._hit_count = 0

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率"""
        total = self._hit_count + self._miss_count
        if total == 0:
            return 0.0
        return self._hit_count / total

    def enable_cache(self, enabled: bool = True):
        """启用/禁用缓存"""
        self._cache_enabled = enabled


# 全局默认实例
_default_counter: Optional[TokenCounter] = None


def get_default_counter(model: str = "gpt-4") -> TokenCounter:
    """获取默认的 TokenCounter 实例"""
    global _default_counter
    if _default_counter is None or _default_counter.model != model:
        _default_counter = TokenCounter(model)
    return _default_counter
